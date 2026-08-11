"""Passive process-table scan: ground-truth liveness for running agents.

Zero setup, no wrapper, no PATH shim. Reads the OS process table and finds agent
processes (claude/codex/opencode/...) by image name + command line. The command line
carries the session id for most CLIs (`claude --resume <id>`, `codex resume <id>`,
`opencode --session <id>`), so a process maps to its session EXACTLY. Process alive =
ground-truth `working`; process gone = ground-truth `done`. Works for any terminal,
PTY, tmux pane, IDE terminal, or GUI app — we observe the process, not its tty.

Windows: ctypes read of the PEB (command line + cwd), no subprocess, no deps.
Linux:   /proc/<pid>/cmdline + /proc/<pid>/cwd.
macOS:   sysctl KERN_PROCARGS2 (best-effort).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime

WIN = sys.platform == "win32"
LINUX = sys.platform.startswith("linux")
MAC = sys.platform == "darwin"

# A 36-char UUID, or opencode's `ses_...`, or a codex rollout uuid.
_UUID = re.compile(r"\b([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\b")
_SES = re.compile(r"\b(ses_[A-Za-z0-9]{6,})\b")


def _session_from_cmd(agent: str, cmd: str) -> str | None:
    """Pull the session id out of an agent's command line, if it resumed one."""
    if agent == "opencode":
        m = _SES.search(cmd)
        return m.group(1) if m else None
    m = _UUID.search(cmd)
    return m.group(1) if m else None


def _classify(name: str, cmd: str) -> str | None:
    """Map an (exe name, command line) to an agent, or None if it's not an agent."""
    n = (name or "").lower()
    c = (cmd or "").lower()
    # Direct-named CLIs / apps.
    if n in ("claude.exe", "claude"):
        return "claude"
    if n in ("codex.exe", "codex"):
        return "codex"
    if n in ("opencode.exe", "opencode"):
        return "opencode"
    if n in ("kimi.exe", "kimi"):
        return "kimi"
    if n in ("agy.exe", "agy", "antigravity.exe", "antigravity"):
        return "antigravity"
    # Node/bun-hosted CLIs: identify by the script in the command line.
    if n in ("node.exe", "node", "bun.exe", "bun"):
        if "opencode" in c:
            return "opencode"
        if re.search(r"\bcli\.js\b.*\bclaude\b|\bclaude\b.*\.js", c) or "@anthropic-ai/claude" in c:
            return "claude"
        if "codex" in c and "node_modules" in c:
            return "codex"
    return None


# ------------------------------------------------------------------ Windows

def _scan_windows() -> list[dict]:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)

    TH32CS_SNAPPROCESS = 0x00000002
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_VM_READ = 0x10

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    class PROCESS_BASIC_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("Reserved1", ctypes.c_void_p),
            ("PebBaseAddress", ctypes.c_void_p),
            ("Reserved2", ctypes.c_void_p * 2),
            ("UniqueProcessId", ctypes.c_void_p),
            ("Reserved3", ctypes.c_void_p),
        ]

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD),
                    ("dwHighDateTime", wintypes.DWORD)]

    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.ReadProcessMemory.argtypes = [
        wintypes.HANDLE, wintypes.LPCVOID, wintypes.LPVOID,
        ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]

    # x64 offsets: PEB.ProcessParameters, RTL_USER_PROCESS_PARAMETERS.{CurrentDir,CommandLine}
    OFF_PARAMS = 0x20
    OFF_CWD = 0x38       # CurrentDirectory.DosPath (UNICODE_STRING)
    OFF_CMDLINE = 0x70   # CommandLine (UNICODE_STRING)

    def _read(h, addr, size):
        buf = (ctypes.c_char * size)()
        n = ctypes.c_size_t(0)
        if not k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size, ctypes.byref(n)):
            return None
        return buf.raw[: n.value]

    def _read_ptr(h, addr):
        raw = _read(h, addr, 8)
        return int.from_bytes(raw, "little") if raw and len(raw) == 8 else None

    def _read_ustr(h, addr):
        # UNICODE_STRING: Length(USHORT)@0, Buffer(PWSTR)@8
        head = _read(h, addr, 16)
        if not head or len(head) < 16:
            return None
        length = int.from_bytes(head[0:2], "little")
        buffer = int.from_bytes(head[8:16], "little")
        if not length or not buffer:
            return None
        data = _read(h, buffer, min(length, 0x4000))
        if not data:
            return None
        return data.decode("utf-16-le", "replace")

    def _peb_strings(pid):
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ, False, pid)
        if not h:
            return None, None, None
        try:
            pbi = PROCESS_BASIC_INFORMATION()
            ret_len = ctypes.c_ulong(0)
            status = ntdll.NtQueryInformationProcess(
                h, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), ctypes.byref(ret_len))
            cmd = cwd = None
            if status == 0 and pbi.PebBaseAddress:
                params = _read_ptr(h, pbi.PebBaseAddress + OFF_PARAMS)
                if params:
                    cmd = _read_ustr(h, params + OFF_CMDLINE)
                    cwd = _read_ustr(h, params + OFF_CWD)
            # creation time -> unix epoch
            ct, et, kt, ut = FILETIME(), FILETIME(), FILETIME(), FILETIME()
            create_ts = 0.0
            if k32.GetProcessTimes(h, ctypes.byref(ct), ctypes.byref(et),
                                   ctypes.byref(kt), ctypes.byref(ut)):
                ft = (ct.dwHighDateTime << 32) | ct.dwLowDateTime
                if ft:
                    create_ts = ft / 1e7 - 11644473600.0  # 100ns since 1601 -> unix
            return cmd, cwd, create_ts
        finally:
            k32.CloseHandle(h)

    # 1) cheap snapshot of all (pid, name)
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == wintypes.HANDLE(-1).value or not snap:
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
    out: list[dict] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not k32.Process32FirstW(snap, ctypes.byref(entry)):
            raise OSError(ctypes.get_last_error(), "Process32FirstW failed")
        candidates: list[tuple[int, str]] = []
        while True:
            candidates.append((entry.th32ProcessID, entry.szExeFile))
            if not k32.Process32NextW(snap, ctypes.byref(entry)):
                break
    finally:
        k32.CloseHandle(snap)

    # 2) for name-candidates (and node/bun hosts), read cmdline to classify precisely
    HOSTS = {"node.exe", "bun.exe"}
    NAMEHIT = re.compile(r"^(claude|codex|opencode|kimi|agy|antigravity)", re.I)
    for pid, name in candidates:
        nl = name.lower()
        looks = bool(NAMEHIT.match(nl)) or nl in HOSTS
        if not looks:
            continue
        cmd, cwd, create_ts = _peb_strings(pid)
        agent = _classify(name, cmd or name)
        if not agent:
            continue
        out.append({
            "pid": pid, "name": name, "agent": agent,
            "cmd": cmd or "", "cwd": cwd or "",
            "session": _session_from_cmd(agent, cmd or ""),
            "create_ts": create_ts,
        })
    return out


# ------------------------------------------------------------------ Linux

def _scan_linux() -> list[dict]:
    out: list[dict] = []
    entries = os.listdir("/proc")
    numeric = 0
    readable = 0
    for pid_s in entries:
        if not pid_s.isdigit():
            continue
        numeric += 1
        pid = int(pid_s)
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                raw = f.read()
        except OSError:
            continue
        readable += 1
        if not raw:
            continue
        argv = raw.split(b"\x00")
        cmd = " ".join(a.decode("utf-8", "replace") for a in argv if a)
        name = os.path.basename(argv[0].decode("utf-8", "replace")) if argv else ""
        agent = _classify(name, cmd)
        if not agent:
            continue
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            cwd = ""
        try:
            create_ts = os.stat(f"/proc/{pid}").st_ctime
        except OSError:
            create_ts = 0.0
        out.append({"pid": pid, "name": name, "agent": agent, "cmd": cmd,
                    "cwd": cwd, "session": _session_from_cmd(agent, cmd),
                    "create_ts": create_ts})
    if numeric and not readable:
        raise PermissionError("no process command lines were readable")
    return out


def _mac_procargs(pid: int) -> list[str]:
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    mib = (ctypes.c_int * 3)(1, 49, pid)
    size = ctypes.c_size_t(0)
    if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0 or size.value < 5:
        return []
    buf = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
        return []
    raw = buf.raw[:size.value]
    argc = int.from_bytes(raw[:4], sys.byteorder, signed=True)
    if argc <= 0:
        return []
    pos = raw.find(b"\0", 4)
    if pos < 0:
        return []
    pos += 1
    while pos < len(raw) and raw[pos] == 0:
        pos += 1
    argv = []
    while pos < len(raw) and len(argv) < argc:
        end = raw.find(b"\0", pos)
        if end < 0:
            break
        argv.append(raw[pos:end].decode("utf-8", "replace"))
        pos = end + 1
    return argv


def _mac_cwd(libproc, pid: int) -> str:
    import ctypes

    vnode_info_size = 152
    max_path = 1024
    record_size = 2 * (vnode_info_size + max_path)
    buf = ctypes.create_string_buffer(record_size)
    got = libproc.proc_pidinfo(
        pid, 9, 0, ctypes.byref(buf), ctypes.sizeof(buf))
    if got != record_size:
        return ""
    raw = buf.raw[vnode_info_size:vnode_info_size + max_path]
    return raw.split(b"\0", 1)[0].decode("utf-8", "replace")


_MAC_BIRTH_API = None


def _mac_birth_api():
    global _MAC_BIRTH_API
    if _MAC_BIRTH_API is not None:
        return _MAC_BIRTH_API
    import ctypes

    class ProcBsdInfo(ctypes.Structure):
        _fields_ = [
            ("flags", ctypes.c_uint32), ("status", ctypes.c_uint32),
            ("xstatus", ctypes.c_uint32), ("pid", ctypes.c_uint32),
            ("ppid", ctypes.c_uint32), ("uid", ctypes.c_uint32),
            ("gid", ctypes.c_uint32), ("ruid", ctypes.c_uint32),
            ("rgid", ctypes.c_uint32), ("svuid", ctypes.c_uint32),
            ("svgid", ctypes.c_uint32), ("reserved", ctypes.c_uint32),
            ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32),
            ("nfiles", ctypes.c_uint32), ("pgid", ctypes.c_uint32),
            ("pjobc", ctypes.c_uint32), ("tdev", ctypes.c_uint32),
            ("tpgid", ctypes.c_uint32), ("nice", ctypes.c_int32),
            ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64),
        ]

    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    pidinfo = library.proc_pidinfo
    pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                        ctypes.c_void_p, ctypes.c_int]
    pidinfo.restype = ctypes.c_int
    _MAC_BIRTH_API = ctypes, ProcBsdInfo, pidinfo
    return _MAC_BIRTH_API


def _process_birth(pid: int) -> tuple[str, float]:
    try:
        ctypes, info_type, pidinfo = _mac_birth_api()
        info = info_type()
        got = pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
    except (AttributeError, OSError, OverflowError, ValueError):
        return "", 0.0
    if got != ctypes.sizeof(info) or not info.start_sec:
        return "", 0.0
    identity = f"darwin_{int(info.start_sec)}_{int(info.start_usec)}"
    return identity, float(info.start_sec) + float(info.start_usec) / 1_000_000


def _scan_macos_native() -> list[dict]:
    import ctypes

    libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    libproc.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32,
                                      ctypes.c_void_p, ctypes.c_int]
    libproc.proc_listpids.restype = ctypes.c_int
    libproc.proc_name.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    libproc.proc_name.restype = ctypes.c_int
    libproc.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int,
                                     ctypes.c_uint64, ctypes.c_void_p,
                                     ctypes.c_int]
    libproc.proc_pidinfo.restype = ctypes.c_int
    needed = libproc.proc_listpids(1, 0, None, 0)
    if needed <= 0:
        raise OSError(ctypes.get_errno(), "proc_listpids failed")
    capacity = max(needed + 4096, 8192)
    for _attempt in range(3):
        pids = (ctypes.c_int * (capacity // ctypes.sizeof(ctypes.c_int)))()
        used = libproc.proc_listpids(1, 0, pids, ctypes.sizeof(pids))
        if used < 0:
            raise OSError(ctypes.get_errno(), "proc_listpids failed")
        if used < ctypes.sizeof(pids):
            break
        capacity *= 2
    else:
        raise OSError("process census changed faster than it could be snapshotted")
    out = []
    for pid in pids[:used // ctypes.sizeof(ctypes.c_int)]:
        if pid <= 0:
            continue
        namebuf = ctypes.create_string_buffer(1024)
        if libproc.proc_name(pid, namebuf, len(namebuf)) <= 0:
            continue
        name = namebuf.value.decode("utf-8", "replace")
        lower = name.lower()
        if (not re.match(r"^(claude|codex|opencode|kimi|agy|antigravity)", lower)
                and lower not in {"node", "bun"}):
            continue
        argv = _mac_procargs(pid)
        cmd = " ".join(argv) if argv else name
        agent = _classify(name, cmd)
        if not agent:
            continue
        process_start, create_ts = _process_birth(pid)
        out.append({"pid": pid, "name": name, "agent": agent, "cmd": cmd,
                    "cwd": _mac_cwd(libproc, pid),
                    "session": _session_from_cmd(agent, cmd),
                    "process_start": process_start, "create_ts": create_ts})
    return out


def _scan_macos_ps() -> list[dict]:
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,lstart=,comm=,args="],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=3, check=False, env={**os.environ, "LC_ALL": "C"})
    if result.returncode != 0:
        raise OSError(result.stderr.strip() or f"ps exited {result.returncode}")
    out = []
    for raw in result.stdout.splitlines():
        fields = raw.split(None, 7)
        if len(fields) < 7 or not fields[0].isdigit():
            continue
        pid = int(fields[0])
        started = " ".join(fields[1:6])
        name = os.path.basename(fields[6])
        cmd = fields[7] if len(fields) > 7 else fields[6]
        agent = _classify(name, cmd)
        if not agent:
            continue
        try:
            create_ts = datetime.strptime(
                started, "%a %b %d %H:%M:%S %Y").timestamp()
        except ValueError:
            create_ts = 0.0
        out.append({"pid": pid, "name": name, "agent": agent, "cmd": cmd,
                    "cwd": "", "session": _session_from_cmd(agent, cmd),
                    "create_ts": create_ts})
    return out


def _scan_macos() -> list[dict]:
    try:
        return _scan_macos_native()
    except (OSError, AttributeError):
        return _scan_macos_ps()


def scan_agents_checked() -> tuple[list[dict], str]:
    """Return process evidence and a reason when the platform census was unavailable."""
    try:
        if WIN:
            return _scan_windows(), ""
        if LINUX:
            return _scan_linux(), ""
        if MAC:
            return _scan_macos(), ""
        return [], f"process census is unsupported on {sys.platform}"
    except Exception as error:  # noqa: BLE001 -- callers need unavailable, not a crash
        return [], f"{type(error).__name__}: {error}"


def scan_agents() -> list[dict]:
    """Every running agent process: [{pid, name, agent, cmd, cwd, session, create_ts}].
    Empty list on any platform we can't read (callers degrade to file-tail heuristics)."""
    return scan_agents_checked()[0]


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    import time
    t = time.time()
    procs = scan_agents()
    dt = (time.time() - t) * 1000
    print(f"scanned in {dt:.0f}ms -> {len(procs)} agent process(es)")
    for p in procs:
        print(f"  pid={p['pid']:>6} {p['agent']:11} session={p['session']} "
              f"cwd={p['cwd'][:40]!r}")
        print(f"         cmd={p['cmd'][:90]!r}")
