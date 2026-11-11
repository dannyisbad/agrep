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
  totals across later source ingestion: a continuation belongs to its original answer.
- Recall's structured hit also carries its verified handle and `self` bit, and source reply
  truncation is known from retained reply metadata where 0.4.9 reported it as unknown: 0.5
  keeps that metadata.
- A meaning lane's `semantic_status` (the `semantic` object of `-s --json`) carries no
  `filtered`, `truncated` or `corpus_anchor` and the envelope no
  `semantic_accelerator_coverage`: 0.5's vector scan stops at the score floor instead of
  counting the rows below it, keeps one vector store whose coverage is `semantic_coverage`,
  and probes the scope for the query's words only when the keyword lane found nothing,
  since a keyword row already proves one occurs.
- A keyword row whose conversation family a meaning row already leads stays in the
  merged result, after every other row, where 0.4.9 dropped it as covered: on a history of
  orchestrators with many subagent chats, one meaning row hid every keyword hit of its
  family, and neither `--more` nor `-n` could show them. A compact page shows these rows
  if room is left, otherwise behind `--more`; its floor total counts them, and a page
  merged with meaning rows offers more whenever the row limit cut a merged row.
- Notes from omp's advisor sidecars (`__advisor.jsonl`, an automated reviewer watching a
  session) are left out of the meaning lane: they are not embedded and never return as
  meaning rows, even with `-s --who subagent`. 0.4.9 embedded them, and their short status
  notes (`Nothing to flag.`) crowded real answers out of `-s`. Keyword search, `--who
  subagent`, counts and `around` show them as before.
- A message that several chats hold as copies (a pi or omp fork copies its parent's
  history, ids and times included) is one meaning row: the copy from the most recently
  active chat, marked `also in N forks`, with the other copies under its JSON `forks`.
  0.4.9 folded rows only after the page was cut (and only on its compact and grouped
  pages, as `×N-chats`), so on the owner's history three copies of one turn took three
  of `-s -n 5`'s five rows and its JSON listed all three. 0.5 folds while it ranks, so
  the page fills with other answers, and it folds only copies of one message (same
  speaker, text and time), not different chats that typed the same words. `-l` still
  lists every chat and `-c` counts every chat.
- Keyword pages fold fork copies by the same rule: a message several chats hold as
  copies (same agent, speaker, text and time) is one row at its best copy's rank, shown
  from the most recently active chat and marked `also in N forks` (`(also in N forks)`
  on a compact row); its JSON row lists the other copies under `forks`. A copy differs
  from its row only in its chat, so it can rank just below the last row a page ranked;
  such copies are looked up directly, and a row folds the same way, with the same
  marker, at every `-n`. On keyword and meaning pages alike `shown` and `showing N of M`
  count the copies, so a page whose copies it stands for is not cut. 0.4.9 folded
  keyword rows only on its compact and grouped pages (`×N-chats`, `×N chats`), keeping
  the best-ranked copy, while `--json` and `--flat` listed every copy at its own rank;
  it also folded rows of different chats with the same speaker and words (one prompt
  sent to many subagents) and near-identical successful tool rows of one conversation
  lineage. 0.5 folds only copies of one message, in every page form, so keyword and
  meaning pages agree. `-l` lists every chat and `-c` counts every row, as in 0.4.9.
- `-n N` prints N result rows in every output form, and the copies a row stands for
  take none of them. A `--flat` (or piped) row that stands for copies ends its snippet
  with `(also in N forks)`, as the terminal row says `also in N forks`; `--json` lists
  the copies under `forks`. 0.4.9 printed each copy as a `--flat` and `--json` row of
  its own, so copies used up `-n` and a pipe got fewer answers.
- On an agent's compact page, `-n N` below 40 shows the first N rows of the `-n 40` page,
  meaning rows included: such a search ranks the 40 keyword rows (and automatic meaning
  rows) the page without `-n` ranks, merges and orders them as that page does, and `-n`
  cuts the result, so each page up to `-n 40` is the first rows of every larger one. 0.4.9
  ranked, merged and ordered only the rows `-n` fetched, so a larger `-n` could reorder a
  page and drop one of its rows. `-n 40` and above rank as before.
- `--chat` on a Codex fork child also searches its parent's rows up to the fork. The
  child's rollout starts by replaying the parent's history, which is indexed once, under
  the parent; 0.4.9 searched only the child's own rows, so `-c --chat <child>` answered
  `0` (exit 1) for text the child's file holds. One stderr line names the parent.
- Meaning search answers only from a complete snapshot. 0.4.9 searched the vectors
  already published, even with unembedded rows, and disclosed that gap through
  coverage numbers, partial flags and lines such as `catching up; retry shortly`.
  0.5 embeds a small queued tail inline with the mapped CPU query model. During a larger build,
  automatic hybrid silently uses keywords; explicit `-s` exits 2 with build progress.
  `semantic_partial` and the semantic status's `partial` field are removed;
  `semantic_coverage` is complete or null; `index --semantic` names a ready index
  instead of printing a coverage ratio. This prevents newly indexed prose from
  being silently absent from a meaning answer. `status` and setup explain the build.
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
  list on every hybrid search.
- When the content fallback searches tool output too, a word's weight counts its tool rows
  by the tool word dictionary's frequency total, where 0.4.9 counted them exactly: an
  exact count reads the word’s whole tool posting list. Prose rows are counted exactly,
  so recall’s prose lane matches 0.4.9.
- A lane skipped for a missing model says `model-not-installed` where 0.4.9 said
  `runtime-not-installed`: the model is the only piece 0.5 installs.
- Digestless `@session:turn` handles are refused as malformed (exit 2) where 0.4.9 served
  them by position with a warning: 0.5 drops compatibility parsers, and
  `around SESSION TURN` navigates by position.
- Tool calls of one turn that share their time and text each get a handle of their own,
  where 0.4.9 gave them one handle that `around` served with every such call. Parallel
  calls whose recorded input and output match are such calls, say two reads that differ
  only past the characters ingest keeps. The first by call id keeps 0.4.9's handle, and
  each other one's event selector also hashes its call id; every other handle is 0.4.9's.
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
  replay. Instead the last page of a cut search names the larger `-n` rerun itself
  (`118+ matches (floor; -c exact) · larger page: agrep --classic -n 80 -- deploy`), where
  0.4.9's last page pointed at `--deeper`.
- A cut `-l` page's floor says `(a floor, not the total)` without 0.4.9's `-c counts
  exactly`: `-c` counts rows, not chats, and refuses `-l`.
- A bad `-E` pattern is reported with 0.5's regex engine's message (`bad regex: Parsing
  error at position 1: Opening parenthesis without closing parenthesis`, also the JSON
  record's `reason`) where 0.4.9 quoted Python's `re` (`missing ), unterminated subpattern
  at position 0`): 0.5 has no Python.
- `-E` and `-w` find the rows 0.4.9 found but use the index differently. 0.4.9 narrowed
  an `-E` expression by a run of three or more plain ASCII characters outside its groups
  (`work` in `work(tree|flow)s?`) and read every row when it had none. 0.5 narrows by the
  literals every match begins and ends with, in any script, taking an end's alternatives
  together, up to 64 of them (`worktree` or `workflow`). The regex parser keeps only 100
  bytes of a literal, and 0.5 keeps the whole characters of those: an early 0.5 build cut
  a character in two there and so found nothing for `-E` with a long Japanese sentence.
  `-w` reads only the indexed words in which the term can stand alone (`work`, not
  `homework` or `worktree`), where 0.4.9 read every row holding the term.
- A recall or pack handle that names no turn prints 0.5's handle message (`result handle
  is stale: turn 99 is outside chat turns 0-2; rerun the search for a current handle`),
  also the JSON record's `reason`, under 0.4.9's codes: 0.5 handles carry a content
  digest, and the message says which part no longer resolves where 0.4.9's said only that
  no session matched.
- `run` refuses an unknown agent with `unknown agent 'x'; supported for agrep run: …` where
  0.4.9 said `supported for native resume`: the refusal names the command that was run.
- A `resume` whose agent fails to start names the system's reason in 0.5's wording
  (`couldn't launch pi: No such file or directory (os error 2)`) where 0.4.9 quoted Python's
  (`[Errno 2] No such file or directory: '…'`): 0.5 has no Python.
- `agrep run` or `resume` piped into another command (`agrep run claude | cat`) now stops
  its agent when you press Ctrl-Z and starts it again on `fg`, and the keys you type at
  the shell meanwhile reach the shell. 0.4.9 stopped only itself: it ran the agent cut off
  from the terminal's job control, so the agent kept running and read those keys. The
  agent now stays in the terminal's session. The terminal still belongs to the whole
  pipeline, so a pager reading it is not stopped, until the agent itself reads or sets up
  the terminal; then agrep hands it the terminal, as for a run without a pipe. In the
  background (`bg`, or a launch ending in `&`) an agent that reads the terminal stops with
  its job until `fg`, as any program reading the terminal does; one that never touches the
  terminal keeps running.
- The `resume` picker asks again after an answer that is neither a listed number nor a
  reference to exactly one of its sessions; 0.4.9 printed that message and gave up. A
  number is always a position in the list: 0.4.9 tried a number outside the list as the
  start of a session id, so a typo like `0` could resume a session whose id began with 0.
- `resume` names the program it starts: `↻ resuming omp · …` for an omp session, where
  0.4.9 said `pi` for both forks.
- Claude's resume directory comes from the session's plain transcript, the first in path
  order when several project folders hold one, where 0.4.9 took whichever its directory
  listing gave first, an earlier resume's link included: the choice no longer depends on
  listing order. When no transcript records a `cwd`, resume uses the indexed one before
  falling back to home.
- `summary` names a `--project` or `--agent` value the index lacks, as search does
  (`--project x isn't in the index - indexed projects: …`), where 0.4.9's summary printed
  only its empty table.
- `around` counts in the singular when one item is hidden (`1 tool event omitted · 1
  message omitted`) where 0.4.9 wrote `1 tool events omitted · 1 messages omitted`.

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
- The counts line names sessions, messages and tool rows where 0.4.9 counted messages per
  agent: tool rows are searchable. A message is 0.4.9's: one user-side prose row with its
  reply attached, the same count `index` reports and a keyword miss names.
- The JSON object is 0.5's own, a bounded summary as 0.4.9's was: `version`, `sessions`,
  `messages`, `agents`, `last_indexed_age_s` and `freshness` keep their names; 0.4.9's
  data-dir, index, semantic and instruction keys become `data_root`, `format`, `index`,
  `semantic` and `integrations`, `per_agent` becomes `agents` (each agent's `sessions`,
  `sources` and `parse_errors`), and 0.5 adds `prose`, `tools`, `disk_bytes`, `disk_files`,
  `archive`, `permissions` and `source_errors`. No source is listed one by one: `doctor
  --json` lists the sources whose last read failed. The daemon, diagnostics-tier, warning
  and build-identity keys are gone with that machinery.
- `doctor` prints one line per problem with the action that resolves it: a missing index
  (`agrep index`), each source that failed to read, a corrupt or half-installed model
  (`agrep setup` reinstalls it), an unwritable data directory. Background embedding logs a
  model's failed verification once and then leaves those files alone until they change.
- A source that failed to read is listed with its cause alone (`source failed: PATH:
  permission denied`), and `index` and `reindex` end with one stderr line naming how many
  failed, the first with its cause, and `agrep doctor` for the rest. A store folder that
  cannot be listed is that source, named as the folder itself with its cause, by `index`,
  `reindex` and `audit` alike, and `audit` goes on to count the rest of its store. 0.4.9's
  ingest printed a `cannot read` line per file as it went; 0.5 collects the failures and
  reports them once.
- On a home where no supported agent keeps a store, `doctor` says so and names the agents it
  looked for. Its model row reads `present`, `not installed` (`agrep setup` installs it) or
  `failed`, and it checks an installed model even before the first index.
- A data directory this user may not write behaves as under `AGREP_DATA_READONLY`: commands
  answer from its index with freshness unchecked, after one stderr line naming the directory,
  and `doctor` names it. Without an index there, the refusal names the directory.
- A bare `agrep` runs `status` rather than 0.4.9's welcome page of recent chats and example
  commands: one status page serves both, and `agrep chats` lists the recent chats.

## Around, summary and usage errors

- 0.4.9 served the last-good index and said it was behind when transcripts had changed.
  0.5 indexes those changes before answering: search in every form, recall, pack, around,
  chats, summary (including pending), resume, status and postcompact use the completed
  source census and foreground refresh. This makes a transcript change visible to the next
  command. There is no behind notice or changed-source count in an answer's freshness
  record or status. `--more` continues its first frozen page. `--no-auto` keeps 0.4.9's
  unchecked record; a read-only data root also says `unchecked` because 0.5 skips
  ingestion when it may not write.

## Command line and help

- Help pages and argument errors use clap's layout and wording (`Usage:`, `-h, --help  Print
  help`, `error: unexpected argument '--x' found`) instead of argparse's: 0.5 parses every
  command with one clap parser. Each page keeps 0.4.9's descriptions, option text, examples
  and exit lines wherever the command behaves the same; the refusals 0.4.9 worded itself
  (option gates, time windows, `--more`, unknown agents) keep their wording, without the
  `; to search for the word "tail": agrep search tail …` hint argparse appended to a
  subcommand's refusal, which only the `unrecognized arguments` refusal of `chats`, `recall`
  and `pack` keeps; and every argument error still exits 2, as 0.4.9's did.
- `status` and `resume` also accept `--no-auto`, which 0.4.9 refused with exit 2: in 0.5
  each of them checks the agent stores for changes itself, as search does, where 0.4.9
  left that to its daemon. `index --semantic` is new: it downloads the model if needed and
  embeds every row before exiting.
- search's `--strict-semantic` is gone (exit 2): it was a compatibility alias that changed
  nothing, since `-s` already exits 2 when meaning is unavailable.
- 0.4.9's browser explorer (`agrep ui` and `agrep serve`) is gone. The terminal commands
  cover the same ground: `board` and `tail` show live sessions, and search, `around` and
  `chats` find and read past chats. `agrep ui` and `agrep serve` now search for those
  words, like any other first word that is not a command.
- A one-shot `board` (`--once`, `--json`, or not on a terminal) waits up to 10 s for its
  first live scan by default, where 0.4.9 waited 0.18 s for a daemon that had usually
  scanned already; 0.5 has no daemon, and a large history takes longer than 0.18 s to scan.
  `--wait` still sets the bound.
- `setup` and `index --semantic` show the model download's bytes and percent on a
  terminal's stderr; elsewhere they stay quiet.
- `agrep help` prints the top-level help and `agrep help COMMAND` that command's help,
  without touching the index, where 0.4.9 searched for the word `help` (which on a fresh
  install first built the whole index). `agrep search help` still searches the word, and
  anything else after `help` is refused (exit 2) with that search command.
- `agrep version` on its own prints the version, as `agrep --version` does, where 0.4.9
  searched for the word (on a fresh install, after building the whole index). With more
  words (`agrep version bump`) it still searches, and `agrep search version` always does.
- On a terminal narrower than 60 columns, help puts each option's description on the lines
  under it and never wraps narrower than 40 columns: clap otherwise kept descriptions
  beside an option column wider than the terminal, so `search --help` at 40 columns printed
  one word a line. The live `board` fits its rows to the width, counted in terminal columns
  (a CJK character or an emoji takes two) and cut between whole characters, so an accent or
  a joined emoji is never split: a row that does not fit shortens the project and state,
  marked `…`, so the prompt that tells sessions apart stays visible; a cut line ends in `…`;
  and the key legend shortens but always lists `q quit`.

## Index and reindex

- `index` prints 0.4.9's report without the lines about files 0.5 does not keep (the
  per-agent parse lines, the `-> messages.jsonl` target, and the `replies.jsonl` and
  event-stream lines with their counts), and its phases are 0.5's: census, parse, write,
  bounds and stats, or only the census for an unchanged pass: 0.5 keeps one SQLite
  database instead of those files.
- `reindex` prints that report under 0.4.9's `=== ingest transcripts ===` stage, without
  its `(rust)` label or its `no new messages since last index` shortcut: 0.5's reindex
  always reparses every source, so it always has an index to report. It builds the new
  index beside the old one, the way a first build does, and then copies it over the old
  one in one transaction: searches keep answering from the old index meanwhile, a reindex
  that is stopped partway leaves it as it was, and the result is the size of a first
  build. So its stderr notice comes before the work, `rebuilding the search db (a rebuild
  can take a while on a large history) …`, where 0.4.9 printed `refreshing the search db …`
  after the ingest. Meaning vectors survive it: a row whose text was embedded before the
  reindex is still covered after it, with or without a model or `embeddings` on. A source
  it cannot read keeps its last good rows and stays listed as failed, as after `index`,
  with or without `--full`; 0.4.9's `reindex --full` published its rebuild without them.
- A relative `AGREP_HOME`, `AGREP_DATA_DIR` or `AGREP_MODEL_DIR` is made absolute when
  agrep starts, its links resolved, so a command run from another directory names every
  source as the index does instead of indexing them all again; 0.4.9 resolved only the
  data directory this way.
- `index --semantic` and `reindex` show the embedding stage on a terminal's stderr as one
  rewritten line, `embedding messages: 1200 of 137768 rows · 150 rows/s · about 15m 11s
  left`, and on a stderr that is not a terminal write that line every 30 seconds; 0.4.9's
  embed stage printed nothing until it finished. `status` says the time
  left the way `-s` does (`about 15m 19s left`), where 0.4.9 had no estimate; both divide
  the rows still to cover by the rate rows were covered at over the last minute of the
  build, rows whose text was already embedded included.
- An antigravity chat's project comes from its working directory after decoding a
  double-encoded `Cwd` (a JSON string inside the JSON value), as 0.4.9's resume decoded it,
  where 0.4.9's index read the still-encoded text: one decoding serves both, so an escaped
  character such as `\u00e9` names the project as written.
- 0.4.9 kept one trigram index of all text, tool output included, so every term of three
  or more characters, symbols too, narrowed a search. 0.5 keeps trigrams for prose only
  and indexes tool output by word, which keeps the index smaller and its build faster.
  Beside the words it keeps a small index of the symbols in tool output: each character
  that is no ASCII letter, digit or space (punctuation, emoji, other scripts), alone and
  with its neighbour. A tool term that is no plain word (`C++`, `O(n log n)`, `::`, `🚀`)
  reads only the rows holding all of its symbols, and finds the rows a full scan finds.
  A term of one or two letters or digits (`a`, `go`) still reads every row, as in 0.4.9.
  The symbol index makes this data format 9: an index an earlier 0.5 build made (format
  8) is refused with one line naming `agrep reindex`, which rebuilds it in place and keeps
  settings, archives and meaning vectors.
- Codex rollouts that Codex has compressed (`rollout-*.jsonl.zst`) are indexed like plain
  ones, where 0.4.9 read only `rollout-*.jsonl` and so missed every compressed chat; on a
  long-used Codex install that is most of its history. While Codex compresses a chat both
  copies can exist for a moment: search shows the chat once, and its handles stay the same
  when the plain copy is deleted. A damaged compressed rollout is listed by `status` and
  `doctor` as a failed source (`cannot decompress: ...`); other sources are unaffected.
  `archive` snapshots compressed rollouts as it does plain ones.
- When several files hold one chat (a resumed or copied transcript), 0.4.9 searched one copy
  of each turn and of each tool call and hid the rest, even where the hidden copy held more:
  a tool call's full output beside a copy that elided it, a call or reply only that copy
  has, a call whose input differs. 0.5 searches every row any copy holds, each once. Of
  the copies of a call it keeps the most complete (a real result over one omp elided, then
  the longest output); of a turn, the longest reply as 0.4.9 did, ties going to the first
  file in path order rather than to the first one indexed. A copy is hidden only when a kept
  copy contains its text, or when it holds an omp elision placeholder in place of the
  result of a call a kept copy has with the same input. A reply that differs from the kept
  one takes its own turn, so the chat's turns are renumbered by time, as 0.4.9 did for
  copies that disagreed about a prompt.
- 0.4.9 hid the tool calls of a chat that had no prose turn, such as an omp advisor run
  that only used tools, and did not list the chat. 0.5 searches those calls, lists the
  chat (a pi side transcript as a side chat of its parent, under its file's working
  directory), dates it by its calls and titles it by its first call, and `around --full` shows them on turn 0.
- Codex chats archived in Codex (moved to `~/.codex/archived_sessions/`, plain or
  compressed) are indexed, where 0.4.9 read only `~/.codex/sessions/` and so dropped a chat
  from search once it was archived. A chat keeps its handles when it moves, and is shown
  once even while a copy sits in both folders.
- A store root that is itself a symbolic link is followed once, to its own kind: a store
  directory (`~/.codex/sessions`, `~/.claude/projects`, `~/.pi/agent/sessions` or
  `~/.omp/agent/sessions`, opencode's or crush's data folder) to a directory, and a store
  that is one file (the database `OPENCODE_DB` names, kimi's `kimi.json`, crush's
  `crush.db`, Cursor's `state.vscdb`) to a regular file. Index, search, status, doctor,
  audit, archive and board read it, and its sources keep their paths through the link.
  0.4.9 recorded such a root as an `unsupported-link` source-health issue and read nothing
  from it. Links inside a store are still never followed. A root link that dangles, leads
  to the wrong kind (a folder link to a file, a database link to a folder), or leads into or
  around another store root (so that one store would be read twice) is listed by `status`
  and `doctor` as a failed source naming the reason.
- A Codex tool result written as a list of content items (`[{"type":"input_text","text":
  "Script completed…"}, …]`, as Codex's script tools write it) is indexed as the text of
  those items, one per line, the way 0.4.9 joined a Codex message's text, where 0.4.9 indexed
  the list's raw JSON. Search, snippets and the 800-character cap now see the tool's own words
  instead of the JSON wrapper and its escapes, image items add nothing, and a `Process exited
  with code N` line in the text sets the call's outcome, as it does for a plain-text result.

## Integrations and archive port

- Setup replaces every agrep-marked block, including an edited one: the marked block is
  agrep's to own. Unmarked skills and edited helper files stay protected.
- Hook registration adds agrep's entry beside a user's own hooks for the same event, where
  0.4.9 registered nothing when a hook it had not installed was there: recovery works
  without displacing the user's hook. Setup writes only agrep's entry, plus the `hooks`
  object or event list when the file lacks one, in the layout of the entries beside it
  and the file's indentation, `\u` escaping and line endings; remove takes out exactly
  that entry and any object or list setup added, so setup then remove gives back the file
  byte for byte. 0.4.9 re-serialized the whole file in the style it detected, the user's
  own entries and settings included.
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
- A failed scheduled capture is tried again after ten minutes, then after twice as long with
  each further failure, up to a day, where 0.4.9's daemon started a new pass every minute
  however often it had failed: a lasting fault, such as an unreadable catalog, would
  otherwise start a failing worker and add a log line on every command. An explicit
  `agrep archive` always runs, and `status` and `doctor` report the failing passes.
- Every capture pass checks the stored chunks the catalog relies on, where 0.4.9 trusted
  them until a restore read them: a chunk whose file is missing, or changed since it was
  verified and no longer holds its content, is re-stored from a live file that still holds
  it, and versions no live file can repair are listed by that capture, `archive --status`
  and `doctor`, and counted by `status`, which reads only the damage the last capture or
  deep check recorded. A chunk file is fully re-read only when its file changed, so a pass
  costs one `stat` per chunk. A restore that meets such a chunk says the archived copy is
  damaged, naming the chunk, the archive directory and the remedy, where 0.4.9 gave the
  file system's error for a missing chunk and `chunk … corrupt in archive store` for a
  corrupt one.
- `doctor --deep` decompresses and re-hashes every chunk the catalog references, where
  0.4.9's deep tier validated the archive manifest only: a chunk overwritten in place, which
  keeps its file's size and date and so passes a capture's `stat` check, is found too. It
  reports the damaged versions in two groups: those whose content a live file still holds,
  which `agrep archive` re-stores, and those no live file holds, which are lost, listed by
  path. It records what it found in the catalog, so `status`, `archive --status` and
  `doctor` report it from then on and the next capture re-reads those chunks; it writes
  nothing when it finds no damage. Plain `doctor` and `archive --status` read no chunk.
- An unreadable catalog is named, with `agrep archive --purge --yes` as its remedy, by
  `status` and every archive command; the purge replaces it with an empty catalog and
  deletes every chunk, and `--status` exits 2, as 0.4.9's did for an unreadable manifest.
- A forced restore into an existing SQLite file writes the archived contents into it through
  the backup API under SQLite's write lock, where 0.4.9 replaced the file and deleted its
  `-wal`, `-shm` and `-journal` files: open connections keep a coherent database instead of
  one swapped beneath them. A restore refuses a destination holding such a file without its
  database, which 0.4.9 deleted: it may hold the only copy of committed writes.
- `restore --to` resolves the `.` and `..` of its destination by name against the working
  directory, as a shell's `cd` does, where 0.4.9 left `..` to the file system: `--to
  link/../x` restores into `./x` even when `link` links elsewhere.
- A failing or timed-out `post_index` command prints one line on stderr naming its exit
  status and the file holding its output (`log/post_index.log` in the data directory, kept
  from the last run), where 0.4.9 wrote only to its own log. As in 0.4.9 the command never
  fails the index and is stopped after ten minutes; 0.5 stops everything it started too.
  `off` in any letter case, or a blank value, disables it, where 0.4.9 ran a stored `OFF`.
  As in 0.4.9, INT, TERM or HUP to agrep while the command runs is passed to the command
  and everything it started, whatever is left is killed after a second and a half, and
  agrep then ends by that signal.
- `agrep remove` leaves alone a known instruction file it has no record of writing when the
  file is not UTF-8 or its agrep markers are broken, prints one line saying so and still
  succeeds: the file is the user's own. Every setup and remove error names its file.
- `agrep set` notes say what the new value changes (`tools = off  (searches leave out tool
  calls and their output unless you pass --who tool)`), where 0.4.9 printed one fixed note
  per key; 0.4.9's `embeddings` note claimed `off` blocks model downloads, which only setup
  makes.

## Distribution, CI, release and documentation

- No sdist, no runtime Python, and npm downloads the standalone binary: 0.5 ships one
  binary with no runtime Python.
- `--version` carries no distribution, runtime or build identity, and there is no legacy
  install recovery: 0.5 has no such distribution or install machinery to report.
- Binaries build for the portable target baseline instead of `target-cpu=native`: one
  binary must run on every machine of its target.

## Browse and diagnostics port

Browse and diagnostics differences are listed in [BROWSE.md](BROWSE.md).
