# Changelog

## 0.5.0 — 2026-11-07

- One Rust executable handles search, context recovery, live views, integrations
  and local storage. Python wheels and npm are binary delivery mechanisms.
- 0.4.9's browser explorer (`agrep ui` and `agrep serve`) is removed; the terminal
  `board`, `tail`, search, `around` and `chats` commands cover the same ground.
- A new `agrep/0.5` data directory and transactional SQLite index replace older
  agrep formats. There is no migration: build a fresh index and run `agrep setup`
  again. Before upgrading, run `agrep remove` with 0.4 to remove its blocks,
  hooks and uninstall sentinel. Save old archives before deleting old data.
- Every command checks the agent transcripts and indexes what changed before it
  answers, so an answer includes everything written before the command started,
  with no "behind" notice. The first index is built inline, parsing sources
  while it writes earlier ones, so it holds a bounded share of the history in
  memory whatever the history's size.
- Codex chats that Codex has compressed (`rollout-*.jsonl.zst`) or archived
  (`~/.codex/archived_sessions/`) are indexed and searchable. 0.4.9 skipped
  them, which on a long-used Codex install is most of its history.
- Meaning search runs in process with Candle and a pinned model; a missing model
  leaves automatic hybrid search on keywords and is shown by status. Explicit
  `-s` requires the model. A meaning search embeds rows that arrived since the
  last background pass before it answers. While the meaning index is first being
  built, automatic hybrid search answers on keywords and `-s` reports the build's
  progress. The verified model occupies about 194 MB installed.
- A page that mixes meaning and keyword results no longer hides keyword hits from
  a conversation family a meaning result already represents: they follow the other
  rows and stay reachable through `--more` and `-n`. Notes from omp's automated
  advisor sidecars are left out of meaning search (keyword search still finds
  them), so they no longer crowd out real answers.
- `agrep help` and `agrep help COMMAND` print help instead of searching for the
  word "help"; `agrep search help` still searches it. `agrep version` prints the
  version.
- A store folder or database that is a symbolic link (say `~/.codex/sessions`,
  `~/.claude/projects` or opencode's database moved into a dotfiles folder and
  linked back) is indexed; 0.4.9 skipped it. A link agrep cannot follow is listed
  by `status` and `doctor`.
- Help and the live `board` stay readable in narrow terminals: help puts option
  descriptions on their own lines, and board rows keep each session's prompt and
  the `q quit` key, with Chinese, Japanese or emoji titles too.
- A failing or timed-out `post_index` command prints one line naming the file that
  holds its output; the index still succeeds. `agrep set post_index OFF` turns it off
  like `off`. `agrep set` notes say what each new value changes.
- Archive captures check the stored copies they rely on and re-store a damaged
  one from the live store that still holds it. Restore, `archive --status`,
  `status` and `doctor` name archive damage and the command that resolves it; a
  damaged catalog can always be purged, and a failing background capture waits
  longer after each failure instead of retrying on every command.
- Ctrl-Z on a piped `agrep run` or `resume` (`agrep run claude | cat`) stops the
  agent too, so the shell gets your keys back; `fg` resumes it. Like any program,
  an agent that reads the terminal stops when its job is in the background. The
  `resume` picker asks again after an answer it can't use, and `resume` says
  `↻ resuming omp` when it starts omp.
- `agrep setup` and `agrep remove` change only agrep's own hook entry in Claude's
  `settings.json` and Codex's `hooks.json`: other hooks and settings keep their exact
  text, and remove gives back the file setup found.
- `reindex` builds the new index beside the old one, as a first build does, and
  copies it in at the end: it takes about as long as a first build and leaves an
  index of the same size, searches answer from the old index meanwhile, and a
  reindex stopped partway leaves the old index as it was. Meaning vectors survive
  it. `index --semantic` and `reindex` show embedding progress on a terminal, and
  `status` gives the meaning build's time left as `-s` does (`about 15m 19s left`).
- Six platform wheels bundle the executable. npm verifies package-pinned hashes
  before caching and executing the matching GitHub release binary.
- CI runs tests and a blocking full-process performance comparison with agrep
  0.4.9 on Linux, macOS and Windows, including arm64 runners: every measured case
  must beat 0.4.9 at median and p95. Release publication requires it.
