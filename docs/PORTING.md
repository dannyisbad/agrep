# Deliberate differences from 0.4.9

agrep 0.5 matches 0.4.9's output except where an entry below says otherwise, and each
entry gives its reason. Browse and diagnostics differences are in [BROWSE.md](BROWSE.md).

## Search, recall and pack

- The JSON envelope names the keyword engine `sqlite` and has no publication age, owner or
  generation fields: the daemon and publication machinery they described is gone.
- JSON lines use compact separators and 0.5's own key order everywhere (0.4.9's around
  spaced its separators); decoded values are identical: one serializer writes every surface.
- Meaning-match rows carry `(meaning match)` after `~semantic`: 0.5 labels every semantic
  row as a meaning match, so it never reads as a keyword hit.
- Keyword search hits and `chats` rows carry no `concept`, and `chats` names a chat by its
  project, while `-s` rows carry `concept` and `summary` always empty: 0.4.9 filled them
  only from optional concept-naming and summary stages that 0.5 does not run, and printed
  them empty, naming the chat by its project, without them.
- A handle's session prefix is unique among the indexed chats, so a side chat whose parent
  was never indexed gets the shortest prefix (`@01990003`) where 0.4.9 also lengthened it
  past the missing parent's id (`@01990003-0003`): 0.5 resolves a prefix against indexed
  chats only, so the missing parent cannot make it ambiguous.
- No automatic over-specification lane runs, so a compact page 0.4.9 would have retried
  keeps its exact count and notice: coverage recovery is the explicit `--coverage` lane.
- The `--coverage` lane ranks prose and tool output each by bm25 in its own index and
  merges the scores, where 0.4.9 ranked both in one trigram index: 0.5 indexes tool
  output by word, so no one index holds both. A query word holding anything but ASCII
  letters and digits matches no tool row there, and rows with equal scores keep their
  index order where 0.4.9's order was SQLite's choice, so a rerun lists them the same.
- Under `--no-meta`, recall and pack take each chat's best row from the ranking `--chats`
  uses, which folds tiers into one score, where 0.4.9 took it tier-first: one chat ranking
  serves both, and they differ only for a chat whose best phrase row scores below one of
  its all-terms rows.
- A one- or two-character query scales harness and synthetic rows and a subagent's first
  turn by 0.45 like any other query, where 0.4.9's fast path for such a query without
  filters left ASCII rows unscaled: 0.4.9 scaled the same rows once a filter sent that
  query down its ordinary path, so its order depended on whether a filter was given.
- A tool event keeps only its trimmed search text, so `--tool-output` and JSON
  `input`/`output` lack the surrounding whitespace 0.4.9 stored separately, and
  `[+N indexed chars]` counts the trimmed output: the index stores one text per event.
- `--more` serves a page's frozen rows until its TTL, as 0.4.9 did, without 0.4.9's
  `corpus changed since this page was frozen; newer results may exist - rerun the search
  to refresh` line when the index changed meanwhile: a 0.5 page keeps its rows, query and
  totals but not the state of the index it came from, which the background refresh
  changes between almost any two pages while an agent works.
- Recall's structured hit also carries its verified handle and `self` bit, and source reply
  truncation is known from retained reply metadata where 0.4.9 reported it as unknown: 0.5
  keeps that metadata.
- A meaning lane's `semantic_status` (the `semantic` object of `-s --json`) carries no
  `filtered`, `truncated` or `corpus_anchor` and the envelope no
  `semantic_accelerator_coverage`: 0.5's vector scan stops at the score floor instead of
  counting the rows below it, keeps one vector store whose coverage is `semantic_coverage`,
  and probes the scope for the query's words only when the keyword lane found nothing,
  since a keyword row already proves one occurs.
- An explicit `-s` whose lane failed gives the failure's cause as its reason, and a pack
  whose lanes failed for different causes says `semantic search unavailable: the meaning
  lane did not answer` without 0.4.9's `; it retries automatically`: 0.5 has no daemon to
  retry the lane.
- A page or recall whose automatic meaning lane has no model to run says `meaning
  unavailable; keyword-only (semantic model not installed: run agrep setup)` every time,
  where 0.4.9 named its own cause (its model cache or embedding backlog) once per notice
  window and the bare line after that: 0.5 keeps no notice state between commands, and
  the missing model is its only such cause.
- A meaning row's `sem_score` can differ from 0.4.9's in the fourth decimal (0.882033
  where 0.4.9 said 0.881866): 0.5 embeds with the model's full-precision weights, where
  0.4.9 ran reduced-precision (int8 or fp16) copies.
- A meaning row counts as weak when it holds fewer than half of the query's words, each
  word weighing the same, where 0.4.9 weighted the words by how many chats hold each once
  a row held some but not all of them: counting a word's chats scans its whole posting
  list on every hybrid search (half a second for a common word on a real history).
- When the content fallback searches tool output too, a word's weight counts its tool rows
  by the tool word dictionary's frequency total, where 0.4.9 counted them exactly: an
  exact count reads the word's whole tool posting list (three seconds for "error" on a
  real history). Prose rows are counted exactly, so recall's prose lane matches 0.4.9.
- A lane skipped for a missing model says `model-not-installed` where 0.4.9 said
  `runtime-not-installed`: the model is the only piece 0.5 installs.
- Digestless `@session:turn` handles are refused as malformed (exit 2) where 0.4.9 served
  them by position with a warning: 0.5 drops compatibility parsers, and
  `around SESSION TURN` navigates by position.
- A total the ranked scan stopped short of is a floor (`showing 40 of 118+ matching rows
  (a floor, not the total; -c counts exactly)`, compact `118+ matches (floor; -c exact)`,
  JSON `total_basis: "floor"` with `tool_rows_basis: "unknown"` instead of `tool_rows`)
  where 0.4.9 printed the exact total, so a page holding every row can still say `+`: an
  exact total costs a full count on every search, and `-c` gives it.
- On a terminal a cut classic page ends with the same `showing N of M …` line a pipe
  gets, not 0.4.9's `N hits in C chats · keyword · 12ms` summary: counting the matching
  chats would cost the same full count.
- When stdout and stderr share one pipe, a classic page's rows come before its stderr
  lines, in the order a terminal shows them: 0.4.9 held piped rows in its output buffer
  until it exited, so its notices landed above the rows.
- Compact pages offer no `agrep --deeper` broader rerun: 0.5 drops the hidden `--deeper`
  replay; the larger `-n` rerun stays.
- A bad `-E` pattern is reported with 0.5's regex engine's message (`bad regex: Parsing
  error at position 1: Opening parenthesis without closing parenthesis`, also the JSON
  record's `reason`) where 0.4.9 quoted Python's `re` (`missing ), unterminated subpattern
  at position 0`): 0.5 has no Python.
- A recall or pack handle that names no turn prints 0.5's handle message (`result handle
  is stale: turn 99 is outside chat turns 0-2; rerun the search for a current handle`),
  also the JSON record's `reason`, under 0.4.9's codes: 0.5 handles carry a content
  digest, and the message says which part no longer resolves where 0.4.9's said only that
  no session matched.

## Caller exclusion, truncation flags and terminal quoting

- summary, chats and around also carry the self-exclusion disclosure (summary and chats keep
  `excluded_hits` null because they count chats in `self_excluded`, and a bare `chats`
  listing reports `not-applicable`): every surface that withholds rows says so.
- `-s` results keep `excluded_hits` null where 0.4.9 ran semantic probes: 0.5 has no exact
  semantic count to subtract.
- Indexing diagnostics use the shared `terminal_safe` quoting, where 0.4.9's Rust helper
  quoted with four or five hex digits instead of `\U` and also escaped the two joiners: one
  sanitizer serves every surface.

## Status

- `status` prints one line per remaining fact without `[ok ]` markers instead of 0.4.9's
  sectioned doctor report: its other rows (ingest binary, Python, runtime and installed
  build, daemon freshness, search database generation) describe machinery 0.5 removed.
- The counts line names sessions, prose rows and tool rows where 0.4.9 counted messages per
  agent: tool rows are searchable.
- The JSON object is 0.5's own: `version`, `sessions`, `agents`, `last_indexed_age_s` and
  `freshness` keep their names; 0.4.9's data-dir, index, semantic and instruction keys
  become `data_root`, `format`, `index`, `semantic` and `integrations`, `messages` and
  `per_agent` become `prose`, `tools` and `sources`, and 0.5 adds `disk_bytes`,
  `disk_files`, `archive`, `permissions`, `source_errors` and `pending_sources`. The
  daemon, diagnostics-tier, warning and build-identity keys are gone with that machinery.
- A bare `agrep` runs `status` rather than 0.4.9's welcome page of recent chats and example
  commands: one status page serves both, and `agrep chats` lists the recent chats.

## Around, summary and usage errors

- Sources changed since the last refresh appear as `pending_sources` on around's trailing
  freshness record, with no stderr line, where 0.4.9 printed an `index-behind` record with
  `changed_stores` and `serving the last-good index (N stores changed)`: 0.5 refreshes
  them in the background as it answers, so the count is the whole story, as in search,
  recall and chats. Under `AGREP_DATA_READONLY` the record says `unchecked`: 0.5 skips
  the source check when it may not write.

## Command line and help

- Help pages and argument errors use clap's layout and wording (`Usage:`, `-h, --help  Print
  help`, `error: unexpected argument '--x' found`) instead of argparse's: 0.5 parses every
  command with one clap parser. Each page keeps 0.4.9's descriptions, option text, examples
  and exit lines wherever the command behaves the same; the refusals 0.4.9 worded itself
  (option gates, time windows, `--more`, unknown agents) keep their wording, without the
  `; to search for the word "tail": agrep search tail …` hint argparse appended to a
  subcommand's refusal, which only the `unrecognized arguments` refusal of `chats`, `recall`
  and `pack` keeps; and every argument error still exits 2, as 0.4.9's did.
- `status`, `resume`, `ui` and `serve` also accept `--no-auto`, which 0.4.9 refused with
  exit 2: in 0.5 each of them checks the agent stores for changes itself, as search does,
  where 0.4.9 left that to its daemon. `index --semantic` is new: it downloads the model if
  needed and embeds every row before exiting.
- search's `--strict-semantic` is gone (exit 2): it was a compatibility alias that changed
  nothing, since `-s` already exits 2 when meaning is unavailable.

## Index and reindex

- `index` prints 0.4.9's report without the lines about files 0.5 does not keep (the
  per-agent parse lines, the `-> messages.jsonl` target, and the `replies.jsonl` and
  event-stream lines with their counts), and its phases are 0.5's: census, parse, write,
  bounds and stats, or only the census for an unchanged pass: 0.5 keeps one SQLite
  database instead of those files.
- `reindex` prints that report under 0.4.9's `=== ingest transcripts ===` stage, without
  its `(rust)` label or its `no new messages since last index` shortcut: 0.5's reindex
  always reparses every source, so it always has an index to report.

## Integrations and archive port

- Setup replaces every agrep-marked block, including an edited one: the marked block is
  agrep's to own. Unmarked skills and edited helper files stay protected.
- Hook registration adds agrep's entry beside a user's own hooks for the same event, where
  0.4.9 registered nothing when a hook it had not installed was there: recovery works
  without displacing the user's hook. It rewrites only the top-level `hooks` value, in the
  file's indentation, separators, `\u` escaping and line endings as 0.4.9 detected them,
  and leaves the rest of the file byte for byte where 0.4.9 re-serialized all of it in that
  style: the user's other settings keep their exact text.
- Consent and completion output omit the dependency, daemon, owner and sentinel stages: those
  stages no longer exist. Invalid settings print `agrep: tools must be on/off` without
  0.4.9's `agrep set:` prefix and its `to search for the word "set": …` hint: one error
  path renders every command's failures.
- Archives use a version-1 SQLite catalog and immutable 1 MiB SHA-256 gzip chunks and never
  read a 0.4.x manifest or xz blob: 0.5 shares no data with 0.4.x. The stored size counts the
  compressed chunks alone, where 0.4.9 counted every file in its archive directory, so it and
  the `x smaller than the originals` ratio differ for the same files: the catalog's fixed
  database pages would outweigh a small archive's copies. An explicit capture lists its files
  in path order, not 0.4.9's agent order: one sorted walk also drops a file two agents share.
- Scheduled captures run inside `__work` at most once per ten minutes and snapshot a changed
  SQLite store at once through the backup API, where 0.4.9's daemon checked every minute,
  re-imaged a file rewritten in place at most every ten minutes and, explicit captures
  included, deferred a SQLite store written in the last 15 minutes: there is no daemon tick,
  and a backup-API snapshot is consistent while the store is in use. A file written while it
  is copied, or a SQLite store a scheduled snapshot finds locked, fails for that pass where
  0.4.9 listed it as deferred: the next pass retries it either way.
- A forced restore into an existing SQLite file writes the archived contents into it through
  the backup API under SQLite's write lock, where 0.4.9 replaced the file and deleted its
  `-wal`, `-shm` and `-journal` files: open connections keep a coherent database instead of
  one swapped beneath them. A restore refuses a destination holding such a file without its
  database, which 0.4.9 deleted: it may hold the only copy of committed writes.

## Distribution, CI, release and documentation

- No sdist, no runtime Python, and npm downloads the standalone binary: 0.5 ships one
  binary with no runtime Python.
- `--version` carries no distribution, runtime or build identity, and there is no legacy
  install recovery: 0.5 has no such distribution or install machinery to report.
- Binaries build for the portable target baseline instead of `target-cpu=native`: one
  binary must run on every machine of its target.

## Browse and diagnostics port

Browse and diagnostics differences are listed in [BROWSE.md](BROWSE.md).
