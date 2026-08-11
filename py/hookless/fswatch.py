"""Storeless liveness floor: file churn in the user's own project roots + process presence.

Some agents leave nothing tailable (GUI editors' agent modes: Cursor, Windsurf, Copilot
agent) and we didn't launch them, so neither store-tail nor a pty wrapper covers them.
This is the coverage FLOOR beneath both: watch the user's own project directories for
file-change activity and fuse it with process presence. It can only ever say "something
is editing files under project X right now" -- never what the conversation is. Rows
sourced here are liveness, not state.

Windows: one daemon thread per root blocks in ReadDirectoryChangesW -- 0% CPU until the
kernel wakes it (same family as teach.py's sentinel waiter, but RDCW instead of
FindFirstChangeNotification because the floor needs the CHANGED PATHS to drop noise
dirs and classify bursts). linux/mac: a bounded mtime-poll fallback works today;
inotify / FSEvents are the native upgrades (see _watch_posix_poll).

Attribution is honest-heuristic, in confidence order:
  0. "pid"    - an atomic-save temp name leaked the writer's pid (claude code writes
                `<file>.tmp.<pid>.<rand>`) and that pid is a known agent process: EXACT
  1. "agent"  - a known agent process (procscan) has cwd at/under the root. Real for
                codex (sits in its project dir); misses harness-launched claude on
                Windows (PEB cwd stays ~), which tier 0 catches instead
  2. "gui"    - a GUI agent app is running AND user input has been idle > IDLE_S while
                files churn: nobody is typing, yet source files are changing
  3. ""       - unattributed (user save, build, codegen); callers show dimly or not at all
Bulk bursts (>= BULK_N paths inside BULK_WINDOW_S) are branch switches / builds /
codegen, not editing -- labelled kind='bulk', never 'working'.
"""

from __future__ import annotations

import os
import queue
import re
import struct
import sys
import threading
import time
from collections import deque

# claude code's atomic save: `<file>.tmp.<pid>.<rand>` -- the temp name IS the writer id
_PID_TMP = re.compile(r"\.tmp\.(\d+)\.[0-9a-f]+$", re.I)

WIN = sys.platform == "win32"

ACTIVE_WINDOW_S = 90        # churn within this = the root is live (mirrors live.py)
IDLE_S = 8                  # no keyboard/mouse this long -> churn isn't the user typing
BULK_N = 40                 # this many paths inside BULK_WINDOW_S = checkout/build, not editing
BULK_WINDOW_S = 5
RING = 400                  # per-root recent-change ring

# Output/vendor trees: churn here is builds and installs, never agent editing.
_NOISE_DIRS = {".git", "node_modules", "target", "dist", "build", "out", "__pycache__",
               ".venv", "venv", ".next", ".idea", ".vs", ".cache", ".pytest_cache",
               ".mypy_cache", ".ruff_cache", "coverage"}
# Editor scratch: vim swap, atomic-save temporaries, partial downloads.
_TMP_SUFFIX = ("~", ".tmp", ".swp", ".swx", ".part", ".crdownload", ".partial")

# GUI agent hosts we can only see by process NAME (no per-session cmdline, no store we
# know how to tail). Presence alone is tier-2 evidence; churn+input-idle upgrades it.
_GUI_AGENT_EXES = {"cursor.exe": "cursor", "windsurf.exe": "windsurf",
                   "zed.exe": "zed", "trae.exe": "trae"}


def _noisy(relpath: str) -> bool:
    parts = relpath.replace("\\", "/").split("/")
    if any(p in _NOISE_DIRS for p in parts[:-1]):
        return True
    leaf = parts[-1].lower()
    return leaf.endswith(_TMP_SUFFIX) or leaf == "4913"  # vim's write-probe file


# ------------------------------------------------------------------ input idle (win)

def _input_idle_s() -> float | None:
    """Seconds since the user's last keyboard/mouse input (session-wide, no focus needed).
    None off-Windows or on failure. This is the cheapest 'nobody is typing' oracle."""
    if not WIN:
        return None
    import ctypes
    from ctypes import wintypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    li = LASTINPUTINFO()
    li.cbSize = ctypes.sizeof(li)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li)):
        return None
    # both are 32-bit ms tick counters, so the subtraction wraps consistently
    return max(0, (ctypes.windll.kernel32.GetTickCount() - li.dwTime) & 0xFFFFFFFF) / 1000.0


# ------------------------------------------------------------------ gui-agent presence

def _gui_agents() -> set[str]:
    """Names of running GUI agent apps ('cursor', 'windsurf', ...). Name-only toolhelp
    walk -- no cmdline read needed; presence is all tier-2 attribution uses."""
    if not WIN:
        try:  # linux: /proc names are free
            found = set()
            for pid in os.listdir("/proc"):
                if not pid.isdigit():
                    continue
                try:
                    with open(f"/proc/{pid}/comm", encoding="utf-8", errors="replace") as f:
                        n = f.read().strip().lower()
                except OSError:
                    continue
                if n + ".exe" in _GUI_AGENT_EXES or n in ("cursor", "windsurf", "zed"):
                    found.add(n)
            return found
        except OSError:
            return set()
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260)]

    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == wintypes.HANDLE(-1).value:
        return set()
    found: set[str] = set()
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if k32.Process32FirstW(snap, ctypes.byref(e)):
            while True:
                a = _GUI_AGENT_EXES.get(e.szExeFile.lower())
                if a:
                    found.add(a)
                if not k32.Process32NextW(snap, ctypes.byref(e)):
                    break
    finally:
        k32.CloseHandle(snap)
    return found


# ------------------------------------------------------------------ windows watcher

def _watch_windows(root: str, q: queue.Queue) -> None:
    """Blocking ReadDirectoryChangesW loop; each wake posts (root, relpath, ts) per
    changed path. Daemon thread: dies with the process, no teardown protocol needed."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    FILE_LIST_DIRECTORY = 0x1
    SHARE_ALL = 0x1 | 0x2 | 0x4          # read | write | delete: never block the writer
    OPEN_EXISTING = 3
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000  # required to open a directory handle
    # FILE_NAME | DIR_NAME | SIZE | LAST_WRITE -- creates, renames (atomic saves), writes
    NOTIFY = 0x1 | 0x2 | 0x8 | 0x10

    h = k32.CreateFileW(root, FILE_LIST_DIRECTORY, SHARE_ALL, None,
                        OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None)
    if not h or h == wintypes.HANDLE(-1).value:
        return
    buf = ctypes.create_string_buffer(64 * 1024)
    got = wintypes.DWORD()
    try:
        while True:
            if not k32.ReadDirectoryChangesW(h, buf, len(buf), True, NOTIFY,
                                             ctypes.byref(got), None, None):
                return  # handle died (root deleted / unmounted); aggregator just goes quiet
            ts = time.time()
            off = 0
            while off + 12 <= got.value:
                nxt, _action, fnlen = struct.unpack_from("<III", buf, off)
                name = buf.raw[off + 12: off + 12 + fnlen].decode("utf-16-le", "replace")
                q.put((root, name, ts))
                if not nxt:
                    break
                off += nxt
    finally:
        k32.CloseHandle(h)


# ------------------------------------------------------------------ posix fallback

def _watch_posix_poll(root: str, q: queue.Queue) -> None:
    """Portable fallback: bounded file-mtime poll. Correct but coarse -- the native
    upgrades are inotify (linux: IN_CLOSE_WRITE|IN_CREATE|IN_MOVED_TO, one watch per
    dir, added lazily as dirs appear) and FSEvents (mac: FSEventStreamCreate on the
    root, kFSEventStreamCreateFlagFileEvents, scheduled on a dedicated CFRunLoop
    thread). Both deliver paths, so the same _noisy filter and burst classifier apply
    unchanged; only this collection layer differs."""
    FILES_CAP = 20_000  # beyond this the walk is too hot to poll; degrade to silence
    seen: dict[str, float] = {}
    first = True
    while True:
        n = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _NOISE_DIRS]
            for f in filenames:
                n += 1
                if n > FILES_CAP:
                    break
                p = os.path.join(dirpath, f)
                try:
                    mt = os.stat(p).st_mtime
                except OSError:
                    continue
                old = seen.get(p)
                seen[p] = mt
                if old is not None and mt > old and not first:
                    q.put((root, os.path.relpath(p, root), time.time()))
            if n > FILES_CAP:
                break
        first = False
        time.sleep(2.5)


# ------------------------------------------------------------------ aggregator

class FsFloor:
    """Consumes change events, keeps per-root churn state, answers snapshot().
    One instance, threads spawned per root; all daemon, zero teardown."""

    def __init__(self, roots: list[str]):
        self.roots = [os.path.abspath(r) for r in roots if os.path.isdir(r)]
        self.q: queue.Queue = queue.Queue(maxsize=4000)
        self._recent: dict[str, deque] = {r: deque(maxlen=RING) for r in self.roots}
        self._writers: dict[str, dict[int, float]] = {r: {} for r in self.roots}  # pid -> last ts
        self._dedup: dict[tuple, float] = {}  # (root, path) -> last accept ts
        self._gui: set[str] = set()
        self._gui_ts = 0.0
        for r in self.roots:
            t = _watch_windows if WIN else _watch_posix_poll
            threading.Thread(target=t, args=(r, self.q), daemon=True,
                             name=f"agrep-fs:{os.path.basename(r)}").start()
        threading.Thread(target=self._drain, daemon=True, name="agrep-fs-drain").start()

    def _drain(self) -> None:
        while True:
            root, rel, ts = self.q.get()
            m = _PID_TMP.search(rel)
            if m:
                # writer identity, then drop the temp itself (its rename lands as the real file)
                self._writers[root][int(m.group(1))] = ts
                continue
            if _noisy(rel):
                continue
            # RDCW reports the parent dir itself when a child lands in it; the floor
            # tracks files (a still-existing dir entry is a duplicate of its child's event)
            if os.path.isdir(os.path.join(root, rel)):
                continue
            k = (root, rel)
            # RDCW fires several records per save (create+modify+modify); one per second is plenty
            if ts - self._dedup.get(k, 0.0) < 1.0:
                continue
            self._dedup[k] = ts
            if len(self._dedup) > 20_000:
                self._dedup.clear()
            self._recent[root].append((ts, rel))

    def _agent_procs(self) -> list[dict]:
        try:
            try:
                from hookless import procscan
            except ImportError:  # run as a bare script: sibling import
                import importlib
                procscan = importlib.import_module("procscan")
            return procscan.scan_agents()
        except Exception:  # noqa: BLE001 -- floor must degrade, never raise
            return []

    def snapshot(self) -> list[dict]:
        """One row per root with fresh churn:
        {root, project, n, last_ts, kind: edit|bulk, working, attrib: agent|gui|'',
         agent, idle_s, files: [up to 5 recent relpaths]}"""
        now = time.time()
        if now - self._gui_ts > 5:  # presence changes slowly; don't snapshot the
            self._gui = _gui_agents()   # process table on every poll
            self._gui_ts = now
        procs = self._agent_procs()
        idle = _input_idle_s()
        out = []
        for root, ring in self._recent.items():
            ev = [(t, p) for (t, p) in ring if now - t <= ACTIVE_WINDOW_S]
            if not ev:
                continue
            # bulk = many paths landing nearly at once (checkout/build/codegen)
            bulk = False
            times = [t for t, _ in ev]
            for i in range(len(times)):
                j = i
                while j < len(times) and times[j] - times[i] <= BULK_WINDOW_S:
                    j += 1
                if j - i >= BULK_N:
                    bulk = True
                    break
            rl = root.replace("\\", "/").rstrip("/").lower()
            wpids = {pid for pid, t in self._writers[root].items()
                     if now - t <= ACTIVE_WINDOW_S}
            writer = next((p for p in procs if p["pid"] in wpids), None)
            owner = next((p for p in procs
                          if (p.get("cwd") or "").replace("\\", "/").rstrip("/").lower()
                          .startswith(rl)), None)
            if writer:  # temp-name pid matched a live agent process: exact
                attrib, agent = "pid", writer["agent"]
            elif owner:
                attrib, agent = "agent", owner["agent"]
            elif self._gui and idle is not None and idle > IDLE_S:
                # nobody is typing, a GUI agent app is up, and files are changing
                attrib, agent = "gui", "/".join(sorted(self._gui))
            else:
                attrib, agent = "", ""
            out.append({
                "root": root, "project": os.path.basename(root),
                "n": len(ev), "last_ts": int(ev[-1][0] * 1000),
                "kind": "bulk" if bulk else "edit",
                # 'working' only for edit-shaped churn tied to some agent evidence;
                # bare churn stays visible but unclaimed
                "working": (not bulk) and bool(attrib),
                "attrib": attrib, "agent": agent,
                # exact writer's session id rides along so the caller can
                # suppress churn the store-tail already covers
                "session": (writer or {}).get("session") if writer else None,
                "idle_s": round(idle, 1) if idle is not None else None,
                "files": [p for _, p in ev[-5:]],
            })
        return out


if __name__ == "__main__":  # demo: python fswatch.py <root>... then churn files
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    roots = sys.argv[1:] or [os.getcwd()]
    fl = FsFloor(roots)
    print(f"watching {len(fl.roots)} root(s): {', '.join(fl.roots)}")
    last = ""
    while True:
        time.sleep(1.0)
        rows = fl.snapshot()
        cur = repr(rows)
        if cur != last:
            last = cur
            for r in rows:
                print(f"[{time.strftime('%H:%M:%S')}] {r['project']}: {r['kind']} "
                      f"n={r['n']} working={r['working']} attrib={r['attrib'] or '-'}"
                      f"{' agent=' + r['agent'] if r['agent'] else ''} "
                      f"idle={r['idle_s']}s files={r['files']}")
            if not rows:
                print(f"[{time.strftime('%H:%M:%S')}] quiet")
