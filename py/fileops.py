"""Stable file identity and atomic replacement shared by storage paths."""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path
from typing import Callable

from proc import WIN


FileIdentity = tuple[int, int, int, int, int]
_WINDOWS_IDENTITY_API = None
_WINDOWS_USN_API = None


def _windows_identity_api():
    global _WINDOWS_IDENTITY_API
    if _WINDOWS_IDENTITY_API is not None:
        return _WINDOWS_IDENTITY_API
    import ctypes
    from ctypes import wintypes

    class ByHandleFileInfo(ctypes.Structure):
        _fields_ = (
            ("attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("last_access_time", wintypes.FILETIME),
            ("last_write_time", wintypes.FILETIME),
            ("volume_serial", wintypes.DWORD),
            ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD),
            ("link_count", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        )

    class FileBasicInfo(ctypes.Structure):
        _fields_ = (
            ("creation_time", ctypes.c_longlong),
            ("last_access_time", ctypes.c_longlong),
            ("last_write_time", ctypes.c_longlong),
            ("change_time", ctypes.c_longlong),
            ("attributes", wintypes.DWORD),
        )

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetFileInformationByHandle.argtypes = (
        wintypes.HANDLE, ctypes.POINTER(ByHandleFileInfo),
    )
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.GetFileInformationByHandleEx.argtypes = (
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    )
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    _WINDOWS_IDENTITY_API = (
        ctypes, kernel32, ByHandleFileInfo, FileBasicInfo,
    )
    return _WINDOWS_IDENTITY_API


def _windows_handle_identity(handle: int) -> FileIdentity:
    ctypes, kernel32, legacy_type, basic_type = _windows_identity_api()
    basic = basic_type()
    if not kernel32.GetFileInformationByHandleEx(
            handle, 0, ctypes.byref(basic), ctypes.sizeof(basic)):
        raise ctypes.WinError(ctypes.get_last_error())
    if basic.attributes & (0x00000010 | 0x00000400):
        raise OSError("file identity requires a plain regular file")

    legacy = legacy_type()
    if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(legacy)):
        raise ctypes.WinError(ctypes.get_last_error())
    device = int(legacy.volume_serial)
    inode = (int(legacy.file_index_high) << 32) | int(legacy.file_index_low)
    size = (int(legacy.size_high) << 32) | int(legacy.size_low)
    epoch = 116_444_736_000_000_000
    modified = (int(basic.last_write_time) - epoch) * 100
    return device, inode, size, modified, int(basic.change_time) * 100


def windows_file_identity(path: Path) -> FileIdentity:
    ctypes, kernel32, _legacy, _basic = _windows_identity_api()
    handle = kernel32.CreateFileW(
        str(path), 0x0080, 0x00000007, None, 3, 0x00200000, None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in (None, invalid):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return _windows_handle_identity(handle)
    finally:
        kernel32.CloseHandle(handle)


def windows_rust_change_token(
        path: Path, expected_identity: FileIdentity | None = None) -> int:
    """Return the same NTFS USN token Rust serializes in ``ChangeToken``."""
    if not WIN:
        raise OSError("Windows change tokens are unavailable on this platform")
    global _WINDOWS_USN_API
    if _WINDOWS_USN_API is None:
        import ctypes
        from ctypes import wintypes

        class ReadFileUsnData(ctypes.Structure):
            _fields_ = (
                ("minimum", wintypes.WORD),
                ("maximum", wintypes.WORD),
            )

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = (
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        )
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.DeviceIoControl.argtypes = (
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, wintypes.LPDWORD, wintypes.LPVOID,
        )
        kernel32.DeviceIoControl.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        _WINDOWS_USN_API = (
            ctypes, wintypes, kernel32, ReadFileUsnData,
        )

    ctypes, wintypes, kernel32, request_type = _WINDOWS_USN_API
    handle = kernel32.CreateFileW(
        str(path), 0x80000000, 0x00000007, None, 3, 0x00200000, None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in (None, invalid):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        opened_identity = _windows_handle_identity(handle)
        if (expected_identity is not None
                and opened_identity != expected_identity):
            raise OSError("file changed before reading its USN token")
        request = request_type(2, 3)
        output = (ctypes.c_ubyte * 1024)()
        returned = wintypes.DWORD()
        if not kernel32.DeviceIoControl(
                handle, 0x000900EB, ctypes.byref(request),
                ctypes.sizeof(request), output, ctypes.sizeof(output),
                ctypes.byref(returned), None):
            raise ctypes.WinError(ctypes.get_last_error())
        raw = bytes(output[:returned.value])
        if len(raw) < 8:
            raise OSError("invalid file USN response")
        record_length = int.from_bytes(raw[0:4], "little")
        major = int.from_bytes(raw[4:6], "little")
        usn_offset = 24 if major == 2 else 40 if major == 3 else -1
        if (usn_offset < 0 or record_length > len(raw)
                or record_length < usn_offset + 8):
            raise OSError("invalid file USN response")
        usn = int.from_bytes(
            raw[usn_offset:usn_offset + 8], "little", signed=True)
        if _windows_handle_identity(handle) != opened_identity:
            raise OSError("file changed while reading its USN token")
        value = usn & 0xFFFFFFFFFFFFFFFF
        rotated = ((value << 7) | (value >> 57)) & 0xFFFFFFFFFFFFFFFF
        return rotated ^ 0x55534E5F46494C45
    finally:
        kernel32.CloseHandle(handle)


def file_identity_fd(fd: int) -> FileIdentity:
    if WIN:
        import msvcrt

        handle = msvcrt.get_osfhandle(fd)
        if handle == -1:
            raise OSError(f"invalid file descriptor: {fd}")
        return _windows_handle_identity(handle)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise OSError("file identity requires a plain regular file")
    return (
        int(info.st_dev), int(info.st_ino), int(info.st_size),
        int(info.st_mtime_ns), int(info.st_ctime_ns),
    )


def file_identity(path: Path) -> FileIdentity:
    if WIN:
        return windows_file_identity(path)
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(path, flags)
    try:
        return file_identity_fd(fd)
    finally:
        os.close(fd)


def replace_with_retry(
        src: Path, dst: Path, attempts: int = 80,
        before_attempt: Callable[[], object] | None = None) -> None:
    """Replace dst with src, retrying transient Windows reader locks."""
    delay = 0.025
    last: OSError | None = None
    for _ in range(attempts):
        if before_attempt is not None:
            before_attempt()
        try:
            os.replace(src, dst)
            return
        except OSError as e:
            last = e
            if not WIN:
                break
            time.sleep(delay)
            delay = min(delay * 1.5, 0.5)
    if last is not None:
        raise last
