"""Host resource probes and bounded private logs.

Best-effort memory, battery, and CPU readings feed the background governors
(indexer policy, embed governor, semantic residency); every probe degrades to
"unknown" so a failed reading can never stall deferrable work. data_dir_usage
and open_bounded_log keep the on-disk footprint observable and bounded.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

from events import DATA_DIR, data_dir_readonly
from proc import WIN

EMBEDDINGS_PATH = DATA_DIR / "embeddings.f32"


def _darwin_available_memory_fraction() -> float | None:
    import ctypes

    host_vm_info64 = 4
    free_index = 0
    inactive_index = 2
    speculative_index = 23
    try:
        system = ctypes.CDLL(None, use_errno=True)
        sysctlbyname = system.sysctlbyname
        sysctlbyname.argtypes = [
            ctypes.c_char_p, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_void_p, ctypes.c_size_t]
        sysctlbyname.restype = ctypes.c_int
        total = ctypes.c_uint64()
        total_size = ctypes.c_size_t(ctypes.sizeof(total))
        if sysctlbyname(
                b"hw.memsize", ctypes.byref(total),
                ctypes.byref(total_size), None, 0) != 0:
            return None

        mach_host_self = system.mach_host_self
        mach_host_self.restype = ctypes.c_uint32
        host_statistics64 = system.host_statistics64
        host_statistics64.argtypes = [
            ctypes.c_uint32, ctypes.c_int, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32)]
        host_statistics64.restype = ctypes.c_int
        mach_port_deallocate = system.mach_port_deallocate
        mach_port_deallocate.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        mach_port_deallocate.restype = ctypes.c_int
        task_self = ctypes.c_uint32.in_dll(
            system, "mach_task_self_").value
        host = mach_host_self()
        if not host:
            return None
        try:
            values = (ctypes.c_uint32 * 256)()
            count = ctypes.c_uint32(len(values))
            if host_statistics64(
                    host, host_vm_info64, values,
                    ctypes.byref(count)) != 0:
                return None
        finally:
            mach_port_deallocate(task_self, host)
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        available_pages = (
            int(values[free_index])
            + int(values[inactive_index])
            + int(values[speculative_index]))
        return max(
            0.0, min(1.0, available_pages * page_size / max(1, total.value)))
    except (AttributeError, OSError, ValueError):
        return None


def available_memory_fraction() -> float | None:
    """Best-effort host memory headroom for resident-cache release policy."""
    if WIN:
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                        ("total_phys", ctypes.c_ulonglong),
                        ("avail_phys", ctypes.c_ulonglong),
                        ("total_page", ctypes.c_ulonglong),
                        ("avail_page", ctypes.c_ulonglong),
                        ("total_virtual", ctypes.c_ulonglong),
                        ("avail_virtual", ctypes.c_ulonglong),
                        ("avail_extended", ctypes.c_ulonglong)]

        state = MemoryStatus()
        state.length = ctypes.sizeof(state)
        return (state.avail_phys / max(1, state.total_phys)
                if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(state)) else None)
    if sys.platform == "darwin":
        return _darwin_available_memory_fraction()
    try:
        values = {}
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                values[key] = int(value.strip().split()[0])
        return values.get("MemAvailable", 0) / max(1, values.get("MemTotal", 0))
    except (OSError, ValueError, IndexError):
        return None


def battery_state() -> "tuple[bool | None, int | None]":
    """Best-effort (on_battery, percent) without a resident dependency.

    AGREP_ON_BATTERY forces the AC/battery answer. None means unknown; background
    consumers (indexer policy, embed governor) read unknown as AC so a failed
    probe can never stall deferrable work.
    """
    forced = os.environ.get("AGREP_ON_BATTERY")
    if forced is not None:
        return forced.strip().lower() in ("1", "true", "yes", "on"), None
    if sys.platform == "win32":
        return _battery_state_windows()
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["pmset", "-g", "batt"], capture_output=True,
                                 text=True, encoding="utf-8", errors="replace",
                                 timeout=2).stdout.lower()
        except (OSError, subprocess.SubprocessError):
            return None, None
        match = re.search(r"(\d{1,3})%", out)
        percent = int(match.group(1)) if match else None
        if "battery power" in out:
            return True, percent
        if "ac power" in out:
            return False, percent
        return None, percent
    if sys.platform.startswith("linux"):
        try:
            online: list[bool] = []
            for entry in Path("/sys/class/power_supply").iterdir():
                kind = (entry / "type").read_text(encoding="ascii").strip().lower()
                if kind in ("mains", "usb", "usb_c"):
                    online.append(
                        (entry / "online").read_text(encoding="ascii").strip() == "1")
            if online:
                return not any(online), None
        except OSError:
            return None, None
    return None, None


def _battery_state_windows() -> "tuple[bool | None, int | None]":
    """GetSystemPowerStatus: ACLineStatus 0=battery/1=AC/255=unknown, percent
    255=unknown. Failure or unknown reads as unknown so a pass is never blocked
    wrongly."""
    try:
        import ctypes

        class _PowerStatus(ctypes.Structure):
            _fields_ = [("ACLineStatus", ctypes.c_ubyte),
                        ("BatteryFlag", ctypes.c_ubyte),
                        ("BatteryLifePercent", ctypes.c_ubyte),
                        ("SystemStatusFlag", ctypes.c_ubyte),
                        ("BatteryLifeTime", ctypes.c_uint32),
                        ("BatteryFullLifeTime", ctypes.c_uint32)]

        status = _PowerStatus()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            return None, None
        pct = int(status.BatteryLifePercent)
        on_battery = (True if status.ACLineStatus == 0
                      else False if status.ACLineStatus == 1 else None)
        return on_battery, pct if pct <= 100 else None
    except (AttributeError, OSError, ValueError):
        return None, None


_WINDOWS_CPU_SAMPLE: tuple[int, int, int] | None = None
_WINDOWS_CPU_LOCK = threading.Lock()


def _host_cpu_fraction_windows() -> float | None:
    """Busy CPU fraction between two native GetSystemTimes samples."""
    global _WINDOWS_CPU_SAMPLE
    try:
        import ctypes

        class _FileTime(ctypes.Structure):
            _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

        idle, kernel, user = _FileTime(), _FileTime(), _FileTime()
        with _WINDOWS_CPU_LOCK:
            if not ctypes.windll.kernel32.GetSystemTimes(
                    ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
                return None
            def value(item):
                return (int(item.high) << 32) | int(item.low)

            current = value(idle), value(kernel), value(user)
            previous, _WINDOWS_CPU_SAMPLE = _WINDOWS_CPU_SAMPLE, current
    except (AttributeError, OSError, ValueError):
        return None
    if previous is None:
        return None
    idle_delta, kernel_delta, user_delta = (
        current[index] - previous[index] for index in range(3))
    total = kernel_delta + user_delta
    if min(idle_delta, kernel_delta, user_delta) < 0 or total <= 0:
        return None
    return min(1.0, max(0.0, (total - idle_delta) / total))


def host_cpu_fraction() -> float | None:
    """Host pressure as a logical-CPU fraction.

    Windows reports interval utilization from GetSystemTimes (the first sample
    is unknown). POSIX reports one-minute runnable load per logical CPU.
    AGREP_HOST_CPU_FRACTION forces either answer.
    """
    forced = os.environ.get("AGREP_HOST_CPU_FRACTION")
    if forced is not None:
        try:
            return max(0.0, float(forced))
        except ValueError:
            return None
    if WIN:
        return _host_cpu_fraction_windows()
    try:
        return max(0.0, os.getloadavg()[0] / max(1, os.cpu_count() or 1))
    except (AttributeError, OSError):
        return None


def semantic_idle_seconds(requests: int) -> float:
    """Adaptive ONNX/vector residency shared by the CLI's semantic workers."""
    raw = os.environ.get("AGREP_SEM_IDLE_S")
    if raw:
        try:
            base = max(5.0, float(raw))
        except ValueError:
            base = 600.0
    else:
        try:
            matrix_bytes = EMBEDDINGS_PATH.stat().st_size
        except OSError:
            matrix_bytes = 0
        try:
            record = json.loads((EMBEDDINGS_PATH.parent / "embeddings.meta").read_bytes())
            if isinstance(record, dict) and record.get("version") == 2:
                matrix_bytes = sum(
                    int(segment["artifacts"]["f32"]["size"])
                    for segment in record.get("segments", ()))
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            pass
        # Size the lease by what residency actually holds, not by the matrix:
        # the vectors are file-backed and stay OS-reclaimable, while the model
        # session is ~100 MiB of private memory against a 512 MiB resident
        # budget. Reloading it costs 0.6-2.2 s, so releasing it to reclaim a
        # fifth of a budget we are not short of is a bad trade - and the
        # availability clamps below already surrender the whole lease the
        # moment memory is genuinely scarce. A first query therefore has to
        # outlive ordinary think time, or a caller working at human pace never
        # reaches the repeat tier and pays the cold load every single query.
        if matrix_bytes >= 1024 ** 3:
            base = 300.0 if requests >= 2 else 180.0
        elif matrix_bytes >= 256 * 1024 ** 2:
            base = 600.0 if requests >= 2 else 300.0
        else:
            base = 900.0 if requests >= 2 else 600.0
    available = available_memory_fraction()
    if available is not None and available < 0.10:
        return 5.0
    if available is not None and available < 0.15:
        return min(base, 30.0)
    if available is not None and available < 0.25:
        return min(base, 60.0)
    return base


def data_dir_usage(root: Path) -> dict[str, int]:
    """On-disk footprint of `root`, evaluated only by explicit status/doctor calls."""
    files = 0
    total = 0
    try:
        for path in root.rglob("*"):
            try:
                if path.is_file() and not path.is_symlink():
                    files += 1
                    total += path.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return {"files": files, "bytes": total}


def open_bounded_log(path: Path, *, max_bytes: int = 2 * 1024 * 1024,
                     backups: int = 2, data_root: Path | None = None):
    """Open a private append log, rotating it before it grows without bound."""
    if _protected_log_path(path, data_root=data_root):
        raise PermissionError(
            "AGREP_DATA_READONLY protects this data directory")
    try:
        if path.stat().st_size >= max_bytes:
            for index in range(backups, 0, -1):
                src = path if index == 1 else path.with_name(f"{path.name}.{index - 1}")
                dst = path.with_name(f"{path.name}.{index}")
                if src.exists():
                    try:
                        os.replace(src, dst)
                    except OSError:
                        pass
    except OSError:
        pass
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    if not WIN:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return os.fdopen(fd, "ab", buffering=0)


def _protected_log_path(
        path: Path, *, data_root: Path | None = None) -> bool:
    """Whether ``path`` is beneath the exact protected data-dir root."""
    root = DATA_DIR if data_root is None else data_root
    if not data_dir_readonly(root):
        return False
    try:
        target = os.path.normcase(os.path.realpath(os.fspath(path)))
        resolved_root = os.path.normcase(
            os.path.realpath(os.fspath(root)))
        return os.path.commonpath((target, resolved_root)) == resolved_root
    except (OSError, ValueError):
        return False
