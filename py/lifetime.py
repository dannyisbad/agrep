"""Bind one POSIX subprocess group to an indexd owner generation."""

from __future__ import annotations

import argparse
import errno
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402
import ownerfile  # noqa: E402


_TOKEN_RE = re.compile(r"[0-9a-f]{32}")
_OUTPUT_TAIL_BYTES = 64 * 1024
_FINAL_DRAIN_READS = 16


def _protected_target(path: Path) -> bool:
    protected = os.environ.get("AGREP_DATA_READONLY")
    if not protected:
        return False
    try:
        root = os.path.normcase(os.path.realpath(protected))
        target = os.path.normcase(os.path.realpath(os.fspath(path)))
        return os.path.commonpath((root, target)) == root
    except (OSError, ValueError):
        return True


def _group_present(group: int) -> bool | None:
    active = common._process_group_active(group)
    if active is not None:
        return active
    try:
        os.killpg(group, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return None


def _reaped_leader_occupied(group: int) -> bool:
    """After the guard reaps its leader, any pid==group occupant is a recycled
    stranger - and its existence proves the original group already ended."""
    return (common.pid_alive(group)
            or common.process_start_identity(group) is not None)


def _drain_group(
        group: int, *, grace_s: float = 1.0,
        reaped_leader: bool = False) -> bool:
    if reaped_leader and _reaped_leader_occupied(group):
        return True
    try:
        os.killpg(group, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        present = _group_present(group)
        if present is False:
            return True
        if present is None:
            return False
        time.sleep(0.02)
    if reaped_leader and _reaped_leader_occupied(group):
        return True
    try:
        os.killpg(group, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if _group_present(group) is False:
            return True
        time.sleep(0.02)
    return False


def _exec_after_gate(fd: int, command: list[str]) -> int:
    try:
        go = os.read(fd, 1)
    finally:
        os.close(fd)
    if go != b"1":
        print("index lifetime exec gate never opened", file=sys.stderr)
        return 125
    for name in ("SIGPIPE", "SIGXFZ", "SIGXFSZ"):
        inherited = getattr(signal, name, None)
        if inherited is not None:
            signal.signal(inherited, signal.SIG_DFL)
    try:
        os.execvpe(command[0], command, os.environ)
    except OSError as exc:
        print(f"index lifetime exec failed: {exc}", file=sys.stderr)
        return 127 if exc.errno == errno.ENOENT else 126
    return 126


def _parent_gone(fd: int) -> bool:
    selector = selectors.DefaultSelector()
    try:
        selector.register(fd, selectors.EVENT_READ)
        if not selector.select(0):
            return False
        return os.read(fd, 1) == b""
    finally:
        selector.close()


def _append_tail(tail: bytearray, data: bytes) -> None:
    tail.extend(data)
    excess = len(tail) - _OUTPUT_TAIL_BYTES
    if excess > 0:
        del tail[:excess]


def _read_tail(pipe, tail: bytearray) -> bool | None:
    try:
        data = os.read(pipe.fileno(), 8192)
    except BlockingIOError:
        return None
    except OSError:
        return False
    if not data:
        return False
    _append_tail(tail, data)
    return True


def _relay_tail(tail: bytearray, stream) -> None:
    if not tail:
        return
    try:
        stream.buffer.write(tail)
        stream.buffer.flush()
    except (AttributeError, OSError):
        pass


def _drain_owned_group(pid: int, expected_start: str | None) -> bool:
    if (expected_start is None
            or common.process_start_identity(pid) != expected_start):
        return False
    return _drain_group(pid)


def _guard(
        parent_fd: int, fence: Path, token: str, command: list[str]) -> int:
    if _TOKEN_RE.fullmatch(token) is None:
        print("index lifetime guard: malformed owner token", file=sys.stderr)
        return 125
    if (common.data_dir_readonly(common.DATA_DIR)
            or _protected_target(fence)):
        print(
            "index lifetime guard: AGREP_DATA_READONLY protects this data "
            "directory",
            file=sys.stderr)
        return 125
    stop = False

    def request_stop(_signum, _frame) -> None:
        nonlocal stop
        stop = True

    for sent in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sent, request_stop)
    gate_read, gate_write = os.pipe()
    process = None
    target_start = None
    fence_handle = None
    fence_removable = False
    stdout_tail = bytearray()
    stderr_tail = bytearray()
    try:
        # Guard-owned pipes keep bounded tails; the target never owns the
        # daemon-facing descriptors whose EOF controls the outer caller.
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()),
             "--exec-fd", str(gate_read), "--", *command],
            stdin=subprocess.DEVNULL, pass_fds=(gate_read,),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True)
        pipes = ((process.stdout, stdout_tail), (process.stderr, stderr_tail))
        for pipe, _tail in pipes:
            if pipe is not None:
                os.set_blocking(pipe.fileno(), False)
        os.close(gate_read)
        gate_read = -1
        guard_start = common.process_start_identity(os.getpid())
        target_start = common.process_start_identity(process.pid)
        if guard_start is None or target_start is None:
            print(
                "index lifetime guard: process start identity unavailable",
                file=sys.stderr)
            return 125
        raw = (
            f"owner={token} guard={os.getpid()} guard_start={guard_start} "
            f"target={process.pid} target_start={target_start} "
            f"group={process.pid}\n"
        ).encode("ascii")
        fence_handle = ownerfile.create_exclusive(
            fence, raw, mode=0o600, fsync=True, retain_fd=True)
        if _parent_gone(parent_fd):
            print(
                "index lifetime guard: parent daemon exited before start",
                file=sys.stderr)
            return 125
        os.write(gate_write, b"1")
        os.close(gate_write)
        gate_write = -1
        if process.poll() is None:
            selector = selectors.DefaultSelector()
            selector.register(parent_fd, selectors.EVENT_READ, None)
            for pipe, tail in pipes:
                if pipe is not None:
                    selector.register(pipe, selectors.EVENT_READ, tail)
            try:
                while process.poll() is None and not stop:
                    for key, _mask in selector.select(0.05):
                        if key.data is None:
                            if os.read(parent_fd, 1) == b"":
                                stop = True
                        elif _read_tail(key.fileobj, key.data) is False:
                            selector.unregister(key.fileobj)
            finally:
                selector.close()
        if stop:
            fence_removable = _drain_owned_group(
                process.pid, target_start)
            if not fence_removable and process.poll() is None:
                print(
                    "index lifetime guard: target survived stop drain",
                    file=sys.stderr)
                return 125
        code = process.wait()
        present = _group_present(process.pid)
        if present is False:
            fence_removable = True
        elif not _drain_group(process.pid, reaped_leader=True):
            print(
                "index lifetime guard: target group drain failed",
                file=sys.stderr)
            return 125
        else:
            fence_removable = True
        return code
    except (OSError, TypeError, ValueError) as exc:
        print(
            f"index lifetime guard failed: {type(exc).__name__}: {exc}",
            file=sys.stderr)
        return 125
    finally:
        for fd in (gate_read, gate_write, parent_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if process is not None:
            if process.poll() is None:
                fence_removable = _drain_owned_group(
                    process.pid, target_start)
                try:
                    process.wait(timeout=2.0)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            elif _group_present(process.pid) is not False:
                fence_removable = _drain_group(
                    process.pid, reaped_leader=True)
        if fence_handle is not None:
            try:
                if (fence_removable
                        and _group_present(process.pid) is False):
                    fence_handle.release(
                        tombstone=True, require_stable_mtime=True)
                else:
                    fence_handle.close()
            except OSError:
                pass
        if process is not None:
            for pipe, tail in (
                    (process.stdout, stdout_tail),
                    (process.stderr, stderr_tail)):
                if pipe is not None:
                    for _ in range(_FINAL_DRAIN_READS):
                        if _read_tail(pipe, tail) is not True:
                            break
                    pipe.close()
        _relay_tail(stdout_tail, sys.stdout)
        _relay_tail(stderr_tail, sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--exec-fd", type=int)
    parser.add_argument("--parent-fd", type=int)
    parser.add_argument("--fence")
    parser.add_argument("--owner-token")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        return 125
    if common.data_dir_readonly(common.DATA_DIR):
        print(
            "index lifetime guard: AGREP_DATA_READONLY protects this data "
            "directory",
            file=sys.stderr)
        return 125
    if args.exec_fd is not None:
        return _exec_after_gate(args.exec_fd, command)
    if (args.parent_fd is None or args.fence is None
            or args.owner_token is None):
        return 125
    return _guard(
        args.parent_fd, Path(args.fence), args.owner_token, command)


if __name__ == "__main__":
    raise SystemExit(main())
