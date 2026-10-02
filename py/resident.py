"""POSIX warm CLI: descriptor-passing clients and a single-threaded fork server.

AGREP_NO_RESIDENT=1 selects ordinary execution. AGREP_RESIDENT_IDLE_S controls
idle lifetime (600 seconds). A miss starts a detached server without delaying
that invocation for its imports. Only bounded, noninteractive commands qualify.
"""
from __future__ import annotations

import os
import sys

PROTOCOL = 2
RESIDENT_KEY_ENV = (
    "AGREP_HOME", "AGREP_DATA_DIR", "AGREP_DATA_DIR_SOURCE", "AGREP_DATA_READONLY",
    "AGREP_RS_BIN", "AGREP_DEBUG", "AGREP_T0", "AGREP_DERIVED_JOURNAL_SETTLE_S",
    "AGREP_INDEXD_REFRESH_EXPECTED_WRITER", "AGREP_SEM_BOOTSTRAP_RETRY_S",
    "AGREP_SEM_BOOTSTRAP_RETRY_BASE_S", "AGREP_SEM_BOOTSTRAP_MAX_NEW",
    "AGREP_SEM_DEMAND_MAX_NEW", "AGREP_SEM_REFS_DEMAND_S", "AGREP_SEM_DEMAND_REFRESH_S",
    "AGREP_SEM_QUERY_RECOVERY_WAIT_S", "AGREP_SEM_QUERY_RECOVERY_POLL_S",
    "AGREP_SEM_INTEGRITY_MIN_DROPS", "AGREP_RESIDENT_IDLE_S",
    "AGREP_CALLER_PUBLICATION_DIR", "AGREP_DERIVED_ADOPTION_OWNER_TOKEN",
    "AGREP_DERIVED_WRITER_IDENTITY_BLOCKED", "AGREP_FAMILY_PUBLICATION_WAIT_S",
    "AGREP_RUNTIME_BUILD_ID",
    "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "XDG_DATA_HOME",
    "XDG_RUNTIME_DIR", "TMPDIR", "TMP", "TEMP", "PATH", "PYTHONPATH", "PYTHONHOME",
    "PYTHONIOENCODING", "PYTHONUTF8", "PYTHONCOERCECLOCALE", "PYTHONUNBUFFERED",
    "PYTHONWARNINGS", "PYTHONSAFEPATH", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TZ",
)
PRELOAD_MODULES = (
    "cli", "search", "around", "recall", "indexd_runtime", "semantic", "semworker",
)
_COMMANDS = frozenset((
    "status", "doctor", "audit", "tail", "board", "live", "setup", "remove", "index",
    "reindex", "resume", "run", "search", "chats", "around", "postcompact", "recall",
    "pack", "archive", "restore", "set", "inject", "ui", "up", "serve", "explorer",
    "summary", "why",
))
_READ_COMMANDS = frozenset(("search", "chats", "around", "recall", "summary", "why"))
_MAX_REQUEST = 4 * 1024 * 1024
_SUN_PATH_MAX = 104 if sys.platform == "darwin" else 108
# Result kinds: the command returned a code, the child exited without one, or a signal ended it.
_REPORTED, _EXITED, _SIGNALED = 0, 1, 2


def eligible(argv: list[str]) -> bool:
    if (sys.platform == "win32" or os.name != "posix"
            or not hasattr(os, "fork") or os.environ.get("AGREP_NO_RESIDENT") == "1"):
        return False
    return (not argv or argv in (["--version"], ["-V"], ["--help"], ["-h"])
            or argv[0] in _READ_COMMANDS
            or (argv[0] not in _COMMANDS
                and argv[0] not in ("--version", "-V", "--help", "-h")))


def _directory(*, create: bool = True) -> str:
    import stat
    base = os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("TMPDIR") or "/tmp"
    path = os.path.join(os.path.abspath(base), f"agrep-resident-{os.getuid()}")
    if create:
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
    info = os.lstat(path)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise OSError("unsafe resident directory")
    return path


def _idle_seconds() -> float | None:
    import math
    try:
        idle = float(os.environ.get("AGREP_RESIDENT_IDLE_S", "600"))
    except ValueError:
        return None
    return idle if math.isfinite(idle) and idle > 0 else None


def _endpoint_usable(path: str) -> bool:
    """Client and server share one verdict: a server that cannot bind must never be spawned."""
    return len(os.fsencode(path)) < _SUN_PATH_MAX and _idle_seconds() is not None


def _stopping_marker_active(directory: str) -> bool:
    """Only a remove's flock vetoes a start; a marker left by a killed remove does not."""
    import fcntl
    try:
        descriptor = os.open(os.path.join(directory, ".stopping"),
                             os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(descriptor)
    return False


def _claim_stopping_marker(directory: str) -> int | None:
    import fcntl
    import stat
    try:
        holder = os.open(os.path.join(directory, ".stopping"),
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(holder).st_mode):
            raise OSError("stopping marker is not a regular file")
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(holder)
        return None
    return holder


def _file_stamp(path: str, *, follow: bool = True) -> tuple:
    try:
        info = os.stat(path, follow_symlinks=follow)
    except FileNotFoundError:
        return ()
    return info.st_size, info.st_mtime_ns, info.st_ino


def _code_stamp(root: str) -> list[tuple]:
    ignored = {".git", ".venv", "venv", "target", "node_modules", "__pycache__",
               ".pytest_cache", ".mypy_cache", ".ruff_cache", "data"}
    rows = []
    pending = [(root, "")]
    while pending:
        directory, relative = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                name = relative + entry.name
                if entry.is_dir(follow_symlinks=False):
                    if entry.name not in ignored:
                        pending.append((entry.path, name + "/"))
                elif entry.name.endswith(".py"):
                    info = entry.stat()
                    rows.append((name, info.st_size, info.st_mtime_ns, info.st_ino))
    manifest = os.path.join(root, "py", "runtime_manifest.json")
    rows.append(("py/runtime_manifest.json", *_file_stamp(manifest)))
    binary = os.environ.get("AGREP_RS_BIN")
    if binary:
        binaries = [os.path.abspath(binary)]
    else:
        binaries = [os.path.join(root, "target", "release", "agrep-rs"),
                    os.path.join(root, "_bin", "agrep-rs")]
        home = os.path.expanduser(os.environ.get("AGREP_HOME") or "~")
        if sys.platform == "darwin":
            default = os.path.join(home, "Library", "Application Support", "agrep")
        else:
            base = (os.path.join(home, ".local", "share") if os.environ.get("AGREP_HOME")
                    else os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share"))
            default = os.path.join(base, "agrep")
        data = os.path.expanduser(os.environ.get("AGREP_DATA_DIR") or default)
        try:
            with os.scandir(os.path.join(data, "bin")) as entries:
                binaries.extend(os.path.join(entry.path, "agrep-rs") for entry in entries
                                if entry.is_dir())
        except FileNotFoundError:
            pass
    rows.extend((path, *_file_stamp(path)) for path in binaries)
    return sorted(rows)


def socket_path() -> str:
    """Resolve a private endpoint from code identity and import-time inputs."""
    try:
        from _blake2 import blake2b
    except ImportError:
        from hashlib import blake2b
    root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    values = [(key, os.environ.get(key)) for key in RESIDENT_KEY_ENV]
    relative = [(key, os.path.abspath(os.path.expanduser(value)))
                for key, value in values if value and key in
                {"AGREP_DATA_DIR", "AGREP_HOME", "AGREP_RS_BIN", "HOME", "XDG_DATA_HOME"}]
    identity = (PROTOCOL, sys.executable, root, _code_stamp(root), values, relative,
                sys.path, sys.flags.optimize, sys.flags.dont_write_bytecode)
    key = blake2b(repr(identity).encode("utf-8", "surrogateescape"), digest_size=10).hexdigest()
    return os.path.join(_directory(), key + ".sock")


def _peer_ok(connection) -> bool:
    import socket
    if hasattr(socket, "SO_PEERCRED"):
        import struct
        _, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        return uid == os.getuid()
    if hasattr(connection, "getpeereid"):
        return connection.getpeereid()[0] == os.getuid()
    if sys.platform == "darwin":
        import struct
        credentials = connection.getsockopt(0, 1, 84)  # SOL_LOCAL, LOCAL_PEERCRED: struct xucred.
        return struct.unpack_from("I", credentials, 4)[0] == os.getuid()
    return True


def _peer_pid(connection) -> int | None:
    import socket
    import struct
    if hasattr(socket, "SO_PEERCRED"):
        return struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[0]
    if sys.platform == "darwin":
        return struct.unpack("i", connection.getsockopt(0, 2, 4))[0]  # LOCAL_PEERPID.
    return None


def _recv_exact(connection, size: int) -> bytes:
    body = bytearray()
    while len(body) < size:
        chunk = connection.recv(size - len(body))
        if not chunk:
            raise OSError("resident disconnected")
        body.extend(chunk)
    return bytes(body)


def _start(path: str) -> None:
    """An inherited flock spans startup and service, including concurrent misses."""
    import fcntl
    import stat
    directory = _directory()
    if _stopping_marker_active(directory):
        return
    guard = os.open(path + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        # Darwin's posix_spawn dup2(fd, fd) retains CLOEXEC; use a distinct source.
        if guard == 3:
            source = fcntl.fcntl(guard, fcntl.F_DUPFD_CLOEXEC, 4)
            os.close(guard)
            guard = source
        info = os.fstat(guard)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            return
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        # The endpoint key carries these flags, so the server must run with the same ones.
        flags = ["-B"] * bool(sys.flags.dont_write_bytecode) + ["-O"] * sys.flags.optimize
        code = (f"import sys;sys.path[:]={sys.path!r};import resident;"
                f"resident.serve({path!r},3)")
        null = os.open(os.devnull, os.O_RDWR)
        try:
            actions = [(os.POSIX_SPAWN_DUP2, null, fd) for fd in (0, 1, 2)]
            actions.append((os.POSIX_SPAWN_DUP2, guard, 3))
            if null > 3:
                actions.append((os.POSIX_SPAWN_CLOSE, null))
            os.posix_spawn(sys.executable, [sys.executable, *flags, "-c", code], dict(os.environ),
                           file_actions=actions, setsid=True)
        finally:
            os.close(null)
    finally:
        os.close(guard)


def _kill_group(pid: int, signum: int) -> None:
    try:
        os.killpg(pid, signum)
    except ProcessLookupError:
        pass


def _end_orphan(pid: int) -> None:
    """Without its server nothing else ends the child; SIGCONT lets a stopped one take SIGTERM."""
    import signal
    import time
    _kill_group(pid, signal.SIGTERM)
    _kill_group(pid, signal.SIGCONT)
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.01)
    _kill_group(pid, signal.SIGKILL)


def _client_handlers(pid: int) -> dict:
    """Terminal signals reach only the client; the child's group is in the server's session."""
    import signal

    def forward(signum, frame):
        _kill_group(pid, signum)

    def stop(signum, frame):
        _kill_group(pid, signal.SIGSTOP)
        signal.signal(signal.SIGTSTP, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTSTP)
        signal.signal(signal.SIGTSTP, stop)
        # Resumed, or never stopped because an orphaned group discards it: the child continues.
        _kill_group(pid, signal.SIGCONT)

    def resume(signum, frame):
        _kill_group(pid, signal.SIGCONT)

    handlers = {signum: forward for signum in
                (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)}
    handlers[signal.SIGTSTP] = stop
    handlers[signal.SIGCONT] = resume
    return handlers


def try_run(argv: list[str] | None = None) -> int | None:
    """Return None only before a command is acknowledged; never replay a command."""
    args = sys.argv[1:] if argv is None else argv
    if not eligible(args):
        return None
    started = False
    connection = None
    handlers = {}
    try:
        import stat
        path = socket_path()
        if not _endpoint_usable(path):
            return None
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            _start(path)
            return None
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            return None
        import socket
        import marshal
        import array
        import signal
        if not hasattr(socket, "SCM_RIGHTS"):
            return None
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(0.25)
        try:
            connection.connect(path)
        except (FileNotFoundError, ConnectionRefusedError):
            _start(path)
            return None
        if not _peer_ok(connection):
            return None
        streams = [(stream.encoding, stream.errors,
                    getattr(stream, "line_buffering", False),
                    getattr(stream, "write_through", False))
                   for stream in (sys.stdin, sys.stdout, sys.stderr)]
        mask = os.umask(0)
        os.umask(mask)
        body = marshal.dumps((
            [sys.argv[0], *args], os.getcwd(), dict(os.environ), streams, os.getpid(), mask))
        if len(body) > _MAX_REQUEST:
            return None
        packet = len(body).to_bytes(4, "big") + body
        sent = connection.sendmsg([packet], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                             array.array("i", [0, 1, 2]))])
        connection.sendall(packet[sent:])
        reply = _recv_exact(connection, 5)
        if reply[:1] != b"P":
            return None
        started = True
        pid = int.from_bytes(reply[1:], "big")
        for signum, handler in _client_handlers(pid).items():
            handlers[signum] = signal.signal(signum, handler)
        connection.sendall(b"G")
        connection.settimeout(None)
        reply = _recv_exact(connection, 6)
        if reply[:1] != b"R":
            raise OSError("invalid resident result")
        code = int.from_bytes(reply[1:5], "big")
        kind = reply[5]
        if kind == _SIGNALED:
            if not 0 < code < signal.NSIG:
                raise OSError("invalid resident signal")
            for signum, handler in handlers.items():
                signal.signal(signum, handler)
            handlers.clear()
            try:
                signal.signal(code, signal.SIG_DFL)
            except (OSError, ValueError):
                pass
            os.kill(os.getpid(), code)
            return 128 + code
        if kind not in (_REPORTED, _EXITED):
            raise OSError("invalid resident result")
        return code
    except Exception:
        if started:
            _end_orphan(pid)
            try:
                os.write(2, b"agrep: resident command disconnected without reporting\n")
            except OSError:
                pass
            return 1
        return None
    finally:
        if connection is not None:
            connection.close()
        for signum, handler in handlers.items():
            signal.signal(signum, handler)


def _preload():
    import importlib
    for name in PRELOAD_MODULES:
        importlib.import_module(name)
    import indexd_runtime
    indexd_runtime.derived_writer_build_id(require_binary=False)
    return sys.modules["cli"].main


def _environment_delta(before: dict[str, str]) -> tuple[dict[str, str], tuple[str, ...]]:
    after = dict(os.environ)
    return ({key: value for key, value in after.items() if before.get(key) != value},
            tuple(key for key in before if key not in after))


def _restore_caller(client_pid: int) -> None:
    # Agent identity belongs to the client's ancestry, not this detached server.
    context = sys.modules.get("session_context")
    if context is None:
        return
    found = None
    if context._private_directory(context.CALLER_PUBLICATION_DIR):
        proc = context.hookless_proc
        child_start = proc.process_start_time(client_pid)
        pid = proc.parent_pid(client_pid)
        seen = set()
        for _ in range(context.CALLER_ANCESTRY_MAX_DEPTH):
            if not pid or pid <= 1 or pid in seen:
                break
            seen.add(pid)
            parent_start = proc.process_start_time(pid)
            if child_start is not None and parent_start is not None and parent_start > child_start:
                break
            found = context.read_caller_publication(pid)
            if found is not None:
                break
            child_start = parent_start
            pid = proc.parent_pid(pid)
    context._PUBLISHED_CALLER_CACHE = ((context.CALLER_PUBLICATION_DIR, os.getppid()), found)


def _run_child(entry, request, fds, gate: int, result: int, close_fds, delta) -> None:
    import io
    import signal
    import time
    code = 1
    try:
        os.setpgid(0, 0)
        for descriptor in close_fds:
            if descriptor not in fds and descriptor not in (gate, result):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        signal.signal(signal.SIGINT, signal.default_int_handler)
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)
        signal.signal(signal.SIGTTOU, signal.SIG_IGN)
        for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
            signal.signal(signum, signal.SIG_DFL)
        if os.read(gate, 1) != b"G":
            os._exit(1)
        os.close(gate)
        argv, cwd, environ, streams, client_pid, mask = request
        exported, removed = delta
        os.umask(mask)
        os.environ.clear()
        os.environ.update(environ)
        # Replay what the preloaded modules did to the environment at import time.
        for key in removed:
            os.environ.pop(key, None)
        os.environ.update(exported)
        os.chdir(cwd)
        for target, descriptor in enumerate(fds):
            os.dup2(descriptor, target)
        for descriptor in fds:
            if descriptor > 2:
                os.close(descriptor)
        rebuilt = []
        for fd, (encoding, errors, line_buffering, write_through) in enumerate(streams):
            raw = io.FileIO(fd, "r" if fd == 0 else "w", closefd=False)
            if not write_through:
                raw = io.BufferedReader(raw) if fd == 0 else io.BufferedWriter(raw)
            rebuilt.append(io.TextIOWrapper(raw, encoding=encoding, errors=errors,
                                           line_buffering=line_buffering or os.isatty(fd),
                                           write_through=write_through))
        sys.stdin, sys.stdout, sys.stderr = rebuilt
        sys.__stdin__, sys.__stdout__, sys.__stderr__ = rebuilt
        sys.argv = argv
        _restore_caller(client_pid)
        os.environ["AGREP_T0"] = repr(time.perf_counter())
        console = sys.modules.get("console")
        if console is not None:
            console._PROFILE_T0 = time.perf_counter()
            console._PROFILE_LAPS.clear()
            console._DBG_T0 = time.time()
        logger = sys.modules.get("hookless._log")
        if logger is not None:
            logger._DBG_T0 = time.time()
        try:
            code = entry()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        except KeyboardInterrupt:
            code = 130
        code = (code or 0) & 255
        sys.stdout.flush()
        sys.stderr.flush()
        os.write(result, code.to_bytes(4, "big"))
    except BaseException:
        pass
    finally:
        os._exit(code)


def _valid_request(request) -> bool:
    if not isinstance(request, tuple) or len(request) != 6:
        return False
    argv, cwd, environ, streams, client_pid, mask = request
    return (isinstance(argv, list) and bool(argv) and all(isinstance(s, str) for s in argv)
            and isinstance(cwd, str) and isinstance(environ, dict)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in environ.items())
            and isinstance(streams, list) and len(streams) == 3
            and type(client_pid) is int and client_pid > 1
            and type(mask) is int and 0 <= mask <= 0o777 and eligible(argv[1:]))


def _serve_loop(listener, entry, guard: int, path: str, stamp: tuple, idle: float, delta) -> None:
    import array
    import marshal
    import selectors
    import signal
    import socket
    import time
    selector = selectors.DefaultSelector()
    selector.register(listener, selectors.EVENT_READ, None)
    clients = {}
    children = {}
    last_connection = time.monotonic()

    def close_client(state):
        connection = state["socket"]
        clients.pop(connection.fileno(), None)
        try:
            selector.unregister(connection)
        except (KeyError, ValueError):
            pass
        connection.close()
        for fd in state["fds"]:
            os.close(fd)
        state["fds"] = []
        if state.get("gate") is not None:
            os.close(state.pop("gate"))

    def lose_client(state):
        """A stopped child takes SIGTERM only once continued; SIGKILL follows if it outlives the deadline."""
        _kill_group(state["pid"], signal.SIGTERM)
        _kill_group(state["pid"], signal.SIGCONT)
        state["kill_at"] = time.monotonic() + 1

    try:
        while True:
            try:
                _directory(create=False)
                if _file_stamp(path, follow=False) != stamp:
                    break
            except OSError:
                break
            current = time.monotonic()
            if not clients and not children and current - last_connection >= idle:
                break
            for key, _ in selector.select(min(0.1, idle)):
                if key.fileobj is listener:
                    connection, _ = listener.accept()
                    connection.setblocking(False)
                    if not _peer_ok(connection):
                        connection.close()
                        continue
                    last_connection = time.monotonic()
                    state = {"socket": connection, "body": bytearray(), "fds": [],
                             "deadline": last_connection + 2, "pid": None}
                    clients[connection.fileno()] = state
                    selector.register(connection, selectors.EVENT_READ, state)
                    continue
                state = key.data
                if isinstance(key.fileobj, int):
                    result = os.read(key.fileobj, 4)
                    selector.unregister(key.fileobj)
                    os.close(key.fileobj)
                    pid = state["pid"]
                    _, status = os.waitpid(pid, 0)
                    children.pop(pid, None)
                    kind = _REPORTED
                    if len(result) != 4:
                        if os.WIFSIGNALED(status):
                            kind, code = _SIGNALED, os.WTERMSIG(status)
                        else:
                            kind, code = _EXITED, os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1
                        result = code.to_bytes(4, "big")
                    try:
                        state["socket"].sendall(b"R" + result + bytes([kind]))
                    except OSError:
                        pass
                    close_client(state)
                    continue
                connection = state["socket"]
                try:
                    if state["pid"] is not None:
                        data = connection.recv(1)
                        if data == b"G" and state.get("gate") is not None:
                            gate = state.pop("gate")
                            os.write(gate, b"G")
                            os.close(gate)
                        elif not data:
                            lose_client(state)
                            selector.unregister(connection)
                        continue
                    data, ancillary, flags, _ = connection.recvmsg(65536, socket.CMSG_SPACE(3 * 4))
                    for level, kind, body in ancillary:
                        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                            received = array.array("i")
                            received.frombytes(body[:len(body) - len(body) % received.itemsize])
                            state["fds"].extend(received)
                    if not data or flags & socket.MSG_CTRUNC:
                        close_client(state)
                        continue
                    state["body"].extend(data)
                    body = state["body"]
                    if body == b"STOP":
                        connection.sendall(b"OK")
                        close_client(state)
                        return
                    if len(body) < 4:
                        continue
                    size = int.from_bytes(body[:4], "big")
                    if size > _MAX_REQUEST or len(state["fds"]) > 3:
                        close_client(state)
                        continue
                    if len(body) < size + 4:
                        continue
                    request = marshal.loads(body[4:])
                    if (len(state["fds"]) != 3 or not _valid_request(request)
                            or _peer_pid(connection) != request[4]):
                        close_client(state)
                        continue
                    gate_read, gate_write = os.pipe()
                    result_read, result_write = os.pipe()
                    close_fds = [guard, listener.fileno(), gate_write, result_read, selector.fileno()]
                    close_fds.extend(clients)
                    close_fds.extend(child["result"] for child in children.values())
                    close_fds.extend(fd for pending in clients.values() for fd in pending["fds"])
                    close_fds.extend(child["gate"] for child in children.values() if "gate" in child)
                    pid = os.fork()
                    if pid == 0:
                        _run_child(entry, request, state["fds"], gate_read, result_write, close_fds, delta)
                    os.setpgid(pid, pid)
                    os.close(gate_read)
                    os.close(result_write)
                    for fd in state["fds"]:
                        os.close(fd)
                    state["fds"] = []
                    state.update(pid=pid, gate=gate_write, result=result_read)
                    children[pid] = state
                    selector.register(result_read, selectors.EVENT_READ, state)
                    connection.sendall(b"P" + pid.to_bytes(4, "big"))
                except (OSError, ValueError, EOFError, TypeError):
                    if state["pid"] is not None:
                        lose_client(state)
                    else:
                        close_client(state)
            now = time.monotonic()
            for state in list(clients.values()):
                if now > state["deadline"]:
                    if state["pid"] is None:
                        close_client(state)
                    elif "gate" in state:
                        os.close(state.pop("gate"))
                if now > state.get("kill_at", now):
                    del state["kill_at"]
                    _kill_group(state["pid"], signal.SIGKILL)
    finally:
        for pid in children:
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for state in list(clients.values()):
            close_client(state)
        deadline = time.monotonic() + 1
        for pid, state in children.items():
            os.close(state["result"])
            while os.waitpid(pid, os.WNOHANG) == (0, 0):
                if time.monotonic() >= deadline:
                    os.killpg(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                    break
                time.sleep(0.01)
        selector.close()


def _close_inherited(guard: int) -> None:
    """The spawner's stray descriptors must not outlive it in a server that idles for minutes."""
    for listing in ("/proc/self/fd", "/dev/fd"):
        try:
            names = os.listdir(listing)
        except OSError:
            continue
        for name in names:
            if name.isdigit() and int(name) > 2 and int(name) != guard:
                try:
                    os.close(int(name))
                except OSError:
                    pass
        return


def serve(path: str, guard: int) -> None:
    _close_inherited(guard)
    import socket
    import threading
    try:
        os.setsid()
    except PermissionError:
        pass
    listener = None
    stamp = None
    try:
        _directory(create=False)
        if os.stat(path + ".lock").st_ino != os.fstat(guard).st_ino:
            return
        if not _endpoint_usable(path) or path != socket_path():
            return
        idle = _idle_seconds()
        # Import reads only keyed variables; without the spawner's others, every import-time
        # export of an unkeyed variable is a visible delta rather than a coincidental match.
        for key in list(os.environ):
            if key not in RESIDENT_KEY_ENV:
                del os.environ[key]
        before = dict(os.environ)
        entry = _preload()
        delta = _environment_delta(before)
        if threading.active_count() != 1 or any(
                name == "numpy" or name == "mlx" or name.startswith("mlx.") for name in sys.modules):
            return
        directory = _directory(create=False)
        if _stopping_marker_active(directory):
            return
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        mask = os.umask(0o077)
        try:
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(path)
            os.chmod(path, 0o600)
        finally:
            os.umask(mask)
        stamp = _file_stamp(path, follow=False)
        listener.listen(64)
        _serve_loop(listener, entry, guard, path, stamp, idle, delta)
    finally:
        if listener is not None:
            listener.close()
        if stamp is not None and _file_stamp(path, follow=False) == stamp:
            os.unlink(path)
        os.close(guard)


def stop_servers() -> dict:
    """Stop this user's endpoints in the selected private runtime directory."""
    if sys.platform == "win32" or os.name != "posix":
        return {"ok": True, "stopped": []}
    import fcntl
    import socket
    import stat
    import time
    stopped = []
    locks = {}
    try:
        directory = _directory(create=False)
    except FileNotFoundError:
        return {"ok": True, "stopped": []}
    except OSError:
        return {"ok": False, "stopped": []}
    marker = os.path.join(directory, ".stopping")
    holder = _claim_stopping_marker(directory)
    if holder is None:
        return {"ok": False, "stopped": []}
    try:
        deadline = time.monotonic() + 3
        while True:
            busy = False
            with os.scandir(directory) as entries:
                paths = [entry.path for entry in entries]
            for path in paths:
                try:
                    info = os.lstat(path)
                except FileNotFoundError:
                    continue
                if stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid():
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                        connection.settimeout(1)
                        try:
                            connection.connect(path)
                        except (FileNotFoundError, ConnectionRefusedError):
                            if os.path.lexists(path):
                                os.unlink(path)
                            continue
                        if not _peer_ok(connection):
                            return {"ok": False, "stopped": stopped}
                        connection.sendall(b"STOP")
                        if _recv_exact(connection, 2) != b"OK":
                            return {"ok": False, "stopped": stopped}
                    stopped.append("resident CLI")
                elif path.endswith(".sock.lock") and path not in locks:
                    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        os.close(descriptor)
                        busy = True
                    else:
                        locks[path] = descriptor
            for path in paths:
                if not path.endswith(".sock"):
                    continue
                if os.path.exists(path):
                    busy = True
            if busy:
                if time.monotonic() >= deadline:
                    return {"ok": False, "stopped": stopped}
                time.sleep(0.02)
                continue
            break
        for path in locks:
            os.unlink(path)
        os.unlink(marker)
        os.rmdir(directory)
        return {"ok": True, "stopped": stopped}
    except OSError:
        return {"ok": False, "stopped": stopped}
    finally:
        for descriptor in locks.values():
            os.close(descriptor)
        try:
            os.unlink(marker)
        except FileNotFoundError:
            pass
        os.close(holder)
