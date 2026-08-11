"""Token-bounded compact result pages and frozen continuations."""

from __future__ import annotations

import base64
import bisect
import hashlib
import json
import math
import os
import re
import secrets
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

import common
import surface_policy as surface
from session_context import SessionPrefixIndex, session_prefix_index


SNAPSHOT_VERSION = 6
DEFAULT_TTL_S = 300
MAX_TTL_S = 900
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
MAX_FROZEN_HITS = 40
MIN_PAGE_HITS = 4
MAX_PAGE_HITS = 16
FAMILY_PAGE_CAP = 3
TERMS_RESERVE = 2
DEFAULT_BYTE_BUDGET = 3584
SCORE_DROP_RATIO = 0.35
_HANDLE_RE = re.compile(r"m\.([A-Za-z0-9_-]{8})\Z")
# the optional `.hex4` tail is the content digest (see docs/HANDLE_IDENTITY.md):
# a handle is an address plus a claim about what stands there
_RESULT_RE = re.compile(
    r"@?([A-Za-z0-9][A-Za-z0-9._-]{2,127}):(\d{1,19})"
    r"(?:\.([0-9a-f]{4})(?:~([0-9a-f]{24}):(\d{1,7})-(\d{1,7}))?)?\Z")
_OPAQUE_RESULT_RE = re.compile(
    r"@~([A-Za-z0-9_-]{2,1024}):(\d{1,19})"
    r"(?:\.([0-9a-f]{4})(?:~([0-9a-f]{24}):(\d{1,7})-(\d{1,7}))?)?\Z")
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_EVENT_ID_RE = re.compile(r"[0-9a-f]{24}\Z")
_MAX_RESULT_TURN = (1 << 63) - 1
_MAX_MATCH_OFFSET = 4 * 1024 * 1024
SESSION_ID_MAX_BYTES = 4096
_DEEPER_VALUE_OPTIONS = (
    "--agent=", "--project=", "--exclude-project=", "--model=",
    "--who=", "--no-who=", "--chat=",
    "--since=", "--until=", "--sort=", "--color=",
)
_DEEPER_MODE_FLAGS = {"-s", "--hybrid", "-E", "-w", "--lexical"}
_DEEPER_BOOL_FLAGS = {
    "--soft", "--self", "--no-self", "--no-meta", "--all-side-chats",
    "--strict-semantic", "--no-auto", "--classic",
}


class CompactError(ValueError):
    """A compact page or handle cannot be served safely."""


class SnapshotExpired(CompactError):
    """The continuation is no longer valid."""


@dataclass(frozen=True)
class CompactPage:
    records: tuple[dict, ...]
    more: bool
    handle: str | None
    stopped_by: str
    known_min: int
    corpus_more: bool = False
    query: str = ""
    exact_total: int | None = None
    deeper_argv: tuple[str, ...] | None = None
    more_unknown: bool = False
    shown_before: int = 0
    # an explicit -n: diversity caps waived up to this many rows; a page that
    # still stops short owes the reader stopped_by as its reason
    requested_rows: int | None = None
    # a floor OF THE RESULT SET, counted or fully scored. known_min is only
    # what THIS page proves, so without it a header would state the page's own
    # row count as if it were the corpus.
    total_floor: int | None = None
    # nothing counted the result set and the lane stopped early: the page knows
    # its own rows and nothing about the total, and says exactly that.
    total_uncounted: bool = False
    # the query was narrowed to one chat, so this page spreads over nothing
    # and the diversity caps stay off it (their own pages and continuations).
    chat_scoped: bool = False

    @property
    def lines(self) -> list[str]:
        return [record["line"] for record in self.records]

    @property
    def floor(self) -> int:
        """The largest number of matching rows this page can prove exist."""
        return max(int(self.known_min), int(self.total_floor or 0))


@dataclass(frozen=True)
class _SnapshotData:
    records: tuple[dict, ...]
    generation: str
    corpus_more: bool
    query: str
    exact_total: int | None
    deeper_argv: tuple[str, ...] | None
    more_unknown: bool
    shown_before: int
    # total ranked rows served across the whole chain, accumulating across
    # --deeper hops (shown_before resets on a fresh deeper replay).
    deeper_covered: int = 0
    total_floor: int | None = None
    total_uncounted: bool = False
    chat_scoped: bool = False


def profile_enabled(*, classic: bool = False, json_mode: bool = False,
                    environ: Mapping[str, str] | None = None) -> bool:
    """Use compact output for agents unless an explicit profile overrides it."""
    env = os.environ if environ is None else environ
    if classic or json_mode:
        return False
    explicit = env.get("AGREP_PROFILE", "").strip().lower()
    if explicit == "compact":
        return True
    if explicit == "classic":
        return False
    return common.in_agent_context(environ=env)


def generation_fingerprint(generation: object) -> str:
    """Return a stable token for a transcript generation identity."""
    try:
        raw = json.dumps(generation, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CompactError("generation is not serializable") from exc
    return hashlib.sha256(raw).hexdigest()


def _family_value(hit: dict, family_key: Callable[[dict], str] | None) -> str:
    if family_key is not None:
        value = family_key(hit)
    else:
        value = hit.get("family") or hit.get("family_root") or hit.get("session") or ""
    return str(value)


def diversify_hits(hits: Sequence[dict],
                   family_key: Callable[[dict], str] | None = None) -> list[dict]:
    """Diversify within phrase, meaning, and all-terms quality lanes."""
    def one_lane(rows: list[dict]) -> list[dict]:
        head, tail, seen = [], [], set()
        for hit in rows:
            family = hit.get("family_root") or ""
            if family and family not in seen:
                seen.add(family)
                head.append(hit)
            else:
                tail.append(hit)
        return head + tail

    # Stamp the family so page composition and frozen continuations group the
    # same way: continuation pages cannot re-run family_key.
    frozen = [{**hit, "family_root": _family_value(hit, family_key)}
              for hit in list(hits)[:MAX_FROZEN_HITS]]
    phrase = [hit for hit in frozen if _lane(hit) == "phrase"]
    meaning = [hit for hit in frozen if _lane(hit) == "semantic"]
    all_terms = [hit for hit in frozen if _lane(hit) == "all"]
    phrase, meaning, all_terms = map(one_lane, (phrase, meaning, all_terms))
    if meaning:
        aligned = [hit for hit in phrase
                   if hit.get("_boundary_class") != "interior"]
        interior = [hit for hit in phrase
                    if hit.get("_boundary_class") == "interior"]
        lead = min(3, len(aligned))
        ordered = aligned[:lead] + meaning + aligned[lead:] + interior + all_terms
    else:
        ordered = phrase + all_terms
    # search demoted ~meta rows upstream; family promotion must not undo it.
    return ([hit for hit in ordered if not hit.get("_meta_row")]
            + [hit for hit in ordered if hit.get("_meta_row")])


def visible_bytes(text: str) -> int:
    """Measure rendered UTF-8 while excluding terminal escape sequences."""
    return len(_ANSI_RE.sub("", text).encode("utf-8"))


def freeze_records(hits: Iterable[dict], render_row: Callable[[dict], str]) -> list[dict]:
    """Freeze both result identity and presentation for later pages."""
    records = []
    for hit in hits:
        line = common.one_line(render_row(hit))
        frozen_hit = {key: value for key, value in hit.items()
                      if key != "_search_text"}
        records.append({"hit": frozen_hit, "line": line})
    return records


def _truncate_visible_line(line: str, byte_cap: int) -> str:
    """Shrink an over-budget row without splitting UTF-8 or hiding its identity."""
    cap = max(0, int(byte_cap))
    plain = _ANSI_RE.sub("", line)
    if len(plain.encode("utf-8")) <= cap:
        return line
    if cap <= 0:
        return ""
    marker = "…" if cap >= 3 else "." * cap
    room = cap - len(marker.encode("utf-8"))
    out: list[str] = []
    used = 0
    for char in plain:
        width = len(char.encode("utf-8"))
        if used + width > room:
            break
        out.append(char)
        used += width
    return "".join(out).rstrip() + marker


def _fit_page_records(records: Sequence[dict], byte_budget: int) -> tuple[list[dict], bool]:
    """Hard-cap mandatory rows by sharing the remaining visible-byte allowance."""
    chosen = list(records)
    budget = max(1, int(byte_budget))
    sizes = [visible_bytes(record["line"]) for record in chosen]
    if sum(sizes) + len(chosen) <= budget:
        return chosen, False

    available = max(0, budget - len(chosen))
    caps = [0] * len(chosen)
    pending = set(range(len(chosen)))
    while pending:
        share, extra = divmod(available, len(pending))
        small = [index for index in pending if sizes[index] <= share]
        if not small:
            for offset, index in enumerate(sorted(pending)):
                caps[index] = share + (1 if offset < extra else 0)
            break
        for index in small:
            caps[index] = sizes[index]
            available -= sizes[index]
            pending.remove(index)

    fitted = []
    for record, cap in zip(chosen, caps):
        fitted.append({**record, "line": _truncate_visible_line(record["line"], cap)})
    return fitted, True


def _lane(hit: dict) -> str:
    if hit.get("sem_score") is not None or hit.get("lane") == "semantic":
        return "semantic"
    return "all" if hit.get("matched") in ("all-terms", "content-terms") else "phrase"


def _score(hit: dict) -> float | None:
    try:
        raw = (hit.get("sem_score") if hit.get("sem_score") is not None
               else hit.get("score"))
        score = float(raw)
    except (TypeError, ValueError):
        return None
    return score if score >= 0 else None


def _page_family(hit: dict) -> str:
    return str(hit.get("family_root") or hit.get("family") or hit.get("session") or "")


def _event_identity(hit: Mapping[str, object]) -> str | None:
    """Return a strong bounded identity for a tool row."""
    value = hit.get("_event_identity")
    if isinstance(value, str) and _EVENT_ID_RE.fullmatch(value):
        return value
    if hit.get("who") != "tool":
        return None
    text = common.tool_search_record(dict(hit))[0]
    if not text:
        snippet = hit.get("snippet")
        text = snippet if isinstance(snippet, str) else ""
    return common.tool_event_identity(
        hit.get("session"), hit.get("turn"), hit.get("ts"), text)


def _turn_key(hit: dict) -> tuple[object, ...] | None:
    session = str(hit.get("session") or "")
    turn = hit.get("turn")
    if not session or turn is None:
        return None
    if hit.get("who") == "tool":
        identity = _event_identity(hit)
        if identity is not None:
            return session, turn, identity
        return session, turn, hit.get("content_digest"), hit.get("snippet")
    return session, turn


def _compose_page(records: Sequence[dict], byte_budget: int, *,
                  terms_reserve: int = 0,
                  requested_rows: int | None = None,
                  first_page_rows: int | None = None,
                  tool_rescue_page: bool = False,
                  chat_scoped: bool = False) -> tuple[list[int], str]:
    """Pick page rows by index, deferring family pileups behind the continuation.

    ``requested_rows`` is an explicit -n: diversity (family cap, lane and
    score drops) is a default, not a ceiling, so the caps are waived up to the
    request. The byte budget still binds - a shorter page names it.

    ``chat_scoped`` is a query already narrowed to one conversation. Every cap
    here spreads a page across the corpus and that page has nowhere to spread,
    so the caps could only withhold the rows the caller asked for. Rows and
    bytes still bound the page."""
    budget = max(1, int(byte_budget))
    if requested_rows is not None:
        row_cap = max(1, min(int(requested_rows), MAX_FROZEN_HITS))
    elif first_page_rows is not None:
        row_cap = max(1, min(int(first_page_rows), MAX_PAGE_HITS))
    else:
        row_cap = MAX_PAGE_HITS
    spread = not chat_scoped
    diversify = spread and requested_rows is None
    chosen: list[int] = []
    used = 0
    leader_lane = None
    leader_score = None
    family_rows: dict[str, int] = {}
    turns_seen: set[tuple[object, ...]] = set()
    deferred = False
    reason = "exhausted"
    for index, record in enumerate(records):
        if len(chosen) >= row_cap:
            reason = "row-limit"
            break
        hit = record["hit"]
        rescue = hit.get("_tool_rescue") is True
        if tool_rescue_page and hit.get("who") == "tool" and not rescue:
            deferred = True
            continue
        family = _page_family(hit)
        turn = _turn_key(hit)
        if spread and not rescue and turn is not None and turn in turns_seen:
            deferred = True
            continue
        if (diversify and not rescue and family
                and family_rows.get(family, 0) >= FAMILY_PAGE_CAP):
            deferred = True
            continue
        line_bytes = visible_bytes(record["line"]) + 1
        lane = _lane(hit)
        score = _score(hit)
        if not chosen:
            leader_lane, leader_score = lane, score
        if len(chosen) >= MIN_PAGE_HITS:
            if used + line_bytes > budget:
                reason = "byte-budget"
                break
            # Deferrals freed page space: let cross-family lower-lane rows
            # fill it instead of shrinking the page.
            if (diversify and leader_lane == "phrase"
                    and lane == "all" and not deferred and not rescue):
                reason = "lane-drop"
                break
            if (diversify and lane == leader_lane
                    and not rescue
                    and score is not None and leader_score is not None):
                if score < leader_score * SCORE_DROP_RATIO:
                    reason = "score-drop"
                    break
        chosen.append(index)
        used += line_bytes
        if family:
            family_rows[family] = family_rows.get(family, 0) + 1
        if turn is not None:
            turns_seen.add(turn)
    if reason == "exhausted" and deferred:
        reason = "diversity"
    if terms_reserve > 0:
        chosen = _reserve_terms_rows(records, chosen, used, family_rows,
                                     turns_seen, budget, terms_reserve,
                                     spread=spread,
                                     tool_rescue_page=tool_rescue_page)
    return chosen, reason


def _reserve_terms_rows(records: Sequence[dict], chosen: list[int], used: int,
                        family_rows: dict[str, int],
                        turns_seen: set[tuple[object, ...]], budget: int,
                        reserve: int, *, spread: bool = True,
                        tool_rescue_page: bool = False) -> list[int]:
    # Echo sessions quoting the query regrow the phrase lane forever; the
    # direct response must still show the top scattered-terms rows.
    need = reserve - sum(1 for index in chosen
                         if _lane(records[index]["hit"]) == "all")
    picked = set(chosen)
    for index, record in enumerate(records):
        if need <= 0:
            break
        hit = record["hit"]
        if index in picked or _lane(hit) != "all":
            continue
        if (tool_rescue_page and hit.get("who") == "tool"
                and hit.get("_tool_rescue") is not True):
            continue
        family = _page_family(hit)
        turn = _turn_key(hit)
        if spread and ((turn is not None and turn in turns_seen)
                       or (family
                           and family_rows.get(family, 0) >= FAMILY_PAGE_CAP)):
            continue
        chosen.append(index)
        picked.add(index)
        used += visible_bytes(record["line"]) + 1
        if family:
            family_rows[family] = family_rows.get(family, 0) + 1
        if turn is not None:
            turns_seen.add(turn)
        need -= 1
    while len(chosen) > MAX_PAGE_HITS or (used > budget
                                          and len(chosen) > MIN_PAGE_HITS):
        # Displaced phrase rows defer to the continuation like cap overflow.
        victim = next((position for position in range(len(chosen) - 1, -1, -1)
                       if _lane(records[chosen[position]]["hit"]) == "phrase"),
                      None)
        if victim is None:
            break
        used -= visible_bytes(records[chosen[victim]]["line"]) + 1
        del chosen[victim]
    chosen.sort()
    return chosen


def select_page(records: Sequence[dict], *,
                byte_budget: int = DEFAULT_BYTE_BUDGET) -> tuple[list[dict], str]:
    """Select one adaptive page without hiding a small set of valid hits."""
    indices, reason = _compose_page(records, byte_budget)
    return [records[index] for index in indices], reason


def _snapshot_dir(data_dir: Path | None = None) -> Path:
    root = (data_dir or common.DATA_DIR) / ".compact-snapshots"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        info = root.lstat()
    except OSError as exc:
        raise CompactError("snapshot directory is unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise CompactError("snapshot directory is not private storage")
    if os.name != "nt":
        os.chmod(root, 0o700)
    return root


def _snapshot_path(handle: str, data_dir: Path | None = None) -> Path:
    match = _HANDLE_RE.fullmatch(handle)
    if match is None:
        raise CompactError("invalid continuation handle")
    return _snapshot_dir(data_dir) / f"{match.group(1)}.json"


def _cleanup(root: Path, now: float) -> None:
    for pattern in ("*.json", ".new-*"):
        for index, path in enumerate(root.glob(pattern)):
            if index >= 64:
                break
            try:
                info = path.lstat()
                if (stat.S_ISREG(info.st_mode)
                        and now - info.st_mtime > MAX_TTL_S):
                    path.unlink(missing_ok=True)
            except OSError:
                pass


def _unlink_best_effort(path: Path) -> None:
    # A short retry spans transient Windows AV locks without charging healthy
    # searches; stale remnants are reaped by later snapshot cleanup.
    for delay in (0.0, 0.005, 0.01):
        if delay:
            time.sleep(delay)
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except OSError:
            pass


def _validate_records(value: object) -> list[dict]:
    if not isinstance(value, list) or len(value) > MAX_FROZEN_HITS:
        raise CompactError("invalid frozen result list")
    records = []
    for record in value:
        if not isinstance(record, dict) or set(record) != {"hit", "line"}:
            raise CompactError("invalid frozen result")
        if not isinstance(record["hit"], dict) or not isinstance(record["line"], str):
            raise CompactError("invalid frozen result fields")
        if "\n" in record["line"] or "\r" in record["line"]:
            raise CompactError("frozen result is not one line")
        records.append(record)
    return records


def _validate_snapshot_metadata(
        query: object, exact_total: object, deeper_argv: object,
        corpus_more: bool, more_unknown: object, shown_before: object, *,
        stored: bool = False,
) -> tuple[str, int | None, tuple[str, ...] | None]:
    if not isinstance(query, str) or not query.strip():
        raise CompactError("continuation snapshot has invalid query")
    if exact_total is not None and (
            type(exact_total) is not int or exact_total < 0):
        raise CompactError("continuation snapshot has invalid exact total")
    allowed = list if stored else (list, tuple)
    if deeper_argv is None:
        argv = None
    elif not isinstance(deeper_argv, allowed):
        raise CompactError("continuation snapshot has invalid deeper command")
    else:
        argv = tuple(deeper_argv)
        if (len(argv) < 3 or any(not isinstance(arg, str) or "\0" in arg
                                 for arg in argv)
                or argv[0] != "agrep" or argv[-2:] != ("--", query)):
            raise CompactError("continuation snapshot has invalid deeper command")
        body = argv[1:-2]
        seen: set[str] = set()
        modes = 0
        index = 0
        while index < len(body):
            arg = body[index]
            if arg in _DEEPER_MODE_FLAGS:
                modes += 1
            elif arg in _DEEPER_BOOL_FLAGS:
                if arg in seen:
                    raise CompactError(
                        "continuation snapshot has invalid deeper command")
                seen.add(arg)
            elif arg == "-n" and index + 1 < len(body) and body[index + 1] == "80":
                if arg in seen:
                    raise CompactError(
                        "continuation snapshot has invalid deeper command")
                seen.add(arg)
                index += 1
            elif any(arg.startswith(prefix) for prefix in _DEEPER_VALUE_OPTIONS):
                name = arg.split("=", 1)[0]
                if name in seen:
                    raise CompactError(
                        "continuation snapshot has invalid deeper command")
                seen.add(name)
            else:
                raise CompactError(
                    "continuation snapshot has invalid deeper command")
            index += 1
        if (modes > 1 or "--classic" not in seen or "-n" not in seen
                or ({"--self", "--no-self"} <= seen)):
            raise CompactError("continuation snapshot has invalid deeper command")
    if corpus_more and argv is None:
        raise CompactError("continuation snapshot lacks a deeper command")
    if not isinstance(more_unknown, bool):
        raise CompactError("continuation snapshot has invalid uncertainty state")
    if (type(shown_before) is not int
            or not 0 <= shown_before <= MAX_FROZEN_HITS):
        raise CompactError("continuation snapshot has invalid page offset")
    return query, exact_total, argv


def _validate_total_floor(value: object, exact_total: int | None) -> int | None:
    """A measured lower bound, and never one beside an exact total: two totals
    on one page is the disagreement this field exists to end."""
    if value is None:
        return None
    if type(value) is not int or value < 0 or exact_total is not None:
        raise CompactError("continuation snapshot has invalid total floor")
    return value


def _validate_flag(value: object, name: str) -> bool:
    """A stored flag is a bool or it is damage."""
    if value is None or value is False:
        return False
    if value is not True:
        raise CompactError(f"continuation snapshot has invalid {name}")
    return True


def _validate_total_uncounted(value: object, exact_total: int | None,
                              total_floor: object) -> bool:
    """An uncounted total is the absence of a total: a page carrying one beside
    an exact total or a floor would be stating two answers at once."""
    if value is None or value is False:
        return False
    if value is not True or exact_total is not None or total_floor is not None:
        raise CompactError("continuation snapshot has invalid total basis")
    return True


def _validate_snapshot_counts(
        exact_total: int | None, corpus_more: bool, more_unknown: bool,
        shown_before: int, record_count: int,
) -> None:
    frozen_total = shown_before + record_count
    if exact_total is not None and more_unknown:
        raise CompactError(
            "continuation snapshot mixes exact and unknown totals")
    if exact_total is None:
        return
    # >= not ==: near-duplicate matches fold into one served row (disclosed
    # per-row as xN markers), so a finished chain may hold fewer distinct
    # rows than counted matches. More rows than matches stays contradictory.
    valid = (
        exact_total > frozen_total
        if corpus_more else exact_total >= frozen_total)
    if not valid:
        raise CompactError("continuation snapshot has contradictory totals")


def _save_snapshot(records: Sequence[dict], generation_token: str, ranking_version: str,
                   *, ttl_s: int, data_dir: Path | None, now: float | None,
                   corpus_more: bool, query: str, exact_total: int | None,
                   deeper_argv: Sequence[str] | None,
                   more_unknown: bool, shown_before: int,
                   deeper_covered: int = 0,
                   total_floor: int | None = None,
                   total_uncounted: bool = False,
                   chat_scoped: bool = False) -> str:
    current = time.time() if now is None else float(now)
    ttl = max(1, min(int(ttl_s), MAX_TTL_S))
    frozen = _validate_records(list(records))
    if not ranking_version:
        raise CompactError("ranking version is required")
    query, exact_total, argv = _validate_snapshot_metadata(
        query, exact_total, deeper_argv, corpus_more, more_unknown,
        shown_before)
    total_floor = _validate_total_floor(total_floor, exact_total)
    total_uncounted = _validate_total_uncounted(
        total_uncounted, exact_total, total_floor)
    if type(deeper_covered) is not int or deeper_covered < 0:
        raise CompactError("continuation snapshot has invalid covered count")
    if shown_before + len(frozen) > MAX_FROZEN_HITS:
        raise CompactError("continuation snapshot exceeds the frozen result limit")
    _validate_snapshot_counts(
        exact_total, corpus_more, more_unknown, shown_before, len(frozen))
    payload = {
        "version": SNAPSHOT_VERSION,
        "created": current,
        "expires": current + ttl,
        "generation": generation_token,
        "ranking_version": str(ranking_version),
        "corpus_more": bool(corpus_more),
        "query": query,
        "exact_total": exact_total,
        "total_floor": total_floor,
        "total_uncounted": total_uncounted,
        "chat_scoped": bool(chat_scoped),
        "deeper_argv": list(argv) if argv is not None else None,
        "more_unknown": more_unknown,
        "shown_before": shown_before,
        "deeper_covered": deeper_covered,
        "records": frozen,
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_SNAPSHOT_BYTES:
        raise CompactError("continuation snapshot is too large")
    root = _snapshot_dir(data_dir)
    _cleanup(root, current)
    # 48 bits: agents retype these; the namespace is <=64 ephemeral files.
    token = secrets.token_urlsafe(6)
    handle = f"m.{token}"
    target = _snapshot_path(handle, data_dir)
    fd, tmp_name = tempfile.mkstemp(prefix=".new-", dir=root)
    tmp = Path(tmp_name)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        # No fsync: the continuation is ephemeral (300s TTL) and the atomic
        # rename alone keeps readers off partial writes.
        stream = os.fdopen(fd, "wb")
        fd = -1
        with stream:
            stream.write(body)
        common.replace_with_retry(tmp, target)
        if os.name != "nt":
            os.chmod(target, 0o600)
    finally:
        if fd >= 0:
            os.close(fd)
        _unlink_best_effort(tmp)
    return handle


def save_snapshot(records: Sequence[dict], generation: object, ranking_version: str,
                  *, ttl_s: int = DEFAULT_TTL_S, data_dir: Path | None = None,
                  now: float | None = None, corpus_more: bool = False,
                  query: str, exact_total: int | None = None,
                  deeper_argv: Sequence[str] | None = None,
                  more_unknown: bool = False,
                  shown_before: int = 0,
                  total_floor: int | None = None,
                  total_uncounted: bool = False,
                  chat_scoped: bool = False) -> str:
    """Publish a private immutable continuation snapshot."""
    return _save_snapshot(records, generation_fingerprint(generation), ranking_version,
                          ttl_s=ttl_s, data_dir=data_dir, now=now,
                          corpus_more=corpus_more, query=query,
                          exact_total=exact_total, deeper_argv=deeper_argv,
                          more_unknown=more_unknown,
                          shown_before=shown_before, total_floor=total_floor,
                          total_uncounted=total_uncounted,
                          chat_scoped=chat_scoped)


# A well-formed handle with no file behind it is an expiry far more often than
# a typo, and the two are indistinguishable here; the lever is the same either
# way, so it is stated either way rather than left to be guessed.
_HANDLE_MISSING = ("continuation handle not found (expired, or issued "
                   "elsewhere); rerun the search")


def _load_snapshot(handle: str, expected_generation: object | None, ranking_version: str,
                   *, data_dir: Path | None,
                   now: float | None) -> _SnapshotData:
    path = _snapshot_path(handle, data_dir)
    try:
        if stat.S_ISLNK(path.lstat().st_mode):
            raise CompactError("continuation storage is unsafe")
    except FileNotFoundError as exc:
        raise CompactError(_HANDLE_MISSING) from exc
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise CompactError(_HANDLE_MISSING) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise CompactError("continuation storage is unsafe")
        if os.name != "nt" and info.st_mode & 0o077:
            raise CompactError("continuation storage is not private")
        if info.st_size > MAX_SNAPSHOT_BYTES:
            raise CompactError("continuation snapshot is too large")
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            payload = json.loads(stream.read(MAX_SNAPSHOT_BYTES + 1))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CompactError("continuation snapshot is corrupt") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    if not isinstance(payload, dict):
        raise CompactError("continuation snapshot is corrupt")
    if payload.get("version") != SNAPSHOT_VERSION:
        raise SnapshotExpired("continuation format changed; rerun the search")
    required = {
        "version", "created", "expires", "generation", "ranking_version",
        "corpus_more", "query", "exact_total", "total_floor",
        "total_uncounted", "chat_scoped", "deeper_argv", "more_unknown",
        "shown_before", "deeper_covered", "records",
    }
    if set(payload) != required:
        raise CompactError("continuation snapshot is corrupt")
    current = time.time() if now is None else float(now)
    try:
        created = float(payload["created"])
        expires = float(payload["expires"])
    except (TypeError, ValueError) as exc:
        raise CompactError("continuation snapshot has invalid time fields") from exc
    valid_times = (math.isfinite(created) and math.isfinite(expires)
                   and created <= expires <= created + MAX_TTL_S)
    if not valid_times:
        raise CompactError("continuation snapshot has invalid time fields")
    if current > expires or created > current + 5:
        # the file IS the evidence that this handle expired rather than never
        # existing; _cleanup reaps it at MAX_TTL_S, a re-read must not
        raise SnapshotExpired("continuation expired; rerun the search")
    generation_token = payload.get("generation")
    if not isinstance(generation_token, str) or not re.fullmatch(r"[0-9a-f]{64}", generation_token):
        raise CompactError("continuation snapshot has invalid generation")
    if (expected_generation is not None
            and generation_token != generation_fingerprint(expected_generation)):
        raise SnapshotExpired("corpus changed; rerun the search")
    if payload.get("ranking_version") != str(ranking_version):
        raise SnapshotExpired("ranking changed; rerun the search")
    if not isinstance(payload.get("corpus_more"), bool):
        raise CompactError("continuation snapshot has invalid corpus state")
    query, exact_total, deeper_argv = _validate_snapshot_metadata(
        payload.get("query"), payload.get("exact_total"),
        payload.get("deeper_argv"), payload["corpus_more"],
        payload.get("more_unknown"), payload.get("shown_before"), stored=True)
    records = tuple(_validate_records(payload.get("records")))
    if payload["shown_before"] + len(records) > MAX_FROZEN_HITS:
        raise CompactError("continuation snapshot exceeds the frozen result limit")
    _validate_snapshot_counts(
        exact_total, payload["corpus_more"], payload["more_unknown"],
        payload["shown_before"], len(records))
    deeper_covered = payload.get("deeper_covered")
    if type(deeper_covered) is not int or deeper_covered < 0:
        raise CompactError("continuation snapshot has invalid covered count")
    return _SnapshotData(
        records, generation_token,
        payload["corpus_more"], query, exact_total, deeper_argv,
        payload["more_unknown"], payload["shown_before"], deeper_covered,
        _validate_total_floor(payload.get("total_floor"), exact_total),
        _validate_total_uncounted(payload.get("total_uncounted"),
                                  exact_total, payload.get("total_floor")),
        _validate_flag(payload.get("chat_scoped"), "chat scope"))


def load_snapshot(handle: str, expected_generation: object | None, ranking_version: str,
                  *, data_dir: Path | None = None, now: float | None = None) -> list[dict]:
    """Load a frozen continuation, optionally requiring the current generation."""
    snapshot = _load_snapshot(
        handle, expected_generation, ranking_version,
        data_dir=data_dir, now=now)
    return list(snapshot.records)


def load_deeper_argv(handle: str, ranking_version: str, *,
                     data_dir: Path | None = None,
                     now: float | None = None) -> tuple[str, ...]:
    """Load a validated deeper search without exposing its query to a shell."""
    return load_deeper_context(
        handle, ranking_version, data_dir=data_dir, now=now)[0]


def load_deeper_context(
        handle: str, ranking_version: str, *,
        data_dir: Path | None = None,
        now: float | None = None,
) -> tuple[tuple[str, ...], int]:
    """The deeper argv plus how many ranked rows the frozen chain covers.

    Deeper means BEYOND the frozen pages: a replay that re-serves page one
    mints a fresh --more chain and an obedient agent loops forever. The
    chain's own remaining records may be empty by the time deeper is offered,
    so the covered span is a count - the replay reproduces the same ranked
    order and resumes past it."""
    snapshot = _load_snapshot(
        handle, None, ranking_version, data_dir=data_dir, now=now)
    if not snapshot.corpus_more or snapshot.deeper_argv is None:
        raise CompactError("continuation has no deeper search")
    # deeper_covered accumulates across hops (shown_before resets to 0 on a
    # fresh deeper replay), so deeper-on-deeper resumes past the prior hop.
    covered = int(snapshot.deeper_covered)
    if covered <= 0:
        covered = snapshot.shown_before + len(snapshot.records)
    return snapshot.deeper_argv, covered


def _page(records: Sequence[dict],
          generation: object | Callable[[], object], ranking_version: str,
          *, ttl_s: int, data_dir: Path | None, now: float | None,
          byte_budget: int, generation_is_token: bool = False,
          corpus_more: bool = False, terms_reserve: int = 0,
          query: str, exact_total: int | None,
          deeper_argv: Sequence[str] | None,
          more_unknown: bool, shown_before: int,
          tolerate_save_failure: bool = False,
          requested_rows: int | None = None,
          first_page_rows: int | None = None,
          tool_rescue_page: bool = False,
          deeper_covered: int = 0,
          total_floor: int | None = None,
          total_uncounted: bool = False,
          chat_scoped: bool = False) -> CompactPage:
    indices, reason = _compose_page(records, byte_budget,
                                    terms_reserve=terms_reserve,
                                    requested_rows=requested_rows,
                                    first_page_rows=first_page_rows,
                                    tool_rescue_page=tool_rescue_page,
                                    chat_scoped=chat_scoped)
    chosen = [records[index] for index in indices]
    chosen, clipped = _fit_page_records(chosen, byte_budget)
    if clipped:
        reason = "byte-budget"
    # Deferred rows interleave with chosen ones; the remainder keeps ranked
    # order so continuation pages replay them without re-ranking.
    picked = set(indices)
    remaining = [record for index, record in enumerate(records)
                 if index not in picked]
    handle = None
    continuation_lost = False
    if (remaining or corpus_more) and common.data_dir_readonly(
            data_dir or common.DATA_DIR):
        # a protected corpus takes no snapshot writes; the page still renders
        continuation_lost = True
        common.log("continuation storage is read-only here; showing this "
                   "page without a further handle")
    elif remaining or corpus_more:
        resolved = generation() if callable(generation) else generation
        token = str(resolved) if generation_is_token else generation_fingerprint(resolved)
        try:
            handle = _save_snapshot(
                remaining, token, ranking_version, ttl_s=ttl_s,
                data_dir=data_dir, now=now, corpus_more=corpus_more,
                query=query, exact_total=exact_total,
                deeper_argv=deeper_argv, more_unknown=more_unknown,
                shown_before=shown_before + len(chosen),
                deeper_covered=deeper_covered + len(chosen),
                total_floor=total_floor, total_uncounted=total_uncounted,
                chat_scoped=chat_scoped)
        except OSError:
            if not tolerate_save_failure:
                raise
            continuation_lost = True
            common.log(
                "continuation storage is unavailable; showing this page "
                "without a further handle")
    return CompactPage(tuple(chosen), bool(remaining) and not continuation_lost,
                       handle, reason,
                       shown_before + len(chosen)
                       + (1 if remaining or corpus_more else 0),
                       corpus_more and not continuation_lost, query, exact_total,
                       tuple(deeper_argv) if deeper_argv is not None else None,
                       more_unknown or continuation_lost, shown_before,
                       requested_rows, total_floor, total_uncounted,
                       chat_scoped)


def start_compact(ranked_hits: Sequence[dict], render_row: Callable[[dict], str],
                  generation: object | Callable[[], object], ranking_version: str, *,
                  family_key: Callable[[dict], str] | None = None,
                  preordered: bool = False,
                  ttl_s: int = DEFAULT_TTL_S, data_dir: Path | None = None,
                  now: float | None = None,
                  byte_budget: int = DEFAULT_BYTE_BUDGET,
                  corpus_more: bool = False, query: str,
                  exact_total: int | None = None,
                  deeper_argv: Sequence[str] | None = None,
                  more_unknown: bool = False,
                  requested_rows: int | None = None,
                  first_page_rows: int | None = None,
                  tool_rescue_page: bool = False,
                  deeper_covered: int = 0,
                  total_floor: int | None = None,
                  total_uncounted: bool = False,
                  chat_scoped: bool = False) -> CompactPage:
    """Create a first page, resolving a generation loader only for continuation."""
    ordered = (list(ranked_hits) if preordered
               else diversify_hits(ranked_hits, family_key))
    records = freeze_records(ordered, render_row)
    # Only the direct response reserves ~all slots: --more is already an
    # explicit descent into the ranked order.
    return _page(records, generation, ranking_version, ttl_s=ttl_s,
                 data_dir=data_dir, now=now, byte_budget=byte_budget,
                 corpus_more=corpus_more,
                 terms_reserve=0 if requested_rows is not None else TERMS_RESERVE,
                 query=query, exact_total=exact_total,
                 deeper_argv=deeper_argv, more_unknown=more_unknown,
                 shown_before=0, requested_rows=requested_rows,
                 first_page_rows=first_page_rows,
                 tool_rescue_page=tool_rescue_page,
                 deeper_covered=deeper_covered,
                 total_floor=total_floor, total_uncounted=total_uncounted,
                 chat_scoped=chat_scoped, tolerate_save_failure=True)


def fixed_compact(ranked_hits: Sequence[dict], render_row: Callable[[dict], str], *,
                  family_key: Callable[[dict], str] | None = None,
                  corpus_more: bool = False) -> CompactPage:
    """Render an explicit row request without adaptive stopping."""
    frozen = list(ranked_hits)
    ordered = diversify_hits(frozen[:MAX_FROZEN_HITS], family_key)
    ordered.extend(frozen[MAX_FROZEN_HITS:])
    records = freeze_records(ordered, render_row)
    known = len(records) + (1 if corpus_more else 0)
    return CompactPage(
        tuple(records), False, None, "explicit-limit", known, corpus_more)


def continue_compact(handle: str, expected_generation: object | None, ranking_version: str,
                     *, ttl_s: int = DEFAULT_TTL_S, data_dir: Path | None = None,
                     now: float | None = None,
                     byte_budget: int = DEFAULT_BYTE_BUDGET) -> CompactPage:
    """Serve the next immutable page for its full TTL and note a changed corpus."""
    snapshot = _load_snapshot(
        handle, None, ranking_version, data_dir=data_dir, now=now)
    if not snapshot.records and snapshot.corpus_more:
        raise CompactError(
            f"no frozen rows remain; use: agrep --deeper {handle}")
    if (expected_generation is not None
            and snapshot.generation != generation_fingerprint(expected_generation)):
        # Frozen rows are self-contained, so background ingests cannot corrupt
        # this page; on live boxes every ingest bumps the global generation,
        # which must not kill the handle. Meta already travels on stderr.
        common.log("corpus changed since this page was frozen; "
                   "newer results may exist - rerun the search to refresh")
    return _page(snapshot.records, snapshot.generation, ranking_version, ttl_s=ttl_s,
                 data_dir=data_dir, now=now, byte_budget=byte_budget,
                 generation_is_token=True, corpus_more=snapshot.corpus_more,
                 query=snapshot.query, exact_total=snapshot.exact_total,
                 deeper_argv=snapshot.deeper_argv,
                 more_unknown=snapshot.more_unknown,
                 shown_before=snapshot.shown_before,
                 deeper_covered=snapshot.deeper_covered,
                 tolerate_save_failure=True,
                 total_floor=snapshot.total_floor,
                 total_uncounted=snapshot.total_uncounted,
                 chat_scoped=snapshot.chat_scoped)


def _common_prefix_len(a: str, b: str) -> int:
    length = 0
    for x, y in zip(a, b):
        if x != y:
            break
        length += 1
    return length


def _needed_prefix(session: str, ordered: Sequence[str]) -> int:
    # In sorted order the immediate neighbors carry the longest prefix any
    # other id shares with this one.
    position = bisect.bisect_left(ordered, session)
    longest = 0
    for near in (position - 1, position, position + 1):
        if 0 <= near < len(ordered) and ordered[near] != session:
            longest = max(longest, _common_prefix_len(session, ordered[near]))
    return longest + 1


_SESSION_TARGET_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,127}")


def encode_session_target(session: object, prefix_chars: int = 8,
                          session_index: Sequence[str] | None = None) -> str:
    """Encode the shortest unambiguous session argument for CLI follow-ups."""
    value = str(session or "")
    if _SESSION_TARGET_RE.fullmatch(value) is None:
        return value
    size = max(3, min(int(prefix_chars), len(value)))
    if session_index is not None:
        if (isinstance(session_index, SessionPrefixIndex)
                and value in session_index.force_full):
            return value
        size = min(len(value), max(size, _needed_prefix(value, session_index)))
    return value[:size]


def encode_session_handle(
        session: object, prefix_chars: int = 8,
        session_index: Sequence[str] | None = None,
) -> str | None:
    """Return a pasteable @session only when the public grammar can represent it."""
    value = str(session or "")
    if _SESSION_TARGET_RE.fullmatch(value) is None:
        return None
    return "@" + encode_session_target(
        value, prefix_chars=prefix_chars, session_index=session_index)


def encode_result_handle(hit: Mapping[str, object], prefix_chars: int = 8,
                         session_index: Sequence[str] | None = None, *,
                         text: str | None = None) -> str:
    """Encode a search result as an around-compatible short target.

    Pass session_index (from session_prefix_index) to size row handles against
    a page-wide index instead of rescanning sessions for every row. Pass text
    to bind the handle to what it points at, so a later replay against a
    renumbered or rebound session is caught instead of silently served.
    """
    session = str(hit.get("session") or "")
    turn = hit.get("turn")
    if turn is None:
        raise CompactError("result has no turn")
    try:
        number = int(turn)
    except (TypeError, ValueError) as exc:
        raise CompactError("result has no turn") from exc
    if not 0 <= number <= _MAX_RESULT_TURN:
        raise CompactError("result turn is invalid")
    digest = content_digest(text) if text is not None else hit.get("content_digest")
    if digest is not None and re.fullmatch(r"[0-9a-f]{4}", str(digest)) is None:
        raise CompactError("result content digest is invalid")
    span_tail = ""
    identity = _event_identity(hit) if hit.get("who") == "tool" else None
    span = hit.get("_match_span") if identity is not None else None
    if (isinstance(span, (list, tuple)) and len(span) == 2
            and all(type(value) is int for value in span)
            and 0 <= span[0] < span[1] <= _MAX_MATCH_OFFSET):
        span_tail = f"~{identity}:{span[0]}-{span[1]}"
    tail = f".{digest}{span_tail}" if digest else ""
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,127}", session):
        target = encode_session_target(
            session, prefix_chars=prefix_chars, session_index=session_index)
        return f"@{target}:{number}{tail}"
    token = base64.urlsafe_b64encode(session.encode("utf-8")).rstrip(b"=").decode("ascii")
    if not token or len(token) > 1024:
        raise CompactError("session id cannot be encoded safely")
    return f"@~{token}:{number}{tail}"


def encode_bound_result_handle(
        hit: Mapping[str, object], prefix_chars: int = 8,
        session_index: Sequence[str] | None = None, *,
        text: str | None = None) -> str:
    """Encode a handle only when it carries a verifiable content claim."""
    value = encode_result_handle(
        hit, prefix_chars=prefix_chars, session_index=session_index, text=text)
    if parse_result_handle_parts(value)[2] is None:
        raise CompactError("result has no content identity")
    return value


# FNV-1a-64 carried mod 2^16. Multiplication mod 2^16 depends only on the
# operands mod 2^16 and a byte XOR touches only the low 8 bits, so the narrow
# state reproduces the wide hash's low word exactly - the published digest.
_FNV_OFFSET_16 = 0xCBF29CE484222325 & 0xFFFF
_FNV_PRIME_16 = 0x100000001B3 & 0xFFFF


def content_digest(text: str) -> str:
    """Stable FNV-1a-64, low 16 bits, hex. Deliberately not a seeded hash:
    this travels in saved notes and must mean the same thing forever."""
    digest = _FNV_OFFSET_16
    for byte in (text or "").encode("utf-8"):
        digest = ((digest ^ byte) * _FNV_PRIME_16) & 0xFFFF
    return f"{digest:04x}"


def require_content_digest(value: object) -> str:
    """Return a persisted handle digest only when it is in canonical form."""
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{4}", value) is None:
        raise CompactError("result content digest is invalid")
    return value


def is_result_handle(value: object) -> bool:
    """Recognize a turn handle. ``@session:turn`` is always handle-shaped;
    without ``@``, require the digest that distinguishes it from positional
    ``session:turn`` syntax."""
    text = str(value or "").strip()
    try:
        _session, _turn, digest = parse_result_handle_parts(text)
    except CompactError:
        return False
    return text.startswith("@") or digest is not None


def normalize_session_arg(value: object) -> str:
    """Accept any printed identity where a session is accepted: `@` is the
    sigil agrep prints, so pasting a row back must never be a syntax error."""
    text = str(value or "").strip()
    if text.startswith("@") and not is_result_handle(text):
        text = text[1:]
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise CompactError("session id is not valid UTF-8") from exc
    if size > SESSION_ID_MAX_BYTES:
        raise CompactError(
            f"session id exceeds {SESSION_ID_MAX_BYTES} UTF-8 bytes")
    return text


def parse_result_handle(value: str) -> tuple[str, int]:
    """Parse a compact result handle without resolving its session prefix."""
    session, turn, _digest = parse_result_handle_parts(value)
    return session, turn


def parse_result_handle_parts(value: str) -> tuple[str, int, str | None]:
    """Parse a handle into (session, turn, content digest or None).

    Digestless handles stay valid forever: every note written before the
    digest existed still resolves, it just cannot be verified."""
    session, turn, digest, _identity, _span = parse_result_handle_claim(value)
    return session, turn, digest


def parse_result_handle_claim(
        value: str,
) -> tuple[str, int, str | None, str | None, tuple[int, int] | None]:
    """Parse content identity plus an optional bounded tool display claim."""
    opaque = _OPAQUE_RESULT_RE.fullmatch(value.strip())
    if opaque is not None:
        token = opaque.group(1)
        padded = token + "=" * (-len(token) % 4)
        try:
            raw = base64.b64decode(padded, altchars=b"-_", validate=True)
            session = raw.decode("utf-8")
        except (ValueError, UnicodeError) as exc:
            raise CompactError("invalid result handle") from exc
        if not session:
            raise CompactError("invalid result handle")
        match = opaque
    else:
        match = _RESULT_RE.fullmatch(value.strip())
        if match is None:
            raise CompactError("invalid result handle")
        session = match.group(1)
    turn = int(match.group(2))
    if turn > _MAX_RESULT_TURN:
        raise CompactError("invalid result handle")
    identity = match.group(4)
    start = int(match.group(5)) if match.group(5) is not None else None
    end = int(match.group(6)) if match.group(6) is not None else None
    span = (start, end) if start is not None and end is not None else None
    if span is not None and not 0 <= span[0] < span[1] <= _MAX_MATCH_OFFSET:
        raise CompactError("invalid result handle")
    return session, turn, match.group(3), identity, span


def resolve_result_handle_candidates(
        value: str,
        resolve_session: Callable[[str], Sequence[str]]) -> tuple[list[str], int]:
    """The sessions a handle's prefix can mean: one when it pins a session,
    several when something else must break the tie. Only an empty match is an
    error - a caller holding a content digest can still resolve a tie."""
    prefix, turn = parse_result_handle(value)
    sessions = list(resolve_session(prefix))
    exact = [session for session in sessions if session == prefix]
    if exact:
        return [exact[0]], turn
    if not sessions:
        raise CompactError(
            "no indexed session matches this handle - the handle is stale "
            f"or its session was pruned; {surface.REMEDIES['stale-handle'].text}")
    return sessions, turn


def resolve_result_handle(value: str,
                          resolve_session: Callable[[str], Sequence[str]]) -> tuple[str, int]:
    """Resolve a result handle while rejecting absent or ambiguous prefixes."""
    sessions, turn = resolve_result_handle_candidates(value, resolve_session)
    if len(sessions) == 1:
        return sessions[0], turn
    raise CompactError("result handle session is ambiguous")
