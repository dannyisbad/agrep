#!/usr/bin/env python3
"""codex SessionStart(compact) hook payload.

The codex CLI fires SessionStart with source "compact" when a compaction
begins a new turn. This hook reads the event on stdin, and - ONLY for a
compaction - emits the post-compact recovery guidance as the JSON envelope
codex reads as additional context. It never fires on a normal prompt and
carries no per-message gate.

The payload text lives in codex-postcompact.md next to this script, written
for the codex model's prompting register (terse imperatives plus one worked
example, per OpenAI's model guidance) - NOT the claude summarizer contract,
whose audience is a summary writer, not a resumed agent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

MAX_INPUT = 65536
HOOKS_DIR = Path(__file__).resolve().parent
PAYLOAD = HOOKS_DIR / "codex-postcompact.md"

# Served when the md is unreadable - the mechanism must not go silent over a
# packaging fault.
GUIDANCE = (
    "This session resumed from a compaction. The summary is lossy: it "
    "records what was decided but routinely drops the exact values, names, "
    "and paths those decisions rest on. Before acting on this request, if "
    "the work needs something the summary refers to but does not state, "
    "run `agrep postcompact` to recover this same session's proven "
    "pre-boundary tail. Do not use `agrep recall` for this session's own "
    "facts: it hides the current context window and ranks this session's "
    "turns (marked ~self, `--self` overrides) against all history, while "
    "postcompact is exact and needs no query. If postcompact cannot "
    "identify the calling session, use `agrep postcompact --session <id>`."
)


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_INPUT + 1)
    if len(raw) > MAX_INPUT:
        return 0
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0
    event = payload.get("hook_event_name")
    source = payload.get("source")
    if event != "SessionStart" or source != "compact":
        # not a compaction: stay silent, never gate a normal prompt
        return 0
    try:
        context = PAYLOAD.read_text(encoding="utf-8")
    except OSError:
        context = GUIDANCE
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": context,
        },
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
