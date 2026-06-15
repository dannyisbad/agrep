# agrep

Every AI coding agent you run writes its full history to disk - Claude Code, Codex,
opencode, Antigravity, Kimi CLI, Cline. agrep reads those stores directly and makes
your entire cross-agent history greppable from the shell, with a local web explorer
(**tilt**) on top for browsing, organizing, and live-watching everything.

No hooks, no agent-side install, no telemetry. agrep never modifies the agents or
their data - it only reads. Everything stays on your machine.

```
uvx agrep "race condition"      # or: pipx run agrep "race condition"
```

That fetches a small prebuilt package (the rust ingest binary + the app - no clone,
no cargo), indexes your agent stores on first run (one-time, ~30s for years of
history), and greps. After that, searches are instant - a derived full-text index
answers cold CLI calls in well under a second.

For a permanent `agrep` command, install it as a uv tool:

```
uv tool install agrep
uv tool update-shell   # only needed if uv's tool bin is not already on PATH
```

The PyPI package already exposes the `agrep` console script; uv/pipx/pip decide
where that script lands. npm users can install the shim globally instead:

```
npm i -g @mundy/agrep   # or: npm i -g agrep-cli
```

npm puts its `agrep` shim on PATH and, when uv is available, preinstalls the
matching PyPI tool during global install so first run is already warm.

```
agrep deadlock --agent codex    # filter to one agent
agrep "cache bug" --model gpt-5  # exact model filter
agrep "cache bug" --model spark --soft  # substring model filter (*spark*)
agrep -E 'TODO|FIXME'           # regex
agrep -l auth                   # list matching chats, not every line (like grep -l)
agrep "memory leak" --json      # one JSON object per hit, for piping
agrep "flaky test" --semantic   # meaning search (needs a running server)
agrep warm                      # keep semantic search hot
agrep around 00da9752 144       # the conversation around a hit, tool calls inline
agrep resume 00da9752           # reopen that session in its own agent, cd'd there
agrep ui                        # tilt: the web explorer
```

Bare `agrep` prints where your index stands and the commands that matter. Keyword
search exits like grep (1 when nothing matched), so it composes in scripts and pipes.
When a server is running and your terminal supports it, each result header is a
clickable link that opens the chat in the app (`AGREP_NO_HYPERLINKS=1` to disable).

## From a hit to the story: `around`

Search tells you *which* session touched a thing. `around` tells you *what happened* -
the local story of a hit (the error, the attempts, the fix) for a few KB instead of a
whole transcript:

```
agrep around 00da9752 144           # ±4 turns around turn 144 of that session
agrep around 00da9752:144 -n 10     # wider window; colon form pastes from --json
agrep around 00da9752 144 --full    # nothing truncated
agrep around 00da9752 144 --json    # one object per message/event
```

Tool calls render inline (name, input, ok/failed) but never their output - the token
bomb - unless you opt in with `--tool-output N`. Long messages are capped, and every
truncation marker carries the exact command that prints the rest, so the follow-up
never needs guessing.

## Your agents can use it too

An agent that solved a gnarly bug last week re-derives it from zero today - session
context dies, transcripts don't. agrep makes that history queryable at the moment it's
needed: `--json` everywhere, grep exit codes, stateless addressing (`session:turn`),
and `around` defaults tuned for token budgets.

One line in your agent's instructions does it:

> Before deep-diving an unfamiliar error, `agrep "<the error string>" --json`; pull
> context on a hot hit with `agrep around <session> <turn>`.

The first search builds the index by itself, so this works on a box where nothing was
ever set up (`--no-auto` opts out for strict scripts).

## tilt: the explorer

`agrep ui` opens the human surface - a single local web app over the same index. It
builds the base index only if one is missing; after that the page opens immediately
and the server refreshes the index in the background. Use `agrep ui --force-index`
when you explicitly want to rebuild before opening.

- **One searchable history** across every supported agent. Keyword search is exact and
  instant; semantic search and topic clustering light up with the optional model tier.
- **A live board** of every running session - across all agents at once - with real
  state (thinking, which tool is running, queued prompts, errors, durations) read
  straight from the stores. No hooks: it sees sessions you started in any terminal,
  and the subagents they spawn. Images an agent reads or sends render inline.
  (Live tailing and native resume cover claude/codex/opencode/antigravity today;
  kimi and cline are search/browse-only so far.)
- **Per-chat detail**: the full transcript with the tool/subagent event tree, and a
  one-click "resume this session in its own CLI".
- **Native resume**: jump back into any past session in its own agent, cd'd to the
  directory it ran in.

While the server runs it re-indexes automatically after new agent activity settles;
you never run a command to see new chats. `agrep warm` is the same server path with
semantic models preloaded, so the first meaning search does not pay the model-load
tax. The auto-indexer also refreshes the derived full-text database, so the next
terminal search does not pay the sqlite rebuild after new activity.

## The three tiers

The core works on a bare install and gets better as you add pieces. Nothing past the
first tier is required.

| Tier | Needs | Unlocks |
|---|---|---|
| **Core** | Python 3.10+ (Rust only if building from source) | Grep, `around`, resume, browse, live view, event trees. Titles come from each chat's first message. |
| **Smart** | one click in the app's setup panel (torch et al.) | Semantic search, topic/concept clustering, mood arcs. |
| **Named** | [Ollama](https://ollama.com) + a small local model | Clean generated titles, summaries, concept names, and arc verdicts instead of first-message fallbacks. |

The offline model passes for titles, summaries, concepts, and arcs run at index time:
a model loads, does its pass, and releases its memory. Semantic search is different:
the server loads the embedder/reranker on the first semantic query, or immediately
with `agrep warm`, then releases them after an idle period. `agrep doctor` reports
which tiers are live and the exact command to unlock each missing one (`--fix` does
the safe setup itself).

## Where it reads

agrep discovers sessions under your home directory. Read-only, always:

| Agent | Store |
|---|---|
| Claude Code | `~/.claude/projects/<slug>/*.jsonl` |
| Codex | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` |
| opencode | `~/.local/share/opencode/*.db` (SQLite) |
| Antigravity | `~/.gemini/antigravity-cli/brain/<uuid>/` |
| Kimi CLI | `~/.kimi/sessions/<workdir-hash>/<uuid>/` |
| Cline | each editor's `globalStorage/saoudrizwan.claude-dev/tasks/`, plus `~/.cline/data/` |

Whichever of these exist get indexed; missing ones are skipped. Works on Windows,
macOS, and Linux.

## Normalized schema

`agrep index` flattens every adapter into one user-side row shape in
`messages.jsonl`:

```
{id, agent, project, session, ts, turn, who, text, model?, model_source}
```

`turn` is the adapter-normalized 0-based user-turn index inside that session. Agent
replies are kept in `replies.jsonl` and joined by the same `id`, so terminal search
and `--json` can emit both sides with `who`.

`who` is one of `user`, `agent`, `control`, `synthetic`, `recap`, or `harness`.
Use `--who user` for model-vs-model comparisons; control/synthetic/recap/harness
rows stay searchable but are excluded from doctor's model-attribution denominator.

`model_source` explains attribution: `explicit` came from the source store,
`session` was backfilled because that session had exactly one explicit model,
`temporal_session` came from an unambiguous same-session model run, `unknown`
means the adapter/store did not expose one, and `ambiguous_session` means the
session had multiple explicit models so no backfill was safe.

Synthetic, control, recap, and model-less harness rows keep visible placeholder
model buckets like `model="<synthetic>"`, but `who` is the authoritative tag.
Use `--who user` for real model-vs-model comparisons.

## Commands

```
agrep <pattern>          # grep your history (first run builds the index)
agrep around <id> <turn> # the conversation around a hit
agrep resume <id>        # reopen a past session in its own agent, cd'd there
agrep ui                 # tilt: serve/open; builds the base index if missing
agrep ui --force-index   # rebuild the base index before opening
agrep doctor             # what's installed + what each tier needs (--fix does setup)
agrep index              # rebuild the base index only (fast, no models)
agrep reindex            # full pipeline: embeddings + affect + topics + arcs
agrep serve --port N     # just the server (auto-indexes in the background)
agrep warm               # server + preloaded semantic models
```

To hack on it, clone and use the same commands as `python cli.py <cmd>`
(needs Rust for the ingest binary - https://rustup.rs):

```
git clone https://github.com/dannyisbad/agrep && cd agrep
python cli.py ui
```

A dev checkout also has thin wrappers: `agrep.cmd` (Windows) forwards the full CLI,
and `./tilt` / `tilt.cmd` is shorthand for `agrep ui` - type `tilt`, get the explorer.

Installed and dev runs use the same per-user index by default, so `agrep`,
`uvx agrep`, and `python cli.py` see one corpus. Use `AGREP_DATA_DIR=./data` only
when you intentionally want a repo-local test index; `AGREP_VENV_DIR=py/.venv`
does the same for an isolated smart-tier venv.

## Privacy

Everything is local. agrep makes no network calls except to a local Ollama if you opt
into that tier; the one exception is the web explorer, which loads its fonts from
Google Fonts (none of your data goes with it - and the app works fine offline on
system-font fallbacks). Your index lives in a per-user data dir unless you explicitly
set `AGREP_DATA_DIR`; repo-local `data/` is for opt-in test indexes. The server binds
to `127.0.0.1` only.

## How it's built

A Rust ingest (`crates/agrep-core` + `crates/agrep-cli`) reads the stores and writes a
compact index; a sqlite FTS5 index derived from it makes cold CLI searches instant; a
read-only Python server (`py/`) serves the single-file web app (`web/app.html`). The
Rust ingest is the only required dependency for the core experience; the Python ML
scripts are the optional enhancement layer. See [CONTRIBUTING.md](CONTRIBUTING.md) for
the layout and how to add an adapter for another agent.

## License

MIT - see [LICENSE](LICENSE).
