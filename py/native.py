"""Resume a session in its OWN native CLI, cd'd to the directory it ran in.

The live view shows sessions across four agents; this is the "jump back into that one"
button. Given (agent, session), it:
  1. resolves the real working directory from that agent's store (each records its cwd),
  2. builds the agent's documented resume command,
  3. opens a NEW terminal there running it. Windows: Windows Terminal if present, else
     `start`. macOS: Terminal.app via osascript. Linux: $TERMINAL or the first common
     emulator found.

Resume commands (verified against each CLI's --help):
  claude    claude --resume <id>
  codex     codex resume <id>
  opencode  opencode --session <id>
  agy       agy --conversation <id>      (antigravity)

This SPAWNS a process on the local machine. It's gated to: a known agent, a session id
that matches a strict id pattern (no shell metacharacters), and a cwd read from the store
(never from the client). The spawn is list-form (no shell) so nothing is interpolated.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys

HOME = os.path.expanduser("~")

# session ids: claude/codex/antigravity uuids, opencode `ses_...`. Strict allowlist — this
# value reaches a process argv, so anything outside [A-Za-z0-9._-] is rejected outright.
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{6,128}$")

_OPENCODE_DBS = ["opencode.db", "opencode-dev.db", "opencode-local.db",
                 "opencode-dev-before-copy.db"]

# agent -> (exe, [args before the id], id-goes-here)
_RESUME = {
    "claude":      ("claude", ["--resume"]),
    "codex":       ("codex", ["resume"]),
    "opencode":    ("opencode", ["--session"]),
    "antigravity": ("agy", ["--conversation"]),
}


def _claude_cwd(session: str) -> str:
    for path in glob.glob(os.path.join(HOME, ".claude", "projects", "*", f"{session}.jsonl")):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"cwd"' not in line:
                        continue
                    cwd = (json.loads(line) or {}).get("cwd")
                    if cwd:
                        return cwd
        except (OSError, json.JSONDecodeError):
            continue
    return ""


def _codex_cwd(session: str) -> str:
    pat = os.path.join(HOME, ".codex", "sessions", "**", f"rollout-*{session}.jsonl")
    for path in glob.glob(pat, recursive=True):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                p = (json.loads(f.readline()) or {}).get("payload") or {}
            if p.get("cwd"):
                return p["cwd"]
        except (OSError, json.JSONDecodeError):
            continue
    return ""


def _opencode_cwd(session: str) -> str:
    for name in _OPENCODE_DBS:
        path = os.path.join(HOME, ".local", "share", "opencode", name)
        if not os.path.exists(path):
            continue
        try:
            conn = sqlite3.connect(f"file:{path.replace(chr(92), '/')}?mode=ro&immutable=1", uri=True)
            row = conn.execute("SELECT directory FROM session WHERE id = ?", (session,)).fetchone()
            conn.close()
            if row and row[0]:
                return row[0]
        except sqlite3.Error:
            continue
    return ""


def _antigravity_cwd(session: str) -> str:
    tr = os.path.join(HOME, ".gemini", "antigravity-cli", "brain", session,
                      ".system_generated", "logs", "transcript.jsonl")
    counts: dict[str, int] = {}
    try:
        with open(tr, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for tc in e.get("tool_calls") or []:
                    cwd = (tc.get("args") or {}).get("Cwd")
                    if isinstance(cwd, str):
                        # antigravity double-encodes args: the value is itself a JSON string
                        # ("\"C:\\\\...\""). Decode that inner layer, else fall back to a strip.
                        try:
                            cwd = json.loads(cwd)
                        except (json.JSONDecodeError, TypeError):
                            cwd = cwd.strip().strip('"')
                        if isinstance(cwd, str) and cwd:
                            counts[cwd] = counts.get(cwd, 0) + 1
    except OSError:
        return ""
    return max(counts, key=counts.get) if counts else ""


_RESOLVERS = {"claude": _claude_cwd, "codex": _codex_cwd,
              "opencode": _opencode_cwd, "antigravity": _antigravity_cwd}


def resolve_cwd(agent: str, session: str) -> str:
    fn = _RESOLVERS.get(agent)
    cwd = fn(session) if fn else ""
    return cwd if cwd and os.path.isdir(cwd) else ""


def resume_argv(agent: str, session: str, cwd: str) -> list[str]:
    """The exact resume command for an agent. opencode scopes sessions by project, so
    it gets the directory as its positional (`opencode <dir> --session <id>`) — relying
    on the process cwd alone was unreliable (the 'opencode resume is broken' bug).
    The others find the session from cwd, set by the caller."""
    exe, pre = _RESUME[agent]
    if agent == "opencode" and cwd:
        return [exe, cwd, *pre, session]
    return [exe, *pre, session]


def resume_in_place(agent: str, session: str) -> int:
    """Run the agent's resume IN THE CURRENT terminal (cd'd to the session's dir), for
    the `agrep resume` CLI. Unlike open_session this doesn't spawn a window — the agent
    inherits this terminal and replaces the prompt until it exits. Returns its exit code
    (127 if the agent CLI isn't on PATH)."""
    if agent not in _RESUME:
        print(f"unknown agent {agent!r}", file=sys.stderr)
        return 2
    cwd = resolve_cwd(agent, session) or HOME
    exe = _RESUME[agent][0]
    exe_path = shutil.which(exe)
    if not exe_path:
        print(f"the {agent} CLI ('{exe}') isn't on your PATH — install it to resume here.",
              file=sys.stderr)
        return 127
    argv = resume_argv(agent, session, cwd)
    argv[0] = exe_path  # resolved (handles .cmd/.exe shims on Windows)
    print(f"\033[2m↻ resuming {agent} · {os.path.basename(cwd)} · {cwd}\033[0m",
          file=sys.stderr)
    try:
        return subprocess.run(argv, cwd=cwd).returncode
    except OSError as e:
        print(f"couldn't launch {agent}: {e}", file=sys.stderr)
        return 1


def _spawn_windows(argv: list[str], cwd: str) -> str:
    # cmd /k: run the resume command and KEEP the shell open in `cwd` afterwards —
    # exiting the agent should leave you in a usable shell already cd'd into the
    # project, not close the tab under you. (Also required for claude: --resume only
    # finds the session when run from the directory the session belongs to.)
    keep = ["cmd", "/k", *argv]
    wt = shutil.which("wt") or shutil.which("wt.exe")
    if wt:
        subprocess.Popen([wt, "-d", cwd, *keep], close_fds=True)
        return "wt"
    subprocess.Popen(["cmd", "/c", "start", "tilt", *keep], cwd=cwd,
                     close_fds=True, creationflags=0)
    return "start"


def _spawn_macos(argv: list[str], cwd: str) -> str:
    # Terminal.app via osascript: `do script` opens a new window, runs the command,
    # and leaves the shell open — same UX as cmd /k. argv pieces are shlex-quoted
    # before being embedded, then AppleScript-escaped.
    sh = f"cd {shlex.quote(cwd)} && {' '.join(shlex.quote(a) for a in argv)}"
    esc = sh.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.Popen(["osascript",
                      "-e", f'tell application "Terminal" to do script "{esc}"',
                      "-e", 'tell application "Terminal" to activate'],
                     close_fds=True)
    return "Terminal.app"


def _spawn_linux(argv: list[str], cwd: str) -> str:
    sh = f"cd {shlex.quote(cwd)} && {' '.join(shlex.quote(a) for a in argv)}; exec $SHELL"
    term = os.environ.get("TERMINAL") or ""
    term = shutil.which(term) if term else None
    gnome = None if term else shutil.which("gnome-terminal")
    if gnome:
        subprocess.Popen([gnome, f"--working-directory={cwd}", "--",
                          "bash", "-lc", sh], close_fds=True)
        return "gnome-terminal"
    term = term or shutil.which("x-terminal-emulator") or shutil.which("konsole") \
        or shutil.which("xterm")
    if not term:
        raise OSError("no terminal emulator found (set $TERMINAL)")
    subprocess.Popen([term, "-e", "bash", "-lc", sh], cwd=cwd, close_fds=True)
    return os.path.basename(term)


def open_session(agent: str, session: str) -> dict:
    """Spawn a terminal in the session's cwd running the agent's resume command."""
    if agent not in _RESUME:
        return {"ok": False, "error": f"unknown agent {agent!r}"}
    if not _ID_RE.match(session or ""):
        return {"ok": False, "error": "invalid session id"}
    cwd = resolve_cwd(agent, session) or HOME
    argv = resume_argv(agent, session, cwd)
    try:
        if sys.platform == "win32":
            via = _spawn_windows(argv, cwd)
        elif sys.platform == "darwin":
            via = _spawn_macos(argv, cwd)
        else:
            via = _spawn_linux(argv, cwd)
        return {"ok": True, "agent": agent, "cwd": cwd, "cmd": " ".join(argv), "via": via}
    except (OSError, ValueError) as e:
        return {"ok": False, "error": str(e), "cmd": " ".join(argv), "cwd": cwd}
