# Output contracts: the two layers

agrep's surfaces live under exactly one of two contracts. The split exists
because the two consumers want opposite virtues: scripts and harnesses want
*predictability*; mid-task agents and humans want *the answer with the fewest
round trips*. Blurring the layers trades both away. This document is the
stable contract for the 0.2 line; the search JSON page shape is specified
below and pinned by producer, consumer, and release-canary tests.

## Deterministic machine output

`--flat` (including implicit piped TSV), `--lexical`, `-l`, `-c`,
`--count-by-tier`, `--json`. Grep semantics:
deterministic, stable columns/fields, explicit contract migrations before a
release boundary, and identical bytes for identical corpus + query.

Argument-parser failures are a separate front-door contract shared by every
command: exit 2, empty stdout, and one terminal-safe stderr line. They never
dump the full help page; explicit `--help` still does. A recognized command
word always keeps its grammar. When the remaining arguments also form a safely
copyable search invocation, the refusal appends the exact
`agrep search <word> ...` escape hatch instead of silently reinterpreting the
command. Versioned machine-mode validation errors keep their structured shape.

`agrep setup` is the command installers run unattended, so its exit status is
a contract: 0 when the box is set up, including when nothing needed changing;
1 when a step it attempted did not complete (the output names the step and
the fix, and the remaining steps still ran); 2 for an argument error; 3 when
it declined without changing anything and a retry declines the same way until
the cause is resolved. Today the one such cause is a data dir a newer agrep
owns: setup writes no agent files, starts no daemon and enables no archive
capture there, names the owner and the fix (upgrade agrep, or point
`AGREP_DATA_DIR` elsewhere), and exits 0 instead when a corpus is searchable,
because searches work read-only. The npm postinstall cannot read setup's
output, so it keys its one-line summary on these codes.

- **Never routed.** No lane substitution, semantic escalation, or interactive
  content-term recovery. A script gets the same shape every time.
  Caller-window exclusion is renderer-independent: once the calling session
  is identified and its family index resolves, the live window - every turn
  from the newest indexed recap, or the whole session when it never
  compacted - is withheld from every surface and older caller turns are
  labeled `~self`; without an identity or a resolving family, automatic
  exclusion withholds nothing.
- **Never collapsed.** No inlined follow-ups; machine callers compose their
  own pipelines.
- **A miss is relative to the indexed snapshot.** Exit 1 means no match in a
  usable committed snapshot, not absence at the current millisecond. Healthy
  live-update lag stays silent on hits and misses while each semantic lane covers
  at least 99% of its source; the tolerated row count scales with the corpus. A verified read
  also remains usable while the background indexer owns its refresh. Raw coverage
  metadata remains unchanged. Material gaps, required-query failures, integrity
  failures, and unchecked absence exit 2; `--no-auto` keeps empty results
  unverified. Optional meaning-lane unavailability is disclosed without changing
  the completed keyword search's exit status. Exact count and filter qualification
  still require complete coverage; meaning results cannot make inexact keyword
  totals exact.
  A store census observed within the last 5 seconds (possibly by the daemon)
  may be reused when the publication, ingest binary, discovery environment
  and working directory for relative roots are unchanged and the wall and
  monotonic clocks agree on its age.
- **Every surface states its own completeness.** A machine surface that
  printed part of the answer says so in the payload: search `--json` begins
  with one `agrep-meta` run envelope carrying `completeness` (`shown`, `total`,
  `total_basis` exact/floor, `unit`, `truncated`, `tool_rows`), and a cut page
  names a bounded live rerun (`more_command`, explicitly `broader-rerun`), the
  uncapped invocation (`full_command`), or why none exists
  (`no_exhaustive_form`, the meaning lane). When the local shell cannot quote
  an argument safely, the corresponding `more_argv` / `full_argv` is a JSON
  string array for direct process execution without a shell; unsafe copyable
  prose is never emitted. An already-uncapped partial run instead carries
  `action_unavailable_reason`. These are reruns, never frozen `--more`
  continuations. `--flat`, `-c` and
  `--count-by-tier` render the same judgement to stderr, so stdout keeps
  grep parity. A row count is never the signal; inferring a cap from it is
  how a parser reports 40 for 1,765. `tool_rows` counts matching ROWS that are
  tool output whatever the unit: under `-l` (unit `chat`) the stderr line says
  `N matching rows are tool output` beside the chat total, never `of them`.
  A lane that excluded tool rows (`tools_excluded` set) omits `tool_rows`: it
  never counted them, so no number stands in for them, and its floor notes drop
  the `-c counts exactly` lever because `-c` is a floor there too.
  Emitter and checker share `surface_policy.completeness_disclosure`; pinned in
  `test_machine_completeness.py`.
- **Indexed JSON hits are directly inspectable.** Each search row carries a
  digest-bound `handle` accepted by `agrep around`; serialization never mints
  one from an elided snippet when an invalid legacy row lacks a content claim.
- **Search JSON separates run state from hit evidence.** A non-empty page is
  one leading `agrep-meta` envelope followed by row-only hit objects. Run-level
  completeness, freshness, filter, self-exclusion, engine, and semantic state
  appear once in the envelope; `self` and the digest-bound `handle` remain on
  each hit. An empty page remains one `agrep-meta` record with `hits: []`.
- **Chats JSON uses the same one-envelope page shape.** The leading
  `agrep-meta` record carries completeness, freshness, and filter state once;
  chat records contain chat identity and navigation fields, and a content
  match additionally carries the ranking facts behind "best match first"
  (`score`, `matched`, `who`, `match_ts`, `match_turn`, `match_handle`) as
  search `--json` spells them. An empty page remains one self-contained
  `agrep-meta` record with `hits: []`.
- **Summary JSON keeps the same envelope.** `summary --json` leads with one
  `agrep-meta` record (`mode`, `window`, `timezone`, `idle_cap`, `metric`,
  counts including `unknown_timestamp_rows`, `family_dedup_ms` and
  `self_excluded`, `caveats`, freshness), followed by one record per project
  (`kind: project`), open item (`kind: pending`, with `status`, `confidence`
  and a handle) or time slice (`kind: time`). Time is always labelled an
  estimate; exits follow search: 0 something reported, 1 proven nothing,
  2 unverified. Pinned in `py/test_summary.py`.
- **Pending reads what the ingest kept, never a guess at what it lost.** A
  turn's reply text is exactly what 0.4.2 published (assistant text blocks
  joined with one space, cut at 64,000 characters with `…`), so every reply
  digest, handle, embedding hash and FTS row is unchanged across releases
  except for Gemini reply rows reconstructed by the Gemini lineage pass; those
  rows may change text, digest, and handles when lineage is recovered.
  The block structure travels beside it in `replies.jsonl` and, keyed like the
  agent row, in corpus.db's `reply_shape` table (read by key for the rows a
  briefing classifies; `summary time` reads none; the session signature the
  incremental refresh compares covers it, so a capped reply whose frozen text
  gains a later block still refreshes its ending): `reply_breaks`, the offsets
  (Unicode scalar values, Python `str` indices) of the joining spaces;
  `reply_worked`, per join, 1 when the agent made a work call (a tool or a
  delegation, never todo/task bookkeeping) between the two blocks, 0 when none,
  2 when the index holds no order for it (every adapter records its store's
  order; a row restored from an older cache has none); `reply_lead`, the same
  for the join before the first block, which for a compaction recap row is the
  work since the previous row's last block (absent when unrecorded; a
  single-block recap reply keeps its `reply_shape` row for it); and for a
  capped reply `reply_end`, the latest block's last 8,000 characters, with
  `reply_end_cut` when that tail is itself cut. A next-steps list in a block
  the agent then worked after was a plan it executed: the terminal block
  decides as 0.4.2 decides a turn's ending. With no work after the list block,
  later sign-offs keep the list (high) and other prose keeps it at medium with
  the start of that block quoted (`a later reply block may report this done:
  '…'`). Where the order is unrecorded, later sign-offs alone keep the list at
  medium (`whether work followed this list is not recorded; a later block:
  '…'`) and any other later block hands the turn to its terminal block, as
  0.4.2 read it; prose in the list's own block keeps 0.4.2's rule. A `To do:`
  heading counts like `Todo:`. A capped reply classifies by its kept ending;
  an ending cut past that tail, or one the index does not hold (a row not yet
  reparsed), is
  `unknown`, which the signal says. Every tool event carries `after_reply=1`
  in `meta` when no reply text of its turn followed it, so a turn that stopped
  after a call reads `agent_work_incomplete` while a call answered by a final
  reply does not; a todo, task or `yield` call after the final text is
  bookkeeping. Todo/plan/task writes carry their list facts whole in `state`
  beside the 800-character display input, and a Claude `TaskList` result its
  whole listing, so task state never depends on the display cap. What the
  source never recorded (a result that never came back, a run with no text)
  stays unknown or incomplete, as before. Pinned in `py/test_summary.py` and
  the adapter unit tests.
- **Why JSON is one versioned verdict.** `why --json` prints one object with
  `version` (1), `reference`, `verdict`, `exit`, `summary`, `candidates`,
  `evidence` and `next_action`; every evidence line names the file it came
  from. Exit 0 means the chat is indexed, 1 is an explained not-indexed
  verdict, 2 is ambiguous, unprovable or a usage error. "Indexed" is what
  `agrep search` would serve: `evidence.corpus.served` names that engine
  (`corpus.db` or the direct scan of `messages.jsonl`), decided by corpusdb's
  own reader predicates and connectors (`interactive_snapshot_lane`,
  `protected_read_lane`, `sqlite_failure_is_contention`,
  `_connect_read_direct`/`_connect_read_snapshot` by ownership), so `why` and
  search never disagree about a chat: a lock held by a writer is the busy
  direct-scan lane, a database the reader cannot open is the direct-scan lane
  with `agrep doctor` as the next step, a dead writer's hot journal reads from
  the reader's private snapshot, a chat only the lagging `corpus.db` still
  holds resolves there when search serves it, and a torn `sessions.jsonl`
  falls back to the rows `messages.jsonl` derives, as resume does. A moved
  store-wide intake key (one database holding many chats) is a caveat, never a
  verdict against an untouched chat. A token store of several databases
  (crush) is judged database by database: the census lists the conversations
  of every database it could open beside an issue naming each it could not,
  so a chat in a readable database is current, stale or deleted on its own
  token while a sibling database is unreadable. A registered database a
  stat cannot reach (its directory unsearchable, a symlink loop, a link
  pointing at a file) is unreadable, never absent: the index keeps its
  published chats, refuses a cold `--full`, and `why` says so; one a stat
  proves gone (a real parent now a file, a name too long, a volume
  unmounted) is a deletion candidate under the same two-observation
  protocol as a Stat store root: the first pass keeps its chats (disclosed
  as `source-root-unavailable`; `why` calls them unprovable rather than
  deleted and says to mount the store before the next index, which prunes
  whatever is still absent - no pass can tell an unmounted volume from a
  removed one), a database that returns in between leaves them untouched, a
  second agreeing preflight confirms the removal, and `--full`, which
  inherits no older expectation, converges a genuine deletion at once.
  Cursor lists every chat; while a workspace source it attributes chats
  through cannot be read, a chat keeps the project the last index published
  and a chat that never had one carries `cursor`, both disclosed as
  provisional (attribution is part of the token, so repair relabels only
  what the source changes, in one reparse), and `why` calls them indexed
  with the pending source named. That gap is attribution-only
  (`attribution-unreadable`): every chat was read, so it never holds the
  other agents' publication back, even with no parse cache, where the pass
  takes each chat's project from the published `messages.jsonl` instead
  (unreadable, the gap stays unadmitted); it settles nothing but itself, so
  a parse that fails on the same store is judged on its own. It is no
  freshness failure either: drift alone decides whether the index is
  behind (the record is neither a store member nor a census row of its
  own), and doctor's agent-stores line and `why` carry the provisional
  note, never "unreadable". A token chat that grew and whose parse then
  panicked keeps its last good tally; the read-failure record the index
  wrote names that conversation (`session`, absent from older records and
  from a failure no conversation owns), and only for it does `why` name
  the panic with the build update that can mend it, not "written after
  the last index". A sibling that grew since, even through the store's
  WAL with the file's times unmoved, was never tried and stays written
  after the last index; a record naming no conversation is charged to
  none. A chat whose stored rows do not parse completely (a Cursor
  composer, a crush session) fails whole: none of its rows publish, its
  record names it, and doctor's agent-stores line and the status freshness
  reason name it beside its store. A last good parse stays served, even
  one cached under the path its database had before it moved; with none,
  it holds nothing back unless a published generation may hold it, so
  neither a first index nor a new malformed chat stops its siblings or
  the other agents from publishing, and it is never cached as read. A
  failed chat settles only in its own database: a crush session id two
  databases share names two conversations. A
  store whose read fails mid-pass (the preflight saw it readable)
  contributes nothing past its last good rows and holds back only what a
  published generation may hold, under `--emit-rows` too when nothing is
  published yet. Material a published generation holds that turns
  unreadable with no complete cache fallback (the parse cache lost or
  discarded, `--full`'s cold parse, or a pass killed between its cache
  commit and publication, whose rows seen go are still published) holds
  every agent back: a chat, a per-file transcript or a store alike. The
  pass keeps the old generation and exits 1, naming each conversation or
  source (the first three, then how many more). A rebuild may leave it
  out only when the failure is durable and stable. Durable: content that
  does not parse, a denied permission on a file or any SQLite sidecar
  (`-wal`, `-journal`, `-shm`), pages SQLite reads as corrupt or as no
  database, or a hot rollback journal no writer holds, which needs
  recovery by the agent that wrote it. Never durable, however often it
  repeats: a live writer's lock, a generation that moved, a file
  replaced, changed or shrinking under the read, an open that is
  otherwise unavailable; these refuse "and will retry". An empty or idle
  journal no writer holds is no failure: the store is read as usual. A
  failed open names its cause in the record and the refusal (the
  journal, the writer, the defect). Where a writer's lock cannot be
  probed (Windows), a journal is named as present with how its agent
  recovers it ("if the agent that wrote it is not running, opening it
  once in that agent recovers it"), promised no retry, and never dropped.
  Stable: each source-health record carries the failing item's identity
  (a SQLite store's database, `-journal` and `-wal` each by device,
  inode, size, modification time and a head/tail digest, as the census
  names them: no ctime, which agrep's own hard-link snapshot moves; any
  other file by device, inode, size, change times and a content digest,
  so one torn, whole and torn again at one length and stamp is two
  failures; a token conversation by a digest of its own stored rows, so
  one stuck chat in a busy database stays the same), and a durable
  failure is stable only when an earlier pass recorded it for the same
  item, failing the same way with the same identity. A failure observed
  while the file (or a SQLite sidecar) was modified or changed within a
  timestamp tick of the pass (granule-aware, as the parse cache judges
  racy stamps: a whole-second stamp is two seconds wide) is recorded but
  proves nothing persists, since two catches of one torn state on a
  1-second clock share an identity; a later pass past that tick starts
  the count. A denied stamp
  read, of a store or a sidecar, is durable too. An empty session file
  is durable content ("file is empty"), stable once it stays so. A first
  observation is retried: the refusal ends "and will
  retry; if it persists, `agrep reindex --full` rebuilds without it",
  and nothing is marked or dropped, under the consent too. Once stable,
  the refusal gives the remedy "fix or remove it in <agent>, or run
  `agrep reindex --full` to rebuild without it (it is indexed again once
  readable)", in the plural for several. A corrupt database its agent
  keeps rewriting never stabilizes; its refusal says to stop that agent,
  and once agrep has seen it unchanged, `agrep reindex --full` rebuilds
  without it. Source health marks each persistent
  record (`blocks_publication`), so doctor's agent-stores line, `agrep
  audit` and `why` of the held chat give that remedy instead of a retry,
  and status says new chats are not being indexed (freshness code
  `unreadable-holds-index`). A record of a retryable or moving failure
  carries `retry` however often it repeats. Neither a mark nor `retry`
  outlives its cause: while one stands no pass takes the unchanged-source
  shortcut, so the next pass, the daemon's included, reads the material
  again and keeps either only if it still holds. The
  daemon's pass, `agrep index`, `agrep index --full` and a bare
  `agrep-rs index` keep failing closed. `agrep reindex --full` alone is
  the user's consent to rebuild from what is readable now: it runs
  `agrep-rs index --full --drop-unreadable`, a flag no other caller
  passes and that needs `--full`. When the only thing holding publication
  back is persistent unreadable material with no complete cache
  fallback, it publishes everything readable,
  drops that material's rows, names each on stdout ("rebuilt without
  unreadable ..."), and keeps each disclosed on source health, unmarked.
  It never publishes an empty generation over a published one: with
  nothing readable left it refuses, naming the material, and no pass
  offers that consent. Then no rebuild can help, so the refusal names the
  agent's own fix ("and will retry; if it persists, fix or remove it in
  <agent>" on a first observation, "fix or remove it in <agent>" once
  stable; for a denied permission alone, that the index recovers once the
  source is readable). So does a first index whose only material is a
  chat it cannot read. Once stable the record carries `rebuild: false`:
  status (freshness code `unreadable-holds-everything`), doctor, audit
  and `why` give that same fix. Anything else holding the pass back (a source
  gone or transiently incomplete, a parser panic) still refuses it. The
  consent's commit point is recorded right after its parse cache commits,
  before it writes the event store or any derived file. A consented
  rebuild that fails or is killed before that point is rolled back, there
  or by the next pass, which restores the source health it found, marks
  included, and discards the parse cache it committed; the event store and
  every published file are still the prior generation's, so every pass
  still holds until a consent publishes. One that stops after it (killed
  during its event write or promotion, an event store or marker it cannot
  write) stays recorded with its cache and source health, and every later
  pass trusts none of its outputs: it rewrites the event store and its
  proof and publishes the whole generation again without what it dropped,
  failing as any pass does while the obstacle stays, and only then clears
  the record; each says which it did. A consented rebuild may rebuild
  and prune the event store as a complete safe pass does, since what it
  publishes without is consented. While a record holds publication, the
  event store's interrupted-pass row and line give the hold's remedy
  instead of promising that `agrep index` publishes; the search index's
  say that `agrep index` rolls its journal back, which any pass does,
  one the hold refuses included, though new chats wait on the hold; and
  `agrep reindex` skipped by corpus.db's journal names the interrupted
  pass and `agrep index`, which rolls it back first. What it dropped was never
  cached as read, so it is indexed again once readable: an explicit pass
  retries it, and any pass does after it changes; until then it holds no
  later pass back, since no published generation holds it any more.
  `agrep index --full` (the daemon's own escalation included) parses
  without the parse cache, so material only that cache still serves fails
  there alone: its refusal ("could not read a source ... without the parse
  cache") names it, says the cache this --full pass skipped still serves
  it, gives "run `agrep index`, which publishes with it", and marks
  nothing. That cache is judged as a plain pass decodes it under the index
  lock (one whose journal is unreadable serves nothing), and a token
  conversation as the writer serves it: from its own database, or from one
  no longer present. So --full and a plain pass always agree, and only
  material no complete cache serves is ever marked. A
  refusal names the material the published generation holds before a
  failed chat that was never published. Where `reindex --full` would also
  redo meaning search (embeddings on, their runtime installed), the
  surfaces' consent remedy says so ("the rebuild also redoes meaning
  search, which can take a while"), with the rendered invocation, `why`'s
  included. Under a newer agrep's anchor, where this build refuses
  `reindex --full` and `agrep index`, they, audit included, give "update
  agrep to rebuild without it" for held material (freshness code
  `unreadable-holds-newer-owner`) and "update agrep to index it again" for
  any other failure, interrupted pass or `why` action whose remedy would run
  this build's index. While records hold publication, every row and line
  that gives the hold's remedy (doctor's agent-stores and freshness rows,
  audit, setup, the interrupted-pass lines, status and bare `agrep`) names
  every held agent in one clause, as the ingest's refusal does ("fix or
  remove them in claude, crush, or run `<cmd> reindex --full` to rebuild
  without them ..."), beside each other record's own remedy. Each such
  line says the rebuild's cost, so doctor and status may say it on more
  than one line; setup says it once, in its receipt, and audit once, in
  its degraded line, which names a held source past its first three rows
  by that clause too. `agrep setup`
  reports the hold and its remedy instead of a fresh index and "setup
  complete", and `why` of a chat known only to source health (never
  published, or left out by a consent) names its standing failure with
  doctor's remedy for it.
  A pass refuses to publish an empty generation when its
  only material was a chat or token store it could not read and let
  through as never published, a crush registry too corrupt to name any
  database included, or when the consent would drop all that was
  published. Two empty generations do publish: a first index
  whose only store fails mid-pass (the failure disclosed), and a deletion
  confirmed by two observations, which may empty what was published.
  While any read failure stands, chat or store, status, doctor, the census
  and `why` name it and the verified-current record never vouches for it;
  `agrep audit` cannot certify a store it could not read and exits 2, its
  problem naming each failure (the chat where one failed) with the remedy
  doctor gives. The source snapshot published beside a failure records
  the files the pass read, not the failure. The record stays until a pass
  reads the source again: every pass does once its content or identity
  changes, and the explicit passes
  (`agrep index`, `agrep reindex`, setup's index step, which run `agrep-rs
  index --retry-failed-sources`) do with nothing changed, so "make the
  file readable, then `agrep index`" is a true remedy. An automatic pass
  over an unchanged source takes the unchanged-source shortcut and keeps
  the disclosure it finds. A parser panic's remedy stays "update agrep,
  then `agrep index`": the same build panics the same way on a retry.
  Without the parse cache the census cannot tell which project each pending
  chat kept, so it lists no token for them and `why` calls them unprovable
  until the source is readable, never "written after". Only a reference
  nothing discovered matches stays unprovable while a database is unread,
  because that database may hold it; no census can rule that out. `why`
  never indexes or writes. Pinned in `py/test_why.py`.
- **Every surface states why it is empty.** A zero from a filter selecting a
  dimension the index holds no value for is not the same answer as a zero
  from a search that looked and found nothing, and the two must not render
  alike: the leading `agrep-meta` record carries
  `filter_coverage` (`empty_dimensions`, each naming `flag`, `value`,
  `dimension`, the `indexed_values` count and the `known` values it does
  hold; plus `checked` and, when the domain could not be enumerated, the
  `reason` no gap is claimed). The interactive render, `-c` and `--count-by-tier`
  render the same record as one stderr line. A legal value the index has not
  seen stays a legal query, disclosed rather than refused. Emitter
  and checker share `surface_policy.empty_dimension_disclosure`; pinned in
  `test_zero_coverage.py`.
- **Self-exclusion is counted, never advertised speculatively.** A proven
  current-window policy is silent unless matching rows were actually omitted.
  When the matching lane can prove the complete hidden scope, search JSON
  carries its exact pre-pagination count once in the leading `agrep-meta`
  envelope; flat/count surfaces put the same one-line count on stderr without
  changing stdout. An unprovable count stays explicitly unknown and produces
  no prose notice. `--self` disables the automatic window and explicit
  `--no-self` expands the scope to the caller's indexed family. The one
  exception to silence is the failure itself: when an agent shell is
  detected but the calling session cannot be named (`caller-unresolved`,
  `identity-conflict`), search and recall prose surfaces print one stderr
  line saying so (`surface_policy.caller_unknown_notice`); JSON keeps the
  same reason in `self_exclusion.reason` and the `identity` source
  (`claude`, `codex`, `pi`, `pi-process`, `corroborated`) when active.
- **The family index degrades loudly, not silently.** `[side chat]` marks,
  prefix-shortened handles, and the caller's family are read from the last
  published family index even while a store drifts past its `family_stamp`;
  that fallback prints one stderr line per invocation
  (`surface_policy.FAMILY_INDEX_BEHIND_LINE`). Query-time family expansion stays
  generation-bound. Compaction recovery from a behind-generation publication is
  marked partial with an `index_freshness` disclosure.
- **A writer does not invalidate a verified snapshot.** If SQLite is unavailable
  during ownership adoption or indexing, the generation-bound JSONL reader may
  serve a complete committed publication while the index lock is held. Missing,
  moving, or damaged publications remain subject to the bounded publication retry
  and fail closed when no verified result becomes available. Cached message and
  reply parse damage applies on every attempt, not only the first cache read.
  Event payloads and their database generation commit before the external event
  marker. Readers compare the database generation with that marker and recheck
  identities, so the database-commit/marker-publication gap cannot serve mixed events.
- **Rollback-journal event writes are readable at their last commit.** The
  event store is published in SQLite `DELETE` mode and a direct read during a
  writer transaction serves the last committed events. A non-hot `-journal` is
  not an unavailable or empty event set; hot journals left by an interrupted
  writer are different: read-only SQLite correctly refuses them, and surfaces
  disclose that tool events are unavailable until the next index pass rolls the
  journal back (waiting never clears it; without a background indexer that pass
  is an explicit `agrep index`). `around` never labels such a blocked read as an
  exact zero, and keyword search and recall in the transcript scan serve prose
  with `tools_excluded.reason` `events-unavailable`.
- **Parser-policy exclusions can remove previously cached material.** A complete
  empty reparse of the same source identity, mtime and size replaces its old rows
  and exact event ownership without a source-health warning. A complete empty
  reparse of an unchanged affected sibling follows the same rule. Changed or
  unverified source identities, incomplete reads and unattributed legacy events
  retain the last-good guard. Claude's empty-result fast path validates JSONL
  before declaring the read complete; malformed input is a read failure, not a
  policy exclusion.
  Cache generation 23 recognizes Codex Desktop `item_completed` / `UserMessage`
  submissions; generation 24 rechecks empty Claude sources under that read
  contract. Supported prior generations retain fallback material during reparse.
- **A window that selects no time is a usage error.** `--since`/`--until`
  name the half-open interval `[since, until)`; with the bounds the wrong way
  round it holds no instant, so every corpus answers zero. Both surfaces
  refuse before touching the index, naming both bounds
  (`surface_policy.window_bounds_error`, `--json` error code
  `empty-time-window`).
- **Every surface states what it served.** Positional `around SESSION TURN`
  requests may clamp and disclose the served turn. Result handles are exact
  references: an out-of-range, pruned, or content-mismatched handle is refused
  with one actionable error instead of being recentered. Successful differences
  travel in the stream the machine reads, never on stderr alone: `around --json`
  leads with an `agrep-meta` `served` record whose `divergences` name a clamped
  positional turn, a moved or unverifiable handle digest, or a prefix tie broken
  by digest. Each record carries the same rendered sentence stderr gets, so the
  two surfaces cannot disagree. Emitter and checker share
  `surface_policy.around_service_note`; pinned in
  `test_around_disclosure.py`.
- Correctness regime: contract tests, which are cheap, binary, and stable.
  Disclosure travels as fields rather than prose.
- **Compaction recovery is machine output.** `agrep postcompact` serves the
  pre-boundary tail of the calling session family after a provider compaction.
  It is continuation recovery, not retrieval: no query, no routing, and
  `semantic_search_performed` is always false. One record carries
  `schema_version`, `status`, and an `authority` block stating its own
  subordination — `role: supplement`, the visible user turns and the
  platform's own summary as `primary`, and `newer_visible_state_wins` — so a
  recovered tail can never read as an override of what the agent can already
  see. Scope is `root-only`: tool rows and delegated sessions are excluded and
  the output says so. Exit follows the proven-miss rule: 0 recovered, 1 proven
  empty against a generation verified current, 2 partial, unavailable, or invalid, and
  `--no-auto` marks the packet partial and exits 2 rather than presenting an
  unchecked absence as empty. Pages are bounded at 8,000 bytes of text or
  16,000 of JSON across at most eight blocks.
  Recovery never acquires the ingestion lock or runs a foreground full-store
  ingest. A timestamped request first checks the verified committed generation:
  its exact boundary can return recovered or empty with
  `index_freshness: indexed-snapshot`, without claiming global source freshness.
  A missing boundary or unproven generation queues a nonce-bound source/FTS refresh
  with the existing background owner. Recovery checks for the requested boundary
  during that refresh; an unrelated source failure cannot veto a verified packet.
  Only requests captured before a source pass receive completion receipts; a live
  daemon or suppressed warning is not proof. A source flush landing after that pass
  triggers another request within the same deadline. Manual calls without a
  timestamp select the newest indexed recap after their requested refresh.
  The reader also checks the database's full materialized-source stamp: unchanged
  family metadata alone cannot validate older recap rows. Bounded polling waits
  for up to eight seconds, including when the daemon was already running.
  A behind-generation fallback remains partial. An unavailable requested recap is
  never replaced by another boundary.
  For timestamped requests, an identity missing from the published snapshot
  stays pending rather than proving that its source has no compaction.
  Caller exit cancels its exact pending request and removes any completion receipt;
  a canceled request cannot force a later ingest.
- This layer is the embedding surface: a harness builds on it precisely
  because it never surprises, so its stability is maintained deliberately
  across releases.

## Interactive output

Bare `agrep <need>`, `recall`, `around`, and their tty/compact renders.
Evidence-shaped, allowed to evolve, judged on the frozen fixture
set (hit-rate by mechanism class), not on contract stability.

Finite `recall` and `pack` `--budget` values cap UTF-8 bytes actually written,
including ANSI control bytes and the final newline; fitting never splits a
code point. A zero budget remains uncapped. The fixed minimum is 64 bytes, and
a page may need more: text pages render every hit's own turn before any block
widens, the first hit's head line (its handle) is never cut, omitted hits are
counted in a trailing `[+N hit(s) over budget - … see --json omitted_hits]`
line, and a budget below the first block's head plus that line is refused
with exit 2 naming the bytes the page needs. `--json` pages carry
`omitted_hits` instead.

Compact output stays silent when an exact page is exhausted. Every
incomplete compact page emits one line from
`surface_policy.compact_completeness_line`: exact count, measured floor, or
unknown-total basis, followed by its copyable continuation when one exists.
It never prints `more=no` or promotes the page's row count to a corpus floor.
`--more` continues frozen rows; `--deeper` is labeled as a broader rerun that
may repeat rows rather than implying page two.

The routing rules for this layer:

1. **Disclose the route.** One line: which lane served, why, what would go
   deeper. A refused or degraded lane says so, because silence reads as "that
   capability doesn't exist." If automatic current-chat exclusion cannot
   resolve one caller, compact output gives one identifier-free warning that
   current-chat rows may appear.
2. **Every route is forcible.** Any decision the router makes, a flag pins:
   `--lexical` forces keyword, `-s` forces semantic, and `--classic` forces
   classic rendering without automatic semantic escalation. The router is a
   default, never a cage.
3. **Machine modes never route.** Crossing rule: an interactive-layer feature
   may not leak into machine output. Enforced by the machine contract tests.
4. **Collapse the near-certain round trip.** When an agent reading the output
   would fire a specific follow-up call with near-certainty, run it and include
   the result as a labeled, budget-capped block instead of printing the
   command. Auto-semantic escalation is the only implemented instance, and its
   threshold has not been generalized: per-shape thresholds calibrated from the
   fixture set are target-state (see Status). A follow-up needed in
   substantially all miss-to-hit paths collapses; anything genuinely optional
   stays a pointer. Costs are capped (bounded rows/bytes per collapsed block)
   and every collapsed block is labeled with what was run.
5. **Degrade legibly.** The interactive layer falls back predictably: semantic
   down → keyword-only, disclosed, still correct. When automatic meaning
   actually starts and then fails or times out, keyword hits remain available
   beside the canonical `meaning unavailable; keyword-only` notice. A runtime
   that was never present and a completed meaning miss are not lane failures.
   A meaning lane that ran against partial coverage can still serve hits.
   Ordinary misses tolerate the bounded live tail above; material backlog or
   unknown coverage exits 2 rather than presenting an ordinary empty result.
   The deterministic layer is the availability floor that makes the
   interactive layer safe to depend on.
6. **Compression has a floor: the judgment call.** A compact row exists so
   the reader can decide relevance without expanding it. Compression that
   destroys the discriminating detail (which error, which decision, which
   session) has negative value at any token count: the reader expands
   everything or, worse, judges wrong. Under budget pressure, evict whole
   rows before degrading rows; a shorter page of judgeable lines beats a
   full page of stubs. One-liners are a means; the judgment call is the
   contract. On compact page one, useful prose may rescue at most two
   non-redundant tool rows; tool-only results show four. Explicit `-n` and
   `--who tool` keep their requested behavior. Rows withheld by that display
   policy stay in the frozen continuation, including proven exact echoes.
   One prose row plus two rescued tool rows is intentionally a three-row page:
   the mixed-tool cap outranks the generic four-row floor. `around` never
   collapses a selected tool event or a sole distinct failure, and centers
   selected input or output previews on the verified match. Human recall context
   hides incidental tool events behind a counted `around --full` pointer; when
   the hit itself is tool-origin, its one match-centered preview remains judgeable.

7. **A remedy the product can run is never delegated to the reader.**
   "Run X to fix" is legal only when the remedy needs human judgment
   (destructive, consent), privileges the product lacks (chmod, disk
   access), or is too heavy to run unasked, and then the message states
   the cost. Everything else self-heals, backgrounded where possible,
   guarded against loops (an escalation is spent before it launches), while
   the surface serves last-good state and says what is running. The inverse
   lie is the same violation: "retries automatically" when the retry cannot
   succeed. Diagnosis is part of the remedy: "run doctor to see why" is
   delegation when the surface could carry the reason itself.

One meaning lane, two participations (rules 1, 2, and 4 together): the agent
profile (compact) fuses keyword and meaning evidence into one labeled page,
because its reader consumes exactly one page programmatically. The human
profile (classic tty) keeps the hit list exact-match-first with
single-meaning keyword scores; the meaning lane still participates as
disclosed sidelines - a zero-exact-hit query escalates to labeled semantic
matches, and a weak page (tool-output-only, or a related-terms fallback)
appends the `chats about this semantically` block, which counts the
neighbors held below the similarity floor and points at `agrep recall` as
the lane-fusing surface. Cross-lane scores are never merged into one human
ranking, and `--lexical` / `-s` pin either lane on both surfaces.

## Status

Every rule above is classified here, and every rule listed as standing names
the mechanism that enforces it. `py/test_output_contracts_doc.py` fails when a
rule is added without classification, or claims standing without an enforcer.
That test exists because rule 6 was once added without being classified here,
and went unenforced until an audit found it.

Standing today, in full:

- Rule 1 (disclose the route): lane labels, degradation notices, and uncertain
  caller disclosure pinned in `test_auto_semantic_hybrid.py` and
  `test_agent_context_contracts.py`.
- Rule 2 (every route forcible): the flag set, with the coverage lane's
  forced mode pinned in `test_search_correctness.py`.
- Rule 3 (machine modes never route): pinned in
  `test_search_query_contract.py` and `test_output_contracts_doc.py`.
- Rule 4 (collapse the near-certain round trip): auto-semantic escalation is
  the only implemented instance, pinned in `test_auto_semantic_hybrid.py`. The
  generalized threshold is target-state below.
- Rule 5 (degrade legibly): one degradation story on every surface: search,
  recall, and the probe render the same keyword-only notice when the meaning
  lane is down; the `-s` failure line, doctor's state rows, and the tty retry
  hint all render from `surface_policy.SEMANTIC_LANE_POLICY`; recall `--json`
  carries a `semantic_status` that separates "searched, empty" from "never
  ran"; parity-pinned in `test_surface_policy.py`.
  The notice is shared, not the exit requirement: a completed keyword search
  retains its exit when optional meaning is unavailable; a recall/probe miss
  still exits 2 when its requested meaning lane never ran.
- Rule 6 (compression preserves the judgment call): blocks are evicted whole
  before any block degrades to stubs, with the eviction disclosed; compact
  completeness is fixture-locked to the documented exact/floor/unknown forms;
  pinned in `test_compact.py` and `test_output_contracts_doc.py`.
- Rule 7 (remedies self-run), partially: remedy text lives in
  `surface_policy.REMEDIES`, typed by kind (auto / consent / privilege /
  human-prereq): auto remedies name their owning mechanism and their text
  reports it (the freshness footer names the pending or spent automatic
  rebuild; stale handles say "rerun the search"; audit's gap line states
  the self-heal; the daemon spends `--full` escalations before launching
  them). Registry structure pinned in `test_surface_policy.py`; renderers
  consume the registry, so a new hand-written remedy string is a review smell
  rather than a grep target. Doctor's remedies, the semantic remedies, and the
  foreground search path's recovery from a wedged parse cache have not migrated
  into `REMEDIES` yet; until they do, rule 7 holds only where the registry
  reaches.

Target-state, not yet standing: the remaining rule-7 sites named above; rule 4
generalized (probe→pull collapse, top-hit context inline, prose-miss→tool-lane
inline, mega-session auto-narrow); the front-door router (bare agrep choosing
rows vs windows from evidence shape, verbs remaining as expert lanes); and the
per-shape collapse thresholds calibrated from the frozen fixture set. Target
items move up only with a named mechanism and a fixture-set result beside
them.

## Live observation

`agrep tail` is machine JSONL. Its `tail_ready` marker means the watcher's
initial store census is complete and the subscription is armed. `--snapshot`
also waits for that census; its object includes `booting`, `last_err`, and
`degraded_sources`, so access failure cannot be interpreted as an empty store.
Every emitted event precedes the byte-offset commit that acknowledges its
journal line. A failed line handler is retried rather than skipping later
events in the same batch.

Tail snapshots apply agent and session filters before computing their counts
and diagnostics. A scoped snapshot omits global `last_err`; it retains only
`degraded_sources` attributable to the selected agent. SIGINT returns 130 after
any event already emitted as a complete JSON line.

`agrep board` is the other live surface: a bounded window over what is running
on this box right now, where `chats` is indexed history. Its `--json` page
carries the same completeness vocabulary the indexed surfaces use, so the two
cannot be read differently: `matched` with a `matched_basis` of exact or floor,
`shown`, `sessions_complete`, `page_complete`, and a `booting` flag, plus
per-source error detail rather than a silent omission. A partial page names its
bounded rerun as `retry`, and when the local shell cannot safely quote every
filter it supplies `retry_argv` for direct process execution and says why the
copyable form is missing. As with `tail`, a still-scanning or degraded snapshot
is disclosed rather than rendered as an idle box: absence of rows is not
evidence that nothing is running.

Interactively, `agrep board` (and `agrep live`) owns the terminal while it runs
and hands it back as it found it on every exit: `q`, the end of input, Ctrl-C
(however many, also on a terminal slow to take output), SIGTERM, SIGHUP,
SIGQUIT and a job stopped and continued. That covers its modes, the main
screen, the cursor and default colours. A fatal signal ends the board even
when its terminal has stopped taking output; the main screen comes back once
the terminal takes output again, a second signal arriving meanwhile changes
nothing, and the exit status names the first signal handled (of two arriving
together, CPython handles the lower-numbered first). A restore that hangs
anyway ends after 5 seconds with SIGALRM. On a POSIX terminal the board turns
on bracketed paste, so a paste arrives as one inert key: no letter or line
break in it acts, and input still unread at exit is discarded rather than
reaching the shell. A Windows console delivers a paste as typed keys, so that
guarantee is POSIX-only; there Ctrl-Break hands the console back and exits
with code 3.

Live discovery is deliberately bounded. A new watcher seeds journal-backed
sessions whose stores moved within the last 90 seconds; a session already quiet
inside a long tool call appears on its next write. This is an observer-start
horizon, not a claim that the agent stopped. A tracked working session is kept
alive by exact process evidence when available. Writer disappearance and
working-row expiry produce terminal `done` events instead of silent removal.
