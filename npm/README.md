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

Global npm installs run one-time setup from postinstall
(`agrep setup --yes --no-semantic --no-archive`): the index is built and the
detected agents are taught immediately; every written block is disclosed in
the setup output and undone by `agrep remove`. That includes the
uninstall-cleanup sentinel (a launchd agent, systemd user units, or a
scheduled task) whose only job is stripping agrep's own blocks and hooks if
the package disappears. Set `AGREP_NO_AUTO_SETUP=1` to only warm uv's cache
instead, or `AGREP_SKIP_POSTINSTALL=1` to skip postinstall entirely. npm's
own `bin` shim remains the PATH entrypoint, so the install does not create
or replace a persistent uv-managed command.

The npm shim pins the matching PyPI version under the hood, so npm and PyPI releases
do not drift. Prefer the direct route? `uv tool install agrep` installs the same
agrep package without the Node shim.

Full docs: https://github.com/dannyisbad/agrep
