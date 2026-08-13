"""`agrep recall` / `agrep pack` - budgeted context packs: "have I hit this before?"

    agrep recall "index lock"                 # top 3 hits, each with its local window
    agrep recall "oom" --hits 5 --budget 4000 # more hits, tighter budget
    agrep recall "flaky test" --semantic      # force semantic-only search
    agrep pack "unicode" "encoding" "cp1252"  # several queries, deduped, one budget
    agrep recall "deadlock" --json            # {query, engine, hits: [{... window}]}

Search says WHICH sessions touched a thing; recall answers the question an agent
mid-session actually has - it runs the search, pulls the conversation around each
top hit (explore.get_window), and caps the whole thing at --budget bytes so the
caller gets context, not a transcript. pack is the same over several queries at
once: hits deduped by (session, turn), one shared budget. Renderer-cap markers
carry the exact `agrep around` command; ingest-cap loss is labeled separately.
The budget follows relevance, not content size: weak evidence (bag-of-words
scatter, sub-strong meaning) past the top hit renders as one summary line with
its handle, and the strongest surviving block draws the largest share.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sqlite3
import sys
import time

import common
import compact
import console
import corpusdb
import display_policy
import explore
import indexd_runtime
import search
import surface_policy as surface
from around import (
    _cap,
    _reply_loss_marker,
    _resolve_handle_claims,
    _selected_tool_match,
    _tool_line,
    _ts_label,
)


class _RecallQueryAbort(RuntimeError):
    pass

# The calibration harness pins these semantic bands. Sub-STRONG hits are labeled weak.
PROBE_MIN_SEM = search.SEMANTIC_MIN_COSINE
PROBE_STRONG_SEM = search._RECALL_STRONG_SEM
PROBE_MIN_SCATTER_EVIDENCE = search.SCATTER_MIN_EVIDENCE
MIN_FINITE_BUDGET = 64
MIN_JSON_BUDGET = 2048

_C = surface.PALETTE


def _command(*argv: object) -> str:
    return console.shell_command(
        *argv, fallback="agrep recall <query-with-control-characters>")


def _utf8_size(value: str) -> int:
    return len(value.encode("utf-8", errors="replace"))


def _json_tool_row_key(row: dict) -> tuple:
    return tuple(row.get(key) for key in (
        "kind", "session", "turn", "ts", "name"))


def _utf8_prefix(value: str, budget: int) -> str:
    """Longest valid UTF-8 prefix whose rendered bytes fit ``budget``."""
    if budget <= 0:
        return ""
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= budget:
        return value
    return encoded[:budget].decode("utf-8", errors="ignore")




def _write_payload(payload: str, budget: int) -> None:
    """Write one complete line while counting its newline in the byte budget."""
    content_budget = _content_budget(budget)
    if budget and _utf8_size(payload) > content_budget:
        payload = _utf8_prefix(payload, content_budget)
    rendered = payload + "\n"
    sys.stdout.write(rendered)


def _content_budget(budget: int) -> int:
    """Rendered bytes available after reserving the final newline."""
    return max(0, budget - 1) if budget else 0


# _cap's tail marker; stripped before re-shrinking so a second cut never slices
# through the agrep-around command the first one embedded.
_CAP_MARKER_RE = re.compile(
    r" \[\+[\d,]+ chars - agrep around .+ \d+ -C 0(?: --full)?\]\Z")


def _shrink_row(row: dict, session_index=None,
                anchors: list[re.Pattern] | tuple = ()) -> bool:
    """Halve one window row's text, ending in _cap's exact marker so the shrink
    still carries its `agrep around` follow-up (module contract). False when the
    marker would eat the savings - the caller then tries the next-largest row."""
    old = row["text"]
    body = _CAP_MARKER_RE.sub("", old)
    # an anchored slice leads with its own head marker + the match it exists
    # to show; shrinks must keep both, not slice into the marker
    head = re.match(r"\[\+[\d,]+ chars - [^\]]*\] ", body)
    keep = max(head.end() + 192 if head else 16, (len(body) - 1) // 2)
    sess, turn = row.get("session"), row.get("turn")
    if not sess or turn is None:  # no follow-up exists for an addressless row
        row["text"] = body[:keep] + "…"
        row["omitted_chars"] = int(row.get("omitted_chars") or 0) + len(body) - keep
        return len(row["text"]) < len(old)
    target = compact.encode_session_target(sess, session_index=session_index)
    expand = _command("agrep", "around", target, turn, "-C", 0)
    if anchors and head is None:
        # law 6: a halving that would drop the row's own query match slides
        # the kept slice to the match instead of keeping the blind head
        new_body, cut_now = _anchored_cap(body, keep, expand, list(anchors))
        omitted = int(row.get("omitted_chars") or 0) + cut_now
        new = new_body
    else:
        cut = body[:keep]
        if " " in cut[-200:]:
            cut = cut[: cut.rfind(" ")]
        omitted = int(row.get("omitted_chars") or 0) + len(body) - len(cut)
        new = f"{cut} [+{omitted:,} chars - {expand}]"
    # 8-char floor absorbs digit growth of the omitted_chars field, keeping every
    # accepted shrink a strict net reduction (the fit loop must terminate).
    if len(new) > len(old) - 8:
        return False
    row["text"] = new
    row["omitted_chars"] = omitted
    return True


def _fit_json_payload(
        obj: dict, budget: int, session_index=None,
        hit_anchors: list[list[re.Pattern]] | None = None,
        required_tool_rows: list[set[tuple]] | None = None,
) -> str:
    """Serialize a recall envelope under ``budget`` without erasing its state.

    JSON must remain valid. Shrink message bodies first, then low-value context rows
    and hits. Accepted JSON budgets leave room for the disclosure floor.
    ``hit_anchors`` aligns each hit's own query patterns to obj["hits"].
    """
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    if not budget or _utf8_size(raw) <= budget:
        return raw

    out = copy.deepcopy(obj)
    original_hits = len(out.get("hits") or [])
    out["truncated"] = True
    out["omitted_hits"] = 0
    query = out.get("query")
    if isinstance(query, str) and len(query) > 256:
        out["query"] = query[:240] + "…"
    elif isinstance(query, list):
        out["query"] = [str(q)[:120] + ("…" if len(str(q)) > 120 else "")
                        for q in query[:8]]

    def encode() -> str:
        return json.dumps(out, ensure_ascii=False, separators=(",", ":"))

    # Long message bodies are the normal overflow. Repeatedly halve the largest,
    # retaining a visible omitted count and exact pull command already in the row.
    # Query anchors keep each row's own match inside the kept slice (law 6).
    anchors: list[re.Pattern] = []
    weak_anchors: list[re.Pattern] = []
    raw_queries = query if isinstance(query, list) else [query]
    for value in raw_queries:
        if isinstance(value, str) and value and not value.startswith("@"):
            pats = _anchor_patterns(value)
            anchors.extend(pats[:1])  # exact phrases outrank term fallbacks
            weak_anchors.extend(pats[1:])
    anchors.extend(weak_anchors)
    # a hit's own query leads its anchor list: an incidental match for
    # another pack query must never displace the phrase this row exists to
    # show (hits are only ever popped from the tail, so indexes stay aligned)
    per_hit = [own + anchors for own in hit_anchors or []]

    def required(hit_index: int, hit: dict, row: dict) -> bool:
        if (row.get("kind") == "msg"
                and row.get("turn") == hit.get("turn")):
            return True
        keys = required_tool_rows or []
        return (hit_index < len(keys)
                and _json_tool_row_key(row) in keys[hit_index])

    while True:
        raw = encode()
        if _utf8_size(raw) <= budget:
            return raw
        text_rows = sorted(
            ((row, per_hit[i] if i < len(per_hit) else anchors)
             for i, hit in enumerate(out.get("hits") or [])
             for row in hit.get("window") or []
             if isinstance(row.get("text"), str)
             and len(row["text"]) > 48),
            key=lambda pair: -len(pair[0]["text"]))
        if not any(_shrink_row(row, session_index, row_anchors)
                   for row, row_anchors in text_rows):
            break

    # Optional narration goes before a hit's selected exchange or tool event.
    while True:
        raw = encode()
        if _utf8_size(raw) <= budget:
            return raw
        dropped = False
        hits = out.get("hits") or []
        for hit_index in range(len(hits) - 1, -1, -1):
            hit = hits[hit_index]
            window = hit.get("window") or []
            for row_index in range(len(window) - 1, -1, -1):
                if required(hit_index, hit, window[row_index]):
                    continue
                window.pop(row_index)
                hit["window_truncated"] = True
                dropped = True
                break
            if dropped:
                break
        if not dropped:
            break

    while out.get("hits"):
        raw = encode()
        if _utf8_size(raw) <= budget:
            return raw
        out["hits"].pop()
        out["omitted_hits"] = original_hits - len(out["hits"])

    raw = encode()
    if _utf8_size(raw) <= budget:
        return raw

    def fields(name: str, keys: tuple[str, ...]) -> dict | None:
        value = out.get(name)
        if not isinstance(value, dict):
            return None
        picked = {}
        for key in keys:
            if key not in value:
                continue
            item = value[key]
            picked[key] = _utf8_prefix(item, 96) if isinstance(item, str) else item
        return picked

    query = out.get("query")
    if isinstance(query, list):
        query = [_utf8_prefix(str(value), 48) for value in query[:4]]
    else:
        query = _utf8_prefix(str(query or ""), 96)
    minimal = {
        "query": query,
        "engine": _utf8_prefix(str(out.get("engine") or ""), 64),
        "hits": [],
        "error": fields("error", ("code", "reason")),
        "self_exclusion": fields(
            "self_exclusion",
            ("active", "reason", "scope", "session",
             "excluded_hits", "excluded_hits_known",
             "postfilter_excluded_hits", "from_turn")),
        "freshness": fields(
            "freshness",
            ("state", "failing", "checked", "may_be_stale", "code",
             "reason", "consecutive_failures")),
        "semantic_coverage": fields(
            "semantic_coverage",
            ("indexed", "total", "pending", "fraction", "complete", "order")),
        "truncated": True,
        "omitted_hits": original_hits,
    }
    tools = fields("tools_excluded", ("reason",))
    if tools is not None:
        minimal["tools_excluded"] = tools
    return json.dumps(minimal, ensure_ascii=False, separators=(",", ":"))


# every in-payload truncation marker recall/around emit: message caps, omitted
# tool calls, over-budget hit tails. The final cap must never slice one open.
_TRUNC_MARKER_RE = re.compile(
    r"\[(?:\+[\d,]+ (?:chars|tool calls|hit\(s\) over budget) - [^\]]*"
    r"|[\d,]+ chars)\]")

# the over-budget trailer recall builds; parsed back when it must be degraded
_TRAILER_RE = re.compile(r"\[\+(\d+) hit\(s\) over budget - (.*)\]\Z", re.S)

# the final-cap eviction marker _fit_text_payload appends; dimmed like the rest
_BUDGET_CAP_MARKER_RE = re.compile(r"\[output truncated to --budget[^\]]*\]")
_MSG_ROW_RE = re.compile(r"^( +)(\d+) (\S+): (.*)$")

# A weak line's head/snippet boundary: content can't forge \x1f (terminal_safe
# strips C0 controls), so a ' - ' in a project name or snippet never moves the
# split. Renders as " - " at the same width, keeping budget arithmetic exact.
_SNIP_SEP = "\x1f- "


def _dim_markers(text: str) -> str:
    def dim(m: re.Match) -> str:
        return f"{_C['d']}{m.group(0)}{_C['r']}"
    return _BUDGET_CAP_MARKER_RE.sub(dim, _TRUNC_MARKER_RE.sub(dim, text))


def _paint_line(line: str) -> str:
    """search's grouped-render vocabulary: cyan block headers, yellow turn
    numbers, speaker tags, dim tool rows and continuation markers."""
    if line.startswith("── "):
        head, sep, snip = line.partition(_SNIP_SEP)
        painted = f"{_C['hd']}{head}{_C['r']}"
        return painted + (f" - {_dim_markers(snip)}" if sep else "")
    m = _MSG_ROW_RE.match(line)
    if m:
        pad, turn, who, body = m.groups()
        code = "a" if who == "agent" else "y"
        return (f"{pad}{_C['y']}{turn}{_C['r']} {_C[code]}{who}:{_C['r']} "
                f"{_dim_markers(body)}")
    if line.startswith("     "):  # tool rows and their collapse markers
        code = "bad" if f"{surface.GLYPHS.failure} FAILED" in line else "d"
        return f"{_C[code]}{line}{_C['r']}"
    return _dim_markers(line)


def _paint_payload(payload: str) -> str:
    """Colorize the FITTED payload: the budget never counts these bytes, and
    the fit-loop marker regexes above only ever see plain text. A stray
    sentinel (a --budget cut landing inside one) degrades to a space."""
    painted = "\n".join(_paint_line(line) for line in payload.split("\n"))
    return painted.replace("\x1f", " ")


def _degrade_trailer(trailer: str, block_ends: list[tuple[int, str]] | None,
                     budget: int) -> str:
    """Disclosure-only page: the content blocks are evicted whole (law 6 - no
    stub rows), so fold them into the over-budget count with the lead hit's
    command first, then drop trailing commands - never the count - to fit."""
    m = _TRAILER_RE.match(trailer)
    if m is None:  # unknown trailer shape: keep it whole when it fits
        return trailer if _utf8_size(trailer) <= budget else _utf8_prefix(trailer, budget)
    n = int(m.group(1)) + len(block_ends or [])
    cmds = [c for _, c in block_ends or [] if c]
    tail = m.group(2)
    words = tail.split()
    handles_only = bool(words) and all(word.startswith("@") for word in words)
    cmds += words if handles_only else [c for c in tail.split(" · ") if c]
    joiner = (" " if cmds and all(c.startswith("@") and not any(
        ch.isspace() for ch in c) for c in cmds) else " · ")
    while cmds:
        out = f"[+{n} hit(s) over budget - {joiner.join(cmds)}]"
        if _utf8_size(out) <= budget:
            return out
        cmds.pop()
    out = f"[+{n} hit(s) over budget]"
    return out if _utf8_size(out) <= budget else _utf8_prefix(out, budget)


def _fit_text_payload(text: str, budget: int,
                      block_ends: list[tuple[int, str]] | None = None,
                      trailer: str = "") -> str:
    """Hard-cap a text payload at ``budget`` without corrupting markers already in it.

    ``block_ends``: (end offset, agrep-around command) per window block, so the cap
    marker names the first window the cut lands in (module contract: every
    truncation marker carries the command that prints the rest).
    ``trailer``: trailing disclosure block (the over-budget hit marker) that must
    survive the cap - content is evicted first, the trailer re-appended whole."""
    if trailer:
        joined = f"{text}\n\n{trailer}" if text else trailer
        if not budget or _utf8_size(joined) <= budget:
            return joined
        room = budget - _utf8_size(trailer) - 2
        if room >= 80:
            return _fit_text_payload(text, room, block_ends) + "\n\n" + trailer
        # no room for judgeable content beside the disclosure: evict every
        # content block into it and shrink it until it fits, count intact
        return _degrade_trailer(trailer, block_ends, budget)
    if not budget or _utf8_size(text) <= budget:
        return text
    generic = "\n[output truncated to --budget; rerun with a larger budget]"
    marker = generic
    byte_ends = [(_utf8_size(text[:end]), cmd) for end, cmd in block_ends or []]
    for _ in range(3):  # marker length and the block the cut lands in are interdependent
        cut_bytes = budget - _utf8_size(marker)
        cmd = next((c for end, c in byte_ends if end > cut_bytes and c), None)
        wanted = (f"\n[output truncated to --budget - rest at {cmd}]"
                  if cmd else generic)
        if wanted == marker:
            break
        marker = wanted
    if _utf8_size(marker) >= budget:
        marker = generic  # a command that cannot fit whole must not be emitted cut
    if _utf8_size(marker) >= budget:
        return _utf8_prefix(generic, budget)
    cut = len(_utf8_prefix(text, budget - _utf8_size(marker)))
    for m in _TRUNC_MARKER_RE.finditer(text, max(0, cut - 400)):
        if m.start() >= cut:
            break
        if m.end() > cut:
            cut = m.start()
            break
    return text[:cut].rstrip() + marker


def _render_text_payload(text: str, budget: int,
                         block_ends: list[tuple[int, str]] | None,
                         trailer: str, *, color: bool) -> str:
    """Fit the bytes actually written, including optional terminal escapes."""
    content_budget = _content_budget(budget)
    if not color:
        return _fit_text_payload(
            text, content_budget, block_ends, trailer).replace("\x1f", " ")
    if not budget:
        return _paint_payload(
            _fit_text_payload(text, 0, block_ends, trailer))

    plain_budget = content_budget
    for _ in range(12):
        plain = _fit_text_payload(text, plain_budget, block_ends, trailer)
        painted = _paint_payload(plain)
        excess = _utf8_size(painted) - content_budget
        if excess <= 0:
            return painted
        if plain_budget <= 0:
            break
        next_budget = max(0, plain_budget - excess)
        plain_budget = max(0, min(plain_budget - 1, next_budget))

    # A tiny finite page may not hold even one intact paint sequence.
    return _fit_text_payload(
        text, content_budget, block_ends, trailer).replace("\x1f", " ")


_PHRASE_GROUP = "agrep_phrase"
_TERM_GROUP_PREFIX = "agrep_term_"


def _anchor_patterns(query: str) -> list[re.Pattern]:
    """Span finders for a hit's own query: the exact phrase first (whitespace-
    flexible), then any content term, longest first - what made the row a hit."""
    toks = [re.escape(t) for t in query.split() if t]
    pats = []
    if toks:
        phrase = r"\s+".join(toks)
        pats.append(re.compile(f"(?P<{_PHRASE_GROUP}>{phrase})", re.I))
    terms = sorted({t.lower() for t in search._content_terms(query)
                    if len(t) >= 3}, key=lambda term: (-len(term), term))
    if terms:
        choices = (f"(?P<{_TERM_GROUP_PREFIX}{i}>{re.escape(term)})"
                   for i, term in enumerate(terms))
        pats.append(re.compile("|".join(choices), re.I))
    return pats


_WINDOW_MIN = 140      # below this a window stops being judgeable (law 6)
_ELISION_COST = 16     # "[1,234 chars] " - what one gap marker costs


def _merge_match_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(set(spans)):
        if merged and start <= merged[-1][1] + _WINDOW_MIN // 2:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _match_windows(text: str, limit: int, expand_cmd: str,
                   patterns: list[re.Pattern]) -> tuple[str, int] | None:
    """Keep the text AROUND each match instead of the head, with every gap
    accounted: `[252 chars] …match… [258 chars] …match… [277 chars]`.

    A message's head is usually preamble; the match is the reason the row is
    on the page. Returns None when no match is found or one window is all the
    budget affords - callers fall back to the single-window path."""
    action = f" [snippet - open: {expand_cmd}]"
    window_limit = limit - len(action)
    capacity = max(1, (window_limit - _ELISION_COST)
                   // (_WINDOW_MIN + _ELISION_COST))
    if capacity < 2:
        return None
    phrase_spans: list[tuple[int, int]] = []
    distinct: dict[str, tuple[int, int]] = {}
    repeats: list[tuple[int, int]] = []
    for pattern_index, pat in enumerate(patterns):
        term_groups = {name for name in pat.groupindex
                       if name.startswith(_TERM_GROUP_PREFIX)}
        for match in pat.finditer(text):
            name = match.lastgroup
            span = match.span(name) if name else match.span()
            if name == _PHRASE_GROUP:
                phrase_spans.append(span)
                if len(phrase_spans) >= capacity:
                    break
                continue
            key = name if name in term_groups else f"pattern:{pattern_index}"
            if key not in distinct:
                distinct[key] = span
            elif len(repeats) < capacity:
                repeats.append(span)
            if (term_groups and term_groups.issubset(distinct)
                    and len(repeats) >= capacity):
                break
        if phrase_spans:
            break
    candidates = phrase_spans if phrase_spans else [*distinct.values(), *repeats]
    if not candidates:
        return None
    selected: list[tuple[int, int]] = []
    for span in candidates:
        if len(_merge_match_spans([*selected, span])) <= capacity:
            selected.append(span)
    merged = _merge_match_spans(selected)
    count = len(merged)
    if count < 2:
        return None  # one window: the existing anchored path does it better
    room = (window_limit - (count + 1) * _ELISION_COST) // count
    out, cursor, omitted = [], 0, 0
    for start, end in merged:
        pad = max(0, (room - (end - start)) // 2)
        left, right = max(cursor, start - pad), min(len(text), end + pad)
        left = _snap(text, left, forward=True, floor=cursor)
        right = _snap(text, right, forward=False, floor=left + 1)
        if left > cursor:
            out.append(f"[{left - cursor:,} chars]")
            omitted += left - cursor
        out.append(text[left:right].strip())
        cursor = right
    if cursor < len(text):
        out.append(f"[{len(text) - cursor:,} chars]")
        omitted += len(text) - cursor
    return " ".join(out) + action, omitted


def _snap(text: str, index: int, *, forward: bool, floor: int) -> int:
    """Move an offset to the nearest word boundary, never past its floor."""
    if index <= floor:
        return floor
    window = text[max(floor, index - 40):index] if not forward else \
        text[index:index + 40]
    if forward:
        offset = window.find(" ")
        return index + offset + 1 if offset >= 0 else index
    offset = window.rfind(" ")
    return max(floor, index - (len(window) - offset)) if offset >= 0 else index


def _anchored_cap(text: str, limit: int, expand_cmd: str,
                  patterns: list[re.Pattern]) -> tuple[str, int]:
    """_cap, but the matched turn's slice must contain its match (law 6: the
    discriminating detail renders). When the match span sits past the head
    window, slide the window there; both elisions carry the around command."""
    if not limit or len(text) <= limit:
        return text, 0
    multi = _match_windows(text, limit, expand_cmd, patterns)
    if multi is not None:
        return multi
    span = None
    for pat in patterns:
        span = pat.search(text)
        if span is not None:
            break
    if span is not None:
        head = text[:limit]
        if " " in head[limit - 200:]:
            head = head[: head.rfind(" ")]
        # short lead-in: later budget shrinks keep heads, so the match must
        # sit near the slice start to survive them (law 6 over context)
        start = max(0, span.start() - min(120, max(48, limit // 6)))
        sp = text.find(" ", start)
        if 0 <= sp < span.start():
            start = sp + 1
        if span.end() > len(head) and start:
            body, tail_omitted = _cap(text[start:], limit, expand_cmd)
            return (f"[+{start:,} chars - {expand_cmd}] {body}",
                    start + tail_omitted)
    return _cap(text, limit, expand_cmd)


def _windows(
        hits: list[dict], context: int,
        cached: dict[tuple[str, int], dict] | None = None,
) -> list[tuple[dict, dict]]:
    """(hit, window) pairs, deduped by (session, turn) - a user turn and its reply are
    separate hits but one window. Semantic hits carry no turn; center those on the
    chat's first turn, where the problem is usually stated (get_window clamps)."""
    selected = []
    seen = set()
    for h in hits:
        key = (h["session"], h.get("turn"))
        if key in seen:
            continue
        seen.add(key)
        selected.append(h)
    windows: list[dict | None] = [None] * len(selected)
    missing = []
    missing_indexes = []
    for index, hit in enumerate(selected):
        turn = hit["turn"] if hit.get("turn") is not None else 0
        window = (cached or {}).get((hit["session"], turn))
        if window is None or context > search._HISTORY_META_LOOKBACK:
            missing.append((hit["session"], turn, context))
            missing_indexes.append(index)
            continue
        center = int(window.get("center", turn))
        lo, hi = center - context, center + context
        windows[index] = {
            **window,
            "turns": [row for row in window.get("turns") or []
                      if lo <= int(row.get("turn", center)) <= hi],
            "events": [event for event in window.get("events") or []
                       if lo <= int(event.get("turn", center)) <= hi],
        }
    if missing:
        for index, window in zip(missing_indexes, explore.get_windows(missing)):
            windows[index] = window
    return [(hit, window) for hit, window in zip(selected, windows)
            if isinstance(window, dict) and "error" not in window]


def _center_text(window: dict, hit: dict) -> str | None:
    """Return the indexed side of a window's center turn."""
    for turn in window.get("turns") or []:
        if int(turn.get("turn", -1)) != int(window.get("center", -1)):
            continue
        if str(hit.get("who") or "") == "agent" and turn.get("reply"):
            return turn["reply"]
        return turn.get("text") or turn.get("reply") or None
    return None


def _window_result_handle(window: dict, hit: dict, session_index) -> str:
    claim = hit.get("content_digest")
    row = {"session": window["session"], "turn": window["center"],
           "content_digest": claim, "who": hit.get("who"),
           "_event_identity": hit.get("_event_identity"),
           "_match_span": hit.get("_match_span")}
    text = None if claim else _center_text(window, hit)
    return compact.encode_bound_result_handle(
        row, session_index=session_index, text=text)


def _select(hits: list[dict], limit: int, query_count: int = 1, *,
            family_diverse: bool = True) -> list[dict]:
    """Merge results into a conversation-family- and query-diverse pack.

    Failure-trace analysis on LongMemEval found `--hits 3` being eaten by three
    turns of ONE (wrong) chat while the right chat sat at session-rank 4 - three
    copies of one conversation is strictly worse than three conversations for
    "have I hit this before?". Pack also promises several search angles: reserve
    one result per query in round-robin order before any query gets a second slot,
    then keep cycling until the shared budget target is full.
    """
    buckets = [[] for _ in range(max(1, query_count))]
    for hit in hits:
        i = int(hit.get("_recall_query", 0) or 0)
        buckets[i if 0 <= i < len(buckets) else 0].append(hit)
    out, seen_sessions, seen_query_families = [], set(), set()
    roots = search._family_roots_for_hits(hits) if family_diverse else {}
    offsets = [0] * len(buckets)
    while len(out) < limit:
        progressed = False
        for i, bucket in enumerate(buckets):
            while offsets[i] < len(bucket):
                hit = bucket[offsets[i]]
                offsets[i] += 1
                key = (roots.get(hit["session"], hit["session"])
                       if family_diverse else hit["session"])
                # One family slot per query - different queries may need different children of one root.
                query_family = (i, key)
                if (hit["session"] in seen_sessions
                        or query_family in seen_query_families):
                    continue
                seen_sessions.add(hit["session"])
                seen_query_families.add(query_family)
                out.append(hit)
                progressed = True
                break
            if len(out) >= limit:
                break
        if not progressed:
            break
    return out


def _reserve_semantic_hits(selected: list[dict], ranked: list[dict], limit: int,
                           query_count: int, *, family_diverse: bool) -> list[dict]:
    """Give meaning evidence a slot without sacrificing multi-query coverage."""
    if query_count == 1:
        keyword = [hit for hit in ranked if hit.get("lane") != "semantic"]
        meaning = [hit for hit in ranked if hit.get("lane") == "semantic"]
        return search._merge_auto_semantic_hits(
            keyword, meaning, limit, family_diverse=family_diverse)
    if limit <= query_count:
        return selected
    out = list(selected)
    roots = (search._family_roots_for_hits([*selected, *ranked])
             if family_diverse else {})

    def family(hit: dict) -> str:
        session = str(hit.get("session") or "")
        return roots.get(session, session) if family_diverse else session

    for query_i in range(max(1, query_count)):
        positions = [i for i, hit in enumerate(out)
                     if int(hit.get("_recall_query", 0) or 0) == query_i]
        if any(out[i].get("lane") == "semantic" for i in positions):
            continue
        seen = {(hit.get("session"), hit.get("turn")) for hit in out}
        seen_text = {key for hit in out if (key := search._hybrid_text_key(hit))}
        other_sessions = {out[i].get("session") for i in range(len(out))
                          if i not in positions}
        candidates = [hit for hit in ranked
                      if int(hit.get("_recall_query", 0) or 0) == query_i
                      and hit.get("lane") == "semantic"
                      and hit.get("session") not in other_sessions
                      and (hit.get("session"), hit.get("turn")) not in seen
                      and search._hybrid_text_key(hit) not in seen_text]
        if not candidates:
            continue
        meaning = candidates[0]
        same_family = next((i for i in positions
                            if family(out[i]) == family(meaning)), None)
        if same_family is not None:
            out[same_family] = meaning
        elif len(out) < limit:
            out.append(meaning)
        elif len(positions) >= 2:
            replace = max(
                positions,
                key=lambda i: (out[i].get("who") == "tool",
                               out[i].get("matched") in
                               ("all-terms", "content-terms"), i))
            out[replace] = meaning
    return out


def _weak_scatter(hit: dict) -> bool:
    """Bag-of-words fallback rows: context around a miss, never strong evidence."""
    return hit.get("matched") in ("all-terms", "content-terms")


_WEAK_SNIPPET = 160    # snippet cap inside a collapsed weak line
_WEAK_LINE_COST = 240  # budget reserved per collapsed line (head + snippet)


def _weak_block(hit: dict) -> bool:
    """Blocks that earn one line, not a transcript: weak scatter and sub-strong
    meaning rows - the same bands the probe refuses to fire on."""
    return _weak_scatter(hit) or search._semantic_row_weak(hit)


def _weak_line(head: str, text: str, anchors: list[re.Pattern],
               fallback: str = "") -> str:
    """One line for a weak block: the head's handle is the pull affordance and
    the snippet keeps the row's own match (law 6) - never a transcript.

    When the window prose does not re-find the match (a tool-output hit whose
    surrounding turns never repeat it), the hit's own search snippet is the
    match window - the head of unrelated prose is not."""
    def _anchored(value: str) -> tuple[str, tuple[int, int] | None]:
        flat = " ".join((value or "").split())
        for pat in anchors:
            m = pat.search(flat)
            if m is not None:
                return flat, m.span(m.lastgroup) if m.lastgroup else m.span()
        return flat, None

    flat, span = _anchored(text)
    if span is None and fallback:
        anchored_fallback, span = _anchored(fallback)
        if span is not None:
            flat = anchored_fallback
    snip = (console.snip_at(flat, *span, pad=60) if span
            else flat[:_WEAK_SNIPPET] + ("…" if len(flat) > _WEAK_SNIPPET else ""))
    snip = console.terminal_safe(snip)
    return f"── {head}{_SNIP_SEP}{snip}" if snip else f"── {head}"


def _merge_key(hit: dict) -> tuple:
    """Merged page order: lived evidence before command-lineage echoes, then
    strength and lane. An all-meta set keeps the prior score order unchanged;
    semantic rows sort by cosine rather than display-annotated score."""
    return (hit.get("_meta_row") is True,
            _weak_scatter(hit), hit.get("_recall_lane", 0),
            hit.get("matched") == "all-terms",
            -(hit.get("sem_score") if hit.get("sem_score") is not None
              else hit.get("score", 0)),
            -(hit.get("ts") or 0), hit["session"], hit.get("turn") or 0)


def _auto_semantic_query(query: str) -> bool:
    """Admit prose-shaped misses without treating identifier bags as prose."""
    return search._auto_semantic_query(query)


def _expand(pairs: list[tuple[dict, dict]], queries: list[str],
            budget: int, context: int,
            self_policy: common.SelfExclusion | None = None) -> list[tuple[dict, dict]]:
    """Coverage expansion: from 'a blind ±N window around each best hit' to 'the
    parts of the hit sessions that actually bear on the query'. Failure-trace
    analysis on LongMemEval put 70% of QA misses on windows that sliced the WRONG
    turns out of the RIGHT session - the evidence sat elsewhere in a session recall
    had already chosen. So: scan each hit session for turns matching the query's
    content terms that no existing window covers, rank across sessions by how many
    distinct terms they hold, and add compact windows around the best until the
    pack's pair count reaches what the budget can feed. Same budget - more places,
    each a little shorter - because a missing evidence turn costs an answer while a
    shorter window just costs padding."""
    if not budget:
        return pairs
    sessions = list(dict.fromkeys(w["session"] for _, w in pairs))
    terms = list(dict.fromkeys(
        t.lower() for t in search._content_terms(" ".join(queries))))[
            :corpusdb.SESSION_TERM_QUERY_LIMIT]
    room = min(12, budget // 2500)
    db = corpusdb.connect(quiet=True, allow_stale=True)
    if db is None:
        return pairs
    before_turns = (
        {self_policy.family.session: int(self_policy.boundary)}
        if self_policy is not None and self_policy.windowed else None)
    database_error = None
    try:
        sizes = corpusdb.session_text_sizes(
            db, sessions, before_turns=before_turns)
        candidates = (corpusdb.session_term_turns(
            db, sessions, terms, max(256, room * 32),
            before_turns=before_turns)
            if terms and room > len(pairs) else [])
    except sqlite3.DatabaseError as exc:
        database_error = exc
        sizes, candidates = {}, []
    finally:
        try:
            db.close()
        except sqlite3.DatabaseError as exc:
            database_error = database_error or exc
    if database_error is not None:
        corpusdb.record_query_database_error(database_error, db)
        return pairs
    if self_policy is not None:
        candidates = [
            row for row in candidates
            if not self_policy.excludes(str(row["session"]), row.get("turn"))
        ]

    # Phase 1 - widen: a session that fits its budget share is taken whole. Paraphrase
    # evidence shares no query terms, so term scans can't find it - never keyhole a fitting chat.
    out = list(pairs)
    share = budget // len(pairs)
    widen_requests, widen_indexes = [], []
    for i, (h, w) in enumerate(pairs):
        sess_chars = sizes.get(w["session"])
        if sess_chars is not None and sess_chars <= share * 1.2:
            widen_indexes.append(i)
            # Turns need not be contiguous (compaction/importers leave gaps); span the
            # actual first/last turn so "fits whole" means the whole conversation.
            radius = max(abs(w["center"] - w["first_turn"]),
                         abs(w["last_turn"] - w["center"]))
            widen_requests.append((w["session"], w["center"], radius))
    for i, full in zip(widen_indexes, explore.get_windows(widen_requests)):
        if "error" not in full:
            out[i] = (out[i][0], full)

    # Phase 2 - coverage extras: pull turns elsewhere in hit sessions that share content terms.
    if len(out) >= room:
        return out
    if not terms:
        return out
    covered = {(w["session"], t["turn"]) for _, w in out for t in w["turns"]}
    cands = []
    for row in candidates:
        key = (row["session"], row["turn"])
        if key not in covered:
            cands.append((row["term_hits"], row["session"], row["turn"], row["ts"]))
    cands.sort(key=lambda c: (-c[0], -c[3], c[1], c[2]))
    extra_requests, extra_hits = [], []
    radius = min(1, context)
    for n, sess, turn, ts in cands:
        if len(out) + len(extra_requests) >= room:
            break
        if (sess, turn) in covered:
            continue  # an earlier selected numeric window already owns it
        # Reserve the exact numeric range get_windows will read before the batch, so
        # overlapping candidates are suppressed without reopening the corpus per candidate.
        covered.update((sess, nearby)
                       for nearby in range(turn - radius, turn + radius + 1))
        extra_requests.append((sess, turn, radius))
        # surface the lane's own rank (distinct-term coverage) as a real number:
        # --json consumers sort/compare scores, and search never emits null ones
        extra_hits.append({"session": sess, "turn": turn, "ts": ts,
                           "score": round(n / len(terms), 4),
                           "score_kind": "expanded", "matched": "expanded"})
    for hit, w in zip(extra_hits, explore.get_windows(extra_requests)):
        if "error" in w:
            continue
        out.append((hit, w))
    return out


def _provenance_marks(hit: dict) -> str:
    """The marks search prints, so recall and search tell one story."""
    return " ".join(m for m in ("~self" if hit.get("_self") else "",
                                "~meta" if hit.get("_meta_row") else "") if m)


def _probe_line(
        queries: list[str], hits: list[dict], engine: str,
        total_sessions: int | None = None, session_index=None,
) -> str | None:
    """Build the one-line probe pointer, or None when evidence is too weak.

    Confidence is judged per lane: when no hit carries a semantic score, only
    the keyword lane served, and its own standard applies - a row holding every
    query term is a confident pointer. Absent semantic scores must never demote
    keyword evidence to a miss that plain search would answer first try.
    Related-terms rows stay weak in every lane.

    Every lane has a floor, because "this box has not hit that before" is a
    useful answer and the best available row is not evidence that it is a
    good one. The bag-of-words lane's floor is the lexical evidence its own
    ranking measured: all-terms means every token was found as a SUBSTRING,
    which unrelated prose satisfies routinely, and an unscored row cannot be
    judged at all."""
    keyword_only = all(h.get("sem_score") is None for h in hits)

    def confident(h: dict) -> bool:
        if h.get("sem_score") is not None:
            return h["sem_score"] >= PROBE_MIN_SEM
        if h.get("matched") not in ("all-terms", "content-terms"):
            return True
        if not (keyword_only and h.get("matched") == "all-terms"):
            return False
        try:
            return float(h["_evidence"]) >= PROBE_MIN_SCATTER_EVIDENCE
        except (KeyError, TypeError, ValueError):
            return False

    strong = [h for h in hits if confident(h)]
    if not strong:
        return None
    top = strong[0]
    sessions = len({h["session"] for h in strong})
    top_turn = top.get("turn")
    if session_index is None:
        session_index = common.indexed_session_prefix_candidates(
            hit.get("session") for hit in strong)
    top_ref = (compact.encode_bound_result_handle(
                   top, session_index=session_index)
               if top_turn is not None else compact.encode_session_target(
                   top["session"], session_index=session_index))
    where = " · ".join(x for x in (
                                   console.terminal_safe(top_ref),
                                   console.terminal_safe(top.get("agent", "")),
                                   console.terminal_safe(search._proj(top.get("project", ""))),
                                   _ts_label(top.get("ts") or 0),
                                   "~self" if top.get("_self") else "") if x)
    meaning = top.get("sem_score") is not None
    # F11: no internal store names, real plurals, and the evidence kind is
    # said once - by the pointer label, not the lead too
    if total_sessions is None:
        lead = f"top results span {surface.count_noun(sessions, 'past session')}"
    elif not total_sessions and meaning:
        lead = ("no prose match; "
                f"{surface.count_noun(sessions, 'semantic candidate')}")
    elif not total_sessions:
        # keyword-lane confidence without a phrase-tier session count: the
        # lead states the evidence kind instead of claiming zero matches
        lead = (f"no exact phrase match, but "
                f"{surface.count_noun(sessions, 'past session')} "
                f"{'holds' if sessions == 1 else 'hold'} every query term")
    else:
        lead = (f"{surface.count_noun(total_sessions, 'past session')} "
                f"{'matches' if total_sessions == 1 else 'match'}")
    # the pointer is hedged evidence, never a conclusion; the label owns the
    # hedge, the provenance clause, and the ~meta marker
    label = display_policy.probe_pointer_label(
        top, semantic=meaning,
        weak=bool(meaning and search._semantic_row_weak(top)))
    # the pull command is followed mechanically: it must rerun THIS probe, so a
    # multi-query probe suggests pack with each query as its own argument
    pull = (_command("agrep", "recall", queries[0]) if len(queries) == 1
            else _command("agrep", "pack", *queries))
    return f"recall: {lead} - {label}: {where} - pull: {pull}"


def _probe(queries: list[str], hits: list[dict], engine: str,
           total_sessions: int | None = None, budget: int = 0,
           session_index=None, *, meaning_down: bool = False,
           tools_excluded: bool = False,
           coverage: dict | None = None,
           verdict: surface.MissVerdict | None = None,
           said_once: set[str] | None = None,
           weak_neighbors_command: str | None = None) -> int:
    """Emit one pointer for confident past context, or the owned miss line:
    rc 1 still means no confident pointer, but never means no output."""
    line = _probe_line(
        queries, hits, engine, total_sessions=total_sessions,
        session_index=session_index)
    if line is None:
        summary = common.index_summary()
        # a filter over an empty dimension searched nothing (law 4): the miss
        # says 0 and carries the same owned record the zero-hit path renders
        empty = (coverage or {}).get("empty_dimensions") or []
        line = display_policy.probe_miss_line(
            engine,
            corpus_sessions=0 if empty
            else summary.get("sessions") if summary else None)
        for dimension in empty:
            line = f"{line} - {surface.empty_dimension_line(dimension)}"
        if not empty and verdict is not None:
            # search's zero speaks the same verdict vocabulary (law 5): the
            # proof this miss carries, or its ONE lever - never a stack
            line = f"{line} - {verdict.tail}"
            if verdict.owns_freshness and said_once is not None:
                said_once.add("freshness")
        elif meaning_down:
            # the one owned lane-down story (F4), not a third phrasing
            line = f"{line} - {surface.SEMANTIC_LANE_POLICY.keyword_only}"
        _write_payload(
            _fit_probe_line(line, budget, tools_excluded), budget)
        return 1
    _write_payload(
        _fit_probe_line(line, budget, tools_excluded), budget)
    return 0


def _fit_probe_line(line: str, budget: int, tools_excluded: bool) -> str:
    if not tools_excluded:
        return _fit_text_payload(line, _content_budget(budget))
    fact = surface.SCAN_TOOLS_PENDING_LINE
    full = f"{fact} - {line}"
    room = _content_budget(budget)
    if not room or _utf8_size(full) <= room:
        return full
    handle = re.search(r"@[^\s·]+", line)
    if handle is not None:
        pointer = f"{fact} · {handle.group(0)}"
        if _utf8_size(pointer) <= room:
            return pointer
    return _utf8_prefix(fact, room)


# Below this, a block becomes a stub that cannot be judged from retrieved evidence.
_JUDGEABLE_BLOCK = 900


def _msg_cap(share: int, w: dict, reserve: int = 120) -> int:
    """Per-message cap that fits a window into its share of the budget: the share
    spread over the window's texts, minus per-message room for the truncation marker
    (json rows reserve more - their key/session envelope counts too)."""
    if not share:
        return 0
    n = sum(1 for t in w["turns"] for x in (t["text"], t["reply"]) if x)
    return max(80, share // max(1, n) - reserve)


def _event_budget_cost(window: dict, hit: dict) -> int:
    """Rendered byte cost of the selected event; ambient tools stay hidden."""
    selected_identity = (
        hit.get("_event_identity") if hit.get("who") == "tool" else None)
    selected_span = hit.get("_match_span")
    return sum(
        min(360, 80 + _utf8_size(str(event.get("input") or ""))
            + min(200, _utf8_size(str(event.get("output") or ""))))
        for event in window.get("events") or []
        if _selected_tool_match(
            event, str(window.get("session") or ""),
            selected_identity, selected_span).selected
    )


def _window_expand_command(window: dict, target: str) -> str:
    """Open every actual turn represented by an expanded recall window."""
    center = int(window["center"])
    turns = [
        int(row["turn"])
        for rows in (window.get("turns") or (), window.get("events") or ())
        for row in rows
        if row.get("turn") is not None
    ]
    radius = max((abs(turn - center) for turn in turns), default=0)
    return _command(
        "agrep", "around", target, center, "-C", radius, "--tool-output", 200)


def _cap_line_bytes(line: str, budget: int) -> str:
    if _utf8_size(line) <= budget:
        return line
    if budget <= _utf8_size("…"):
        return _utf8_prefix(line, budget)
    return _utf8_prefix(line, budget - _utf8_size("…")).rstrip() + "…"


def _cap_required_lines(head: str, records: list[dict], indexes: list[int],
                        budget: int) -> str:
    """Share exact rendered bytes so every required row retains a visible prefix."""
    values = [head, *(records[index]["line"] for index in indexes)]
    remaining = max(0, budget - max(0, len(values) - 1))
    pending = list(range(len(values)))
    allocations = [0] * len(values)
    while pending:
        share = remaining // len(pending)
        small = [index for index in pending
                 if _utf8_size(values[index]) <= share]
        if not small:
            for offset, index in enumerate(pending):
                allocations[index] = share + (1 if offset < remaining % len(pending) else 0)
            break
        for index in small:
            size = _utf8_size(values[index])
            allocations[index] = size
            remaining -= size
            pending.remove(index)
    values = [_cap_line_bytes(value, allocations[index])
              for index, value in enumerate(values)]
    for index, value in zip(indexes, values[1:]):
        records[index]["line"] = value
    return values[0]


def _fit_recall_records(head: str, records: list[dict], limit: int,
                        expand: str, *, hidden_tools: int = 0) -> tuple[str, int]:
    """Keep required hit rows, then spend any remaining block budget on context."""
    if not limit:
        lines = [head, *(record["line"] for record in records)]
        if hidden_tools:
            lines.append(f"       [+{hidden_tools} tool calls - {expand}]")
        return "\n".join(lines), hidden_tools

    keep = set(range(len(records)))
    required = [index for index, record in enumerate(records)
                if record["required"]]
    dropped_tools = max(0, int(hidden_tools))
    dropped_context = 0

    def marker() -> str:
        if dropped_context:
            return f"     [output truncated to --budget - rest at {expand}]"
        if dropped_tools:
            return f"       [+{dropped_tools} tool calls - {expand}]"
        return ""

    def render() -> str:
        lines = [head, *(record["line"] for i, record in enumerate(records)
                         if i in keep)]
        if tail := marker():
            lines.append(tail)
        return "\n".join(lines)

    droppable = sorted(
        (i for i, record in enumerate(records) if not record["required"]),
        key=lambda i: records[i]["drop_key"])
    # Measuring a candidate drop by re-rendering is quadratic: one 6.4k-row
    # window re-joined and re-encoded ~670 KB per drop - 4.3 GB of UTF-8 to
    # settle a 3 KB block. Carry the byte total and pay each drop once.
    line_bytes = [_utf8_size(record["line"]) for record in records]
    head_bytes = _utf8_size(head)
    kept_bytes = sum(line_bytes)
    kept_rows = len(records)

    def size() -> int:
        """Bytes render() would emit: the parts, plus one "\n" per seam."""
        tail = marker()
        rows = 1 + kept_rows + (1 if tail else 0)
        return (head_bytes + kept_bytes
                + (_utf8_size(tail) if tail else 0) + rows - 1)

    for index in droppable:
        if size() <= limit:
            break
        keep.remove(index)
        kept_bytes -= line_bytes[index]
        kept_rows -= 1
        if records[index]["kind"] == "tool":
            dropped_tools += 1
        else:
            dropped_context += 1
    if size() > limit:
        tail = marker()
        if tail:
            compact_tail = ("     … surrounding context omitted"
                            if dropped_context else
                            "       … surrounding tool calls omitted")
            if _utf8_size(compact_tail) < _utf8_size(tail):
                tail = compact_tail
            head = _cap_required_lines(
                head, records, required,
                max(0, limit - _utf8_size(tail) - 1))
            return "\n".join([head, *(records[index]["line"] for index in required),
                              tail]), dropped_tools
        head = _cap_required_lines(head, records, required, limit)
        return "\n".join(
            [head, *(records[index]["line"] for index in required)]), dropped_tools
    return render(), dropped_tools


def _handle_filter_override(args, session: str, window: dict,
                            ts: int | None, since_ms: int | None,
                            until_ms: int | None) -> str | None:
    """An explicit @handle never runs the query layer, so supplied filters are
    not applied to it. That override is disclosed, never silent - naming the
    filters whose own predicates would have excluded the served session."""
    supplied = [flag for flag, value in (
        ("--chat", args.chat), ("--agent", args.agent),
        ("--project", args.project), ("--model", args.model),
        ("--who", args.who), ("--no-who", args.no_who),
        ("--since", args.since), ("--until", args.until))
        if value not in (None, "")]
    if not supplied:
        return None
    mismatched = []
    if args.chat and session != args.chat:
        mismatched.append("--chat")
    # mirror the engine predicates (substring, casefolded); --model/--who row
    # facts are not in the window, so those stay named-but-unverified
    if args.agent and args.agent.casefold() not in str(
            window.get("agent") or "").casefold():
        mismatched.append("--agent")
    if args.project and args.project.casefold() not in str(
            window.get("project") or "").casefold():
        mismatched.append("--project")
    if ts is not None:
        if since_ms is not None and ts < since_ms:
            mismatched.append("--since")
        if until_ms is not None and ts >= until_ms:
            mismatched.append("--until")
    return surface.handle_filter_override(supplied, mismatched)


def _main(argv: list[str] | None = None, prog: str = "recall", *,
          auto_semantic_timeout_s: float | None = None) -> int:
    common.utf8_stdio()

    one = prog == "recall"
    examples = (
        "examples:\n"
        "  agrep recall \"index lock\"            have I hit this before?\n"
        "  agrep recall @11111111:144          pull one compact result directly\n"
        "  agrep recall oom --hits 5 --budget 4000\n"
        "  agrep recall deadlock --json          structured, for piping\n"
        if one else
        "examples:\n"
        "  agrep pack unicode encoding cp1252    search several angles together\n"
        "  agrep pack \"index lock\" deadlock --hits 5\n"
        "  agrep pack oom timeout --budget 8000  one shared output budget\n"
        "  agrep pack parser dispatch --json     structured, for piping\n"
    )
    ap = surface.ArgumentParser(
        prog=f"agrep {prog}",
        description=("top hits + their conversation windows within one byte budget"
                     if one else
                     "several queries merged and deduped within one byte budget"),
        allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=examples +
               "\nprose recall combines keyword + meaning; -s forces meaning only.\n"
               "exit: 0 found, 1 proven no confident result, "
               "2 usage, unverified result, or a required lane unavailable.")
    ap.add_argument("query", nargs="+",
                    help="what to look for" if one else "queries, each searched separately")
    ap.add_argument("--hits", type=int, default=None, metavar="N",
                    help="top chats in the shared pack, round-robin across queries "
                         "(default: 3, growing with --budget; floored at one per query)")
    ap.add_argument("--budget", type=int, default=None, metavar="BYTES",
                    help="cap on rendered output bytes, including the final newline "
                         "(default 200000; 0 = uncapped)")
    ap.add_argument("-C", "--context", type=int, default=2, metavar="N",
                    help="turns before and after each hit (default 2)")
    ap.add_argument("-s", "--semantic", action="store_true",
                    help="force semantic-only search (recall normally uses keyword + meaning)")
    ap.add_argument("--lexical", action="store_true",
                    help="keyword only: disable the default meaning lane")
    self_group = ap.add_mutually_exclusive_group()
    self_group.add_argument("--self", dest="include_self", action="store_true",
                            help="include the calling agent's current-window echoes "
                                 "(older/family hits are otherwise labeled ~self)")
    self_group.add_argument("--no-self", dest="force_no_self",
                            action="store_true",
                            help="conservatively exclude the whole calling session "
                                 "family, even outside agent shells")
    ap.add_argument("--all-side-chats", action="store_true",
                    help="allow sibling child chats from one root conversation to "
                         "occupy separate ranked slots")
    ap.add_argument("--agent", help=f"only this agent ({', '.join(common.KNOWN_AGENTS)})")
    ap.add_argument("--project", help="only chats whose project label contains this "
                                      "(usually the workspace folder name)")
    ap.add_argument("--model", help="only turns from this exact model name")
    ap.add_argument("--soft", "--model-soft", dest="model_soft", action="store_true",
                    help="with --model, substring-match the model name (like *model*)")
    ap.add_argument("--who", metavar="LIST",
                    help="only these speakers, comma-separated "
                         "(same names as `agrep search`)")
    ap.add_argument("--no-who", dest="no_who", metavar="LIST",
                    help="exclude these speakers (e.g. --no-who subagent)")
    ap.add_argument("--no-meta", dest="no_meta", action="store_true",
                    help="drop structurally proven ~meta rows; retain one marked "
                         "row when it is the query's only evidence")
    ap.add_argument("--chat", "--session", dest="chat", metavar="ID",
                    help="only this chat: an 8-char id prefix (as shown) or full session uuid")
    ap.add_argument("--since", metavar="WHEN",
                    help="only hits at/after WHEN (7d / 24h / 2w / 30m, or 2026-06-01)")
    ap.add_argument("--until", "--before", dest="until", metavar="WHEN",
                    help="only hits before WHEN (same formats as --since)")
    ap.add_argument("--json", action="store_true",
                    help="one JSON object: {query, engine, hits: [{..., window: [...]}]}")
    ap.add_argument("--probe", action="store_true",
                    help="confidence check only: one qualifying candidate "
                         "pointer, compact scoped miss otherwise; scope notices "
                         "remain on stderr "
                         "(exit 0 hit / 1 proven miss / 2 unverified)")
    ap.add_argument("--no-auto", action="store_true",
                    help=surface.NO_AUTO_HELP)
    ap.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    args = surface.parse_args_with_presence(ap, argv)
    blank_query = any(not query.strip() for query in args.query)

    # a blank filter value is a usage error, not an unfiltered run (surface_policy
    # owns which flags filter and what each empty value would silently widen to)
    blank_filter = surface.filter_value_error(args)
    if blank_filter:
        ap.error(blank_filter)
    if args.agent:
        args.agent = common.normalize_agent_name(args.agent.lower())
    if args.hits is not None and args.hits < 1:
        ap.error("--hits must be at least 1")
    if args.budget is not None and args.budget < 0:
        ap.error("--budget must be 0 (uncapped) or greater")
    if args.budget is not None and 0 < args.budget < MIN_FINITE_BUDGET:
        ap.error(f"a finite --budget must be at least {MIN_FINITE_BUDGET} bytes")
    if (args.json and args.budget is not None
            and 0 < args.budget < MIN_JSON_BUDGET):
        ap.error(
            f"a finite JSON --budget must be at least "
            f"{MIN_JSON_BUDGET} bytes")
    if args.context < 0:
        ap.error("--context must be 0 or greater")
    if args.probe and args.json:
        ap.error("--probe emits a pointer line and cannot be combined with --json")
    # an option a surface renders inert is refused, never dropped
    gated = surface.option_gate_error(args, surface.RECALL_OPTION_GATES)
    if gated:
        ap.error(gated)
    try:
        # one owned parser for --who/--no-who (surface_policy holds the vocabulary)
        args.who_filter = surface.speaker_filter(
            args.who, args.no_who, surface.RECALL_SPEAKER_CHOICES)
    except ValueError as exc:
        ap.error(str(exc))

    raw_queries = [" ".join(args.query)] if one else args.query
    queries = [q.strip() for q in raw_queries if q.strip()]
    self_inactive_reason = "caller-unresolved"
    auto_self_exclusion = common.in_agent_context()
    self_exclusion_requested = (
        not args.include_self
        and (args.force_no_self
             or (auto_self_exclusion and not (args.chat or ""))))

    said_once: set[str] = set()

    def _note_freshness() -> None:
        # every porcelain surface carries the one story line; machine stdout
        # keeps grep parity because the line rides stderr. Ten call sites emit
        # it, so "once per render" is enforced here, not by each site's return.
        if "freshness" in said_once:
            return
        said_once.add("freshness")
        notice = indexd_runtime.agent_freshness_notice()
        if notice:
            common.log(notice)

    def _machine_fields(
            policy=None, *, excluded_hits: int | None = None,
            semantic_coverage: dict | None = None,
            exclusion_pending: bool = False,
            index_unavailable: bool = False,
            filter_coverage: dict | None = None) -> dict:
        if args.include_self:
            inactive_reason = "explicit-include"
        elif self_exclusion_requested:
            inactive_reason = (
                "index-unavailable" if exclusion_pending
                else self_inactive_reason)
        elif args.chat:
            inactive_reason = "chat-filter"
        else:
            inactive_reason = "machine-surface"
        publication_converging = (
            indexd_runtime.foreground_refresh_converging(
                checked=not args.no_auto))
        freshness = indexd_runtime.machine_freshness(
            checked=not args.no_auto,
            publication_converging=publication_converging)
        if index_unavailable and freshness["state"] in (
                "no-known-failure", "unchecked"):
            freshness = {
                "state": "unavailable",
                "failing": False,
                "checked": freshness.get("checked"),
                "may_be_stale": True,
                "code": "index-unavailable",
            }
        generation_fields = corpusdb.machine_freshness_fields(
            freshness, publication_converging=publication_converging)
        if isinstance(generation_fields.get("freshness"), dict):
            # honest counts: a zero failure streak never prints beside
            # failing=true (F7; recall emits one object, so first=True)
            generation_fields["freshness"] = surface.row_freshness_disclosure(
                generation_fields["freshness"], first=True)
        return {
            "self_exclusion": surface.self_exclusion_disclosure(
                policy, inactive_reason=inactive_reason,
                excluded_hits=excluded_hits),
            # never omitted: an absent field would read as "no dimension is
            # empty", the exact inference an unexplained zero already invites
            "filter_coverage": filter_coverage or (
                surface.filter_coverage_disclosure(
                    [], checked=False,
                    reason="the query returned before the index was filtered")),
            **generation_fields,
            "semantic_coverage": semantic_coverage,
        }

    def _json_error(
            code: str, *, engine: str = "query:error",
            reason: str | None = None, policy=None,
            semantic_coverage: dict | None = None,
            exclusion_pending: bool = False,
            index_unavailable: bool = False) -> None:
        if not args.json:
            return
        error = {"code": code}
        if reason:
            error["reason"] = common.terminal_safe(reason)
        obj = {
            "query": queries[0] if one and queries else "" if one else queries,
            "engine": engine, "hits": [], "error": error,
            **_machine_fields(
                policy, semantic_coverage=semantic_coverage,
                exclusion_pending=exclusion_pending,
                index_unavailable=index_unavailable),
        }
        output_budget = args.budget or 0
        _write_payload(
            _fit_json_payload(obj, _content_budget(output_budget)),
            output_budget)

    if blank_query or not queries:
        # bare `agrep recall`/`agrep pack` may have meant the word itself
        common.log("query arguments must not be empty "
                   "(searching the word itself: "
                   f'agrep search "{prog}")')
        _json_error("empty-query")
        return 2
    if args.semantic and args.lexical:
        common.log("-s and --lexical are contradictory; pick one")
        _json_error("conflicting-modes")
        return 2
    if args.semantic and args.who_filter == "tool":
        common.log("-s cannot search --who tool: tool rows are not embedded; "
                   "use --lexical")
        _json_error("unsupported-speaker")
        return 2

    try:
        since_ms = search._parse_when(args.since) if args.since else None
        until_ms = search._parse_when(args.until) if args.until else None
    except SystemExit:
        _json_error("invalid-time-filter")
        return 2
    # an inverted window is empty by construction, so its zero is a usage
    # error the tool can name from the bounds alone (surface_policy owns it)
    inverted = surface.window_bounds_error(
        args.since, since_ms, args.until, until_ms)
    if inverted:
        common.log(inverted)
        _json_error("empty-time-window", reason=inverted)
        return 2

    timing_value = os.environ.get("AGREP_SEM_TIMING", "")
    timing_enabled = common.DEBUG or timing_value.lower() not in (
        "", "0", "false", "no", "off")
    recall_started = time.perf_counter()
    if not indexd_runtime.ensure_index(
            auto=not args.no_auto,
            quiet=bool(args.json or args.probe or not sys.stdout.isatty())):
        _json_error(
            "index-unavailable", engine="index:unavailable",
            exclusion_pending=True, index_unavailable=True)
        _note_freshness()
        return 2
    if not args.no_auto:
        indexd_runtime.wait_for_delegated_publication()
    common.lap("freshen")


    direct_hit = None
    handle_filter_note = None
    # content-verification outcomes are facts on the result: stderr renders
    # them for a human, --json carries them like around's served record
    handle_notes: list[dict] = []
    if (one and len(queries) == 1
            and (queries[0].startswith("@")
                 or compact.is_result_handle(queries[0]))):
        try:
            (_prefix, _turn, handle_digest, handle_event_identity,
             handle_match_span) = compact.parse_result_handle_claim(queries[0])
        except compact.CompactError:
            pass
        else:
            try:
                session, turn = compact.resolve_result_handle(
                    queries[0], explore.resolve_session)
            except compact.CompactError as exc:
                common.log(str(exc))
                _json_error(
                    "result-handle-unresolved", engine="handle",
                    reason=str(exc))
                return 2
            window = explore.get_window(session, turn, max(0, args.context))
            if "error" in window:
                reason = common.terminal_safe(window["error"])
                common.log(reason)
                _json_error("result-handle-unresolved", engine="handle",
                            reason=reason)
                return 2
            if int(window.get("center", -1)) != turn:
                reason = (f"result handle turn {turn} is out of range "
                          f"(session has turns {window['first_turn']}-"
                          f"{window['last_turn']}) - "
                          f"{surface.stale_handle_recovery(common.cli_name())}")
                common.log(reason)
                _json_error("stale-result-handle", engine="handle", reason=reason)
                _note_freshness()
                return 2
            if handle_digest is None:
                note = surface.around_service_note("handle_unverified")
                handle_notes.append(note)
                common.log(note["note"])
                text = _center_text(window, {})
                if not text:
                    reason = "legacy result handle points to no searchable content"
                    common.log(reason)
                    _json_error(
                        "stale-result-handle", engine="handle", reason=reason)
                    return 2
                handle_digest = compact.content_digest(text)
            else:
                rescue = _resolve_handle_claims(
                    window, session, turn, handle_digest,
                    handle_event_identity)
                if rescue.outcome == "ambiguous":
                    # several equal claims: recoverable ambiguity, listed and
                    # refused - never rendered as content loss (around's rule)
                    reason = surface.handle_content_ambiguous(
                        compact.encode_session_target(session), turn,
                        rescue.candidates, common.cli_name())
                    common.log(reason)
                    _json_error(
                        "content-ambiguous", engine="handle", reason=reason)
                    _note_freshness()
                    return 2
                if rescue.outcome == "absent":
                    reason = surface.handle_content_lost(
                        compact.encode_session_target(session), turn,
                        common.cli_name())
                    common.log(reason)
                    _json_error(
                        "stale-result-handle", engine="handle", reason=reason)
                    _note_freshness()
                    return 2
                found = rescue.turn
                if handle_event_identity is None:
                    handle_event_identity = rescue.event_identity
                if found != turn:
                    note = surface.around_service_note(
                        "content_moved", requested=turn, served=found)
                    handle_notes.append(note)
                    common.log(note["note"])
                    turn = found
            # the row states the session's real identity, or honest absence
            # (None), never fabricated blanks the surfaces would render as fact
            center_ts = next(
                (t.get("ts") for t in window.get("turns", ())
                 if int(t.get("turn", -1)) == turn), None)
            direct_hit = {"session": session, "turn": turn, "score": 1.0,
                          "matched": "phrase",
                          "who": "tool" if handle_event_identity else "",
                          "agent": str(window.get("agent") or ""),
                          "project": str(window.get("project") or ""),
                          "ts": center_ts,
                          "content_digest": handle_digest,
                          "_recall_lane": 0, "_recall_query": 0,
                          "_direct_handle": True}
            if handle_event_identity is not None:
                direct_hit["_event_identity"] = handle_event_identity
            if handle_match_span is not None:
                direct_hit["_match_span"] = handle_match_span
            handle_filter_note = _handle_filter_override(
                args, session, window, center_ts, since_ms, until_ms)
            if handle_filter_note and not args.json:
                common.log(handle_filter_note)
    if args.chat and direct_hit is None:
        # Query chat prefixes must resolve before ranking. A direct result
        # handle bypasses every supplied filter instead, and discloses that
        # override without letting an unrelated stale/ambiguous --chat veto it.
        resolved = search._resolve_chat(args.chat)
        if resolved is None:
            _json_error("chat-unresolved")
            return 2
        args.chat = resolved

    # Compute the budget before searching so the query layer retrieves that many SESSIONS
    # directly instead of recall guessing at a turn-row shortlist depth. One default for
    # both surfaces: terminals have scrollback, and 8k starved every human run.
    budget = args.budget if args.budget is not None else 200_000
    target = (1 if args.probe else
              max(1, args.hits) if args.hits is not None else
              max(3, min(8, budget // 5000)) if budget else 3)
    # _select promises one slot per query; a smaller target silently starves
    # whole queries, so floor it (probe keeps its one-pointer contract).
    hits_floored = None
    if not args.probe:
        floored = max(target, len(queries))
        if args.hits is not None and floored > target:
            # law 2: raising an explicit --hits is disclosed, never silent
            hits_floored = {"requested": args.hits, "raised_to": floored}
            if not args.json:
                common.log(f"--hits {args.hits} raised to {floored}: "
                           "pack reserves one slot per query")
        target = floored
    # a lone query needs headroom beyond its keyword lead; floored packs don't
    meaning_room = target >= max(2, len(queries))
    query_target = max(
        target, search._HISTORY_META_ROWS_PER_QUERY if args.no_meta else 8)

    family_diverse = not args.all_side_chats
    current_family = None
    self_policy = None

    # --chat only waives the automatic exclusion; an explicit --no-self still
    # binds inside the chat filter (teach: "--no-self hides the whole family").
    if self_exclusion_requested:
        self_policy = common.calling_self_exclusion(
            conservative=args.force_no_self)
        current_family = self_policy.family if self_policy is not None else None
        if self_policy is None:
            identity = common.calling_identity()
            self_inactive_reason = (
                "window-unresolved" if identity.session else identity.reason)
            if args.force_no_self and not args.json and not identity.session:
                line = ("--no-self was not applied: caller identity is unknown; "
                        "no session was excluded")
                common.log(console.paint(
                    "d", line, common.color_enabled(sys.stderr, args.color)))
    semantic_timeout_s = (search._AUTO_SEMANTIC_TIMEOUT_S
                          if auto_semantic_timeout_s is None
                          else max(0.0, float(auto_semantic_timeout_s)))
    fkw = dict(limit=query_target, session_limit=query_target,
               sort="score", agent=args.agent,
               project=args.project, who=args.who_filter, model=args.model,
               model_soft=args.model_soft, chat=args.chat,
               since_ms=since_ms, until_ms=until_ms,
               family_diverse=family_diverse,
               allow_model_download=bool(args.semantic),
               allow_fallback=not args.lexical,
               # probe prints an exact session count; normal recall skips corpus-wide totals
               exact_totals=args.probe)
    if self_policy is not None:
        # scope discloses once, at the end of the render, through the one
        # owned notice (F1/F4) - no second policy line up front
        query_filters = self_policy.query_filters()
        fkw.update(query_filters)

    active_semantic_pending = None
    tools_excluded = False

    def _finish_active_semantic():
        nonlocal active_semantic_pending
        pending = active_semantic_pending
        active_semantic_pending = None
        return (search._safe_finish_semantic_query(pending)
                if pending is not None else None)

    self_count_specs: list[tuple[str, str, dict, float | None]] = []

    def _run_query(q: str, *, mode: str, **kwargs):
        nonlocal tools_excluded
        try:
            result = search.run_query(q, mode=mode, **kwargs)
            if result is not None and mode == "keyword" and result.get("tools_excluded"):
                tools_excluded = True
            if (self_policy is not None and kwargs.get("exclude_session")
                    and result is not None
                    and not result.get("fallback_recommended")):
                self_count_specs.append((q, mode, dict(kwargs), None))
            return result
        except search.SnapshotPublicationTimeout as exc:
            _finish_active_semantic()
            if not args.json:
                common.log(str(exc))
            _json_error("snapshot-publication-timeout", reason=str(exc))
            raise _RecallQueryAbort from exc
        except search.NativeEventScanError as exc:
            _finish_active_semantic()
            if not args.json:
                common.log(
                    "tool-event search could not verify a complete snapshot")
            _json_error("event-scan-failed", reason=str(exc))
            raise _RecallQueryAbort from exc
        except search.DirectSnapshotQueryError as exc:
            _finish_active_semantic()
            if not args.json:
                common.log(
                    "the published transcript snapshot could not be verified")
            _json_error("direct-snapshot-unverified", reason=str(exc))
            raise _RecallQueryAbort from exc
        except search.QueryDatabaseBusyError as exc:
            _finish_active_semantic()
            common.log(str(exc))
            _json_error(
                "search-index-busy", engine="corpusdb:busy",
                reason=str(exc))
            raise _RecallQueryAbort from exc
        except search.QueryDatabaseUnavailableError as exc:
            _finish_active_semantic()
            common.log(str(exc))
            _json_error(
                "search-index-unavailable", engine="corpusdb:unavailable",
                reason=str(exc))
            raise _RecallQueryAbort from exc

    # The default recall lane is hybrid; explicit modes still choose one engine.
    requested_mode = "semantic" if args.semantic else "keyword"
    engine = "handle" if direct_hit is not None else ""
    fell_back = False
    hybrid_used = False
    # Weak-neighbor escalation arrived as unfinished snapshot work: its render
    # sites landed without the code that ever sets this. None keeps every one
    # of those sites on its existing behavior until the feature actually ships.
    weak_neighbors_command = None
    semantic_unavailable = False
    semantic_runtime_failure = False
    meaning_lane_down = False
    meaning_lanes_run = 0
    meaning_down_queries: list[str] = []
    hits: list[dict] = [direct_hit] if direct_hit is not None else []
    exact_probe_sessions: int | None = 1 if direct_hit is not None else None
    semantic_meta: dict = {}
    semantic_queries: list[dict] = []
    used_engines: list[str] = ["handle"] if direct_hit is not None else []

    def _note_engine(name: str | None) -> None:
        if name and name not in used_engines:
            used_engines.append(name)

    def _merge_semantic_meta(res: dict, query: str) -> None:
        nonlocal semantic_meta
        if not semantic_meta:
            semantic_meta = {
                "score_kind": res.get("score_kind"),
                "semantic_coverage": res.get("semantic_coverage"),
                "semantic_accelerator_coverage": res.get(
                    "semantic_accelerator_coverage"),
                "partial": bool(res.get("partial")),
                "semantic_status": res.get("semantic_status"),
            }
        elif semantic_meta.get("score_kind") != res.get("score_kind"):
            semantic_meta["score_kind"] = "mixed"
        integrity = res.get("semantic_integrity")
        previous_integrity = semantic_meta.get("semantic_integrity") or {}
        priority = (
            integrity.get("state") == "generation-rejected",
            int(integrity.get("dropped") or 0),
        ) if integrity else (False, 0)
        previous_priority = (
            previous_integrity.get("state") == "generation-rejected",
            int(previous_integrity.get("dropped") or 0),
        )
        if integrity and priority > previous_priority:
            semantic_meta["semantic_integrity"] = integrity
        coverage = res.get("semantic_coverage")
        if coverage:
            semantic_meta["semantic_coverage"] = coverage
            semantic_meta["partial"] = not coverage.get("complete", False)
        accelerator = res.get("semantic_accelerator_coverage")
        if accelerator:
            semantic_meta["semantic_accelerator_coverage"] = accelerator
            semantic_meta["partial"] = (semantic_meta.get("partial", False)
                                        or not accelerator.get("complete", False))
        status = res.get("semantic_status")
        semantic_queries.append({"query": query, "status": status,
                                 "coverage": coverage})
        statuses = [row["status"] for row in semantic_queries
                    if isinstance(row.get("status"), dict)]
        if len(statuses) == 1:
            semantic_meta["semantic_status"] = dict(statuses[0])
        elif statuses:
            states = {row.get("state") for row in statuses}
            aggregate_status = {
                "state": next(iter(states)) if len(states) == 1 else "mixed",
                "complete": all(bool(row.get("complete")) for row in statuses),
                "fallback_recommended": all(
                    bool(row.get("fallback_recommended")) for row in statuses),
            }
            unavailable = [
                row for row in statuses
                if (row.get("fallback_recommended")
                    or row.get("state") == "unavailable")]
            if unavailable:
                reasons = {
                    str(row.get("reason") or "") for row in unavailable
                    if row.get("reason")}
                retryable = all(
                    surface.semantic_status_retryable(row)
                    for row in unavailable)
                if retryable:
                    aggregate_status["retryable"] = True
                    aggregate_status["reason"] = (
                        next(iter(reasons)) if len(reasons) == 1
                        else surface.SEMANTIC_WORKER_TRANSIENT_REASON)
                elif len(reasons) == 1:
                    aggregate_status["reason"] = next(iter(reasons))
            semantic_meta["semantic_status"] = aggregate_status
        if len(queries) > 1:
            semantic_meta["semantic_queries"] = list(semantic_queries)

    search_queries = [] if direct_hit is not None else queries
    for query_i, q in enumerate(search_queries):
        res = None
        query_hits: list[dict] = []
        used_tool_fallback = False
        semantic_attempted = False
        semantic_prestart_attempted = False
        semantic_pending = None
        semantic_pending_kwargs = None
        semantic_runtime = search._semantic_runtime_installed()
        if (requested_mode == "keyword" and not args.lexical and not args.probe
                and args.who_filter != "tool"
                and meaning_room and semantic_runtime
                and _auto_semantic_query(q)):
            semantic_prestart_attempted = True
            semantic_pending_kwargs = {
                **fkw,
                "limit": max(query_target, search._AUTO_SEMANTIC_FETCH),
                "session_limit": max(query_target, search._AUTO_SEMANTIC_FETCH),
                "semantic_timeout_s": semantic_timeout_s}
            semantic_pending = search._safe_start_semantic_query(
                q, semantic_pending_kwargs)
            active_semantic_pending = semantic_pending
        if requested_mode == "semantic":
            raw_semantic = None
            semantic_attempted = True
            raw_semantic = _run_query(q, mode="semantic", **fkw)
            res = raw_semantic
            if (res is not None and res.get("fallback_recommended")
                    and not surface.semantic_lane_answered(
                        res.get("semantic_status"))):
                _merge_semantic_meta(res, q)
                res = None
            if res is None:
                semantic_unavailable = True
                status = (raw_semantic or {}).get("semantic_status") or {
                    "state": "unavailable", "complete": False,
                    "fallback_recommended": True}
                state = status.get("state") or "unavailable"
                semantic_runtime_failure = (
                    semantic_runtime_failure or state != "query-rejected")
                engine_name = ("semantic:policy" if state == "query-rejected"
                               else "semantic:unavailable")
                res = {"hits": [], "total": 0, "chats": 0,
                       "tool_hits": 0, "engine": engine_name,
                       "mode": "semantic", "fallback_recommended": True,
                       "semantic_status": status,
                       "semantic_coverage": (raw_semantic or {}).get(
                           "semantic_coverage"), "score_kind": (raw_semantic or {}).get(
                               "score_kind"),
                       "semantic_accelerator_coverage": (raw_semantic or {}).get(
                           "semantic_accelerator_coverage")}
                if not semantic_queries or semantic_queries[-1].get("query") != q:
                    _merge_semantic_meta(res, q)
        if res is None:
            keyword_fkw = ({**fkw,
                            "limit": max(query_target, search._AUTO_SEMANTIC_FETCH),
                            "session_limit": max(query_target,
                                                 search._AUTO_SEMANTIC_FETCH)}
                           if semantic_pending is not None else fkw)
            if args.who_filter is not None:
                res = _run_query(q, mode="keyword", **keyword_fkw)
            else:
                # Prose is the primary lane; the much larger tool corpus is queried only when prose
                # can't fill the requested sessions - tool evidence stays reachable without dominating.
                res = _run_query(
                    q, mode="keyword", include_tools=False, **keyword_fkw)
                # Skipping the tool corpus takes STRONG fill: weak scatter that
                # reaches `target` must not mask tool sessions holding the queried
                # phrase verbatim - the gate is evidence strength, not row count.
                if (len(res["hits"]) < target
                        or all(_weak_scatter(h) for h in res["hits"])):
                    tool_kw = {**fkw, "who": "tool"}
                    tool_res = _run_query(q, mode="keyword", **tool_kw)
                    if tool_res["hits"]:
                        _note_engine(tool_res.get("engine"))
                    for hit in tool_res["hits"]:
                        hit["_recall_lane"] = 2
                    query_hits.extend(tool_res["hits"])
                    used_tool_fallback = bool(tool_res["hits"])
                    fell_back = fell_back or bool(tool_res.get("terms_fallback"))
                    fell_back = fell_back or bool(tool_res.get("content_fallback"))
            # Probe stays miss-triggered to preserve its cheap one-line contract.
            strong = any(not _weak_scatter(h) for h in res["hits"])
            nl_shaped = args.semantic or _auto_semantic_query(q)
            want_meaning = (nl_shaped and (args.probe or meaning_room)
                            and (not args.probe or not strong))
            if (not args.lexical and args.who_filter != "tool"
                    and semantic_runtime and want_meaning
                    and not semantic_attempted):
                if semantic_pending is not None:
                    sem = _finish_active_semantic()
                    if (self_policy is not None and sem is not None
                            and not sem.get("fallback_recommended")):
                        self_count_specs.append(
                            (q, "semantic", dict(semantic_pending_kwargs or fkw),
                             PROBE_MIN_SEM))
                elif semantic_prestart_attempted:
                    # A failed thread start is already this query's semantic attempt.
                    sem = None
                else:
                    sem = _run_query(
                        q, mode="semantic", **{**fkw,
                            "semantic_timeout_s": semantic_timeout_s})
                semantic_pending = None
                meaning_lanes_run += 1
                # search's hybrid degradation predicate: a lane that refused
                # or fell back is down; an empty confident answer is not
                if sem is None or sem.get("fallback_recommended"):
                    meaning_lane_down = True
                    meaning_down_queries.append(q)
                if sem is None:
                    _merge_semantic_meta(
                        {"score_kind": semantic_meta.get("score_kind"),
                         "semantic_status": {
                             "state": "unavailable", "complete": False,
                             "fallback_recommended": True}}, q)
                else:
                    _merge_semantic_meta(sem, q)
                sem_hits = [h for h in (sem["hits"] if sem else [])
                            if (h.get("sem_score") or 0) >= PROBE_MIN_SEM]
                if sem_hits:
                    hybrid_used = True
                    _note_engine(sem.get("engine"))
                    for hit in sem_hits:
                        hit["_recall_lane"] = 1
                        hit["lane"] = "semantic"
                    query_hits.extend(sem_hits)
        if semantic_pending is not None:
            _finish_active_semantic()
        _note_engine(res.get("engine"))
        engine = engine or res["engine"]
        fell_back = fell_back or bool(res.get("terms_fallback"))
        fell_back = fell_back or bool(res.get("content_fallback"))
        for hit in res["hits"]:
            if "_recall_lane" not in hit:
                hit["_recall_lane"] = 2 if hit.get("who") == "tool" else 0
            hit["_recall_query"] = query_i
        for hit in query_hits:
            hit["_recall_query"] = query_i
        query_hits[:0] = res["hits"]
        hits.extend(query_hits)
        if res["engine"].startswith("semantic:"):
            for hit in res["hits"]:
                hit["lane"] = "semantic"
                hit.setdefault("score_kind", res.get("score_kind") or "cosine")
            if not semantic_queries or semantic_queries[-1].get("query") != q:
                _merge_semantic_meta(res, q)
        # Only complete keyword ranked totals are exact: semantic totals cover just the
        # lane's candidate pool, and pack unions can overlap.
        if (one and not res["engine"].startswith("semantic:")
                and not used_tool_fallback and not tools_excluded):
            # the probe line says "prose matches": count phrase sessions the way the
            # fire gate does, not the always-on any-order/content lanes.
            exact_probe_sessions = res.get("phrase_chats", res["chats"])
    if args.semantic and semantic_runtime_failure:
        if not args.json:
            integrity_notice = surface.semantic_integrity_notice(
                semantic_meta.get("semantic_integrity"))
            common.log(integrity_notice or surface.semantic_unavailable_notice(
                semantic_meta.get("semantic_status")))
            if not args.probe:
                return 2
        hits = []
    if used_engines:
        engine = "+".join(used_engines)
    common.lap("query", engine)
    self_excluded_count: int | None = None
    if self_policy is not None:
        exact_specs = []
        if len(self_count_specs) == 1:
            exact_specs = self_count_specs
        elif len(self_count_specs) == 2:
            first, second = self_count_specs
            same_query_lane = (
                first[0] == second[0] and first[1] == second[1] == "keyword")

            def _speaker_partition(spec) -> str | None:
                kwargs = spec[2]
                if kwargs.get("who") == "tool":
                    return "tool"
                if (kwargs.get("who") is None
                        and kwargs.get("include_tools") is False):
                    return "prose"
                return None

            if (same_query_lane
                    and {_speaker_partition(first), _speaker_partition(second)}
                    == {"prose", "tool"}):
                exact_specs = self_count_specs
        if exact_specs:
            total = 0
            for (measured_query, measured_mode, measured_kwargs,
                 minimum_sem_score) in exact_specs:
                measured = search._self_exclusion_match_keys(
                    measured_query, measured_mode, measured_kwargs, self_policy,
                    minimum_sem_score=minimum_sem_score,
                    drop_meta=args.no_meta)
                if measured is None:
                    break
                total += len(measured)
            else:
                self_excluded_count = total
    self_dropped = 0
    if self_policy is not None:
        # A page can render the exclusion notice and still show a labeled row;
        # the probe's whole output is one pointer, so it ranks exactly the rows
        # its notice does not speak for - one policy, not two spellings.
        hidden = self_policy.announced if args.probe else self_policy.excludes
        kept = []
        for hit in hits:
            session = str(hit.get("session") or "")
            hit.pop("_self", None)
            if hidden(session, hit.get("turn")):
                continue
            if self_policy.labels(session, hit.get("turn")):
                hit["_self"] = True
            kept.append(hit)
        if len(kept) != len(hits):
            self_dropped = len(hits) - len(kept)
            hits = kept
    if direct_hit is not None and self_dropped:
        self_excluded_count = self_dropped

    def _note_self_exclusion() -> None:
        # JSON owns the structured count; prose emits one counted line.
        if (self_policy is None or current_family is None
                or args.json or not self_excluded_count):
            return
        common.log(surface.self_exclusion_notice(
            resolved=current_family.resolved, dropped=self_excluded_count,
            windowed=self_policy.windowed))

    def _trim_self_windows(pairs: list[tuple[dict, dict]]) -> list[tuple[dict, dict]]:
        if self_policy is None or not self_policy.windowed:
            return pairs
        boundary = self_policy.boundary
        if type(boundary) is not int:  # noqa: E721 -- malformed fails open
            return pairs
        caller = self_policy.family.session
        out = []
        for hit, window in pairs:
            session = str(window.get("session") or "")
            if self_policy.excludes(session, hit.get("turn")):
                continue
            if self_policy.labels(session, hit.get("turn")):
                hit["_self"] = True
            if session != caller:
                out.append((hit, window))
                continue
            trimmed = dict(window)
            trimmed["turns"] = [
                turn for turn in window.get("turns", [])
                if (type(turn.get("turn")) is not int  # noqa: E721
                    or turn["turn"] < boundary)
            ]
            trimmed["events"] = [
                event for event in window.get("events", [])
                if (type(event.get("turn")) is not int  # noqa: E721
                    or event["turn"] < boundary)
            ]
            if not trimmed["turns"]:
                continue
            turns = [turn["turn"] for turn in trimmed["turns"]
                     if type(turn.get("turn")) is int]  # noqa: E721
            if turns:
                trimmed["first_turn"], trimmed["last_turn"] = min(turns), max(turns)
            out.append((hit, trimmed))
        return out
    lineage_windows = (
        search._mark_history_meta(hits, queries)
        if not args.json or args.no_meta else {})
    hits.sort(key=_merge_key)
    meta_dropped = meta_retained = 0
    if args.no_meta and hits:
        hits, meta_dropped, meta_retained = search._filter_meta_rows(hits)
        if meta_dropped or meta_retained:
            common.log(surface.meta_filter_notice(
                meta_dropped, meta_retained))
    if args.probe:
        # one pointer out, but the judgment set holds each query's best
        # candidate (per lane under hybrid): a weak query-0 row must not
        # eclipse another query's exact-phrase hit into a false miss
        judge_target = max(1, len(queries))
        if hybrid_used:
            selected = _select(
                [hit for hit in hits if hit.get("lane") != "semantic"],
                judge_target, len(queries), family_diverse=family_diverse)
            selected = selected + _select(
                [hit for hit in hits if hit.get("lane") == "semantic"],
                judge_target, len(queries), family_diverse=family_diverse)
        else:
            selected = _select(hits, judge_target, len(queries),
                               family_diverse=family_diverse)
    else:
        selected = _select(hits, target, len(queries),
                           family_diverse=family_diverse)
        if hybrid_used:
            selected = _reserve_semantic_hits(
                selected, hits, target, len(queries),
                family_diverse=family_diverse)
    hybrid_visible = any(hit.get("lane") == "semantic" for hit in selected)
    if hybrid_visible:
        semantic_meta["hybrid"] = True
        if not args.json and not args.probe:
            common.log(
                "semantic-only candidates added "
                "(UNVERIFIED: cosine ranks similarity, not correctness; "
                "topic overlap is a miss; --lexical disables)")
    elif (hybrid_used and requested_mode == "keyword"
          and weak_neighbors_command is None):
        # meaning rows lost the merge, but the lane DID search: keep the
        # searched-vs-never-ran story; drop only the page-level hybrid claim
        semantic_meta = {
            "semantic_status": semantic_meta.get("semantic_status")}
        engine = "+".join(
            name for name in used_engines if not name.startswith("semantic:"))
    if not args.json and not args.probe:
        # one owned notice (search's twin path renders the same artifact), so
        # a partial accelerated lane can never present itself as complete
        coverage_notice = surface.semantic_coverage_notice(
            semantic_meta.get("semantic_coverage"),
            semantic_meta.get("semantic_accelerator_coverage"),
            suppress_trivial=True)
        if coverage_notice:
            common.log(coverage_notice)
    if not args.json:
        integrity_notice = surface.semantic_integrity_notice(
            semantic_meta.get("semantic_integrity"), suppress_trivial=True)
        if integrity_notice:
            common.log(integrity_notice)
        anchor_notice = surface.semantic_anchor_notice(
            semantic_meta.get("semantic_status"))
        if anchor_notice:
            common.log(anchor_notice)
    if meaning_lane_down and requested_mode == "keyword" and not args.probe:
        # search's twin path discloses this; recall must not go silently lexical.
        # A pack with labeled meaning rows on the page scopes the lane-down
        # story to the queries whose runs failed - one story, no contradiction.
        lane_notice = surface.semantic_keyword_only_notice(
            semantic_meta.get("semantic_status"))
        if hybrid_visible and meaning_down_queries:
            down = " · ".join(console.terminal_safe(q)[:60]
                              for q in meaning_down_queries)
            common.log(f"{lane_notice} for: {down}")
        else:
            common.log(lane_notice)

    if args.probe:
        probe_verdict = None
        if (requested_mode == "keyword" and not args.lexical
                and direct_hit is None and search_queries
                and meaning_lanes_run == len(search_queries)
                and not tools_excluded
                and not any((args.chat, args.agent, args.project, args.model,
                             args.since, args.until, args.who, args.no_who))):
            probe_verdict = surface.miss_verdict(
                indexd_runtime.freshness_story(),
                meaning_served=not meaning_lane_down,
                meaning_coverage=semantic_meta.get("semantic_coverage"),
                meaning_accelerator=semantic_meta.get(
                    "semantic_accelerator_coverage"),
                sessions=(common.index_summary() or {}).get("sessions"))
        _note_self_exclusion()
        rc = _probe(queries, selected, engine, exact_probe_sessions, budget,
                    meaning_down=meaning_lane_down,
                    tools_excluded=tools_excluded,
                    coverage=(search.filter_coverage(args) if not selected
                              else surface.filter_coverage_disclosure(
                                  [], checked=True)),
                    verdict=probe_verdict, said_once=said_once,
                    weak_neighbors_command=weak_neighbors_command)
        _note_freshness()
        if rc == 0:
            return 0
        if (semantic_unavailable or meaning_lane_down
                or search._semantic_result_incomplete(semantic_meta)):
            return 2
        return surface.grep_absence_exit(
            exact=not tools_excluded,
            freshness=indexd_runtime.freshness_story())

    if args.json and "semantic_status" not in semantic_meta:
        # law 5 for machines: "searched, empty" and "never ran" must not collapse
        semantic_meta["semantic_status"] = {
            "state": "not-run",
            "reason": ("handle-lookup" if direct_hit is not None else
                       "--lexical" if args.lexical else
                       "tool-lane" if args.who_filter == "tool" else
                       # the setting suppresses the lane regardless of query
                       # shape - name it before the shape fallthrough
                       "embeddings-off"
                       if common.setting("embeddings") == "off" else
                       "runtime-not-installed"
                       if not search._semantic_runtime_installed() else
                       "no-meaning-room" if not meaning_room else
                       "query-not-language-shaped")}

    session_index = common.indexed_session_prefix_candidates(
        hit.get("session") for hit in selected)

    windows_started = time.perf_counter()
    if not lineage_windows:
        raw_windows = _windows(selected, max(0, args.context))
    else:
        raw_windows = _windows(
            selected, max(0, args.context), cached=lineage_windows)
    pairs = _trim_self_windows(raw_windows)
    common.lap("windows")
    recall_timing = {
        "windows": round((time.perf_counter() - windows_started) * 1000.0, 3),
    }

    if direct_hit is not None and pairs and pairs[0][1]["center"] != direct_hit["turn"]:
        # @handles are stored references agents replay: serving the nearest real
        # turn as if it matched would silently rewrite saved content (around's rule)
        w = pairs[0][1]
        common.log(f"result handle turn {direct_hit['turn']} is out of range "
                   f"(session has turns {w['first_turn']}-{w['last_turn']}) - "
                   f"{surface.stale_handle_recovery(common.cli_name())}")
        _json_error(
            "stale-result-handle", engine=engine or "handle",
            policy=self_policy,
            semantic_coverage=semantic_meta.get("semantic_coverage"))
        _note_freshness()
        return 2

    if not pairs:
        # a legal filter over a dimension the index holds nothing for answers
        # zero for a reason the tool already has; the miss names it
        coverage = search.filter_coverage(args)
        if timing_enabled:
            recall_timing["total_to_windows"] = round(
                (time.perf_counter() - recall_started) * 1000.0, 3)
            common.timing_trace("recall timing", recall_timing)
        if args.json:
            obj = {"query": queries[0] if one else queries,
                   "engine": engine, "hits": []}
            served = surface.around_service_disclosure(handle_notes)
            if served is not None:
                obj["served"] = served
            if hits_floored:
                obj["hits_floored"] = hits_floored
            obj.update({k: v for k, v in semantic_meta.items() if v is not None})
            obj.update(_machine_fields(
                self_policy, excluded_hits=self_excluded_count,
                semantic_coverage=semantic_meta.get("semantic_coverage"),
                filter_coverage=coverage))
            if args.semantic and semantic_unavailable:
                obj["error"] = {"code": "semantic-unavailable"}
            elif tools_excluded:
                obj["error"] = {
                    "code": surface.TOOLS_PENDING_ERROR_CODE,
                    "reason": surface.SCAN_TOOLS_PENDING_LINE,
                }
            if tools_excluded:
                obj["tools_excluded"] = {
                    "reason": surface.TOOLS_PENDING_ERROR_CODE}
            _write_payload(
                _fit_json_payload(
                    obj, _content_budget(budget), session_index),
                budget)
            _note_freshness()
        else:
            if args.semantic and semantic_unavailable:
                common.log(surface.semantic_unavailable_notice(
                    semantic_meta.get("semantic_status")))
            elif tools_excluded:
                common.log(surface.tools_pending_zero_line("keyword"))
            elif weak_neighbors_command is not None:
                common.log(
                    "no semantic-only candidate cleared the auto-use "
                    "threshold; inspect below-auto-threshold candidates: "
                    f"{weak_neighbors_command}")
            else:
                summary = common.index_summary()
                sessions = summary.get("sessions") if summary else None
                common.log("no hits." if not sessions else
                           "no hits across "
                           f"{surface.count_noun(sessions, 'indexed session')}.")
                for empty_dimension in coverage["empty_dimensions"]:
                    common.log(surface.empty_dimension_line(empty_dimension))
                _note_self_exclusion()
                _note_freshness()
        if (semantic_unavailable or meaning_lane_down
                or search._semantic_result_incomplete(semantic_meta)):
            return 2
        exact = not selected and not tools_excluded
        return surface.grep_absence_exit(
            exact=exact,
            freshness=indexd_runtime.freshness_story())

    expand_started = time.perf_counter()
    pairs = _trim_self_windows(
        _expand(pairs, queries, budget, max(0, args.context), self_policy))
    common.lap("expand")
    if timing_enabled:
        recall_timing["expand"] = round(
            (time.perf_counter() - expand_started) * 1000.0, 3)
        recall_timing["total_to_expand"] = round(
            (time.perf_counter() - recall_started) * 1000.0, 3)
        common.timing_trace("recall timing", recall_timing)
    # Evict whole blocks before equal splitting degrades the entire page into stubs.
    room = max(1, budget // _JUDGEABLE_BLOCK) if budget else len(pairs)
    share = budget // min(len(pairs), room) if budget else 0
    # truncating while budget goes unused is how evidence gets clipped for nothing:
    # when the whole pack's raw text fits, caps don't exist (events stay share-bound)
    raw = sum(_utf8_size(t["text"] or "") + _utf8_size(t["reply"] or "")
              for _, w in pairs for t in w["turns"])
    fits = not budget or raw <= int(budget * 0.85)

    # the hit's own query anchors its center-turn slice: a cap must not elide
    # the very match the window exists to show (law 6)
    def _hit_anchors(h: dict) -> list[re.Pattern]:
        if direct_hit is not None:  # a handle is an address, not a match
            return []
        i = int(h.get("_recall_query", 0) or 0)
        return _anchor_patterns(queries[i if 0 <= i < len(queries) else 0])

    if args.json:
        out_hits = []
        required_tool_rows = []
        for h, w in pairs:
            cap = 0 if fits else _msg_cap(share, w, 260)
            anchors = [] if fits else _hit_anchors(h)
            target = compact.encode_session_target(
                w["session"], session_index=session_index)
            ev: dict[int, list[dict]] = {}
            for e in w["events"]:
                ev.setdefault(e["turn"], []).append(e)
            rows, size, cut = [], 0, 0
            required_tools: set[tuple] = set()
            for t in w["turns"]:
                # the matched turn is why this window exists: it keeps a privileged slice of
                # the share, so widening can only ever truncate CONTEXT turns, never the hit
                tcap = (0 if fits else
                        max(cap, min(2400, share)) if t["turn"] == w["center"] else cap)
                expand = _command(
                    "agrep", "around", target, t["turn"], "-C", 0)
                for who, text in ((t["who"], t["text"]), ("agent", t["reply"])):
                    if not text:
                        continue
                    capped, omitted = (
                        _anchored_cap(text, tcap, expand, anchors)
                        if anchors and t["turn"] == w["center"]
                        else _cap(text, tcap, expand))
                    row = {"kind": "msg", "session": w["session"],
                           "project": w["project"], "turn": t["turn"],
                           "who": who, "ts": t["ts"], "text": capped,
                           "omitted_chars": omitted}
                    if who == "agent":
                        row["reply_chars"] = int(t.get("reply_chars", len(text)))
                        row["reply_truncated"] = bool(t.get("reply_truncated"))
                    rows.append(row)
                    size += _utf8_size(capped) + 140
                # events ride the same share; a tool-heavy turn can carry hundreds
                for e in ev.get(t["turn"], []):
                    tool_match = _selected_tool_match(
                        e, w["session"],
                        h.get("_event_identity")
                        if h.get("who") == "tool" else None,
                        h.get("_match_span"))
                    selected_event = tool_match.selected
                    if (h.get("who") == "tool"
                            and h.get("_event_identity") is not None
                            and not selected_event):
                        cut += 1
                        continue
                    if share and size >= share and not selected_event:
                        cut += 1
                        continue
                    inp = e["input"] or ""
                    if len(inp) > 120:
                        inp = inp[:120] + "…"
                    row = {"kind": e["kind"], "session": w["session"],
                           "project": w["project"], "turn": e["turn"], "ts": e["ts"],
                           "name": e["name"], "input": inp, "ok": e["ok"],
                           "input_chars": e.get("input_chars", len(e["input"] or "")),
                           "output_chars": e["output_chars"],
                           # recorded source bytes only; never recomputed here
                           "output_bytes": e.get("output_bytes"),
                           "input_truncated": bool(e.get("input_truncated")),
                           "output_truncated": bool(e.get("output_truncated"))}
                    if e.get("payload_bounds") is not None:
                        row["payload_bounds"] = e["payload_bounds"]
                    if selected_event and tool_match.preview:
                        row["match_preview"] = tool_match.preview
                    if selected_event:
                        required_tools.add(_json_tool_row_key(row))
                    rows.append(row)
                    size += _utf8_size(inp) + 160
            if cut:
                rows.append({"kind": "events_omitted", "session": w["session"],
                             "project": w["project"], "n": cut,
                             "expand": _window_expand_command(w, target)})
            out_hits.append({"session": w["session"], "turn": w["center"],
                             "ts": h.get("ts"), "agent": w["agent"],
                             "project": w["project"], "score": h.get("score"),
                             "sem_score": h.get("sem_score"),
                             "score_kind": h.get("score_kind"),
                             **({"lane": h["lane"]} if h.get("lane") else {}),
                             "matched": h.get("matched", "phrase"), "window": rows})
            required_tool_rows.append(required_tools)
        obj = {"query": queries[0] if one else queries, "engine": engine, "hits": out_hits}
        served = surface.around_service_disclosure(handle_notes)
        if served is not None:
            # a pipe must know an unverified or rescued resolve before it
            # reads the rows - the same record around --json emits
            obj["served"] = served
        if hits_floored:
            obj["hits_floored"] = hits_floored
        obj.update({k: v for k, v in semantic_meta.items() if v is not None})
        obj.update(_machine_fields(
            self_policy, excluded_hits=self_excluded_count,
            semantic_coverage=semantic_meta.get("semantic_coverage"),
            # a handle run that skipped supplied filters must not claim they
            # were checked; the reason is the same disclosure a human reads
            filter_coverage=surface.filter_coverage_disclosure(
                [], checked=handle_filter_note is None,
                reason=handle_filter_note)))
        if tools_excluded:
            obj["tools_excluded"] = {
                "reason": surface.TOOLS_PENDING_ERROR_CODE}
        if fell_back:
            obj["note"] = "no exact phrase match; showing hits containing all terms"
        _write_payload(
            _fit_json_payload(obj, _content_budget(budget), session_index,
                              hit_anchors=[_hit_anchors(h) for h, _ in pairs],
                              required_tool_rows=required_tool_rows),
            budget)
        _note_freshness()
        return 0

    if tools_excluded:
        common.log(surface.SCAN_TOOLS_PENDING_LINE)

    # ~meta demotion speaks through the row markers alone (law 7). Display
    # lane only (--json keeps every row): weak blocks past the top hit
    # collapse to a summary line + handle; the strongest draws a double share.
    collapse = [i > 0 and _weak_block(h) for i, (h, _) in enumerate(pairs)]
    full_count = collapse.count(False)
    content_budget = (max(0, budget - _WEAK_LINE_COST * collapse.count(True))
                      if budget else 0)
    room = max(1, content_budget // _JUDGEABLE_BLOCK) if budget else full_count
    denom = min(full_count, room) + (1 if full_count > 1 else 0)
    share = max(80, content_budget // max(1, denom)) if budget else 0
    raw = sum(_utf8_size(t["text"] or "") + _utf8_size(t["reply"] or "")
              for (_h, w), skip in zip(pairs, collapse) if not skip
              for t in w["turns"])
    raw += sum(_event_budget_cost(w, h)
               for (h, w), skip in zip(pairs, collapse) if not skip)
    fits = not budget or raw <= int(content_budget * 0.85)

    used = 0
    shown_full = 0
    blocks: list[str] = []
    block_cmds: list[str] = []  # each block's handle, for the final cap marker
    trailer_cmds: list[str] = []
    trailer = ""
    for i, (h, w) in enumerate(pairs):
        if budget and (used >= budget
                       or (not collapse[i] and shown_full >= room)):
            rest = pairs[i:]
            # Handles preserve actionability while spending the exhausted budget on evidence.
            trailer_cmds = [_window_result_handle(
                w2, h2, session_index)
                for h2, w2 in rest[:3]]
            cmds = " ".join(trailer_cmds)
            # kept out of blocks: _fit_text_payload evicts content before this
            trailer = f"[+{len(rest)} hit(s) over budget - {cmds}]"
            break
        target = compact.encode_session_target(
            w["session"], session_index=session_index)
        # the window holds the text the block renders, so the block's own
        # handle can bind to it - a citation the reader copies is the one
        # that most needs to be verifiable
        result_handle = _window_result_handle(w, h, session_index)
        sem_score = h.get("sem_score")
        score_label = (f"{h.get('score_kind') or 'cosine'} {float(sem_score):.4f}"
                       if sem_score is not None
                       else f"score {h.get('score', 0)}")
        # "weak meaning match" is the taught vocabulary (the block tells agents
        # to weigh a weak semantic match accordingly); it already carries the
        # below-threshold verdict, so no separate threshold flag renders.
        meaning_label = ("weak meaning match" if sem_score is not None
                         and search._semantic_row_weak(h)
                         else "meaning match" if sem_score is not None else "")
        threshold_label = ""
        project_label = f"project={console.terminal_safe(search._proj(w['project']))}"
        # @session:turn is the universal handle; colliding sessions need longer prefixes.
        rendered_handle = console.terminal_safe(result_handle)
        identity = (
            rendered_handle, f"pull: agrep around {rendered_handle}",
            console.terminal_safe(w["agent"]), project_label,
            _ts_label(h.get("ts") or 0),
        )
        head_fields = ((*identity, meaning_label, score_label, threshold_label,
                        _provenance_marks(h)) if sem_score is not None else
                       (*identity, score_label, _provenance_marks(h)))
        head = " · ".join(x for x in head_fields if x)
        if collapse[i]:
            center = _center_text(w, h) or next(
                (t["text"] or t["reply"] for t in w["turns"]
                 if t["text"] or t["reply"]), "")
            line = _weak_line(head, center, _hit_anchors(h),
                              fallback=str(h.get("snippet") or ""))
            blocks.append(line)
            block_cmds.append(result_handle)
            used += _utf8_size(line) + 1
            continue
        block_share = (share * 2 if shown_full == 0 and full_count > 1
                       else share)
        cap = 0 if fits else _msg_cap(block_share, w)
        anchors = [] if fits else _hit_anchors(h)
        head_line = f"── {head}"
        ev: dict[int, list[tuple[
            dict, bool, tuple[int, int] | None, str | None,
        ]]] = {}
        for e in w["events"]:
            tool_match = _selected_tool_match(
                e, w["session"],
                h.get("_event_identity") if h.get("who") == "tool" else None,
                h.get("_match_span"))
            ev.setdefault(e["turn"], []).append(
                (e, tool_match.selected, tool_match.output_span,
                 tool_match.preview))
        center_rows = sum(
            1 for t in w["turns"] if t["turn"] == w["center"]
            for text in (t["text"], t["reply"]) if text)
        context_rows = sum(
            1 for t in w["turns"] if t["turn"] != w["center"]
            for text in (t["text"], t["reply"]) if text)
        center_cap = cap
        if block_share and not fits and center_rows:
            context_reserve = context_rows * (cap + 80)
            tool_reserve = 360 if any(
                selected for events in ev.values()
                for _e, selected, _span, _preview in events) else 0
            center_room = max(
                80 * center_rows,
                block_share - _utf8_size(head_line) - context_reserve - tool_reserve)
            center_cap = max(80, min(2400, center_room // center_rows - 80))
        records: list[dict] = []
        order = 0
        hidden_tools = 0
        for t in w["turns"]:
            tcap = center_cap if t["turn"] == w["center"] else cap
            expand = _command(
                "agrep", "around", target, t["turn"], "-C", 0)
            # every capped text row keeps its own match window, not just the
            # center turn - context rows exist because they bear on the query
            if t["text"]:
                body, _ = (_anchored_cap(" ".join(t["text"].split()), tcap,
                                         expand, anchors) if anchors
                           else _cap(" ".join(t["text"].split()), tcap, expand))
                body = console.terminal_safe(body)
                records.append({
                    "line": f"  {t['turn']:>4} "
                            f"{console.terminal_safe(t['who'])}: {body}",
                    "required": t["turn"] == w["center"], "kind": "context",
                    "drop_key": (2, -abs(t["turn"] - w["center"]), order),
                })
                order += 1
            # Ambient tools stay forensic-only; tool hits keep exact evidence.
            for e, selected_event, output_span, match_preview in ev.get(
                    t["turn"], []):
                if not selected_event:
                    hidden_tools += 1
                    continue
                line = "     " + _tool_line(
                    e, False, 0, match_span=output_span,
                    match_preview=match_preview)
                records.append({
                    "line": console.terminal_safe(line),
                    "required": True, "kind": "tool",
                    "drop_key": (
                        0 if e.get("ok") is True else 1,
                        -abs(t["turn"] - w["center"]), order),
                })
                order += 1
            if t["reply"]:
                body, _ = (_anchored_cap(" ".join(t["reply"].split()), tcap,
                                         expand, anchors) if anchors
                           else _cap(" ".join(t["reply"].split()), tcap, expand))
                body = console.terminal_safe(body)
                body += _reply_loss_marker(t)
                records.append({
                    "line": f"  {t['turn']:>4} agent: {body}",
                    "required": t["turn"] == w["center"], "kind": "context",
                    "drop_key": (2, -abs(t["turn"] - w["center"]), order),
                })
                order += 1
        block, _ = _fit_recall_records(
            head_line, records,
            max(80, block_share - 4) if block_share and not fits else 0,
            _window_expand_command(w, target),
            hidden_tools=hidden_tools)
        blocks.append(block)
        block_cmds.append(result_handle)
        used += _utf8_size(block) + 1
        shown_full += 1

    ends, off = [], 0
    for block, cmd in zip(blocks, block_cmds):
        off += len(block)
        ends.append((off, cmd))
        off += 2
    payload = _render_text_payload(
        "\n\n".join(blocks), budget, ends, trailer,
        color=common.color_enabled(sys.stdout, args.color))
    common.lap("render")
    _write_payload(payload, budget)

    _note_self_exclusion()
    _note_freshness()
    return 0


def main(argv: list[str] | None = None, prog: str = "recall", *,
         auto_semantic_timeout_s: float | None = None) -> int:
    try:
        return _main(
            argv, prog=prog,
            auto_semantic_timeout_s=auto_semantic_timeout_s)
    except _RecallQueryAbort:
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
