"""`agrep live` and `agrep board` hand the terminal back on every exit path.

Each case runs the real CLI on a pseudo-terminal as the foreground job of a
minimal job-control parent that never repairs the tty, so `stty -a` before and
after measures the board alone (macOS sets PENDIN on every return to canonical
mode, so that flag is masked). The bytes the board wrote must also end with the
screen handed back: default colours, the cursor shown, the main screen. Only
fake agents are on PATH, and every chat in the live store is invented. Without
pseudo-terminals (Windows) every case skips.
"""

from __future__ import annotations

import json
import os
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
    import pty
    import termios
except ImportError:  # Windows: no pseudo-terminals
    fcntl = pty = termios = None

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from _test_support import isolate_data_dir  # noqa: E402

isolate_data_dir()

COMMANDS = ("live", "board")
PTYS = os.name == "posix" and pty is not None
ROWS, COLS = 30, 100
START_S = 90.0  # first frame: CLI start and the watcher's boot on a loaded box
STEP_S = 30.0
STTY = shutil.which("stty", path="/bin:/usr/bin") or "stty"
# Enter (CR, as a terminal sends a pasted newline), r, q, a, digits, b and c
PASTE = b"a1\rb2c\rrq\r3 resume it\r"

# The job-control parent: like an interactive shell it ignores the job-control signals, runs
# one job in its own process group, follows its stops on command and takes the terminal back
# whenever the job stops or ends; it never repairs the terminal's modes.
_SHELL = r'''
import fcntl, json, os, select, signal, subprocess, sys
spec = json.loads(sys.argv[1])
ctl, rep, tty = spec["ctl"], spec["rep"], 0
JOB_SIGNALS = ("SIGTTOU", "SIGTTIN", "SIGTSTP", "SIGINT", "SIGQUIT", "SIGHUP")
for name in JOB_SIGNALS:
    signal.signal(getattr(signal, name), signal.SIG_IGN)


def say(*words):
    os.write(rep, (json.dumps(words) + "\n").encode())


def stty():
    try:
        return subprocess.run([spec["stty"], "-a"], stdin=tty, capture_output=True,
                              text=True, timeout=30).stdout
    except Exception as exc:
        return f"unreadable: {exc}"


def own(pgid):
    try:
        os.tcsetpgrp(tty, pgid)
    except OSError:
        pass


def start(argv, foreground, ignore_hup=False, stdout=None):
    def prepare():
        os.setpgid(0, 0)
        if foreground:
            own(os.getpgrp())
        for name in JOB_SIGNALS:
            signal.signal(getattr(signal, name), signal.SIG_DFL)
        if ignore_hup:
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
    job = subprocess.Popen(argv, stdin=tty, stdout=tty if stdout is None else stdout,
                           stderr=2, env=spec["env"], cwd=spec["cwd"], preexec_fn=prepare)
    try:
        os.setpgid(job.pid, job.pid)
    except OSError:
        pass
    if foreground:
        own(job.pid)
    return job


def follow(job):
    while True:
        if select.select([ctl], [], [], 0.05)[0]:
            for command in os.read(ctl, 4096).decode().split():
                if command == "fg":
                    own(job.pid)
                    os.killpg(job.pid, signal.SIGCONT)
                elif command == "bg":
                    os.killpg(job.pid, signal.SIGCONT)
                elif command.startswith("kill:"):
                    os.killpg(job.pid, int(command[5:]))
        pid, status = os.waitpid(job.pid, os.WNOHANG | os.WUNTRACED)
        if not pid:
            continue
        own(os.getpgrp())
        if os.WIFSTOPPED(status):
            say("stopped", os.WSTOPSIG(status), stty())
            continue
        return os.WEXITSTATUS(status) if os.WIFEXITED(status) else -os.WTERMSIG(status)


say("before", stty())
stdout = spec.get("stdout_fd")
job = start(spec["argv"], spec["foreground"], spec["ignore_hup"], stdout)
if stdout is not None:
    os.close(stdout)
say("started", job.pid)
code = follow(job)
say("exit", code, stty())
# the job shares this open terminal: a flag it left set is the shell's problem too
say("nonblocking", bool(fcntl.fcntl(tty, fcntl.F_GETFL) & os.O_NONBLOCK))
if spec["next"]:
    say("next", follow(start(spec["next"], True)))
'''

# The program that gets the terminal after the board: it records every byte
# still waiting on the terminal, complete lines or not.
_RECORDER = r'''
import os, select, sys, termios, time
fd = 0
saved = termios.tcgetattr(fd)
raw = termios.tcgetattr(fd)
raw[3] &= ~termios.ICANON
raw[6][termios.VMIN], raw[6][termios.VTIME] = 0, 0
termios.tcsetattr(fd, termios.TCSANOW, raw)
data = b""
end = time.monotonic() + 1.5
while time.monotonic() < end:
    if select.select([fd], [], [], 0.05)[0]:
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        data += chunk
termios.tcsetattr(fd, termios.TCSANOW, saved)
with open(os.environ["AGENT_RECORD"], "ab") as out:
    out.write(data)
'''

# Test-only sitecustomize: pauses the fatal handler as it writes the leave sequence, so a second
# fatal signal lands inside the first. The product has no hook; this wraps termui._write.
_PAUSE_HOOK = r'''
import os
import time

import termui

_write = termui._write


def _paused_write(stream, text):
    if text == termui.LEAVE_SCREEN:
        open(os.environ["AGREP_TEST_PAUSED"], "w").close()
        time.sleep(3)
    _write(stream, text)


termui._write = _paused_write
'''


def _iso(seconds_ago: float) -> str:
    moment = datetime.fromtimestamp(time.time() - seconds_ago, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _live_store(home: Path) -> None:
    """Two Claude chats that wrote within the board's live window."""
    for index, (project, prompt, reply) in enumerate((
            ("webapp", "make the checkout tests deterministic",
             "Injecting a clock into the checkout tests."),
            ("billing", "add CSV export to the invoices table",
             "Added an Export button that streams invoice rows."))):
        session = f"{index + 1:08x}-1111-4111-8111-111111111111"
        cwd = f"/work/{project}"
        base = {"sessionId": session, "cwd": cwd}
        rows = [
            {**base, "type": "user", "userType": "external", "timestamp": _iso(20),
             "message": {"role": "user", "content": prompt}},
            {**base, "type": "assistant", "timestamp": _iso(10),
             "message": {"role": "assistant", "content": [{"type": "text", "text": reply}]}},
        ]
        directory = home / ".claude" / "projects" / f"-work-{project}"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{session}.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _set_size(fd: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))


def _masked(stty: str) -> str:
    # macOS sets PENDIN whenever the line discipline returns to canonical mode
    return re.sub(r"-?pendin", "", stty)


def _last_frame(data: bytes) -> str:
    """The last complete frame as text, escapes stripped and digits (ages,
    counts) folded, so two frames of one state compare equal."""
    text = data.decode("utf-8", "replace")
    end = text.rfind("\x1b[0J")
    start = text.rfind("\x1b[H", 0, end)
    if start < 0 or end < 0:
        return ""
    frame = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text[start + 3:end])
    return re.sub(r"\d+", "#", frame)


class _Run:
    """One CLI run under the job-control parent: what it wrote, what the parent saw."""

    def __init__(self, sandbox: Path, argv: list[str], *, foreground: bool = True,
                 ignore_hup: bool = False, split_stdout: bool = False,
                 next_argv: list[str] | None = None, env: dict | None = None) -> None:
        self.data = bytearray()
        self.out_data = bytearray()
        self.reports: list[list] = []
        self.consumed: set[int] = set()
        self.pending = b""
        self.reading = threading.Event()  # cleared: the terminal stops taking output
        self.reading.set()
        self.master, slave = pty.openpty()
        _set_size(slave)
        self.out_master = None
        out_slave = None
        if split_stdout:
            self.out_master, out_slave = pty.openpty()
            _set_size(out_slave)
        ctl_read, self.ctl = os.pipe()
        self.rep, rep_write = os.pipe()
        spec = {"ctl": ctl_read, "rep": rep_write, "stty": STTY, "argv": argv,
                "foreground": foreground, "ignore_hup": ignore_hup, "next": next_argv,
                "stdout_fd": out_slave, "env": {**_env(sandbox), **(env or {})},
                "cwd": str(sandbox)}
        self.shell = subprocess.Popen(
            [sys.executable, "-c", _SHELL, json.dumps(spec)],
            stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
            pass_fds=[fd for fd in (ctl_read, rep_write, out_slave) if fd is not None],
            preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0),
            env={"PATH": "/bin:/usr/bin", "LANG": "en_US.UTF-8"})
        for fd in (slave, ctl_read, rep_write, out_slave):
            if fd is not None:
                os.close(fd)
        self.threads = [threading.Thread(target=self._pump, args=(self.master, self.data),
                                         daemon=True)]
        if self.out_master is not None:
            self.threads.append(threading.Thread(
                target=self._pump, args=(self.out_master, self.out_data), daemon=True))
        for thread in self.threads:
            thread.start()

    def _pump(self, fd: int, sink: bytearray) -> None:
        while True:
            if not self.reading.wait(0.2):
                continue
            try:
                if not select.select([fd], [], [], 0.2)[0]:
                    continue
                chunk = os.read(fd, 65536)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            sink += chunk

    def send(self, data: bytes) -> None:
        os.write(self.master, data)

    def stall(self) -> None:
        """Stop taking the job's output, as a frozen ssh session does, and ask
        for repaints (j and k) until any terminal's output queue is full, so
        the job ends up blocked writing a frame. A blocked job reads no keys:
        the 120 sent stay queued, well under any input limit."""
        self.reading.clear()
        time.sleep(0.5)  # the pump finishes the read it was in
        for _ in range(60):
            self.send(b"jk")
            time.sleep(0.05)

    def command(self, text: str) -> None:
        os.write(self.ctl, (text + "\n").encode())

    def report(self, kind: str, timeout: float = STEP_S) -> list:
        """The parent's next report of `kind` not yet returned."""
        deadline = time.monotonic() + timeout
        while True:
            self._read_reports(0)
            for index, found in enumerate(self.reports):
                if found[0] == kind and index not in self.consumed:
                    self.consumed.add(index)
                    return found
            left = deadline - time.monotonic()
            if left <= 0 or self.rep is None:
                raise AssertionError(
                    f"no {kind!r} report within {timeout:.0f}s; reports {self.reports}; "
                    f"tail {bytes(self.data[-600:])!r}")
            self._read_reports(min(left, 0.2))

    def reported(self, kind: str) -> bool:
        self._read_reports(0)
        return any(found[0] == kind for found in self.reports)

    def _read_reports(self, timeout: float) -> None:
        if self.rep is None or not select.select([self.rep], [], [], timeout)[0]:
            return
        chunk = os.read(self.rep, 65536)
        if not chunk:
            os.close(self.rep)
            self.rep = None
            return
        self.pending += chunk
        *lines, self.pending = self.pending.split(b"\n")
        self.reports += [json.loads(line) for line in lines if line]

    def wait_until(self, check, timeout: float, what: str) -> None:
        deadline = time.monotonic() + timeout
        while not check():
            if time.monotonic() > deadline:
                raise AssertionError(f"{what} did not happen within {timeout:.0f}s; tail "
                                     f"{bytes((self.out_data or self.data)[-600:])!r}")
            time.sleep(0.05)

    def frames(self, sink: bytearray | None = None) -> int:
        """Frames drawn since the board last entered the alternate screen."""
        sink = self.data if sink is None else sink
        return sink.count(b"\x1b[0J", max(0, sink.rfind(b"\x1b[?1049h")))

    def close(self) -> None:
        # the job runs in its own process group: a board that never exited
        # must not outlive its test; a paused pump must get to its end too
        self.reading.set()
        with _suppress():
            self._read_reports(0)
        for found in self.reports:
            if found[0] == "started":
                with _suppress():
                    os.killpg(found[1], signal.SIGKILL)
        if self.shell.poll() is None:
            with _suppress():
                os.killpg(self.shell.pid, signal.SIGKILL)
        with _suppress():
            self.send(b"\x11")  # XON, should a frozen terminal hold a write
        for fd in (self.master, self.out_master, self.ctl, self.rep):
            if fd is not None:
                with _suppress():
                    os.close(fd)
        with _suppress():
            self.shell.wait(timeout=10)


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return True


def _env(sandbox: Path) -> dict:
    env = {
        # agent stores resolve under AGREP_HOME: one home holds them and agrep's own state
        "HOME": str(sandbox / "home"), "AGREP_HOME": str(sandbox / "home"),
        "AGREP_DATA_DIR": str(sandbox / "data"), "TMPDIR": str(sandbox / "tmp"),
        "PATH": f"{sandbox / 'bin'}:/bin", "LANG": "en_US.UTF-8", "TZ": "UTC",
        "TERM": "xterm-256color", "PYTHONPATH": f"{ROOT}:{ROOT / 'py'}",
        "AGREP_NO_DAEMON": "1", "AGREP_NO_RESIDENT": "1", "AGREP_NO_SEM_WORKER": "1",
        "AGREP_NO_FETCH": "1", "AGREP_CALLER_PUBLICATION_DIR": str(sandbox / "nocallers"),
        "AGENT_RECORD": str(sandbox / "agent-stdin.bin"),
    }
    if os.environ.get("AGREP_RS_BIN"):
        env["AGREP_RS_BIN"] = os.environ["AGREP_RS_BIN"]
    return env


@unittest.skipUnless(PTYS, "POSIX pseudo-terminals")
class _BoardTerminalCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="agrep-board-tty-")
        self.addCleanup(self._tmp.cleanup)
        self.sandbox = Path(self._tmp.name)
        for name in ("home", "data", "tmp", "bin", "nocallers"):
            (self.sandbox / name).mkdir()
        _live_store(self.sandbox / "home")
        # the only agents on PATH: fakes that record what reaches their stdin
        for agent in ("claude", "codex", "omp"):
            fake = self.sandbox / "bin" / agent
            fake.write_text(f"#!{sys.executable}\n{_RECORDER}", encoding="utf-8")
            fake.chmod(0o755)

    def run_board(self, command: str, **options) -> _Run:
        run = _Run(self.sandbox, [sys.executable, str(ROOT / "cli.py"), command], **options)
        self.addCleanup(run.close)
        return run

    def wait_first_frame(self, run: _Run, sink: bytearray | None = None) -> None:
        run.wait_until(lambda: run.frames(sink) >= 1, START_S, "the first frame")

    def assert_handed_back(self, run: _Run, before: str, after: str,
                           sink: bytearray | None = None) -> None:
        sink = run.data if sink is None else sink
        start = sink.rfind(b"\x1b[?1049h")
        self.assertGreater(start, -1, "the alternate screen was never entered")
        run.wait_until(
            lambda: sink.rfind(b"\x1b[?1049l", start) > start,
            5.0, "the main screen back")
        self.assertEqual(_masked(after), _masked(before), "stty -a changed")
        tail = bytes(sink[start:])
        self.assertGreater(tail.rfind(b"\x1b[?1049l"), 0, "the main screen never came back")
        self.assertGreater(tail.rfind(b"\x1b[?25h"), tail.rfind(b"\x1b[?25l"),
                           "the cursor stayed hidden")
        sgr = re.findall(rb"\x1b\[[0-9;]*m", tail)
        self.assertIn(sgr[-1] if sgr else b"\x1b[0m", (b"\x1b[0m", b"\x1b[m"),
                      "colours were left set")
        for noise in (b"Traceback", b"Exception ignored", b"agrep failed"):
            self.assertNotIn(noise, bytes(run.data), noise)

    def assert_restored_after_signal(self, run: _Run, before: str, after: str) -> None:
        """What a fatal signal must leave behind, also on a terminal that had
        stalled: the modes, a blocking terminal for the shell sharing it, and,
        once the terminal takes output again, the main screen."""
        self.assertEqual(_masked(after), _masked(before), "stty -a changed")
        self.assertFalse(run.report("nonblocking")[1], "the shared terminal was left non-blocking")
        run.reading.set()
        run.wait_until(
            lambda: b"\x1b[?1049l" in bytes(run.data[run.data.rfind(b"\x1b[?1049h"):]),
            STEP_S, "the main screen back")


class BoardExitPathTests(_BoardTerminalCase):
    def test_q_quits_and_hands_the_terminal_back(self) -> None:
        for command in COMMANDS:
            with self.subTest(command=command):
                run = self.run_board(command)
                before = run.report("before")[1]
                self.wait_first_frame(run)
                run.send(b"q")
                _kind, code, after = run.report("exit")
                self.assertEqual(code, 0)
                self.assert_handed_back(run, before, after)

    def test_sigterm_sighup_and_ctrl_backslash(self) -> None:
        for command in COMMANDS:
            for name, deliver in (("SIGTERM", "kill"), ("SIGHUP", "kill"),
                                  ("SIGQUIT", "ctrl-backslash")):
                with self.subTest(command=command, signal=name):
                    run = self.run_board(command)
                    before = run.report("before")[1]
                    self.wait_first_frame(run)
                    signum = getattr(signal, name)
                    if deliver == "kill":
                        run.command(f"kill:{signum}")
                    else:
                        run.send(b"\x1c")
                    _kind, code, after = run.report("exit")
                    # the board dies of the signal itself: the exit status still names it
                    self.assertEqual(code, -signum)
                    self.assert_handed_back(run, before, after)

    def test_a_signal_ends_a_board_whose_terminal_has_stalled(self) -> None:
        # a frozen ssh session: the terminal takes no more output and the board
        # blocks writing a frame; a signal must still end it, everything restored
        for name in ("SIGTERM", "SIGHUP", "SIGQUIT"):
            with self.subTest(signal=name):
                run = self.run_board("board")
                before = run.report("before")[1]
                self.wait_first_frame(run)
                run.stall()
                signum = getattr(signal, name)
                begun = time.monotonic()
                run.command(f"kill:{signum}")
                _kind, code, after = run.report("exit", 20)
                self.assertLess(time.monotonic() - begun, 10)
                self.assertEqual(code, -signum)
                self.assert_restored_after_signal(run, before, after)

    def test_a_signal_ends_a_leave_waiting_on_a_stalled_terminal(self) -> None:
        # kill -INT, not a typed Ctrl-C, which would also make the terminal flush the output
        # the leave is meant to wait on (noflsh off)
        run = self.run_board("board")
        before = run.report("before")[1]
        self.wait_first_frame(run)
        run.stall()
        run.command(f"kill:{signal.SIGINT}")
        time.sleep(1.5)
        self.assertFalse(run.reported("exit"), "the leave never waited on the terminal")
        run.command(f"kill:{signal.SIGTERM}")
        _kind, code, after = run.report("exit", 20)
        self.assertEqual(code, -signal.SIGTERM)
        self.assert_restored_after_signal(run, before, after)

    def test_a_second_fatal_signal_cannot_reach_inside_the_first(self) -> None:
        # SIGTERM, then SIGHUP from the closing terminal: the second lands while
        # the first handler is writing the leave sequence
        hook = self.sandbox / "hook"
        hook.mkdir()
        (hook / "sitecustomize.py").write_text(_PAUSE_HOOK, encoding="utf-8")
        paused = self.sandbox / "paused"
        run = self.run_board("board", env={
            "PYTHONPATH": f"{hook}:{ROOT}:{ROOT / 'py'}", "AGREP_TEST_PAUSED": str(paused)})
        before = run.report("before")[1]
        self.wait_first_frame(run)
        run.command(f"kill:{signal.SIGTERM}")
        run.wait_until(paused.exists, STEP_S, "the first handler's leave sequence")
        run.command(f"kill:{signal.SIGHUP}")
        _kind, code, after = run.report("exit", 30)
        # it dies of the signal handled first, the second turned away, and the
        # terminal the shell shares is still blocking
        self.assertEqual((code, run.report("nonblocking")[1]), (-signal.SIGTERM, False))
        self.assertEqual(_masked(after), _masked(before), "stty -a changed")
        run.wait_until(
            lambda: b"\x1b[?1049l" in bytes(run.data[run.data.rfind(b"\x1b[?1049h"):]),
            STEP_S, "the main screen back")

    def test_a_ctrl_c_storm_cannot_interrupt_the_restore(self) -> None:
        for command in COMMANDS:
            with self.subTest(command=command):
                run = self.run_board(command)
                before = run.report("before")[1]
                self.wait_first_frame(run)
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and not run.reported("exit"):
                    run.send(b"\x03" * 32)
                    time.sleep(0.002)
                _kind, code, after = run.report("exit")
                self.assertIn(code, (130, -signal.SIGINT))
                self.assert_handed_back(run, before, after)

    def test_a_ctrl_c_storm_on_a_slow_terminal_keeps_the_leave_sequence(self) -> None:
        # the leave sequence is still queued when the rest of the storm arrives; with NOFLSH off
        # again, each Ctrl-C would flush it and leave the shell on the alternate screen
        for command in COMMANDS:
            with self.subTest(command=command):
                run = self.run_board(command)
                before = run.report("before")[1]
                self.wait_first_frame(run)
                run.reading.clear()
                time.sleep(0.5)  # the pump finishes the read it was in
                for index in range(300):  # 1.5 s while nothing is taken
                    # Linux: one Ctrl-C ends the view; more would reach the line discipline after
                    # the hand-back (its tcdrain doesn't wait for a reader) and could drop the escapes
                    if sys.platform == "darwin" or index == 0:
                        run.send(b"\x03" * 32 if sys.platform == "darwin" else b"\x03")
                    time.sleep(0.005)
                time.sleep(0.2)  # macOS: the line discipline handles the last Ctrl-C while NOFLSH holds
                run.reading.set()
                # Only a macOS tcdrain waits for the terminal to read, so only there can the storm go
                # on past the hand-back; on Linux that drop is a documented limit of Screen.leave
                deadline = time.monotonic() + 2.0
                while (sys.platform == "darwin" and time.monotonic() < deadline
                       and not run.reported("exit")):
                    run.send(b"\x03" * 32)
                    time.sleep(0.002)
                _kind, code, after = run.report("exit")
                self.assertIn(code, (130, -signal.SIGINT))
                self.assert_handed_back(run, before, after)

    def test_ctrl_z_then_bg_then_fg(self) -> None:
        for command in COMMANDS:
            with self.subTest(command=command):
                run = self.run_board(command)
                before = run.report("before")[1]
                self.wait_first_frame(run)
                run.send(b"\x1a")
                _kind, signum, stopped = run.report("stopped")
                self.assertEqual(signum, signal.SIGTSTP)
                # stopped, the board has already handed the terminal back
                self.assert_handed_back(run, before, stopped)
                entered = run.data.count(b"\x1b[?1049h")
                # continued in the background, it stops again rather than take the terminal
                run.command("bg")
                _kind, signum, _stty = run.report("stopped")
                self.assertEqual(signum, signal.SIGTTOU)
                self.assertEqual(run.data.count(b"\x1b[?1049h"), entered)
                run.command("fg")
                run.wait_until(lambda: run.data.count(b"\x1b[?1049h") > entered
                               and run.frames() >= 1, STEP_S, "the repaint after fg")
                run.send(b"q")
                _kind, code, after = run.report("exit")
                self.assertEqual(code, 0)
                self.assert_handed_back(run, before, after)

    def test_started_with_ampersand_then_fg(self) -> None:
        for command in COMMANDS:
            with self.subTest(command=command):
                run = self.run_board(command, foreground=False)
                before = run.report("before")[1]
                _kind, signum, waiting = run.report("stopped", START_S)
                self.assertEqual(signum, signal.SIGTTOU)
                self.assertEqual(_masked(waiting), _masked(before))
                self.assertNotIn(b"\x1b[?1049h", bytes(run.data))
                run.command("fg")
                self.wait_first_frame(run)
                run.send(b"q")
                _kind, code, after = run.report("exit")
                self.assertEqual(code, 0)
                self.assert_handed_back(run, before, after)

    def test_ctrl_s_cannot_freeze_the_board(self) -> None:
        for command in COMMANDS:
            with self.subTest(command=command):
                run = self.run_board(command)
                before = run.report("before")[1]
                self.wait_first_frame(run)
                run.send(b"\x13")
                time.sleep(1.5)  # repaints keep coming: output is not held
                frames = run.frames()
                time.sleep(1.5)
                self.assertGreater(run.frames(), frames)
                run.send(b"q")
                _kind, code, after = run.report("exit", 15)
                self.assertEqual(code, 0)
                self.assert_handed_back(run, before, after)

    def test_the_end_of_input_quits(self) -> None:
        for command in COMMANDS:
            with self.subTest(command=command):
                # `trap '' HUP; agrep live`, then the terminal goes away: the board
                # reads the end of its input and quits instead of polling forever
                run = self.run_board(command, ignore_hup=True, split_stdout=True)
                run.report("before")
                self.wait_first_frame(run, run.out_data)
                os.close(run.master)
                run.master = None
                _kind, code, _after = run.report("exit", 20)
                self.assertEqual(code, 0)
                tail = bytes(run.out_data[run.out_data.rfind(b"\x1b[?1049h"):])
                self.assertIn(b"\x1b[?1049l", tail)
                self.assertIn(b"\x1b[?25h", tail)
                self.assertNotIn(b"Traceback", bytes(run.out_data))

    def test_a_dead_output_terminal_exits_2_without_noise(self) -> None:
        for command in COMMANDS:
            with self.subTest(command=command):
                run = self.run_board(command, split_stdout=True)
                before = run.report("before")[1]
                self.wait_first_frame(run, run.out_data)
                os.close(run.out_master)
                run.out_master = None
                _kind, code, after = run.report("exit", 30)
                self.assertEqual(code, 2)
                self.assertEqual(_masked(after), _masked(before))
                self.assertIn(b"agrep failed", bytes(run.data))
                for noise in (b"Exception ignored", b"Traceback"):
                    self.assertNotIn(noise, bytes(run.data))


class BoardPasteTests(_BoardTerminalCase):
    def paste(self, run: _Run, text: bytes) -> bytes:
        """`text` as a terminal sends a paste: framed once the program asked for it."""
        if run.data.rfind(b"\x1b[?2004h") > run.data.rfind(b"\x1b[?2004l"):
            return b"\x1b[200~" + text + b"\x1b[201~"
        return text

    def settings(self) -> bytes:
        """settings.json, where the board's c and a keys persist the view."""
        path = self.sandbox / "data" / "settings.json"
        return path.read_bytes() if path.exists() else b""

    def test_a_paste_runs_no_board_key_and_reaches_no_later_program(self) -> None:
        for command in COMMANDS:
            with self.subTest(command=command):
                record = self.sandbox / "agent-stdin.bin"
                record.unlink(missing_ok=True)
                run = self.run_board(command, next_argv=["claude", "--resume", "x"])
                before = run.report("before")[1]
                run.wait_until(lambda: "webapp" in _last_frame(bytes(run.data)),
                               START_S, "the live chats on the board")
                time.sleep(1.0)
                frame, settings = _last_frame(bytes(run.data)), self.settings()
                run.send(self.paste(run, PASTE))
                time.sleep(2.5)
                self.assertFalse(run.reported("exit"), "a pasted q quit the board")
                self.assertEqual(_last_frame(bytes(run.data)), frame)
                self.assertEqual(self.settings(), settings)
                run.send(b"q")
                _kind, code, after = run.report("exit")
                self.assertEqual(code, 0)
                self.assert_handed_back(run, before, after)
                self.assertEqual(run.report("next")[1], 0)
                self.assertEqual(record.read_bytes(), b"")

    def test_input_still_unread_at_quit_never_reaches_the_next_program(self) -> None:
        for command in COMMANDS:
            with self.subTest(command=command):
                record = self.sandbox / "agent-stdin.bin"
                record.unlink(missing_ok=True)
                run = self.run_board(command, next_argv=["claude", "--resume", "x"])
                before = run.report("before")[1]
                self.wait_first_frame(run)
                run.send(b"q" + self.paste(run, PASTE))
                _kind, code, after = run.report("exit")
                self.assertEqual(code, 0)
                self.assert_handed_back(run, before, after)
                self.assertEqual(run.report("next")[1], 0)
                self.assertEqual(record.read_bytes(), b"")


if __name__ == "__main__":
    unittest.main()
