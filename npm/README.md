# agrep

grep your AI coding agents' chat history — Claude Code, Codex, opencode, Antigravity —
straight from the shell. One searchable cross-agent history, a context-window command
(`agrep around`) built for agents and humans, native session resume, and a local web
explorer (`agrep ui`).

This npm package is a thin shim: agrep is a python package with a bundled rust binary,
and the shim runs it through [uv](https://docs.astral.sh/uv/) (or pipx). uv manages
python itself, so this works even on a machine with no python installed.

```
npm i -g agrep
agrep "race condition"     # first run indexes your agent stores, then greps
```

Prefer the direct route? `uv tool install agrep` — same thing, no node in the middle.

Full docs: https://github.com/dannyisbad/agrep
