"""agrep tail: follow live agent-session events as JSON lines on stdout.

Drives the same LiveWatcher the board uses - no server, no
HTTP, stdlib only. One compact JSON object per line, flushed per line, so it
pipes cleanly into anything that watches stdout (e.g. an agent harness's
monitor regex: '"type":"done"').

Event types (see live.py _emit): user, reply, tool, tool_done, done, queued,
subagent_result.
`done` is the turn-end signal; `done` with "why":"interrupted"/"stalled" covers
the unhappy paths. Default filter is done-only since "wake me when the other
agent's turn ends" is the headline use:

  python py/tail.py                          # turn ends, all supported agents
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

from hookless import live, registry
import indexd_runtime
import surface_policy as surface


LIVE_EVENT_TYPES = frozenset({
    "user", "reply", "tool", "tool_done", "done", "queued",
    "subagent_result",
})
_ONESHOT_BOOT_TIMEOUT_S = 0.18
_UNSCOPED_COUNTERS = frozenset({
    "n_subs", "n_emitted", "n_tracked", "n_loops", "tick_ms",
    "work_ms", "loop_ms", "work_total_ms",
})


def _csv(values: list[str] | None) -> set[str] | None:
    if not values:
        return None
    out = {item.strip() for value in values for item in value.split(",") if item.strip()}
    return out or None


def _line(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=True, separators=(",", ":")), flush=True)


def main(argv: list[str] | None = None) -> int:
    live_aliases = tuple(
        alias for adapter in registry.REGISTRY.adapters
        if adapter.live.supported for alias in adapter.aliases)
    p = surface.ArgumentParser(
        prog="agrep tail", description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  agrep tail                    stream turn-end events as JSON lines\n"
               "  agrep tail --agent codex      stream Codex turn-end events only\n"
               "  agrep tail --snapshot         print active sessions once and exit\n"
               "\nexit: 0 snapshot complete or output closed; 2 invalid arguments; "
               "130 interrupted while streaming.",
        allow_abbrev=False)
    p.add_argument("--agent", action="append",
                   help=f"store filter: "
                        f"{'/'.join((*live.LIVE_AGENTS, *live_aliases))} "
                        "(repeatable or comma-separated)")
    p.add_argument("--session", "--chat", dest="session",
                   help="substring filter on the session/chat id")
    p.add_argument("--events", default="done",
                   help=f"comma list ({'/'.join(sorted(LIVE_EVENT_TYPES))}), "
                        'or "all" (default: done = turn ends)')
    p.add_argument("--snapshot", action="store_true",
                   help="print the current active-session state once and exit")
    a = surface.parse_args_with_presence(p, argv)
    # snapshot state has no done/queued rows to filter: a supplied --events
    # would render inert there, so the pair is refused, never dropped
    gated = surface.option_gate_error(a, surface.TAIL_OPTION_GATES)
    if gated:
        p.error(gated)

    blank = surface.blank_value_error(
        "--session/--chat", a.session, "a substring of a session id",
        "follow every chat")
    if blank:
        p.error(blank)
    agents = _csv(a.agent)
    if a.agent and not agents:
        p.error("--agent requires at least one supported agent name")
    if agents:
        agents = {agent.lower() for agent in agents}
        for agent in sorted(agents):
            error = registry.capability_error("live", agent)
            if error:
                p.error(error)
        # sessions carry canonical agent names; a validated alias (`agy`)
        # kept raw could never match, silently filtering everything out
        agents = {registry.normalize_agent_name(agent) for agent in agents}
    event_filter = a.events.strip().lower()
    if not event_filter:
        p.error('--events requires an event type or "all"')
    types = _csv([event_filter])
    if types and "all" in types:
        if types != {"all"}:
            p.error('"all" must be the only --events value')
        types = None
    elif types:
        unknown = sorted(types - LIVE_EVENT_TYPES)
        if unknown:
            p.error(f"unsupported event type: {', '.join(unknown)}")

    if a.snapshot:
        snapshot = indexd_runtime.resident_indexd_live_snapshot()
        if snapshot is None:
            w = live.watcher()
            w.wait_boot(_ONESHOT_BOOT_TIMEOUT_S)
            snapshot = w.snapshot()
        sessions = snapshot.get("sessions")
        if agents or a.session:
            snapshot = dict(snapshot)
            visible = [
                session for session in sessions
                if (not agents or session.get("agent") in agents)
                and (not a.session
                     or a.session in str(session.get("session", "")))
            ] if isinstance(sessions, list) else []
            snapshot["sessions"] = visible
            for key in _UNSCOPED_COUNTERS:
                snapshot.pop(key, None)
            snapshot.pop("last_err", None)
            snapshot["counts"] = {
                "sessions": len(visible),
                "working": sum(bool(row.get("working")) for row in visible),
                "recent_events": sum(
                    len(row.get("recent") or []) for row in visible),
            }
            degraded = snapshot.get("degraded_sources")
            if agents and isinstance(degraded, list):
                snapshot["degraded_sources"] = [
                    row for row in degraded if row.get("agent") in agents]
            elif a.session:
                snapshot.pop("degraded_sources", None)
        snapshot = dict(snapshot)
        snapshot["type"] = "snapshot"
        try:
            _line(snapshot)
        except BrokenPipeError:
            pass
        return 0

    w = live.watcher()
    q = w.subscribe()
    try:
        w.wait_boot()
        _line({"type": "tail_ready", "agents": sorted(agents) if agents else "all",
               "events": sorted(types) if types else "all"})
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
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 130
    finally:
        w.unsubscribe(q)


if __name__ == "__main__":
    sys.exit(main())
