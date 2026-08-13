"""Deterministic recent-root context after a provider compaction.

This is continuation recovery, not semantic search. The visible provider summary
and current user turn remain primary; this command serves only the newest proven
pre-boundary root tail from the exact calling session family: verbatim excerpts
of up to MAX_BLOCKS newest root blocks, clipped per row, with dropped blocks and
omitted bytes reported and source truncation declared unavailable, not guessed.
"""

from __future__ import annotations

import argparse
import json
from collections import OrderedDict
from typing import Mapping

import common
import compact
import indexd_runtime
import session_context
import surface_policy as surface


SCHEMA_VERSION = 1
SCOPE = "root-only"
OUTPUT_BUDGET_BYTES = 8_000
JSON_OUTPUT_BUDGET_BYTES = 16_000
TEXT_BUDGET_BYTES = 5_000
MAX_BLOCKS = 8
MIN_ROW_BYTES = 320
MAX_ROW_BYTES = 2_400


def _stdout(value: object) -> None:
    print(value)


def _authority() -> dict:
    return {
        "role": "supplement",
        "primary": ["visible_user_turns", "platform_compact_summary"],
        "newer_visible_state_wins": True,
        "semantic_search_performed": False,
    }


def _failure(status: str, reason: str, *, json_output: bool) -> int:
    record = {
        "kind": "agrep-postcompact",
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "reason": reason,
        "authority": _authority(),
        "implicit_widening": False,
    }
    if json_output:
        _stdout(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    else:
        common.log(f"postcompact unavailable: {reason}")
    return 2


def _clip_utf8(text: str, limit: int) -> tuple[str, int]:
    """Keep a decision-friendly head and larger tail within a UTF-8 byte cap."""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text, 0
    budget = max(64, int(limit))
    marker_floor = 42
    payload = max(16, budget - marker_floor)
    head_size = max(1, int(payload * 0.38))
    tail_size = max(1, payload - head_size)
    while True:
        head = raw[:head_size].decode("utf-8", errors="ignore")
        tail = raw[-tail_size:].decode("utf-8", errors="ignore")
        kept = len(head.encode("utf-8")) + len(tail.encode("utf-8"))
        omitted = max(0, len(raw) - kept)
        marker = f"\n[… {omitted:,} UTF-8 bytes omitted …]\n"
        rendered = head + marker + tail
        extra = len(rendered.encode("utf-8")) - budget
        if extra <= 0:
            return rendered, omitted
        shrink = max(1, extra)
        tail_cut = min(tail_size - 1, (shrink * 3 + 4) // 5)
        head_cut = min(head_size - 1, shrink - tail_cut)
        if tail_cut <= 0 and head_cut <= 0:
            return rendered.encode("utf-8")[:budget].decode(
                "utf-8", errors="ignore"), omitted
        tail_size -= max(0, tail_cut)
        head_size -= max(0, head_cut)


def _row_handle(row: Mapping[str, object], text: str) -> str:
    session = str(row["session"])
    return compact.encode_bound_result_handle(
        row, prefix_chars=len(session), text=text)


def _previous_boundary(db, session: str, boundary: int) -> int | None:
    row = db.execute(
        "SELECT max(turn) FROM msgs WHERE session=? AND who='recap' AND turn<?",
        (session, boundary),
    ).fetchone()
    return None if not row or row[0] is None else int(row[0])


def _eligible_rows(db, session: str, lower: int | None, upper: int) -> list[dict]:
    where = ["session=?", "turn<?", "who IN ('user','agent')", "text<>''"]
    params: list[object] = [session, int(upper)]
    if lower is not None:
        where.append("turn>?")
        params.append(int(lower))
    rows = db.execute(
        "SELECT session, turn, coalesce(ts,0), who, text, "
        "coalesce(agent,''), coalesce(project,''), coalesce(model,''), "
        "coalesce(model_source,''), content_digest FROM msgs WHERE "
        + " AND ".join(where)
        + " ORDER BY turn, CASE WHEN who='agent' THEN 1 ELSE 0 END, ts, id",
        params,
    )
    fields = (
        "session", "turn", "ts", "who", "text", "agent", "project",
        "model", "model_source", "content_digest",
    )
    return [dict(zip(fields, row)) for row in rows]


def _select_rows(rows: list[dict]) -> tuple[list[dict], int, int]:
    blocks: OrderedDict[int, list[dict]] = OrderedDict()
    for row in rows:
        blocks.setdefault(int(row["turn"]), []).append(row)
    selected_blocks = list(blocks.items())[-MAX_BLOCKS:]
    selected = [row for _turn, block in selected_blocks for row in block]
    if not selected:
        return [], len(blocks), 0
    per_row = max(
        MIN_ROW_BYTES,
        min(MAX_ROW_BYTES, TEXT_BUDGET_BYTES // len(selected)),
    )
    rendered = []
    render_omitted = 0
    for raw in selected:
        original = str(raw.get("text") or "")
        text, omitted = _clip_utf8(original, per_row)
        render_omitted += omitted
        row = {key: raw[key] for key in ("session", "turn", "ts", "who")}
        row.update({
            "tier": "root",
            "kind": "message",
            "handle": _row_handle(raw, original),
            "text": text,
            "render_truncated": bool(omitted),
            "render_omitted_bytes": omitted,
            # The flattened corpus intentionally does not retain the source
            # adapter's reply-truncation bit.  Unknown must not masquerade as
            # a proved complete source row.
            "source_truncated": None,
            "source_truncation_state": "unavailable_in_materialized_index",
        })
        rendered.append(row)
    return rendered, max(0, len(blocks) - len(selected_blocks)), render_omitted


def read_packet(
        db, family: session_context.CallingFamily,
) -> dict:
    """Build one root-only packet from an already-open coherent snapshot."""
    boundary = family.recap_turn
    if boundary is None:
        raise ValueError("no structural compaction boundary is indexed for this caller")
    session = family.session
    # Back-to-back recaps (an archive resume immediately re-compacted) leave
    # the newest window empty; an empty packet helps nobody, so walk back to
    # the nearest window with content and disclose how far the walk went.
    previous = _previous_boundary(db, session, int(boundary))
    eligible = _eligible_rows(db, session, previous, int(boundary))
    window_fallbacks = 0
    while not eligible and previous is not None:
        upper = previous
        previous = _previous_boundary(db, session, upper)
        eligible = _eligible_rows(db, session, previous, upper)
        window_fallbacks += 1
    rows, omitted_blocks, render_omitted = _select_rows(eligible)
    eligible_blocks = len({int(row["turn"]) for row in eligible})
    shown_blocks = len({int(row["turn"]) for row in rows})
    newest = rows[-1]["handle"] if rows else None
    project = next((str(row["project"]) for row in reversed(eligible)
                    if row.get("project")), "")
    agent = next((str(row["agent"]) for row in reversed(eligible)
                  if row.get("agent")), "")
    model = next((str(row["model"]) for row in reversed(eligible)
                  if row.get("model")), "")
    model_source = next((str(row["model_source"]) for row in reversed(eligible)
                         if row.get("model_source")), "")
    packet = {
        "kind": "agrep-postcompact",
        "schema_version": SCHEMA_VERSION,
        "status": "recovered" if rows else "empty",
        "authority": _authority(),
        "selection": {
            "caller_session": family.session,
            "family_root": family.root,
            "family_resolved": family.resolved,
            "scope": SCOPE,
            "boundary_turn": int(boundary),
            "previous_boundary_turn": previous,
            "boundary_basis": "indexed_structural_recap",
            "window_fallbacks": window_fallbacks,
            "tools": "excluded",
            "delegated_sessions": "excluded",
            "project": project,
            "agent": agent,
            "model": model,
            "model_source": model_source,
        },
        "coverage": {
            "family": "complete" if family.resolved else "partial",
            "boundary": "complete",
            "eligible_root_blocks": eligible_blocks,
            "shown_root_blocks": shown_blocks,
            "budget_bytes": OUTPUT_BUDGET_BYTES,
            "json_budget_bytes": JSON_OUTPUT_BUDGET_BYTES,
            "budget_truncated": bool(omitted_blocks or render_omitted),
            "render_omitted_bytes": render_omitted,
            "source_truncated_rows": None,
            "source_truncation_state": "unavailable_in_materialized_index",
        },
        "rows": rows,
        "omissions": {
            "root_blocks": omitted_blocks,
            "tools": "policy_excluded",
            "delegated_sessions": "policy_excluded",
        },
        "next": {
            "around_argv": (
                ["agrep", "around", newest, "-C", "2"]
                if newest else None),
            "recall_argv_template": [
                "agrep", "recall", "<specific missing detail + clues>",
                "--hits", "2", "--budget", "5000",
            ],
        },
        "implicit_widening": False,
    }
    return _fit_packet(packet)


def _human(packet: dict, *, enforce_budget: bool = True) -> str:
    selection = packet["selection"]
    coverage = packet["coverage"]

    def safe(value: object) -> str:
        return common.terminal_safe(str(value))

    lines = [
        "postcompact: supplement to the visible compact summary; newer visible user text wins",
        " · ".join(part for part in (
            f"caller @{safe(selection['caller_session'])}",
            f"root @{safe(selection['family_root'])}",
            safe(selection.get("agent") or ""),
            (f"project {safe(selection['project'])} (orientation only)"
             if selection.get("project") else ""),
        ) if part),
        (f"boundary turn {selection['boundary_turn']} (structural recap) · "
         f"{selection['scope']} · tools and delegated sessions excluded"
         + (f" · newest {selection['window_fallbacks']} window(s) before this "
            "boundary were empty; showing the nearest earlier window"
            if selection.get("window_fallbacks") and packet["rows"] else "")),
        "",
        "── recent root/main tail · newest blocks selected, chronological render ──",
    ]
    for row in packet["rows"]:
        lines.append(
            f"{row['turn']} {row['who']} {row['handle']}: "
            + common.terminal_safe(row["text"], multiline=True))
    lines.extend([
        "",
        (f"coverage: {coverage['shown_root_blocks']}/"
         f"{coverage['eligible_root_blocks']} eligible root blocks shown; "
         f"{packet['omissions']['root_blocks']} older blocks omitted; "
         "tools and delegated sessions excluded"),
    ])
    around = packet["next"].get("around_argv")
    if around:
        lines.append("deepen exact nearby context: " + " ".join(around))
    lines.append(
        "only if one specific different-history detail remains: "
        "agrep recall \"<missing detail + clues>\" --hits 2 --budget 5000")
    rendered = "\n".join(lines)
    if enforce_budget and len(rendered.encode("utf-8")) > OUTPUT_BUDGET_BYTES:
        raise RuntimeError("postcompact renderer exceeded its byte contract")
    return rendered


def _packet_sizes(packet: dict) -> tuple[int, int]:
    human_bytes = len(_human(packet, enforce_budget=False).encode("utf-8"))
    json_bytes = len(json.dumps(
        packet, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return human_bytes, json_bytes


def _drop_oldest_root_block(packet: dict) -> bool:
    rows = packet["rows"]
    if not rows:
        return False
    turns = sorted({int(row["turn"]) for row in rows})
    if len(turns) <= 1:
        return False
    oldest = turns[0]
    dropped = [row for row in rows if int(row["turn"]) == oldest]
    packet["rows"] = [row for row in rows if int(row["turn"]) != oldest]
    packet["coverage"]["shown_root_blocks"] = len({
        int(row["turn"]) for row in packet["rows"]
    })
    packet["omissions"]["root_blocks"] = max(
        0,
        int(packet["coverage"]["eligible_root_blocks"])
        - int(packet["coverage"]["shown_root_blocks"]),
    )
    packet["coverage"]["render_omitted_bytes"] += sum(
        len(str(row["text"]).encode("utf-8"))
        for row in dropped
    )
    packet["coverage"]["budget_truncated"] = True
    return True


def _fit_packet(packet: dict) -> dict:
    """Keep the newest root evidence while honoring both public byte ceilings."""
    while True:
        human_bytes, json_bytes = _packet_sizes(packet)
        if (human_bytes <= OUTPUT_BUDGET_BYTES
                and json_bytes <= JSON_OUTPUT_BUDGET_BYTES):
            return packet
        if _drop_oldest_root_block(packet):
            continue
        raise RuntimeError(
            "postcompact metadata exceeds its bounded output contracts")


def main(argv: list[str] | None = None) -> int:
    common.utf8_stdio()
    parser = surface.ArgumentParser(
        prog="agrep postcompact",
        description="supplement a compact summary with the exact recent root tail",
        allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  agrep postcompact         recent pre-compact root context\n"
            "  agrep postcompact --json  one bounded structured packet\n"
            "\nexit: 0 recovered, 1 proven empty, 2 unavailable or invalid."
        ),
    )
    parser.add_argument("--json", action="store_true",
                        help="emit one structured packet")
    parser.add_argument("--no-auto", action="store_true",
                        help=surface.NO_AUTO_HELP)
    parser.add_argument(
        "--session", metavar="ID",
        help="recover a specific session's tail when the caller cannot be "
             "auto-identified (e.g. under opencode); requires the session to "
             "have an indexed structural compaction boundary")
    args = parser.parse_args(argv)

    if not indexd_runtime.ensure_index(
            auto=not args.no_auto, quiet=bool(args.json)):
        return _failure(
            "index_unavailable", "the materialized history index is unavailable",
            json_output=args.json)

    outcome = _serve(args, retry_pending=not args.no_auto)
    if outcome is not None:
        return outcome
    # The primary moment is seconds after a compaction is written, and the
    # published snapshot legitimately lags live files by a beat; one
    # synchronous ingest closes exactly that race, then the answer is final.
    if common.ingest_bin().exists():
        indexd_runtime.build_index(quiet=True)
    outcome = _serve(args, retry_pending=False)
    assert outcome is not None
    return outcome


def _serve(args, *, retry_pending: bool) -> int | None:
    """One resolution attempt. None = staleness-shaped miss worth one retry."""

    def stale(status: str, reason: str) -> int | None:
        if retry_pending:
            return None
        return _failure(status, reason, json_output=args.json)

    if args.session:
        db = session_context._open_session_family_index()
        if db is None:
            return stale(
                "index_unavailable",
                "the generation-bound caller family could not be resolved")
        try:
            indexed = session_context._indexed_calling_family_state_in_db(
                db, args.session)
            if indexed is None:
                return stale(
                    "boundary_unavailable",
                    "no structural compaction boundary is indexed for "
                    f"session {args.session}")
            root, members, recap_turn = indexed
            if recap_turn is None:
                # a known family with no indexed recap is the same
                # staleness-shaped miss as an unknown session: the boundary
                # was written seconds ago and one ingest closes the race
                return stale(
                    "boundary_unavailable",
                    "no structural compaction boundary is indexed for "
                    f"session {args.session}")
            family = session_context.CallingFamily(
                session=args.session, root=root,
                members=members | {args.session},
                resolved=True, recap_turn=recap_turn)
            try:
                packet = read_packet(db, family)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                return _failure(
                    "index_unavailable", str(exc), json_output=args.json)
        finally:
            db.close()
        if args.no_auto:
            packet["status"] = "partial"
            packet["coverage"]["index_freshness"] = "unchecked"
        else:
            notice = indexd_runtime.agent_freshness_notice()
            if notice:
                return _failure(
                    "index_unavailable", notice, json_output=args.json)
            packet["coverage"]["index_freshness"] = "fresh"
        if args.json:
            _stdout(json.dumps(
                packet, ensure_ascii=False, separators=(",", ":")))
        else:
            _stdout(_human(packet))
        # Same exit contract as the identity path (0 recovered, 1 proven
        # empty, 2 partial); a hardcoded 0 here once reported emptiness as success.
        if packet["status"] == "empty":
            return 1
        return 2 if packet["status"] == "partial" else 0

    with session_context.calling_family_snapshot() as (identity, family, db):
        if not identity.session:
            return _failure(
                "identity_unavailable",
                "the calling agent session could not be identified; "
                "run `agrep postcompact --session <id>` to name it "
                "explicitly",
                json_output=args.json)
        if db is None or family is None:
            return stale(
                "index_unavailable",
                "the generation-bound caller family could not be resolved")
        if family.recap_turn is None:
            return stale(
                "boundary_unavailable",
                "no structural compaction boundary is indexed for this caller")
        try:
            packet = read_packet(db, family)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return _failure(
                "index_unavailable", str(exc), json_output=args.json)

    if args.no_auto:
        packet["status"] = "partial"
        packet["coverage"]["index_freshness"] = "unchecked"
    else:
        notice = indexd_runtime.agent_freshness_notice()
        if notice:
            return _failure(
                "index_unavailable", notice, json_output=args.json)
        packet["coverage"]["index_freshness"] = "fresh"

    if args.json:
        _stdout(json.dumps(packet, ensure_ascii=False, separators=(",", ":")))
    else:
        _stdout(_human(packet))
    if packet["status"] == "empty":
        return 1
    return 2 if packet["status"] == "partial" else 0


if __name__ == "__main__":
    raise SystemExit(main())
