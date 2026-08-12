"""`agrep around <session> <turn>` - the context window around a search hit.

    agrep around 11111111 144          # ±4 turns, root/main prose by default
    agrep around @11111111:144         # a precise handle: the named turn alone (-C 0)
    agrep around 11111111:144 --context 10   # wider window, colon form pastes from --json
    agrep around 11111111 144 --full         # uncap indexed messages (deep read: -C 0)
    agrep around 11111111 144 --json   # one object per message/event, for piping

Search tells you WHICH session touched a thing; around tells you WHAT happened -
the local story of a hit (error, attempts, fix) for a few KB instead of a whole
transcript. A pasted @session:turn handle names one exact turn, never a nearby
clamped turn, and defaults to -C 0; the positional session+turn form keeps the
±4 radius. Digest-bound handles are role-aware: a prose handle omits generic
event noise, while a tool handle retains only its exact cited event. Positional
and bare-session exploration also defaults to root/main prose; ``--who tool`` or
``--tool-output N`` explicitly opts into tools. ``--full`` restores the
same-window forensic stream. Source truncation is labeled.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from typing import NamedTuple

import common
import compact
import console
import display_policy
import explore
import indexd_runtime
import session_context
import surface_policy as surface

_C = surface.PALETTE


# Tool events dominate oversized pulls (measured: 92KB with --full vs 6KB
# bare on one tool-heavy turn); past this many rendered bytes the run owes
# the caller one stderr line naming the cheaper form - never a truncation.
_PULL_WARN_BYTES = 30_000
_stdout_bytes = 0


def _stdout_print(value: object = "") -> None:
    """Print one stdout line and count its rendered bytes."""
    global _stdout_bytes
    print(value)
    _stdout_bytes += len(str(value)) + 1


def _warn_if_oversized(argv: list[str] | None) -> None:
    """One stderr line when a window ran large; silent for bounded pulls."""
    if _stdout_bytes < _PULL_WARN_BYTES or "--no-tools" in (argv or []):
        return
    print(f"large pull: ~{_stdout_bytes // 1024}KB rendered - "
          "`--no-tools` keeps the conversation only",
          file=sys.stderr)


def _color_on(when: str) -> bool:
    return common.color_enabled(sys.stdout, when)


def _expand_command(target: str, turn: int) -> str:
    return console.shell_command(
        "agrep", "around", target, turn, "-C", 0, "--full",
        fallback="agrep around <session> <turn> -C 0 --full")


class _DigestRescue(NamedTuple):
    """One rescue outcome per HANDLE_IDENTITY.md's table: a unique holder,
    several equal claims (listed, refused), or content absent (the loss
    refusal). Ambiguity and absence are distinct rows, never one None.

    ``event_identity`` carries tool ownership only when the verified digest
    names one event and no prose field on that turn. This recovers the exact
    event for pre-event-suffix tool handles without guessing from ambient
    tools.
    """
    outcome: str  # "unique" | "ambiguous" | "absent"
    turn: int | None = None
    candidates: tuple[int, ...] = ()
    event_identity: str | None = None


def _digest_claims(
        window: dict, center: int, digest: str,
        event_identity: str | None = None,
) -> _DigestRescue:
    """Which turns of this window hold the cited content? The center holding
    verifies in place; otherwise every equal claim is reported, so the caller
    can tell a recoverable ambiguity from content that is gone."""
    events: dict[int, list[tuple[str, str | None]]] = {}
    session = str(window.get("session") or "")
    for event in window.get("events") or []:
        text = common.tool_search_text(event)
        if text:
            identity = common.tool_event_identity(
                session, event.get("turn"), event.get("ts"), text)
            events.setdefault(int(event.get("turn", -1)), []).append(
                (text, identity))

    def claim(turn: dict) -> tuple[bool, str | None]:
        number = int(turn.get("turn", -1))
        matching_events = [
            identity for text, identity in events.get(number, ())
            if compact.content_digest(text) == digest
            and (event_identity is None or identity == event_identity)
        ]
        if event_identity is not None:
            return (bool(matching_events),
                    event_identity if matching_events else None)
        prose_match = any(
            text and compact.content_digest(text) == digest
            for text in (turn.get("text") or "", turn.get("reply") or ""))
        if prose_match:
            return True, None
        if len(matching_events) == 1 and matching_events[0] is not None:
            return True, matching_events[0]
        return bool(matching_events), None

    turns = window.get("turns") or []
    for turn in turns:
        if int(turn.get("turn", -1)) == center:
            held, owner = claim(turn)
            if held:
                return _DigestRescue(
                    "unique", center, event_identity=owner)
            break
    matches = []
    for turn in turns:
        held, owner = claim(turn)
        if held:
            matches.append((int(turn["turn"]), owner))
    if len(matches) == 1:
        turn, owner = matches[0]
        return _DigestRescue("unique", turn, event_identity=owner)
    if matches:
        return _DigestRescue(
            "ambiguous", None, tuple(turn for turn, _owner in matches))
    return _DigestRescue("absent")


def _verify_handle_digest(window: dict, center: int,
                          digest: str,
                          event_identity: str | None = None) -> int | None:
    """The unique holder (center when unmoved) or None. Callers that must
    tell ambiguity from absence read _digest_claims instead."""
    rescue = _digest_claims(window, center, digest, event_identity)
    return rescue.turn if rescue.outcome == "unique" else None


def _resolve_handle_claims(window: dict, session: str, center: int,
                           digest: str,
                           event_identity: str | None = None) -> _DigestRescue:
    rescue = _digest_claims(window, center, digest, event_identity)
    if rescue.outcome == "unique" and rescue.turn == center:
        return rescue
    first = int(window.get("first_turn", center))
    last = int(window.get("last_turn", center))
    # floor keeps the moved-content rescue at the old default reach even when
    # the displayed window is -C 0 (handles default to the named turn alone)
    radius = max(4, abs(center - first), abs(last - center))
    full = explore.get_window(session, center, radius)
    if "error" in full:
        return _DigestRescue("absent")
    return _digest_claims(full, center, digest, event_identity)


def _resolve_handle_digest(window: dict, session: str, center: int,
                           digest: str,
                           event_identity: str | None = None) -> int | None:
    rescue = _resolve_handle_claims(
        window, session, center, digest, event_identity)
    return rescue.turn if rescue.outcome == "unique" else None


def _digest_candidate(
        cands: list[str], center: int, digest: str,
        event_identity: str | None = None,
) -> str | None:
    """An ambiguous prefix carrying a digest is not ambiguous when exactly one
    candidate holds the cited content: the digest pins content, and pinning is
    what it is for."""
    windows = explore.get_windows([(session, center, 0) for session in cands])
    holders = [session for session, window in zip(cands, windows)
               if "error" not in window and window.get("center") == center
               and _verify_handle_digest(
                   window, center, digest, event_identity) == center]
    return holders[0] if len(holders) == 1 else None


def _serve(notes: list[dict], json_output: bool, kind: str, **fields) -> None:
    """Record one divergence between the asked-for window and the served one,
    and render it once: stderr for a human, the --json meta record for a pipe."""
    note = surface.around_service_note(kind, **fields)
    notes.append(note)
    if not json_output:
        common.log(note["note"])


def _fail(json_output: bool, code: str, reason: str, **fields) -> int:
    """Emit one error on the channel owned by the selected output contract."""
    if json_output:
        _stdout_print(json.dumps(
            {"kind": "agrep-meta",
             "error": {"code": code, "reason": reason, **fields}},
            ensure_ascii=False, separators=(",", ":")))
    else:
        common.log(reason)
    return 2


def _unchecked_freshness() -> dict:
    """Describe --no-auto without running the source census it disabled."""
    drift = indexd_runtime.DriftReport(
        "unknown", code="freshness-unchecked",
        detail=indexd_runtime.NO_AUTO_REFRESH_REASON)
    return indexd_runtime.machine_freshness(
        checked=False, drift_report=drift, publication_converging=False)


def _miss(json_output: bool, code: str, reason: str, *,
          checked: bool = True, **fields) -> int:
    """Render one scoped empty selection; unchecked absence is unverified."""
    if json_output:
        row = {"kind": "agrep-meta",
               "miss": {"code": code, "reason": reason, **fields}}
        if not checked:
            row["freshness"] = _unchecked_freshness()
        _stdout_print(json.dumps(row, ensure_ascii=False))
    else:
        suffix = (f"; {indexd_runtime.NO_AUTO_REFRESH_REASON}"
                  if not checked else "")
        common.log(reason + suffix)
    return 1 if checked else 2


def _stale_handle_reason(detail: str | None = None) -> str:
    state = (f"result handle is stale: {detail}" if detail else
             "result handle is stale or its chat was pruned")
    return f"{state}; rerun the search for a current handle"


_LATEST_TURN = (1 << 63) - 1


def _parse_target(args_session: str, args_turn: str | None,
                  json_output: bool) -> tuple[str, int]:
    """Accept `around <session> <turn>` and `around <session>:<turn>` (the colon form
    pastes straight from a --json hit's fields). Exit 2 on an unparseable turn."""
    s = args_session
    if ((s.strip().startswith("@") and ":" in s)
            or compact.is_result_handle(s)):
        try:
            parsed = compact.parse_result_handle(s)
        except compact.CompactError as exc:
            raise SystemExit(
                _fail(json_output, "bad-target", str(exc))) from exc
        if args_turn is not None:
            raise SystemExit(_fail(
                json_output, "bad-target",
                "a result handle already includes its turn"))
        return parsed
    # A printed bare @prefix addresses that chat's latest indexed turn. An
    # unprefixed session remains positional and therefore still needs a turn.
    bare_session_handle = s.strip().startswith("@") and args_turn is None
    try:
        s = compact.normalize_session_arg(s)
    except compact.CompactError as exc:
        raise SystemExit(
            _fail(json_output, "bad-target", str(exc))) from exc
    if args_turn is None and ":" in s:
        s, _, t = s.rpartition(":")
        args_turn = t
    if args_turn is None:
        if bare_session_handle:
            return s, _LATEST_TURN
        raise SystemExit(_fail(
            json_output, "bad-target",
            "need a turn: `agrep around <session> <turn>` "
            "(turns come from `agrep <pattern> --json`)."))
    try:
        return s, int(args_turn)
    except ValueError:
        raise SystemExit(_fail(
            json_output, "bad-target",
            f"turn must be an integer, got {args_turn!r}."))


def _cap(text: str, limit: int, expand_cmd: str) -> tuple[str, int]:
    """Cap text at a whitespace boundary; the marker carries the command that prints
    the rest, so agents never have to derive the follow-up call."""
    if limit < 0:
        raise ValueError("text cap cannot be negative")
    if limit == 0 or len(text) <= limit:
        return text, 0
    cut = text[:limit]
    cut = cut[: cut.rfind(" ")] if " " in cut[limit - 200:] else cut
    omitted = len(text) - len(cut)
    return f"{cut} [+{omitted:,} chars - {expand_cmd}]", omitted


def _ts_label(ts: int) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M")


# renderer-cap and ingest-loss markers inside message bodies; dimmed so the
# conversation reads over the plumbing (search's continuation-marker convention)
_MARKER_RE = re.compile(
    r"\[(?:\+[\d,]+ chars - [^\]]*|source[^\]]*at ingest[^\]]*)\]")


def _dim_markers(text: str, color: bool) -> str:
    if not color:
        return text
    return _MARKER_RE.sub(lambda m: f"{_C['d']}{m.group(0)}{_C['r']}", text)


def _reply_loss_marker(turn: dict) -> str:
    if not turn.get("reply_truncated"):
        return ""
    chars = int(turn.get("reply_chars", len(turn.get("reply", ""))))
    return f" [source truncated at ingest; original reply {chars:,} chars]"


def _tool_line(
        e: dict, color: bool, out_cap: int, *,
        match_span: tuple[int, int] | None = None,
        match_preview: str | None = None,
) -> str:
    inp = common.terminal_safe(" ".join((e.get("input") or "").split()))
    if len(inp) > 120:
        inp = inp[:120] + "…"
    if e.get("input_truncated"):
        chars = int(e.get("input_chars", len(e.get("input") or "")))
        inp += f" [source input truncated at ingest; {chars:,} chars]"
    centered = common.terminal_safe(match_preview or "")
    if e["kind"] == "subagent_start":
        body = f"{surface.GLYPHS.subagent_start} subagent {centered or inp}"
    elif e["kind"] == "subagent_result":
        out = common.terminal_safe(
            " ".join((e.get("output") or e.get("name") or "").split()))
        result = out[:120] + ("…" if len(out) > 120 else "")
        if e.get("output_truncated"):
            chars = int(e.get("output_chars", len(e.get("output") or "")))
            result += f" [source result truncated at ingest; {chars:,} chars]"
        body = (f"{surface.GLYPHS.subagent_result} subagent result "
                f"{centered or result}")
    else:
        # ok is tri-state: only an exact False is a failure; None stays neutral.
        mark = (f"{surface.GLYPHS.failure} FAILED" if e.get("ok") is False
                else surface.GLYPHS.tool)
        preview = display_policy.tool_output_preview(e, match_span=match_span)
        out = (f" {common.terminal_safe(preview.text)}"
               if out_cap <= 0 and preview.text else "")
        if preview.source_bytes is not None:
            size = f" ({preview.source_bytes:,}B)"
        elif e.get("output_truncated"):
            size = f" ({e['output_chars']:,}c source; indexed excerpt)"
        else:
            size = f" ({e['output_chars']:,}c)" if e.get("output_chars") else ""
        if centered and match_span is None:
            name = common.terminal_safe(e["name"])
            evidence = centered if centered.startswith(name) else f"{name} {centered}"
            body = f"{mark} {evidence}{size}"
        else:
            body = f"{mark} {common.terminal_safe(e['name'])} {inp}{out}{size}"
    line = f"  {body}"
    if color:
        c = _C["bad"] if e.get("ok") is False else _C["d"]
        line = f"  {c}{body}{_C['r']}"
    if out_cap > 0 and e.get("output"):
        raw_out = e["output"][:out_cap]
        out = common.terminal_safe(raw_out, multiline=True)
        more = len(e["output"]) - len(raw_out)
        tail = f" [+{more:,} indexed chars]" if more > 0 else ""
        if e.get("output_truncated"):
            tail += " [source truncated at ingest]"
        line += "\n" + "\n".join(f"    {ln}" for ln in out.splitlines()) + tail
    return line


_TOOL_RUN_MIN = 5
_TOOL_DETAIL_MAX = 16


class _SelectedToolMatch(NamedTuple):
    selected: bool
    output_span: tuple[int, int] | None = None
    preview: str | None = None


def _selected_tool_match(
        event: dict, session: str, selected_event_identity: str | None,
        selected_match_span: tuple[int, int] | None,
) -> _SelectedToolMatch:
    """Identify one strong-bound event and retain its matched evidence."""
    if selected_event_identity is None:
        return _SelectedToolMatch(False)
    text, bounds = common.tool_search_record(event)
    identity = common.tool_event_identity(
        session, event.get("turn"), event.get("ts"), text)
    if identity != selected_event_identity:
        return _SelectedToolMatch(False)
    if selected_match_span is None:
        return _SelectedToolMatch(True)
    start, end = selected_match_span
    if not 0 <= start < end <= len(text):
        return _SelectedToolMatch(True)
    try:
        preview = display_policy.payload_snip_at(
            text, start, end, pad=60, payload_bounds=bounds)
    except ValueError:
        preview = display_policy.payload_snip_at(text, start, end, pad=60)
    if bounds is None or not bounds[0] <= start < end <= bounds[1]:
        return _SelectedToolMatch(True, preview=preview)
    payload = text[bounds[0]:bounds[1]]
    raw_output = event.get("output")
    raw_at = raw_output.find(payload) if type(raw_output) is str else -1
    if raw_at < 0:
        return _SelectedToolMatch(True, preview=preview)
    output_span = raw_at + start - bounds[0], raw_at + end - bounds[0]
    return _SelectedToolMatch(True, output_span, preview)


def _selected_tool_output_span(
        event: dict, session: str, selected_event_identity: str | None,
        selected_match_span: tuple[int, int] | None,
) -> tuple[bool, tuple[int, int] | None]:
    match = _selected_tool_match(
        event, session, selected_event_identity, selected_match_span)
    return match.selected, match.output_span


def _tool_detail_key(event: dict) -> tuple:
    """Group only events whose complete indexed evidence is identical."""
    fields = (
        "kind", "name", "input", "output", "ok", "input_chars",
        "output_chars", "output_bytes", "input_truncated", "output_truncated",
    )
    return tuple(event.get(field) for field in fields)


def _spread_tool_groups(groups: list[dict], limit: int) -> list[dict]:
    """Sample a long distinct tail across its full chronological span."""
    if limit <= 0 or not groups:
        return []
    if len(groups) <= limit:
        return groups
    if limit == 1:
        return [groups[0]]
    indexes = {
        round(offset * (len(groups) - 1) / (limit - 1))
        for offset in range(limit)
    }
    return [groups[index] for index in sorted(indexes)]


def _tool_run_summary(events: list[dict], color: bool, expand: str) -> str:
    by_name: dict[str, int] = {}
    for event in events:
        by_name[event["name"]] = by_name.get(event["name"], 0) + 1
    ranked = sorted(by_name.items(), key=lambda item: -item[1])
    top = " · ".join(
        f"{count} {common.terminal_safe(name)}"
        for name, count in ranked[:3])
    rest = len(events) - sum(count for _, count in ranked[:3])
    tail = f" · {rest} other" if rest > 0 else ""
    body = (f"{surface.GLYPHS.tool} {len(events)} tool calls ({top}{tail})"
            f" - {expand} shows each")
    return f"  {_C['d']}{body}{_C['r']}" if color else f"  {body}"


def _tool_block(events: list[dict], color: bool, tool_output: int,
                expand: str, *, collapse: bool, session: str = "",
                selected_event_identity: str | None = None,
                selected_match_span: tuple[int, int] | None = None) -> list[str]:
    """Keep decisive tool evidence visible while bounding pathological tails."""
    selected: dict[int, _SelectedToolMatch] = {}
    for event in events:
        match = _selected_tool_match(
            event, session, selected_event_identity, selected_match_span)
        if match.selected:
            selected[id(event)] = match
            break
    plain = [e for e in events
             if e["kind"] not in ("subagent_start", "subagent_result")
             and e.get("ok") is True and id(e) not in selected]
    if not collapse or tool_output > 0:
        return [_tool_line(
            e, color, tool_output,
            match_span=(selected[id(e)].output_span if id(e) in selected else None),
            match_preview=(selected[id(e)].preview if id(e) in selected else None))
                for e in events]
    if len(plain) < _TOOL_RUN_MIN and len(events) <= _TOOL_DETAIL_MAX:
        return [_tool_line(
            e, color, tool_output,
            match_span=(selected[id(e)].output_span if id(e) in selected else None),
            match_preview=(selected[id(e)].preview if id(e) in selected else None))
                for e in events]

    plain_ids = {id(event) for event in plain}
    details = [event for event in events if id(event) not in plain_ids]
    if (len(plain) >= _TOOL_RUN_MIN
            and len(details) + 1 <= _TOOL_DETAIL_MAX):
        lines = [_tool_line(
            event, color, tool_output,
            match_span=(selected[id(event)].output_span
                        if id(event) in selected else None),
            match_preview=(selected[id(event)].preview
                           if id(event) in selected else None))
                 for event in details]
        lines.append(_tool_run_summary(plain, color, expand))
        return lines

    groups: list[dict] = []
    by_key: dict[tuple, dict] = {}
    for index, event in enumerate(details):
        if id(event) in selected:
            groups.append({"event": event, "count": 1, "index": index,
                           "selected": True})
            continue
        key = _tool_detail_key(event)
        group = by_key.get(key)
        if group is None:
            group = {"event": event, "count": 0, "index": index,
                     "selected": False}
            by_key[key] = group
            groups.append(group)
        group["count"] += 1

    required = [group for group in groups if group["selected"]]
    singleton_failures = [
        group for group in groups
        if group["event"].get("ok") is False and group["count"] == 1
        and not group["selected"]
    ]
    if len(singleton_failures) == 1:
        required.append(singleton_failures[0])
    required_ids = {id(group) for group in required}
    remaining = [group for group in groups if id(group) not in required_ids]
    failures = [group for group in remaining
                if group["event"].get("ok") is False]
    other = [group for group in remaining
             if group["event"].get("ok") is not False]
    slots = max(0, _TOOL_DETAIL_MAX - len(required))
    chosen = list(required)
    picked_failures = _spread_tool_groups(failures, slots)
    chosen.extend(picked_failures)
    slots -= len(picked_failures)
    chosen.extend(_spread_tool_groups(other, slots))
    chosen.sort(key=lambda group: group["index"])

    lines = [_tool_line(
        group["event"], color, tool_output,
        match_span=(selected[id(group["event"])].output_span
                    if id(group["event"]) in selected else None),
        match_preview=(selected[id(group["event"])].preview
                       if id(group["event"]) in selected else None))
             for group in chosen]
    for line_index, group in enumerate(chosen):
        if group["count"] > 1:
            lines[line_index] += f" [\u00d7{group['count']:,} identical]"

    collapsed = len(events) - len(chosen)
    collapsed_failed = sum(
        group["count"] - (1 if group in chosen else 0)
        for group in groups if group["event"].get("ok") is False)
    if (len(plain) >= _TOOL_RUN_MIN
            and collapsed == len(plain) and not collapsed_failed):
        lines.append(_tool_run_summary(plain, color, expand))
    else:
        failed = f" ({collapsed_failed:,} failed)" if collapsed_failed else ""
        body = (f"{surface.GLYPHS.tool} {collapsed:,} tool event details "
                f"collapsed{failed} - {expand} shows each")
        lines.append(f"  {_C['d']}{body}{_C['r']}" if color else f"  {body}")
    return lines


def _who_selected(requested: str | None, actual: str) -> bool:
    """Match a corpus role while retaining `you` as the legacy alias for `user`."""
    return surface.around_speaker_matches(requested, actual)


def _around_scope(
        window: dict, *, policy: str, tool_mode: str, context: int,
        selection_order: str, session_role: str, selected_record_role: str,
        expansion_argv: list[str], message_cap_chars: int | None,
        message_cap_state: str,
        render_truncated_rows: int,
        prose_available: dict[str, int],
        prose_shown: dict[str, int], events_shown: int,
) -> dict:
    """Stable, answer-free disclosure for the rows the renderer did and did not show."""
    event_total = len(window.get("events") or ())
    return {
        "policy": policy,
        "session": str(window.get("session") or ""),
        "selected_session_role": session_role,
        "selected_record_role": selected_record_role,
        "center_turn": int(window.get("center", 0)),
        "radius": int(context),
        "bounds": {
            "first_indexed_turn": int(window.get("first_turn", 0)),
            "last_indexed_turn": int(window.get("last_turn", 0)),
        },
        "selection_order": selection_order,
        "render_order": "chronological",
        "family_widened": False,
        "project_or_folder_ranked": False,
        "counts_state": "exact",
        "tools": {
            "mode": tool_mode,
            "available": event_total,
            "shown": int(events_shown),
            "hidden": max(0, event_total - int(events_shown)),
        },
        "prose": {
            "available_by_role": prose_available,
            "shown_by_role": prose_shown,
            "hidden": max(
                0, sum(prose_available.values()) - sum(prose_shown.values())),
        },
        "adoption": ("unresolved" if session_role == "delegated" else
                     "root_main" if session_role == "root_main" else "unknown"),
        "truncation": {
            "message_cap_chars": message_cap_chars,
            "message_cap_state": message_cap_state,
            "render_truncated_rows": int(render_truncated_rows),
            "source_truncated_agent_rows": sum(
                1 for turn in window.get("turns") or ()
                if turn.get("reply_truncated")),
            "source_truncated_events": sum(
                1 for event in window.get("events") or ()
                if event.get("input_truncated") or event.get("output_truncated")),
        },
        "expansion_argv": expansion_argv,
    }


def _disambiguation_hint(cands: list[str], shown: int) -> str:
    """The shortest prefix growth that singles out each listed ambiguous candidate."""
    import compact
    ordered = compact.session_prefix_index(cands)
    hints = [session[:compact._needed_prefix(session, ordered)]
             for session in cands[:shown]]
    return " / ".join(hints)


def _note_freshness(json_output: bool, *, checked: bool = True) -> None:
    if json_output:
        # rc 0 with no field reads as "fresh" (OUTPUT_CONTRACTS law 5), so
        # every degraded state surfaces here, index-behind included
        freshness = (indexd_runtime.machine_freshness()
                     if checked else _unchecked_freshness())
        if freshness.get("failing") or freshness.get("may_be_stale"):
            _stdout_print(json.dumps(
                {"kind": "agrep-meta", "freshness": freshness},
                ensure_ascii=False))
        return
    if not checked:
        common.log(indexd_runtime.NO_AUTO_REFRESH_REASON)
        return
    notice = indexd_runtime.agent_freshness_notice()
    if notice:
        common.log(notice)


def _main(argv: list[str] | None = None) -> int:
    common.utf8_stdio()

    ap = surface.ArgumentParser(
        prog="agrep around", description="show the conversation around one turn of a chat",
        allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  agrep around 11111111 144            ±4 turns, root/main prose\n"
               "  agrep around 11111111:144 --context 10  wider window (colon form ok)\n"
               "  agrep around 11111111 144 -C 0 --full  same-window forensic stream\n"
               "  agrep around 11111111 144 --tool-output 800  include tool results\n"
               "  agrep around @11111111              latest indexed turn in that chat\n"
               "  agrep around @11111111:144          compact result handle: that turn only\n"
               "  agrep around 11111111 144 --json     one object per message/event\n"
               "\nsession ids and turns come from `agrep <pattern> --json`.\n"
               "exit: 0 shown, 1 no selected messages/events, "
               "2 bad target / no index.")
    ap.add_argument("session", help="session id, bare @session for its latest turn, "
                                    "session:turn, or a compact @session:turn handle")
    ap.add_argument("turn", nargs="?", help="turn number to center on")
    ap.add_argument("-C", "--context", "--radius",
                    dest="context", type=int, default=None, metavar="N",
                    help="turns before and after the center turn (default 4; "
                         "a precise @session:turn handle defaults to 0 - "
                         "just the named turn)")
    ap.add_argument("--max-chars", type=int, default=4000, metavar="M",
                    help="per-message text cap (default 4000; exact delegated "
                         "prose may use the ingest cap; 0 = uncapped)")
    ap.add_argument("--full", action="store_true",
                    help="show the same-window forensic stream and uncap indexed "
                         "message text (ingest and tool-result caps still "
                         "apply); can be enormous - for deliberate forensics "
                         "rather than routine reading")
    ap.add_argument("--no-tools", action="store_true", help="hide tool-call lines")
    ap.add_argument("--tool-output", type=int, default=0, metavar="N",
                    help="include raw multiline tool results, capped at N chars "
                         "(default: concise one-line preview)")
    ap.add_argument("--who", choices=surface.AROUND_SPEAKER_CHOICES,
                    help="only this speaker (`you` is an alias for `user`)")
    ap.add_argument("--json", action="store_true",
                    help="one JSON object per message/event (for piping)")
    ap.add_argument("--no-auto", action="store_true",
                    help=surface.NO_AUTO_HELP)
    ap.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    args = surface.parse_args_with_presence(ap, argv)
    # an option a surface renders inert is refused, never dropped: --full
    # swallowed an explicit --max-chars and --no-tools an explicit --tool-output
    gated = surface.option_gate_error(args, surface.AROUND_OPTION_GATES)
    if gated:
        ap.error(gated)

    is_handle = compact.is_result_handle(args.session)
    latest_session = (str(args.session).strip().startswith("@")
                      and not is_handle and args.turn is None
                      and ":" not in str(args.session))
    if args.context is not None and args.context < 0:
        ap.error("--context must be 0 or greater")
    if args.context is None:
        # a handle addresses one exact turn; radius is for exploratory reads
        args.context = 0 if is_handle else 4
    if args.max_chars < 0:
        ap.error("--max-chars must be 0 (uncapped) or greater")
    if args.tool_output < 0:
        ap.error("--tool-output must be 0 (preview only) or greater")

    sess_q, center = _parse_target(args.session, args.turn, args.json)
    requested_center = center
    if is_handle:
        (_prefix, _turn, handle_digest, handle_event_identity,
         handle_match_span) = compact.parse_result_handle_claim(args.session)
    else:
        handle_digest = handle_event_identity = handle_match_span = None
    if not common.MESSAGES_PATH.exists():
        if not indexd_runtime.ensure_index(
                auto=not args.no_auto,
                quiet=bool(args.json or not sys.stdout.isatty())):
            if args.json:
                # ensure_index already spoke per its quiet policy; the pipe
                # still needs the machine record (search's index-missing shape)
                _stdout_print(json.dumps(
                    {"kind": "agrep-meta",
                     "error": {"code": "index-missing",
                               "remedy": "agrep index"}},
                    ensure_ascii=False, separators=(",", ":")))
            _note_freshness(args.json, checked=not args.no_auto)
            return 2

    notes: list[dict] = []
    if is_handle:
        # one resolution policy for @handles everywhere: recall already routes
        # through compact, so around must accept/reject the same inputs identically.
        try:
            cands, _ = compact.resolve_result_handle_candidates(
                args.session, explore.resolve_session)
        except compact.CompactError:
            return _fail(args.json, "stale-handle", _stale_handle_reason())
        if len(cands) > 1 and handle_digest:
            picked = _digest_candidate(
                cands, center, handle_digest, handle_event_identity)
            if picked:
                _serve(notes, args.json, "handle_disambiguated",
                       candidates=len(cands), session=picked)
                cands = [picked]
    else:
        cands = explore.resolve_session(sess_q)
    if not cands:
        return _fail(
            args.json, "no-session",
            f"no session matches {common.terminal_safe(sess_q)!r} - "
            "ids come from `agrep <pattern> --json`.")
    if len(cands) > 1:
        rc = _fail(
            args.json, "ambiguous-session",
            f"{common.terminal_safe(sess_q)!r} is ambiguous ({len(cands)} sessions):",
            candidates=[common.terminal_safe(s) for s in cands[:10]])
        if not args.json:
            for s in cands[:10]:
                common.log(f"  {common.terminal_safe(s)}")
            common.log(
                f"add a char: {common.terminal_safe(_disambiguation_hint(cands, 10))}")
        return rc
    common.lap("resolve")

    w = explore.get_window(cands[0], center, args.context)
    common.lap("window")
    if "error" in w:
        return _fail(args.json, "window-unavailable",
                     common.terminal_safe(w["error"]))
    if latest_session:
        center = int(w["center"])
    elif w["center"] != center:
        if is_handle:
            return _fail(
                args.json, "stale-handle",
                _stale_handle_reason(
                    f"turn {center} is out of range "
                    f"(chat has turns {w['first_turn']}-{w['last_turn']})"),
                requested_turn=center,
                first_turn=w["first_turn"], last_turn=w["last_turn"])
        _serve(notes, args.json, "turn_clamped", requested=center,
               served=w["center"], first_turn=w["first_turn"],
               last_turn=w["last_turn"])

    if is_handle and handle_digest is None:
        _serve(notes, args.json, "handle_unverified")

    if handle_digest:
        rescue = _resolve_handle_claims(
            w, cands[0], center, handle_digest, handle_event_identity)
        if rescue.outcome == "ambiguous":
            # several turns hold the cited content: recoverable, disclosed,
            # and refused - never rendered as loss, never guessed between
            rc = _fail(
                args.json, "content-ambiguous",
                surface.handle_content_ambiguous(
                    compact.encode_session_target(w["session"]), center,
                    rescue.candidates, common.cli_name()),
                candidates=list(rescue.candidates))
            return rc
        if rescue.outcome == "absent":
            rc = _fail(
                args.json, "content-missing",
                "result handle no longer names the cited content; "
                "rerun the search for a current handle",
                session=w["session"], turn=center)
            return rc
        found = rescue.turn
        if handle_event_identity is None:
            handle_event_identity = rescue.event_identity
        if found != center:
            _serve(notes, args.json, "content_moved", requested=center,
                   served=found)
            w = explore.get_window(cands[0], found, args.context)
            if "error" in w:
                return _fail(args.json, "window-unavailable",
                             common.terminal_safe(w["error"]))

    if handle_event_identity is not None and args.no_tools:
        return _fail(
            args.json, "selected-tool-hidden",
            "--no-tools conflicts with this tool result handle because it "
            "would erase the cited evidence; open the handle without "
            "--no-tools or use a different prose handle",
        )


    raw_session_rows = explore._session_index()
    if hasattr(raw_session_rows, "items"):
        session_rows = dict(raw_session_rows)
    else:
        # Older callers/tests consumed `_session_index()` as an iterable of ids.
        # Metadata is optional here; keep that public seam while preferring the
        # richer mapping when the current index supplies one.
        session_rows = {str(session): {} for session in raw_session_rows}
    session_meta = dict(session_rows.get(str(w["session"])) or {})
    session_meta.setdefault("session", str(w["session"]))
    family_roots = session_context.indexed_family_roots((str(w["session"]),))
    role_proven = bool(
        family_roots is not None and str(w["session"]) in family_roots)
    if role_proven:
        side_session = family_roots[str(w["session"])] != str(w["session"])
    elif str(w["session"]) in session_rows:
        # A published parent/sub marker is stronger than legacy `agent-*`
        # naming. Some old root fixtures and providers also used that prefix.
        side_session = bool(
            session_meta.get("parent") or session_meta.get("sub")
            or any(
                str(session_meta.get(field) or "").lstrip().lower().startswith(
                    ("[subagent task]", "[subagent message]"))
                for field in ("title", "last_text", "first_text")))
    else:
        side_session = session_context.is_side_session(session_meta)
    session_role = (
        "delegated" if role_proven and side_session else
        "root_main" if role_proven else "unknown")

    cap = 0 if args.full else args.max_chars
    selected_delegated_prose_uncapped = bool(
        session_role == "delegated"
        and is_handle
        and handle_digest is not None
        and handle_event_identity is None
        and not args.full
        and "max_chars" not in args._agrep_supplied_options
    )
    if selected_delegated_prose_uncapped:
        # A short prose digest proves the turn, not an answer span; a capsule can
        # delete the child's only answer. Keep its prose, hide event noise, and
        # retain the 64k ingest safety cap plus source-loss marker.
        cap = 0

    session_index = compact.session_prefix_index(session_rows)
    target = compact.encode_session_target(w["session"], session_index=session_index)

    if args.no_tools:
        tool_mode = "excluded"
    elif args.full or args.who == "tool" or args.tool_output > 0:
        tool_mode = "all"
    elif (is_handle and (handle_digest is None
                         or (handle_event_identity is None and not role_proven))):
        # A legacy/unverified handle may predate tool identity. Keep its exact
        # turn inclusive rather than delete a possible cited event; positional
        # and latest-session reads have no selected event and stay root-only.
        tool_mode = "compact"
    elif handle_event_identity is not None:
        tool_mode = "selected"
    else:
        tool_mode = "excluded"

    events_by_turn: dict[int, list[dict]] = {}
    if tool_mode != "excluded" and args.who in (None, "tool"):
        for event in w["events"]:
            if tool_mode == "selected":
                match = _selected_tool_match(
                    event, w["session"], handle_event_identity,
                    handle_match_span)
                if not match.selected:
                    continue
            events_by_turn.setdefault(event["turn"], []).append(event)

    def prose_selected(turn: dict, who: str, text: str) -> bool:
        if not text or not _who_selected(args.who, who):
            return False
        if args.full or args.who is not None:
            return True
        if is_handle and handle_digest is None:
            # Legacy handles have no role/content claim. Preserve their exact
            # radius-zero turn instead of guessing which field they named.
            return True
        if (is_handle and handle_digest is not None
                and handle_event_identity is None and not role_proven):
            # The digest verifies content, not root/child authority. Without
            # a generation-bound family role, keep the exact turn inclusive.
            return True
        if session_role == "delegated":
            return who in ("user", "agent", "subagent")
        return who in ("user", "agent")

    def cap_prose(turn: dict, who: str, text: str, expand: str) -> tuple[str, int]:
        return _cap(text, cap, expand)

    def turn_selected(turn: dict) -> bool:
        return bool(
            prose_selected(
                turn, str(turn.get("who") or ""), str(turn.get("text") or ""))
            or prose_selected(turn, "agent", str(turn.get("reply") or ""))
            or (events_by_turn.get(turn.get("turn"))
                and _who_selected(args.who, "tool")))

    turns = [turn for turn in w["turns"] if turn_selected(turn)]
    if not turns:
        if w["turns"]:
            first_turn = int(w["turns"][0]["turn"])
            last_turn = int(w["turns"][-1]["turn"])
        else:
            first_turn = last_turn = int(w["center"])
        role = common.terminal_safe(args.who) if args.who else "default-scope"
        reason = (
            f"no {role} messages or events in turns {first_turn}-{last_turn} "
            f"of {target}")
        if args.who is None:
            reason += "; use --full for the same-window forensic stream"
        return _miss(
            args.json, "no-speaker-match", reason,
            checked=not args.no_auto,
            scope={"session": w["session"], "who": args.who,
                   "first_turn": first_turn, "last_turn": last_turn})

    prose_available: dict[str, int] = {}
    prose_shown: dict[str, int] = {}
    render_truncated_rows = 0
    for turn in w["turns"]:
        for who, text in (
                (str(turn.get("who") or ""), str(turn.get("text") or "")),
                ("agent", str(turn.get("reply") or ""))):
            if not text:
                continue
            prose_available[who] = prose_available.get(who, 0) + 1
            if prose_selected(turn, who, text):
                prose_shown[who] = prose_shown.get(who, 0) + 1
                if cap and len(text) > cap:
                    render_truncated_rows += 1
    events_shown = sum(len(events) for events in events_by_turn.values())
    selected_record_role = (
        "none" if not is_handle else
        "tool" if handle_event_identity is not None else
        "legacy_unbound" if handle_digest is None else
        "prose_turn_inclusive")
    expansion_argv = [
        "agrep", "around", target, str(int(w["center"])),
        "-C", str(args.context), "--full",
    ]
    scope = _around_scope(
        w,
        policy=(
            "forensic" if args.full else
            "root_prose" if not is_handle else
            "role_aware" if handle_event_identity is not None else
            "bounded_inclusive"),
        tool_mode=tool_mode,
        context=args.context,
        selection_order=("newest_tail" if latest_session else
                         "requested_center"),
        session_role=session_role,
        selected_record_role=selected_record_role,
        expansion_argv=expansion_argv,
        message_cap_chars=None if cap == 0 else int(cap),
        message_cap_state=(
            "forensic_uncapped" if args.full else
            "selected_delegated_exact_turn_uncapped"
            if selected_delegated_prose_uncapped else
            "explicit_uncapped" if cap == 0 else "bounded"),
        render_truncated_rows=render_truncated_rows,
        prose_available=prose_available,
        prose_shown=prose_shown,
        events_shown=events_shown,
    )

    if args.json:
        served = surface.around_service_disclosure(notes)
        # Ahead of every row: a consumer must know what scope it is reading
        # before it reads it. Divergence disclosure shares this one leading
        # record so the previous "served comes first" contract still holds.
        meta = {"kind": "agrep-meta", "scope": scope}
        if served is not None:
            meta["served"] = served
        _stdout_print(json.dumps(meta, ensure_ascii=False))
        for t in turns:
            for who, text in ((t["who"], t["text"]), ("agent", t["reply"])):
                if not prose_selected(t, who, text):
                    continue
                expand = _expand_command(target, t["turn"])
                capped, omitted = cap_prose(t, who, text, expand)
                row = {"kind": "msg", "session": w["session"],
                       "project": w["project"], "turn": t["turn"],
                       "who": who, "ts": t["ts"], "text": capped,
                       "omitted_chars": omitted}
                if who == "agent":
                    row["reply_chars"] = int(t.get("reply_chars", len(text)))
                    row["reply_truncated"] = bool(t.get("reply_truncated"))
                _stdout_print(json.dumps(row, ensure_ascii=False))
            selected_events = (events_by_turn.get(t["turn"], [])
                               if _who_selected(args.who, "tool") else [])
            for e in selected_events:
                match = _selected_tool_match(
                    e, w["session"], handle_event_identity, handle_match_span)
                o = {"kind": e["kind"], "session": w["session"], "project": w["project"],
                     "turn": e["turn"], "ts": e["ts"], "name": e["name"],
                     "input": e["input"], "ok": e["ok"],
                     "input_chars": e.get("input_chars", len(e["input"])),
                     "output_chars": e["output_chars"],
                     # source UTF-8 bytes ride through as recorded; null stays
                     # an honest unknown, never recomputed from the excerpt
                     "output_bytes": e.get("output_bytes"),
                     "input_truncated": bool(e.get("input_truncated")),
                     "output_truncated": bool(e.get("output_truncated"))}
                if e.get("payload_bounds") is not None:
                    o["payload_bounds"] = e["payload_bounds"]
                if match.selected and match.preview:
                    o["match_preview"] = match.preview
                if args.tool_output > 0:
                    o["output"] = e["output"][: args.tool_output]
                _stdout_print(json.dumps(o, ensure_ascii=False))
        common.lap("render")
        _note_freshness(args.json, checked=not args.no_auto)
        return 0

    color = _color_on(args.color)
    head = " · ".join(common.terminal_safe(x)
                      for x in (target, w["agent"], w["project"], w["concept"], w["title"])
                      if x)
    span = (f"turns {w['turns'][0]['turn']}-{w['turns'][-1]['turn']}"
            f" of {w['first_turn']}-{w['last_turn']}")
    _stdout_print(
        f"{_C['hd']}{head}{_C['r']}  {_C['d']}{span}{_C['r']}" if color
        else f"{head}  {span}")

    hidden_tools = int(scope["tools"]["hidden"])
    hidden_prose = int(scope["prose"]["hidden"])
    if hidden_tools or hidden_prose or session_role != "root_main" or latest_session:
        if scope["policy"] == "root_prose" and session_role == "root_main":
            scope_label = "root/main prose default"
        elif scope["policy"] == "root_prose" and session_role == "unknown":
            scope_label = "root/main prose default; session lineage unverified"
        elif session_role == "delegated":
            scope_label = (
                "selected delegated session; secondary/provisional, root adoption "
                "unresolved")
        elif session_role == "unknown":
            scope_label = "selected session role unverified; bounded inclusive scope"
        else:
            scope_label = "role-aware root/main scope"
        parts = [scope_label]
        if latest_session:
            parts.append("newest tail selected; chronological render")
        if selected_delegated_prose_uncapped:
            parts.append(
                "selected turn prose uncapped; ingest safety cap still applies")
        if hidden_tools:
            parts.append(f"{hidden_tools:,} unselected tool/workflow events hidden")
        if hidden_prose:
            parts.append(f"{hidden_prose:,} non-selected prose rows hidden")
        expand_scope = console.shell_command(
            "agrep", "around", target, int(w["center"]), "-C", args.context,
            "--full", fallback="agrep around <session> <turn> -C <N> --full")
        if hidden_tools or hidden_prose:
            parts.append("same-window forensic view: " + expand_scope)
        line = "scope: " + " · ".join(parts)
        _stdout_print(f"{_C['d']}{line}{_C['r']}" if color else line)

    for t in turns:
        if color:  # turn numbers keep search's yellow inside the dim rule
            _stdout_print(
                f"{_C['d']}── turn {_C['r']}{_C['y']}{t['turn']}{_C['r']} "
                f"{_C['d']}{'─' * 40} {_ts_label(t['ts'])}{_C['r']}")
        else:
            _stdout_print(
                f"── turn {t['turn']} " + "─" * 40 + f" {_ts_label(t['ts'])}")
        expand = _expand_command(target, t["turn"])
        if prose_selected(t, t["who"], t["text"]):
            tag = common.terminal_safe(t["who"])
            source = " ".join(t["text"].split()) if cap else t["text"]
            body, _ = cap_prose(t, t["who"], source, expand)
            body = _dim_markers(common.terminal_safe(body, multiline=True), color)
            _stdout_print(
                f"{_C['y']}{tag}:{_C['r']} {body}" if color else f"{tag}: {body}")
        selected_events = (events_by_turn.get(t["turn"], [])
                           if _who_selected(args.who, "tool") else [])
        for line in _tool_block(selected_events, color,
                                args.tool_output, expand,
                                collapse=not args.full, session=w["session"],
                                selected_event_identity=handle_event_identity,
                                selected_match_span=handle_match_span):
            _stdout_print(line)
        if prose_selected(t, "agent", t["reply"]):
            body, _ = cap_prose(t, "agent", t["reply"], expand)
            body = common.terminal_safe(body, multiline=True)
            body += _reply_loss_marker(t)
            body = _dim_markers(body, color)
            _stdout_print(
                f"{_C['a']}agent:{_C['r']} {body}" if color else f"agent: {body}")
    common.lap("render")
    if not args.no_auto:
        _note_freshness(args.json)
    return 0


def main(argv: list[str] | None = None) -> int:
    global _stdout_bytes
    _stdout_bytes = 0
    rc = _main(argv)
    if rc == 0:
        _warn_if_oversized(argv)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
