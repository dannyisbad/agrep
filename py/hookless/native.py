"""Resume a session in its OWN native CLI, cd'd to the directory it ran in.

Two paths: open_session() spawns a NEW terminal (the app's resume button - wt/start on
Windows, Terminal.app via osascript on mac, $TERMINAL on linux); resume_in_place() runs
the agent in the CURRENT terminal (`agrep resume`). Both resolve the real cwd from the
agent's store and build the command via resume_argv().

Resume commands (verified against each CLI's --help):
  claude    claude --resume <id>
  codex     codex resume <id>
  opencode  opencode <dir> --session <id>   (dir POSITIONAL - opencode scopes by project)
  agy       agy --conversation <id>         (antigravity)

Spawn safety: known agent only, session id checked against a strict pattern (no shell
metacharacters), cwd read from the store (never from the client), list-form spawn.
"""

from __future__ import annotations

import base64
import errno
import glob
import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Callable

from hookless import proc as process_util

from hookless.locators import (
    discovery_home,
    opencode_data_dirs,
    opencode_db_name as _opencode_db_name,
    opencode_explicit_db,
    store_root,
)
from hookless.registry import (
    NATIVE_RESUME_AGENTS,
    capability_error,
    require_exact,
)

HOME = discovery_home()

# ids reach a process argv - strict allowlist, anything outside [A-Za-z0-9._-] is rejected.
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{6,128}$")

# agent -> (exe, [args before the id], id-goes-here)
_RESUME = {
    "claude":      ("claude", ["--resume"]),
    "codex":       ("codex", ["resume"]),
    "opencode":    ("opencode", ["--session"]),
    "antigravity": ("agy", ["--conversation"]),
}


def opencode_db_paths(home: str = HOME, *, include_default: bool = False) -> list[str]:
    dirs = opencode_data_dirs(home)
    paths = []
    override = opencode_explicit_db(home)
    if override:
        paths.append(override)
    for directory in dirs:
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        regular = entry.is_file(follow_symlinks=False)
                    except OSError:
                        continue
                    if _opencode_db_name(entry.name) and regular:
                        paths.append(entry.path)
        except OSError:
            continue
    if include_default and not paths:
        paths.append(os.path.join(dirs[0], "opencode.db"))
    out = []
    seen = set()
    for path in paths:
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen and (include_default or os.path.isfile(path)) and not os.path.islink(path):
            seen.add(key)
            out.append(path)
    return out


_VT_OK: bool | None = None


def _vt_ok() -> bool:
    """stderr accepts ANSI: any posix tty, or a win console with VT enabled.
    Inline (no common import - the hookless boundary bans product imports)."""
    global _VT_OK
    if _VT_OK is not None:
        return _VT_OK
    if not sys.stderr.isatty():
        _VT_OK = False
    elif os.name != "nt":
        _VT_OK = True
    else:
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            handle = k32.GetStdHandle(-12)  # STD_ERROR_HANDLE
            mode = ctypes.c_uint32()
            _VT_OK = bool(
                k32.GetConsoleMode(handle, ctypes.byref(mode))
                and k32.SetConsoleMode(handle, mode.value | 0x0004))
        except Exception:  # noqa: BLE001 -- no VT is a rendering downgrade, not an error
            _VT_OK = False
    return _VT_OK


def _dim(text: str) -> str:
    return f"\033[2m{text}\033[0m" if _vt_ok() else text


def _terminal_safe(value: object) -> str:
    """Render store and filesystem text without terminal control sequences."""
    out = []
    for char in str(value):
        code = ord(char)
        if (code < 0x20 or 0x7F <= code <= 0x9F
                or unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"}):
            out.append(f"\\u{code:04x}")
        else:
            out.append(char)
    return "".join(out)


def _claude_slug(path: str) -> str:
    """Claude Code's project-folder encoding: every non-alphanumeric char -> '-'. So
    `C:\\Temp\\vo-exp\\gatetest` -> `C--Temp-vo-exp-gatetest`. It's lossy (a real '-' and a
    separator both become '-'), so you can't decode a folder name back to a path - but you
    CAN re-encode a candidate cwd and check it matches the folder."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def _claude_cwd(session: str) -> str:
    """The dir `claude --resume <id>` must run from: the one ANCHORING the session, i.e. whose
    slug equals the `~/.claude/projects/<slug>/` folder the transcript lives under. Claude
    scopes resume to that folder. The recorded `cwd` drifts mid-session (the agent cd's
    around) and the UI shows the most-WORKED-in folder, so neither the first nor the busiest
    cwd is reliably the anchor - match the containing folder. Falls back to the first recorded
    cwd only when nothing matches (unexpected: a session's own cwd should encode to its folder)."""
    root = store_root("claude", HOME)
    for path in glob.iglob(os.path.join(root, "*", f"{session}.jsonl")):
        slug = os.path.basename(os.path.dirname(path))
        cwds: list[str] = []
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"cwd"' not in line:
                        continue
                    try:
                        cwd = (json.loads(line) or {}).get("cwd")
                    except json.JSONDecodeError:
                        continue
                    if cwd and cwd not in cwds:
                        cwds.append(cwd)
        except OSError:
            continue
        if not cwds:
            continue
        # slug casing can drift from the cwd's; Windows paths are case-insensitive anyway.
        want = slug.lower()
        for cwd in cwds:
            if _claude_slug(cwd).lower() == want:
                return cwd
        return cwds[0]
    return ""


def _codex_cwd(session: str) -> str:
    pat = os.path.join(
        store_root("codex", HOME), "**", f"rollout-*{session}.jsonl")
    for path in glob.iglob(pat, recursive=True):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                p = (json.loads(f.readline()) or {}).get("payload") or {}
            if p.get("cwd"):
                return p["cwd"]
        except (OSError, json.JSONDecodeError):
            continue
    return ""


def _opencode_cwd(session: str) -> str:
    for path in opencode_db_paths(HOME, include_default=True):
        conn = None
        try:
            conn = sqlite3.connect(
                Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
            row = conn.execute("SELECT directory FROM session WHERE id = ?", (session,)).fetchone()
            if row and row[0]:
                return row[0]
        except sqlite3.Error:
            continue
        finally:
            if conn is not None:
                conn.close()
    return ""


def _antigravity_cwd(session: str) -> str:
    tr = os.path.join(
        store_root("antigravity", HOME), session,
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
require_exact("native resume commands", _RESUME, NATIVE_RESUME_AGENTS)
require_exact("native cwd resolvers", _RESOLVERS, NATIVE_RESUME_AGENTS)


def _remap_dead_cwd(cwd: str) -> str:
    """A recorded cwd that doesn't exist here is usually a tree migrated from another
    machine (e.g. an imported Windows home under another local subtree). Re-anchor it: try each
    path suffix, longest first, under $HOME and $HOME's immediate subdirs, and accept a
    match only when it's unique - two candidates means guessing, so give up instead."""
    parts = [p for p in re.split(r"[\\/]+", cwd) if p and not re.fullmatch(r"[A-Za-z]:", p)]
    roots = [HOME]
    try:
        roots += [e.path for e in os.scandir(HOME)
                  if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")]
    except OSError:
        pass
    for i in range(len(parts)):
        hits = [h for h in sorted({os.path.join(r, *parts[i:]) for r in roots})
                if os.path.isdir(h)]
        if len(hits) == 1:
            print(_dim(f"↪ {_terminal_safe(cwd)} doesn't exist here - re-anchored to "
                       f"{_terminal_safe(hits[0])}"),
                  file=sys.stderr)
            return hits[0]
        if hits:
            print(_dim(f"↪ {_terminal_safe(cwd)} doesn't exist here and {len(hits)} "
                       "local dirs match its "
                       "suffix - not guessing"), file=sys.stderr)
            return ""
    return ""


def _claude_link_session(session: str, cwd: str) -> None:
    """`claude --resume` only sees transcripts under the launch cwd's project slug, and a
    re-anchored cwd has a different slug than the transcript was recorded under - link it
    across so resume finds it (copy where symlinks aren't allowed, e.g. stock Windows).
    No-op when the transcript is already visible from `cwd`."""
    root = store_root("claude", HOME)
    dst_dir = os.path.join(root, _claude_slug(cwd))
    dst = os.path.join(dst_dir, f"{session}.jsonl")

    def linklike(path: str, info: os.stat_result) -> bool:
        attrs = getattr(info, "st_file_attributes", 0)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return stat.S_ISLNK(info.st_mode) or bool(attrs & reparse)

    def ensure_parent() -> None:
        current = Path(HOME)
        parts = Path(dst_dir).relative_to(current).parts
        for part in (None, *parts):
            current = current if part is None else current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                try:
                    current.mkdir()
                except FileExistsError:
                    pass
                info = current.lstat()
            if linklike(os.fspath(current), info) or not stat.S_ISDIR(info.st_mode):
                raise OSError(f"unsafe Claude resume destination parent: {current}")

    ensure_parent()
    try:
        existing = os.lstat(dst)
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if linklike(dst, existing) or not stat.S_ISREG(existing.st_mode):
            raise OSError(f"unsafe Claude resume destination: {dst}")
        return
    for src in glob.iglob(os.path.join(root, "*", f"{session}.jsonl")):
        source = os.lstat(src)
        if linklike(src, source) or not stat.S_ISREG(source.st_mode):
            continue
        ensure_parent()
        try:
            os.symlink(src, dst)
            how = "linked"
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                raise OSError(f"unsafe Claude resume destination appeared: {dst}") from exc
            source_flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
                            | getattr(os, "O_NOFOLLOW", 0))
            source_fd = os.open(src, source_flags)
            try:
                def private_opener(path: str, flags: int) -> int:
                    flags |= (getattr(os, "O_BINARY", 0)
                              | getattr(os, "O_NOFOLLOW", 0))
                    return os.open(path, flags, 0o600)

                with open(dst, "xb", buffering=0,
                          opener=private_opener) as destination:
                    while True:
                        chunk = os.read(source_fd, 1024 * 1024)
                        if not chunk:
                            break
                        view = memoryview(chunk)
                        while view:
                            written = destination.write(view)
                            view = view[written:]
            finally:
                os.close(source_fd)
            how = "copied"
        ensure_parent()
        print(_dim(f"↪ {how} transcript into {_terminal_safe(dst_dir)} so "
                   "claude --resume finds it"),
              file=sys.stderr)
        return


_OWNED_PAYLOAD = "AGREP_OWNED_PAYLOAD"
_OWNED_BOOTSTRAP = (
    "import runpy,sys;"
    "sys.path.insert(0,sys.argv.pop(1));"
    "runpy.run_module('hookless.native',run_name='__main__')"
)


class OwnedProcessResult(subprocess.CompletedProcess):
    relayed_signal: int | None = None


def _owned_bootstrap_argv() -> list[str]:
    package_root = str(Path(__file__).resolve().parents[1])
    return [
        sys.executable, "-I", "-c", _OWNED_BOOTSTRAP,
        package_root, "--owned-child",
    ]


def _canonical_returncode(returncode: int | None) -> int:
    if returncode is None:
        return 1
    return 128 - returncode if returncode < 0 else returncode


def _owned_windows_payload(argv: list[str], cwd: str) -> str:
    return base64.b64encode(json.dumps(
        {"argv": argv, "cwd": cwd}, ensure_ascii=False,
        separators=(",", ":")).encode("utf-8")).decode("ascii")


def _launch_owned(
        argv: list[str], *, cwd: str, env: dict[str, str] | None = None,
        stdout=None, stderr=None, hide_window: bool = False) -> subprocess.Popen:
    if sys.platform != "win32":
        return subprocess.Popen(
            argv, cwd=cwd, env=env, stdout=stdout, stderr=stderr,
            start_new_session=True)
    child_env = dict(os.environ if env is None else env)
    child_env[_OWNED_PAYLOAD] = _owned_windows_payload(argv, cwd)
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
    if hide_window:
        creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return subprocess.Popen(
        _owned_bootstrap_argv(),
        cwd=cwd, env=child_env, stdout=stdout, stderr=stderr,
        creationflags=creationflags)


def _posix_group_active(group: int) -> bool | None:
    return process_util._process_group_active(group)


def _signal_owned(process: subprocess.Popen, signum: int) -> None:
    if process.poll() is not None:
        return
    if sys.platform != "win32":
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass
        return
    if signum == signal.SIGINT and hasattr(signal, "CTRL_BREAK_EVENT"):
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
            return
        except OSError:
            pass
    process.terminate()


def _reaped_owner_group_gone(process: subprocess.Popen) -> bool:
    """A reaped leader whose pid is occupied again marks a recycled pgid: the
    owned group's lifetime already ended, so it must never be signalled."""
    if process.poll() is None:
        return False
    group = process.pid
    return (process_util.pid_alive(group)
            or process_util.process_start_identity(group) is not None)


def _drain_owned(process: subprocess.Popen, signum: int | None = None) -> bool:
    if sys.platform == "win32":
        if process.poll() is None:
            _signal_owned(process, signum or signal.SIGTERM)
        try:
            process.wait(timeout=3)
            return True
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=2)
                return True
            except subprocess.TimeoutExpired:
                return False
    group = process.pid
    if process.poll() is None:
        _signal_owned(process, signum or signal.SIGTERM)
        try:
            process.wait(timeout=0.75)
        except subprocess.TimeoutExpired:
            pass
    active = _posix_group_active(group)
    if process.poll() is not None and active is not False:
        if _reaped_owner_group_gone(process):
            return True
        try:
            os.killpg(group, signal.SIGTERM)
        except ProcessLookupError:
            return True
    deadline = time.monotonic() + 1.5
    while active is not False and time.monotonic() < deadline:
        process.poll()
        time.sleep(0.02)
        active = _posix_group_active(group)
    if active is not False:
        if _reaped_owner_group_gone(process):
            return True
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        return False
    deadline = time.monotonic() + 1.0
    active = _posix_group_active(group)
    while active is not False and time.monotonic() < deadline:
        time.sleep(0.02)
        active = _posix_group_active(group)
    return active is False or _reaped_owner_group_gone(process)


def _wait_owned(
        process: subprocess.Popen, *, timeout: float | None = None,
        relay_signals: bool = False,
        tick: Callable[[subprocess.Popen], None] | None = None,
        signal_box: list[int | None] | None = None,
) -> tuple[int, int | None, bool, bool]:
    caught: int | None = signal_box[0] if signal_box is not None else None
    handlers: dict[int, object] = {}

    def forward(signum, _frame) -> None:
        nonlocal caught
        caught = caught or signum
        if signal_box is not None:
            signal_box[0] = signal_box[0] or signum
        _signal_owned(process, signum)

    if relay_signals:
        for signum in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGHUP", None)):
            if signum is None:
                continue
            try:
                handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, forward)
            except (ValueError, OSError):
                handlers.pop(signum, None)
    if signal_box is not None:
        caught = caught or signal_box[0]
    if caught is not None:
        _signal_owned(process, caught)
    deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
    signal_deadline: float | None = None
    timed_out = False
    try:
        if tick is not None:
            tick(process)
        while process.poll() is None:
            if tick is not None:
                tick(process)
            if caught is not None:
                signal_deadline = signal_deadline or time.monotonic() + 1.0
                if time.monotonic() >= signal_deadline:
                    break
            if deadline is not None and time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.05)
    finally:
        for signum, previous in handlers.items():
            signal.signal(signum, previous)
    drained = _drain_owned(process, caught or signal.SIGTERM)
    return _canonical_returncode(process.returncode), caught, timed_out, drained


def run_owned_process(
        argv: list[str], *, cwd: str, env: dict[str, str] | None = None,
        timeout: float | None = None, capture_output: bool = False,
        relay_signals: bool = False, hide_window: bool = False,
        tick: Callable[[subprocess.Popen], None] | None = None,
) -> subprocess.CompletedProcess:
    stdout_file = tempfile.TemporaryFile() if capture_output else None
    stderr_file = tempfile.TemporaryFile() if capture_output else None
    process = None
    signal_box: list[int | None] | None = [None] if relay_signals else None
    launch_handlers: dict[int, object] = {}

    def defer_signal(signum, _frame) -> None:
        if signal_box is not None:
            signal_box[0] = signal_box[0] or signum

    try:
        if relay_signals:
            for signum in (
                    signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGHUP", None)):
                if signum is None:
                    continue
                try:
                    launch_handlers[signum] = signal.getsignal(signum)
                    signal.signal(signum, defer_signal)
                except (ValueError, OSError):
                    launch_handlers.pop(signum, None)
        process = _launch_owned(
            argv, cwd=cwd, env=env, stdout=stdout_file, stderr=stderr_file,
            hide_window=hide_window)
        returncode, caught, timed_out, drained = _wait_owned(
            process, timeout=timeout, relay_signals=relay_signals, tick=tick,
            signal_box=signal_box)
        stdout = stderr = None
        if capture_output:
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read().decode("utf-8", errors="replace")
            stderr = stderr_file.read().decode("utf-8", errors="replace")
        if timed_out:
            raise subprocess.TimeoutExpired(argv, timeout, output=stdout, stderr=stderr)
        if not drained:
            raise OSError("owned child process tree did not drain")
        if caught is not None:
            returncode = 128 + caught
        result = OwnedProcessResult(argv, returncode, stdout, stderr)
        result.relayed_signal = caught
        return result
    except BaseException:
        if process is not None:
            _drain_owned(
                process,
                (signal_box[0] if signal_box and signal_box[0]
                 else signal.SIGTERM))
        raise
    finally:
        for signum, previous in launch_handlers.items():
            signal.signal(signum, previous)
        if stdout_file is not None:
            stdout_file.close()
        if stderr_file is not None:
            stderr_file.close()


def _owned_windows_child() -> int:
    raw = os.environ.pop(_OWNED_PAYLOAD, "")
    try:
        payload = json.loads(base64.b64decode(raw, validate=True).decode("utf-8"))
        argv, cwd = payload["argv"], payload["cwd"]
        if (not isinstance(argv, list) or not argv
                or not all(isinstance(arg, str) and arg for arg in argv)
                or not isinstance(cwd, str) or not cwd):
            raise ValueError("invalid owned child payload")
    except (KeyError, TypeError, UnicodeError, ValueError) as exc:
        print(f"owned child payload error: {exc}", file=sys.stderr)
        return 125
    if not process_util.bind_descendants_to_process_lifetime():
        print("owned child could not establish a Windows Job boundary", file=sys.stderr)
        return 125
    try:
        child = subprocess.Popen(argv, cwd=cwd)
    except OSError as exc:
        print(f"owned child launch failed: {_terminal_safe(exc)}", file=sys.stderr)
        return 127 if getattr(exc, "errno", None) == errno.ENOENT else 126
    return _canonical_returncode(child.wait())


def resolve_cwd(agent: str, session: str) -> str:
    fn = _RESOLVERS.get(agent)
    cwd = fn(session) if fn else ""
    if cwd and not os.path.isdir(cwd):
        cwd = _remap_dead_cwd(cwd)
    return cwd if cwd and os.path.isdir(cwd) else ""


def resume_argv(agent: str, session: str, cwd: str) -> list[str]:
    """The exact resume command for an agent. opencode scopes sessions by project, so
    it gets the directory as its positional (`opencode <dir> --session <id>`) - relying
    on the process cwd alone was unreliable (the 'opencode resume is broken' bug).
    The others find the session from cwd, set by the caller."""
    exe, pre = _RESUME[agent]
    if agent == "opencode" and cwd:
        return [exe, cwd, *pre, session]
    return [exe, *pre, session]


def resume_in_place(agent: str, session: str) -> int:
    """Run the agent's resume IN THE CURRENT terminal (cd'd to the session's dir), for
    the `agrep resume` CLI. Unlike open_session this doesn't spawn a window - the agent
    inherits this terminal and replaces the prompt until it exits. Returns its exit code
    (127 if the agent CLI isn't on PATH)."""
    unsupported = capability_error("native_resume", agent)
    if unsupported:
        print(_terminal_safe(unsupported), file=sys.stderr)
        return 2
    if _ID_RE.fullmatch(session or "") is None:
        print("invalid session id", file=sys.stderr)
        return 2
    exe = _RESUME[agent][0]
    exe_path = shutil.which(exe)
    if not exe_path:
        print(f"the {agent} CLI ('{exe}') isn't on your PATH - install it to resume here.",
              file=sys.stderr)
        return 127
    cwd = resolve_cwd(agent, session) or HOME
    if agent == "claude":
        try:
            _claude_link_session(session, cwd)
        except OSError as exc:
            print(f"couldn't prepare Claude resume: {_terminal_safe(exc)}", file=sys.stderr)
            return 1
    argv = resume_argv(agent, session, cwd)
    argv[0] = exe_path  # resolved (handles .cmd/.exe shims on Windows)
    print(_dim(f"↻ resuming {_terminal_safe(agent)} · "
               f"{_terminal_safe(os.path.basename(cwd))} · {_terminal_safe(cwd)}"),
          file=sys.stderr)
    try:
        if sys.platform == "win32" and Path(exe_path).suffix.lower() in (".cmd", ".bat"):
            host, env = _windows_resume_host(argv, cwd, keep_open=False)
            return run_owned_process(
                host, cwd=cwd, env=env, relay_signals=True).returncode
        return run_owned_process(argv, cwd=cwd, relay_signals=True).returncode
    except OSError as e:
        print(f"couldn't launch {_terminal_safe(agent)}: {_terminal_safe(e)}", file=sys.stderr)
        return 1


_POWERSHELL_RESUME = (
    "$raw=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("
    "$env:AGREP_RESUME_PAYLOAD));"
    "[Environment]::SetEnvironmentVariable('AGREP_RESUME_PAYLOAD',$null,'Process');"
    "$o=$raw|ConvertFrom-Json;Set-Location -LiteralPath ([string]$o.cwd);"
    "$a=@($o.argv);$rest=@($a|Select-Object -Skip 1);& ([string]$a[0]) @rest"
)


def _windows_resume_host(argv: list[str], cwd: str,
                         keep_open: bool = True) -> tuple[list[str], dict[str, str]]:
    """PowerShell host whose command text contains no user/store-controlled bytes.

    cmd.exe reparses ``&^()%!`` even when subprocess receives a list.  Carry the
    argv/cwd as base64 JSON in a one-shot environment variable and splat the decoded
    array, preserving legal Windows paths as data rather than shell syntax.
    """
    payload = base64.b64encode(json.dumps(
        {"argv": argv, "cwd": cwd}, ensure_ascii=False,
        separators=(",", ":")).encode("utf-8")).decode("ascii")
    env = os.environ.copy()
    env["AGREP_RESUME_PAYLOAD"] = payload
    powershell = (shutil.which("powershell.exe") or shutil.which("powershell")
                  or "powershell.exe")
    flags = [powershell, "-NoLogo", "-NoProfile"]
    if keep_open:
        flags.append("-NoExit")
    return ([*flags, "-Command", _POWERSHELL_RESUME], env)


def _spawn_windows(argv: list[str], cwd: str, same_window: bool = True) -> str:
    host, env = _windows_resume_host(argv, cwd)
    wt = shutil.which("wt") or shutil.which("wt.exe")
    if wt:
        # The cwd stays in the encoded payload because Windows Terminal parses semicolons.
        if same_window:
            subprocess.Popen([wt, "-w", "0", "new-tab", *host],
                             close_fds=True, env=env)
            return "wt-tab"
        subprocess.Popen([wt, "new-tab", *host], close_fds=True, env=env)
        return "wt"
    # a direct console avoids `cmd /c start` and its second metacharacter parse.
    subprocess.Popen(host, cwd=cwd, close_fds=True, env=env,
                     creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10))
    return "start"


def _spawn_macos(argv: list[str], cwd: str) -> str:
    # `do script` leaves the shell open after the command runs (cmd /k UX).
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


def open_session(agent: str, session: str, same_window: bool = True) -> dict:
    """Spawn a terminal in the session's cwd running the agent's resume command. On Windows,
    same_window (the default) opens it as a new tab in the current Windows Terminal window
    rather than a separate window; pass False for the old new-window behavior. (mac/linux
    always open a new window - reliable same-window tabbing there is terminal-specific.)"""
    unsupported = capability_error("native_resume", agent)
    if unsupported:
        return {"ok": False, "error": unsupported}
    if _ID_RE.fullmatch(session or "") is None:
        return {"ok": False, "error": "invalid session id"}
    exe = _RESUME[agent][0]
    exe_path = shutil.which(exe)
    if not exe_path:
        return {"ok": False,
                "error": f"the {agent} CLI ('{exe}') isn't on your PATH"}
    cwd = resolve_cwd(agent, session) or HOME
    argv = resume_argv(agent, session, cwd)
    argv[0] = exe_path
    try:
        if agent == "claude":
            _claude_link_session(session, cwd)
        if sys.platform == "win32":
            via = _spawn_windows(argv, cwd, same_window)
        elif sys.platform == "darwin":
            via = _spawn_macos(argv, cwd)
        else:
            via = _spawn_linux(argv, cwd)
        return {"ok": True, "agent": agent, "cwd": cwd, "cmd": " ".join(argv), "via": via}
    except (OSError, ValueError) as e:
        return {"ok": False, "error": str(e), "cmd": " ".join(argv), "cwd": cwd}


if __name__ == "__main__":
    if sys.argv[1:] == ["--owned-child"]:
        raise SystemExit(_owned_windows_child())
    raise SystemExit(2)
