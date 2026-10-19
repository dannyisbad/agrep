"""Install, setup, teach, remove and resume surfaces: regressions for the
0.4.3 lifecycle hunt. Every test here failed on the pre-fix tree."""

from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _test_support import isolate_data_dir

isolate_data_dir()
import cli  # noqa: E402
import common  # noqa: E402
import doctor  # noqa: E402
import hookinstall  # noqa: E402
import indexd_runtime  # noqa: E402
import removal_fence  # noqa: E402
import semantic  # noqa: E402
import semworker  # noqa: E402
import teach  # noqa: E402
from hookless import capture, native  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PY_DIR = ROOT / "py"
POSIX = sys.platform != "win32"


class _Sandbox(unittest.TestCase):
    """teach/hookinstall paths rooted in a temporary home and data dir."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.data = self.root / "data"
        self.home.mkdir()
        self.data.mkdir()
        self.saved = {name: getattr(teach, name) for name in (
            "HOME", "REPO", "STATE_PATH", "MD_TARGETS", "SKILL_TARGETS")}
        self.saved_data = teach.common.DATA_DIR
        teach.HOME = self.home
        teach.REPO = self.root / "repo"
        teach.REPO.mkdir()
        (teach.REPO / "cli.py").write_text("# cli\n", encoding="utf-8")
        teach.common.DATA_DIR = self.data
        teach.STATE_PATH = self.data / "teach.json"
        teach.MD_TARGETS = []
        teach.SKILL_TARGETS = []
        teach._ADDED_SEPARATORS.clear()
        teach._CREATED_DIRS.clear()
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("AGREP_INSTALL_ANCHOR", None)
        os.environ.pop("CODEX_HOME", None)

    def tearDown(self) -> None:
        self.env.stop()
        for name, value in self.saved.items():
            setattr(teach, name, value)
        teach.common.DATA_DIR = self.saved_data
        self.temp.cleanup()

    def _agent(self, name: str, filename: str = "AGENTS.md") -> Path:
        proof = self.home / f".{name}"
        proof.mkdir(parents=True, exist_ok=True)
        target = proof / filename
        teach.MD_TARGETS.append((name, proof, target))
        return target

    @contextlib.contextmanager
    def _removal(self):
        fence = mock.Mock()
        with mock.patch.object(
                teach.removal_fence, "acquire_background_removal_fence",
                return_value=fence), \
                mock.patch.object(
                    teach.removal_fence, "finish_background_removal_fence",
                    return_value=True), \
                mock.patch.object(teach, "_stop_daemons", return_value=True), \
                mock.patch.object(teach, "_sentinel_remove", return_value=True), \
                mock.patch.object(hookinstall, "remove", return_value=0), \
                contextlib.redirect_stdout(io.StringIO()):
            yield


class SetupContinuesPastTeachFailures(_Sandbox):
    """[0] one unwritable instruction target must not cost the index."""

    def _setup(self, *, archive: bool = False) -> tuple[int, str, mock.Mock, mock.Mock]:
        index = mock.Mock(return_value=0)
        archive_step = mock.Mock()
        output = io.StringIO()
        args = SimpleNamespace(
            rest=[], yes=True, no_teach=False, no_hook=True, no_semantic=True,
            archive=archive, no_archive=not archive)
        with mock.patch.object(cli.common, "DATA_DIR", self.data), \
                mock.patch("doctor.main", return_value=0), \
                mock.patch.object(teach, "_sentinel_install", return_value=True), \
                mock.patch.object(teach, "enrollment_active", return_value=False), \
                mock.patch.object(cli, "_setup_index_state", return_value=(None, False)), \
                mock.patch.object(cli, "_preserve_invalid_corpus_leaf", return_value=True), \
                mock.patch.object(cli, "cmd_index", index), \
                mock.patch.object(cli, "_setup_archive", archive_step), \
                mock.patch.object(cli, "_core_evidence_path", return_value="proof"), \
                contextlib.redirect_stdout(output):
            rc = cli.cmd_setup(args)
        return rc, output.getvalue(), index, archive_step

    def test_undecodable_target_reports_fix_and_setup_still_indexes(self) -> None:
        bad = self._agent("claude", "CLAUDE.md")
        good = self._agent("gemini", "GEMINI.md")
        bad.write_bytes(b"Caf\xe9 rules\n")
        rc, rendered, index, archive_step = self._setup(archive=True)
        self.assertEqual(rc, 1)
        index.assert_called_once()
        archive_step.assert_called_once_with(True, assume_yes=True)
        self.assertIn(teach.MARK_PREFIX, good.read_text(encoding="utf-8"))
        self.assertIn("not valid UTF-8", rendered)
        self.assertIn("1 instruction file(s) not written (claude)", rendered)
        self.assertIn("step 2 (agent instructions) did not complete", rendered)
        self.assertNotIn("setup complete", rendered)

    def test_stray_marker_names_file_reason_and_fix(self) -> None:
        bad = self._agent("codex")
        bad.write_text(f"rules\n{teach.MARK_PREFIX} v3 -->\n", encoding="utf-8")
        rc, rendered, index, _ = self._setup()
        self.assertEqual(rc, 1)
        index.assert_called_once()
        self.assertIn(str(bad), rendered)
        self.assertIn("delete the stray", rendered)
        self.assertIn("re-run `agrep setup`", rendered)

    def test_clean_run_still_completes(self) -> None:
        self._agent("gemini", "GEMINI.md")
        rc, rendered, index, _ = self._setup()
        self.assertEqual(rc, 0)
        index.assert_called_once()
        self.assertIn("setup complete", rendered)


class FencedIndexDeclines(_Sandbox):
    """[W16] a fenced writer is a decline with its cause on every explicit
    path, never a crash line, and setup says so before launching anything."""

    FENCE = indexd_runtime.DerivedMutationInfo(
        "unavailable", None,
        "derived-store ownership record .derived-owner.json is malformed")
    NEWER = indexd_runtime.newer_anchor_reason(2, 1)

    def test_a_fenced_full_index_declines_with_its_cause(self) -> None:
        stderr = io.StringIO()
        with mock.patch.object(cli, "_ensure_binary", return_value=True), \
                mock.patch.object(
                    indexd_runtime, "retire_displaceable_indexd_owner",
                    return_value=False), \
                mock.patch.object(
                    indexd_runtime, "explicit_rust_writer_env",
                    side_effect=indexd_runtime.DerivedWriteFenced(self.FENCE)), \
                mock.patch.object(
                    cli.subprocess, "run",
                    side_effect=AssertionError("ingest launched")), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(stderr):
            rc = cli.cmd_index(SimpleNamespace(full=True, rest=[]))
        self.assertEqual(rc, 1)
        self.assertIn(f"indexing declined; {self.FENCE.reason}", stderr.getvalue())
        self.assertNotIn("DerivedWriteFenced", stderr.getvalue())

    @contextlib.contextmanager
    def _fenced_setup(self, *, index_state, newer: bool = False):
        binary = self.root / "agrep-rs"
        binary.write_bytes(b"exact writer binary")
        self._agent("claude", "CLAUDE.md")  # a consentable write exists
        untouched = SimpleNamespace(
            doctor=mock.Mock(return_value=0),
            enrol=mock.Mock(return_value=0),
            hooks=mock.Mock(return_value=0),
            sentinel=mock.Mock(return_value=True),
            index=mock.Mock(side_effect=AssertionError("index launched despite the fence")),
            archive=mock.Mock(),
            semantic=mock.Mock(side_effect=AssertionError("semantic started under the fence")),
            prompt=mock.Mock(side_effect=AssertionError("consent asked under the fence")),
        )
        with mock.patch.object(cli.common, "DATA_DIR", self.data), \
                mock.patch("doctor.main", untouched.doctor), \
                mock.patch.object(cli.common, "ingest_bin", return_value=binary), \
                mock.patch.object(
                    indexd_runtime, "newer_anchor_fence",
                    return_value=self.NEWER if newer else None), \
                mock.patch.object(
                    indexd_runtime, "assert_python_runtime_unchanged",
                    return_value=None), \
                mock.patch.object(
                    indexd_runtime, "derived_writer_build_id",
                    return_value="a" * 20), \
                mock.patch.object(
                    indexd_runtime, "derived_writer_mutation_info",
                    return_value=self.FENCE), \
                mock.patch.object(cli, "_setup_index_state", return_value=index_state), \
                mock.patch.object(cli, "cmd_index", untouched.index), \
                mock.patch.object(cli, "_setup_archive", untouched.archive), \
                mock.patch.object(cli, "_setup_start_semantic", untouched.semantic), \
                mock.patch.object(teach, "teach", untouched.enrol), \
                mock.patch.object(teach, "refresh_sentinel", untouched.sentinel), \
                mock.patch.object(hookinstall, "install", untouched.hooks), \
                mock.patch("builtins.input", untouched.prompt):
            yield untouched

    @staticmethod
    def _setup(*, yes: bool = True, archive: bool = False,
               no_semantic: bool = False) -> tuple[int, str]:
        output = io.StringIO()
        args = SimpleNamespace(
            rest=[], yes=yes, no_teach=False, no_hook=False,
            no_semantic=no_semantic, archive=archive, no_archive=not archive)
        with contextlib.redirect_stdout(output):
            rc = cli.cmd_setup(args)
        return rc, output.getvalue()

    def test_setup_reports_a_declined_index_before_launching_one(self) -> None:
        # a fence that is not a newer owner (here a malformed anchor): the
        # steps run and step 4 declines with the cause
        with self._fenced_setup(index_state=(None, False)) as steps:
            rc, rendered = self._setup()
        self.assertEqual(rc, 1)
        self.assertIn(f"indexing declined; {self.FENCE.reason}", rendered)
        self.assertIn("setup incomplete: the index build was declined", rendered)
        self.assertNotIn("the index build failed", rendered)
        steps.doctor.assert_called_once()
        steps.enrol.assert_called_once()
        steps.index.assert_not_called()
        steps.archive.assert_not_called()

    def test_setup_reports_a_built_index_it_cannot_refresh_as_read_only(
            self) -> None:
        # [W16 round 4] a corpus under a fence that is not a newer owner:
        # setup still enrols (searches work) but never claims freshness
        state = ({"messages": 23, "sessions": 6}, False)
        with self._fenced_setup(index_state=state) as steps:
            rc, rendered = self._setup(no_semantic=True)
        self.assertEqual(rc, 0, rendered)
        self.assertIn("already built - 23 messages across 6 sessions; "
                      f"read-only for this agrep: {self.FENCE.reason}", rendered)
        self.assertNotIn("searches keep it fresh", rendered)
        self.assertIn("setup complete", rendered)
        steps.enrol.assert_called_once()
        steps.archive.assert_called_once()
        steps.index.assert_not_called()

    def test_setup_reports_a_hold_with_its_remedy_never_a_fresh_index(self) -> None:
        # [W34] a record holding publication back fails every pass: setup's receipt says so,
        # with the hold's remedy, instead of "searches keep it fresh" and "setup complete"
        (self.data / ".source-health.json").write_text(json.dumps({
            "code": "source-unreadable",
            "issues": [{"agent": "claude", "path": str(self.root / "held.jsonl"),
                        "kind": "permission-denied", "reason": "denied",
                        "blocks_publication": True}]}), encoding="utf-8")
        state = ({"messages": 23, "sessions": 6}, False)
        import source_health
        with self._fenced_setup(index_state=state) as steps, \
                mock.patch.object(cli, "_explicit_index_refusal", return_value=None), \
                mock.patch.object(source_health, "rebuild_cost", return_value="; COST"):
            rc, rendered = self._setup(no_semantic=True)
        self.assertEqual(rc, 0, rendered)
        self.assertIn("already built - 23 messages across 6 sessions, but new chats are not "
                      "being indexed: fix or remove it in claude, or run `", rendered)
        # [W39] the rebuild's cost is said once, in the receipt, not in the progress line too.
        self.assertEqual(rendered.count("COST"), 1, rendered)
        self.assertIn("COST", rendered.rsplit("setup finished", 1)[1])
        self.assertIn("reindex --full` to rebuild without it", rendered)
        self.assertIn("setup finished", rendered)
        self.assertIn("but new chats are not being indexed", rendered.rsplit("setup finished", 1)[1])
        self.assertNotIn("searches keep it fresh", rendered)
        self.assertNotIn("setup complete", rendered)
        steps.index.assert_not_called()

    def _assert_nothing_changed(self, steps, rendered: str) -> None:
        for name in ("doctor", "enrol", "hooks", "sentinel", "index",
                     "archive", "semantic", "prompt"):
            getattr(steps, name).assert_not_called()
        self.assertIn(self.NEWER, rendered)
        self.assertIn("agent instructions, recovery integrations, the "
                      "uninstall sentinel and the archive choice stay as "
                      "they are", rendered)
        self.assertNotIn("=== setup 1/", rendered)
        self.assertNotIn("setup complete", rendered)
        self.assertNotIn("headless setup", rendered)
        self.assertNotIn("write these instructions?", rendered)

    def test_setup_changes_nothing_under_a_newer_owner(self) -> None:
        # [W16 round 3] the newer build enrolled the agents and keeps the
        # index: blocks, hooks, launcher and sentinel stay as they are, no
        # daemon starts, no capture is enabled; searches still work
        state = ({"messages": 23, "sessions": 6}, False)
        with self._fenced_setup(index_state=state, newer=True) as steps:
            rc, rendered = self._setup(yes=False, archive=True)
        self.assertEqual(rc, 0, rendered)
        self.assertIn("nothing to change: a newer agrep owns this data dir; "
                      "searches work read-only (23 messages across 6 "
                      "sessions).", rendered)
        self._assert_nothing_changed(steps, rendered)

    def test_setup_declines_by_its_own_code_when_nothing_is_searchable(
            self) -> None:
        # [W16 round 3] no corpus and a newer owner: exit 3 is the
        # documented "declined, nothing changed" an installer keys on
        with self._fenced_setup(index_state=(None, False), newer=True) as steps:
            rc, rendered = self._setup(archive=True)
        self.assertEqual(rc, cli.SETUP_DECLINED_RC)
        self.assertEqual(rc, 3)
        self.assertIn("nothing to change: a newer agrep owns this data dir "
                      "and nothing is searchable here yet; this agrep cannot "
                      "build it.", rendered)
        self.assertNotIn("setup incomplete", rendered)
        self._assert_nothing_changed(steps, rendered)


class ReconcileNeverFabricatesAHome(_Sandbox):
    """[1] a deleted agent home stays deleted; the enrollment goes dormant."""

    def test_missing_proof_dir_is_skipped_and_repairs_once_it_returns(self) -> None:
        target = self._agent("gemini", "GEMINI.md")
        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(teach, "_sentinel_install", return_value=True):
            self.assertEqual(teach._install(), 0)
        shutil.rmtree(target.parent)
        self.assertEqual(teach.reconcile(), [])
        self.assertFalse(target.parent.exists())
        self.assertEqual(teach.current_reconcile_health()["state"], "clean")
        self.assertTrue(teach.enrollment_active())
        target.parent.mkdir()
        self.assertEqual(teach.reconcile(), [str(target)])
        self.assertIn(teach.MARK_PREFIX, target.read_text(encoding="utf-8"))


_STUB = r'''
import json, os, signal, sys, time
got = {"winch": False, "cont": False}
signal.signal(signal.SIGWINCH, lambda *_: got.__setitem__("winch", True))
signal.signal(signal.SIGCONT, lambda *_: got.__setitem__("cont", True))
def report(tag, **fields):
    sys.stdout.write("STUB " + json.dumps({"tag": tag, **fields}) + "\n")
    sys.stdout.flush()
def foreground():
    try:
        return os.tcgetpgrp(0)
    except OSError:
        return None
try:
    os.close(os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY))
    tty_ok = True
except OSError:
    tty_ok = False
report("ready", pgrp=os.getpgrp(), foreground=foreground(), tty=tty_ok,
       parent_pgrp=os.getpgid(os.getppid()), sid=os.getsid(0),
       parent_sid=os.getsid(os.getppid()))
mode = os.environ.get("STUB_MODE")
if mode == "quick":
    raise SystemExit(0)
if mode == "sleep":
    time.sleep(1.5)
    raise SystemExit(0)
deadline = time.monotonic() + 20
while not got["winch"] and time.monotonic() < deadline:
    time.sleep(0.02)
report("winch", got=got["winch"])
if mode == "stop":
    deadline = time.monotonic() + 20
    while not got["cont"] and time.monotonic() < deadline:
        time.sleep(0.02)
    report("cont", got=got["cont"], foreground=foreground(), pgrp=os.getpgrp())
'''

_DRIVER = r'''
import json, os, sys
sys.path.insert(0, %(pydir)r)
from hookless import native
result = native.run_owned_process(
    [sys.executable, %(stub)r], cwd=os.getcwd(), relay_signals=True, foreground=True)
try:
    restored = os.tcgetpgrp(0) == os.getpgrp()
except OSError:
    restored = None
sys.stdout.write("DRIVER " + json.dumps(
    {"rc": result.returncode, "foreground_restored": restored}) + "\n")
sys.stdout.flush()
'''

# A job-control shell in miniature: the driver runs as a foreground job in its own
# group, so its stop is a reportable event (a pty.fork child itself is an orphaned
# group whose stop signals the kernel discards).
_SHELL = r'''
import json, os, signal, sys, time
signal.signal(signal.SIGTTOU, signal.SIG_IGN)
def say(tag, **fields):
    sys.stdout.write("SHELL " + json.dumps({"tag": tag, **fields}) + "\n")
    sys.stdout.flush()
pid = os.fork()
if pid == 0:
    os.setpgid(0, 0)
    os.tcsetpgrp(0, os.getpid())
    signal.signal(signal.SIGTTOU, signal.SIG_DFL)
    os.execv(sys.executable, [sys.executable, "-c", %(driver)r])
try:
    os.setpgid(pid, pid)
except OSError:
    pass
os.tcsetpgrp(0, pid)
while True:
    _, status = os.waitpid(pid, os.WUNTRACED)
    if not os.WIFSTOPPED(status):
        break
    say("stopped", signal=os.WSTOPSIG(status), foreground=os.tcgetpgrp(0), job=pid)
    os.tcsetpgrp(0, os.getpgrp())
    time.sleep(0.2)
    os.tcsetpgrp(0, pid)
    os.killpg(pid, signal.SIGCONT)
    say("fg")
before = os.tcgetpgrp(0)
os.tcsetpgrp(0, os.getpgrp())
say("exit", rc=os.waitstatus_to_exitcode(status), foreground=before, job=pid,
    foreground_is_shell=os.tcgetpgrp(0) == os.getpgrp())
'''

# The same shell running `driver | reader` as one foreground job: both stages in
# one process group, the reader showing what arrives on the pipe while it waits
# for a key from /dev/tty, like a pager.
_PIPELINE = r'''
import json, os, signal, sys
signal.signal(signal.SIGTTOU, signal.SIG_IGN)
def say(tag, **fields):
    sys.stdout.write("SHELL " + json.dumps({"tag": tag, **fields}) + "\n")
    sys.stdout.flush()
reader = r"""
import json, os, sys, threading
def forward():
    for line in sys.stdin:
        sys.stdout.write(line)
        sys.stdout.flush()
pump = threading.Thread(target=forward, daemon=True)
pump.start()
with open("/dev/tty") as tty:
    line = tty.readline().rstrip("\n")
sys.stdout.write("READER " + json.dumps({"tag": "got", "line": line}) + "\n")
sys.stdout.flush()
pump.join()
"""
r, w = os.pipe()
first = os.fork()
if first == 0:
    os.setpgid(0, 0)
    os.tcsetpgrp(0, os.getpid())
    signal.signal(signal.SIGTTOU, signal.SIG_DFL)
    os.dup2(w, 1); os.close(r); os.close(w)
    os.execv(sys.executable, [sys.executable, "-c", %(driver)r])
try:
    os.setpgid(first, first)
except OSError:
    pass
second = os.fork()
if second == 0:
    os.setpgid(0, first)
    signal.signal(signal.SIGTTOU, signal.SIG_DFL)
    os.dup2(r, 0); os.close(r); os.close(w)
    os.execv(sys.executable, [sys.executable, "-c", reader])
try:
    os.setpgid(second, first)
except OSError:
    pass
os.close(r); os.close(w)
os.tcsetpgrp(0, first)
say("started", job=first)
for _ in range(2):
    pid, status = os.waitpid(-first, os.WUNTRACED)
    say("stopped" if os.WIFSTOPPED(status) else "exited", pid=pid,
        rc=None if os.WIFSTOPPED(status) else os.waitstatus_to_exitcode(status))
    if os.WIFSTOPPED(status):
        break
os.tcsetpgrp(0, os.getpgrp())
say("done", foreground_is_shell=os.tcgetpgrp(0) == os.getpgrp())
'''

_MARKERS = ("STUB ", "DRIVER ", "SHELL ", "READER ")


class _PtySession:
    """A pty whose session leader runs `program`; every read keeps the master
    drained (an exiting session leader blocks until its output is consumed)
    and every wait has a deadline, after which the whole tree is killed."""

    def __init__(self, case: unittest.TestCase, program: str, env: dict[str, str]) -> None:
        import pty
        self.case = case
        self.lines: list[str] = []
        self.pending = ""
        self.groups: set[int] = set()
        self.status: int | None = None
        self.pid, self.master = pty.fork()
        if self.pid == 0:  # pragma: no cover - child side
            os.environ.update(env)
            os.execv(sys.executable, [sys.executable, "-c", program])
        self.groups.add(self.pid)
        case.addCleanup(self.close)

    def _pump(self, timeout: float) -> bool:
        import select
        try:
            ready, _, _ = select.select([self.master], [], [], timeout)
        except (OSError, ValueError):
            return False
        if not ready:
            return True
        try:
            chunk = os.read(self.master, 4096)
        except OSError:
            return False
        if not chunk:
            return False
        self.pending += chunk.decode("utf-8", "replace")
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            # the pty echoes what the test types (^Z) ahead of the next report
            starts = [line.find(m) for m in _MARKERS if m in line]
            self.lines.append(line[min(starts):] if starts else line.strip("\r"))
        return True

    def _reap(self) -> None:
        if self.status is None:
            try:
                waited, status = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                waited, status = self.pid, 0
            if waited == self.pid:
                self.status = status

    def expect(self, prefix: str, tag: str, deadline_s: float = 30.0) -> dict:
        import time
        deadline = time.monotonic() + deadline_s
        while True:
            for line in list(self.lines):
                if not line.startswith(prefix):
                    continue
                self.lines.remove(line)
                payload = json.loads(line[len(prefix):])
                for key in ("pgrp", "parent_pgrp", "job"):
                    if isinstance(payload.get(key), int):
                        self.groups.add(payload[key])
                if payload.get("tag", tag) == tag:
                    return payload
                self.case.fail(f"expected {prefix}{tag}, got {line!r}")
            if time.monotonic() >= deadline:
                self.case.fail(f"deadline waiting for {prefix}{tag}; read so far: {self.lines!r}")
            self._reap()
            if not self._pump(0.1):
                if self.status is not None:
                    self.case.fail(
                        f"session ended (status {self.status}) before {prefix}{tag}; "
                        f"read so far: {self.lines!r}")
                time.sleep(0.02)

    def wait(self, deadline_s: float = 30.0) -> int:
        import time
        deadline = time.monotonic() + deadline_s
        while self.status is None and time.monotonic() < deadline:
            if not self._pump(0.1):
                time.sleep(0.02)
            self._reap()
        if self.status is None:
            self.case.fail(f"session leader did not exit; read so far: {self.lines!r}")
        return self.status

    def close(self) -> None:
        import signal
        import time
        self._reap()
        if self.status is None:
            for group in self.groups:
                try:
                    os.killpg(group, signal.SIGKILL)
                except OSError:
                    pass
        try:
            os.close(self.master)
        except OSError:
            pass
        deadline = time.monotonic() + 5
        while self.status is None and time.monotonic() < deadline:
            self._reap()
            time.sleep(0.02)


@unittest.skipUnless(POSIX, "POSIX job control")
class OwnedAgentKeepsTheTerminal(unittest.TestCase):
    """[2] resume/run agents stay on the controlling terminal: own group in the
    caller's session, foreground, SIGWINCH, /dev/tty; Ctrl-Z stops the job the
    way a shell expects; the foreground returns to the caller afterwards."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.stub = Path(self.temp.name) / "stub.py"
        self.stub.write_text(_STUB, encoding="utf-8")
        self.addCleanup(self.temp.cleanup)
        self.driver = _DRIVER % {"pydir": str(PY_DIR), "stub": str(self.stub)}

    def _resize(self, session: _PtySession) -> None:
        import fcntl
        import struct
        import termios
        fcntl.ioctl(session.master, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))

    def _check_ready(self, ready: dict) -> None:
        self.assertTrue(ready["tty"], "agent could not open /dev/tty")
        self.assertEqual(ready["foreground"], ready["pgrp"],
                         "agent group is not the terminal foreground")
        self.assertNotEqual(ready["pgrp"], ready["parent_pgrp"],
                            "agent must own its process group")
        self.assertEqual(ready["sid"], ready["parent_sid"],
                         "agent must stay in the caller's session")

    def test_agent_is_foreground_gets_sigwinch_and_terminal_is_restored(self) -> None:
        session = _PtySession(self, self.driver, {})
        self._check_ready(session.expect("STUB ", "ready"))
        self._resize(session)
        self.assertTrue(session.expect("STUB ", "winch")["got"],
                        "agent never received SIGWINCH")
        done = session.expect("DRIVER ", "")
        self.assertEqual(done["rc"], 0)
        self.assertTrue(done["foreground_restored"],
                        "terminal foreground was not given back to the caller")
        self.assertEqual(os.waitstatus_to_exitcode(session.wait()), 0)

    def test_ctrl_z_stops_the_job_and_fg_resumes_the_agent(self) -> None:
        import signal
        shell = _SHELL % {"driver": self.driver}
        session = _PtySession(self, shell, {"STUB_MODE": "stop"})
        self._check_ready(session.expect("STUB ", "ready"))
        self._resize(session)
        self.assertTrue(session.expect("STUB ", "winch")["got"])
        os.write(session.master, b"\x1a")  # ^Z: SIGTSTP to the foreground group
        stopped = session.expect("SHELL ", "stopped")
        self.assertEqual(stopped["signal"], int(signal.SIGTSTP))
        self.assertEqual(stopped["foreground"], stopped["job"],
                         "wrapper stopped without taking the terminal back from the agent")
        session.expect("SHELL ", "fg")
        cont = session.expect("STUB ", "cont")
        self.assertTrue(cont["got"], "agent was not continued after fg")
        self.assertEqual(cont["foreground"], cont["pgrp"],
                         "terminal was not handed back to the agent after fg")
        done = session.expect("DRIVER ", "")
        self.assertEqual(done["rc"], 0)
        self.assertTrue(done["foreground_restored"])
        finished = session.expect("SHELL ", "exit")
        self.assertEqual(finished["rc"], 0)
        self.assertEqual(finished["foreground"], finished["job"],
                         "wrapper exited leaving the terminal with a dead group")
        self.assertTrue(finished["foreground_is_shell"])
        self.assertEqual(os.waitstatus_to_exitcode(session.wait()), 0)

    def test_pipeline_partner_reading_the_tty_is_not_stopped(self) -> None:
        """`agrep run x | less`: the agent is one stage of the job; the terminal
        stays with the whole job, so the pager's /dev/tty read goes through
        while the agent is still running."""
        shell = _PIPELINE % {"driver": self.driver}
        session = _PtySession(self, shell, {"STUB_MODE": "sleep"})
        started = session.expect("SHELL ", "started")
        self.assertIsInstance(started["job"], int)
        # a pipeline stage runs detached: the terminal belongs to the whole job
        self.assertFalse(session.expect("STUB ", "ready")["tty"])
        os.write(session.master, b"q\n")  # a keypress while the agent is running
        self.assertEqual(session.expect("READER ", "got")["line"], "q")
        for _ in range(2):
            self.assertEqual(session.expect("SHELL ", "exited")["rc"], 0)
        self.assertTrue(session.expect("SHELL ", "done")["foreground_is_shell"])
        self.assertEqual(os.waitstatus_to_exitcode(session.wait()), 0)

    def test_without_a_controlling_terminal_the_launch_still_works(self) -> None:
        env = {**os.environ, "STUB_MODE": "quick"}
        completed = subprocess.run(
            [sys.executable, "-c", self.driver], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, env=env, start_new_session=True,
            timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        rows = [json.loads(line.split(" ", 1)[1])
                for line in completed.stdout.splitlines() if line.startswith(("STUB ", "DRIVER "))]
        ready, done = rows[0], rows[-1]
        self.assertFalse(ready["tty"])
        self.assertNotEqual(ready["pgrp"], ready["parent_pgrp"])
        # piped stdio is not an interactive run: the detached launch of old
        self.assertNotEqual(ready["sid"], ready["parent_sid"])
        self.assertEqual(done["rc"], 0)


class ClaudeResumeAcceptsItsOwnLink(unittest.TestCase):
    """[3] the second resume of a dead-cwd session finds agrep's own link."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.projects = self.home / ".claude" / "projects"
        self.session = "86273555-1c49-41ad-838f-47eede8db9cf"
        source_dir = self.projects / "-home-u-proj"
        source_dir.mkdir(parents=True)
        (source_dir / f"{self.session}.jsonl").write_text("{}\n", encoding="utf-8")
        self.cwd = self.home / "work"
        self.cwd.mkdir()

    def _link(self) -> None:
        with mock.patch.object(native, "HOME", str(self.home)), \
                mock.patch.object(native, "store_root",
                                  return_value=str(self.projects)), \
                contextlib.redirect_stderr(io.StringIO()):
            native._claude_link_session(self.session, str(self.cwd))

    @unittest.skipIf(os.name == "nt", "symlink fixture is not portable on Windows")
    def test_second_resume_reuses_the_link_but_refuses_a_foreign_one(self) -> None:
        self._link()
        dst = self.projects / native._claude_slug(str(self.cwd)) / f"{self.session}.jsonl"
        self.assertTrue(dst.is_symlink())
        self._link()  # the hunt's failing second resume
        self.assertTrue(dst.is_symlink())
        dst.unlink()
        dst.symlink_to(self.home / "elsewhere.jsonl")
        with self.assertRaisesRegex(OSError, "unsafe Claude resume destination"):
            self._link()


class RunLaunchesTheForkTheUserNamed(unittest.TestCase):
    """[4] `agrep run pi` runs pi; a missing CLI is 'not found on PATH' (127)."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bin = Path(self.temp.name) / "bin"
        self.bin.mkdir()

    def _stub(self, name: str) -> Path:
        path = self.bin / name
        path.write_text(f"#!/bin/sh\necho I am {name}\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_new_session_exe_prefers_the_typed_spelling(self) -> None:
        pi, omp = self._stub("pi"), self._stub("omp")
        with mock.patch.dict(os.environ, {"PATH": str(self.bin)}):
            self.assertEqual(native.new_session_exe("pi", "pi"), ("pi", str(pi)))
            self.assertEqual(native.new_session_exe("pi", "omp"), ("omp", str(omp)))
            pi.unlink()
            self.assertEqual(native.new_session_exe("pi", "pi"), ("omp", str(omp)))
            omp.unlink()
            self.assertEqual(native.new_session_exe("pi", "pi"), ("pi", None))

    def test_missing_cli_reports_not_found_with_127(self) -> None:
        captured = io.StringIO()
        with mock.patch.dict(os.environ, {"PATH": str(self.bin)}), \
                contextlib.redirect_stderr(captured):
            self.assertEqual(capture.run_captured("pi", []), 127)
        self.assertIn("not found on PATH", captured.getvalue())
        self.assertNotIn("a file agrep expected is gone", captured.getvalue())

    def test_run_pi_launches_pi_not_omp(self) -> None:
        self._stub("pi")
        self._stub("omp")
        launched: list[list[str]] = []

        def fake_run(argv, **_kwargs):
            launched.append(list(argv))
            return SimpleNamespace(returncode=0)

        with mock.patch.dict(os.environ, {"PATH": str(self.bin)}), \
                mock.patch.object(native, "run_owned_process", side_effect=fake_run), \
                mock.patch("hookless.live.watcher", return_value=None):
            self.assertEqual(cli.cmd_run(SimpleNamespace(rest=["pi"])), 0)
            self.assertEqual(cli.cmd_run(SimpleNamespace(rest=["omp"])), 0)
        self.assertEqual([Path(argv[0]).name for argv in launched], ["pi", "omp"])


@unittest.skipUnless(POSIX and shutil.which("perl"), "sh sentinel needs perl")
class SentinelTracksTheInstall(_Sandbox):
    """[5] the sentinel watches the install the user has, never a cache copy."""

    def _enrol(self) -> Path:
        target = self._agent("gemini", "GEMINI.md")
        target.write_text("mine\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(teach, "_sentinel_install", return_value=True):
            self.assertEqual(teach._install(), 0)
        return target

    def _script(self) -> Path:
        subs = teach._sh_subs([Path(t) for t in json.loads(
            teach.STATE_PATH.read_text(encoding="utf-8"))["targets"]]) | {
            "@@LABEL@@": "com.agrep.sentinel-test", "@@PLIST@@": "''",
            "@@UNIT@@": "agrep-sentinel-test", "@@UNITS@@": "''"}
        tail = (teach._SENTINEL_TAIL_MAC if sys.platform == "darwin"
                else teach._SENTINEL_TAIL_LINUX)
        script = teach._write_sentinel_sh(tail, subs)
        text = script.read_text(encoding="utf-8")
        self.assertNotIn("@@", text)
        script.write_text(text.replace("sleep 20", "sleep 0")
                          .replace("launchctl ", "true ")
                          .replace("systemctl ", "true "), encoding="utf-8")
        return script

    def _run(self, script: Path) -> None:
        subprocess.run(["/bin/sh", str(script)], check=False, timeout=60,
                       capture_output=True)

    def test_uv_tool_and_pipx_installs_watch_their_tool_dirs(self) -> None:
        for layout in ("tools/agrep", "pipx/venvs/agrep"):
            prefix = self.root / layout
            teach.REPO = prefix / "lib" / "site-packages"
            teach.REPO.mkdir(parents=True)
            (teach.REPO / "cli.py").write_text("# cli\n", encoding="utf-8")
            with mock.patch.object(teach.sys, "prefix", str(prefix)):
                self.assertEqual(teach.install_anchor(), teach.REPO / "cli.py")
                self.assertEqual(teach._sentinel_watch_paths(),
                                 [teach.REPO, prefix.parent])

    def test_npm_install_survives_a_uv_cache_clean_and_dies_with_the_package(self) -> None:
        package = self.root / "node_modules" / "@mundy" / "agrep"
        package.mkdir(parents=True)
        anchor = package / "bin.js"
        anchor.write_text("// shim\n", encoding="utf-8")
        cache_copy = self.root / "uvcache" / "archive-v0" / "abc" / "lib" / "site-packages"
        cache_copy.mkdir(parents=True)
        (cache_copy / "cli.py").write_text("# cli\n", encoding="utf-8")
        teach.REPO = cache_copy
        os.environ["AGREP_INSTALL_ANCHOR"] = str(anchor)
        self.assertEqual(teach.install_anchor(), anchor)
        self.assertEqual(teach._sentinel_watch_paths(), [package, package.parent])
        target = self._enrol()
        script = self._script()
        shutil.rmtree(self.root / "uvcache")  # `uv cache clean`
        self._run(script)
        self.assertIn(teach.MARK_PREFIX, target.read_text(encoding="utf-8"))
        self.assertTrue(script.exists())
        anchor.unlink()  # `npm uninstall -g @mundy/agrep`
        self._run(script)
        self.assertEqual(target.read_text(encoding="utf-8"), "mine\n")
        self.assertFalse(script.exists())

    def test_a_newer_installs_block_outlives_this_installs_uninstall(self) -> None:
        anchor = self.root / "pkg" / "bin.js"
        anchor.parent.mkdir()
        anchor.write_text("// shim\n", encoding="utf-8")
        os.environ["AGREP_INSTALL_ANCHOR"] = str(anchor)
        newer = self._agent("gemini", "GEMINI.md")
        older = self._agent("codex")
        for target in (newer, older):
            target.write_text("mine\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(teach, "_sentinel_install", return_value=True):
            self.assertEqual(teach._install(), 0)
        marker = f"{teach.MARK_PREFIX} v{teach.NUDGE_V} -->"
        # a later agrep upgraded one file's block; the other still carries the
        # block an earlier version wrote, which this uninstall must still clean
        upgraded = newer.read_text(encoding="utf-8").replace(
            marker, f"{teach.MARK_PREFIX} v{teach.NUDGE_V + 1} -->")
        self.assertNotEqual(upgraded, newer.read_text(encoding="utf-8"))
        newer.write_text(upgraded, encoding="utf-8")
        older.write_text(older.read_text(encoding="utf-8").replace(
            marker, f"{teach.MARK_PREFIX} v{teach.NUDGE_V - 1} -->"), encoding="utf-8")
        script = self._script()
        anchor.unlink()
        self._run(script)
        self.assertEqual(newer.read_text(encoding="utf-8"), upgraded)
        self.assertEqual(older.read_text(encoding="utf-8"), "mine\n")
        self.assertFalse(script.exists())

    def test_windows_config_names_the_anchor(self) -> None:
        anchor = self.root / "pkg" / "bin.js"
        anchor.parent.mkdir()
        anchor.write_text("// shim\n", encoding="utf-8")
        os.environ["AGREP_INSTALL_ANCHOR"] = str(anchor)
        with mock.patch.object(teach.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, "", "")), \
                mock.patch.object(teach.subprocess, "Popen"), \
                mock.patch.object(teach.common, "windows_background_child_flags",
                                  return_value=0), \
                mock.patch.object(teach, "_retire_legacy_windows_sentinel",
                                  return_value=True), \
                mock.patch.object(teach, "sentinel_armed", return_value=True):
            self.assertTrue(teach._sentinel_install_win([]))
        config = json.loads((self.data / "sentinel.json").read_text(encoding="utf-8"))
        self.assertEqual(config["cli"], str(anchor))
        self.assertEqual(config["block_version"], teach.NUDGE_V)


class RemoveRestoresEveryByte(_Sandbox):
    """[7] the separator agrep appended leaves with its block; lines the user
    added after the block keep exactly one EOL in front of them."""

    CASES = (
        ("single newline", b"</style>\n", 1),
        ("no trailing newline", b"No emojis.", 2),
        ("crlf no trailing newline", b"RULE\r\nNo emojis.", 2),
        ("crlf single newline", b"RULE\r\nNo emojis.\r\n", 1),
        ("blank line", b"rules\n\n", 0),
    )
    # what claude's `#` memory shortcut appends after agrep's block, and the result
    APPENDED = (
        ("no trailing newline", b"- Prefer pnpm.\n", b"No emojis.\n- Prefer pnpm.\n"),
        ("crlf no trailing newline", b"User footer\r\n",
         b"RULE\r\nNo emojis.\r\nUser footer\r\n"),
        ("single newline", b"- Prefer pnpm.\n", b"</style>\n- Prefer pnpm.\n"),
        ("blank line", b"- Prefer pnpm.\n", b"rules\n\n- Prefer pnpm.\n"),
    )

    def _scenarios(self):
        """(label, original bytes, units, bytes appended after the block, expected)."""
        appended = {label: (extra, expected) for label, extra, expected in self.APPENDED}
        for label, original, units in self.CASES:
            yield label, original, units, b"", original
            if label in appended:
                extra, expected = appended[label]
                yield f"{label} + appended", original, units, extra, expected

    def _installed(self, label: str, original: bytes, extra: bytes) -> Path:
        target = self.home / f"{label.replace(' ', '_').replace('+', 'and')}.md"
        target.write_bytes(original)
        self.assertEqual(teach._write_block(target), "added")
        if extra:
            target.write_bytes(target.read_bytes() + extra)
        return target

    def _check(self, strip) -> None:
        for label, original, units, extra, expected in self._scenarios():
            with self.subTest(label):
                target = self._installed(label, original, extra)
                self.assertEqual(teach._ADDED_SEPARATORS[str(target)], units)
                self.assertNotEqual(target.read_bytes(), expected)
                strip(target, units)
                self.assertEqual(target.read_bytes(), expected)

    def test_python_removal_is_byte_exact(self) -> None:
        def strip(target: Path, units: int) -> None:
            self.assertTrue(teach._remove_block(target, separator=units))
        self._check(strip)

    def test_remove_and_install_use_the_recorded_separator(self) -> None:
        target = self._agent("omp", "AGENTS.md")
        target.write_bytes(b"<style>\nrules\n</style>\n")
        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(teach, "_sentinel_install", return_value=True):
            self.assertEqual(teach._install(), 0)
        state = json.loads(teach.STATE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(state["separators"], {str(target): 1})
        with self._removal():
            self.assertEqual(teach._remove(), 0)
        self.assertEqual(target.read_bytes(), b"<style>\nrules\n</style>\n")

    def _perl(self, target: Path, sidecar: Path) -> None:
        stripper = self.data / "sentinel_strip.pl"
        if not stripper.exists():
            stripper.write_text(teach._SENTINEL_STRIP_PERL, encoding="utf-8")
        subprocess.run(
            ["perl", str(stripper), str(target), teach.MARK_PREFIX,
             teach.MARK_END, str(target), str(sidecar), str(teach.NUDGE_V)],
            check=True, timeout=30)

    @unittest.skipUnless(POSIX and shutil.which("perl"), "perl stripper")
    def test_perl_stripper_honours_the_sidecar(self) -> None:
        sidecar = self.data / "sentinel_separators"

        def strip(target: Path, units: int) -> None:
            sidecar.write_text(f"{units}\t{target}\n" if units else "", encoding="utf-8")
            self._perl(target, sidecar)
        self._check(strip)

    @unittest.skipUnless(POSIX and shutil.which("perl"), "perl stripper")
    def test_perl_stripper_reads_every_sidecar_row(self) -> None:
        first = self._installed("first", b"one\ntwo", b"")
        second = self._installed("second", b"x\n", b"")
        third = self._installed("third", b"y", b"- later\n")
        sidecar = self.data / "sentinel_separators"
        sidecar.write_text(f"2\t{first}\n1\t{second}\n2\t{third}\n", encoding="utf-8")
        for target in (first, second, third):
            self._perl(target, sidecar)
        self.assertEqual(first.read_bytes(), b"one\ntwo")
        self.assertEqual(second.read_bytes(), b"x\n")
        self.assertEqual(third.read_bytes(), b"y\n- later\n")

    def test_windows_watcher_honours_the_separators(self) -> None:
        parsed = ast.parse(teach._SENTINEL_WATCH_PY)
        selected = [node for node in parsed.body
                    if isinstance(node, ast.FunctionDef) and node.name in ("_atomic", "strip")]
        namespace: dict = {"Path": Path, "os": os, "re": __import__("re"),
                           "tempfile": tempfile, "subprocess": mock.Mock()}
        exec(compile(ast.Module(body=selected, type_ignores=[]), "<watcher>", "exec"),
             namespace)
        namespace["DIR"] = self.data
        namespace["__file__"] = str(self.data / "sentinel_watch.py")

        def strip(target: Path, units: int) -> None:
            namespace["strip"]({
                "mark_prefix": teach.MARK_PREFIX, "mark_end": teach.MARK_END,
                "block_version": teach.NUDGE_V,
                "targets": [str(target)], "separators": {str(target): units},
                "task_name": "t"})
        self._check(strip)


class HookRemovalLeavesNoTrace(unittest.TestCase):
    """[8] settings.json stub, reformatting and empty dirs after remove."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.data = Path(self.temp.name) / "data"
        (self.home / ".claude").mkdir(parents=True)
        self.data.mkdir()
        for patcher in (mock.patch.object(teach, "HOME", self.home),
                        mock.patch.object(hookinstall.common, "DATA_DIR", self.data)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.settings = self.home / ".claude" / "settings.json"

    def test_settings_agrep_created_is_deleted_and_hooks_dir_removed(self) -> None:
        self.assertTrue(hookinstall.install_claude_hook(warn=False))
        self.assertTrue(self.settings.is_file())
        self.assertTrue((self.home / ".claude" / "hooks").is_dir())
        self.assertEqual(hookinstall.remove(warn=False), 0)
        self.assertFalse(self.settings.exists())
        self.assertFalse((self.home / ".claude" / "hooks").exists())
        self.assertTrue((self.home / ".claude").is_dir())

    def test_existing_settings_come_back_byte_for_byte(self) -> None:
        original = '{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"echo ✓ ready"}]}]}}'
        self.settings.write_text(original, encoding="utf-8")
        self.assertTrue(hookinstall.install_claude_hook(warn=False))
        installed = self.settings.read_text(encoding="utf-8")
        self.assertIn("compact-contract.md", installed)
        self.assertIn("✓", installed)
        self.assertNotIn("\\u2713", installed)
        self.assertEqual(hookinstall.remove(warn=False), 0)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), original)

    def test_crlf_settings_keep_their_line_endings_and_come_back_byte_for_byte(self) -> None:
        original = b'{\r\n    "model": "opus"\r\n}\r\n'
        self.settings.write_bytes(original)
        self.assertTrue(hookinstall.install_claude_hook(warn=False))
        installed = self.settings.read_bytes()
        self.assertIn(b"compact-contract.md", installed)
        self.assertNotIn(b"\n", installed.replace(b"\r\n", b""))
        self.assertEqual(hookinstall.remove(warn=False), 0)
        self.assertEqual(self.settings.read_bytes(), original)

    def test_user_changes_since_install_are_kept_in_their_shape(self) -> None:
        self.settings.write_text('{\n    "model": "opus"\n}\n', encoding="utf-8")
        self.assertTrue(hookinstall.install_claude_hook(warn=False))
        value = json.loads(self.settings.read_text(encoding="utf-8"))
        value["theme"] = "dark"
        self.settings.write_text(json.dumps(value, indent=4) + "\n", encoding="utf-8")
        self.assertEqual(hookinstall.remove(warn=False), 0)
        text = self.settings.read_text(encoding="utf-8")
        self.assertEqual(json.loads(text), {"model": "opus", "theme": "dark"})
        self.assertIn('\n    "model"', text)

    def test_user_precompact_hook_means_no_contract_file_is_written(self) -> None:
        self.settings.write_text(json.dumps({"hooks": {"PreCompact": [
            {"matcher": "manual", "hooks": [{"type": "command", "command": "mine"}]}]}}),
            encoding="utf-8")
        self.assertTrue(hookinstall.install_claude_hook(warn=False))
        self.assertFalse((self.home / ".claude" / "hooks").exists())

    def test_pi_extension_dir_created_by_setup_is_removed(self) -> None:
        (self.home / ".omp" / "agent").mkdir(parents=True)
        self.assertTrue(hookinstall.install_pi_extensions(warn=False))
        extensions = self.home / ".omp" / "agent" / "extensions"
        self.assertTrue(extensions.is_dir())
        self.assertEqual(hookinstall.remove(warn=False), 0)
        self.assertFalse(extensions.exists())
        self.assertTrue((self.home / ".omp" / "agent").is_dir())

    def test_user_owned_extension_dir_stays(self) -> None:
        extensions = self.home / ".omp" / "agent" / "extensions"
        extensions.mkdir(parents=True)
        (extensions / "mine.ts").write_text("export default {}\n", encoding="utf-8")
        self.assertTrue(hookinstall.install_pi_extensions(warn=False))
        self.assertEqual(hookinstall.remove(warn=False), 0)
        self.assertTrue((extensions / "mine.ts").is_file())


class SecondRemoveWithinCooldown(unittest.TestCase):
    """[9] a foreground remove right after another is not 'active'."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        data = Path(self.temp.name)
        patcher = mock.patch.object(removal_fence, "DATA_DIR", data)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_foreground_removal_supersedes_the_cooldown(self) -> None:
        first = removal_fence.acquire_background_removal_fence()
        self.assertIsNotNone(first)
        self.assertTrue(removal_fence.finish_background_removal_fence(first))
        self.assertTrue(removal_fence.background_removal_active())
        self.assertIsNone(removal_fence.acquire_background_removal_fence())
        second = removal_fence.acquire_background_removal_fence(supersede_cooldown=True)
        self.assertIsNotNone(second)
        self.assertTrue(removal_fence.finish_background_removal_fence(second))


class ExtensionRecoveryTextMatchesPostcompact(unittest.TestCase):
    """[10] the queued recovery text names what postcompact prints."""

    def test_names_the_around_continuation_not_more(self) -> None:
        text = hookinstall.PI_EXTENSION.read_text(encoding="utf-8")
        self.assertIn("agrep around <handle> -C 2", text)
        self.assertNotIn("--more", text)


# A login shell that is not POSIX sh: it accepts a single `<program> <file>` command
# and rejects everything else (assignments, `[`, `||`), like fish does with `p=`.
_STRICT_SHELL = r'''#!/bin/sh
[ "$1" = "-lc" ] || { echo "strict: expected -lc" >&2; exit 2; }
set -- $2
case "$#:$1:$2" in
    2:/*:/*) exec "$1" "$2" ;;
esac
echo "strict: unsupported command" >&2
exit 127
'''


@unittest.skipUnless(POSIX, "the codex hook command is a sh snippet")
class CodexHookSurvivesCachePrunes(unittest.TestCase):
    """The codex hook is `/bin/sh <launcher>`: shell-agnostic (codex runs hooks
    through `$SHELL -lc`), running a payload copy inside the codex home with the
    prune-proof base interpreter and a PATH fallback, so a uv/pipx cache prune or
    an upgrade changes nothing it needs. Exactly the registrations agrep wrote
    stay recognisable, so setup repairs them and remove deletes them."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.data = Path(self.temp.name) / "data"
        self.codex = self.home / ".codex"
        self.codex.mkdir(parents=True)
        self.data.mkdir()
        for patcher in (mock.patch.object(teach, "HOME", self.home),
                        mock.patch.object(hookinstall.common, "DATA_DIR", self.data),
                        mock.patch.object(teach.common, "DATA_DIR", self.data),
                        mock.patch.dict(os.environ, {}, clear=False)):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop("CODEX_HOME", None)
        self.hooks = self.codex / "hooks.json"
        self.payload = self.codex / "hooks" / "agrep" / "codex_compact_payload.py"
        self.guidance = self.payload.parent / "codex-postcompact.md"
        self.launcher = self.payload.parent / "run"

    def _command(self) -> str:
        value = json.loads(self.hooks.read_text(encoding="utf-8"))
        return value["hooks"]["SessionStart"][0]["hooks"][0]["command"]

    def _fire(self, command: str, path: str, shell: str = "/bin/sh",
              flag: str = "-lc") -> subprocess.CompletedProcess:
        """What codex does: `$SHELL -lc "<command>"` with the hook event on stdin."""
        event = json.dumps({"hook_event_name": "SessionStart", "source": "compact",
                            "session_id": "s1"})
        return subprocess.run(
            [shell, flag, command], input=event, capture_output=True, text=True,
            env={"PATH": path, "HOME": str(self.home)}, timeout=60)

    def _assert_envelope(self, fired: subprocess.CompletedProcess) -> None:
        self.assertEqual(fired.returncode, 0, fired.stderr)
        self.assertIn("agrep postcompact", json.dumps(json.loads(fired.stdout)))

    def test_hook_runs_from_the_codex_home_after_the_interpreter_is_gone(self) -> None:
        gone = self.home / "cache" / "bin" / "python"
        with mock.patch.object(hookinstall, "_codex_python", return_value=str(gone)):
            self.assertTrue(hookinstall.install_codex_hooks(warn=False))
        for path in (self.payload, self.guidance, self.launcher):
            self.assertTrue(path.is_file(), path)
        command = self._command()
        self.assertEqual(shlex.split(command), ["/bin/sh", str(self.launcher)])
        bin_dir = self.home / "bin"
        bin_dir.mkdir()
        (bin_dir / "python3").symlink_to(sys.executable)
        self._assert_envelope(self._fire(command, str(bin_dir)))
        # the recorded interpreter is used while it exists
        gone.parent.mkdir(parents=True)
        gone.symlink_to(sys.executable)
        self._assert_envelope(self._fire(command, "/nonexistent"))

    def test_hook_works_under_non_posix_login_shells(self) -> None:
        self.assertTrue(hookinstall.install_codex_hooks(warn=False))
        command = self._command()
        bin_dir = self.home / "bin"
        bin_dir.mkdir()
        (bin_dir / "python3").symlink_to(sys.executable)
        strict = bin_dir / "strictsh"
        strict.write_text(_STRICT_SHELL, encoding="utf-8")
        strict.chmod(0o755)
        shells = {"strict stub": (str(strict), "-lc")}
        for name, flag in (("tcsh", "-c"), ("fish", "-lc"), ("nu", "-lc")):
            found = shutil.which(name)
            if found:
                shells[name] = (found, flag)
        for label, (shell, flag) in shells.items():
            with self.subTest(label):
                self._assert_envelope(self._fire(command, str(bin_dir), shell, flag))
        # the inline form the launcher replaced is what those shells reject
        inline = (f"p={shlex.quote(sys.executable)}; [ -x \"$p\" ] || p=python3; "
                  f"exec \"$p\" {shlex.quote(str(self.payload))}")
        self.assertNotEqual(self._fire(inline, str(bin_dir), str(strict)).returncode, 0)

    def test_exactly_the_registrations_agrep_wrote_are_repaired_and_removable(self) -> None:
        ours = {
            "cache copy, shell-rendered": (
                "/x/uv/archive-v0/abc/bin/python "
                "/x/uv/archive-v0/abc/lib/python3.12/site-packages/agrep/py/hooks/"
                "codex_compact_payload.py"),
            "cache copy, legacy quoting": (
                "/x/pipx/.cache/abc/bin/python3.12 "
                "'/x/pipx/.cache/abc/lib/site-packages/agrep/py/hooks/codex_compact_payload.py'"),
            "spaced interpreter, legacy unquoted": (
                "/opt/Python Builds/bin/python "
                "'/opt/agrep/py/hooks/codex_compact_payload.py'"),
            "framework python, quoted": (
                "'/Library/Frameworks/Python.framework/Versions/3.12/Resources/Python.app/"
                "Contents/MacOS/Python' '/opt/agrep/py/hooks/codex_compact_payload.py'"),
            "inline fallback with agrep's own interpreter": (
                f"p={shlex.quote(sys.executable)}; [ -x \"$p\" ] || p=python3; exec \"$p\" "
                f"{self.payload}"),
            "launcher of another home": "/bin/sh '/Users/alice/altcodex/hooks/agrep/run'",
        }
        for label, command in ours.items():
            with self.subTest(label):
                raw = json.dumps(hookinstall._codex_hooks_value(command), indent=1)
                self.hooks.write_text(raw, encoding="utf-8")
                self.assertTrue(hookinstall._codex_hooks_owned(raw))
                self.assertEqual(hookinstall._codex_hook_state(), "ours")
                receipt = io.StringIO()
                with contextlib.redirect_stdout(receipt):
                    self.assertEqual(hookinstall.install(yes=True), 0)
                self.assertIn("codex: SessionStart/compact hook repaired", receipt.getvalue())
                self.assertEqual(self.hooks.read_text(encoding="utf-8"),
                                 hookinstall._codex_hook_payload(self.hooks))
                self.assertEqual(hookinstall.remove(warn=False), 0)
                self.assertFalse(self.hooks.exists())
                self.assertFalse(self.payload.parent.exists())
                self.assertTrue(self.codex.is_dir())
        theirs = {
            "user's own copy": "/user/python '/user/hooks/codex_compact_payload.py'",
            "wrapper script": (
                "/Users/alice/bin/audit-wrapper.sh "
                "/Users/alice/src/agrep/py/hooks/codex_compact_payload.py"),
            "env prefix": f"PYTHONWARNINGS=ignore /usr/bin/python3 {self.payload}",
            "nice prefix": f"nice -n 5 python3 {self.payload}",
            "interpreter option": f"/usr/bin/python3 -I {self.payload}",
            "destructive prefix": f"rm -rf /Users/alice/important {self.payload}",
            "fallback with a user-chosen interpreter": (
                "p=/opt/homebrew/bin/python3.13; [ -x \"$p\" ] || p=python3; exec \"$p\" "
                f"{self.payload}"),
            "extra argument": (
                f"p={shlex.quote(sys.executable)}; [ -x \"$p\" ] || p=python3; exec \"$p\" "
                f"{self.payload} --mine"),
            "prefixed command": f"true; /bin/sh {self.launcher}",
            "launcher via another shell": f"/bin/bash {self.launcher}",
            "launcher elsewhere": "/bin/sh /Users/alice/bin/run",
        }
        for label, command in theirs.items():
            with self.subTest(label):
                raw = json.dumps(hookinstall._codex_hooks_value(command), indent=1)
                self.assertFalse(hookinstall._codex_hooks_owned(raw), label)
                self.hooks.write_text(raw, encoding="utf-8")
                self.assertTrue(hookinstall.install_codex_hooks(warn=False))
                self.assertEqual(self.hooks.read_text(encoding="utf-8"), raw)
                self.assertEqual(hookinstall.remove(warn=False), 0)
                self.assertEqual(self.hooks.read_text(encoding="utf-8"), raw)

    def test_older_shipped_copies_are_recognised_without_the_restore_record(self) -> None:
        self.assertTrue(hookinstall.install_codex_hooks(warn=False))
        old_payload = b"# an older shipped payload\n"
        old_guidance = b"older guidance\n"
        self.payload.write_bytes(old_payload)
        self.guidance.write_bytes(old_guidance)
        (self.data / "hook-restore.json").unlink()
        prior = hookinstall.PRIOR_CODEX_PAYLOAD_HASHES | {
            hookinstall._digest(old_payload), hookinstall._digest(old_guidance)}
        with mock.patch.object(hookinstall, "PRIOR_CODEX_PAYLOAD_HASHES", prior):
            self.assertTrue(hookinstall.install_codex_hooks(warn=False))
        self.assertEqual(self.payload.read_bytes(), hookinstall.CODEX_PAYLOAD.read_bytes())
        self.assertEqual(self.guidance.read_bytes(), hookinstall.CODEX_GUIDANCE.read_bytes())
        # the shipped digests of every release are in the list
        for digest in ("34d8c5d8fec176392054cf56da33de1cb35aa36ebc2ff54a2bab2b21842235fa",
                       "71c34acca9512759ed62edb2cdd90f75eac3c674674c3f9878f7485b769b99b6"):
            self.assertIn(digest, hookinstall.PRIOR_CODEX_PAYLOAD_HASHES)

    def test_user_edited_copies_are_kept_not_failed(self) -> None:
        self.assertTrue(hookinstall.install_codex_hooks(warn=False))
        self.guidance.write_bytes(b"my own guidance\n")
        self.payload.write_bytes(self.payload.read_bytes() + b"# mine\n")
        logged = io.StringIO()
        with contextlib.redirect_stderr(logged), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hookinstall.install(yes=True), 0)
        self.assertIn("edited by hand; kept as it is", logged.getvalue())
        self.assertEqual(self.guidance.read_bytes(), b"my own guidance\n")
        self.assertEqual(hookinstall.remove(warn=False), 0)
        self.assertFalse(self.hooks.exists())
        self.assertFalse(self.launcher.exists())
        self.assertTrue(self.payload.is_file())
        self.assertTrue(self.guidance.is_file())

    def _edited_launcher_is_kept_by_setup(self, edit, *, left_by_remove: bool) -> None:
        self.assertTrue(hookinstall.install_codex_hooks(warn=False))
        edited = edit(self.launcher.read_text(encoding="utf-8"))
        self.launcher.write_text(edited, encoding="utf-8")
        logged = io.StringIO()
        with contextlib.redirect_stderr(logged), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hookinstall.install(yes=True), 0)
        self.assertEqual(self.launcher.read_text(encoding="utf-8"), edited,
                         "setup overwrote the user's launcher")
        self.assertIn(f"{self.launcher} was edited by hand; kept as it is", logged.getvalue())
        self.assertEqual(hookinstall.remove(warn=False), 0)
        self.assertFalse(self.hooks.exists())
        self.assertFalse(self.payload.exists())
        self.assertEqual(self.launcher.is_file(), left_by_remove)

    def test_a_hand_edited_launcher_is_kept_on_setup_and_left_by_remove(self) -> None:
        self._edited_launcher_is_kept_by_setup(lambda text: text.replace(
            "\nexec ", "\nexport AGREP_HOOK_LOG=/tmp/my-codex-hook.log  # my addition\nexec "),
            left_by_remove=True)

    @staticmethod
    def _with_interpreter(text: str, python: str) -> str:
        return "\n".join(f"p={python}" if line.startswith("p=") else line
                         for line in text.split("\n"))

    def test_a_launcher_with_the_users_interpreter_is_kept_by_setup(self) -> None:
        # setup never reverts the user's python; remove, deleting agrep's own
        # registration of that launcher, takes the now-dead template with it
        self._edited_launcher_is_kept_by_setup(
            lambda text: self._with_interpreter(text, "/opt/homebrew/bin/python3.13"),
            left_by_remove=False)

    def test_untouched_launcher_is_removed_without_the_record_under_another_python(self) -> None:
        """setup under one interpreter, the restore record gone (data dir deleted
        or another AGREP_DATA_DIR), remove under another: agrep's own launcher
        must not be stranded as an orphan beside a deleted hooks.json."""
        self.assertTrue(hookinstall.install_codex_hooks(warn=False))
        installed = self.launcher.read_text(encoding="utf-8")
        (self.data / "hook-restore.json").unlink()
        other = str(self.home / "other-venv" / "bin" / "python3.14")
        with mock.patch.object(hookinstall, "_codex_python", return_value=other), \
                mock.patch.object(hookinstall.sys, "executable", other):
            self.assertFalse(hookinstall._launcher_owned(self.launcher))
            self.assertEqual(hookinstall.remove(warn=False), 0)
        self.assertFalse(self.hooks.exists())
        self.assertFalse(self.launcher.exists(), "agrep's own launcher was orphaned")
        self.assertFalse(self.payload.parent.exists())
        # without agrep's registration the relaxed rule does not apply
        self.assertTrue(hookinstall.install_codex_hooks(warn=False))
        self.hooks.write_text(json.dumps({"hooks": {"SessionStart": [
            {"matcher": "startup", "hooks": [{"type": "command", "command": "echo mine"}]}]}}),
            encoding="utf-8")
        self.launcher.write_text(self._with_interpreter(installed, other), encoding="utf-8")
        self.assertEqual(hookinstall.remove(warn=False), 0)
        self.assertTrue(self.hooks.exists())
        self.assertTrue(self.launcher.is_file())

    def test_a_launcher_naming_a_previously_recorded_interpreter_is_refreshed(self) -> None:
        self.assertTrue(hookinstall.install_codex_hooks(warn=False))
        current = self.launcher.read_text(encoding="utf-8")
        record = json.loads((self.data / "hook-restore.json").read_text(encoding="utf-8"))
        record["codex_interpreters"].append("/old/uv/python3.11")
        (self.data / "hook-restore.json").write_text(json.dumps(record), encoding="utf-8")
        self.launcher.write_text(
            self._with_interpreter(current, "/old/uv/python3.11"), encoding="utf-8")
        self.assertTrue(hookinstall._launcher_owned(self.launcher))
        self.assertTrue(hookinstall.install_codex_hooks(warn=False))
        self.assertEqual(self.launcher.read_text(encoding="utf-8"), current)

    def test_hook_under_a_per_shell_codex_home_is_still_removed_later(self) -> None:
        alt = self.home / "altcodex"
        alt.mkdir()
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(alt)}):
            self.assertTrue(hookinstall.install_codex_hooks(warn=False))
        self.assertTrue((alt / "hooks.json").is_file())
        self.assertIn(str(alt / "hooks.json"),
                      json.loads((self.data / "hook-restore.json").read_text())["codex_hooks"])
        self.assertEqual(hookinstall._codex_hook_state(), "ours")
        self.assertEqual(hookinstall.remove(warn=False), 0)
        self.assertFalse((alt / "hooks.json").exists())
        self.assertFalse((alt / "hooks").exists())
        self.assertFalse(self.hooks.exists())
        self.assertTrue(alt.is_dir())

    def test_recorded_interpreter_is_the_prune_proof_base_python(self) -> None:
        base = getattr(sys, "_base_executable", "") or sys.executable
        if not Path(base).exists():
            base = sys.executable
        self.assertEqual(hookinstall._codex_python(), base)

    @unittest.skipUnless(shutil.which("perl"), "sh sentinel needs perl")
    def test_sentinel_strips_the_hook_payload_and_launcher_of_every_home(self) -> None:
        alt = self.home / "altcodex"
        alt.mkdir()
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(alt)}):
            self.assertTrue(hookinstall.install_codex_hooks(warn=False))
        saved = {name: getattr(teach, name) for name in ("REPO", "STATE_PATH")}
        teach.REPO = self.home / "repo"
        teach.REPO.mkdir()
        (teach.REPO / "cli.py").write_text("# cli\n", encoding="utf-8")
        teach.STATE_PATH = self.data / "teach.json"
        self.addCleanup(lambda: [setattr(teach, k, v) for k, v in saved.items()])
        script = teach._write_sentinel_sh("", teach._sh_subs([]))
        text = script.read_text(encoding="utf-8")
        owned = [self.hooks, alt / "hooks.json", self.payload, self.guidance, self.launcher,
                 alt / "hooks" / "agrep" / "codex_compact_payload.py",
                 alt / "hooks" / "agrep" / "run"]
        for path in owned:
            self.assertIn(f"drop_owned {teach._sh_squote(path)}", text)
        self.assertNotIn("@@", text)
        script.write_text(text.replace("sleep 20", "sleep 1"), encoding="utf-8")
        self.guidance.write_bytes(b"edited\n")
        (teach.REPO / "cli.py").unlink()
        subprocess.run(["/bin/sh", str(script)], capture_output=True, timeout=60)
        for path in owned:
            if path == self.guidance:
                self.assertTrue(path.is_file(), "edited copy must survive")
            else:
                self.assertFalse(path.exists(), f"{path} survived the sentinel")
        self.assertFalse((alt / "hooks").exists())


class ActiveHomesAreHonoured(unittest.TestCase):
    """[11] CODEX_HOME for the codex hook, XDG_CONFIG_HOME for opencode."""

    def test_codex_hooks_follow_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            active = home / "altcodex"
            with mock.patch.object(teach, "HOME", home), \
                    mock.patch.dict(os.environ, {"CODEX_HOME": str(active)}):
                self.assertEqual(
                    hookinstall.codex_hooks_paths(),
                    (home / ".codex" / "hooks.json", active / "hooks.json"))
                active.mkdir()
                (active / "config.toml").write_text("", encoding="utf-8")
                self.assertTrue(hookinstall.install_codex_hooks(warn=False))
                self.assertTrue((active / "hooks.json").is_file())
                self.assertFalse((home / ".codex").exists())
                self.assertEqual(hookinstall._codex_hook_state(), "ours")

    def test_opencode_target_follows_xdg_config_home(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            xdg = home / "xdgconf"
            resolved = dict(
                (label, target) for label, _, target in teach.manifest_targets(
                    "markdown", home=home,
                    environ={"XDG_CONFIG_HOME": str(xdg)}, os_name="posix"))
            self.assertEqual(resolved["opencode"], xdg / "opencode" / "AGENTS.md")
            default = dict(
                (label, target) for label, _, target in teach.manifest_targets(
                    "markdown", home=home, environ={}, os_name="posix"))
            self.assertEqual(default["opencode"], home / ".config" / "opencode" / "AGENTS.md")


class ConsentTellsTheTruthAboutTheIndex(unittest.TestCase):
    """[12] nothing removes the data dir; the screen says where it is."""

    def test_consent_screen_names_the_path_and_the_delete_command(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            data = Path(td) / "agrep data"
            with mock.patch.object(cli.common, "DATA_DIR", data), \
                    mock.patch.object(teach, "MD_TARGETS", []), \
                    mock.patch.object(teach, "SKILL_TARGETS", []):
                screen = cli._setup_consent_screen(no_semantic=True)
        self.assertNotIn("removed by uninstall", screen)
        self.assertIn("leave it in place", screen)
        self.assertIn(cli._delete_dir_command(data), screen)

    def test_readme_matches(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Nothing deletes the index for you", readme)
        self.assertNotIn("`--no-teach` skips the instruction\nblocks and the sentinel", readme)
        self.assertIn("only `--no-teach --no-hook`", readme)


class NoSemanticSetupNeverLoadsTheRuntime(unittest.TestCase):
    """[13] setup --no-semantic must not import onnxruntime."""

    def test_setup_without_prefetch_skips_the_deep_probe(self) -> None:
        import importlib.abc

        class Trip(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] == "onnxruntime":
                    raise AssertionError("onnxruntime imported during --no-semantic setup")
                return None

        tripwire = Trip()
        saved = sys.modules.pop("onnxruntime", None)
        sys.meta_path.insert(0, tripwire)
        try:
            with mock.patch.object(doctor, "_json_report",
                                   return_value={"tiers": [], "fixes": []}), \
                    mock.patch.object(doctor, "_semantic_probe") as probe, \
                    mock.patch.object(doctor, "fix") as fix, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(doctor.setup(prefetch_semantic=False), 0)
            probe.assert_not_called()
            fix.assert_not_called()
        finally:
            sys.meta_path.remove(tripwire)
            if saved is not None:
                sys.modules["onnxruntime"] = saved


class LinkedExtensionRootIsNamed(unittest.TestCase):
    """[15] a symlinked ~/.omp/agent is reported as such, not unreadable."""

    @unittest.skipUnless(POSIX, "symlinks")
    def test_receipt_says_symlink_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            real = home / "dotfiles" / "omp-agent"
            real.mkdir(parents=True)
            (home / ".omp").mkdir()
            (home / ".omp" / "agent").symlink_to(real)
            output = io.StringIO()
            with mock.patch.object(teach, "HOME", home), \
                    mock.patch.object(hookinstall, "_readonly_fenced", return_value=False), \
                    contextlib.redirect_stdout(output), \
                    contextlib.redirect_stderr(io.StringIO()):
                hookinstall.install()
            rendered = output.getvalue()
            self.assertNotIn("unreadable", rendered)
            self.assertIn("is a symlink", rendered)
            self.assertIn("left that linked tree alone", rendered)
            self.assertFalse((real / "extensions").exists())



class RemovalFromANonOwnerBuild(unittest.TestCase):
    """`agrep remove` after an upgrade, before the new build owns the index:
    with no semantic process alive there is nothing to stop and removal goes
    through; a live worker of another build that will not retire is named."""

    FOREIGN = indexd_runtime.DerivedMutationInfo(
        "foreign", "a" * 20, "corpus.db owned-by aaaaaaaaaaaaaaaaaaaa; this build is bbbb")

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / "data"
        self.data.mkdir()
        for patcher in (mock.patch.object(common, "DATA_DIR", self.data),
                        mock.patch.object(removal_fence, "DATA_DIR", self.data),
                        mock.patch.object(indexd_runtime, "derived_writer_mutation_info",
                                          return_value=self.FOREIGN)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.namespace = semworker._ephemeral_coordination_dir()
        def clean_namespace() -> None:
            (self.namespace / "worker.lock").unlink(missing_ok=True)
            semworker._remove_empty_dir(self.namespace)
        self.addCleanup(clean_namespace)
        self.fence = removal_fence.acquire_background_removal_fence()
        self.assertIsNotNone(self.fence)
        self.addCleanup(lambda: removal_fence.finish_background_removal_fence(self.fence))

    def _legacy_owner_record(self, pid: int, start: str) -> Path:
        path = semworker.legacy_worker_lock_path()
        path.write_text(json.dumps({
            "pid": pid, "process_start": start, "started_at": time.time(),
            "nonce": "ab" * 16, "tree_bound": False, "named_job": False}),
            encoding="utf-8")
        return path

    def test_nothing_alive_is_stopped_successfully(self) -> None:
        self.assertFalse(indexd_runtime.derived_writer_mutation_info().writable)
        outcome = semworker.stop_worker_and_wait(0.2, 0.0)
        self.assertTrue(outcome["ok"], outcome)
        self.assertFalse(outcome["running"])
        writers = semantic.stop_background_writers_for_removal(wait_s=0.5)
        self.assertTrue(writers["ok"], writers)
        self.assertEqual(writers["state"], "absent")

    def test_dead_records_count_as_nothing_alive(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait(timeout=30)
        self._legacy_owner_record(child.pid, "1-1")
        semantic.embed_claim_path().write_text(json.dumps({
            "pid": child.pid, "process_start": "1-1", "token": "cd" * 16}), encoding="utf-8")
        self.assertTrue(semworker.stop_worker_and_wait(0.2, 0.0)["ok"])
        writers = semantic.stop_background_writers_for_removal(wait_s=0.5)
        self.assertTrue(writers["ok"], writers)
        self.assertFalse(semantic.embed_claim_path().exists())

    def test_a_live_worker_of_another_build_is_named_with_the_remedy(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        start = common.process_start_identity(child.pid)
        if not start:
            self.skipTest("no process start identity on this platform")
        self._legacy_owner_record(child.pid, start)
        outcome = semworker.stop_worker_and_wait(0.3, 0.0)
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["owner_state"], "foreign-owner")
        self.assertEqual(outcome["pid"], child.pid)
        self.assertIn(f"pid {child.pid}", outcome["reason"])
        self.assertIn("index` with this build to take ownership", outcome["reason"])
        self.assertIsNone(child.poll(), "a worker of another build was signalled")
        child.kill()
        child.wait(timeout=30)
        self.assertTrue(semworker.stop_worker_and_wait(0.2, 0.0)["ok"])

    def _start_real_worker(self) -> subprocess.Popen:
        """A real SemanticWorkerServer in its own session, published under this
        data dir with its worker.lock in the gated private namespace."""
        worker = (
            "import semworker\n"
            "owner = semworker.acquire_resident_owner()\n"
            "if owner is None:\n    raise SystemExit(4)\n"
            "server = semworker.SemanticWorkerServer("
            "search_fn=lambda *a, **k: {'results': []}, lifetime=owner)\n"
            "server.serve()\n"
            "owner.close()\n")
        env = {**os.environ, "AGREP_DATA_DIR": str(self.data)}
        for name in ("AGREP_NO_DAEMON", "AGREP_NO_SEM_WORKER", "AGREP_DATA_DIR_SOURCE"):
            env.pop(name, None)
        namespace = semworker._ephemeral_coordination_dir()
        self.addCleanup(lambda: semworker._remove_empty_dir(namespace))
        process = subprocess.Popen(
            [sys.executable, "-c", worker], cwd=PY_DIR, env=env,
            start_new_session=True, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        self.addCleanup(process.stderr.close)
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and process.poll() is None:
            if semworker.descriptor_path().exists():
                break
            time.sleep(0.05)
        self.assertIsNone(process.poll(), process.stderr.read() if process.poll() else "")
        self.assertTrue(semworker.descriptor_path().exists(), "worker never published")
        return process

    def _owned_by_this_build(self):
        return mock.patch.object(
            indexd_runtime, "derived_writer_mutation_info",
            return_value=indexd_runtime.DerivedMutationInfo("absent", None, ""))

    @unittest.skipUnless(POSIX, "POSIX session-leader worker fixture")
    def test_a_live_worker_of_another_build_is_retired_over_its_endpoint(self) -> None:
        """The npm upgrade-then-uninstall window: a real SemanticWorkerServer
        started by the owning build is still serving its idle lease when the
        new build removes; bound to its gated worker.lock, it is stopped."""
        self.fence.release(tombstone=True, require_stable_mtime=True)
        with self._owned_by_this_build():
            process = self._start_real_worker()
        # build B owns nothing here (setUp), and its removal transaction is open
        self.fence = removal_fence.acquire_background_removal_fence()
        self.assertIsNotNone(self.fence)
        live = semworker._live_worker_records()
        self.assertEqual([source for source, _ in live], ["endpoint record"])
        self.assertIn("port", live[0][1])
        self.assertTrue(semworker._bound_to_gated_owner(live[0][1]))
        outcome = semworker.stop_worker_and_wait(5.0, 2.0)
        self.assertTrue(outcome["ok"], outcome)
        self.assertTrue(outcome["stop_ack"], outcome)
        self.assertTrue(outcome["fallback_termination"], "the binding should end the tree")
        self.assertEqual(outcome["pid"], process.pid)
        process.wait(timeout=15)
        self.assertFalse(common.pid_alive(process.pid))
        self.assertEqual(semworker._live_worker_records(), [])
        self.assertTrue(semworker.stop_worker_and_wait(0.2, 0.0)["ok"])

    @unittest.skipUnless(POSIX, "POSIX session-leader worker fixture")
    def test_a_bound_worker_ignoring_the_stop_is_still_ended_by_both_paths(self) -> None:
        """What authorises the signal is the gated worker.lock binding, never
        the reply: a worker that never answers is ended all the same, by the
        owner path and by the non-owner removal path."""
        for path in ("owner", "non-owner"):
            with self.subTest(path):
                self.fence.release(tombstone=True, require_stable_mtime=True)
                with self._owned_by_this_build():
                    process = self._start_real_worker()
                self.fence = removal_fence.acquire_background_removal_fence()
                self.assertIsNotNone(self.fence)
                owned = self._owned_by_this_build() if path == "owner" else contextlib.nullcontext()
                with owned, mock.patch.object(
                        semworker, "_request_worker_stop", return_value=False) as stop:
                    outcome = semworker.stop_worker_and_wait(1.0, 2.0)
                stop.assert_called()
                self.assertTrue(outcome["ok"], outcome)
                self.assertFalse(outcome["stop_ack"])
                self.assertTrue(outcome["fallback_termination"], outcome)
                process.wait(timeout=15)
                self.assertFalse(common.pid_alive(process.pid))
                self.assertEqual(semworker._live_worker_records(), [])

    def _plant_descriptor(self, handler_cls, victim_cmd=("/bin/sh", "-c", "sleep 300 & sleep 300")):
        """A planted endpoint record naming a live session leader plus a port
        served by `handler_cls`, with no worker.lock in the gated namespace."""
        import http.server
        import threading
        listener = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
        threading.Thread(target=listener.serve_forever, daemon=True).start()
        self.addCleanup(listener.shutdown)
        victim = subprocess.Popen(list(victim_cmd), start_new_session=True)

        def end_victim() -> None:
            with contextlib.suppress(OSError):
                os.killpg(victim.pid, 9)
            victim.wait()
        self.addCleanup(end_victim)
        start = common.process_start_identity(victim.pid)
        if not start:
            self.skipTest("no process start identity on this platform")
        record = {
            "version": semworker.PROTOCOL, "pid": victim.pid, "port": listener.server_port,
            "token": "ab" * 32, "started_at": time.time(), "process_start": start,
            "tree_bound": True, "named_job": False, "owner_nonce": "cd" * 16,
            "build_id": semworker.WORKER_BUILD_ID,
            "capabilities": list(semworker.CAPABILITIES), "owner": "disposable"}
        semworker.descriptor_path().write_text(json.dumps(record), encoding="utf-8")
        return victim, record

    @staticmethod
    def _forging_listener(seen: list):
        """The reviewer's impostor: it answers 200 and signs whatever nonce it was
        sent with whatever bearer token it was sent, so any reply-side proof
        computed from the request is forged for free."""
        import hashlib
        import hmac
        import http.server

        class Forger(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - handler contract
                auth = self.headers.get("Authorization", "")
                token = auth[7:] if auth.startswith("Bearer ") else ""
                reply = {"ok": True}
                for name, value in self.headers.items():
                    if name.lower().endswith("nonce"):
                        reply["proof"] = hmac.new(
                            token.encode(), value.encode(), hashlib.sha256).hexdigest()
                seen.append(dict(self.headers.items()))
                body = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                return
        return Forger

    @unittest.skipUnless(POSIX, "POSIX session-leader fixture")
    def test_a_forging_listener_never_earns_a_signal_from_the_non_owner_path(self) -> None:
        seen: list = []
        victim, record = self._plant_descriptor(self._forging_listener(seen))
        self.assertEqual([s for s, _ in semworker._live_worker_records()], ["endpoint record"])
        with mock.patch.object(semworker, "STOP_ACK_ORPHAN_S", 0.2):
            outcome = semworker.stop_worker_and_wait(0.5, 0.0)
        self.assertTrue(seen, "the stop request was sent to the live pid")
        self.assertIsNone(victim.poll(), "the recorded group was terminated on the forged ack")
        self.assertTrue(common._process_group_active(victim.pid))
        self.assertFalse(outcome["ok"])
        self.assertFalse(outcome["fallback_termination"])
        self.assertEqual(outcome["owner_state"], "descriptor-unanswered")
        self.assertTrue(outcome["stop_ack"])
        self.assertEqual(outcome["pid"], victim.pid)
        self.assertIn(f"kill {victim.pid}", outcome["reason"])
        self.assertFalse(semworker._bound_to_gated_owner(record))

    @unittest.skipUnless(POSIX, "POSIX session-leader fixture")
    def test_a_forging_listener_never_earns_a_signal_from_the_owner_path(self) -> None:
        """Same plant, the owning build removing, its private namespace replaced
        by a symlink so the lock cannot vouch: asked, never signalled."""
        seen: list = []
        victim, _record = self._plant_descriptor(self._forging_listener(seen))
        namespace = semworker._ephemeral_coordination_dir()
        planted = self.data / "planted-namespace"
        planted.mkdir()
        namespace.parent.mkdir(parents=True, exist_ok=True)
        if namespace.exists() or namespace.is_symlink():
            self.skipTest("a coordination namespace for this data dir already exists")
        namespace.symlink_to(planted, target_is_directory=True)
        self.addCleanup(namespace.unlink)
        self.fence.release(tombstone=True, require_stable_mtime=True)
        self.addCleanup(lambda: setattr(self, "fence", removal_fence.acquire_background_removal_fence()))
        with self._owned_by_this_build(), mock.patch.object(semworker, "STOP_ACK_ORPHAN_S", 0.2):
            self.assertEqual(semworker._observe_coordination_dir().state,
                             semworker._NamespaceState.BLOCKED)
            outcome = semworker.stop_worker_and_wait(0.5, 0.0)
        self.assertTrue(seen, "the stop request was sent to the live pid")
        self.assertIsNone(victim.poll(), "the recorded group was terminated on the forged ack")
        self.assertTrue(common._process_group_active(victim.pid))
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["owner_state"], "descriptor-unanswered")
        self.assertFalse(outcome["fallback_termination"])
        self.assertIn(f"kill {victim.pid}", outcome["reason"])

    @unittest.skipUnless(POSIX, "POSIX session-leader fixture")
    def test_a_planted_descriptor_without_a_gated_lock_is_never_signalled(self) -> None:
        """Whatever the listener answers - 200, 401, nothing - a record with no
        matching worker.lock in the gated namespace earns no signal."""
        import http.server

        class Refuse(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - handler contract
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args):
                return

        victim, _record = self._plant_descriptor(Refuse)
        with mock.patch.object(semworker, "STOP_ACK_ORPHAN_S", 0.2):
            outcome = semworker.stop_worker_and_wait(0.5, 0.0)
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["owner_state"], "descriptor-unanswered")
        self.assertFalse(outcome["stop_ack"])
        self.assertFalse(outcome["fallback_termination"])
        self.assertIn("did not answer the stop request", outcome["reason"])
        self.assertIsNone(victim.poll())
        self.assertTrue(common._process_group_active(victim.pid))

    def test_a_descriptor_planted_by_another_user_names_no_worker(self) -> None:
        semworker.descriptor_path().write_text("{}", encoding="utf-8")
        with mock.patch.object(
                semworker, "_own_record_snapshot",
                side_effect=semworker.ForeignRecord("foreign owner")):
            self.assertEqual(semworker._live_worker_records(), [])
            self.assertEqual(semworker._inspect_descriptor().state,
                             semworker._DescriptorState.BLOCKED)

    def test_stop_daemons_is_clean_without_semantic_processes(self) -> None:
        output = io.StringIO()
        with mock.patch("resident.stop_servers", return_value={"ok": True, "stopped": ()}), \
                mock.patch.object(indexd_runtime, "stop_indexers_for_removal",
                                  return_value={"ok": True, "stopped": ()}), \
                mock.patch.object(teach, "_data_dir_readonly", return_value=False), \
                contextlib.redirect_stdout(output):
            self.assertTrue(teach._stop_daemons())
        self.assertNotIn("!", output.getvalue())

    def test_the_read_only_guard_still_refuses(self) -> None:
        with mock.patch.object(semworker, "_data_dir_readonly", return_value=True), \
                mock.patch.object(semantic, "_data_dir_readonly", return_value=True):
            self.assertEqual(semworker.stop_worker_and_wait(0.1, 0.0)["owner_state"], "read-only")
            self.assertEqual(semantic.stop_background_writers_for_removal()["state"], "read-only")


if __name__ == "__main__":
    unittest.main()
