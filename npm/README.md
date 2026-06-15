# agrep

grep your AI coding agents' chat history - Claude Code, Codex, opencode, Antigravity,
Kimi CLI, Cline - straight from the shell. One searchable cross-agent history, a context-window command
(`agrep around`) built for agents and humans, native session resume, and a local web
explorer (`agrep ui`).

This npm package is a thin shim: agrep is a python package with a bundled rust binary,
and the shim runs it through [uv](https://docs.astral.sh/uv/) (or pipx). uv manages
python itself, so this works even on a machine with no python installed.

```
npm i -g @mundy/agrep
# or: npm i -g agrep-cli
agrep "race condition"     # first run indexes your agent stores, then greps
```

Global npm installs also try to preinstall the matching PyPI tool with
`uv tool install` when uv is available. npm's own `bin` shim is still the PATH
entrypoint, so `agrep` works even if uv's tool directory is not on PATH.

The npm shim pins the matching PyPI version under the hood, so npm and PyPI releases
do not drift. Prefer the direct route? `uv tool install agrep==0.1.5` - same thing,
no node in the middle.

Full docs: https://github.com/dannyisbad/agrep
