#!/usr/bin/env python3
"""Isolated, cross-platform process-tree resource gate.

The benchmark creates a production-shaped 4,600-file / 14,000-message store and
measures three distinct workloads:

* cold Rust ingest: peak process-tree RSS/handles and total CPU time;
* the headless freshness daemon after it is observably idle: process-tree CPU,
  RSS, and handles;
* a real 128-row mixed-length ONNX embedding batch, then a resident semantic
  worker query over a 14,000-row synthetic matrix, when the optional runtime and
  already-downloaded pinned model exist.

No writer is ever pointed at the user's corpus, and this gate never downloads a
model.  ``--check`` requires the portable ingest/idle lanes.  Add
``--check-semantic`` when CI or a release host is provisioned for semantics.
Budgets can be multiplied with ``AGREP_RESOURCE_SLACK`` or overridden one at a
time with ``AGREP_RESOURCE_<UPPERCASE_METRIC_NAME>``.
"""

from __future__ import annotations

import argparse
import ctypes
import functools
import http.client
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
PY = REPO / "py"
FILES = 4_600
ROWS = 14_000
SEMANTIC_ROWS = 14_000
SEMANTIC_DIM = 384
SEMANTIC_BATCH_ROWS = 128
SEMANTIC_DEADLINE_HEADER = "X-Agrep-Deadline-Monotonic-Ns"
_IDLE_SOURCE_AGE_S = 3600.0
_STOP_TERM_TIMEOUT_S = 2.0
_STOP_KILL_TIMEOUT_S = 5.0
_PRIVATE_GROUP_ATTR = "_agrep_resource_private_group"
_WRITER_ENV_KEYS = (
    "AGREP_RUNTIME_BUILD_ID",
    "AGREP_PYTHON_RUNTIME_BUILD_ID",
    "AGREP_DERIVED_ADOPTION_OWNER_TOKEN",
    "AGREP_DERIVED_WRITER_IDENTITY_BLOCKED",
)

# Ceilings absorb allocator/runtime variation but catch architectural regressions
# (model in indexd, leaked FDs); tree-summed RSS may count shared pages per process.
BUDGETS = {
    "idle_cpu_percent": 5.0,       # percent of one logical CPU
    "rss_mib": 384.0,              # idle indexd tree; legacy key retained
    # Idle indexd tree: FDs on POSIX (measured single-digit); Windows counts
    # kernel objects (threads, events, keys - onnxruntime's pool alone is
    # dozens), first real Windows run measured 310 with green RSS/CPU.
    "handles": 256 if os.name != "nt" else 512,
    "ingest_peak_rss_mib": 512.0,
    "ingest_peak_handles": 512,
    "semantic_resident_rss_mib": 512.0,
    "semantic_peak_rss_mib": 768.0,
    "semantic_peak_handles": 512,
    "semantic_idle_cpu_percent": 5.0,
    "semantic_batch_wall_ms": 5_000.0,
    "semantic_query_cpu_ms": 500.0,
}
PORTABLE_BUDGETS = frozenset({
    "idle_cpu_percent", "rss_mib", "handles",
    "ingest_peak_rss_mib", "ingest_peak_handles",
})
SEMANTIC_BUDGETS = frozenset(BUDGETS) - PORTABLE_BUDGETS


class _MacRusageV2(ctypes.Structure):
    _fields_ = (("uuid", ctypes.c_ubyte * 16), ("user_ns", ctypes.c_uint64),
                ("system_ns", ctypes.c_uint64), ("pkg_idle", ctypes.c_uint64),
                ("interrupt", ctypes.c_uint64), ("pageins", ctypes.c_uint64),
                ("wired", ctypes.c_uint64), ("resident", ctypes.c_uint64),
                ("footprint", ctypes.c_uint64), ("start_abs", ctypes.c_uint64),
                ("exit_abs", ctypes.c_uint64), ("child_user", ctypes.c_uint64),
                ("child_system", ctypes.c_uint64), ("child_pkg_idle", ctypes.c_uint64),
                ("child_interrupt", ctypes.c_uint64), ("child_pageins", ctypes.c_uint64),
                ("child_elapsed", ctypes.c_uint64), ("disk_read", ctypes.c_uint64),
                ("disk_write", ctypes.c_uint64))


@functools.lru_cache(maxsize=1)
def _mac_timebase_ns_per_tick() -> float | None:
    class TimebaseInfo(ctypes.Structure):
        _fields_ = (("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32))

    try:
        fn = ctypes.CDLL(None).mach_timebase_info
        fn.argtypes = [ctypes.POINTER(TimebaseInfo)]
        fn.restype = ctypes.c_int
        info = TimebaseInfo()
        if fn(ctypes.byref(info)) != 0 or not info.denom:
            return None
        return float(info.numer) / float(info.denom)
    except (AttributeError, OSError):
        return None


def _mac_rusage(pid: int) -> _MacRusageV2 | None:
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        fn = libproc.proc_pid_rusage
        fn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
        fn.restype = ctypes.c_int
        info = _MacRusageV2()
        return info if fn(pid, 2, ctypes.byref(info)) == 0 else None
    except (AttributeError, OSError):
        return None


def _mac_open_files(pid: int) -> int | None:
    class ProcBsdInfo(ctypes.Structure):
        _fields_ = (("flags", ctypes.c_uint32), ("status", ctypes.c_uint32),
                    ("xstatus", ctypes.c_uint32), ("pid", ctypes.c_uint32),
                    ("ppid", ctypes.c_uint32), ("uid", ctypes.c_uint32),
                    ("gid", ctypes.c_uint32), ("ruid", ctypes.c_uint32),
                    ("rgid", ctypes.c_uint32), ("svuid", ctypes.c_uint32),
                    ("svgid", ctypes.c_uint32), ("rfu", ctypes.c_uint32),
                    ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32),
                    ("nfiles", ctypes.c_uint32), ("pgid", ctypes.c_uint32),
                    ("pjobc", ctypes.c_uint32), ("tdev", ctypes.c_uint32),
                    ("tpgid", ctypes.c_uint32), ("nice", ctypes.c_int32),
                    ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64))
    try:
        fn = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True).proc_pidinfo
        fn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                       ctypes.c_void_p, ctypes.c_int]
        fn.restype = ctypes.c_int
        info = ProcBsdInfo()
        got = fn(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        return int(info.nfiles) if got == ctypes.sizeof(info) else None
    except (AttributeError, OSError):
        return None


def _windows_times(pid: int) -> float | None:
    class FileTime(ctypes.Structure):
        _fields_ = (("low", ctypes.c_ulong), ("high", ctypes.c_ulong))

    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        k32.GetProcessTimes.argtypes = [ctypes.c_void_p, ctypes.POINTER(FileTime),
                                        ctypes.POINTER(FileTime), ctypes.POINTER(FileTime),
                                        ctypes.POINTER(FileTime)]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = k32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        created, exited, kernel, user = FileTime(), FileTime(), FileTime(), FileTime()
        try:
            if not k32.GetProcessTimes(
                    ctypes.c_void_p(handle), ctypes.byref(created), ctypes.byref(exited),
                    ctypes.byref(kernel), ctypes.byref(user)):
                return None
            ticks = (((kernel.high << 32) | kernel.low)
                     + ((user.high << 32) | user.low))
            return ticks / 10_000_000.0
        finally:
            k32.CloseHandle(handle)
    except (AttributeError, OSError):
        return None


def _cpu_seconds(pid: int) -> float | None:
    if sys.platform == "win32":
        return _windows_times(pid)
    if sys.platform.startswith("linux"):
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            fields = raw[raw.rfind(")") + 2:].split()
            ticks = int(fields[11]) + int(fields[12])
            return ticks / os.sysconf("SC_CLK_TCK")
        except (OSError, ValueError, IndexError):
            return None
    if sys.platform == "darwin":
        info = _mac_rusage(pid)
        scale = _mac_timebase_ns_per_tick()
        return ((info.user_ns + info.system_ns) * scale / 1_000_000_000.0
                if info is not None and scale is not None else None)
    try:
        raw = subprocess.check_output(
            ["ps", "-o", "time=", "-p", str(pid)], text=True,
            encoding="utf-8", errors="replace", timeout=2).strip()
        days = 0
        if "-" in raw:
            day, raw = raw.split("-", 1)
            days = int(day)
        fields = [float(part) for part in raw.split(":")]
        seconds = fields[-1] + (fields[-2] * 60 if len(fields) >= 2 else 0)
        seconds += fields[-3] * 3600 if len(fields) >= 3 else 0
        return days * 86400 + seconds
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _rss_bytes(pid: int) -> int | None:
    if sys.platform == "win32":
        class Counters(ctypes.Structure):
            _fields_ = (("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong),
                        ("peak_ws", ctypes.c_size_t), ("ws", ctypes.c_size_t),
                        ("qpp", ctypes.c_size_t), ("qp", ctypes.c_size_t),
                        ("qnp", ctypes.c_size_t), ("qn", ctypes.c_size_t),
                        ("page", ctypes.c_size_t), ("peak_page", ctypes.c_size_t))
        try:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            k32.OpenProcess.restype = ctypes.c_void_p
            k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            k32.CloseHandle.argtypes = [ctypes.c_void_p]
            psapi.GetProcessMemoryInfo.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
            # 0x0410 = PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, GetProcessMemoryInfo's documented rights.
            handle = k32.OpenProcess(0x0410, False, pid)
            if not handle:
                return None
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            try:
                return (int(counters.ws) if psapi.GetProcessMemoryInfo(
                    ctypes.c_void_p(handle), ctypes.byref(counters), counters.cb) else None)
            finally:
                k32.CloseHandle(handle)
        except (AttributeError, OSError):
            return None
    if sys.platform == "darwin":
        info = _mac_rusage(pid)
        return int(info.resident) if info is not None else None
    if sys.platform.startswith("linux"):
        try:
            for line in Path(f"/proc/{pid}/status").read_text(
                    encoding="ascii").splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            return None
        return None
    try:
        rss_kib = int(subprocess.check_output(
            ["ps", "-o", "rss=", "-p", str(pid)], text=True,
            encoding="utf-8", errors="replace", timeout=2).strip())
        return rss_kib * 1024
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _handle_count(pid: int) -> int | None:
    if sys.platform == "win32":
        try:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = ctypes.c_void_p
            k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            k32.GetProcessHandleCount.argtypes = [ctypes.c_void_p,
                                                  ctypes.POINTER(ctypes.c_ulong)]
            k32.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = k32.OpenProcess(0x1000, False, pid)
            if not handle:
                return None
            count = ctypes.c_ulong()
            try:
                return int(count.value) if k32.GetProcessHandleCount(
                    ctypes.c_void_p(handle), ctypes.byref(count)) else None
            finally:
                k32.CloseHandle(handle)
        except (AttributeError, OSError):
            return None
    if sys.platform == "darwin":
        return _mac_open_files(pid)
    proc_fd = Path(f"/proc/{pid}/fd")
    if proc_fd.exists():
        try:
            return sum(1 for _ in proc_fd.iterdir())
        except OSError:
            return None
    try:
        lines = subprocess.check_output(
            ["lsof", "-p", str(pid)], text=True,
            encoding="utf-8", errors="replace", timeout=3).splitlines()
        return max(0, len(lines) - 1)
    except (OSError, subprocess.SubprocessError):
        return None


def _windows_process_snapshot() -> dict[int, tuple[int, str]]:
    if sys.platform != "win32":
        return {}
    from ctypes import wintypes

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = (("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260))
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        k32.Process32FirstW.argtypes = [ctypes.c_void_p,
                                        ctypes.POINTER(ProcessEntry32W)]
        k32.Process32NextW.argtypes = [ctypes.c_void_p,
                                       ctypes.POINTER(ProcessEntry32W)]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        snapshot = k32.CreateToolhelp32Snapshot(0x00000002, 0)
        invalid = ctypes.c_void_p(-1).value
        if not snapshot or snapshot == invalid:
            return {}
        found = {}
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        try:
            ok = bool(k32.Process32FirstW(snapshot, ctypes.byref(entry)))
            while ok:
                found[int(entry.th32ProcessID)] = (
                    int(entry.th32ParentProcessID), str(entry.szExeFile))
                ok = bool(k32.Process32NextW(snapshot, ctypes.byref(entry)))
        finally:
            k32.CloseHandle(snapshot)
        return found
    except (AttributeError, OSError):
        return {}


def _direct_children(pid: int) -> list[int]:
    """Return direct child processes without treating threads as processes."""
    if sys.platform.startswith("linux"):
        try:
            # A subprocess launched by indexd's Python worker thread belongs to
            # that TID's child list, not necessarily the thread-group leader's.
            children = set()
            for path in Path(f"/proc/{pid}/task").glob("*/children"):
                children.update(int(value) for value in path.read_text(
                    encoding="ascii").split())
            return sorted(children)
        except (OSError, ValueError):
            return []
    if sys.platform == "darwin":
        try:
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            fn = libproc.proc_listchildpids
            fn.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
            fn.restype = ctypes.c_int
            count = fn(pid, None, 0)
            if count <= 0:
                return []
            # The API reports a PID count when buffer=None on supported macOS.
            buf = (ctypes.c_int * (count + 16))()
            got = fn(pid, ctypes.byref(buf), ctypes.sizeof(buf))
            return [int(buf[i]) for i in range(max(0, min(got, len(buf))))
                    if int(buf[i]) > 0]
        except (AttributeError, OSError, ValueError):
            return []
    if sys.platform == "win32":
        return [child for child, (parent, _name) in
                _windows_process_snapshot().items() if parent == pid]
    try:
        raw = subprocess.check_output(
            ["ps", "-axo", "pid=,ppid="], text=True,
            encoding="utf-8", errors="replace", timeout=2)
        return [int(line.split()[0]) for line in raw.splitlines()
                if len(line.split()) == 2 and int(line.split()[1]) == pid]
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def _tree_pids(root: int) -> list[int]:
    """Snapshot ``root`` plus all currently-live descendants."""
    found = {int(root)}
    pending = [int(root)]
    while pending:
        parent = pending.pop()
        for child in _direct_children(parent):
            if child not in found:
                found.add(child)
                pending.append(child)
    return sorted(found)


def _idle_tree_child_free(root: int) -> bool:
    pids = _tree_pids(root)
    if sys.platform != "win32":
        return pids == [int(root)]
    processes = _windows_process_snapshot()
    if root not in processes:
        return False
    for pid in pids:
        if pid == root:
            continue
        record = processes.get(pid)
        if record is None or record[1].casefold() != "conhost.exe":
            return False
    return True


class _TreeAccumulator:
    """Accumulate peaks and CPU for a changing process tree.

    Observed children retain their last CPU sample after exit. Work completed
    entirely between samples is outside this polling meter.
    """

    def __init__(self, root: int, *, new_process: bool = False):
        self.root = int(root)
        self.new_process = new_process
        self.cpu_first: dict[int, float] = {}
        self.cpu_last: dict[int, float] = {}
        self.peak_rss = 0
        self.peak_handles = 0
        self.max_processes = 0
        self.child_work_seen = False
        self.root_rss_seen = False
        self.root_handles_seen = False
        self.observe()

    def observe(self) -> None:
        pids = _tree_pids(self.root)
        self.max_processes = max(self.max_processes, len(pids))
        if sys.platform == "win32":
            processes = _windows_process_snapshot()
            self.child_work_seen |= self.root not in processes or any(
                pid != self.root
                and (processes.get(pid) is None
                     or processes[pid][1].casefold() != "conhost.exe")
                for pid in pids)
        else:
            self.child_work_seen |= any(pid != self.root for pid in pids)
        rss_total = 0
        handles_total = 0
        rss_seen = False
        handles_seen = False
        for pid in pids:
            cpu = _cpu_seconds(pid)
            if cpu is not None:
                if pid not in self.cpu_first:
                    self.cpu_first[pid] = 0.0 if self.new_process else cpu
                self.cpu_last[pid] = max(cpu, self.cpu_last.get(pid, cpu))
            rss = _rss_bytes(pid)
            if rss is not None:
                rss_total += rss
                rss_seen = True
                if pid == self.root:
                    self.root_rss_seen = True
            handles = _handle_count(pid)
            if handles is not None:
                handles_total += handles
                handles_seen = True
                if pid == self.root:
                    self.root_handles_seen = True
        if rss_seen:
            self.peak_rss = max(self.peak_rss, rss_total)
        if handles_seen:
            self.peak_handles = max(self.peak_handles, handles_total)

    @property
    def cpu_seconds(self) -> float | None:
        if not self.cpu_last:
            return None
        return sum(max(0.0, last - self.cpu_first.get(pid, last))
                   for pid, last in self.cpu_last.items())

    def metrics(self) -> dict[str, float | int | None]:
        return {
            "rss_mib": (round(self.peak_rss / (1024 * 1024), 3)
                        if self.root_rss_seen else None),
            "handles": self.peak_handles if self.root_handles_seen else None,
            "cpu_seconds": (round(self.cpu_seconds, 4)
                            if self.cpu_seconds is not None else None),
            "processes": self.max_processes,
            "child_work_seen": self.child_work_seen,
        }


def _sample_tree(root: int, duration: float, *, interval: float = 0.1) -> dict:
    started = time.monotonic()
    acc = _TreeAccumulator(root)
    while time.monotonic() - started < duration:
        time.sleep(min(interval, max(0.0, duration - (time.monotonic() - started))))
        acc.observe()
    elapsed = max(0.001, time.monotonic() - started)
    result = acc.metrics()
    cpu = acc.cpu_seconds
    result["elapsed_s"] = round(elapsed, 3)
    result["cpu_percent"] = (None if cpu is None else
                             round(max(0.0, cpu) / elapsed * 100.0, 3))
    return result


def _wait_for_idle(root: int, *, minimum_s: float, timeout_s: float,
                   ready=None, window_s: float = 1.0,
                   max_cpu_percent: float = 2.0) -> tuple[float, dict]:
    """Wait for two consecutive low-CPU, child-free, ready windows.

    CPU alone is insufficient: indexd deliberately waits for the live watcher to
    quiet before its first reconcile. An early low-CPU interval can therefore be
    scheduled-idle rather than settled-idle. ``ready`` supplies durable evidence
    that the first reconcile/FTS publication actually completed.
    """
    started = time.monotonic()
    deadline = started + max(timeout_s, minimum_s + 2 * window_s)
    if minimum_s > 0:
        time.sleep(minimum_s)
    consecutive = 0
    last: dict = {}
    last_ready = False
    while time.monotonic() < deadline:
        last = _sample_tree(root, window_s)
        if last.get("cpu_percent") is None:
            # Counters unavailable: settle deterministically; the missing
            # CPU metric keeps --check from passing.
            return time.monotonic() - started, last
        is_ready = True if ready is None else bool(ready())
        last_ready = is_ready
        child_free = not bool(last.get("child_work_seen", True))
        if (is_ready and child_free
                and float(last["cpu_percent"]) <= max_cpu_percent):
            consecutive += 1
            if consecutive >= 2:
                return time.monotonic() - started, last
        else:
            consecutive = 0
    raise RuntimeError(
        f"indexd did not become idle within {timeout_s:.1f}s "
        f"(last tree CPU={last.get('cpu_percent')}%, "
        f"processes={last.get('processes')}, ready={last_ready})")


def _private_env(home: Path, data: Path) -> dict[str, str]:
    env = dict(os.environ)
    for name in ("USERPROFILE", "HOME", "APPDATA", "CLINE_DIR",
                 "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        env.pop(name, None)
    env.update({
        "AGREP_HOME": str(home), "HOME": str(home), "USERPROFILE": str(home),
        "AGREP_DATA_DIR": str(data), "AGREP_DATA_DIR_SOURCE": "env",
        "AGREP_INDEXD_IDLE_S": "300", "AGREP_NO_ARCHIVE": "1",
        # Keep the idle lane pure. Each interval is larger than any realistic
        # monotonic clock value, so the first housekeeping pass is also deferred.
        "AGREP_ARCHIVE_CHECK_S": "1000000000000",
        "AGREP_TEACH_CHECK_S": "1000000000000",
        "AGREP_EMBED_CHECK_S": "1000000000000",
        "AGREP_EMBED_LARGE_CHECK_S": "1000000000000",
        "AGREP_HEALTH_CHECK_S": "1000000000000",
    })
    return env


def _indexd_spawn_options() -> dict[str, int | bool]:
    if os.name == "nt":
        return {"creationflags": 0x00000208}
    return {"start_new_session": True}


def _close_process_pipes(proc: subprocess.Popen) -> None:
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        try:
            if stream is not None:
                stream.close()
        except OSError:
            pass


def _terminate_unvalidated_process(proc: subprocess.Popen) -> None:
    try:
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
    finally:
        _close_process_pipes(proc)


def _validated_private_process(
        proc: subprocess.Popen) -> subprocess.Popen:
    if os.name != "posix":
        return proc
    pid = int(proc.pid)
    try:
        pgid = os.getpgid(pid)
        sid = os.getsid(pid)
    except OSError as exc:
        _terminate_unvalidated_process(proc)
        raise RuntimeError(
            f"resource helper {pid} lost its private process group") from exc
    if pgid != pid or sid != pid:
        _terminate_unvalidated_process(proc)
        raise RuntimeError(
            f"resource helper {pid} is not a private session leader "
            f"(pgid={pgid}, sid={sid})")
    setattr(proc, _PRIVATE_GROUP_ATTR, pid)
    return proc


def _write_store(project: Path) -> int:
    """Write the same byte/count shape used by the committed perf ingest gate."""
    payload = "incremental ingest benchmark payload " + ("x" * 2_200)
    reply_payload = "incremental ingest benchmark reply " + ("y" * 2_600)
    stale_mtime = time.time() - _IDLE_SOURCE_AGE_S
    made = 0
    for file_index in range(FILES):
        session = f"resource-session-{file_index:08d}"
        rows_here = 4 if file_index < (ROWS - 3 * FILES) else 3
        records = []
        for turn in range(rows_here):
            records.append(json.dumps({
                "type": "user", "userType": "external", "sessionId": session,
                "timestamp": "2026-01-02T10:00:00.000Z", "cwd": "/work/resource",
                "message": {"role": "user", "content": f"{payload} {turn}"},
            }, separators=(",", ":")))
            records.append(json.dumps({
                "type": "assistant", "sessionId": session,
                "timestamp": "2026-01-02T10:00:00.500Z", "cwd": "/work/resource",
                "message": {"role": "assistant", "model": "claude-resource",
                            "content": [{"type": "text",
                                         "text": f"{reply_payload} {turn}"}]},
            }, separators=(",", ":")))
        call_id = f"toolu_resource_{file_index:08d}"
        records.extend((
            json.dumps({
                "type": "assistant", "sessionId": session,
                "timestamp": "2026-01-02T10:00:58.000Z", "cwd": "/work/resource",
                "message": {"role": "assistant", "model": "claude-resource",
                            "content": [{"type": "tool_use", "id": call_id,
                                         "name": "Read",
                                         "input": {"file_path": "/work/resource.txt"}}]},
            }, separators=(",", ":")),
            json.dumps({
                "type": "user", "userType": "external", "sessionId": session,
                "timestamp": "2026-01-02T10:00:59.000Z", "cwd": "/work/resource",
                "message": {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": call_id,
                    "content": "fixture tool result", "is_error": False}]},
            }, separators=(",", ":")),
        ))
        path = project / f"{session}.jsonl"
        path.write_text("\n".join(records) + "\n", encoding="utf-8")
        # Otherwise fresh fixture mtimes turn the idle lane into a live-session burst.
        os.utime(path, (stale_mtime, stale_mtime))
        made += rows_here
    return made


def _resolve_ingest(env: dict[str, str]) -> Path:
    probe = subprocess.run(
        [sys.executable, "-c", "import common; print(common.ingest_bin())"],
        cwd=PY, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=15)
    if probe.returncode != 0 or not probe.stdout.strip():
        raise RuntimeError(f"could not resolve Rust ingest binary: {probe.stderr[-300:]}")
    path = Path(probe.stdout.strip().splitlines()[-1])
    if not path.is_file():
        raise RuntimeError(f"Rust ingest binary not found: {path}")
    return path


def _bind_ingest_writer_env(
        ingest: Path, env: dict[str, str],
) -> dict[str, str]:
    """Prepare the exact launch-scoped Python+Rust owner outside timed work."""
    code = (
        "import json,sys; from pathlib import Path; import indexd_runtime; "
        "prepared=indexd_runtime.rust_writer_env(Path(sys.argv[1])); "
        f"keys={_WRITER_ENV_KEYS!r}; "
        "print(json.dumps({key:prepared.get(key) for key in keys},"
        "separators=(',',':')))"
    )
    probe = subprocess.run(
        [sys.executable, "-c", code, str(ingest)],
        cwd=PY, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=15)
    if probe.returncode != 0:
        raise RuntimeError(
            f"could not bind Rust ingest writer identity: {probe.stderr[-300:]}")
    try:
        identity = json.loads(probe.stdout)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Rust ingest writer identity probe was invalid") from exc
    if not isinstance(identity, dict) or set(identity) != set(_WRITER_ENV_KEYS):
        raise RuntimeError("Rust ingest writer identity probe had the wrong schema")
    for name in _WRITER_ENV_KEYS[:2]:
        value = identity.get(name)
        if (not isinstance(value, str) or len(value) != 20
                or any(char not in "0123456789abcdef" for char in value)):
            raise RuntimeError(
                f"Rust ingest writer identity probe returned invalid {name}")
    adoption = identity.get("AGREP_DERIVED_ADOPTION_OWNER_TOKEN")
    if (adoption is not None
            and (not isinstance(adoption, str) or len(adoption) != 32
                 or any(char not in "0123456789abcdef" for char in adoption))):
        raise RuntimeError(
            "Rust ingest writer identity probe returned an invalid adoption token")
    if identity.get("AGREP_DERIVED_WRITER_IDENTITY_BLOCKED") is not None:
        raise RuntimeError("Rust ingest writer identity probe remained blocked")
    bound = dict(env)
    for name in _WRITER_ENV_KEYS:
        bound.pop(name, None)
        value = identity.get(name)
        if isinstance(value, str):
            bound[name] = value
    return bound


def _run_ingest(ingest: Path, env: dict[str, str]) -> dict[str, float | int | None]:
    started = time.monotonic()
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as log:
        proc = _validated_private_process(subprocess.Popen(
            [str(ingest), "index"], cwd=REPO, env=env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            **({"creationflags": subprocess.CREATE_NO_WINDOW}
               if os.name == "nt" else {"start_new_session": True})))
        acc = _TreeAccumulator(proc.pid, new_process=True)
        try:
            deadline = time.monotonic() + 600.0
            while proc.poll() is None:
                if time.monotonic() >= deadline:
                    raise RuntimeError("synthetic cold ingest timed out after 600s")
                acc.observe()
                time.sleep(0.025)
            acc.observe()
            proc.wait(timeout=5)
        except Exception:
            _stop_process(proc)
            raise
        log.seek(0)
        output = log.read()
    if proc.returncode != 0:
        raise RuntimeError(
            f"synthetic cold ingest exited {proc.returncode}: {output[-500:]}")
    metrics = acc.metrics()
    metrics["wall_ms"] = round((time.monotonic() - started) * 1000.0, 3)
    return metrics


def _published_rows(data: Path) -> int | None:
    try:
        return int((data / ".ingest.sig").read_text(encoding="utf-8").split(":", 1)[0])
    except (OSError, ValueError, IndexError):
        return None


def _default_model_root() -> Path:
    override = os.environ.get("AGREP_MODEL_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or
                    (Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or
                    (Path.home() / ".local" / "share"))
    return base / "agrep" / "models"


_SEMANTIC_BATCH_HELPER = r"""
import json
import sys
import time
from pathlib import Path

status_path, batch_rows_raw, dim_raw = sys.argv[1:]
batch_rows, dim = int(batch_rows_raw), int(dim_raw)
status = Path(status_path)

def publish(obj):
    status.write_text(json.dumps(obj, separators=(",", ":")), encoding="utf-8")

try:
    import embedder
except ImportError as exc:
    publish({"state": "skipped", "reason": f"optional semantic dependency missing: {exc}"})
    raise SystemExit(75)

try:
    model = embedder.Embedder(download=False)
except embedder.EmbedderUnavailable as exc:
    publish({"state": "skipped", "reason": str(exc)})
    raise SystemExit(75)
except Exception as exc:
    publish({"state": "error", "reason": f"{type(exc).__name__}: {exc}"})
    raise

# Production-shaped short-heavy skew exercises the real bucketing/padded-work path
# and catches silent one-row fallback; common words tokenize like chat, not random ids.
lengths = ([8] * 64 + [32] * 32 + [96] * 16
           + [256] * 8 + [512] * 4 + [900] * 4)
words = (
    "debugging", "parser", "cache", "retry", "command", "output", "agent",
    "session", "project", "semantic", "search", "index", "fresh", "response",
    "tool", "result", "windows", "mac",
)
if batch_rows != len(lengths):
    raise RuntimeError(f"semantic batch fixture expected {len(lengths)} rows")
batch_texts = [
    " ".join(words[j % len(words)] for j in range(lengths[i])) + f" row{i}"
    for i in range(batch_rows)
]
batch_cpu_started = time.process_time()
batch_started = time.monotonic()
batch_vectors = model.embed_texts(batch_texts)
batch_wall_ms = (time.monotonic() - batch_started) * 1000.0
batch_cpu_ms = (time.process_time() - batch_cpu_started) * 1000.0
if tuple(batch_vectors.shape) != (batch_rows, dim):
    raise RuntimeError(f"bad semantic batch shape: {batch_vectors.shape!r}")
del batch_vectors, batch_texts
publish({
    "state": "ready", "batch_rows": batch_rows,
    "batch_wall_ms": round(batch_wall_ms, 3),
    "batch_cpu_ms": round(batch_cpu_ms, 3),
    "active_providers": model.sess.get_providers(),
})
"""


_SEMANTIC_PROFILE_HELPER = r"""
import json

import embedder
import onnxruntime as ort

print(json.dumps({
    "dim": int(embedder.PROFILE["dim"]),
    "profile": str(embedder.PROFILE_STRING),
    "requested_provider": str(embedder.PROFILE.get(
        "provider", "CPUExecutionProvider")),
    "available_providers": ort.get_available_providers(),
    "onnxruntime": ort.__version__,
}, separators=(",", ":")))
"""


_SEMANTIC_HELPER = r"""
import json
import sys
from pathlib import Path

matrix_path, status_path, rows_raw, dim_raw = sys.argv[1:]
rows, dim = int(rows_raw), int(dim_raw)
status = Path(status_path)

def publish(obj):
    status.write_text(json.dumps(obj, separators=(",", ":")), encoding="utf-8")

try:
    import numpy as np
    import embedder
    import semworker
except ImportError as exc:
    publish({"state": "skipped", "reason": f"optional semantic dependency missing: {exc}"})
    raise SystemExit(75)

lifetime = semworker.acquire_resident_owner()
if lifetime is None:
    publish({"state": "skipped",
             "reason": "semantic resource helper could not acquire ownership"})
    raise SystemExit(75)
try:
    model = embedder.Embedder(download=False)
    matrix = np.memmap(
        matrix_path, dtype=np.float32, mode="r", shape=(rows, dim))

    def search(query, *, level, k, filters):
        vector = model.embed_query(query)
        scores = matrix @ vector
        # Force the full scan before the HTTP response; this lane measures
        # model + scan arena, never ranking quality.
        score = float(scores.max()) if len(scores) else 0.0
        return {"results": [], "candidate_sessions": [],
                "score_kind": "cosine", "resource_scan_rows": rows,
                "resource_scan_max": score}

    server = semworker.SemanticWorkerServer(
        search_fn=search, lifetime=lifetime)
    publish({"state": "ready", "pid": __import__("os").getpid(),
             "active_providers": model.sess.get_providers(),
             "owner_poll_s": semworker.OWNER_POLL_S,
             "policy_refresh_s": semworker.IDLE_POLICY_REFRESH_S})
    server.serve()
except embedder.EmbedderUnavailable as exc:
    publish({"state": "skipped", "reason": str(exc)})
    raise SystemExit(75)
except Exception as exc:
    publish({"state": "error", "reason": f"{type(exc).__name__}: {exc}"})
    raise
finally:
    lifetime.close()
"""


def _write_scan_matrix(path: Path, rows: int, dim: int) -> int:
    total = rows * dim * 4
    # Write actual blocks rather than creating a sparse hole. A post-query RSS
    # sample must represent resident matrix pages, not a shared kernel zero page.
    block = b"\0" * (1024 * 1024)
    remaining = total
    with path.open("wb") as stream:
        while remaining:
            chunk = block[:min(remaining, len(block))]
            stream.write(chunk)
            remaining -= len(chunk)
    return total


def _read_json(path: Path) -> dict | None:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _semantic_request(record: dict) -> tuple[int, dict]:
    body = json.dumps({
        "query": "resource benchmark semantic query", "level": "message",
        "k": 1, "filters": {},
    }, separators=(",", ":")).encode()
    conn = http.client.HTTPConnection("127.0.0.1", int(record["port"]), timeout=30)
    try:
        conn.connect()
        deadline_ns = time.monotonic_ns() + 30_000_000_000
        conn.request("POST", "/v1/search", body=body, headers={
            "Authorization": f"Bearer {record['token']}",
            "Content-Type": "application/json", "Content-Length": str(len(body)),
            "Connection": "close",
            SEMANTIC_DEADLINE_HEADER: str(deadline_ns),
        })
        response = conn.getresponse()
        raw = response.read(8 * 1024 * 1024)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {"raw": raw.decode("utf-8", "replace")[-300:]}
        return response.status, payload
    finally:
        conn.close()


def _active_semantic_profile(env: dict[str, str], model_root: Path) -> dict:
    profile_env = dict(env)
    profile_env["AGREP_MODEL_DIR"] = str(model_root)
    probe = subprocess.run(
        [sys.executable, "-c", _SEMANTIC_PROFILE_HELPER], cwd=PY,
        env=profile_env, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=15)
    if probe.returncode != 0:
        raise RuntimeError(f"semantic profile probe failed: {probe.stderr[-500:]}")
    try:
        result = json.loads(probe.stdout)
        dim = int(result["dim"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("semantic profile probe returned invalid JSON") from exc
    if dim < 1 or dim > 16384:
        raise RuntimeError(f"semantic profile dimension is invalid: {dim}")
    return result


def _measure_semantic_batch(data: Path, env: dict[str, str],
                            model_root: Path, dim: int) -> dict:
    """Measure corpus embedding in its production ownership boundary.

    embed.py is disposable by design, so the batch process must exit before the
    resident query worker is measured. Keeping both in one process would retain
    ONNX's largest batch arena and substantially overstate steady query RSS.
    """
    status_path = data / ".resource-semantic-batch.json"
    batch_env = dict(env)
    batch_env.update({"AGREP_MODEL_DIR": str(model_root), "AGREP_NO_DAEMON": "1"})
    started = time.monotonic()
    proc = _validated_private_process(subprocess.Popen(
        [sys.executable, "-c", _SEMANTIC_BATCH_HELPER, str(status_path),
         str(SEMANTIC_BATCH_ROWS), str(dim)],
        cwd=PY, env=batch_env, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        **({"creationflags": subprocess.CREATE_NO_WINDOW}
           if os.name == "nt" else {"start_new_session": True})))
    peak = _TreeAccumulator(proc.pid, new_process=True)
    try:
        deadline = time.monotonic() + 60.0
        while proc.poll() is None:
            if time.monotonic() >= deadline:
                raise RuntimeError("semantic batch helper timed out")
            peak.observe()
            time.sleep(0.01)
        peak.observe()
        proc.wait(timeout=5)
        state = _read_json(status_path) or {}
        stderr = proc.stderr.read() if proc.stderr is not None else ""
        if proc.returncode == 75 or state.get("state") == "skipped":
            return {
                "semantic_batch_status": "skipped",
                "semantic_skip_reason": str(
                    state.get("reason") or stderr or "semantic batch unavailable")[-500:],
            }
        if proc.returncode != 0 or state.get("state") != "ready":
            raise RuntimeError(
                f"semantic batch helper exited {proc.returncode}: "
                f"{state.get('reason') or stderr[-500:]}")
        metrics = peak.metrics()
        rows = int(state.get("batch_rows") or 0)
        wall_ms = float(state.get("batch_wall_ms") or 0)
        return {
            "semantic_batch_status": "measured",
            "semantic_batch_rows": rows,
            "semantic_batch_wall_ms": round(wall_ms, 3),
            "semantic_batch_process_wall_ms": round(
                (time.monotonic() - started) * 1000.0, 3),
            "semantic_batch_cpu_ms": state.get("batch_cpu_ms"),
            "semantic_batch_rows_per_second": round(
                rows / max(0.001, wall_ms / 1000.0), 3),
            "semantic_batch_active_providers": state.get("active_providers"),
            "semantic_batch_peak_rss_mib": metrics.get("rss_mib"),
            "semantic_batch_peak_handles": metrics.get("handles"),
            "semantic_batch_peak_processes": metrics.get("processes"),
        }
    finally:
        _stop_process(proc)
        status_path.unlink(missing_ok=True)


def _semantic_idle_sample_duration(
        requested_s: float, owner_poll_s: object,
        policy_refresh_s: object) -> float:
    try:
        poll_s = float(owner_poll_s)
        refresh_s = float(policy_refresh_s)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "semantic resource helper omitted an idle interval") from exc
    if (not math.isfinite(poll_s) or not 0.05 <= poll_s <= 60.0
            or not math.isfinite(refresh_s)
            or not 0.05 <= refresh_s <= 600.0):
        raise RuntimeError("semantic resource helper reported a bad idle interval")
    return max(
        0.5, requested_s, poll_s * 2.0 + 0.25,
        refresh_s + poll_s + 0.25)


def _semantic_worker_env(env: dict[str, str], model_root: Path) -> dict[str, str]:
    """Enable only the explicitly launched worker in its disposable data directory."""
    result = dict(env)
    result.update({
        "AGREP_MODEL_DIR": str(model_root),
        "AGREP_SEM_IDLE_S": "300",
        "AGREP_NO_DAEMON": "",
        "AGREP_NO_SEM_WORKER": "",
    })
    return result


def _measure_semantic(data: Path, env: dict[str, str], model_root: Path,
                      *, sample_s: float) -> dict:
    try:
        profile = _active_semantic_profile(env, model_root)
    except RuntimeError as exc:
        return {
            "semantic_status": "skipped",
            "semantic_scope": "disposable-batch+disposable-worker+synthetic-matrix-scan",
            "semantic_skip_reason": str(exc)[-500:],
            "semantic_rows": SEMANTIC_ROWS,
            "semantic_batch_rows": SEMANTIC_BATCH_ROWS,
        }
    dim = int(profile["dim"])
    batch = _measure_semantic_batch(data, env, model_root, dim)
    matrix = data / ".resource-semantic-scan.f32"
    matrix_bytes = _write_scan_matrix(matrix, SEMANTIC_ROWS, dim)
    if batch.get("semantic_batch_status") != "measured":
        return {
            "semantic_status": "skipped",
            "semantic_scope": "disposable-batch+disposable-worker+synthetic-matrix-scan",
            "semantic_skip_reason": batch.get("semantic_skip_reason"),
            "semantic_rows": SEMANTIC_ROWS,
            "semantic_dim": dim,
            "semantic_profile": profile,
            "semantic_batch_rows": SEMANTIC_BATCH_ROWS,
            "semantic_matrix_mib": round(matrix_bytes / (1024 * 1024), 3),
        }
    status_path = data / ".resource-semantic-status.json"
    descriptor = data / ".semantic-worker.json"
    sem_env = _semantic_worker_env(env, model_root)
    proc = _validated_private_process(subprocess.Popen(
        [sys.executable, "-c", _SEMANTIC_HELPER, str(matrix), str(status_path),
         str(SEMANTIC_ROWS), str(dim)],
        cwd=PY, env=sem_env, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        **({"creationflags": subprocess.CREATE_NO_WINDOW}
           if os.name == "nt" else {"start_new_session": True})))
    peak = _TreeAccumulator(proc.pid, new_process=True)
    try:
        deadline = time.monotonic() + 30.0
        record = None
        state = None
        while time.monotonic() < deadline:
            peak.observe()
            state = _read_json(status_path)
            record = _read_json(descriptor)
            if state and state.get("state") == "skipped":
                proc.wait(timeout=5)
                return {
                    "semantic_status": "skipped",
                    "semantic_scope": "disposable-batch+disposable-worker+synthetic-matrix-scan",
                    "semantic_skip_reason": str(state.get("reason") or "unavailable")[-500:],
                    "semantic_rows": SEMANTIC_ROWS,
                    "semantic_dim": dim,
                    "semantic_profile": profile,
                    **batch,
                    "semantic_matrix_mib": round(matrix_bytes / (1024 * 1024), 3),
                }
            if state and state.get("state") == "error":
                raise RuntimeError(f"semantic resource helper failed: {state.get('reason')}")
            if record and state and state.get("state") == "ready":
                break
            if proc.poll() is not None:
                stderr = proc.stderr.read() if proc.stderr is not None else ""
                raise RuntimeError(
                    f"semantic resource helper exited {proc.returncode}: {stderr[-500:]}")
            time.sleep(0.025)
        else:
            raise RuntimeError("semantic resource helper did not become ready")

        response: list[tuple[int, dict] | BaseException] = []
        query_resources = _TreeAccumulator(proc.pid)
        query_started = time.monotonic()

        def request() -> None:
            try:
                response.append(_semantic_request(record))
            except BaseException as exc:  # recorded and re-raised in the main thread
                response.append(exc)

        query = threading.Thread(target=request, name="resource-semantic-query")
        query.start()
        while query.is_alive():
            peak.observe()
            query_resources.observe()
            query.join(timeout=0.01)
        peak.observe()
        query_resources.observe()
        query_wall_ms = (time.monotonic() - query_started) * 1000.0
        if not response or isinstance(response[0], BaseException):
            if response:
                raise RuntimeError(f"semantic resource query failed: {response[0]}")
            raise RuntimeError("semantic resource query returned no response")
        status, payload = response[0]
        if (status != 200 or not isinstance(payload, dict)
                or not payload.get("ok", False)):
            raise RuntimeError(
                f"semantic resource query returned HTTP {status}: {payload}")

        resident = _sample_tree(
            proc.pid, _semantic_idle_sample_duration(
                sample_s, state.get("owner_poll_s"),
                state.get("policy_refresh_s")))
        peak.observe()
        peak_metrics = peak.metrics()
        query_cpu = query_resources.cpu_seconds
        peak_rss = max(
            float(peak_metrics.get("rss_mib") or 0),
            float(batch.get("semantic_batch_peak_rss_mib") or 0))
        peak_handles = max(
            int(peak_metrics.get("handles") or 0),
            int(resident.get("handles") or 0),
            int(batch.get("semantic_batch_peak_handles") or 0)) or None
        return {
            "semantic_status": "measured",
            "semantic_scope": "disposable-batch+disposable-worker+synthetic-matrix-scan",
            "semantic_skip_reason": None,
            "semantic_rows": SEMANTIC_ROWS,
            "semantic_dim": dim,
            "semantic_profile": profile,
            "semantic_active_providers": state.get("active_providers"),
            "semantic_matrix_mib": round(matrix_bytes / (1024 * 1024), 3),
            **batch,
            "semantic_peak_rss_mib": round(peak_rss, 3),
            "semantic_resident_rss_mib": resident["rss_mib"],
            "semantic_peak_handles": peak_handles,
            "semantic_idle_cpu_percent": resident["cpu_percent"],
            "semantic_idle_sample_s": resident["elapsed_s"],
            "semantic_query_cpu_ms": (None if query_cpu is None else
                                      round(query_cpu * 1000.0, 3)),
            "semantic_query_wall_ms": round(query_wall_ms, 3),
            "semantic_processes": max(
                int(peak_metrics.get("processes") or 0), int(resident.get("processes") or 0)),
        }
    finally:
        _stop_process(proc)
        matrix.unlink(missing_ok=True)


def _private_group_exists(group: int) -> bool:
    try:
        os.killpg(group, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _wait_private_group(
        proc: subprocess.Popen, group: int, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        proc.poll()
        if not _private_group_exists(group):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.025)


def _stop_posix_process(proc: subprocess.Popen) -> None:
    group = vars(proc).get(_PRIVATE_GROUP_ATTR)
    if group is None:
        if proc.poll() is not None:
            return
        _validated_private_process(proc)
        group = vars(proc)[_PRIVATE_GROUP_ATTR]
    if int(group) != int(proc.pid):
        raise RuntimeError("resource helper private process group changed")
    # Once the exact session leader has exited, a numeric process-group ID can
    # be recycled. Clean surviving same-UID descendants only while the group is
    # still signalable; never turn an unprovable recycled ID into a kill target.
    if proc.poll() is not None:
        try:
            os.killpg(group, 0)
        except (ProcessLookupError, PermissionError):
            return
    try:
        os.killpg(group, signal.SIGTERM)
    except ProcessLookupError:
        proc.poll()
        return
    if _wait_private_group(proc, group, _STOP_TERM_TIMEOUT_S):
        return
    try:
        os.killpg(group, signal.SIGKILL)
    except ProcessLookupError:
        proc.poll()
        return
    if not _wait_private_group(proc, group, _STOP_KILL_TIMEOUT_S):
        raise RuntimeError(
            f"resource helper process group {group} survived SIGKILL")


def _stop_process(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            _stop_posix_process(proc)
        elif proc.poll() is None:
            descendants = [
                pid for pid in _tree_pids(proc.pid) if pid != proc.pid]
            for pid in reversed(descendants):
                try:
                    os.kill(pid, signal.SIGTERM)
                except (OSError, ValueError):
                    pass
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
    finally:
        _close_process_pipes(proc)


def measure(*, settle_s: float = 10.0, sample_s: float = 5.0,
            idle_timeout_s: float = 30.0, measure_semantic: bool = True) -> dict:
    model_root = _default_model_root()  # Resolve before installing the fixture HOME.
    with tempfile.TemporaryDirectory(prefix="agrep-resource-") as tmp:
        root = Path(tmp)
        home = root / "home"
        project = home / ".claude" / "projects" / "resource-project"
        data = root / "data"
        project.mkdir(parents=True)
        data.mkdir()
        made = _write_store(project)
        if made != ROWS:
            raise RuntimeError(f"resource fixture made {made} messages, expected {ROWS}")
        env = _private_env(home, data)
        ingest = _resolve_ingest(env)
        ingest_env = _bind_ingest_writer_env(ingest, env)

        ingest_metrics = _run_ingest(ingest, ingest_env)
        published = _published_rows(data)
        if published != ROWS:
            raise RuntimeError(
                f"synthetic ingest published {published} messages, expected {ROWS}")

        proc = _validated_private_process(subprocess.Popen(
            [sys.executable, str(PY / "indexd.py")], cwd=REPO, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **_indexd_spawn_options()))
        try:
            idle_wait, idle_window = _wait_for_idle(
                proc.pid, minimum_s=max(0.0, settle_s), timeout_s=idle_timeout_s,
                ready=lambda: ((data / "corpus.db").is_file()
                               and not (data / ".index.lock").exists()))
            if proc.poll() is not None:
                raise RuntimeError(
                    f"indexd exited before idle resource sample ({proc.returncode})")
            idle = _sample_tree(proc.pid, max(0.5, sample_s), interval=0.1)
            if proc.poll() is not None:
                raise RuntimeError(
                    f"indexd exited during idle resource sample ({proc.returncode})")
        finally:
            _stop_process(proc)

        result = {
            "files": FILES, "rows": ROWS,
            "ingest_wall_ms": ingest_metrics["wall_ms"],
            "ingest_cpu_seconds": ingest_metrics["cpu_seconds"],
            "ingest_peak_rss_mib": ingest_metrics["rss_mib"],
            "ingest_peak_handles": ingest_metrics["handles"],
            "ingest_peak_processes": ingest_metrics["processes"],
            "idle_wait_s": round(idle_wait, 3),
            "sample_s": idle["elapsed_s"],
            "idle_cpu_percent": idle["cpu_percent"],
            # Stable public key names; values aggregate the complete idle process tree.
            "rss_mib": idle["rss_mib"],
            "handles": idle["handles"],
            "idle_processes": idle["processes"],
            "idle_last_settle_cpu_percent": idle_window.get("cpu_percent"),
        }
        if measure_semantic:
            # The synthetic store must not redirect the verified model cache.
            result.update(_measure_semantic(
                data, env, model_root, sample_s=sample_s))
        else:
            result.update({
                "semantic_status": "not-requested",
                "semantic_scope": "disposable-batch+disposable-worker+synthetic-matrix-scan",
                "semantic_skip_reason": "disabled with --no-semantic",
                "semantic_rows": SEMANTIC_ROWS,
                "semantic_batch_rows": SEMANTIC_BATCH_ROWS,
                "semantic_matrix_mib": round(
                    SEMANTIC_ROWS * SEMANTIC_DIM * 4 / (1024 * 1024), 3),
            })
        return result


def _effective_budgets() -> tuple[dict[str, float], float]:
    raw_slack = os.environ.get("AGREP_RESOURCE_SLACK", "1")
    try:
        slack = float(raw_slack)
    except ValueError as exc:
        raise ValueError(f"invalid AGREP_RESOURCE_SLACK={raw_slack!r}") from exc
    if not 0 < slack <= 100:
        raise ValueError("AGREP_RESOURCE_SLACK must be >0 and <=100")
    effective: dict[str, float] = {}
    for name, default in BUDGETS.items():
        env_name = "AGREP_RESOURCE_" + name.upper()
        raw = os.environ.get(env_name)
        try:
            base = float(raw) if raw is not None else float(default)
        except ValueError as exc:
            raise ValueError(f"invalid {env_name}={raw!r}") from exc
        if base <= 0:
            raise ValueError(f"{env_name} must be positive")
        effective[name] = base * slack
    return effective, slack


def _gate(result: dict, budgets: dict[str, float], *,
          require_semantic: bool) -> tuple[list[str], list[str]]:
    required = set(PORTABLE_BUDGETS)
    if require_semantic:
        required.update(SEMANTIC_BUDGETS)
    missing = [name for name in sorted(required) if result.get(name) is None]
    breached = [name for name, budget in budgets.items()
                if result.get(name) is not None and float(result[name]) > budget]
    if require_semantic and result.get("semantic_status") != "measured":
        missing.append("semantic_status=measured")
    return missing, breached


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help=("enforce portable cold-ingest + idle budgets, plus "
                              "semantic budgets when that optional lane is measured"))
    parser.add_argument("--check-semantic", action="store_true",
                        help=("require the already-provisioned pinned model and enforce "
                              "resident/peak/query-CPU semantic budgets (never downloads)"))
    parser.add_argument("--no-semantic", action="store_true",
                        help="do not attempt the optional local semantic lane")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--settle-s", type=float, default=10.0,
                        help="minimum wait past daemon startup before idle detection")
    parser.add_argument("--sample-s", type=float, default=5.0,
                        help="idle sampling duration")
    parser.add_argument("--idle-timeout-s", type=float, default=60.0)
    args = parser.parse_args()
    if args.check_semantic and args.no_semantic:
        parser.error("--check-semantic and --no-semantic are mutually exclusive")
    try:
        budgets, slack = _effective_budgets()
    except ValueError as exc:
        parser.error(str(exc))
    result = measure(
        settle_s=args.settle_s, sample_s=args.sample_s,
        idle_timeout_s=args.idle_timeout_s, measure_semantic=not args.no_semantic)
    if args.json:
        print(json.dumps({"metrics": result, "budgets": budgets,
                          "resource_slack": slack}, sort_keys=True))
    else:
        print(f"synthetic resource fixture: {result['files']} files / {result['rows']} messages")
        print("resource                         measured       budget")
        for name, budget in budgets.items():
            value = result.get(name)
            suffix = " (optional)" if name in SEMANTIC_BUDGETS else ""
            print(f"{name:32} {str(value):>10} {budget:>10}{suffix}")
        print(f"cold ingest wall/cpu: {result.get('ingest_wall_ms')}ms / "
              f"{result.get('ingest_cpu_seconds')} CPU-s; "
              f"tree processes={result.get('ingest_peak_processes')}")
        print(f"idle detection/sample: {result.get('idle_wait_s')}s / "
              f"{result.get('sample_s')}s; tree processes={result.get('idle_processes')}")
        semantic = result.get("semantic_status")
        if semantic == "measured":
            print(f"semantic: measured model + {result.get('semantic_rows')} row "
                  f"scan ({result.get('semantic_matrix_mib')}MiB matrix); "
                  f"peak RSS={result.get('semantic_peak_rss_mib')}MiB; "
                  f"batch={result.get('semantic_batch_rows')} rows in "
                  f"{result.get('semantic_batch_wall_ms')}ms "
                  f"({result.get('semantic_batch_rows_per_second')} rows/s); "
                  f"idle CPU={result.get('semantic_idle_cpu_percent')}%; "
                  f"query CPU/wall={result.get('semantic_query_cpu_ms')}ms/"
                  f"{result.get('semantic_query_wall_ms')}ms")
        else:
            print(f"semantic: {semantic} ({result.get('semantic_skip_reason')})")
    missing, breached = _gate(
        result, budgets, require_semantic=args.check_semantic)
    if (args.check or args.check_semantic) and (missing or breached):
        print(f"resource gate failed: missing={missing}, breached={breached}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
