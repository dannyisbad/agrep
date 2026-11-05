# Engine design

agrep keeps all derived state for a data root in one SQLite database. Commands answer
from a committed snapshot and do not parse transcripts on the query path; the exceptions
are the first build and `postcompact`, which refreshes the caller's own session. A
stat-only census finds changed sources, and a short-lived detached `agrep __work`
process applies them. Ranking formulas are in [SEARCH_RANKING.md](SEARCH_RANKING.md),
output contracts in [OUTPUT_CONTRACTS.md](OUTPUT_CONTRACTS.md), the meaning lane in
[SEMANTIC.md](SEMANTIC.md) and the library surface in [ENGINE-API.md](ENGINE-API.md).

## Data root

The root is `AGREP_DATA_DIR` if set, otherwise `~/Library/Application Support/agrep/0.5`
on macOS, `$XDG_DATA_HOME/agrep/0.5` (or `~/.local/share/agrep/0.5`) on Linux and
`%LOCALAPPDATA%\agrep\0.5` on Windows.

`FORMAT` holds exactly `agrep-data 1\n`. An absent or empty root is initialized; the
marker is created with `create_new`, so racing initializers agree. Other marker bytes,
or a non-empty root without a marker, are refused with one line naming the path. On Unix
the root and its `archive`, `model` and `log` directories are created with mode 0700,
and validation tightens existing ones. With `AGREP_DATA_READONLY` set (to anything but
empty, `0`, `false` or `off`), commands skip the census, report freshness as unchecked,
and fail instead of taking a lock or writing: validation leaves directory modes as they
are, and nothing is logged.

| Path | Contents |
|---|---|
| `index.db` (`-wal`, `-shm`) | all derived state: sources, sessions, rows, tool events, FTS, block bounds, statistics, vector references, embedding queue |
| `index.building.db` | the first build until it commits; the next first build deletes a leftover |
| `pages.db` | frozen result pages for `agrep --more`, kept five minutes |
| `vectors/<sha256>.q8f16` | immutable embedding blocks referenced from `index.db` |
| `model/granite-embedding-small-english-r2/` | the pinned embedding model (`AGREP_MODEL_DIR` overrides) |
| `config.json` | `tools`, `embeddings`, `post_index`, archive policy, board view, integration enrollment |
| `harness_prefixes.txt` | optional user-written harness prompt prefixes |
| `archive/` | opt-in source archive: `catalog.db`, `blobs/<sha256>.gz`, `archive.lock` |
| `log/agrep.log` | worker stderr and semantic failures; rotated to `agrep.log.1` at 1 MiB |
| `writer.lock`, `writer.queue`, `work.lock`, `merge.lock` | advisory locks, below |
| `repair-<pid>/`, `index.corrupt-<ms>` | `doctor --fix`: the rebuild directory and the retained corrupt database |

## Locks and snapshots

All locks are OS advisory locks taken through the standard library's file locking
(`flock` on Unix, `LockFileEx` on Windows). Lock files are created on first use and
never deleted. Readers take no lock.

- `writer.lock`: one writer at a time. A detached refresh try-locks it and leaves the
  change pending if another writer holds it, except that it waits out a merge step.
  `index`, `reindex`, the first build, `postcompact`'s session refresh, `set` and
  embedding publication wait for it.
- `writer.queue`: a waiting writer holds it shared until it has `writer.lock`, and so
  does a command waiting for `work.lock`. A merge step takes it exclusively before
  `writer.lock` and drops it once it has the writer lock, so a step starts only when no
  writer is waiting. Neither `flock` nor `LockFileEx` hands a released lock to a waiter,
  so without the queue a merger re-taking `writer.lock` in a loop could starve one.
  While a step runs, it try-locks the queue exclusively about every thousand SQLite
  instructions (FTS5 writes each output page through a statement), which fails exactly
  while a writer waits: the step is interrupted and rolled back, its writer lock is free
  at once, and it runs again once no writer waits. A writer therefore never waits for
  merge work, however long the step a common trigram's doclist forces.
- `work.lock`: one background worker for refresh and the queue tasks. A command spawns a
  worker only while the lock is free; the worker takes it itself and exits if another
  process got there first.
- `merge.lock`: the one process merging FTS segments. It holds no `work.lock`, so
  refreshes and queue tasks never wait for a merge.
- `archive/archive.lock` serializes archive operations (scheduled capture skips when it
  is held); `model/<id>/download.lock` serializes model provisioning.

A reader opens `index.db` read-only, sets `query_only` and begins a transaction at once,
so every statement of a command reads one committed snapshot. The writer uses WAL with
`synchronous=NORMAL`. A refresh commits rows, tool events, FTS changes, block bounds,
statistics, cached directory listings and source fingerprints in one transaction. Vector
block files are written and fsynced before the transaction that references them; a crash
can leave only an unreferenced file, which reclamation deletes.

## Census and freshness

`freshness::pending` decides whether anything changed, without parsing:

- Each adapter's freshness roots are walked one directory level at a time on a pool of
  at most eight threads. A directory's listing is cached by name in `source_directories`
  and reused while its mtime is unchanged. Sources are stat'ed in the task that
  confirmed their directory, split across the pool in chunks of 64. The index's
  fingerprints are read from `sources` meanwhile.
- A fingerprint is size, modification time in nanoseconds and a change token (on Unix,
  the inode change time). SQLite stores (`.db`, `.sqlite`, `.sqlite3`, `.vscdb`) include
  their `-wal` file's size, time and token.
- A source has changed when it is new, its fingerprint differs, stat proves it absent,
  or a directory now stands at its path; the refresh removes a source on the same test.
  Symbolic links are not followed. An unreadable directory yields an error record rather
  than the deletion of its unseen subtree, and the record goes once the directory lists.
- The census also reports whether FTS segments need merging. It writes nothing; the
  refresh saves the listings it used.

How commands use it (`ready` and `finish_census` in `crates/agrep/src/main.rs`):

- Without `index.db` the command builds the index inline, printing `building the search
  db (a first build can take a while on a large history) …` on stderr, then answers.
- Otherwise the census runs on its own thread while the command queries its snapshot.
  Afterwards, if sources changed, the command spawns `agrep __work`. If none changed, it
  spawns `agrep __work merge` when segments need merging, and `semantic::schedule`
  spawns `agrep __work` for pending embeddings (model installed, `embeddings` not off),
  a pending post-index hook, or an enabled archive.
- The answer comes from the snapshot. The number of changed sources is part of the
  result (`freshness.pending_sources` in JSON); the changes become visible once the
  worker commits. `--no-auto` skips the census and marks freshness unchecked.
- `postcompact` refreshes only the sources already attributed to the caller's session
  (`store::refresh_session`); other changed sources stay pending.

## Refresh pipeline

`store::refresh`:

1. Takes `writer.lock` as described above.
2. Reads `harness_prefixes.txt`; a digest different from the last refresh's re-parses
   every source.
3. Censuses the sources and compares them with `sources`. With nothing new, changed or
   removed, it returns without a transaction.
4. Parses the changed sources, adapters in parallel (up to four threads; eight for the
   first build or a full refresh). pi, Claude and Codex `.jsonl` sources resume from a
   cursor in `parser_cursors`: byte offset, record count, file identity, parser state, a
   SHA-256 digest of each 1 MiB chunk of the parsed prefix, and pi's header and current
   leaf. A resume needs the same file identity, a length of at least the offset, and
   every prior chunk's digest to match; otherwise the source is parsed from the start. A
   torn final record waits for the next refresh. A pi append whose parent is not the
   previous leaf is a branch: the active branch is rebuilt from the stored entry graph
   (`parser_graph`) instead of rereading the file. Other formats re-parse the changed
   source.
5. Writes, in one transaction:
   - the directory listings, and the removal of removed sources' rows;
   - each parsed source's rows, classified (user, subagent, recap, control, synthetic,
     harness), with its cursor and graph pages. A resumed source upserts the rows its
     new bytes produced and removes none. A source whose parse failed keeps its rows,
     and the error is recorded on its `sources` row;
   - fingerprints, and the deletion of tool texts no source references;
   - a canonical replay of each touched session held by several sources (resumed or
     copied transcripts): every copy is kept, but only one copy of each turn is visible,
     the one with the longest reply, then the most metadata. Turns are renumbered by
     time only when their numbers collide. Duplicate tool calls keep one copy;
     copied pi chats break equal-quality ties and keep the first surviving parent claim
     in adapter discovery order. Their project and first request follow that order too.
     Family roots follow unambiguous filename aliases after a root's header id changes;
   - the FTS changes (below), the tool dictionary, block bounds, session families, prose
     trigram counts and family token statistics;
   - the deletion of sessions left without rows, and the refresh time.
6. For the first build, writes `index.building.db` with a rollback journal and creates
   secondary indexes and triggers after the bulk insert, then queues every visible row
   for embedding. On commit it switches to WAL, renames the file to `index.db` and
   fsyncs the directory.

## Row storage and compression

- `prose` holds conversation rows. Text is plain UTF-8 with its SHA-256 `digest`, native
  `turn`, canonical `position` and a `visible` flag; a row is keyed by source, session,
  turn, speaker and an extra key.
- Tool calls live in `event_rows`, keyed by integers, with strings interned in `symbols`
  and sessions in `event_sessions`. A call's text (input and output) is stored once per
  distinct content in `tool_blobs`, keyed by its SHA-256, behind a codec byte: `0` LZ4,
  or `1` zlib level 1 for texts of 128 bytes or more when that is over 16 bytes smaller.
  `tool_blob_sources` records which sources reference a blob. `tools` and `events` are
  views with the public columns.
- Parser cursors and family token counts are LZ4-compressed JSON, pi graph pages zlib,
  gram postings LZ4.
- `state` keeps trigger-maintained counts of visible prose, user prose and tool rows, so
  status and empty-result messages need no row scan.

## Blocks and bounds

Rows are grouped by id into 512-row blocks per lane (`blocks` for prose, `tool_blocks`
for tools). A block records its newest timestamp, newest user-row timestamp, highest
speaker weight, smallest session and turn, a bound on separator runs inside phrase
matches (`max_gap`), and whether its text is all ASCII.

For every gram of one to three lowercase ASCII letters or digits, `gram_postings` lists
the blocks containing it, each with the highest per-row frequency, and the newest
timestamp and highest speaker weight among the rows containing it; `block_grams` lists
each block's grams. From these a query computes a ceiling on the score any row of a
block can reach, using the score formula in [SEARCH_RANKING.md](SEARCH_RANKING.md) with
the boundary factor at its maximum of 1. Other text gets a conservative universal
ceiling.

A refresh recomputes bounds only for changed blocks. A prose block that only gained rows
(none updated or deleted, which `bound_updates` records) merges the new rows' maxima
without reading its old rows.

## FTS indexes

- `prose_fts`: contentless FTS5 (`contentless_delete=1`), trigram tokenizer,
  `detail=full`, over normalized text: ASCII lowercased, other characters NFKC and case
  folded.
- `tools_fts`: contentless FTS5 with `unicode61 remove_diacritics 0` word tokens and
  positions. SQLite truncates tokens at 32 KiB, so words over 8 KiB are indexed as
  overlapping 8 KiB windows and literals up to 4 KiB stay findable.
- Tool dictionary: `tool_words` holds each indexed word of three or more characters with
  its document frequency and the tool blocks containing it, `tool_lexicon` is a trigram
  FTS5 index of those words (`detail=none`), and `tool_trigrams` holds the summed
  frequency of the words containing each trigram. A term of 3 to 4,096 ASCII letters and
  digits expands through the lexicon to the indexed words containing it; the tool lane
  searches for any of them. Other tool terms are verified on decoded text.
- `prose_terms` holds the exact document frequency of each prose trigram, and
  `term_stats` the family counts the boundary factor uses. Both are maintained in the
  refresh transaction.

### Segments and merges

FTS5 starts a new segment whenever a write's rowid is not above the previous write's,
and at every statement savepoint. A refresh therefore records each touched FTS row's
state at the start and end of the transaction and writes the net change once, in rowid
order, after the row statements: each refresh adds at most one segment to `prose_fts`,
`tools_fts` and `tool_lexicon`. Refreshes never merge (`automerge=0`); pending terms
stay in memory up to `hashsize` 8 MiB.

`__work merge`, under `merge.lock`, merges in steps:

- Level merges first: with `usermerge=2` a level holding two segments merges into the
  next, so each refresh's segment carries upward like a binary counter.
- The first build's segment of each table is moved to level 16 before the index is
  published (`fts_segments::seal`). Left at level 1, it would be rewritten whole to
  absorb the second refresh's few pages, and again at the next carries; above the
  counter's levels it is rewritten only by the cap below, after 31 refreshes at the
  earliest. FTS5 still pulls a segment down beside a new one at least its size, so a
  first build smaller than later segments merges with them as before.
- A table that still has more than 5 segments is merged into one. Each segment adds a
  few percent to lookups, which seek every segment for every term; each step up in the
  cap halves how often the large segment is rewritten.
- Each step asks for 64 output pages and is its own transaction under `writer.lock`,
  taken behind `writer.queue`. FTS5 ends a step only between terms, so a common
  trigram's doclist is one long step whatever the budget; a writer that queues during
  it interrupts it.
- The merger disables auto-checkpoints and runs a passive checkpoint after each run of
  steps and whenever the WAL reaches 4 MiB, so foreground commits do not inherit its
  frames.
- The census reports unfinished merges, levels holding two segments and tables over the
  cap, so a killed merge resumes in the next worker. `index` and `reindex` start a
  detached merger rather than merge inline.

## Query lanes and ranking

`query::search` reads the prose lane, plus the tool lane when the `tools` setting is on
(the default) or `--who` names `tool`. `--who tool` reads only tools and `--no-who tool`
leaves them out.

There are three tiers: phrase (the tokens in order, joined by `[\W_]*`), all terms (each
token, with singular/plural variants of ASCII words) and content terms. A ranked search
(score order, a page limit, no `-w` or `-E`) runs a phrase pass first. When phrase rows
fill a row page the lower tiers are not run; otherwise an all-terms pass continues from
the phrase rows. When nothing matches, interactive and compact output run the
content-terms tier for a multi-word query that contains stop words, one-character terms
or punctuation: one pass per remaining term, highest IDF first, each over the rows
lacking the earlier terms. `--coverage` is a separate bm25 lane; see
[SEARCH_RANKING.md](SEARCH_RANKING.md).

`-E` compiles a case-insensitive fancy-regex. It hands an expression that needs none of
its own features (look-around, backreferences and the like) to the linear-time regex
crate, and backtracks at most 100,000 times per search on the others. `-w` matches as
0.4.9's `(?<!\w)` + term + `(?!\w)` under Python's `re.I`, without backtracking: the
regex crate finds the term with each character spelled as `re`'s case-insensitive
matching allows, and a match stands where neither neighbor is `\w` (`str.isalnum()` or
`_`). The literal every match begins or ends with, if there is one, drives the index.

- Terms are ordered by estimated document frequency: in prose, the least frequent of the
  term's trigrams in `prose_terms`; in tools, the summed frequency of its words. Prose
  ANDs every indexable term; the tool all-terms lane intersects the two rarest term
  groups and verifies the rest. Tool phrase passes check the indexed positions of up
  to three groups before decoding text. Common questions of at most sixteen terms
  prefer three consecutive groups that each expand to one literal word; other plans
  use the rarest groups. Distinct single-word expansions with no extra token gaps
  first use FTS5's native phrase check for consecutive groups, or its proximity check
  for spaced groups, then the same ordered-position verifier.
- Terms shorter than three characters stay out of the FTS expression and are verified on
  row text. Every candidate is verified on its stored text before it scores.
- For a single ASCII alphanumeric tool term, indexed word occurrences in the folded text bound
  the snippet's repetition score. A positive literal occurrence count proves the match;
  when its score ceiling loses to the page, it is counted without decoding or scoring.
  Other query forms and rows that can enter the page retain canonical text verification.
- Outside a selective scope, with more than 128 estimated matches, a page limit and
  score or time order, a pass visits blocks in descending ceiling order (newest first
  for time order) and stops once the page is full and the next block cannot beat the
  page's last row. A block whose rounded ceiling only equals that row's score and loses
  the tie on time and position is skipped, not the end: a lower ceiling can round to the
  same score, and a newer row behind it wins the tie.
- When metadata filters (agent, `--chat`, project, model, `--since`, `--until` or a
  session scope) select at most max(4,096, min(lane rows / 16, 65,536)) rows, the scope
  is selective: its own blocks are scanned and the FTS index only admits rows, one
  rowid-range query per run of up to 64 nearby blocks.

Scores, snippets, boundary grading and tie order follow
[SEARCH_RANKING.md](SEARCH_RANKING.md). `--no-meta` fetches at least 40 rows and drops
the proven `~meta` rows from that page; counts with `--no-meta` are refused.
Row and chat pages fetch display metadata and build handles only for candidates that
can enter the page or improve an existing chat head; matching rows are still counted.

## Counts

`-c` and `--count-by-tier` are exact. For `-c` without `-w`, `-E` or `--chats`, when
every term has three or more characters and every tool term expands, the count comes
from the FTS index rather than a verified scan: the match joined to the rows when
filters narrow it, read through the scope's indexes for session scopes or metadata
scopes of at most 4,096 rows, and otherwise
the corpus-wide count minus the matches in rows the caller filter withholds. Other
counts run the verified scan.

A ranked page reports `total_basis` `exact` when every candidate was examined, and
`floor` when it stopped early: phrase rows filled the page and the lower tiers went
uncounted, a block ceiling ended a pass, or the tool lane was skipped because a tool row
(scoring at most 0.22) cannot outscore the page's last phrase row. A `--no-meta` page
whose matches did not fit the fetched rows, partial semantic coverage, a meaning scan
stopped at its bound and a hybrid merge that added meaning rows also give floors.
Classic output prints a floor on stderr as `showing N of M+ … (a floor, not the total;
-c counts exactly)`. A compact page prints `N+ matches (floor; -c exact)`, or `N+ matches
(floor)` on a meaning page, and a merged page whose lanes returned every row prints none;
JSON then omits `tool_rows` and sets `tool_rows_basis` to `unknown`.

## Caller exclusion

The caller is identified by `CLAUDE_CODE_SESSION_ID`, `CODEX_THREAD_ID` or
`AGREP_PI_SESSION_ID`; disagreeing values disable exclusion (`identity-conflict`).
Without them, agrep walks up to 24 ancestor processes looking for a pi publication
`<pid>.json` in `AGREP_CALLER_PUBLICATION_DIR` or `/tmp/agrep-0.5-caller-v1-<uid>` (the
temporary directory on Windows). The directory must be owned by the user with mode 0700,
the file at most 64 KiB and naming at most 64 sessions, and it must belong to the live
process with that pid. The other agent environment variables only mark an agent context.

`Caller::resolve` finds the session's agent and family root. The window starts at the
session's newest recap row, or turn 0 without one. Live members are the side sessions
descending from the caller that started at or after the window.

- By default, rows of the caller's session from the window on and rows of its live
  members are withheld before ranking and counting. Older rows of the caller's session
  stay, marked `~self`.
- `--self` withholds nothing; `--no-self` withholds the caller's whole family.
- One predicate serves SQL retrieval, counts, session lists and the meaning lane. JSON
  reports it as `self_exclusion`. Withheld matches are counted by probing the withheld
  sessions, and reported as unknown past eight sessions.
- An agent context without an identity prints one stderr line saying the current chat's
  rows may appear.

## `--chat` scopes

`--chat X` must resolve to one session: an exact id, a unique id prefix, a handle's
session, or a session `history::resolve_indexed` finds (a hex fragment of six or more
characters, a project, title text or a source path alias). Otherwise search fails with
`chat is missing or ambiguous`.

The filter then matches every session whose id starts with the resolved id. The prefix
is matched on the small `sessions` table and the rows are read through the session
indexes; agent, time and project predicates get a unary `+` so the planner does not
choose their indexes for a session scope. A `--chat` search waives automatic caller
exclusion (`self_exclusion.reason` is `chat-filter`) unless `--no-self` is given.

## Background work

`agrep __work [TASK…]` runs the selected tasks (all by default) in this order:

- `refresh`, under `work.lock`;
- `merge`, under `merge.lock` alone;
- under `work.lock` again: `semantic` embeds queued rows newest-first in batches of 128,
  publishing each batch in its own transaction, and stops between batches while a writer
  or an explicit command waits in `writer.queue` (the rest is then left to that command
  or the next worker); `reclaim` deletes unreferenced vector
  metadata, and block files that are unreferenced and older than ten minutes; `archive`
  captures sources when the archive is on and ten minutes have passed since the last
  capture; `post_index` runs the configured `post_index` command through the shell after
  an index change.

The worker lowers its priority (nice 10 on Unix, below-normal on Windows), uses at most
half the logical CPUs and keeps BLAS single-threaded. A spawned worker runs in its own
process group, with stdin and stdout null and stderr appended to `log/agrep.log`, and
inherits no other descriptor or handle of the spawning command: a worker holding a pipe
the command got from its caller (`out=$(agrep search x 3>&1)`) would keep the caller
reading to the end waiting until the worker exits. On Unix the command marks its other
descriptors close-on-exec while it spawns, so std still uses `posix_spawn`; on Windows a
`PROC_THREAD_ATTRIBUTE_HANDLE_LIST` names the worker's only inherited handles.

`index` and `reindex` refresh inline and start a detached merger when segments need
merging. `reindex` and `index --semantic` then embed the queue inline, waiting for
`work.lock` in `writer.queue`, and run the remaining tasks; `index --semantic` also
provisions the model and embeds even when `embeddings` is off. A plain `index` never
embeds and never waits for `work.lock`: it runs `reclaim`, `archive` and `post_index`
only while the lock is free, and leaves the queue to a background worker.

## Known limits

- The census stats every source and directory on each command that runs it, so its cost
  grows with the number of source files.
- Resuming a JSONL source hashes its whole parsed prefix, so an append to a large
  transcript costs time in proportion to the file, not the append. Other formats are
  re-parsed whole when they change.
- A source that fails to parse keeps its last good rows and is parsed again only when
  its file changes or on a full rebuild.
- Terms shorter than three characters, and tool terms that are not ASCII letters and
  digits, are verified on row text instead of read through an index, so an exact count
  of such a query reads every row in scope. Non-ASCII text gets the universal block
  ceiling, so early termination helps it less.
- An `-E` expression that needs fancy-regex's own features and exceeds the backtrack limit
  fails the command (exit 2).
- Until a merger runs, each refresh leaves one more segment per FTS table for lookups to
  seek. A carry that reaches the first build's segment rewrites it in the background,
  which can take seconds.
