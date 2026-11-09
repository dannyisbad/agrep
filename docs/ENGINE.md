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

`FORMAT` holds exactly `agrep-data 8\n`. An absent or empty root is initialized; the
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
| `index.building.db` | a first build or `reindex` until it is moved into place; the next writer deletes a leftover |
| `pages.db` | frozen result pages for `agrep --more`, kept five minutes |
| `vectors/<sha256>.q8f16` | immutable embedding blocks referenced from `index.db` |
| `model/granite-embedding-small-english-r2/` | the pinned embedding model (`AGREP_MODEL_DIR` overrides) |
| `config.json` | `tools`, `embeddings`, `post_index`, archive policy, board view, integration enrollment |
| `harness_prefixes.txt` | optional user-written harness prompt prefixes |
| `archive/` | opt-in source archive: `catalog.db`, `blobs/<sha256>.gz`, `archive.lock` |
| `log/agrep.log` | worker stderr and semantic failures; rotated to `agrep.log.1` at 1 MiB |
| `log/post_index.log` | the last `post_index` run's stdout and stderr |
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
- `work.lock`: one background worker for embeddings, hooks and archives. A command spawns a
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

`freshness::check` decides whether anything changed, without parsing, and retains the
source snapshot for the refresh (`pending` exposes only its measurements):

- Each adapter's freshness roots are walked on a pool of at most eight threads.
  Directory traversal has no level barriers; after it confirms the reachable tree,
  source files share stat tasks of at least 64 files across directory boundaries.
  Cached listings stay packed while their directory mtime is unchanged, so each
  filename needs no separate allocation. The index's fingerprints are read meanwhile.
  File change tokens stay numeric in memory and use compact binary storage; JSON
  encoding is reserved for diagnostics.
- A fingerprint is size, modification time in nanoseconds and a change token (on Unix,
  the inode change time). SQLite stores (`.db`, `.sqlite`, `.sqlite3`, `.vscdb`) include
  their `-wal` file's size, time and token.
- A source has changed when it is new, its fingerprint differs, stat proves it absent,
  or a directory now stands at its path; the refresh removes a source on the same test.
  Symbolic links inside a store are not followed. A store root that is itself a link is
  followed to its own kind: a directory root (a dotfiles setup moving `~/.codex/sessions`
  elsewhere) to a directory, and a single-file root (opencode's `OPENCODE_DB`, kimi's
  `kimi.json`, crush's and cursor's databases) to a regular file, which is stamped and opened
  at its target, so its `-wal` is the real one; sources keep their paths through the link.
  A root link that dangles, leads to the wrong kind, or leads into or around another store
  root is an error record instead, so the store is never read twice or silently missed. An
  unreadable directory yields an error record rather than the deletion of its unseen
  subtree, and the record goes once the directory lists.
- The census also reports whether FTS segments need merging or retrieval bounds need
  tightening. It writes nothing; the refresh saves the listings it used.

How commands use it (`ready` and `finish_census` in `crates/agrep/src/main.rs`):

- Without `index.db` the command builds the index inline, printing `building the search
  db (a first build can take a while on a large history) …` on stderr, then answers.
- Otherwise the census runs on its own thread alongside index-independent preparation:
  search model read-ahead, caller detection, recall budgeting, and configuration reads.
  Every reader joins it before its first index read. If sources changed, the command
  waits for `writer.lock` and calls `store::refresh_checked` with that same census;
  it does not walk the sources again or run the query twice. It compares the census
  against the stamps committed by any preceding writer, rechecking only differing files
  so a newer version that writer ingested also counts. It parses only uncovered changes.
  A parse failure retains that source's last-good rows.
- After ingestion, the command schedules `agrep __work merge` if FTS segments need
  merging, and `semantic::schedule` schedules embeddings, the post-index hook and
  archives. Scheduled workers do not refresh sources.
- Every index answer includes source changes present when its command began. A record
  still being written at that instant can be absent. `--no-auto` and a read-only data
  root skip automatic ingestion and mark freshness unchecked. Continuation pages
  (`--more`) retain the first page's frozen results.
- `postcompact` uses the same full freshness path: although recovered rows belong to the
  caller, its displayed family root is resolved through other indexed sessions.
- Board and tail read native stores through the live service, without the index.

## Refresh pipeline

`store::refresh`:

1. Takes `writer.lock` as described above.
2. Reads `harness_prefixes.txt`; a digest different from the last refresh's re-parses
   every source.
3. Uses the caller's census, or takes one for an explicit refresh, and compares it with
   `sources`. With nothing new, changed or
   removed, it returns without a transaction.
4. Parses the changed sources, adapters in parallel (up to four threads; eight for the
   first build or a full refresh). One-source updates parse inline; small record, event,
   graph and bound batches stay serial so they do not create idle worker pools. pi, Claude and Codex `.jsonl` sources resume from a
   cursor in `parser_cursors`: byte offset, record count, file identity, parser state, a
   SHA-256 digest of the first and last parsed 64 KiB chunks, and pi's header and
   current leaf. Claude, Codex and pi transcripts are append-only logs, so resume
   verifies file identity, length at least the saved offset, and these two digests;
   a mismatch re-parses from the start. This reads appended bytes plus at most two
   chunks on every platform. `index --full` always re-parses every source.
   A torn final record waits for the next refresh. A pi append whose parent is not the
   previous leaf is a branch: the active branch is rebuilt from the stored entry graph
   (`parser_graph`) instead of rereading the file. Other formats re-parse the changed
   source.
   Changed file sources parse ahead in source order within a 512 MiB reservation
   window, including the source being written. Active parses reserve their input
   size; completed parses return unused credit after conservatively accounting for
   retained rows, the compact graph and prepared tool text. A dedicated feeder can
   reuse that credit while the writer is busy, handing a source to a parser only when
   one is free and only while the parsed sources the writer has not yet taken are
   estimated at no more than 128 MiB, so parsers that outrun the writer stop instead of
   piling rows up. Sources within the window of a 16 MiB or larger source skip that
   limit, so the large parse starts early enough to overlap earlier writes. A larger
   source initially reserves the whole window. Checkpointed JSONL uses a 16 MiB raw
   buffer; a record which
   exceeds that buffer spills to a private temporary file. A completed spill up to
   256 MiB uses a read-only view for slice parsing; only one view is live at a time,
   and it is released before row extraction. Larger records keep using the bounded
   reader. Oversized plain ASCII
   strings are validated in bounded chunks and decoded only if projection retains
   them; escaped, non-ASCII or malformed strings use the ordinary reader to preserve
   decoding and diagnostics. Pi validates unused
   metadata without building its JSON tree and borrows its compact graph during
   row extraction, avoiding a second tree. Large Claude and Codex inputs use a
   private temporary snapshot whose lines can be replayed for attribution.
   Normalized rows and pi's compact graph remain live until that source is written;
   retained message content and an individual decoded JSON string can exceed the
   raw buffer.
5. Writes, in one transaction:
   - the directory listings, and the removal of removed sources' rows;
   - each parsed source's rows, classified (user, subagent, recap, control, synthetic,
     harness), with its cursor and graph pages. A resumed source upserts the rows its
     new bytes produced and removes none. A source whose parse failed keeps its rows,
     and the error is recorded on its `sources` row;
   - fingerprints, and the deletion of tool texts no source references;
   - a canonical replay of each touched session held by several sources (resumed or
     copied transcripts): every copy is kept, and each row any copy holds is visible
     once. Of each turn the copy with the longest reply, then the most metadata, is
     canonical; another copy's reply that the kept ones do not contain stays visible on a
     turn of its own. Turns are renumbered by time only when their numbers collide. Of
     each tool call the copy with a real (not omp-elided) result, then the longest
     output, is canonical; another copy stays visible unless a kept one contains its text
     or it elides the result of the same input. Equal-quality ties go to adapter
     discovery order for copied pi chats, then source path; pi chats keep the first
     surviving parent claim in that order. Their project and first request follow it too.
     Family roots follow unambiguous filename aliases after a root's header id changes;
   - the FTS changes (below), the tool dictionary, block bounds, session families, prose
     trigram counts and family token statistics;
   - the deletion of sessions left without rows, and the refresh time.
6. For the first build and `reindex`, writes `index.building.db` with a rollback journal
   and creates secondary indexes and triggers after the bulk insert, then queues every
   visible row for embedding. On commit it switches to WAL. A first build renames the file
   to `index.db` and fsyncs the directory. A reindex copies into it the vector chunks and
   blocks of `index.db`, the references of rows whose content key had one, and the `state`
   values it did not set. Then, on Unix, it takes an exclusive lock on `index.db`, which
   SQLite grants only while no other connection has read it (a WAL connection keeps its
   shared lock until it closes), checkpoints and empties its WAL, and renames the new file
   over it. Readers and writers open `index.db` with a read and then check that the file
   they opened is still the one at the path, reopening it if not, so none reads the new
   file's WAL against the old file. When another connection has the index open, or on
   Windows, it copies the new file over `index.db` with SQLite's backup API in one WAL
   transaction instead: readers keep their snapshot of the old index until it commits.
   Either way a crash before the end leaves the old index whole, and the result has a
   first build's pages.

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
  gram postings LZ4. Family token counts occupy ordered pages of at most 256 tokens;
  refreshes locate and update only pages holding changed tokens.
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

A refresh raises bounds using only changed visible rows. A removal, rewrite or
visibility change can leave older maxima in place: these are upper bounds, so they
admit extra candidates without hiding matches or changing scores. Such blocks enter
`bound_rebuild`; the merge worker recomputes one block per transaction, yielding to
queued writers, to tighten them later. First builds and explicit bound rebuilds compute
exact bounds. Base postings are partitioned into 64-block chunks. A refresh writes
per-block term raises without reading the old chunks; queries merge both halves in
one SQLite snapshot. The worker folds raises into exact base postings and clears them
in the same transaction, so concurrent readers cannot lose a term during that move.
Each block's gram presence is a fixed 5,999-byte bitmap, merged with new grams without
materializing its previous vocabulary. Metadata updates that cannot change bounds do
not dirty them.

Family parent and filename-alias claims are persisted. An append with unchanged claims
needs no family traversal; changes revisit the affected names and their descendants.
Turnless chats (tool calls but no turn) stay listed and searched, dated by their calls
and titled by the first;
that settling likewise visits only sessions touched by the refresh.
Copied pi replays derive discovery keys from their sources' ancestor listings, using
files before child directories below each store root. Unavailable paths retain the
native discovery fallback.

## FTS indexes

- `prose_fts`: contentless FTS5 (`contentless_delete=1`), trigram tokenizer,
  `detail=full`, over Python `re.IGNORECASE` equivalence keys. Each input character
  maps to one representative of its case class; accents are not composed and
  compatibility characters are not expanded. The same keys feed tool words and
  block bounds. Verification maps matches back to original characters and uses
  Python's raw word boundaries. The Greek iota class, whose case key changes word
  membership, uses a raw matcher. 0.4.9's lowercase shortcut for dotted İ is
  preserved, including lowercase-first occurrence selection and the raw literal
  fallback. Python Unicode 16 lowercase data also pins contextual final sigma.
  Sparse `prose_case_fts` and `tools_case_fts` indexes reuse the prepared keys only
  for rows where that shortcut differs from the case key; plural counts verify
  their matches with one sparse MATCH query. Both sparse indexes use literal
  trigrams so the tool dictionary need not expand words a second time.
  NFKC remains in the separate boundary-ranking statistics.
- `tools_fts`: contentless FTS5 with `unicode61 remove_diacritics 0` word tokens and
  positions. SQLite truncates tokens at 32 KiB, so words over 8 KiB are indexed as
  overlapping 8 KiB windows and literals up to 4 KiB stay findable.
- Tool dictionary: `tool_words` holds each indexed word of three or more characters with
  its document frequency and the tool blocks containing it, `tool_lexicon` is a trigram
  FTS5 index of those words (`detail=none`), and `tool_trigrams` holds the summed
  frequency of the words containing each trigram. Frequencies use per-row additions and
  removals tokenized exactly as FTS5, without traversing existing term postings. Initial
  block-presence blobs are immutable; keyed `tool_word_blocks` additions extend them
  without decoding the base lists. Presence may conservatively retain removed blocks.
  A term of 3 to 4,096 ASCII letters and
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
  use the rarest groups. Phrase anchors use literal spellings rather than inflections.
  Distinct single-word expansions with no extra token gaps
  first use FTS5's native phrase check for consecutive groups, or its proximity check
  for spaced groups, then the same ordered-position verifier.
- Terms shorter than three characters stay out of the FTS expression and are verified on
  row text. Every candidate is verified on its stored text before it scores.
- For a single ASCII alphanumeric tool term, indexed word occurrences in the folded text bound
  the snippet's repetition score. A positive literal occurrence count proves the match;
  when its score ceiling loses to the page, it is counted without decoding or scoring.
  Other query forms and rows that can enter the page retain canonical text verification.
  ASCII tool blocks also bound snippet repetition directly from FTS token positions.
  Matched tokens supply their exact word widths; unmatched tokens need at least one
  character and a separator. These minimum distances can only overestimate how many
  occurrences fit in a snippet. Windows no wider than the long-token chunk overlap
  retain a complete posting copy. Other text keeps the global occurrence bound.
  Unscoped tool-only searches read a block's ranking metadata once through the existing
  tool-ID index. The FTS cursor retains possible page winners and counts the rest only
  when a strict bound proves that the original uncapped row ceilings remain above every
  possible page cutoff; otherwise the original cursor runs. Retained candidates carry
  their occurrence ceilings forward as the page improves. Broad expansions walk each
  word's postings separately and union IDs and capped counts.
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
Tool candidates defer session and symbol names until a filter, an equal-time tie, or a
surviving row needs them. On ASCII all-terms rows, first fully aligned term spans can
supply snippet boundary spans without regrading. Doubled spelling edges retain the
original path because a cut can duplicate its neighbour. A contiguous window cannot
introduce an absent alphanumeric phrase, so it needs no second phrase scan.

Recency, relative `--since`/`--until` bounds and displayed ages (`36m`, `2d`,
`last_indexed_age_s`) are measured from the system clock, or from `AGREP_NOW_MS`
(milliseconds since the Unix epoch) when it holds an integer. The variable is a
reproducibility knob for tests and for comparing separate runs byte for byte, not a user
setting: without it, rows near a four-place rounding boundary can swap between two runs
seconds apart. Page expiry, locks, logs, archive scheduling, live liveness and freshness
stamps always read the system clock, since they measure real elapsed time.

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
whose matches did not fit the fetched rows, a meaning scan
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
  an index change, in its own process group with stdin null and its output in
  `log/post_index.log`. Past ten minutes the group is killed. A failure or time-out is one
  stderr line and never fails the command that ran it.

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
- JSONL resume checks the first and last parsed chunks under the append-only log
  assumption; use `index --full` after an in-place edit confined to the middle of a
  parsed prefix. Other formats are re-parsed whole when they change.
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
