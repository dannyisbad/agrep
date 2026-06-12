# Contributing

## Layout

```
tilt.py                the CLI front door (search / resume / up / doctor / index / reindex / serve / tail)
agrep/                 the PyPI package shim: maps this flat tree into a wheel (see pyproject)
crates/tilt-core/      Rust: read each agent's store, normalize, write the index
  src/ingest/          one adapter per agent (claude, codex, opencode, antigravity)
crates/tilt-cli/       the `tilt-rs` ingest binary (driven by `tilt.py index`)
py/                    read-only server + the optional ML/LLM pipeline
  server.py            the HTTP server (stdlib only; serves web/app.html)
  explore.py           read layer over the index (browse/search/detail)
  live.py              passive store-tailing watcher (the live view)
  search.py            the terminal grep (`agrep <pattern>`)
  resume.py            `agrep resume` — reopen a session in its own agent
  native.py            per-agent launch/resume commands + cwd resolution
  indexer.py           background auto-reindex while the server runs
  embed/emotion/...    the optional smart-tier stages
web/app.html           the entire frontend, one file
data/                  the built index (gitignored — never committed)
```

Data flows one way: Rust ingest → `data/` → Python server → web app. The Rust core is the
only thing the base experience needs; the Python ML scripts are optional enhancement.

## Running it

```
python tilt.py up            # build + index + serve + open
python tilt.py reindex        # also run the smart/named tiers (needs the deps)
python tilt.py doctor         # what's installed; --fix creates the venv + installs deps
```

`tilt.py up` and `serve` need only Python stdlib + the Rust binary. Editing `web/app.html`
needs no rebuild — the server re-reads it on each request.

## Adding an agent adapter

Each adapter in `crates/tilt-core/src/ingest/` turns one agent's on-disk store into two
streams: `Message`s (the human turns + the agent's reply text) and `Event`s (tool calls
and subagent activity). To add one:

1. Find where the agent journals sessions under `~` and what one record looks like. The
   existing four adapters are worked examples for JSONL (claude/codex/antigravity) and
   SQLite (opencode).
2. Implement `collect() -> (Vec<Message>, Vec<Event>)` following the shape in
   `claude.rs`. Emit only real human turns as messages (filter the harness wrappers via
   `is_wrapper`); attach tool/subagent activity as events.
3. Register it in `ingest/mod.rs` and the CLI's agent list.
4. For the **live view**, mirror the tailing logic in `py/live.py` (JSONL stores are
   tailed by byte offset; databases are polled). This is separate from the Rust ingest —
   it reads the same stores in real time.
5. For **native resume**, add the agent's resume command to `py/native.py`.

Keep adapters read-only. tilt must never write to, move, or delete an agent's data.

## Conventions

- No hooks, ever. tilt's whole premise is passive reading — it must work for sessions it
  didn't start and agents it didn't install into.
- Anything that needs a GPU or a model is optional and runs only at index time. The server
  must stay stdlib-only and modelless.
- Don't hardcode usernames or absolute paths; derive from `home()` / `Path.home()`.
- Verify changes against real store data, not just synthetic fixtures.
