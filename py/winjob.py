"""Exact Windows Job Object lifetime boundaries."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import re
import time


_ERROR_ALREADY_EXISTS = 183
_JOB_OBJECT_BASIC_ACCOUNTING = 1
_JOB_OBJECT_EXTENDED_LIMITS = 9
_JOB_OBJECT_QUERY = 0x0004
_JOB_OBJECT_TERMINATE = 0x0008
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0
_START_RE = re.compile(r"win_[0-9]+")


class _FileTime(ctypes.Structure):
    _fields_ = (("low", wintypes.DWORD), ("high", wintypes.DWORD))


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "read_ops", "write_ops", "other_ops", "read_bytes",
        "write_bytes", "other_bytes")]


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_longlong),
        ("job_time", ctypes.c_longlong),
        ("flags", wintypes.DWORD),
        ("min_working_set", ctypes.c_size_t),
        ("max_working_set", ctypes.c_size_t),
        ("active_processes", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority", wintypes.DWORD),
        ("scheduling", wintypes.DWORD),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits),
        ("io", _IoCounters),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    ]


class _BasicAccounting(ctypes.Structure):
    _fields_ = [
        ("total_user_time", ctypes.c_longlong),
        ("total_kernel_time", ctypes.c_longlong),
        ("period_user_time", ctypes.c_longlong),
        ("period_kernel_time", ctypes.c_longlong),
        ("page_faults", wintypes.DWORD),
        ("total_processes", wintypes.DWORD),
        ("active_processes", wintypes.DWORD),
        ("terminated_processes", wintypes.DWORD),
    ]


def name_for(pid: int, process_start: str) -> str | None:
    if pid <= 0 or _START_RE.fullmatch(process_start) is None:
        return None
    return f"Global\\agrep-descendants-{pid}-{process_start[4:]}"


def _kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.OpenJobObjectW.argtypes = [
        wintypes.DWORD, wintypes.BOOL, ctypes.c_wchar_p]
    kernel32.OpenJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD)]
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [
        wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.IsProcessInJob.argtypes = [
        wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    kernel32.IsProcessInJob.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [
        wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(_FileTime), ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime), ctypes.POINTER(_FileTime)]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.argtypes = ()
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def bind_current(pid: int, process_start: str, limits: int) -> int | None:
    name = name_for(pid, process_start)
    if name is None:
        return None
    kernel32 = _kernel32()
    ctypes.set_last_error(0)
    handle = kernel32.CreateJobObjectW(None, name)
    error = ctypes.get_last_error()
    if not handle:
        return None
    if error == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    keep = False
    try:
        extended = _ExtendedLimits()
        extended.basic.flags = limits
        configured = bool(kernel32.SetInformationJobObject(
            handle, _JOB_OBJECT_EXTENDED_LIMITS,
            ctypes.byref(extended), ctypes.sizeof(extended)))
        assigned = configured and bool(kernel32.AssignProcessToJobObject(
            handle, kernel32.GetCurrentProcess()))
        if not assigned:
            return None
        keep = True
        return int(getattr(handle, "value", handle))
    finally:
        if not keep:
            kernel32.CloseHandle(handle)


def _handle_process_start(kernel32, handle) -> str | None:
    created = _FileTime()
    exited = _FileTime()
    kernel = _FileTime()
    user = _FileTime()
    if not kernel32.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited),
            ctypes.byref(kernel), ctypes.byref(user)):
        return None
    ticks = (int(created.high) << 32) | int(created.low)
    return f"win_{ticks}"


@dataclass
class Handle:
    kernel32: object
    job: int
    process: int
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.kernel32.CloseHandle(wintypes.HANDLE(self.process))
        self.kernel32.CloseHandle(wintypes.HANDLE(self.job))

    def terminate_and_wait(self, wait_s: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(wait_s))
        terminated = bool(self.kernel32.TerminateJobObject(
            wintypes.HANDLE(self.job), 0))
        while True:
            accounting = _BasicAccounting()
            queried = bool(self.kernel32.QueryInformationJobObject(
                wintypes.HANDLE(self.job), _JOB_OBJECT_BASIC_ACCOUNTING,
                ctypes.byref(accounting), ctypes.sizeof(accounting), None))
            root_done = (
                self.kernel32.WaitForSingleObject(
                    wintypes.HANDLE(self.process), 0) == _WAIT_OBJECT_0)
            if queried and accounting.active_processes == 0 and root_done:
                return True
            if not terminated or not queried or time.monotonic() >= deadline:
                return False
            time.sleep(0.02)


def open_exact(pid: int, process_start: str) -> Handle | None:
    name = name_for(pid, process_start)
    if name is None:
        return None
    kernel32 = _kernel32()
    job = kernel32.OpenJobObjectW(
        _JOB_OBJECT_QUERY | _JOB_OBJECT_TERMINATE, False, name)
    if not job:
        return None
    process = kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, pid)
    if not process:
        kernel32.CloseHandle(job)
        return None
    member = wintypes.BOOL()
    exact = _handle_process_start(kernel32, process) == process_start
    bound = exact and bool(kernel32.IsProcessInJob(
        process, job, ctypes.byref(member))) and bool(member.value)
    if not bound:
        kernel32.CloseHandle(process)
        kernel32.CloseHandle(job)
        return None
    return Handle(
        kernel32, int(getattr(job, "value", job)),
        int(getattr(process, "value", process)))
