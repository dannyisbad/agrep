# tilt

Every AI coding agent you run writes its full history to disk — Claude Code, Codex,
opencode, Antigravity. tilt reads those stores directly and gives you one place to
browse, search, and watch them: a single local web app over your entire cross-agent
history, plus a live board of what's running right now.

No hooks, no agent-side install, no telemetry. tilt never modifies the agents or their
data — it only reads. Everything stays on your machine.

```
uvx agrep          # or: pipx run agrep
```

That fetches a small prebuilt package (the rust ingest binary + the app — no clone, no
cargo), scans your agent stores, starts a local server, and opens the app. New chats show
up the moment an agent writes one; the index keeps itself current while the server runs.
Your index and any optional models install to a per-user data dir, not the package.

The heavier tiers (semantic search, generated titles) install on demand from the app's
own setup panel — one click each. To get them up front, or to hack on tilt, clone instead:

```
git clone https://github.com/dannyisbad/tilt && cd tilt
python tilt.py up      # builds the indexer; needs Rust (https://rustup.rs)
```

Not sure what's installed? `agrep doctor` (or `python tilt.py doctor`) reports which
tiers are live and the exact command to unlock each missing one.

---

## What you get

- **One searchable history** across all four agents. Keyword search is exact and instant.
  Semantic search and topic clustering light up if you install the optional model tier.
- **A live view** of every running session — across all agents at once — with real state
  (thinking, which tool is running, queued prompts, errors, durations) read straight from
  the stores. No hooks: this works for sessions you started in any terminal, and for
  subagents the agents spawn. Images an agent reads or sends render inline as they happen.
- **Per-chat detail**: the full transcript with the tool/subagent event tree, and a
  one-click "resume this session in its own CLI" button.
- **Native resume**: jump back into any past session in its own agent, cd'd to the
  directory it ran in.

## The three tiers

tilt is built so the core works on a bare clone and gets better as you add pieces. Nothing
below the first tier is required.

| Tier | Needs | Unlocks |
|---|---|---|
| **Core** | Python 3.10+, Rust | Browse, exact keyword search, live view, event trees, native resume. Titles come from each chat's first message. |
| **Smart** | `pip install -r requirements.txt` (torch et al.) | Semantic search, topic/concept clustering, mood arcs. |
| **Named** | [Ollama](https://ollama.com) + a small local model | Clean generated titles, summaries, concept names, and arc verdicts instead of first-message fallbacks. |

The model tiers run **only at index time** — a model loads, does its pass, and releases
its memory. tilt never holds a model resident, and the server itself needs no GPU. If you
can't run a model at all, the core tier is fully usable on its own.

## Where it reads

tilt discovers sessions under your home directory. Read-only, always:

| Agent | Store |
|---|---|
| Claude Code | `~/.claude/projects/<slug>/*.jsonl` |
| Codex | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` |
| opencode | `~/.local/share/opencode/*.db` (SQLite) |
| Antigravity | `~/.gemini/antigravity-cli/brain/<uuid>/` |

Whichever of these exist get indexed; missing ones are skipped. Works on Windows, macOS,
and Linux.

## Commands

```
python tilt.py up            # index what's new, serve, open the browser (the default)
python tilt.py up --no-open   # serve without launching a browser
python tilt.py doctor         # what's installed + what each tier needs (--fix does setup)
python tilt.py index          # rebuild the base index only (fast, no models)
python tilt.py reindex        # full pipeline: embeddings + affect + topics + arcs
python tilt.py reindex --full # recompute every stage from scratch
python tilt.py serve --port N # just the server (auto-indexes in the background)
```

On Unix you can run `./tilt up` (a thin wrapper); on Windows, `tilt up` via `tilt.cmd`.

While the server runs it re-indexes automatically after new agent activity settles. The
status chip in the app shows when it's indexing and surfaces any errors; click it to force
a refresh. You never need to run a command to see new chats.

## Privacy

Everything is local. tilt has no network calls except to a local Ollama if you opt into
that tier. Your indexed history lives in `data/`, which is **gitignored** — it is never
committed. The server binds to `127.0.0.1` only.

## How it's built

Rust ingest (`crates/`) reads the stores and writes a compact index to `data/`; a
read-only Python server (`py/`) serves a single-file web app (`web/app.html`). The Rust
ingest is the only required dependency for the core experience; the Python ML scripts are
the optional enhancement layer. See [CONTRIBUTING.md](CONTRIBUTING.md) for the layout and
how to add an adapter for another agent.

## License

MIT — see [LICENSE](LICENSE).
