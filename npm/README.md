# agrep

grep your AI coding agents' chat history - Claude Code, Codex, opencode,
Antigravity, Kimi CLI, Cline, Gemini CLI, crush, Cursor, pi, and oh-my-pi -
straight from the shell. One searchable cross-agent history, budgeted context
recall, live session board, and native resume.

This npm package is a thin shim: agrep is a python package with a bundled rust binary,
and the shim runs it through [uv](https://docs.astral.sh/uv/) (or pipx). uv manages
python itself, so this works even on a machine with no python installed.

```
uvx agrep "race condition"   # zero-install try (uv runs the real PyPI package)
# or on npm: npm i -g @mundy/agrep   (npx @mundy/agrep "race condition")
agrep "race condition"     # first run indexes your agent stores, then greps
```

Global npm installs also warm uv's ephemeral cache with the matching PyPI tool.
npm's own `bin` shim remains the PATH entrypoint, so the install does not create
or replace a persistent uv-managed command.

The npm shim pins the matching PyPI version under the hood, so npm and PyPI releases
do not drift. Prefer the direct route? `uv tool install agrep` installs the same
agrep package without the Node shim.

Full docs: https://github.com/dannyisbad/agrep
