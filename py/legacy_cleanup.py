"""One-shot cleanup for the retired resident web explorer."""

from __future__ import annotations

import json
import os
import subprocess

from events import DATA_DIR, data_dir_readonly
from hookless._log import log
from index_lock import _unlink_if_unchanged
from proc import (
    WIN,
    pid_alive,
    process_start_identity,
    terminate_exact_process,
)


_REMOVED_EXPLORER_CHECKED = False


def retire_removed_explorer() -> None:
    """Retire the exact resident web explorer left behind by an older install."""
    global _REMOVED_EXPLORER_CHECKED
    if data_dir_readonly(DATA_DIR):
        return
    if _REMOVED_EXPLORER_CHECKED:
        return
    _REMOVED_EXPLORER_CHECKED = True
    path = DATA_DIR / ".server"
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
        info = json.loads(raw)
        pid = int(info["pid"])
        port = int(info.get("port") or 0)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return
    if pid == os.getpid():
        return
    if not pid_alive(pid):
        _unlink_if_unchanged(path, raw)
        return

    expected_start = str(info.get("process_start") or "")
    if expected_start not in ("", "unknown"):
        actual_start = process_start_identity(pid)
        if actual_start != expected_start:
            _unlink_if_unchanged(path, raw)
            return
        if info.get("mode") != "explorer":
            log(f"old agrep resident descriptor has an unknown mode; leaving pid {pid} alone")
            return
        if terminate_exact_process(pid, expected_start):
            log(f"retired removed web explorer (pid {pid}, port {port})")
            _unlink_if_unchanged(path, raw)
        return

    actual_start = process_start_identity(pid)
    if not actual_start:
        log("a legacy .server owner is still running but has no stable process identity; "
            f"leaving pid {pid} alone")
        return
    command = ""
    try:
        if WIN:
            probe = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=4,
                creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            probe = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=2)
        command = probe.stdout
    except (OSError, subprocess.SubprocessError):
        pass
    verified = "agrep" in command.lower() and "server.py" in command
    if verified:
        try:
            import urllib.request
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/status", timeout=2) as response:
                status = json.loads(response.read().decode("utf-8", "replace"))
            verified = isinstance(status, dict) and "semantic" in status and "indexer" in status
        except Exception:  # noqa: BLE001 -- an unverifiable legacy owner stays untouched
            verified = False
    if not verified:
        log("a legacy .server owner is still running but cannot be verified safely; "
            f"leaving pid {pid} alone")
        return
    if terminate_exact_process(pid, actual_start):
        log(f"retired verified legacy web explorer (pid {pid}, port {port})")
        _unlink_if_unchanged(path, raw)
