# agrep 0.5 output contracts

This page defines the public command interface and output formats.

## Process and framing

- Exit **0** means success or a result; **1** means a supported absence in a usable
  snapshot; **2** means invalid input, an unsupported data format, or a required
  operation that could not complete. Live commands preserve their documented
  interrupt and child exit behavior.
- stdout contains requested results. Progress and diagnostics go to stderr.
  A closed output pipe must not produce a traceback.
- Search JSON and chats JSON are JSON Lines: one `kind: "agrep-meta"` envelope,
  then public row objects. An empty search has one envelope with `hits: []`.
  Do not flatten the envelope into every hit.
- Search TSV has six columns in this order: **full session, agent, display
  project, turn, who, snippet**. A missing turn is empty. Fields escape or
  sanitize embedded tabs, newlines and terminal controls. `--flat` selects TSV;
  pipes outside an agent profile also default to TSV. A row standing for fork
  copies ends its snippet with ` (also in N forks)`.
- `-c` prints the decimal match count, or `>=N` for a measured floor.
  `--count-by-tier` prints `phrase_aligned=N phrase_partial=N
  phrase_interior=N all_terms=N total=N` on one line. Counts advertised as exact
  must enumerate all eligible matches.
- Metadata describes engine, applied filters, freshness, caller exclusion and
  completeness once. Public rows omit private implementation fields.

## Search and completeness

A hit identifies its session, agent, project, turn, speaker and snippet. Where a
verified reference exists, `handle` carries it. Ranking distinguishes phrase,
all-terms and labeled content-term fallback; meaning rows are labeled meaning
matches. `--lexical` keeps keyword search, `-s` requests meaning search explicitly.

Completeness uses `shown`, `total`, `unit`, `total_basis` and `truncated`.
`total_basis` is `exact`, or `floor` for a measured lower bound. A page length is
not a corpus total. Unmeasured tool counts are omitted: with a floor total, JSON
drops `tool_rows` and sets `tool_rows_basis` to `unknown`. A cut page names a
bounded larger page as `more_command` (`more_command_kind: "broader-rerun"`) and
the exhaustive keyword rerun as `full_command`; piped text says the same in one
`showing N of M` line. Exhausted exact
pages do not suggest more results. `--more` reads frozen rows, rather than silently
rerunning a query against a changed corpus.

A message that several chats hold as copies (a pi or omp fork copies its parent's
history, so a fork's copy has the same agent, speaker, text and time) is one row on a
keyword or meaning page: the copy from the most recently active chat, marked `also in N
forks`, with the other copies under its JSON `forks` (session and handle). The copies
take no row of the page: `-n N` bounds the printed result rows in every form (terminal,
compact, TSV and JSON), and the page ranks deeper to fill them. A row's copies are found
however deep the page ranked, so a row carries the same marker at every `-n`. `shown`
and `showing N of M` count the copies, in the unit of `total`, which is never below
them. `-l` lists every chat and `-c` counts every row.

Normal commands check source files and answer from a committed snapshot.
Index-reading commands finish their source census and ingest its changes before querying.
`postcompact` also refreshes the sessions used to resolve its displayed family root.
Changes present when a command began are included; a record still being written at that
instant can be absent. Freshness records and status contain no pending-source count or
behind notice. Background maintenance does not ingest source changes. `--more` reads the
first page's frozen results.

`--no-auto` and a read-only data root suppress automatic freshness work and mark freshness
unchecked. `--no-auto` retains its existing unchecked notices and machine records.
A parse failure keeps the last-good rows and is reported with its source by
status/doctor.

Caller context excludes the calling agent's current window by default when
identified; older same-session evidence is labeled `~self`. `--self` includes
the current window, `--no-self` excludes the known session family, and `--no-side`
excludes side conversations. Unknown identity is not invented. Project filters
match exact project/folder names or explicit globs; Board's project filter is a
substring filter.
`--chat` on a Codex fork child, whose rollout begins by replaying its parent's history,
also covers the parent's rows up to the fork, which are indexed once under the parent,
and says so in one stderr line.

## Meaning search

Query embedding happens in the executable. If the model is absent, automatic
hybrid search uses keywords; status reports the missing model. Explicit `-s`
prints `semantic model not installed: run agrep setup` and exits 2. It must not
report that as a verified zero match.

Only vectors belonging to the exact snapshot text can supply a meaning hit. Before
scanning, the lane embeds queued text with the mapped CPU query model and includes those
vectors with the published blocks. A lane that answers covers every visible prose
row in that snapshot; `semantic_coverage`, when non-null, is always complete.
There is no `semantic_partial` field or coverage notice.

When the queue exceeds about one second of estimated CPU embedding, automatic
hybrid quietly returns keyword results. Explicit `-s` exits 2 with
`semantic search unavailable: the meaning index is still building (N of M rows, about T left)`,
or `estimating time left` in place of `about T left` until a drain on this machine has
measured how fast rows embed (a rebuild keeps the rate the index it replaces measured).
Under `--json`, it returns `error.code: "semantic-unavailable"`, no hits, and
`progress` in its semantic status: `indexed_rows`, `total_rows`, `remaining_rows`,
`estimated_seconds` (null while estimating). A build is not a verified zero match. `status`
shows this progress; setup says the build runs in the background and when meaning joins
searches.

## Handles and around

A result handle has the form `@session:turn.digest`. Session identifiers may use
an opaque encoding; tool handles can also include an event selector and matched
span. A digest is an accidental-drift check, not a cryptographic signature.

`around SESSION TURN` is exploratory: an out-of-range turn may clamp, with the
served change disclosed. An exact result handle must verify the content claim;
it never silently clamps. A uniquely moved match can be rescued with disclosure;
a missing or ambiguous match is refused with exit 2. Tool identity is verified
before applying a span. Earlier agrep's serialized references have no migration
promise; copy handles from current results.

`around --json` starts with metadata and optional served divergences. Prose rows
have `kind: "msg"`, session, project, turn, who, timestamp, text and omitted
character counts. Reply rows may carry original reply length and truncation.
Events identify kind/name/input/success and input/output lengths, truncation,
optional payload bounds and match previews. `--tool-output N` bounds raw output;
`--no-tools` hides tools. `--full` removes the display cap and includes tool
results, without inventing bytes absent from the source.

## Recall, pack and postcompact

Recall and pack JSON are a single object with query, engine, hits, windows,
omissions and completeness. `--probe` returns one pointer or a qualified miss
using exit 0/1/2. Human-readable pages retain enough context to judge a hit.

A finite `--budget` limits UTF-8 bytes actually written, including ANSI and the
final newline. It never splits a codepoint. Zero means uncapped; the finite
minimum is 64 bytes. Hit turns take priority over surrounding context; whole
omitted hits are counted. If even the first reference plus required framing
cannot fit, refuse with exit 2 and name the bytes required. Never cut a handle.

`postcompact --json` emits one `kind: "agrep-postcompact"` packet with
`schema_version`, status, authority, selection, coverage, rows, omissions and
navigation. It is supplemental: the user's current request and context win.
Recovery is root-session-only and tied to the exact structural boundary. It does
not perform a semantic search or silently widen to side sessions. At most eight
blocks and 8,000 text / 16,000 JSON bytes are returned. Exit 0 is recovered,
1 verified empty, 2 partial or unavailable. Failure packets retain authority and
`implicit_widening: false`.

## Live output and diagnostics

`tail` is JSONL. `type: "tail_ready"` means its initial census is complete and
the subscription is armed. Events use `user`, `reply`, `tool`, `tool_done`,
`done`, `queued` and `subagent_result`. A snapshot has `type: "snapshot"`.
Apply filters before counting, and omit unscoped errors/counters in scoped
snapshots. Emit a complete event before acknowledging its position. Ctrl-C
exits 130 after any already-emitted complete line.

Board JSON is a bounded `kind: "agrep-board-snapshot"`, `scope: "live-window"`
object. It includes sessions, selector, sort, source, age/window information and
completeness. `matched_basis`, matched, shown, session/page completeness and
source errors must distinguish an empty live set from incomplete discovery.
A partial page supplies a bounded rerun or argv. Feed truncation is independent
of session completeness. Interactive Board restores terminal state on exit;
POSIX bracketed paste is treated as input rather than commands.

`why --json` is one object with version 1, reference, verdict, exit, summary,
candidates, evidence and next action. It returns 0 for indexed, 1 for explained
absence, 2 for ambiguity or inability to prove the answer. `audit` recounts
independently of cached ingest counters; strict mode promotes coverage gaps.
Status/doctor distinguish unchecked from failed, list named parse errors, and
show model/coverage information without obsolete process coordination states.

`status --json` is one bounded object whose size does not grow with the history:
`version`, `data_root`, `format`, `disk_bytes`, `disk_files`, `index` (`state`, and when
ready `counts` and `last_refresh`), `last_indexed_age_s`, `sessions`, `messages` (user-side
prose rows, each with its reply, as `index` and a keyword miss count them), `prose`,
`tools`, `source_errors`, `agents` (per agent: `agent`, `sessions`, `sources`,
`parse_errors`), `semantic`, `integrations`, `archive`, `permissions` and `freshness`. It
lists no individual source. `doctor --json` is `{"version":1,"status":…,"checks":[…],
"repairs":[…],"exit":…}`: its `status` adds `sources`, only the sources whose last read
failed (path, agent, bytes, mtime, last error, change token); each check has `check`,
`state` and `detail`, and a problem's check adds `action`, the step that resolves it. The
`semantic_model` check is `present`, `not installed` (`agrep setup` installs it) or
`failed`; on a home where no supported agent keeps a store, an `agent_history` check
names the home and the agents looked for. The text form prints one line per check, then
one `source failed: PATH: ERROR` line per failed source, ERROR being the cause alone
(`permission denied`).

Setup/remove preserve text outside their owned instruction blocks and remove
only enrolled integrations or recognizable agrep blocks. Archive restore checks
content and requires explicit `--force` to replace an existing file.

## Explicit rebuilds and optional meaning failures

`reindex` rebuilds derived rows and keeps vector references of rows whose text is
unchanged; `reindex --full` also invalidates every vector reference and queues visible
rows for embedding. `index` and `reindex` print 0.4.9's human report
(message and session counts, the refresh's time and phases, and the setup pointer
while no agent is enrolled). With `AGREP_QUERY_TIMING` set they also print the
refresh's statistics as a JSON `{"index": …}` line on stderr. While any source's last read
failed, they print one stderr line with the count, the first such source and its reason,
and, when there are more, `agrep doctor` to list them. While they embed, a stderr that is
not a terminal gets an `embedding messages: …` progress line every 30 seconds and one with
the final count; stdout gets none.

Only a missing model quietly disables automatic meaning retrieval. In automatic hybrid
retrieval, any failure from an installed model is disclosed once on stderr and written to
the diagnostic log, and the keyword results stand; recall/pack JSON marks that semantic
attempt as failed. An explicit `-s` (search, recall, pack) never answers with keyword rows:
when its lane fails or the meaning policy refuses its query, it prints `semantic search
unavailable: <reason>` (a model that fails its pinned size or hash adds `` - `agrep setup`
reinstalls it``), or under `--json` 0.4.9's record with `error.code`
`semantic-unavailable`, and exits 2. `status` and `doctor` verify installed model files
against their pinned hashes and unchanged verification stamps.
