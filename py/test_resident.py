"""Black-box parity and lifecycle coverage for the POSIX resident CLI."""
from __future__ import annotations

import atexit
import errno
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))
import resident
from _test_support import write_budget_injection



# The home screen observes under routine budgets a loaded host spends, dropping a line
# from one run of a normal/served pair; every CLI process here runs with them widened.
INJECT = Path(tempfile.mkdtemp(prefix="resident-inject-"))
atexit.register(shutil.rmtree, INJECT, ignore_errors=True)
write_budget_injection(INJECT, "generous")
# Every CLI process these tests start, launcher and served client alike, runs under
# this PYTHONPATH; the endpoint key hashes the sys.path it produces.
CLI_PYTHONPATH = os.pathsep.join((str(INJECT), str(ROOT), str(ROOT / "py")))


def _cli_sys_path() -> list[str]:
    """sys.path exactly as `python cli.py` has it when it hands off to the resident.

    The endpoint key hashes sys.path, so the served client must present the
    launcher's: a shared unittest process accumulates entries from earlier
    modules, and the runner's own PYTHONPATH (unset under plain discovery,
    extended under a gate) is not the one the launcher runs with.
    """
    probe = subprocess.run(
        [sys.executable, "-c",
         "import json, os, sys; sys.path.insert(0, os.path.join(sys.argv[1], 'py')); "
         "print(json.dumps(sys.path))", str(ROOT)],
        cwd=ROOT, env={**os.environ, "PYTHONPATH": CLI_PYTHONPATH},
        capture_output=True, text=True, timeout=60, check=True)
    path = json.loads(probe.stdout)
    # A `-c` program's path[1] is '' (the cwd); cli.py's is its own directory.
    path[1] = str(ROOT)
    return path


CLIENT_PATH = _cli_sys_path()
# Bound on waiting for a condition that a correct resident reaches promptly; only a
# hang reaches it, so it is sized for a saturated host rather than for a measurement.
WAIT_S = 60.0
# The resident declines a command it cannot acknowledge within its deadline,
# which a loaded host can cause; while its endpoint exists the client asks again.
CLIENT = (
    "import os,sys,time;import resident;"
    f"sys.path[:]={CLIENT_PATH!r};"
    f"sys.argv[0]={str(ROOT / 'cli.py')!r};"
    f"deadline=time.monotonic()+{WAIT_S!r}\n"
    "while True:\n"
    " code=resident.try_run()\n"
    " if code is not None or time.monotonic()>=deadline: break\n"
    " if not os.path.exists(resident.socket_path()): break\n"
    " time.sleep(0.03)\n"
    "sys.exit(97 if code is None else code)"
)
# One request and its verdict: a server that cannot be acknowledged is 97, not asked again.
CLIENT_ONCE = (
    "import sys;import resident;"
    f"sys.path[:]={CLIENT_PATH!r};"
    f"sys.argv[0]={str(ROOT / 'cli.py')!r};"
    "code=resident.try_run();sys.exit(97 if code is None else code)"
)
IMPORT_EXPORTS = (
    "AGREP_DATA_DIR", "AGREP_DATA_DIR_SOURCE", "AGREP_PYTHON_RUNTIME_BUILD_ID",
    "AGREP_RUNTIME_BUILD_ID", "AGREP_DERIVED_WRITER_IDENTITY_BLOCKED",
    "AGREP_DERIVED_ADOPTION_OWNER_TOKEN", "AGREP_INDEXD_REFRESH_EXPECTED_WRITER",
)
# Each process owns its own continuation namespace; compare the protocol
# while ignoring the intentionally random handle bytes.
_MORE_HANDLE = re.compile(rb"m\.[A-Za-z0-9_-]{8}")
# The slow-command self-explanation appears only when a loaded host crosses
# its wall-clock threshold; it is not part of a command's answer.
_SLOW_DISCLOSURE = re.compile(rb"(?m)^took \d+(?:\.\d+)?s\b.*\n?")


def _comparable(result):
    code, stdout, stderr = result
    stdout = _MORE_HANDLE.sub(b"m.<TOKEN>", stdout)
    stderr = _MORE_HANDLE.sub(b"m.<TOKEN>", stderr)
    return code, stdout, _SLOW_DISCLOSURE.sub(b"", stderr)


@unittest.skipIf(sys.platform == "win32", "SCM_RIGHTS and fork require POSIX")
class ResidentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = Path(tempfile.mkdtemp(prefix="resident-"))
        cls.addClassCleanup(shutil.rmtree, cls.base, ignore_errors=True)
        # sun_path is 104 bytes on macOS; a TMPDIR-rooted runtime dir would not fit.
        cls.runtime = Path(tempfile.mkdtemp(prefix="agr-", dir="/tmp"))
        cls.addClassCleanup(shutil.rmtree, cls.runtime, ignore_errors=True)
        cls.home = cls.base / "home"
        cls.data = cls.base / "data"
        for path in (cls.home, cls.data):
            path.mkdir()
        # A served call after a gap longer than this idle would meet a missing socket
        # and answer 97 while a new server starts; setUp re-warms the class server. The
        # idle stays short so a killed run leaves no server or runtime directory for long.
        cls.env = {**os.environ, "HOME": str(cls.home), "AGREP_HOME": str(cls.home),
                   "AGREP_DATA_DIR": str(cls.data), "XDG_RUNTIME_DIR": str(cls.runtime),
                   "AGREP_NO_DAEMON": "1", "AGREP_RESIDENT_IDLE_S": "120",
                   "AGREP_CALLER_PUBLICATION_DIR": str(cls.base / "callers"),
                   "AGREP_CLI_NAME": "agrep", "PYTHONPATH": CLI_PYTHONPATH}
        cls.env.pop("AGREP_NO_RESIDENT", None)
        for name in ("claude", "codex", "pi", "omp"):
            shutil.copytree(ROOT / "bench" / "fixtures" / "upgrade_store" / name,
                            cls.home / ("." + name))
        cls.project = cls.base / "project"
        cls.project.mkdir()
        for path in cls.home.rglob("*.jsonl"):
            path.write_text(path.read_text().replace("/work/upgrade-fixture", str(cls.project)))
        (cls.data / "settings.json").write_text('{"embeddings":"off"}\n')
        indexed = subprocess.run([sys.executable, str(ROOT / "cli.py"), "index"],
                                 env={**cls.env, "AGREP_NO_RESIDENT": "1"}, cwd=ROOT,
                                 capture_output=True, timeout=300)
        if indexed.returncode:
            raise AssertionError((indexed.returncode, indexed.stdout, indexed.stderr))
        # A future ingest receipt makes the wall-clock age explicitly unknown on both paths.
        future = time.time() + 3600
        os.utime(cls.data / ".ingest.sig", (future, future))
        sessions = [json.loads(line) for line in (cls.data / "sessions.jsonl").read_text().splitlines()]
        project = next(row["project"] for row in sessions if row["session"].startswith("11111111"))
        cls.project = cls.base / project
        cls.project.mkdir(exist_ok=True)
        cls._warm()

    def setUp(self):
        if not os.path.exists(self._socket_path(self.env)):
            self._warm()

    @classmethod
    def tearDownClass(cls):
        with mock.patch.dict(os.environ, cls.env, clear=True):
            outcome = resident.stop_servers()
        shutil.rmtree(cls.base)
        shutil.rmtree(cls.runtime, ignore_errors=True)
        if not outcome["ok"]:
            raise AssertionError(outcome)

    @classmethod
    def _command(cls, args, *, served=False):
        return ([sys.executable, "-c", CLIENT, *args] if served
                else [sys.executable, str(ROOT / "cli.py"), *args])

    @classmethod
    def _environ(cls, env=None):
        """Class environment with overrides; a None value removes the variable."""
        return {key: value for key, value in {**cls.env, **(env or {})}.items() if value is not None}

    @classmethod
    def _call(cls, args, *, served=False, normal=False, env=None, cwd=None, pty=False):
        environ = cls._environ(env)
        if normal:
            environ["AGREP_NO_RESIDENT"] = "1"
        command = cls._command(args, served=served)
        if not pty:
            result = subprocess.run(command, cwd=cwd or ROOT, env=environ,
                                    capture_output=True, timeout=30)
            return result.returncode, result.stdout, result.stderr
        import pty as terminal
        import termios
        master, slave = terminal.openpty()
        # Compare the program's own bytes: with output processing on, macOS ptys intermittently
        # emit an extra CR before a translated newline.
        attributes = termios.tcgetattr(slave)
        attributes[1] &= ~termios.OPOST
        termios.tcsetattr(slave, termios.TCSANOW, attributes)
        try:
            process = subprocess.Popen(command, cwd=cwd or ROOT, env=environ,
                                       stdin=subprocess.DEVNULL, stdout=slave,
                                       stderr=subprocess.PIPE)
            os.close(slave)
            slave = -1
            body = bytearray()
            while True:
                try:
                    chunk = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    break
                body.extend(chunk)
            _, stderr = process.communicate(timeout=30)
            return process.returncode, bytes(body), stderr
        finally:
            os.close(master)
            if slave >= 0:
                os.close(slave)

    @classmethod
    def _warm(cls, env=None):
        deadline = time.monotonic() + WAIT_S
        while time.monotonic() < deadline:
            result = cls._call(["--help"], served=True, env=env)
            if result[0] != 97:
                if result[0] != 0:
                    raise AssertionError(result)
                return
            time.sleep(0.03)
        raise AssertionError("resident did not become ready")

    @classmethod
    def _socket_path(cls, env):
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "path", CLIENT_PATH):
            return resident.socket_path()

    def _private_runtime(self, **extra):
        """An endpoint in its own runtime directory, stopped and removed with the test."""
        runtime = tempfile.mkdtemp(prefix="agr-", dir="/tmp")
        env = {**self.env, "XDG_RUNTIME_DIR": runtime, **extra}

        def cleanup():
            with mock.patch.dict(os.environ, env, clear=True):
                resident.stop_servers()
            shutil.rmtree(runtime, ignore_errors=True)

        self.addCleanup(cleanup)
        return env

    def _hand_server(self, env, preload):
        """A server whose entry is replaced; its socket exists once this returns."""
        code = f'''
import os, sys, time
sys.path[:] = {CLIENT_PATH!r}
import resident
path = resident.socket_path()
import fcntl
lock = os.open(path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(lock, fcntl.LOCK_EX)
{preload}
resident.serve(path, lock)
'''
        server = subprocess.Popen([sys.executable, "-c", code], env=env, cwd=ROOT,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        def cleanup():
            with mock.patch.dict(os.environ, env, clear=True):
                resident.stop_servers()
            try:
                server.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.communicate()

        self.addCleanup(cleanup)
        runtime = Path(env["XDG_RUNTIME_DIR"])
        self._poll(lambda: list(runtime.rglob("*.sock")) or server.poll() is not None)
        if server.poll() is not None:
            self.fail(server.stderr.read())
        return server

    def _poll(self, condition, timeout=WAIT_S):
        deadline = time.monotonic() + timeout
        while True:
            value = condition()
            if value:
                return value
            self.assertLess(time.monotonic(), deadline, "condition never held")
            time.sleep(0.02)

    @staticmethod
    def _ps_rows():
        listing = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,stat="], capture_output=True, text=True)
        rows = {}
        for line in listing.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 3:
                rows[int(parts[0])] = (int(parts[1]), parts[2])
        return rows

    def _server_pid(self, path):
        listing = subprocess.run(["ps", "-A", "-ww", "-o", "pid=,command="], capture_output=True, text=True)
        pids = [int(line.split(None, 1)[0]) for line in listing.stdout.splitlines()
                if f"resident.serve({path!r},3)" in line]
        self.assertEqual(len(pids), 1, pids)
        return pids[0]

    def _served_child(self, server_pid):
        def find():
            found = [pid for pid, (ppid, _) in self._ps_rows().items() if ppid == server_pid]
            return found[0] if len(found) == 1 else None
        return self._poll(find)

    @staticmethod
    def _filled_pipe():
        """A pipe at capacity blocks the next write; the flag is cleared since the client shares it."""
        read_end, write_end = os.pipe()
        os.set_blocking(write_end, False)
        filler = 0
        try:
            while True:
                filler += os.write(write_end, b"\0" * 65536)
        except BlockingIOError:
            pass
        os.set_blocking(write_end, True)
        return read_end, write_end, filler

    def _read_until(self, descriptor, *, marker=None, timeout=10):
        """Bytes read up to EOF, or up to the first appearance of a marker."""
        body = bytearray()
        deadline = time.monotonic() + timeout
        while marker is None or marker not in body:
            remaining = deadline - time.monotonic()
            self.assertGreater(remaining, 0, bytes(body))
            if select.select([descriptor], [], [], remaining)[0]:
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    self.assertIsNone(marker, bytes(body))
                    break
                body.extend(chunk)
        return bytes(body)

    def test_byte_identical_commands_pipe_and_tty(self):
        commands = [[], ["search", "copper", "-n", "1"], ["copper", "-n", "1"],
                    ["search", "resident-unfindable-token", "-n", "1"],
                    ["chats", "-n", "1"], ["around", "11111111", "0"],
                    ["recall", "copper", "--json"], ["search", "--agent", "unknown-agent", "copper"],
                    ["--version"], ["--help"]]
        for pty in (False, True):
            for args in commands:
                with self.subTest(args=args, pty=pty):
                    if not args:
                        # It ages messages.jsonl in whole seconds; start the pair at a wrap.
                        fraction = (time.time() - (self.data / "messages.jsonl").stat().st_mtime) % 1
                        time.sleep((1 - fraction) % 1 + 0.02)
                    normal = self._call(args, normal=True, pty=pty)
                    served = self._call(args, served=True, pty=pty)
                    if not args:
                        # A loaded box can push the pair across the next whole-second wrap.
                        normal, served = (
                            (code, re.sub(rb"last indexed \d+[smhd] ago",
                                          b"last indexed <AGE> ago", out), err)
                            for code, out, err in (normal, served))
                    normal, served = _comparable(normal), _comparable(served)
                    self.assertNotEqual(served[0], 97)
                    self.assertEqual(served, normal)
                    if "resident-unfindable-token" in args:
                        self.assertEqual(served[0], 1)
                    if "unknown-agent" in args:
                        self.assertEqual(served[0], 2)
                        self.assertIn(b"valid:", served[2])
                    if args[:2] == ["search", "copper"]:
                        self.assertEqual(served[0], 0)
                        self.assertIn(b"copper", served[1])

    def test_environment_and_client_cwd(self):
        args = ["search", "copper", "--here", "-n", "1"]
        for cwd in (self.project, self.home):
            for pty in (False, True):
                env = {"NO_COLOR": "1", "COLUMNS": "49"}
                with self.subTest(cwd=cwd, pty=pty):
                    normal = self._call(args, normal=True, env=env, cwd=cwd, pty=pty)
                    served = self._call(args, served=True, env=env, cwd=cwd, pty=pty)
                    self.assertEqual(_comparable(served), _comparable(normal))
                    self.assertEqual(served[0], 0 if cwd == self.project else 1)
        self.assertEqual(self._call(["--help"], served=True, env={"COLUMNS": "49"}),
                         self._call(["--help"], normal=True, env={"COLUMNS": "49"}))

    def test_published_caller_uses_client_ancestry(self):
        publication = self.base / "callers"
        publication.mkdir(mode=0o700)
        path = publication / f"{os.getpid()}.json"
        path.write_text(json.dumps({
            "pid": os.getpid(), "sessions": ["99999999-9999-4999-8999-999999999999"],
            "updated": int(time.time() * 1000), "cwd": str(self.project),
        }))
        try:
            for args in (["search", "Both synthetic checklists", "-n", "40"], ["recall", "copper", "--json"]):
                normal = _comparable(self._call(args, normal=True))
                served = _comparable(self._call(args, served=True))
                self.assertEqual(served, normal)
                self.assertEqual(served[0], 0)
            self.assertTrue(self._call(["search", "Both synthetic checklists", "-n", "40"],
                                      served=True)[1].startswith(b"@"))
        finally:
            path.unlink()
            publication.rmdir()

    def test_import_environment_completeness(self):
        code = '''
import os
from collections.abc import MutableMapping
import resident
seen = set()
class Recording(MutableMapping):
    def __init__(self, source): self.source = source
    def __getitem__(self, key):
        seen.add(key)
        return self.source[key]
    def __setitem__(self, key, value): self.source[key] = value
    def __delitem__(self, key): del self.source[key]
    def __iter__(self): return iter(self.source)
    def __len__(self): return len(self.source)
os.environ = Recording(os.environ)
resident._preload()
assert seen <= set(resident.RESIDENT_KEY_ENV), sorted(seen - set(resident.RESIDENT_KEY_ENV))
assert "numpy" not in __import__("sys").modules
assert not any(name == "mlx" or name.startswith("mlx.") for name in __import__("sys").modules)
assert __import__("threading").active_count() == 1
'''
        result = subprocess.run([sys.executable, "-c", code], env=self.env, cwd=ROOT,
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_import_environment_reaches_rust_children(self):
        real = self.env.get("AGREP_RS_BIN") or str(ROOT / "target" / "release" / "agrep-rs")
        if not os.access(real, os.X_OK):
            self.skipTest("no agrep-rs binary to wrap")
        dumps = self.base / "env-dumps"
        dumps.mkdir()
        wrapper = self.base / "agrep-rs"
        wrapper.write_text(f'#!/bin/sh\nenv > "{dumps}/$$.env"\nexec "{real}" "$@"\n')
        wrapper.chmod(0o755)
        home = self.base / "export-home"
        shutil.copytree(self.home, home, symlinks=True)
        if sys.platform == "darwin":
            data = home / "Library" / "Application Support" / "agrep"
        else:
            data = home / ".local" / "share" / "agrep"
        overrides = {"HOME": str(home), "AGREP_HOME": str(home), "AGREP_RS_BIN": str(wrapper),
                     "AGREP_DERIVED_ADOPTION_OWNER_TOKEN": "leaked", "AGREP_DATA_DIR": None}
        indexed = subprocess.run(self._command(["index"]), cwd=ROOT, capture_output=True, timeout=300,
                                 env={**self._environ(overrides), "AGREP_NO_RESIDENT": "1",
                                      "AGREP_RS_BIN": real})
        self.assertEqual(indexed.returncode, 0, indexed.stderr)
        self._warm(overrides)

        def exports(*, normal):
            for stale in dumps.iterdir():
                stale.unlink()
            (data / ".store-census.json").unlink(missing_ok=True)
            code, _, stderr = self._call(["search", "copper", "-n", "1"], served=not normal,
                                         normal=normal, env=overrides)
            self.assertNotEqual(code, 97, stderr)
            seen = set()
            for dump in dumps.iterdir():
                pairs = dict(line.split("=", 1) for line in dump.read_text().splitlines()
                             if re.match(r"[A-Za-z_][A-Za-z0-9_]*=", line))
                seen.add(tuple((key, pairs.get(key)) for key in IMPORT_EXPORTS))
            self.assertEqual(len(seen), 1, seen)
            return code, dict(seen.pop())

        normal = exports(normal=True)
        self.assertEqual(normal[1]["AGREP_DATA_DIR"], str(data))
        self.assertIsNone(normal[1]["AGREP_DERIVED_ADOPTION_OWNER_TOKEN"])
        self.assertEqual(exports(normal=False), normal)

    def test_staleness_routes_to_new_endpoint(self):
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(sys, "path", CLIENT_PATH):
            original = resident.socket_path()
            path = ROOT / "py" / "resident.py"
            info = path.stat()
            try:
                os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
                self.assertNotEqual(resident.socket_path(), original)
                self.assertEqual(self._call(["--help"], served=True)[0], 97)
            finally:
                os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
            with mock.patch.dict(os.environ, {"AGREP_DEBUG": "1"}):
                self.assertNotEqual(resident.socket_path(), original)
            self.assertEqual(self._call(["--help"], served=True, env={"AGREP_DEBUG": "1"})[0], 97)

    def test_noninteractive_allowlist_and_opt_out(self):
        forbidden = resident._COMMANDS - resident._READ_COMMANDS
        for command in forbidden:
            with self.subTest(command=command):
                self.assertIsNone(resident.try_run([command]))
        with mock.patch.object(sys, "platform", "win32"):
            self.assertIsNone(resident.try_run(["--version"]))
        with mock.patch.dict(os.environ, {"AGREP_NO_RESIDENT": "1"}):
            self.assertIsNone(resident.try_run(["--version"]))
        for args in ([], ["--help"], ["-V"], ["copper"], ["-E", "copper|lantern"]):
            self.assertTrue(resident.eligible(args))

    def test_two_simultaneous_clients(self):
        args = ["search", "copper", "-n", "1"]
        expected = _comparable(self._call(args, normal=True))
        children = [subprocess.Popen(self._command(args, served=True), cwd=ROOT, env=self.env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    for _ in range(2)]
        for child in children:
            stdout, stderr = child.communicate(timeout=30)
            self.assertEqual(_comparable((child.returncode, stdout, stderr)), expected)

    def test_concurrent_misses_start_one_server(self):
        import fcntl
        env = {**self.env, "AGREP_RESIDENT_IDLE_S": "8"}
        path = self._socket_path(env)
        before = set(Path(path).parent.glob("*.sock"))
        children = [subprocess.Popen(self._command(["--help"], served=True), cwd=ROOT, env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    for _ in range(8)]
        for child in children:
            child.communicate(timeout=15)
            self.assertIn(child.returncode, (0, 97))
        self._warm({"AGREP_RESIDENT_IDLE_S": "8"})
        after = set(Path(path).parent.glob("*.sock"))
        self.assertEqual(after - before, {Path(path)})
        with open(path + ".lock", "rb") as lock:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self._server_pid(path)

    def test_launcher_miss_starts_server(self):
        env = {"AGREP_RESIDENT_IDLE_S": "9"}
        self.assertEqual(self._call(["--help"], env=env)[0], 0)
        # A served probe would start a server itself on a miss; wait for the
        # launcher's server to accept before asking it anything.
        path = self._socket_path({**self.env, **env})

        def accepting():
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.settimeout(0.25)
                probe.connect(path)
                return True
            except OSError:
                return False
            finally:
                probe.close()

        self._poll(accepting)
        self.assertEqual(self._call(["--help"], served=True, env=env)[0], 0)

    def test_a_slow_server_fork_is_awaited_not_declined(self):
        """Forking the preloaded server outlasts the connect's quarter second on a
        loaded host; the client waits the request deadline for the acknowledgement
        instead of giving up and asking again, which forked another child each time."""
        env = self._private_runtime()
        self._hand_server(env, '''
import os, time
real_fork = os.fork
def slow_fork():
    time.sleep(0.6)
    return real_fork()
os.fork = slow_fork
''')
        result = subprocess.run(
            [sys.executable, "-c", CLIENT_ONCE, "--version"], cwd=ROOT, env=env,
            capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count(b"\n"), 1, result.stdout)

    def test_a_late_acknowledgement_runs_the_command_once_or_not_at_all(self):
        """A descheduled client whose request lands late, on a server whose fork is
        slow: the command runs exactly once with its output, or the client is
        declined before committing and falls back. Never a silent exit 1 with no run."""
        env = self._private_runtime()
        marks = self.base / "late-ack-runs"
        marks.mkdir()
        self._hand_server(env, f'''
import os, time
real_fork = os.fork
def slow_fork():
    time.sleep(1.7)
    return real_fork()
os.fork = slow_fork
preload = resident._preload
def wrapped():
    entry = preload()
    def command():
        with open(os.path.join({str(marks)!r}, str(os.getpid())), "w") as fh:
            fh.write("ran")
        print("ran once")
        return 0
    return command
resident._preload = wrapped
''')
        # a client descheduled between connect and request, then one descheduled
        # between the acknowledgement and its go byte for longer than the server waits
        stalls = {
            "request": (
                "import socket, time\n"
                "real = socket.socket.sendmsg\n"
                "def stalled(self, *args, **kwargs):\n"
                "    time.sleep(0.5)\n"
                "    return real(self, *args, **kwargs)\n"
                "socket.socket.sendmsg = stalled\n"),
            "go byte": (
                "import socket, time\n"
                "real = socket.socket.sendall\n"
                "def stalled(self, data, *args, **kwargs):\n"
                "    if data == b'G':\n"
                "        time.sleep(2.5)\n"
                "    return real(self, data, *args, **kwargs)\n"
                "socket.socket.sendall = stalled\n"),
        }
        for stalled, prelude in stalls.items():
            with self.subTest(stalled=stalled):
                before = len(list(marks.iterdir()))
                result = subprocess.run(
                    [sys.executable, "-c", prelude + CLIENT_ONCE, "--version"], cwd=ROOT,
                    env=env, capture_output=True, timeout=60)
                runs = len(list(marks.iterdir())) - before
                self.assertIn(
                    (result.returncode, runs), {(0, 1), (97, 0)},
                    (result.returncode, runs, result.stdout, result.stderr))
                if result.returncode == 0:
                    self.assertEqual(result.stdout, b"ran once\n")

    def test_a_wedged_server_is_abandoned_within_the_short_wait(self):
        """A server that accepts but never greets (stopped, swapped out, hung before
        its loop) costs a command the quarter-second connect round, not the
        acknowledgement wait a greeting server earns."""
        env = self._private_runtime()
        marks = self.base / "wedged-runs"
        marks.mkdir()
        server = self._hand_server(env, f'''
preload = resident._preload
def wrapped():
    entry = preload()
    def command():
        with open(os.path.join({str(marks)!r}, str(os.getpid())), "w") as fh:
            fh.write("ran")
        return entry()
    return command
resident._preload = wrapped
''')
        os.kill(server.pid, signal.SIGSTOP)
        try:
            timed_client = (
                "import time\n"
                "started = time.perf_counter()\n"
                "try:\n"
                + "".join("    " + line + "\n" for line in CLIENT_ONCE.split(";"))
                + "finally:\n"
                "    import sys\n"
                "    sys.stderr.write(repr(time.perf_counter() - started))\n")
            result = subprocess.run(
                [sys.executable, "-c", timed_client, "--version"], cwd=ROOT, env=env,
                capture_output=True, timeout=60)
        finally:
            os.kill(server.pid, signal.SIGCONT)
        self.assertEqual(result.returncode, 97, result.stderr)
        self.assertLess(float(result.stderr.rsplit(b"\n", 1)[-1]), 1.0, result.stderr)
        # Resumed, the server reads the stale request of a client that has already run
        # the command itself; without a go byte that request runs nothing, and the
        # server goes on serving.
        self.assertEqual(
            subprocess.run([sys.executable, "-c", CLIENT_ONCE, "--version"], cwd=ROOT,
                           env=env, capture_output=True, timeout=60).returncode, 0)
        self.assertEqual(len(list(marks.iterdir())), 1, "the stale request ran the command")
        self.assertIsNone(server.poll())

    def test_dont_write_bytecode_client_is_served(self):
        command = [sys.executable, "-B", "-c", CLIENT, "--help"]
        deadline = time.monotonic() + WAIT_S
        while True:
            result = subprocess.run(command, cwd=ROOT, env=self.env, capture_output=True, timeout=30)
            if result.returncode != 97:
                break
            self.assertLess(time.monotonic(), deadline, "a -B client never got served")
            time.sleep(0.03)
        self.assertEqual((result.returncode, result.stderr), (0, b""))

    def test_overlong_socket_path_never_spawns(self):
        parent = Path(tempfile.mkdtemp(prefix="agr-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, parent, ignore_errors=True)
        runtime = parent / ("x" * 90)
        runtime.mkdir()
        env = {**self.env, "XDG_RUNTIME_DIR": str(runtime)}
        self.assertGreater(len(os.fsencode(self._socket_path(env))), 108)
        self.assertEqual(self._call(["--version"], served=True, env=env)[0], 97)
        self.assertEqual(list(runtime.rglob("*.lock")), [])

    def test_nonpositive_idle_never_spawns(self):
        env = self._private_runtime(AGREP_RESIDENT_IDLE_S="0")
        self.assertEqual(self._call(["--version"], served=True, env=env)[0], 97)
        self.assertEqual(list(Path(env["XDG_RUNTIME_DIR"]).rglob("*.lock")), [])

    def test_server_drops_inherited_descriptors(self):
        env = {**self.env, "AGREP_RESIDENT_IDLE_S": "7"}
        read_end, write_end = os.pipe()
        self.addCleanup(os.close, read_end)
        try:
            result = subprocess.run(self._command(["--version"], served=True), cwd=ROOT, env=env,
                                    capture_output=True, timeout=30, pass_fds=(write_end,))
        finally:
            os.close(write_end)
        self.assertIn(result.returncode, (0, 97))
        self.assertTrue(select.select([read_end], [], [], 3)[0], "server kept the inherited pipe")
        self.assertEqual(os.read(read_end, 1), b"")

    def test_forged_client_pid_is_refused_before_fork(self):
        import array
        import marshal
        path = self._socket_path(self.env)
        body = marshal.dumps((
            ["agrep", "--version"], str(ROOT), self.env,
            [("utf-8", "strict", False, False)] * 3, os.getpid() + 1_000_000, 0o022))
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(2)
            connection.connect(path)
            connection.sendall(resident._HELLO)
            self.assertEqual(connection.recv(1), resident._GREETING)
            connection.sendmsg([len(body).to_bytes(4, "big") + body],
                               [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [0, 1, 2]))])
            self.assertEqual(connection.recv(5), b"")
        self.assertEqual(self._call(["--help"], served=True)[0], 0)

    def test_socket_removal_retires_server(self):
        import fcntl
        env = {"AGREP_RESIDENT_IDLE_S": "11"}
        self._warm(env)
        path = self._socket_path({**self.env, **env})
        os.unlink(path)
        deadline = time.monotonic() + WAIT_S
        with open(path + ".lock", "rb") as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.025)
        self.assertFalse(os.path.exists(path))

    def test_unsafe_directory_falls_back(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            directory = resident._directory()
            os.chmod(directory, 0o755)
            try:
                self.assertIsNone(resident.try_run(["--version"]))
            finally:
                os.chmod(directory, 0o700)
        self._warm()

    def test_idle_exit(self):
        env = {"AGREP_RESIDENT_IDLE_S": "0.4"}
        self._warm(env)
        path = self._socket_path({**self.env, **env})
        deadline = time.monotonic() + WAIT_S
        while os.path.exists(path) and time.monotonic() < deadline:
            time.sleep(0.025)
        self.assertFalse(os.path.exists(path))

    def test_remove_stops_resident(self):
        code = '''
import resident, teach
from unittest import mock
with mock.patch.object(teach.indexd_runtime, "stop_indexers_for_removal", return_value={"ok": True}), \\
     mock.patch("semworker.stop_worker_and_wait", return_value={"ok": True}), \\
     mock.patch("semantic.stop_background_writers_for_removal", return_value={"ok": True}):
    assert teach._stop_daemons()
try:
    resident._directory(create=False)
except FileNotFoundError:
    pass
else:
    raise AssertionError("resident directory remains after remove")
'''
        result = subprocess.run([sys.executable, "-c", code], env=self.env, cwd=ROOT,
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self._warm()

    def test_remove_waits_for_a_server_still_starting(self):
        """A server spawned just before a remove holds its lock through its preload
        and can only notice the stopping marker past it; the remove waits for that
        on a loaded host instead of reporting the resident unstoppable."""
        env = self._private_runtime()
        preloading = self.base / "preloading"
        code = f'''
import os, sys, time
sys.path[:] = {CLIENT_PATH!r}
import resident
path = resident.socket_path()
import fcntl
lock = os.open(path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(lock, fcntl.LOCK_EX)
preload = resident._preload
def slow_preload():
    open({str(preloading)!r}, "w").close()
    time.sleep(4.0)
    return preload()
resident._preload = slow_preload
resident.serve(path, lock)
'''
        server = subprocess.Popen([sys.executable, "-c", code], env=env, cwd=ROOT,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: (server.poll() is None and server.kill(), server.communicate()))
        with mock.patch.dict(os.environ, env, clear=True):
            runtime = Path(resident._directory())
            self._poll(lambda: preloading.exists() or server.poll() is not None)
            if server.poll() is not None:
                self.fail(server.stderr.read())
            started = time.monotonic()
            outcome = resident.stop_servers()
        self.assertTrue(outcome["ok"], outcome)
        self.assertGreaterEqual(time.monotonic() - started, 3.0, "the starting server was not waited for")
        self.assertEqual(server.wait(timeout=30), 0)
        self.assertEqual(list(runtime.parent.glob(runtime.name)), [], "runtime directory remains")

    def test_stop_is_a_plain_ok_in_both_directions(self):
        """`agrep remove` right after an upgrade meets the other version's resident
        for up to its idle time: a stop is STOP answered by OK on the wire, with no
        greeting either way, so each version's stopper stops the other's server."""
        env = self._private_runtime()
        # a server of the previous protocol: reads a request, answers a STOP with OK
        old_server = f'''
import os, socket, sys
sys.path[:] = {CLIENT_PATH!r}
import resident
path = os.path.join(resident._directory(), "0" * 20 + ".sock")
listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
mask = os.umask(0o077)
listener.bind(path)
os.umask(mask)
listener.listen(8)
print("ready", flush=True)
while True:
    connection, _ = listener.accept()
    request = connection.recv(4)
    if request == b"STOP":
        connection.sendall(b"OK")
        connection.close()
        break
    connection.close()
listener.close()
os.unlink(path)
'''
        server = subprocess.Popen([sys.executable, "-c", old_server], env=env, cwd=ROOT,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: (server.poll() is None and server.kill(), server.communicate()))
        self.assertEqual(server.stdout.readline(), b"ready\n")
        with mock.patch.dict(os.environ, env, clear=True):
            outcome = resident.stop_servers()
        self.assertTrue(outcome["ok"], outcome)
        self.assertIn("resident CLI", outcome["stopped"])
        self.assertEqual(server.wait(timeout=30), 0, server.stderr.read())

        # a stopper of the previous protocol against this server: STOP, then exactly OK
        env = self._private_runtime()
        server = self._hand_server(env, "")
        path = next(Path(env["XDG_RUNTIME_DIR"]).rglob("*.sock"))
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(WAIT_S)
            connection.connect(str(path))
            connection.sendall(b"STOP")
            self.assertEqual(connection.recv(2), b"OK")
            self.assertEqual(connection.recv(1), b"")
        self.assertEqual(server.wait(timeout=WAIT_S), 0, server.stderr.read())

    def test_stale_stopping_marker_is_reclaimed(self):
        import fcntl
        env = self._private_runtime(AGREP_RESIDENT_IDLE_S="15")
        with mock.patch.dict(os.environ, env, clear=True):
            marker = Path(resident._directory()) / ".stopping"
            marker.touch(mode=0o600)
            holder = os.open(marker, os.O_RDWR)
            try:
                fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertFalse(resident.stop_servers()["ok"])
                self.assertIsNone(resident.try_run(["--version"]))
            finally:
                os.close(holder)
            self.assertTrue(marker.exists())
            self.assertEqual(list(marker.parent.glob("*.lock")), [])
            outcome = resident.stop_servers()
        self.assertTrue(outcome["ok"], outcome)
        self.assertFalse(marker.exists())
        self._warm({"XDG_RUNTIME_DIR": env["XDG_RUNTIME_DIR"], "AGREP_RESIDENT_IDLE_S": "15"})

    def test_signal_death_matches_normal_path(self):
        results = []
        for served in (False, True):
            read_end, write_end = os.pipe()
            os.close(read_end)
            env = self.env if served else {**self.env, "AGREP_NO_RESIDENT": "1"}
            try:
                process = subprocess.Popen(self._command(["--help"], served=served), cwd=ROOT, env=env,
                                           stdin=subprocess.DEVNULL, stdout=write_end,
                                           stderr=subprocess.PIPE)
            finally:
                os.close(write_end)
            _, stderr = process.communicate(timeout=30)
            results.append((process.returncode, stderr))
        self.assertEqual(results[0], (-signal.SIGPIPE, b""))
        self.assertEqual(results[1], results[0])

    def test_server_death_ends_served_child(self):
        extra = {"AGREP_RESIDENT_IDLE_S": "13", "AGREP_DEBUG": "1"}
        env = {**self.env, **extra}
        self._warm(extra)
        server = self._server_pid(self._socket_path(env))
        read_end, write_end, filler = self._filled_pipe()
        err_read, err_write = os.pipe()
        self.addCleanup(os.close, read_end)
        self.addCleanup(os.close, err_read)
        client = subprocess.Popen(self._command(["search", "copper", "-n", "1"], served=True), cwd=ROOT,
                                  env=env, stdin=subprocess.DEVNULL, stdout=write_end, stderr=err_write)
        os.close(write_end)
        os.close(err_write)
        try:
            stderr = self._read_until(err_read, marker=b"search start")
            os.kill(server, signal.SIGKILL)
            client.wait(timeout=10)
        finally:
            if client.poll() is None:
                client.kill()
                client.wait()
        self.assertEqual(client.returncode, 1)
        stdout = self._read_until(read_end)
        stderr += self._read_until(err_read)
        self.assertIn(b"disconnected without reporting", stderr)
        self.assertEqual(len(stdout), filler, stdout[filler:])

    def test_end_orphan_treats_an_unsignalable_group_as_gone(self):
        """macOS answers signal 0 with EPERM for a group whose only member is exiting or a zombie."""
        sent = []

        def killpg(pid, signum):
            sent.append((pid, signum))
            raise PermissionError(errno.EPERM, "Operation not permitted")

        started = time.monotonic()
        with mock.patch.object(os, "killpg", killpg):
            resident._end_orphan(424242)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(sent, [(424242, signal.SIGTERM), (424242, signal.SIGCONT), (424242, 0)])

    def test_terminal_stop_reaches_served_child(self):
        extra = {"AGREP_RESIDENT_IDLE_S": "14", "AGREP_DEBUG": "1"}
        env = {**self.env, **extra}
        self._warm(extra)
        server = self._server_pid(self._socket_path(env))
        read_end, write_end, filler = self._filled_pipe()
        err_read, err_write = os.pipe()
        self.addCleanup(os.close, read_end)
        self.addCleanup(os.close, err_read)
        # Default stop actions are discarded in an orphaned group: keep the client in this session.
        client = subprocess.Popen(self._command(["search", "copper", "-n", "1"], served=True), cwd=ROOT,
                                  env=env, stdin=subprocess.DEVNULL, stdout=write_end, stderr=err_write,
                                  preexec_fn=os.setpgrp)
        os.close(write_end)
        os.close(err_write)
        try:
            self._read_until(err_read, marker=b"search start")
            child = self._served_child(server)
            client.send_signal(signal.SIGTSTP)
            self._poll(lambda: all(self._ps_rows().get(pid, (0, ""))[1].startswith("T")
                                   for pid in (client.pid, child)))
            client.send_signal(signal.SIGCONT)
            self._poll(lambda: not self._ps_rows().get(child, (0, "T"))[1].startswith("T"))
            stdout = self._read_until(read_end)
            client.wait(timeout=10)
        finally:
            if client.poll() is None:
                client.send_signal(signal.SIGCONT)
                client.kill()
                client.wait()
        self.assertEqual(client.returncode, 0)
        self.assertIn(b"copper", stdout[filler:])

    def test_killed_stopped_client_ends_served_child(self):
        env = self._private_runtime(AGREP_RESIDENT_IDLE_S="16")
        ready = self.base / "stop-kill.ready"
        handled = self.base / "stop-kill.handled"
        # macOS kills a stopped SIG_DFL process outright; only a handled SIGTERM waits for SIGCONT there.
        server = self._hand_server(env, f'''
import signal
def terminated(*_):
    open({str(handled)!r}, "w").close()
    sys.exit(143)
def command():
    if sys.argv[1:] != ["--version"]:
        return 0
    signal.signal(signal.SIGTERM, terminated)
    open({str(ready)!r}, "w").close()
    time.sleep(30)
    return 0
resident._preload = lambda: command
''')
        # Default stop actions are discarded in an orphaned group: keep the client in this session.
        client = subprocess.Popen(self._command(["--version"], served=True), env=env, cwd=ROOT,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, preexec_fn=os.setpgrp)
        child = None
        try:
            self._poll(ready.exists)
            child = self._served_child(server.pid)
            client.send_signal(signal.SIGTSTP)
            self._poll(lambda: all(self._ps_rows().get(pid, (0, ""))[1].startswith("T")
                                   for pid in (client.pid, child)))
            client.kill()
            client.wait(timeout=10)
            self._poll(lambda: child not in self._ps_rows(), timeout=5)
        finally:
            if client.poll() is None:
                client.send_signal(signal.SIGCONT)
                client.kill()
                client.wait()
            if child is not None:
                resident._kill_group(child, signal.SIGKILL)
        self.assertEqual(client.returncode, -signal.SIGKILL)
        self.assertTrue(handled.exists())
        self.assertEqual(self._call(["--help"], served=True, env=env)[0], 0)
        self.assertIsNone(server.poll())

    def test_orphaned_stop_never_strands_served_child(self):
        extra = {"AGREP_RESIDENT_IDLE_S": "15", "AGREP_DEBUG": "1"}
        env = {**self.env, **extra}
        self._warm(extra)
        server = self._server_pid(self._socket_path(env))
        read_end, write_end, filler = self._filled_pipe()
        err_read, err_write = os.pipe()
        self.addCleanup(os.close, read_end)
        self.addCleanup(os.close, err_read)
        # A new session orphans the client's group, so the kernel discards the client's own stop.
        client = subprocess.Popen(self._command(["search", "copper", "-n", "1"], served=True), cwd=ROOT,
                                  env=env, stdin=subprocess.DEVNULL, stdout=write_end, stderr=err_write,
                                  start_new_session=True)
        os.close(write_end)
        os.close(err_write)
        child = None
        try:
            self._read_until(err_read, marker=b"search start")
            child = self._served_child(server)
            client.send_signal(signal.SIGTSTP)
            # The handler's stop and resume leave no state to poll; let the signal land first.
            time.sleep(0.3)
            stdout = self._read_until(read_end)
            client.wait(timeout=10)
        finally:
            if client.poll() is None:
                if child is not None:
                    resident._kill_group(child, signal.SIGKILL)
                client.kill()
                client.wait()
        self.assertEqual(client.returncode, 0)
        self.assertIn(b"copper", stdout[filler:])

    def test_debug_clocks_restart_per_command(self):
        env = self._private_runtime(AGREP_DEBUG="1")
        # The server's clocks are aged an hour so an inherited clock is unmistakable
        # from a command's own however slowly a loaded host runs the command.
        self._hand_server(env, '''
preload = resident._preload
def wrapped():
    entry = preload()
    import console
    from hookless import _log
    _log._DBG_T0 -= 3600.0
    console._DBG_T0 -= 3600.0
    def command():
        _log.dbg("resident probe")
        return entry()
    return command
resident._preload = wrapped
''')
        code, _, stderr = self._call(["--version"], served=True, env=env)
        self.assertEqual(code, 0, stderr)
        self.assertIn(b"resident probe", stderr)
        offsets = [float(value) for value in re.findall(rb"\[agrep \+\s*([0-9.]+)ms\]", stderr)]
        self.assertTrue(offsets, stderr)
        self.assertLess(max(offsets), 1_800_000, stderr)

    def test_signal_forwarding_and_abnormal_exit(self):
        env = self._private_runtime()
        pidfile = self.base / "child.pid"
        self._hand_server(env, f'''
def command():
    staged = {str(pidfile)!r} + ".tmp"
    with open(staged, "w") as f: f.write(str(os.getpid()))
    os.replace(staged, {str(pidfile)!r})
    time.sleep(30)
    return 0
resident._preload = lambda: command
''')
        client = subprocess.Popen(self._command(["--version"], served=True), env=env, cwd=ROOT,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self._poll(pidfile.exists)
            child_pid = int(pidfile.read_text())
            client.send_signal(signal.SIGINT)
            client.communicate(timeout=5)
            self.assertNotEqual(client.returncode, 0)
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)
            pidfile.unlink()
            client = subprocess.Popen(self._command(["--version"], served=True), env=env, cwd=ROOT,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self._poll(pidfile.exists)
            child_pid = int(pidfile.read_text())
            os.kill(child_pid, signal.SIGKILL)
            stdout, stderr = client.communicate(timeout=5)
            self.assertEqual((client.returncode, stdout, stderr), (-signal.SIGKILL, b"", b""))
        finally:
            if client.poll() is None:
                client.kill()
                client.communicate()


if __name__ == "__main__":
    unittest.main()
