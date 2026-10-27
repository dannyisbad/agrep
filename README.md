# agrep

**Search everything your AI agents have done.**

`agrep` is a local search engine for the conversations, tool calls, decisions,
and source context your agents leave on disk. It gives you one grep-like index
across Claude Code, Codex, opencode, Gemini, Cursor, pi, and more—then lets
you open the exact context or resume the original chat.

```console
$ agrep "retry backoff"
claude  payments  ... capping the backoff at 30s and adding jitter ...

$ agrep around @3f2a91c4:88.d1e0
$ agrep resume @3f2a91c4:88.d1e0
$ agrep board
```

## Install

```console
uv tool install agrep
agrep setup
agrep "race condition"
```

Other options:

```console
uvx agrep "race condition"       # try it without installing
pipx install agrep
npm install -g @mundy/agrep      # npm shim; requires uv or pipx
```

`setup` shows a consent screen before it writes agent instruction blocks,
compaction hooks, the cleanup sentinel, or optional model/archive data. The
only `--no-teach --no-hook` together leave the box without those integrations;
`agrep remove` undoes them without deleting your histories or search index.

Nothing deletes the index for you: it is the searchable copy of your agent
conversations. `agrep doctor` prints its data path; delete that directory
yourself when you want the copy gone.

## The commands you actually need

```console
agrep <pattern>                  # keyword search; first run builds the index
agrep "cache bug" --agent codex  # filter by agent
agrep -E "TODO|FIXME"            # regular expression search
agrep -l auth                    # matching chats, not every line
agrep "exit 137" --who tool     # include tool output explicitly
agrep "flaky test" --semantic    # optional meaning search
agrep "rust simd" --sort time    # newest matches first
agrep akd --count-by-tier        # exhaustive diagnostics

agrep around <id> <turn>         # context around a hit
agrep around @handle             # context from a printed handle
agrep resume <id>                # reopen a session in its own agent
agrep chats [topic]              # search indexed sessions and identities
agrep board                      # bounded live activity across agents
agrep status --json              # machine-readable health summary
agrep doctor                     # bounded health check
agrep audit                      # ingest coverage and accounting checks
```

Agent-facing output is compact, bounded, and includes digest-checked
`@session:turn.digest` handles. Human terminals get the classic renderer.
`agrep around @handle` opens the cited evidence; `agrep --more m.XXXXXXXX` gets
the next page.

## What it indexes

Sources are opened read-only. Missing stores are skipped, and the index records
what it could actually observe rather than pretending every agent has the same
format.

| Agent | Store |
|---|---|
| Claude Code | `~/.claude/projects/<slug>/*.jsonl` |
| Codex | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` |
| opencode | `~/.local/share/opencode/*.db` (SQLite) |
| Antigravity | `~/.gemini/antigravity-cli/brain/<uuid>/` |
| Kimi CLI | `~/.kimi/sessions/<workdir-hash>/<uuid>/` |
| Cline | editor `globalStorage/saoudrizwan.claude-dev/tasks/`, plus `~/.cline/data/` |
| Gemini CLI | `~/.gemini/tmp/<hash>/chats/session-*.jsonl` plus legacy session files |
| crush | `~/.local/share/crush/crush.db` plus registered per-project stores |
| Cursor | editor `globalStorage/state.vscdb` (SQLite) |
| pi / oh-my-pi | `~/.pi/agent/sessions/<cwd-slug>/*.jsonl` and `~/.omp/agent/sessions/<cwd-slug>/*.jsonl` |

All ten adapters are searchable through keyword search and recall. Live tailing
and native resume support the agents that expose those capabilities. Copilot
CLI and qwen-code are detected by `doctor` but are not parsed yet.

## Local by design

- Transcript content is never uploaded.
- Source stores are read-only to the indexer.
- Derived data lives in the platform data directory (`agrep doctor` prints it):
  `~/Library/Application Support/agrep` on macOS, `~/.local/share/agrep` on
  Linux, or `%LOCALAPPDATA%\agrep` on Windows.
- A current, exhaustive keyword miss exits `1`; an unverified absence exits `2`.

Semantic search is optional. Core keyword search, `around`, recall, resume,
archive/restore, live views, and event trees do not require the semantic model.
Use `agrep doctor --deep` to see exactly which optional capabilities are
available on the current machine.

## Speed

The release gate measures full process runs, not an internal scanner timer.
The committed keyword budgets are:

| Path | Budget |
|---|---:|
| Selective keyword query | 120 ms |
| 100,000-hit sweep | 900 ms |
| Two-character query | 350 ms |
| Cold ingest: 4,600-file store | 3.5 s |
| Cold streamed completion | 3.75 s |
| First streamed hit | 150 ms |

Run the proofs locally:

```console
python bench/perf.py --check
python bench/semantic_cli_scale.py --check
```

The benchmark suite also covers semantic scale, embedding throughput, memory,
portable builds, and release identity. See [`bench/`][bench] for the contracts
rather than trusting one machine's best-case number.

## How it works

A Rust ingest reads each supported store and writes one normalized row shape.
The Python layer owns command orchestration and terminal output. A derived
SQLite FTS5 index makes warm keyword searches fast; an optional local ONNX
worker powers meaning search. No hosted service is required.

The index is refreshed on demand. Later searches can reuse a lightweight
freshness daemon while the previous complete publication remains readable.
The semantic lane has its own generation and coverage disclosures, and every
meaning hit is re-proved against the current transcript text, so changed or
mismatched vectors are not served as current evidence.

## Development

```console
git clone https://github.com/dannyisbad/agrep.git
cd agrep
python cli.py setup
python cli.py "search term"
```

Source builds need Python 3.10+ and Rust. The core implementation is in
`crates/agrep-core`, `crates/agrep-cli`, and `py/`; contributor conventions are
in [`CONTRIBUTING.md`][contributing].

## License

MIT. See [`LICENSE`][license].

[bench]: https://github.com/dannyisbad/agrep/tree/master/bench
[contributing]: https://github.com/dannyisbad/agrep/blob/master/CONTRIBUTING.md
[license]: https://github.com/dannyisbad/agrep/blob/master/LICENSE
