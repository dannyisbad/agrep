"""Stderr logging for the hookless layer. `log` is canonical here (common.py
re-exports it); `dbg` stays duplicated in console.py by design: hookless imports
nothing from the product layer, and a small duplicate is cheaper than a
shared-utils dependency that would erode the boundary."""

from __future__ import annotations

import os
import sys
import time

DEBUG = os.environ.get("AGREP_DEBUG", "").lower() not in ("", "0", "false", "no", "off")
_DBG_T0 = time.time()
_TIMESTAMPS = False


def enable_timestamps() -> None:
    """Prefix log lines with wall-clock time. The daemon's stderr is its log
    file, and without stamps an incident there cannot be dated at all."""
    global _TIMESTAMPS
    _TIMESTAMPS = True


def disable_timestamps() -> None:
    """Reset seam for in-process callers of daemon entrypoints (tests): the
    enablement is process-permanent otherwise and poisons later bare logs."""
    global _TIMESTAMPS
    _TIMESTAMPS = False


def log(msg: str) -> None:
    """Stderr logging so stdout stays clean for any machine-readable output."""
    if _TIMESTAMPS:
        msg = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    print(msg, file=sys.stderr, flush=True)


def color_override() -> bool | None:
    """The environment's verdict on colour: NO_COLOR set and non-empty (no-color.org)
    disables it, FORCE_COLOR set and non-empty forces it, otherwise the stream decides.
    console.color_enabled applies this first; the hookless layer cannot import it."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return None


def dbg(msg: str, sign: str = "*") -> None:
    """Trace one line to stderr when AGREP_DEBUG is on. No-op otherwise."""
    if not DEBUG:
        return
    ms = (time.time() - _DBG_T0) * 1000.0
    line = f"{sign} [agrep +{ms:8.1f}ms] {msg}"
    try:
        override = color_override()
        if override if override is not None else sys.stderr.isatty():
            line = f"\033[2m{line}\033[0m"  # dim, like curl's verbose chatter
    except Exception:  # noqa: BLE001 -- isatty on an odd stream; just print plain
        pass
    print(line, file=sys.stderr, flush=True)
