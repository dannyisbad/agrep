"""Exact process observation and teardown primitives shared across agrep.

Liveness, kernel start identity, POSIX group census, exit witnesses, and the
exact-match terminate paths all live here so every coordination surface above
judges processes with one vocabulary. A PID is never trusted alone: ownership
decisions pair it with the kernel-recorded birth identity, and teardown only
ever signals a process (or group) whose identity still matches the descriptor
that named it.
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

WIN = sys.platform == "win32"
_MAX_PROCESS_ID = 0xFFFFFFFF if WIN else 0x7FFFFFFF


class _PosixProcessObservation(NamedTuple):
    state: str
    group: int | None


def _posix_state_is_zombie(state: str) -> bool:
    return state in ("Z", "X", "x")


_DARWIN_PROC_API = None


def _darwin_proc_api():
    global _DARWIN_PROC_API
    if _DARWIN_PROC_API is not None:
        return _DARWIN_PROC_API
    import ctypes

    class ProcBsdShortInfo(ctypes.Structure):
        _fields_ = [
            ("pid", ctypes.c_uint32), ("ppid", ctypes.c_uint32),
            ("pgid", ctypes.c_uint32), ("status", ctypes.c_uint32),
            ("comm", ctypes.c_char * 16), ("flags", ctypes.c_uint32),
            ("uid", ctypes.c_uint32), ("gid", ctypes.c_uint32),
            ("ruid", ctypes.c_uint32), ("rgid", ctypes.c_uint32),
            ("svuid", ctypes.c_uint32), ("svgid", ctypes.c_uint32),
            ("reserved", ctypes.c_uint32),
        ]

    class ProcBsdInfo(ctypes.Structure):
        _fields_ = [
            ("pbi_flags", ctypes.c_uint32),
            ("pbi_status", ctypes.c_uint32),
            ("pbi_xstatus", ctypes.c_uint32),
            ("pbi_pid", ctypes.c_uint32),
            ("pbi_ppid", ctypes.c_uint32),
            ("pbi_uid", ctypes.c_uint32),
            ("pbi_gid", ctypes.c_uint32),
            ("pbi_ruid", ctypes.c_uint32),
            ("pbi_rgid", ctypes.c_uint32),
            ("pbi_svuid", ctypes.c_uint32),
            ("pbi_svgid", ctypes.c_uint32),
            ("rfu_1", ctypes.c_uint32),
            ("pbi_comm", ctypes.c_char * 16),
            ("pbi_name", ctypes.c_char * 32),
            ("pbi_nfiles", ctypes.c_uint32),
            ("pbi_pgid", ctypes.c_uint32),
            ("pbi_pjobc", ctypes.c_uint32),
            ("e_tdev", ctypes.c_uint32),
            ("e_tpgid", ctypes.c_uint32),
            ("pbi_nice", ctypes.c_int32),
            ("pbi_start_tvsec", ctypes.c_uint64),
            ("pbi_start_tvusec", ctypes.c_uint64),
        ]

    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    pidinfo = library.proc_pidinfo
    pidinfo.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
        ctypes.c_void_p, ctypes.c_int]
    pidinfo.restype = ctypes.c_int
    listpids = library.proc_listpids
    listpids.argtypes = [
        ctypes.c_uint32, ctypes.c_uint32,
        ctypes.c_void_p, ctypes.c_int]
    listpids.restype = ctypes.c_int
    _DARWIN_PROC_API = (
        ctypes, library, ProcBsdShortInfo, ProcBsdInfo, pidinfo, listpids)
    return _DARWIN_PROC_API


def _observe_posix_process(pid: int) -> _PosixProcessObservation:
    if sys.platform == "darwin":
        try:
            ctypes, _, short_info, _, pidinfo, _ = _darwin_proc_api()
            info = short_info()
            ctypes.set_errno(0)
            got = pidinfo(
                pid, 13, 0, ctypes.byref(info), ctypes.sizeof(info))
            if got == ctypes.sizeof(info):
                state = (
                    "zombie" if info.status == 5 else
                    "stopped" if info.status == 4 else "active")
                return _PosixProcessObservation(state, int(info.pgid))
            if ctypes.get_errno() == errno.ESRCH:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return _PosixProcessObservation("gone", None)
                except OSError:
                    return _PosixProcessObservation("unknown", None)
                try:
                    group = os.getpgid(pid)
                except ProcessLookupError:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        return _PosixProcessObservation("gone", None)
                    except OSError:
                        return _PosixProcessObservation("unknown", None)
                    return _PosixProcessObservation("zombie", None)
                except OSError:
                    return _PosixProcessObservation("unknown", None)
                return _PosixProcessObservation("unknown", group)
            return _PosixProcessObservation("unknown", None)
        except (AttributeError, OSError, OverflowError, ValueError):
            return _PosixProcessObservation("unknown", None)
    if sys.platform.startswith("linux"):
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(
                encoding="ascii", errors="replace")
        except FileNotFoundError:
            return _PosixProcessObservation("gone", None)
        except OSError:
            return _PosixProcessObservation("unknown", None)
        fields = raw[raw.rfind(")") + 2:].split()
        try:
            state, group = fields[0], int(fields[2])
        except (IndexError, ValueError):
            return _PosixProcessObservation("unknown", None)
        return _PosixProcessObservation(
            "zombie" if _posix_state_is_zombie(state) else
            "stopped" if state in ("T", "t") else "active", group)
    return _PosixProcessObservation("unknown", None)


def process_execution_state(pid: int) -> str:
    if WIN:
        return "unknown"
    return _observe_posix_process(pid).state


def _windows_open_failure_is_alive(error: int) -> bool:
    return error != 87


def _darwin_cross_uid_identity(
        uid: int, *, current_uid: int | None = None) -> str | None:
    """A different uid proves a recycled PID even when birth time is private."""
    own_uid = os.geteuid() if current_uid is None else current_uid
    return f"darwin_uid_{uid}" if uid != own_uid else None


def pid_alive(pid: int) -> bool:
    """Whether this PID can still execute; zombies are exited, ambiguity is live."""
    if pid <= 0 or pid > _MAX_PROCESS_ID:
        return False
    if WIN:
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # argtypes matter: without them ctypes passes the 64-bit HANDLE as a 32-bit int,
        # truncating it. GetExitCodeProcess (not WaitForSingleObject) because the query
        # access we hold doesn't include SYNCHRONIZE.
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return _windows_open_failure_is_alive(ctypes.get_last_error())
        try:
            code = wintypes.DWORD()
            if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return code.value == STILL_ACTIVE  # still running (vs a real exit code)
            return True  # couldn't query the code -> assume alive (conservative)
        finally:
            k32.CloseHandle(h)
    observed = _observe_posix_process(pid)
    if observed.state in ("zombie", "gone"):
        return False
    if observed.state == "active":
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True  # EPERM (exists, not ours) or anything unexpected -> assume alive


def process_start_identity(pid: int) -> str | None:
    """Kernel start identity, so a recycled PID cannot inherit file ownership."""
    if pid <= 0 or pid > _MAX_PROCESS_ID:
        return None
    if pid == os.getpid():
        cached = getattr(process_start_identity, "_self_identity", None)
        if (isinstance(cached, tuple) and len(cached) == 2
                and cached[0] == pid):
            return cached[1]
    if WIN:
        import ctypes

        class FILETIME(ctypes.Structure):
            _fields_ = (("low", ctypes.c_ulong), ("high", ctypes.c_ulong))

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        open_process.restype = ctypes.c_void_p
        handle = open_process(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        created, exited, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        try:
            ok = kernel32.GetProcessTimes(
                ctypes.c_void_p(handle), ctypes.byref(created), ctypes.byref(exited),
                ctypes.byref(kernel), ctypes.byref(user))
            if not ok:
                return None
            identity = f"win_{(int(created.high) << 32) | int(created.low)}"
            if pid == os.getpid():
                process_start_identity._self_identity = (pid, identity)
            return identity
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    if sys.platform == "darwin":
        # libproc's BSD info carries the kernel-recorded process birth time. PID
        # liveness alone is insufficient for long-lived lock/port descriptors: a
        # recycled PID must not inherit an old worker's ownership.
        try:
            ctypes, _, short_info, bsd_info, pidinfo, _ = _darwin_proc_api()
            info = bsd_info()
            got = pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
            if got == ctypes.sizeof(info) and info.pbi_start_tvsec:
                identity = (f"darwin_{int(info.pbi_start_tvsec)}_"
                            f"{int(info.pbi_start_tvusec)}")
                if pid == os.getpid():
                    process_start_identity._self_identity = (pid, identity)
                return identity
            short = short_info()
            got = pidinfo(
                pid, 13, 0, ctypes.byref(short), ctypes.sizeof(short))
            if got == ctypes.sizeof(short):
                return _darwin_cross_uid_identity(int(short.uid))
        except (AttributeError, OSError, OverflowError, ValueError):
            pass
        return None
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        fields = raw[raw.rfind(")") + 2:].split()
        identity = f"proc_{fields[19]}"
        if pid == os.getpid():
            process_start_identity._self_identity = (pid, identity)
        return identity
    except (OSError, IndexError):
        return None


_WINDOWS_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_WINDOWS_JOB_BREAKAWAY_OK = 0x00000800
_WINDOWS_JOB_KILL_ON_CLOSE = 0x00002000
_WINDOWS_DESCENDANT_JOB_LIMITS = (
    _WINDOWS_JOB_BREAKAWAY_OK | _WINDOWS_JOB_KILL_ON_CLOSE)
WINDOWS_DESCENDANT_TREE = "global-job-v1"
_DESCENDANT_JOB_HANDLE = None
_DESCENDANT_LIFETIME_BOUND = False
_DESCENDANT_LIFETIME_PID = None


def windows_detached_child_flags(base: int) -> int:
    """Let deliberate detached work escape only agrep's own descendant Job."""
    if (WIN and _DESCENDANT_JOB_HANDLE is not None
            and _DESCENDANT_LIFETIME_BOUND
            and _DESCENDANT_LIFETIME_PID == os.getpid()):
        return int(base) | _WINDOWS_CREATE_BREAKAWAY_FROM_JOB
    return int(base)


def _windows_current_process_in_job() -> bool | None:
    """Return Windows Job membership, or None when the kernel cannot prove it."""
    if not WIN:
        return False
    import ctypes
    from ctypes import wintypes

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.argtypes = ()
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.IsProcessInJob.argtypes = (
            wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
        kernel32.IsProcessInJob.restype = wintypes.BOOL
        in_job = wintypes.BOOL()
        if not kernel32.IsProcessInJob(
                kernel32.GetCurrentProcess(), None, ctypes.byref(in_job)):
            return None
        return bool(in_job.value)
    except (AttributeError, OSError):
        return None


def windows_background_child_flags(base: int) -> int:
    """Keep durable background work independent of the invoking Windows Job."""
    flags = windows_detached_child_flags(base)
    if not WIN or flags & _WINDOWS_CREATE_BREAKAWAY_FROM_JOB:
        return flags
    if _windows_current_process_in_job() is not False:
        flags |= _WINDOWS_CREATE_BREAKAWAY_FROM_JOB
    return flags


def bind_descendants_to_process_lifetime() -> bool:
    """Establish the platform lifetime boundary used by resident owners."""
    global _DESCENDANT_JOB_HANDLE
    global _DESCENDANT_LIFETIME_BOUND, _DESCENDANT_LIFETIME_PID
    pid = os.getpid()
    if not WIN:
        try:
            _DESCENDANT_LIFETIME_BOUND = (
                os.getpgrp() == pid and os.getsid(0) == pid)
            _DESCENDANT_LIFETIME_PID = (
                pid if _DESCENDANT_LIFETIME_BOUND else None)
            return _DESCENDANT_LIFETIME_BOUND
        except OSError:
            _DESCENDANT_LIFETIME_BOUND = False
            _DESCENDANT_LIFETIME_PID = None
            return False
    if _DESCENDANT_JOB_HANDLE is not None:
        valid = (
            _DESCENDANT_LIFETIME_BOUND
            and _DESCENDANT_LIFETIME_PID == pid)
        _DESCENDANT_LIFETIME_BOUND = valid
        if not valid:
            _DESCENDANT_LIFETIME_PID = None
        return valid
    _DESCENDANT_LIFETIME_BOUND = False
    _DESCENDANT_LIFETIME_PID = None
    process_start = process_start_identity(pid)
    if process_start is None:
        return False
    import winjob

    handle = winjob.bind_current(
        pid, process_start, _WINDOWS_DESCENDANT_JOB_LIMITS)
    if handle is None:
        return False
    _DESCENDANT_JOB_HANDLE = handle
    _DESCENDANT_LIFETIME_BOUND = True
    _DESCENDANT_LIFETIME_PID = pid
    return True


class DescendantLifetime(NamedTuple):
    group: str
    tree: str | None


def descendant_lifetime_contract() -> DescendantLifetime | None:
    """Return the verified boundary owned by this process."""
    if (not _DESCENDANT_LIFETIME_BOUND
            or _DESCENDANT_LIFETIME_PID != os.getpid()):
        return None
    return DescendantLifetime(
        group="job" if WIN else str(os.getpgrp()),
        tree=WINDOWS_DESCENDANT_TREE if WIN else None,
    )


def _process_group_present(process_group: int) -> bool | None:
    if WIN or process_group <= 0:
        return False
    try:
        os.killpg(process_group, 0)
        return True
    except ProcessLookupError:
        return False
    except (OSError, OverflowError):
        return None


def _process_group_live_member_pids(
        process_group: int) -> frozenset[int] | None:
    """List executable members so an exited leader cannot transfer group ownership."""
    if WIN or process_group <= 0:
        return frozenset()
    if sys.platform == "darwin":
        proc_pgrp_only = 2

        try:
            ctypes, _, _, _, _, listpids = _darwin_proc_api()
            ctypes.set_errno(0)
            needed = listpids(
                proc_pgrp_only, process_group, None, 0)
            if needed < 0 or (needed == 0 and ctypes.get_errno()):
                return None
            slots = max(64, min(65_536, needed // ctypes.sizeof(ctypes.c_int) + 32))
            pids = (ctypes.c_int * slots)()
            ctypes.set_errno(0)
            used = listpids(
                proc_pgrp_only, process_group, pids, ctypes.sizeof(pids))
            if (used < 0 or (used == 0 and ctypes.get_errno())
                    or used >= ctypes.sizeof(pids)
                    or used % ctypes.sizeof(ctypes.c_int)):
                return None
            if used == 0:
                return (
                    frozenset()
                    if _process_group_present(process_group) is False
                    else None)
            live = set()
            matched = False
            for pid in pids[:used // ctypes.sizeof(ctypes.c_int)]:
                if pid <= 0:
                    continue
                observed = _observe_posix_process(pid)
                if observed.state == "gone":
                    continue
                if observed.state == "unknown":
                    return None
                if observed.state == "zombie":
                    matched = True
                    continue
                if observed.group != process_group:
                    continue
                matched = True
                live.add(pid)
            if matched:
                return frozenset(live)
            return (
                frozenset()
                if _process_group_present(process_group) is False
                else None)
        except (AttributeError, OSError, OverflowError, ValueError):
            return None
    if sys.platform.startswith("linux"):
        try:
            entries = os.scandir("/proc")
        except OSError:
            return None
        matched = False
        live = set()
        with entries:
            for entry in entries:
                if not entry.name.isdecimal():
                    continue
                try:
                    raw = Path(entry.path, "stat").read_text(
                        encoding="ascii", errors="replace")
                except FileNotFoundError:
                    continue
                except OSError:
                    return None
                fields = raw[raw.rfind(")") + 2:].split()
                try:
                    state, group = fields[0], int(fields[2])
                except (IndexError, ValueError):
                    return None
                if group == process_group:
                    matched = True
                    if state != "Z":
                        live.add(int(entry.name))
        if matched or _process_group_present(process_group) is False:
            return frozenset(live)
        return None
    return None


def _process_group_has_live_members(process_group: int) -> bool | None:
    """Distinguish live members from zombies when a dead leader keeps the PGID."""
    members = _process_group_live_member_pids(process_group)
    return None if members is None else bool(members)


def _process_group_active(process_group: int) -> bool | None:
    present = _process_group_present(process_group)
    if present is False:
        return False
    live = _process_group_has_live_members(process_group)
    return live if live is not None else None


def _owner_group_active(
        process_group: int, owner_pid: int,
        owner_reused: bool) -> bool | None:
    """Probe a leader-numbered group only while its recorded leader may hold it.

    Proof of death is terminal. A reissued PID proves the leader-numbered group
    already emptied, because no kernel reissues a PID while a process group
    still names it; every occupant of that number now is a recycled stranger's,
    so no probe on it may raise the uncertainty a start-identity proof settled.
    """
    if owner_reused and process_group == owner_pid:
        return False
    return _process_group_active(process_group)


class _ProcessExitWitness:
    __slots__ = ("_fd", "_pid", "_queue", "_seen")

    def __init__(self, pid: int):
        self._fd = None
        self._pid = pid
        self._queue = None
        self._seen = False
        if sys.platform == "darwin":
            import select

            queue = None
            try:
                queue = select.kqueue()
                event = select.kevent(
                    pid, filter=select.KQ_FILTER_PROC,
                    flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE,
                    fflags=select.KQ_NOTE_EXIT)
                queue.control([event], 0, 0)
                self._queue = queue
            except (AttributeError, OSError, ValueError):
                if queue is not None:
                    try:
                        queue.close()
                    except OSError:
                        pass
        elif sys.platform.startswith("linux"):
            pidfd_open = getattr(os, "pidfd_open", None)
            if pidfd_open is not None:
                try:
                    self._fd = pidfd_open(pid, 0)
                except OSError:
                    pass

    @property
    def registered(self) -> bool:
        return self._queue is not None or self._fd is not None

    def exited(self) -> bool:
        if self._seen:
            return True
        if self._queue is not None:
            import select

            try:
                events = self._queue.control(None, 1, 0)
            except (OSError, ValueError):
                return False
            self._seen = any(
                event.ident == self._pid
                and event.filter == select.KQ_FILTER_PROC
                and not event.flags & select.KQ_EV_ERROR
                and event.fflags & select.KQ_NOTE_EXIT
                for event in events)
        elif self._fd is not None:
            import select

            try:
                poller = select.poll()
                poller.register(
                    self._fd, select.POLLIN | select.POLLHUP | select.POLLERR)
                self._seen = any(
                    fd == self._fd and mask & select.POLLIN
                    for fd, mask in poller.poll(0))
            except (OSError, ValueError):
                return False
        return self._seen

    def close(self) -> None:
        if self._queue is not None:
            try:
                self._queue.close()
            except OSError:
                pass
            self._queue = None
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None


class _WatchedGroupMember(NamedTuple):
    pid: int
    process_start: str
    exit_witness: _ProcessExitWitness


_MAX_GROUP_MEMBER_WITNESSES = 128


def _watch_process_group_members(
        process_group: int, process_session: int,
        root_pid: int) -> list[_WatchedGroupMember]:
    """Keep birth-bound survivors available if the group leader exits first."""
    members = _process_group_live_member_pids(process_group)
    if members is None:
        return []
    watched = []
    for member_pid in sorted(members):
        if len(watched) >= _MAX_GROUP_MEMBER_WITNESSES:
            break
        if member_pid == root_pid:
            continue
        process_start = process_start_identity(member_pid)
        if process_start is None:
            continue
        witness = _ProcessExitWitness(member_pid)
        if not witness.registered or witness.exited():
            witness.close()
            continue
        observed = _observe_posix_process(member_pid)
        try:
            member_session = os.getsid(member_pid)
        except OSError:
            member_session = None
        if (observed.state != "active"
                or observed.group != process_group
                or member_session != process_session
                or process_start_identity(member_pid) != process_start
                or witness.exited()):
            witness.close()
            continue
        watched.append(_WatchedGroupMember(
            member_pid, process_start, witness))
    return watched


def _original_group_survivor(
        watched: list[_WatchedGroupMember],
        process_group: int, process_session: int) -> bool:
    """Prove the numeric PGID is still anchored by a member seen before TERM."""
    for member in watched:
        if member.exit_witness.exited():
            continue
        if process_start_identity(member.pid) != member.process_start:
            continue
        observed = _observe_posix_process(member.pid)
        try:
            member_session = os.getsid(member.pid)
        except OSError:
            continue
        if (observed.state == "active"
                and observed.group == process_group
                and member_session == process_session
                and process_start_identity(member.pid) == member.process_start
                and not member.exit_witness.exited()):
            return True
    return False


def terminate_exact_process(pid: int, expected_start: str) -> bool:
    """Terminate only the process whose kernel birth identity matches the descriptor.

    This is used solely to retire an old resident agrep component after an upgrade;
    a recycled PID or unverifiable identity is never signalled.
    """
    if (pid <= 0 or pid > _MAX_PROCESS_ID or pid == os.getpid()
            or not expected_start):
        return False
    if WIN:
        import ctypes

        class FILETIME(ctypes.Structure):
            _fields_ = (("low", ctypes.c_ulong), ("high", ctypes.c_ulong))

        access = 0x0001 | 0x1000 | 0x00100000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(access, False, pid)
        if not handle:
            return False
        created, exited, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        try:
            if not kernel32.GetProcessTimes(
                    ctypes.c_void_p(handle), ctypes.byref(created), ctypes.byref(exited),
                    ctypes.byref(kernel), ctypes.byref(user)):
                return False
            actual = f"win_{(int(created.high) << 32) | int(created.low)}"
            if actual != expected_start:
                return False
            terminated = bool(kernel32.TerminateProcess(ctypes.c_void_p(handle), 0))
            return terminated
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    if process_start_identity(pid) != expected_start:
        return False
    try:
        import signal
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def terminate_exact_process_tree(
        pid: int, expected_start: str, *, wait_s: float = 5.0,
        require_bound_tree: bool = False,
        term_grace_s: float | None = None) -> bool:
    """Terminate one exact owner and drain its private process group when safe."""
    if (pid <= 0 or pid > _MAX_PROCESS_ID or pid == os.getpid()
            or expected_start in ("", "None", "unknown")):
        return False

    def owner_state() -> str:
        if not pid_alive(pid):
            return "gone"
        actual = process_start_identity(pid)
        if actual is None:
            return "unverifiable"
        return "exact" if actual == expected_start else "reused"

    wait_s = max(0.0, float(wait_s))
    deadline = time.monotonic() + wait_s
    state = owner_state()

    if WIN and require_bound_tree:
        if state != "exact":
            return False
        import winjob

        tree = winjob.open_exact(pid, expected_start)
        if tree is None:
            return False
        try:
            return tree.terminate_and_wait(
                max(0.0, deadline - time.monotonic()))
        finally:
            tree.close()

    def wait_for_root() -> bool:
        while True:
            current = owner_state()
            if current in ("gone", "reused"):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)

    if WIN:
        if state in ("gone", "reused"):
            return True
        if state != "exact":
            return False
        if not terminate_exact_process(pid, expected_start):
            return owner_state() in ("gone", "reused")
        return wait_for_root()

    import signal

    def bound_group_drained(current: str, process_group: int) -> bool:
        return (
            current in ("gone", "reused")
            and _process_group_active(process_group) is False)

    if state != "exact":
        if not require_bound_tree:
            return state in ("gone", "reused")
        return bound_group_drained(state, pid)

    try:
        process_group = os.getpgid(pid)
        process_session = os.getsid(pid)
        caller_group = os.getpgrp()
    except ProcessLookupError:
        if require_bound_tree:
            return bound_group_drained(owner_state(), pid)
        return owner_state() in ("gone", "reused")
    except OSError:
        return False
    state = owner_state()
    if state != "exact":
        if require_bound_tree:
            return bound_group_drained(state, pid)
        return state in ("gone", "reused")
    group_safe = (
        process_group == process_session == pid
        and process_group != caller_group
    )
    if require_bound_tree and not group_safe:
        return False
    witness = _ProcessExitWitness(pid) if group_safe else None

    if witness is not None:
        state = owner_state()
        try:
            topology_stable = (
                os.getpgid(pid) == process_group
                and os.getsid(pid) == process_session)
        except OSError:
            topology_stable = False
        if state != "exact" or not topology_stable:
            witness.close()
            if require_bound_tree:
                return bound_group_drained(state, process_group)
            return state in ("gone", "reused")
    watched = (
        _watch_process_group_members(
            process_group, process_session, pid)
        if group_safe else [])
    reaped_root = False

    def reap_exited_direct_child() -> bool:
        nonlocal reaped_root
        if reaped_root:
            return True
        if owner_state() not in ("exact", "unverifiable"):
            return False
        try:
            waited, _ = os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            return False
        reaped_root = waited == pid
        return reaped_root

    def await_exited_direct_child() -> bool:
        retry_deadline = min(deadline, time.monotonic() + 0.1)
        while True:
            if reap_exited_direct_child():
                return True
            if (owner_state() != "unverifiable"
                    or time.monotonic() >= retry_deadline):
                return False
            time.sleep(0.01)

    def witnessed_exit() -> bool:
        if reaped_root:
            return True
        if witness is not None and witness.exited():
            return True
        waitid = getattr(os, "waitid", None)
        p_pid = getattr(os, "P_PID", None)
        exited = getattr(os, "WEXITED", None)
        nohang = getattr(os, "WNOHANG", None)
        nowait = getattr(os, "WNOWAIT", None)
        if None not in (waitid, p_pid, exited, nohang, nowait):
            try:
                info = waitid(p_pid, pid, exited | nohang | nowait)
                if info is not None and getattr(info, "si_pid", 0) == pid:
                    return True
            except (ChildProcessError, OSError):
                pass
        return False

    def root_exited() -> bool:
        if witnessed_exit():
            return True
        return owner_state() in ("gone", "reused")

    def drained() -> bool:
        if group_safe:
            active = _process_group_active(process_group)
            return active is False
        return root_exited()

    def drain_ambiguous_zombie() -> bool:
        if witnessed_exit():
            reap_exited_direct_child()
        elif not await_exited_direct_child():
            return False
        return drained()

    try:
        try:
            if group_safe:
                state = owner_state()
                try:
                    topology_stable = (
                        os.getpgid(pid) == process_group
                        and os.getsid(pid) == process_session)
                except OSError:
                    topology_stable = False
                if (state != "exact" or not topology_stable
                        or (witness is not None and witness.exited())):
                    return False
                os.killpg(process_group, signal.SIGTERM)
            else:
                os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return drained()
        except OSError:
            return False if group_safe else root_exited()

        grace = (min(1.0, wait_s / 2.0) if term_grace_s is None
                 else min(wait_s, max(0.0, float(term_grace_s))))
        grace_deadline = min(deadline, time.monotonic() + grace)
        while not drained() and time.monotonic() < grace_deadline:
            time.sleep(0.02)
        if drained():
            return True
        if drain_ambiguous_zombie():
            return True
        if time.monotonic() >= deadline:
            return False

        try:
            if group_safe:
                state = owner_state()
                if state == "reused":
                    return False
                if state != "exact" and not witnessed_exit():
                    if not await_exited_direct_child():
                        return False
                active = _process_group_active(process_group)
                if active is False:
                    return True
                if active is None:
                    return False
                state = owner_state()
                if state == "exact":
                    try:
                        topology_stable = (
                            os.getpgid(pid) == process_group
                            and os.getsid(pid) == process_session)
                    except OSError:
                        topology_stable = False
                    if (not topology_stable
                            or (witness is not None and witness.exited())):
                        return False
                elif (
                        state == "reused"
                        or not witnessed_exit()
                        or not _original_group_survivor(
                            watched, process_group, process_session)):
                    return False
                os.killpg(process_group, signal.SIGKILL)
            else:
                state = owner_state()
                if state == "exact":
                    os.kill(pid, signal.SIGKILL)
                else:
                    return (
                        state in ("gone", "reused")
                        or await_exited_direct_child())
        except ProcessLookupError:
            return drained()
        except OSError:
            return drained()

        while not drained():
            if time.monotonic() >= deadline:
                return drain_ambiguous_zombie()
            time.sleep(0.02)
        return True
    finally:
        if witness is not None:
            witness.close()
        for member in watched:
            member.exit_witness.close()


def process_rss_bytes(pid: int) -> int | None:
    """Best-effort current private working set for diagnostics/resource policy."""
    if pid <= 0 or not pid_alive(pid):
        return None
    if WIN:
        import ctypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("page_faults", ctypes.c_ulong),
                        ("peak_working_set", ctypes.c_size_t),
                        ("working_set", ctypes.c_size_t),
                        ("quota_peak_paged", ctypes.c_size_t),
                        ("quota_paged", ctypes.c_size_t),
                        ("quota_peak_nonpaged", ctypes.c_size_t),
                        ("quota_nonpaged", ctypes.c_size_t),
                        ("pagefile", ctypes.c_size_t),
                        ("peak_pagefile", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        handle = k32.OpenProcess(0x1000 | 0x0010, False, pid)
        if not handle:
            return None
        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        try:
            if psapi.GetProcessMemoryInfo(
                    ctypes.c_void_p(handle), ctypes.byref(counters), counters.cb):
                return int(counters.working_set)
            return None
        finally:
            k32.CloseHandle(ctypes.c_void_p(handle))
    if sys.platform == "darwin":
        import ctypes

        class RusageInfoV2(ctypes.Structure):
            _fields_ = (("uuid", ctypes.c_ubyte * 16), ("user", ctypes.c_uint64),
                        ("system", ctypes.c_uint64), ("pkg", ctypes.c_uint64),
                        ("interrupt", ctypes.c_uint64), ("pageins", ctypes.c_uint64),
                        ("wired", ctypes.c_uint64), ("resident", ctypes.c_uint64),
                        ("footprint", ctypes.c_uint64),
                        ("tail", ctypes.c_uint64 * 10))

        try:
            fn = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True).proc_pid_rusage
            fn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
            fn.restype = ctypes.c_int
            info = RusageInfoV2()
            return int(info.resident) if fn(pid, 2, ctypes.byref(info)) == 0 else None
        except (AttributeError, OSError):
            return None
    try:
        result = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)],
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=2)
        return int(result.stdout.strip().split()[0]) * 1024 if result.returncode == 0 else None
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None
