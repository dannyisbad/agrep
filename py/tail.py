"""tilt tail: follow live agent-session events as JSON lines on stdout.

Drives the same LiveWatcher the explorer's /live/stream uses — no server, no
HTTP, stdlib only. One compact JSON object per line, flushed per line, so it
pipes cleanly into anything that watches stdout (e.g. an agent harness's
monitor regex: '"type":"done"').

Event types (see live.py _emit): user, reply, tool, tool_done, done, queued.
`done` is the turn-end signal; `done` with "why":"interrupted"/"stalled" covers
the unhappy paths. Default filter is done-only since "wake me when the other
agent's turn ends" is the headline use:

  python py/tail.py                          # turn ends, all agents
  python py/tail.py --agent claude           # one store
  python py/tail.py --events all             # firehose
  python py/tail.py --events done,tool       # turn ends + tool starts
  python py/tail.py --snapshot               # one-shot current state, then exit
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import time

import live


def _csv(values: list[str] | None) -> set[str] | None:
    if not values:
        return None
    out = {item.strip() for value in values for item in value.split(",") if item.strip()}
    return out or None


def _line(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=True, separators=(",", ":")), flush=True)


def main() -> int:
    p = argparse.ArgumentParser(prog="tilt tail", description=__doc__.splitlines()[0])
    p.add_argument("--agent", action="append",
                   help="store filter: claude/opencode/codex/antigravity (repeatable or comma-separated)")
    p.add_argument("--session", help="substring filter on the session id")
    p.add_argument("--events", default="done",
                   help='comma list of event types, or "all" (default: done = turn ends)')
    p.add_argument("--snapshot", action="store_true",
                   help="print the current active-session state once and exit")
    a = p.parse_args()

    w = live.watcher()

    if a.snapshot:
        # The watcher needs one poll loop to seed its session model.
        time.sleep(live.POLL_S * 2)
        _line(w.snapshot())
        return 0

    agents = _csv(a.agent)
    types = None if a.events.strip().lower() == "all" else _csv([a.events])

    q = w.subscribe()
    # Armed marker: lets a harness know the subscription is live (subscribers
    # only receive post-boot deltas, never the seed backfill).
    _line({"type": "tail_ready", "agents": sorted(agents) if agents else "all",
           "events": sorted(types) if types else "all"})
    try:
        while True:
            try:
                ev = q.get(timeout=30)
            except queue.Empty:
                continue
            if types and ev.get("type") not in types:
                continue
            if agents and ev.get("agent") not in agents:
                continue
            if a.session and a.session not in str(ev.get("session", "")):
                continue
            _line(ev)
    except KeyboardInterrupt:
        return 0
    finally:
        w.unsubscribe(q)


if __name__ == "__main__":
    sys.exit(main())
