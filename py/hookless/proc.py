"""Process identity primitives for the dependency-isolated hookless layer."""

from __future__ import annotations

import os
from pathlib import Path
import sys


_MAX_PROCESS_ID = (1 << 32) - 1
_DESCENDANT_JOB_HANDLE = None


def _posix_state(pid: int) -> str:
    if sys.platform.startswith("linux"):
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            return raw[raw.rfind(")") + 2:].split()[0]
        except (IndexError, OSError):
            return ""
    if sys.platform == "darwin":
        try:
            from hookless import procscan

            ctypes, info_type, pidinfo = procscan._mac_birth_api()
            info = info_type()
            got = pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
            if got == ctypes.sizeof(info):
                return "Z" if int(info.status) == 5 else "R"
        except (AttributeError, OSError, OverflowError, ValueError):
            pass
    return ""


def _windows_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return ctypes.get_last_error() != 87
    try:
        code = wintypes.DWORD()
        return not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
    finally:
        kernel32.CloseHandle(handle)


def pid_alive(pid: int) -> bool:
    """Return false only when the process is conclusively gone or a zombie."""
    if pid <= 0 or pid > _MAX_PROCESS_ID:
        return False
    if sys.platform == "win32":
        return _windows_alive(pid)
    if _posix_state(pid) == "Z":
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def _windows_start(pid: int) -> str | None:
    import ctypes

    class FileTime(ctypes.Structure):
        _fields_ = (("low", ctypes.c_ulong), ("high", ctypes.c_ulong))

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    created, exited, kernel, user = FileTime(), FileTime(), FileTime(), FileTime()
    try:
        if not kernel32.GetProcessTimes(
                ctypes.c_void_p(handle), ctypes.byref(created), ctypes.byref(exited),
                ctypes.byref(kernel), ctypes.byref(user)):
            return None
        return f"win_{(int(created.high) << 32) | int(created.low)}"
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def process_start_identity(pid: int) -> str | None:
    """Return a kernel birth identity so a recycled PID cannot inherit liveness."""
    if pid <= 0 or pid > _MAX_PROCESS_ID:
        return None
    if sys.platform == "win32":
        return _windows_start(pid)
    if sys.platform == "darwin":
        from hookless import procscan

        return procscan._process_birth(pid)[0] or None
    if sys.platform.startswith("linux"):
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            fields = raw[raw.rfind(")") + 2:].split()
            return f"proc_{fields[19]}"
        except (IndexError, OSError):
            return None
    return None


def _process_group_active(process_group: int) -> bool | None:
    if sys.platform == "win32" or process_group <= 0:
        return False
    if sys.platform.startswith("linux"):
        matched = False
        try:
            entries = os.scandir("/proc")
        except OSError:
            return None
        with entries:
            for entry in entries:
                if not entry.name.isdecimal():
                    continue
                try:
                    raw = Path(entry.path, "stat").read_text(
                        encoding="ascii", errors="replace")
                    fields = raw[raw.rfind(")") + 2:].split()
                    state, group = fields[0], int(fields[2])
                except FileNotFoundError:
                    continue
                except (IndexError, OSError, ValueError):
                    return None
                if group == process_group:
                    matched = True
                    if state != "Z":
                        return True
        if matched:
            return False
    elif sys.platform == "darwin":
        try:
            import ctypes
            from hookless import procscan

            library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            listpids = library.proc_listpids
            listpids.argtypes = [
                ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
            listpids.restype = ctypes.c_int
            ctypes.set_errno(0)
            needed = listpids(2, process_group, None, 0)
            if needed < 0 or (needed == 0 and ctypes.get_errno()):
                return None
            slots = max(64, needed // ctypes.sizeof(ctypes.c_int) + 32)
            if slots > 65_536:
                return None
            pids = (ctypes.c_int * slots)()
            ctypes.set_errno(0)
            used = listpids(2, process_group, pids, ctypes.sizeof(pids))
            if (used < 0 or (used == 0 and ctypes.get_errno())
                    or used >= ctypes.sizeof(pids)):
                return None
            ctypes_api, info_type, pidinfo = procscan._mac_birth_api()
            matched = False
            for pid in pids[:used // ctypes.sizeof(ctypes.c_int)]:
                if pid <= 0:
                    continue
                info = info_type()
                got = pidinfo(
                    pid, 3, 0, ctypes_api.byref(info), ctypes_api.sizeof(info))
                if got != ctypes_api.sizeof(info):
                    return None
                if int(info.pgid) == process_group:
                    matched = True
                    if int(info.status) != 5:
                        return True
            if matched:
                return False
        except (AttributeError, OSError, OverflowError, ValueError):
            return None
    try:
        os.killpg(process_group, 0)
        return None
    except ProcessLookupError:
        return False
    except OSError:
        return None


def bind_descendants_to_process_lifetime() -> bool:
    global _DESCENDANT_JOB_HANDLE
    if sys.platform != "win32":
        return False
    if _DESCENDANT_JOB_HANDLE is not None:
        return True
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
            ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
            ("max_working_set", ctypes.c_size_t),
            ("active_processes", wintypes.DWORD), ("affinity", ctypes.c_size_t),
            ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "read_ops", "write_ops", "other_ops", "read_bytes",
            "write_bytes", "other_bytes")]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("basic", BasicLimits), ("io", IoCounters),
            ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
            ("peak_process_memory", ctypes.c_size_t),
            ("peak_job_memory", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        return False
    keep = False
    try:
        limits = ExtendedLimits()
        limits.basic.flags = 0x00002000
        configured = bool(kernel32.SetInformationJobObject(
            handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        assigned = configured and bool(kernel32.AssignProcessToJobObject(
            handle, kernel32.GetCurrentProcess()))
        if not assigned:
            return False
        _DESCENDANT_JOB_HANDLE = int(getattr(handle, "value", handle))
        keep = True
        return True
    finally:
        if not keep:
            kernel32.CloseHandle(handle)
