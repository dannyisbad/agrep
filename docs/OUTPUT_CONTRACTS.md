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
  pipes outside an agent profile also default to TSV.
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

Normal commands check source files and answer from a committed snapshot.
`--no-auto` suppresses automatic freshness work and marks freshness unchecked.
A parse failure keeps the last-good rows and is reported with its source by
status/doctor.

Caller context excludes the calling agent's current window by default when
identified; older same-session evidence is labeled `~self`. `--self` includes
the current window, `--no-self` excludes the known session family, and `--no-side`
excludes side conversations. Unknown identity is not invented. Project filters
match exact project/folder names or explicit globs; Board's project filter is a
substring filter.

## Meaning search

Query embedding happens in the executable. If the model is absent, automatic
hybrid search uses keywords; status reports the missing model. Explicit `-s`
prints `semantic model not installed: run agrep setup` and exits 2. It must not
report that as a verified zero match.

Only vectors belonging to the exact current text can supply a meaning hit.
Coverage is a number, such as `semantic 164499/173524 rows`. A completed
semantic-only miss with partial coverage exits 1 and states the coverage.

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

Setup/remove preserve text outside their owned instruction blocks and remove
only enrolled integrations or recognizable agrep blocks. Archive restore checks
content and requires explicit `--force` to replace an existing file.

## Explicit rebuilds and optional meaning failures

`reindex --full` rebuilds derived rows, invalidates every vector reference and
queues visible rows for embedding. `index` and `reindex` print 0.4.9's human report
(message and session counts, the refresh's time and phases, and the setup pointer
while no agent is enrolled) and write the refresh's public statistics, with the
time each lock file blocked the command, to `index-stats.json` in the data root.

Only a missing model quietly disables automatic meaning retrieval. Any failure
from an installed model is disclosed once on stderr and written to the diagnostic
log; keyword results are returned even for an explicit `-s` request. Recall/pack
JSON marks that semantic attempt as failed. `status` and `doctor` verify installed
model files against their pinned hashes and unchanged verification stamps.
