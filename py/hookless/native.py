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
    store_roots,
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
    "pi":          ("omp", ["-r"]),
}


def _pi_session_file(session: str) -> tuple[str, str] | None:
    """The pi/omp transcript owning ``session`` and its store root.

    Layout is <root>/<slug-dir>/<stamp>_<session>.jsonl under both ~/.pi and
    ~/.omp; the file name carries the full session id, so one bounded scandir
    walk resolves it without opening transcripts."""
    suffix = f"_{session}.jsonl"
    for root in store_roots("pi"):
        try:
            slugs = list(os.scandir(root))
        except OSError:
            continue
        for slug in slugs:
            try:
                if not slug.is_dir(follow_symlinks=False):
                    continue
                with os.scandir(slug.path) as files:
                    for entry in files:
                        if (entry.name.endswith(suffix)
                                and entry.is_file(follow_symlinks=False)):
                            return entry.path, root
            except OSError:
                continue
    return None


def _pi_cwd(session: str) -> str:
    """The session header row carries the launch cwd (`type: session`)."""
    found = _pi_session_file(session)
    if not found:
        return ""
    try:
        with open(found[0], encoding="utf-8", errors="replace") as stream:
            for _ in range(64):
                line = stream.readline()
                if not line:
                    break
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("type") == "session":
                    cwd = row.get("cwd")
                    return cwd if isinstance(cwd, str) else ""
    except OSError:
        return ""
    return ""


def _resume_exe(agent: str, session: str) -> str:
    """pi sessions resume with the binary owning their store root (oh-my-pi is
    a pi fork sharing the CLI surface, `-r <id>` included); when the owner is
    not installed the sibling fork can open the shared format. Every other
    agent is the static table."""
    exe = _RESUME[agent][0]
    if agent != "pi":
        return exe
    found = _pi_session_file(session)
    parts = found[1].split(os.sep) if found else []
    owner = "omp" if ".omp" in parts else "pi"
    if shutil.which(owner):
        return owner
    sibling = "pi" if owner == "omp" else "omp"
    return sibling if shutil.which(sibling) else owner


def new_session_exe(agent: str, requested: str | None = None) -> tuple[str, str | None]:
    """(executable, resolved path or None) for a fresh session. A pi-family
    launch runs the fork the user named (`pi` or `omp`), falling back to the
    sibling fork when that one is not installed; every other agent is the
    static table."""
    exe = _RESUME[agent][0]
    if agent != "pi":
        return exe, shutil.which(exe)
    wanted = requested if requested in ("pi", "omp") else "pi"
    sibling = "omp" if wanted == "pi" else "pi"
    for candidate in (wanted, sibling):
        path = shutil.which(candidate)
        if path:
            return candidate, path
    return wanted, None


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
    from hookless import _log

    override = _log.color_override()
    if override is None:
        colour = _vt_ok()
    else:
        colour = override
        if colour:
            _vt_ok()  # forced colour on a console still needs VT processing on
    return f"\033[2m{text}\033[0m" if colour else text


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
              "opencode": _opencode_cwd, "antigravity": _antigravity_cwd,
              "pi": _pi_cwd}
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


def _own_claude_link(root: str, session: str, dst: str) -> bool:
    """Exactly the link a previous resume made: absolute, aimed at this
    session's transcript under another slug of the same store, and that
    transcript is a plain file. Anything else stays refused."""
    try:
        target = os.readlink(dst)
    except OSError:
        return False
    if (not os.path.isabs(target)
            or os.path.basename(target) != f"{session}.jsonl"
            or os.path.normpath(os.path.dirname(os.path.dirname(target)))
            != os.path.normpath(root)
            or os.path.normpath(os.path.dirname(target))
            == os.path.normpath(os.path.dirname(dst))):
        return False
    try:
        info = os.lstat(target)
    except OSError:
        return False
    attrs = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISREG(info.st_mode) and not (attrs & reparse)


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

    # the destination is created only once a transcript to link is in hand: an
    # unresumable id must not leave an empty project folder behind
    try:
        existing = os.lstat(dst)
    except (FileNotFoundError, NotADirectoryError):
        existing = None
    if existing is not None:
        if stat.S_ISLNK(existing.st_mode) and _own_claude_link(root, session, dst):
            return
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
        stdout=None, stderr=None, hide_window: bool = False,
        handoff: _TerminalHandoff | None = None) -> subprocess.Popen:
    if sys.platform != "win32":
        if handoff is None:
            return subprocess.Popen(
                argv, cwd=cwd, env=env, stdout=stdout, stderr=stderr,
                start_new_session=True)
        # an interactive agent stays in the caller's session, so the terminal remains
        # its controlling tty (SIGWINCH, /dev/tty prompts), in a process group of its
        # own that is lent the foreground - the way a shell starts a foreground job
        tty_fd = handoff.fd if handoff.holds_foreground() else None

        def own_group_and_terminal() -> None:
            os.setpgid(0, 0)
            if tty_fd is not None:
                # the child takes the terminal too (so does the parent): whichever
                # runs first wins, and the agent never sees itself in the background
                _tcsetpgrp(tty_fd, os.getpid())

        process = subprocess.Popen(
            argv, cwd=cwd, env=env, stdout=stdout, stderr=stderr,
            preexec_fn=own_group_and_terminal)
        try:
            os.setpgid(process.pid, process.pid)
        except OSError:
            pass  # EACCES once it exec'd, ESRCH once it exited: the group is already its own
        handoff.give(process.pid)
        return process
    child_env = dict(os.environ if env is None else env)
    child_env[_OWNED_PAYLOAD] = _owned_windows_payload(argv, cwd)
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
    if hide_window:
        creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return subprocess.Popen(
        _owned_bootstrap_argv(),
        cwd=cwd, env=child_env, stdout=stdout, stderr=stderr,
        creationflags=creationflags)


def _tcsetpgrp(fd: int, group: int) -> bool:
    """tcsetpgrp from a background group raises SIGTTOU, which stops the caller
    unless blocked: block it around the call, as a job-control shell does."""
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTTOU})
    try:
        os.tcsetpgrp(fd, group)
        return True
    except OSError:
        return False
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, mask)


def _continue_group(group: int) -> None:
    try:
        os.killpg(group, signal.SIGCONT)
    except OSError:
        pass


def _interactive_stdio() -> bool:
    """An interactive agent run: the user's keyboard and screen are this process's
    stdin and stdout. A pipe on either side makes agrep a pipeline stage whose
    partners share the terminal, and the foreground must stay with the whole job."""
    try:
        return os.isatty(0) and os.isatty(1)
    except (OSError, ValueError):
        return False


class _TerminalHandoff:
    """Lends the controlling terminal's foreground to an owned process group and
    takes it back when that group exits or stops. Without a controlling terminal,
    or while this process is not the foreground itself, nothing is lent and the
    child simply runs in its own group (a background job's child)."""

    def __init__(self) -> None:
        self.fd: int | None = None
        self.lent: int | None = None
        try:
            self.fd = os.open(
                "/dev/tty", os.O_RDWR | os.O_NOCTTY | getattr(os, "O_CLOEXEC", 0))
        except OSError:
            pass

    def foreground(self) -> int | None:
        if self.fd is None:
            return None
        try:
            return os.tcgetpgrp(self.fd)
        except OSError:
            return None

    def holds_foreground(self) -> bool:
        return self.fd is not None and self.foreground() == os.getpgrp()

    def give(self, group: int) -> bool:
        """Hand the foreground over, only while this process holds it (or the
        child already took it for itself)."""
        if self.fd is None:
            return False
        if self.foreground() != group:
            if not self.holds_foreground() or not _tcsetpgrp(self.fd, group):
                return False
        self.lent = group
        return True

    def lend(self, group: int) -> None:
        """Give once this process becomes the foreground (`fg` after a `&` launch)."""
        if self.lent is None and self.fd is not None:
            self.give(group)

    def take_back(self) -> None:
        """Reclaim the foreground this process lent; never steal one it did not."""
        if self.lent is None:
            return
        if self.foreground() not in (None, os.getpgrp()):
            _tcsetpgrp(self.fd, os.getpgrp())
        self.lent = None

    def close(self) -> None:
        self.take_back()
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


def _reap_owned(process: subprocess.Popen) -> tuple[str, int]:
    """Poll the owned leader without hiding stops: 'running', 'stopped' (with
    the stop signal) or 'exited' (returncode recorded on `process`)."""
    if process.returncode is not None:
        return "exited", 0
    try:
        pid, status = os.waitpid(process.pid, os.WNOHANG | os.WUNTRACED)
    except ChildProcessError:
        process.returncode = 0
        return "exited", 0
    if pid == 0:
        return "running", 0
    if os.WIFSTOPPED(status):
        return "stopped", os.WSTOPSIG(status)
    process.returncode = os.waitstatus_to_exitcode(status)
    return "exited", 0


def _owned_child_stopped(
        process: subprocess.Popen, handoff: _TerminalHandoff, stop_signal: int,
) -> None:
    """Mirror the agent's stop onto this job as a shell would see it: take the
    terminal back, stop this whole process group (a wrapper such as the npm shim
    sits in it too), and once continued hand the terminal back and continue the agent."""
    group = process.pid
    if (stop_signal in (signal.SIGTTIN, signal.SIGTTOU)
            and handoff.foreground() == group):
        # it touched the terminal a beat before the handoff landed; it is the
        # foreground now, so it just goes on
        _continue_group(group)
        return
    handoff.take_back()
    previous = signal.getsignal(signal.SIGTSTP)
    signal.signal(signal.SIGTSTP, signal.SIG_DFL)
    try:
        # discarded by the kernel when this group is orphaned (no shell to `fg` it),
        # in which case the agent is simply continued below
        os.killpg(os.getpgrp(), signal.SIGTSTP)
    except OSError:
        pass
    finally:
        signal.signal(signal.SIGTSTP, previous)
    handoff.give(group)
    _continue_group(group)


def _posix_group_active(group: int) -> bool | None:
    return process_util._process_group_active(group)


def _signal_owned(process: subprocess.Popen, signum: int) -> None:
    if process.poll() is not None:
        return
    if sys.platform != "win32":
        try:
            os.killpg(process.pid, signum)
            if signum != signal.SIGINT:
                # a stopped group acts on a termination only once continued
                os.killpg(process.pid, signal.SIGCONT)
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


_OWNED_KILLED_GROUP_REAP_S = 10.0


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
    # Every member has SIGKILL by now; only the kernel's reaping is left, and a
    # saturated host can take seconds over it. Waiting here cannot hang.
    deadline = time.monotonic() + _OWNED_KILLED_GROUP_REAP_S
    active = _posix_group_active(group)
    while active is not False and time.monotonic() < deadline:
        if _reaped_owner_group_gone(process):
            # the pgid is a stranger's now: the owned group ended, nothing to wait for
            return True
        time.sleep(0.02)
        active = _posix_group_active(group)
    return active is False or _reaped_owner_group_gone(process)


def _wait_owned(
        process: subprocess.Popen, *, timeout: float | None = None,
        relay_signals: bool = False,
        tick: Callable[[subprocess.Popen], None] | None = None,
        signal_box: list[int | None] | None = None,
        handoff: _TerminalHandoff | None = None,
) -> tuple[int, int | None, bool, bool]:
    caught: int | None = signal_box[0] if signal_box is not None else None
    handlers: dict[int, object] = {}
    suspend_requested = False

    def forward(signum, _frame) -> None:
        nonlocal caught
        caught = caught or signum
        if signal_box is not None:
            signal_box[0] = signal_box[0] or signum
        _signal_owned(process, signum)

    def suspend(_signum, _frame) -> None:
        nonlocal suspend_requested
        suspend_requested = True

    def alive() -> bool:
        if handoff is None:
            return process.poll() is None
        state, stop_signal = _reap_owned(process)
        if state == "stopped":
            _owned_child_stopped(process, handoff, stop_signal)
            return True
        if state == "running":
            handoff.lend(process.pid)
        return state == "running"

    if relay_signals:
        for signum in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGHUP", None)):
            if signum is None:
                continue
            try:
                handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, forward)
            except (ValueError, OSError):
                handlers.pop(signum, None)
        if handoff is not None:
            try:
                handlers[signal.SIGTSTP] = signal.getsignal(signal.SIGTSTP)
                signal.signal(signal.SIGTSTP, suspend)
            except (ValueError, OSError):
                handlers.pop(signal.SIGTSTP, None)
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
        while alive():
            if tick is not None:
                tick(process)
            if suspend_requested:
                # a SIGTSTP aimed at this wrapper: stop the agent's group; alive()
                # then mirrors the stop and resumes both on SIGCONT
                suspend_requested = False
                try:
                    os.killpg(process.pid, signal.SIGTSTP)
                except ProcessLookupError:
                    pass
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
        if handoff is not None:
            handoff.take_back()
    drained = _drain_owned(process, caught or signal.SIGTERM)
    return _canonical_returncode(process.returncode), caught, timed_out, drained


def run_owned_process(
        argv: list[str], *, cwd: str, env: dict[str, str] | None = None,
        timeout: float | None = None, capture_output: bool = False,
        relay_signals: bool = False, hide_window: bool = False,
        tick: Callable[[subprocess.Popen], None] | None = None,
        foreground: bool = False,
) -> subprocess.CompletedProcess:
    """Run argv as an owned process tree and wait for it.

    foreground (POSIX): for an interactive run - stdin and stdout both the
    terminal - the child shares this session, gets its own process group and
    is lent the controlling terminal's foreground, so an interactive agent
    receives SIGWINCH and Ctrl-C/Ctrl-Z directly and can open /dev/tty; a stop
    is mirrored onto this job and the foreground is restored on exit. As a
    pipeline stage (`agrep run x -- -p ... | less`) nothing is lent and the child
    runs detached as before, so a partner reading /dev/tty is never stopped."""
    stdout_file = tempfile.TemporaryFile() if capture_output else None
    stderr_file = tempfile.TemporaryFile() if capture_output else None
    process = None
    handoff: _TerminalHandoff | None = None
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
        if foreground and sys.platform != "win32" and _interactive_stdio():
            handoff = _TerminalHandoff()
        process = _launch_owned(
            argv, cwd=cwd, env=env, stdout=stdout_file, stderr=stderr_file,
            hide_window=hide_window, handoff=handoff)
        returncode, caught, timed_out, drained = _wait_owned(
            process, timeout=timeout, relay_signals=relay_signals, tick=tick,
            signal_box=signal_box, handoff=handoff)
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
        if handoff is not None:
            handoff.take_back()
        if process is not None:
            _drain_owned(
                process,
                (signal_box[0] if signal_box and signal_box[0]
                 else signal.SIGTERM))
        raise
    finally:
        if handoff is not None:
            handoff.close()
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
    exe = _resume_exe(agent, session)
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
        return run_owned_process(
            argv, cwd=cwd, relay_signals=True, foreground=True).returncode
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
    exe = _resume_exe(agent, session)
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
