"""Terminal-facing rendering primitives shared across agrep's surfaces.

Debug tracing, VT/color/UTF-8 console negotiation, terminal-safe text
escaping, snippet windowing, and the compact-row age/paint/meter vocabulary
all live here so every surface renders with one voice. Leaf layer: imports
only stdlib and its own layer, never the store or coordination hub above.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

from proc import WIN
import surface_policy as surface

# --- debug tracing --------------------------------------------------------
# AGREP_DEBUG=1: curl -v style stderr trace of freshness/ingest decisions; zero-cost when off.
# Signs mirror curl:  * info   > work kicked off   < result   ! failure/skip
DEBUG = os.environ.get("AGREP_DEBUG", "").lower() not in ("", "0", "false", "no", "off")
_DBG_T0 = time.time()


def dbg(msg: str, sign: str = "*") -> None:
    """Trace one line to stderr when AGREP_DEBUG is on. No-op otherwise."""
    if not DEBUG:
        return
    ms = (time.time() - _DBG_T0) * 1000.0
    line = f"{sign} [agrep +{ms:8.1f}ms] {msg}"
    try:
        if sys.stderr.isatty() and enable_vt():
            line = f"\033[2m{line}\033[0m"  # dim, like curl's verbose chatter
    except Exception:  # noqa: BLE001 -- isatty on an odd stream; just print plain
        pass
    print(line, file=sys.stderr, flush=True)


_PROFILE_T0 = float(os.environ.get("AGREP_T0") or time.perf_counter())
_PROFILE_LAPS: list[tuple[str, str, float]] = []


def lap(label: str, detail: str = "") -> None:
    """Mark a phase boundary for the slow-command self-explanation."""
    _PROFILE_LAPS.append((label, detail, time.perf_counter()))


def profile_report(threshold_s: float = 1.5) -> str | None:
    """One line naming where a command's time went.

    Returned always under AGREP_TIMING, else only past the threshold - a
    30ms call that turns into 7s must say why without anyone re-running it."""
    total = time.perf_counter() - _PROFILE_T0
    forced = (os.environ.get("AGREP_TIMING") or "").lower() not in (
        "", "0", "false", "no", "off")
    if total < threshold_s and not forced:
        return None
    parts, prev = [], _PROFILE_T0
    for label, detail, t in _PROFILE_LAPS:
        span = t - prev
        prev = t
        if span < 0.05 and not forced:
            continue
        name = f"{label}[{detail}]" if detail else label
        parts.append((name, span))
    tail = time.perf_counter() - prev
    if tail >= 0.05 or forced:
        parts.append(("rest", tail))
    if len(parts) == 1 and not forced:
        # One phase is the total; repeating it reads as two measurements.
        return f"took {total:.2f}s ({parts[0][0]})"
    detail = (" · ".join(f"{name} {span:.2f}s" for name, span in parts)
              if parts else "no phase detail recorded")
    return f"took {total:.2f}s: {detail}"


def timing_trace(label: str, payload: dict) -> None:
    """Keep requested timing output independent of the much noisier debug trace."""
    line = f"{label} {json.dumps(payload, separators=(',', ':'), sort_keys=True)}"
    if DEBUG:
        dbg(line)
    else:
        print(line, file=sys.stderr, flush=True)


def enable_vt() -> bool:
    """Turn on ANSI escape processing for this process's console streams. Windows
    conhost ships with VT processing off, so \\r\\x1b[K-style repaints render as
    literal garbage there until it's flipped on. Returns True when every
    console-attached std handle accepts VT (trivially true off Windows); False
    tells callers to paint without escape sequences."""
    if not WIN:
        return True
    try:
        import ctypes
        from ctypes import wintypes
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetStdHandle.argtypes = (wintypes.DWORD,)
        k32.GetStdHandle.restype = wintypes.HANDLE
        k32.GetConsoleMode.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k32.GetConsoleMode.restype = wintypes.BOOL
        k32.SetConsoleMode.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        k32.SetConsoleMode.restype = wintypes.BOOL
        ok = True
        for std_handle in (-11, -12):  # stdout, stderr
            h = k32.GetStdHandle(std_handle)
            mode = wintypes.DWORD()
            if not k32.GetConsoleMode(h, ctypes.byref(mode)):
                continue  # redirected stream, not a console - nothing paints there
            if not mode.value & ENABLE_VIRTUAL_TERMINAL_PROCESSING and not \
                    k32.SetConsoleMode(h, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING):
                ok = False
        return ok
    except (AttributeError, OSError):
        return False


def color_enabled(stream, when: str = "auto") -> bool:
    if when == "always":
        return True
    if when == "never":
        return False
    return bool(stream.isatty() and enable_vt())


def utf8_stdio() -> None:
    """Windows consoles default to cp1252 while snippets carry em-dashes and smart
    quotes; every CLI entry forces UTF-8 (errors=replace) before its first print so
    output renders and --json never crashes on encode."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 -- not a reconfigurable stream (piped/redirected oddly)
            pass


def terminal_safe(value: object, *, multiline: bool = False) -> str:
    """Render untrusted text without letting it issue terminal commands."""
    return surface.terminal_safe(value, multiline=multiline)


def shell_command(*argv: object, fallback: str = "<command>") -> str:
    """A copyable command only when the current shell family can quote it safely."""
    return surface.render_cli_argv(argv, windows=WIN) or fallback


def lowered_span_to_original(text: str, start: int, end: int) -> tuple[int, int]:
    """Map a ``text.lower()`` span back to the covering original-codepoint span.

    Most strings are length-preserving under ``lower`` and callers avoid this walk.
    Unicode characters such as U+0130 expand to multiple codepoints, so a lowered
    offset cannot be used to slice ``text`` directly. Python's default lowercase may
    change context-sensitive contents (for example final sigma), but its output length
    is the sum of each codepoint's lowercase width; walking those widths gives the
    original characters that cover a rare expanded match.
    """
    if start < 0 or end < start:
        raise ValueError("invalid lowered span")
    lowered_at = 0
    original_start = None
    if start == end == 0:
        return 0, 0
    for index, char in enumerate(text):
        next_lowered = lowered_at + len(char.lower())
        if original_start is None and start < next_lowered:
            original_start = index
        if original_start is not None and end <= next_lowered:
            return original_start, index + 1
        lowered_at = next_lowered
    if start == end == lowered_at:
        return len(text), len(text)
    raise ValueError("lowered span outside text")


def original_span_for_lowered(
        text: str, lowered: str, start: int, end: int) -> tuple[int, int]:
    """Return original indices for a known span of ``lowered == text.lower()``."""
    if start < 0 or end < start or end > len(lowered):
        raise ValueError("lowered span outside text")
    # A shifted index after an earlier expansion can land on a later copy (``İaa`` / ``a``);
    # equal total lengths prove every lower boundary is aligned.
    if len(text) == len(lowered) and text[start:end].lower() == lowered[start:end]:
        return start, end
    return lowered_span_to_original(text, start, end)


def insensitive_span(text: str, token: str,
                     lowered: str | None = None) -> tuple[int, int] | None:
    """Find one Python re.I substring while preserving original offsets."""
    low = text.lower() if lowered is None else lowered
    query = token.lower()
    start = low.find(query)
    if start >= 0:
        return original_span_for_lowered(text, low, start, start + len(query))
    match = re.search(re.escape(token), text, re.I)
    return match.span() if match is not None else None


def literal_word_pattern(value: str) -> re.Pattern:
    """Compile grep-style whole-word boundaries around one literal value."""
    return re.compile(r"(?<!\w)" + re.escape(value) + r"(?!\w)", re.I)


def one_line(text: object) -> str:
    """Collapse whitespace runs to single spaces so every rendered row stays grep-able."""
    return " ".join(str(text).split())


def snip_at(text: str, start: int, end: int, pad: int = 80) -> str:
    """A one-line window of `text` around [start,end), with ellipses + collapsed whitespace."""
    a, b = max(0, start - pad), min(len(text), end + pad)
    return one_line(("…" if a > 0 else "") + text[a:b] + ("…" if b < len(text) else ""))


def snip_spans(text: str, spans: list[tuple[int, int]], pad: int = 80) -> str:
    """Stitch bounded term windows so an any-order hit carries all its evidence.

    Snippet bytes are contract: corpusdb hydration, explore's scan, and search's
    stitched lanes must render identical strings, so all of them route through here.
    """
    windows: list[list[int]] = []
    for start, end in sorted(spans):
        a, b = max(0, start - pad), min(len(text), end + pad)
        if windows and a <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], b)
        else:
            windows.append([a, b])
    parts = []
    for a, b in windows:
        part = ("…" if a > 0 else "") + text[a:b] + ("…" if b < len(text) else "")
        parts.append(one_line(part))
    return " ".join(part for part in parts if part)


def age_label(ts_ms: int | float | None) -> str:
    """Compact-row age vocabulary: '5m', '3h', '2d', '1y'; '-' unknown.

    Shared by the compact renderer and doctor's drift report. Compact-width short
    forms (page budgets count bytes), minutes granularity below an hour, capped at
    years so ancient rows don't stretch columns. Live/status views keep their own
    seconds-granularity labels - a stopwatch, not a page budget.
    """
    if not ts_ms:
        return "-"
    seconds = max(0.0, time.time() - float(ts_ms) / 1000.0)
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86_400:
        return f"{int(seconds // 3600)}h"
    if seconds < 86_400 * 365:
        return f"{int(seconds // 86_400)}d"
    return f"{int(seconds // (86_400 * 365))}y"


PALETTE = surface.PALETTE


def paint(code: str, s: str, on: bool) -> str:
    return f"{PALETTE[code]}{s}{PALETTE['r']}" if on else s


def meter(done: int, total: int, width: int = 20) -> str:
    fill = int(width * done / total) if total else 0
    return "█" * fill + "░" * (width - fill)
