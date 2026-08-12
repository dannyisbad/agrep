"""agrep's terminal search - grep your agent history from the shell.

    agrep "rust simd"            # every message across every agent that matches
    agrep deadlock --agent codex # filter to one agent
    agrep -E "TODO|FIXME"        # regex
    agrep -l auth                # list matching chats, not every line (like grep -l)
    agrep "memory leak" --json   # run envelope, then one JSON object per hit

Keyword is the default: no model, runs straight off the materialized corpus
(core tier, any Python). --semantic adds meaning search through a short-lived
headless worker or an in-process fallback. Output is grep-style and pipe-friendly:
matches are highlighted only on a TTY, and trailing counts go to stderr.
"""

from __future__ import annotations

import argparse
import errno
import heapq
import itertools
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

import boundary_rank
import common
import compact
import console
import display_policy
import indexd_runtime
import surface_policy as surface

corpusdb = None

_SEMANTIC_WORKER_START_MISS = (
    "semantic worker did not become query-ready before the automatic deadline")


def _load_corpusdb():
    """Load the FTS engine only when a finished index can be queried.

    The streamed first-run path searches Rust's emitted rows and can print a hit before
    this comparatively heavy sqlite/index module is imported.
    """
    global corpusdb
    if corpusdb is None:
        import corpusdb as corpusdb_module
        corpusdb = corpusdb_module
    return corpusdb


_C = surface.PALETTE


def _color_on(when: str) -> bool:
    return common.color_enabled(sys.stdout, when)


def _proj(p: str) -> str:
    """Last path segment of a project dir, for compact display."""
    p = (p or "").rstrip("/\\")
    if not p:
        return "-"
    # rfind over both separators == re.split(r"[/\\]", p)[-1], without the
    # regex machinery; this runs once per row on ranking and machine paths.
    cut = p.rfind("/")
    back = p.rfind("\\")
    return p[(cut if cut > back else back) + 1:]


# One implementation: common owns the snippet renderers, the alias keeps call sites stable.
_snip_at = common.snip_at


def terminal_safe(value: object, *, multiline: bool = False) -> str:
    """Compatibility alias for the shared terminal trust boundary."""
    return common.terminal_safe(value, multiline=multiline)


def _hl(snippet: str, pat: re.Pattern | None, color: bool) -> str:
    snippet = terminal_safe(snippet)
    if not color or pat is None:
        return snippet
    return pat.sub(lambda m: _C["m"] + m.group(0) + _C["r"], snippet)


def _hl_regex(q: str, regex: bool) -> re.Pattern | None:
    """The pattern used to RE-highlight the match inside a snippet. Keyword mode mirrors
    explore.keyword_search's punctuation-flexible matcher (so 'cyber filter' lights up
    'cyber_filter', and a phrase lights up across em-dashes/parens); regex mode uses the
    user's pattern verbatim. Must stay in lockstep with the [\\W_]* join used to match."""
    try:
        if regex:
            return re.compile(q, re.I)
        toks = [re.escape(t) for t in re.split(r"[\s\-_]+", q.strip()) if t]
        return re.compile(r"[\W_]*".join(toks), re.I) if toks else None
    except re.error:
        return None


def _terms_hl_pat(q: str) -> re.Pattern | None:
    """Highlight for any-order fallback rows (~all): each content term marks
    itself - the in-order phrase pattern can never match a scattered row, and
    an unmarked row reads as "the match isn't shown"."""
    terms = sorted({t.lower() for t in _content_terms(q) if len(t) >= 3},
                   key=lambda term: (-len(term), term))
    if not terms:
        return None
    return re.compile("|".join(re.escape(term) for term in terms), re.I)


def _match_pat(q: str, mode: str) -> re.Pattern | None:
    """The pattern that re-finds the match inside a snippet, per mode - shared by scoring
    and highlighting so they agree on what 'the match' is. None for semantic (chat titles,
    nothing to re-find)."""
    if mode == "semantic":
        return None
    if mode == "word":
        return common.literal_word_pattern(q)
    return _hl_regex(q, mode == "regex")


def _word_scan(q: str, k: int, flt: dict | None = None) -> dict:
    """Whole-word JSONL scan with the same Python re.I semantics as corpusdb."""
    import explore
    pat = common.literal_word_pattern(q)
    hits = []
    for e in explore._iter_kw_corpus(flt):
        match = pat.search(e["text"])
        if match is not None:
            hits.append(explore.scan_hit(e, match.start(), match.end()))
    hits.sort(key=lambda h: (h["session"], h["turn"], 0 if h["who"] != "agent" else 1))
    return {"hits": hits[:k], "total": len(hits), "chats": len({h["session"] for h in hits})}


def _regex_scan(pattern: str, k: int, flt: dict | None = None) -> dict:
    """Regex search over the same corpus keyword_search uses (-E mode). Same hit shape."""
    import explore
    try:
        rx = re.compile(pattern, re.I)
    except re.error as e:
        common.log(f"bad regex: {e}")
        raise SystemExit(2)
    hits = []
    for e in explore._iter_kw_corpus(flt):
        # original text only, like corpusdb.regex: matching the pre-lowered
        # copy first made (?-i:...) constructs hit text the pattern rejects,
        # so the two engines disagreed depending on index state
        m = rx.search(e["text"])
        if m:
            hits.append(explore.scan_hit(e, m.start(), m.end()))
    hits.sort(key=lambda h: (h["session"], h["turn"], 0 if h["who"] != "agent" else 1))
    return {"hits": hits[:k], "total": len(hits), "chats": len({h["session"] for h in hits})}


def _terms_scan(q: str, k: int, flt: dict | None = None) -> dict:
    """JSONL bag-of-words AND (no corpus db): every token present in an entry, any
    order. Mirrors corpusdb.terms - the fallback when keyword's in-order matcher finds
    nothing. Snippet sits at the earliest matched token."""
    import explore
    toks = [t for t in re.split(r"[\s\-_]+", q.strip()) if t]
    if len(toks) < 2:
        return {"hits": [], "total": 0, "chats": 0}
    toks = list(dict.fromkeys(toks))
    fields = ("session", "agent", "project", "concept", "model", "model_source",
              "turn", "ts", "who")
    hits = []
    for row_key, e in enumerate(explore._iter_kw_corpus(flt)):
        spans = [common.insensitive_span(e["text"], token, e["low"])
                 for token in toks]
        if any(span is None for span in spans):
            continue
        matched_spans = [span for span in spans if span is not None]
        # multi-span stitching keeps its own renderer; the event columns still
        # ride through for the display passes' structural guards
        hit = {**{f: e[f] for f in fields},
               "content_digest": explore._entry_content_digest(e),
               **explore.scan_event_columns(e),
               "snippet": explore._snip_spans(e["text"], matched_spans),
               "_match_span": min(
                   matched_spans, key=lambda span: (span[0], span[1])),
               "_agrep_row_key": row_key}
        if e.get("who") == "tool":
            identity = common.tool_event_identity(
                e.get("session"), e.get("turn"), e.get("ts"), e.get("text"))
            if identity is not None:
                hit["_event_identity"] = identity
        hits.append(hit)
    hits.sort(key=lambda h: (h["session"], h["turn"], 0 if h["who"] != "agent" else 1))
    return {"hits": hits[:k], "total": len(hits), "chats": len({h["session"] for h in hits})}


def _content_scan(toks: list[str], k: int, flt: dict | None = None) -> dict:
    """JSONL scored-OR (no corpus db): mirrors corpusdb.content. Any content term
    hits, idf-weighted coverage ranks, score rides out on h['coverage'].

    IDF population matches corpusdb.content: speaker/prose selection chooses the
    FTS lane, while agent/project/chat/model/time filters constrain candidates but
    do not redefine global document frequency. That takes a second streaming pass
    in this rare content-fallback tier, trading a little I/O for bounded memory."""
    import math
    import explore
    lows = list(dict.fromkeys(t.lower() for t in toks))
    # DF universe = prose/speaker selection only, never the row filters (docstring).
    who = (flt or {}).get("who")
    prose_only = (not surface.speaker_filter_admits(who, "tool")
                  if who is not None
                  else (flt or {}).get("include_tools") is False)
    tool_lane = ({"_tool_lane_enabled": flt["_tool_lane_enabled"]}
                 if flt is not None and "_tool_lane_enabled" in flt else {})
    idf_flt = ({**tool_lane, "include_tools": False}
               if prose_only else tool_lane or None)
    n_docs = 0
    # FTS5 trigram cannot index one/two-character terms. corpusdb treats those as
    # maximally common instead of pretending a substring scan has equivalent token
    # semantics, so keep them out of the DF pass and assign df=n_docs below.
    df = dict.fromkeys(lows, 0)
    for e in explore._iter_kw_corpus(idf_flt):
        n_docs += 1
        low = e["low"]
        for t in lows:
            if len(t) >= 3 and t in low:
                df[t] += 1
    n_docs = n_docs or 1
    for t in lows:
        if len(t) < 3:
            df[t] = n_docs
    idf = {t: math.log(1.0 + n_docs / (df[t] + 1)) for t in lows}
    anchors = [t for t in sorted(lows, key=lambda t: -idf[t])
               if len(t) >= 3 and idf[t] > 0][:3]
    if not anchors:
        return {"hits": [], "total": 0, "chats": 0}
    total_idf = sum(idf.values()) or 1.0
    hits = []
    for e in explore._iter_kw_corpus(flt):
        # Mirror corpusdb's candidate lane: score only rows in the three strongest
        # anchors' OR posting lists - fallback semantics, not a result cap.
        if not any(t in e["low"] for t in anchors):
            continue
        pos = {t: e["low"].find(t) for t in lows}
        matched = [t for t, p in pos.items() if p >= 0]
        if not matched:
            continue
        cov = sum(idf[t] for t in matched) / total_idf  # no floor - see corpusdb.content
        first = min(pos[t] for t in matched)
        tok = next(t for t in matched if pos[t] == first)
        start, end = common.original_span_for_lowered(
            e["text"], e["low"], first, first + len(tok))
        h = explore.scan_hit(e, start, end)
        h["coverage"] = round(cov, 4)
        hits.append(h)
    hits.sort(key=lambda h: (-h["coverage"], h["session"], h["turn"]))
    return {"hits": hits[:k], "total": len(hits), "chats": len({h["session"] for h in hits})}


SEMANTIC_MAX_RESULTS = 200
SEMANTIC_SCORE_BANDS = surface.DEFAULT_SEMANTIC_SCORE_BANDS
SEMANTIC_MIN_COSINE = SEMANTIC_SCORE_BANDS.floor
_RECALL_STRONG_SEM = SEMANTIC_SCORE_BANDS.strong
# Lexical evidence a scattered-terms row must carry before a pointer calls it
# a match: substring scatter ("div" inside "individual") tops out at 0.42 on
# the jul 2026 fixture calibration; lived bag-of-words matches start at 0.64.
SCATTER_MIN_EVIDENCE = 0.55
_COMMON_ENGLISH_BIGRAMS = frozenset(
    "th he in er an re on at en nd ti es or te of ed is it al ar st to nt ng se "
    "ha as ou io le ve co me de hi ri ro ic ne ea ra ce li ch ll be ma si om ur "
    "ca el ta la ns di fo ho pe ec pr no ct us ac ot il tr ly nc et ut ss so rs un "
    "lo wa ge ie wh ee wi em ad ol rt po we na ul ni ts mo ow pa im mi ai sh do ck "
    "ke eb gr ph ap ry cr pt".split())


def _singleton_wordlike(letters: str) -> bool:
    """Cheap deterministic negative calibration for long one-token queries."""
    low = letters.lower()
    if len(low) < 7:
        return True
    pairs = [low[i:i + 2] for i in range(len(low) - 1)]
    sequential = sum(abs(ord(low[i + 1]) - ord(low[i])) == 1
                     for i in range(len(low) - 1))
    if sequential >= max(4, int(len(pairs) * .6)):
        return False
    keyboard = ("qwertyuiop", "asdfghjkl", "zxcvbnm")
    if any(fragment in row or fragment in row[::-1]
           for row in keyboard for fragment in (low,)
           if len(fragment) >= 5):
        return False
    common = sum(pair in _COMMON_ENGLISH_BIGRAMS for pair in pairs)
    return common / max(1, len(pairs)) >= .25


def semantic_query_policy(query: str) -> dict:
    """Classify whether dense meaning search is appropriate for ``query``.

    Dense retrieval always returns a nearest neighbour. Exact identifiers, hashes,
    paths and random consonant strings are therefore especially dangerous: a miss
    becomes a plausible-looking unrelated answer. Keyword fallback is both faster
    and more accurate for those shapes. Natural-language phrases containing an
    identifier remain eligible; this guard targets identifier-*only* requests.

    This public policy is shared by the CLI and recall paths.
    """
    q = (query or "").strip()
    base = {"eligible": True, "reason": None,
            "min_score": SEMANTIC_MIN_COSINE}
    if not q:
        return {**base, "eligible": False, "reason": "empty-query"}
    if any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F for c in q):
        return {**base, "eligible": False, "reason": "control-characters"}

    whitespace_tokens = q.split()
    words = re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", q)
    if not words:
        return {**base, "eligible": False, "reason": "non-language-query"}

    # Multiple natural words are useful even when one is a stack symbol/error code.
    # A single shell-ish token is instead an exact-lookup request.
    if len(whitespace_tokens) == 1:
        token = whitespace_tokens[0]
        letters = "".join(re.findall(r"[A-Za-z]", token))
        obvious_identifier = bool(
            re.search(r"[/\\:@]|[_]{1,}|[A-Z][a-z]+[A-Z]|[a-z][A-Z]", token)
            or (len(token) >= 4 and token.isupper())
            or re.fullmatch(r"(?:0x)?[0-9a-fA-F]{8,}", token)
            # dotted-suffix tokens (server.yaml, example.com, mod.attr) and
            # semver tags are exact-lookup shapes, not prose
            or re.fullmatch(r"[A-Za-z0-9_.-]+\.[A-Za-z][A-Za-z0-9]{1,7}", token)
            or re.fullmatch(r"[vV]\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.-]+)?",
                            token))
        has_vowel = bool(re.search(r"[aeiouy]", letters, re.I))
        if obvious_identifier:
            return {**base, "eligible": False, "reason": "identifier-query"}
        if len(letters) >= 7 and not has_vowel:
            return {**base, "eligible": False, "reason": "gibberish-query"}
        if len(letters) >= 7 and not _singleton_wordlike(letters):
            return {**base, "eligible": False, "reason": "gibberish-query"}
        if len(token) >= 24 and not re.fullmatch(r"[A-Za-z'-]+", token):
            return {**base, "eligible": False, "reason": "identifier-query"}
    return base


_SEMANTIC_ANCHOR_PROBE_MAX = 8


def semantic_corpus_anchor(query: str, flt: dict | None = None) -> dict:
    """Does any query word occur in the corpus the meaning lane just searched?

    Multi-word out-of-vocabulary mush ("zqxjklwvutplmb frobnicated quuxstring")
    is never a single token, so the gibberish gate above cannot see it, and
    embedding hubness carries it over the strong band anyway. Zero anchored
    words does not make the page wrong enough to refuse - it makes the rows
    speculative, which is a label question. ``anchored`` is None when the
    corpus cannot answer; an unprobed lane never demotes.

    ``flt`` is the lane's own filter set, so the probe measures the same scope
    the neighbors came from: a caller's family is excluded from both, and the
    query's own echo in the transcript that typed it never anchors itself.
    """
    words: list[str] = []
    for word in re.findall(r"[A-Za-z0-9]+", query or ""):
        low = word.lower()
        if len(low) < 3 or low in _STOP or low in words:
            continue
        words.append(low)
        if len(words) >= _SEMANTIC_ANCHOR_PROBE_MAX:
            break
    if not words:
        return {"anchored": None, "probed": 0}
    _load_corpusdb()
    db = corpusdb.connect(allow_stale=True)
    if db is None:
        return {"anchored": None, "probed": 0}
    try:
        for probed, word in enumerate(words, 1):
            # cap 1 stops at the first confirmed row: an existence probe walks
            # one posting list, never the corpus. The candidate lane's cheaper
            # superset count is unusable here - it anchors on phantoms.
            if corpusdb.keyword_count(db, word, flt, cap=1)["total"]:
                return {"anchored": True, "probed": probed}
    except sqlite3.DatabaseError as exc:
        corpusdb.record_query_database_error(exc, db)
        return {"anchored": None, "probed": 0}
    except ValueError:
        # a filter set the keyword lane never validated: unknown, not weak
        return {"anchored": None, "probed": 0}
    finally:
        try:
            db.close()
        except sqlite3.DatabaseError as exc:
            corpusdb.record_query_database_error(exc, db)
    return {"anchored": False, "probed": len(words)}


def semantic_result_policy(query: str, rows: list[dict], *, requested: int,
                           explicit_who: object = None,
                           coverage: dict | None = None,
                           partial: bool = False,
                           score_kind: str = "cosine") -> dict:
    """Apply the one semantic relevance/noise contract to raw ranked rows.

    Returns ``results`` plus a JSON-safe ``semantic_status`` envelope. Callers may
    normalize row shapes after this function, but must not invent their own score or
    noise threshold. Suppressed roles remain explicitly searchable with ``--who``.
    """
    query_state = semantic_query_policy(query)
    requested = max(0, min(int(requested), SEMANTIC_MAX_RESULTS))
    if not query_state["eligible"]:
        return {
            "results": [],
            "semantic_status": {
                "state": "query-rejected", "complete": False,
                "fallback_recommended": True, **query_state,
                "coverage": coverage,
            },
        }

    accepted: list[dict] = []
    weak = noise = invalid = 0
    for row in rows:
        who = str(row.get("who") or "")
        if explicit_who is None and who in common.SEMANTIC_DEFAULT_EXCLUDED_ROLES:
            noise += 1
            continue
        raw_score = row.get("sem_score", row.get("score"))
        try:
            score = float(raw_score)
        except (TypeError, ValueError):
            invalid += 1
            continue
        if not math.isfinite(score):
            invalid += 1
            continue
        if score < query_state["min_score"]:
            weak += 1
            continue
        accepted.append(row)

    shown = accepted[:requested] if requested else accepted
    complete_coverage = bool(coverage and coverage.get("complete") and not partial)
    state = "ready" if shown else "no-confident-match"
    fallback_recommended = not shown and not complete_coverage
    return {
        "results": shown,
        "semantic_status": {
            "state": state,
            "eligible": True,
            "reason": None,
            "min_score": query_state["min_score"],
            "score_kind": score_kind or "cosine",
            "coverage": coverage,
            "partial": bool(partial),
            "complete": complete_coverage,
            "fallback_recommended": fallback_recommended,
            "filtered": {"weak": weak, "noise": noise, "invalid": invalid},
            "truncated": len(accepted) > len(shown),
        },
    }


def _semantic_runtime_unavailable(
        query: str, requested: int, who: object, reason: str,
) -> dict:
    policy = semantic_result_policy(
        query, [], requested=requested, explicit_who=who,
        coverage=None, partial=True, score_kind="unavailable")
    status = policy["semantic_status"]
    status.update(
        state="unavailable", reason=reason, complete=False,
        fallback_recommended=True)
    if surface.semantic_status_retryable({"reason": reason}):
        status["retryable"] = True
    return {
        "hits": [], "total": 0, "chats": 0, "truncated": False,
        "semantic_coverage": None, "semantic_accelerator_coverage": None,
        "partial": True, "score_kind": "unavailable",
        "semantic_status": status, "fallback_recommended": True,
        "semantic_integrity": None,
    }


def _semantic_index_update_active() -> bool:
    try:
        import segment_query
        return segment_query.corpus_update_active()
    except (ImportError, OSError):
        return False


def _semantic_bootstrap_disabled() -> bool:
    if not os.environ.get("AGREP_NO_DAEMON"):
        return False
    try:
        import semantic
        coherence = semantic.embedding_coherence()
    except (ImportError, OSError, TypeError, ValueError):
        return True
    return not coherence.get("searchable", coherence.get("coherent", False))


def _semantic_row_weak(hit: dict) -> bool:
    """One weak verdict for a meaning row, shared by every label site: a
    sub-strong score, or a page whose query anchored nowhere in the corpus -
    those scores rank noise against noise, so none of them earns confidence."""
    score = hit.get("sem_score")
    return bool(hit.get("_sem_unanchored")) or (
        score is not None and float(score) < _RECALL_STRONG_SEM)


def _weak_only_semantic_hits(hits: list[dict]) -> bool:
    """True when every candidate is a scored, sub-strong meaning neighbor.

    The semantic floor remains useful for explicit exploration, but automatic
    assist must not turn an empty lexical result into success merely because a
    nearest-neighbor index can always return something.
    """
    rows = list(hits)
    if not rows:
        return False
    for hit in rows:
        try:
            score = float(hit["sem_score"])
        except (KeyError, TypeError, ValueError):
            return False
        if not math.isfinite(score) or score >= _RECALL_STRONG_SEM:
            return False
    return True


def _weak_semantic_neighbors_notice(query: str) -> str:
    inspect = console.shell_command("agrep", "-s", "--", query)
    return ("no semantic-only candidate cleared the auto-use threshold; "
            "inspect below-auto-threshold candidates: "
            f"{inspect}")


def _semantic_local(q: str, k: int, level: str = "hybrid", *,
                    agent: str | None = None, project: str | None = None,
                    exclude_project: str | None = None,
                    who: object = None, model: str | None = None,
                    model_soft: bool = False, chat: str | None = None,
                    since_ms: int | None = None,
                    until_ms: int | None = None,
                    exclude_session: str | None = None,
                    exclude_session_from_turn: int | None = None,
                    exclude_sessions: tuple[str, ...] = (),
                    exclude_family: bool = True,
                    family_diverse: bool = True,
                    timeout_s: float | None = None,
                    allow_model_download: bool = False) -> dict | None:
    """Meaning search through a disposable resident worker, then an in-process
    fallback when local IPC cannot start. Returns the normalized hit dict,
    or None with a logged reason when the lane cannot answer."""
    requested = min(max(0, int(k)), SEMANTIC_MAX_RESULTS)
    deadline = (
        None if timeout_s is None
        else time.monotonic() + max(0.0, float(timeout_s))
    )

    def remaining() -> float | None:
        if deadline is None:
            return None
        return max(0.0, deadline - time.monotonic())

    query_state = semantic_query_policy(q)
    if not query_state["eligible"]:
        status = semantic_result_policy(
            q, [], requested=requested, explicit_who=who)["semantic_status"]
        return {"hits": [], "total": 0, "chats": 0, "truncated": False,
                "semantic_coverage": None, "partial": False,
                "score_kind": "cosine", "semantic_status": status,
                "fallback_recommended": True}
    fetch_k = (SEMANTIC_MAX_RESULTS if requested == 0 else
               requested if who is not None else
               min(SEMANTIC_MAX_RESULTS, max(requested * 4, 40)))
    filters = {key: value for key, value in {
        "agent": agent, "project": project,
        "exclude_project": exclude_project, "model": model,
        "chat": chat, "since_ms": since_ms, "until_ms": until_ms}.items()
        if value is not None and value != ""}
    if isinstance(who, str):
        filters["who"] = who
    elif isinstance(who, surface.SpeakerFilter):
        if who.include is not None:
            filters["_include_who"] = tuple(who.include)
        if who.exclude:
            filters["_exclude_who"] = tuple(who.exclude)
    if exclude_session:
        if exclude_session_from_turn is None:
            filters["exclude_session"] = exclude_session
        elif type(exclude_session_from_turn) is int:  # noqa: E721
            filters["exclude_session"] = exclude_session
            filters["exclude_session_from_turn"] = exclude_session_from_turn
    if exclude_sessions:
        filters["_exclude_sessions"] = tuple(exclude_sessions)
    if not exclude_family:
        filters["exclude_family"] = False
    if who is None:
        filters["_exclude_who"] = tuple(sorted(
            common.SEMANTIC_DEFAULT_EXCLUDED_ROLES))
    if model and model_soft:
        filters["model_soft"] = True
    filters["_family_diverse"] = bool(family_diverse)
    sem_level = "message-session" if level == "message" else level
    import semworker
    resident = semworker.resident_status()
    if (not resident.get("running")
            and not resident.get("protected")):
        import embedder
        cached = embedder.model_cached()
        embeddings_off = common.setting("embeddings") == "off"
        bootstrap_disabled = (
            not cached and not embeddings_off and _semantic_bootstrap_disabled())
        if cached:
            launch_detail = "starting the semantic worker ..."
        elif embeddings_off:
            launch_detail = (
                "semantic model is not cached; "
                "embeddings=off prevents its download")
        elif bootstrap_disabled:
            launch_detail = (
                "semantic model is not cached; background refresh is disabled")
        elif not allow_model_download:
            launch_detail = (
                "semantic model is not cached; automatic search stays offline "
                "(`-s` or `agrep setup` permits the one-time fetch)")
        else:
            launch_detail = "fetching the semantic model (~52 MB, one-time) ..."
        (common.dbg
         if (cached or timeout_s is not None or _semantic_index_update_active()
             or bootstrap_disabled)
         else common.log)(launch_detail)
    preflight_reason = None
    preflight_retry_reason = None
    try:
        available = remaining()
        if available is not None and available <= 0:
            return None
        worker_timeout = ({}
                          if available is None else {
                              "timeout_s": available,
                              "start_timeout_s": _semantic_worker_start_timeout(
                                  available),
                          })
        wire_filters = {
            **filters,
            "_allow_model_download": bool(allow_model_download),
        }
        data = semworker.search_worker(
            q, level=sem_level, k=fetch_k, filters=wire_filters,
            **worker_timeout)
    except semworker.ResidentSemanticProtocolMismatch:
        # No inference ran, so one in-process attempt is safe;
        # the stale resident exits without a lease refresh.
        preflight_retry_reason = _SEMANTIC_WORKER_START_MISS
        data = None
    except semworker.ResidentSemanticLoopbackUnavailable as exc:
        # The sandbox denied local IPC before acceptance. The longer pass may
        # use the guarded read-only child, so this exact typed condition earns
        # one bounded recovery attempt without blessing other permissions.
        preflight_reason = str(exc)
        preflight_retry_reason = _SEMANTIC_WORKER_START_MISS
        data = None
    except semworker.ResidentSemanticPreflightUnavailable as exc:
        # The worker rejected the call before request bytes could be accepted.
        # A local attempt is safe only behind the same cross-process model-owner
        # lease used by the resident worker.
        preflight_reason = str(exc)
        if surface.semantic_status_retryable({"reason": preflight_reason}):
            preflight_retry_reason = preflight_reason
        data = None
    except semworker.ResidentSemanticUnavailable as exc:
        # Once bytes may have been accepted, never stack duplicate inference on
        # top of the resident. Accepted timeouts/disconnects are terminal.
        return _semantic_runtime_unavailable(
            q, requested, who, str(exc))
    if data is None:
        worker_disabled = semworker._worker_query_disabled()
        disabled_reason = (
            semworker._worker_query_disabled_reason()
            if worker_disabled else None)
        if disabled_reason is not None:
            preflight_reason = disabled_reason
            preflight_retry_reason = None
        elif preflight_reason is None and timeout_s is not None:
            preflight_reason = _SEMANTIC_WORKER_START_MISS
            preflight_retry_reason = preflight_reason
        constrained_fallback = bool(
            preflight_reason is not None
            or worker_disabled
            or semworker._data_dir_readonly())
        if timeout_s is not None:
            available = remaining()
            if (available is None
                    or available <= _AUTO_SEMANTIC_TIMEOUT_S + 0.10):
                # Preserve the cheap hot path. This typed pre-acceptance miss
                # opens the separately bounded recovery pass below.
                return _semantic_runtime_unavailable(
                    q, requested, who,
                    preflight_retry_reason or preflight_reason
                    or _SEMANTIC_WORKER_START_MISS)
        if timeout_s is not None or constrained_fallback:
            available = remaining()
            try:
                guarded = _guarded_semantic_local_fallback(
                    q, level=sem_level, k=fetch_k,
                    filters=filters, timeout_s=available)
            except SemanticQueryTimeoutError:
                return _semantic_runtime_unavailable(
                    q, requested, who,
                    "bounded local semantic fallback timed out")
            except SemanticQueryWorkerError as exc:
                return _semantic_runtime_unavailable(
                    q, requested, who, str(exc))
            if guarded.get("ok") is not True:
                return _semantic_runtime_unavailable(
                    q, requested, who,
                    str(guarded.get("reason")
                        or preflight_reason
                        or "bounded local semantic fallback failed"))
            data = guarded.get("data")
            if not isinstance(data, dict):
                return _semantic_runtime_unavailable(
                    q, requested, who,
                    "bounded local semantic fallback returned an invalid result")
        else:
            owner = semworker.acquire_inprocess_owner()
            if owner is None:
                status = semworker.resident_status()
                state = str(status.get("owner_state") or "unavailable")
                reason = (
                    f"semantic ownership is {state}"
                    if status.get("protected") else
                    preflight_reason
                    or "a safe in-process owner could not be acquired")
                return _semantic_runtime_unavailable(
                    q, requested, who, reason)
            semantic_module = None
            resources_released = False
            try:
                import semantic as semantic_module
                try:
                    semworker.verify_inprocess_owner(owner)
                    data = semantic_module.search(
                        q, level=sem_level, k=fetch_k,
                        filters=filters,
                        refresh_if_stale=not constrained_fallback,
                        allow_model_download=(
                            allow_model_download and not constrained_fallback),
                        diagnostic_only=constrained_fallback)
                    semworker.verify_inprocess_owner(owner)
                except semantic_module.SemanticUnavailable as exc:
                    return _semantic_runtime_unavailable(
                        q, requested, who, str(exc))
                except semworker.ResidentSemanticUnavailable as exc:
                    return _semantic_runtime_unavailable(
                        q, requested, who, str(exc))
            finally:
                if semantic_module is not None:
                    try:
                        resources_released = bool(semantic_module.release())
                    except Exception as exc:  # noqa: BLE001 -- owner remains until exit
                        common.log(
                            f"semantic release failed: {type(exc).__name__}")
                semworker.finish_inprocess_owner(
                    owner, resources_released=resources_released)
    timing_value = os.environ.get("AGREP_SEM_TIMING", "")
    if (common.DEBUG or timing_value.lower() not in ("", "0", "false", "no", "off")):
        timing = data.get("_semantic_timing")
        if isinstance(timing, dict):
            common.timing_trace("semantic timing", timing)
    rows = []
    invalid_turns = 0
    for raw in data.get("results") or []:
        turn = raw.get("turn")
        if type(turn) is not int or turn < 0 or not raw.get("session"):  # noqa: E721
            invalid_turns += 1
            continue
        rows.append(raw)
    coverage = data.get("semantic_coverage")
    accelerator_coverage = data.get("semantic_accelerator_coverage")
    partial = bool(data.get("partial"))
    score_kind = data.get("score_kind") or "cosine"
    policy = semantic_result_policy(
        q, rows, requested=requested, explicit_who=who,
        coverage=coverage, partial=partial, score_kind=score_kind)
    integrity = data.get("semantic_integrity")
    if data.get("semantic_unavailable") and isinstance(integrity, dict):
        policy["semantic_status"].update(
            state=str(integrity.get("state") or "unavailable"),
            reason=str(integrity.get("reason") or "semantic integrity failure"),
            complete=False,
            fallback_recommended=True,
        )
    filtered = policy["semantic_status"].get("filtered")
    if isinstance(filtered, dict):
        filtered["invalid"] = int(filtered.get("invalid") or 0) + invalid_turns
    # Only a lane that served rows gets probed: nothing else can mislabel.
    anchor = (semantic_corpus_anchor(q, filters) if policy["results"] else None)
    if anchor is not None:
        policy["semantic_status"]["corpus_anchor"] = anchor
    unanchored = bool(anchor and anchor.get("anchored") is False)
    hits = []
    for o in policy["results"]:
        turn = o["turn"]
        snip = o.get("text") or o.get("title") or (o.get("summary") or "")[:140]
        semantic_source = o.get("semantic_source", level)
        hits.append({"session": o.get("session", ""), "agent": o.get("agent", ""),
                     "project": o.get("project", "") or o.get("cwd_project", ""),
                     "concept": o.get("concept", ""),
                     "model": o.get("model", ""),
                     "model_source": o.get("model_source") or (
                         "summary" if semantic_source == "summary" and o.get("model")
                         else "unknown"),
                     "turn": turn, "ts": o.get("ts", 0), "who": o.get("who", ""),
                     "sem_score": o.get("score"),
                     "score_kind": o.get("score_kind") or data.get("score_kind") or "cosine",
                     "content_digest": o.get("content_digest"),
                     "snippet": snip, "summary": o.get("summary", ""),
                     "semantic_source": semantic_source,
                     "semantic_partial": partial,
                     "_sem_unanchored": unanchored,
                     "semantic_coverage": coverage,
                     "semantic_accelerator_coverage": accelerator_coverage})
    policy_truncated = bool(policy["semantic_status"].get("truncated"))
    result = {"hits": hits, "total": len(hits),
            "chats": len({h["session"] for h in hits}),
            # No corpus-wide semantic count exists: filling k means more may exist;
            # a confidence-short page is exact (omitted neighbors ranked below the tail).
            # requested==0 means uncapped: the worker's own cap IS truncation
            "truncated": bool(policy_truncated or (
                data.get("truncated")
                and (not requested or len(hits) >= requested))),
            "semantic_coverage": coverage,
            "semantic_accelerator_coverage": accelerator_coverage,
            "partial": partial,
            "score_kind": score_kind,
            # rows the meaning lane refused to trust: the page is short by
            # exactly this many, and the reader has to know before judging
            "semantic_integrity": integrity,
            "semantic_status": policy["semantic_status"],
            "fallback_recommended": bool(
                policy["semantic_status"].get("fallback_recommended"))}
    return result


def _semantic_result_incomplete(result: dict | None) -> bool:
    """Whether semantic absence is bounded by incomplete evidence."""
    if not result:
        return False
    if result.get("self_exclusion_more_unknown"):
        return True
    if result.get("partial"):
        return True
    for field in ("semantic_coverage", "semantic_accelerator_coverage"):
        coverage = result.get(field)
        if isinstance(coverage, dict) and coverage.get("complete") is False:
            return True
    status = result.get("semantic_status")
    if isinstance(status, dict) and status.get("complete") is False:
        return True
    integrity = result.get("semantic_integrity")
    if isinstance(integrity, dict):
        if integrity.get("state") == "generation-rejected":
            return True
        try:
            if int(integrity.get("dropped") or 0) > 0:
                return True
        except (TypeError, ValueError):
            return True
    return False


def _parse_when(s: str) -> int:
    """A --since/--until value -> epoch ms (the unit hits store ts in). Accepts a
    relative age (`90s` `30m` `24h` `7d` `2w` = that long ago) or an absolute local
    `YYYY-MM-DD`, optionally with ` HH:MM[:SS]`."""
    s = s.strip().lower()
    m = re.fullmatch(r"(\d+)\s*([smhdw])", s)
    if m:
        per = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
        return int((time.time() - int(m.group(1)) * per) * 1000)
    import datetime
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.datetime.strptime(s, fmt).timestamp() * 1000)
        except ValueError:
            pass
    common.log(f"bad time {s!r} - use 7d / 24h / 2w / 30m or YYYY-MM-DD")
    raise SystemExit(2)


def _disambiguation_hint(cands: list[str], shown: int) -> str:
    """The shortest prefix growth that singles out each listed ambiguous candidate."""
    ordered = compact.session_prefix_index(cands)
    return " / ".join(session[:compact._needed_prefix(session, ordered)]
                      for session in cands[:shown])


def _resolve_chat(sess_q: str) -> str | None:
    """UUIDv7 ids share their leading chars across sessions started the same minute,
    so a --chat prefix must resolve to exactly one full session id before filtering.
    Zero or several matches log why and exit 2, mirroring `agrep around`."""
    import explore
    try:
        sess_q = compact.normalize_session_arg(sess_q)
    except compact.CompactError as exc:
        common.log(str(exc))
        return None
    cands = explore.resolve_session(sess_q)
    if len(cands) == 1:
        return cands[0]
    if not cands:
        common.log(f"no session matches {terminal_safe(sess_q)!r} - "
                   "ids come from `agrep <pattern> --json`.")
        return None
    common.log(f"{terminal_safe(sess_q)!r} is ambiguous ({len(cands)} sessions):")
    for s in cands[:10]:
        common.log(f"  {terminal_safe(s)}")
    common.log(f"add a char: {terminal_safe(_disambiguation_hint(cands, 10))}")
    return None


def _excluding_project(hits: list[dict],
                       exclude_project: str | None) -> list[dict]:
    """--exclude-project: --project's substring semantics, negated. Applied to
    full candidate sets before ranking/top-k, never as a page post-filter."""
    if not exclude_project:
        return hits
    needle = exclude_project.lower()
    return [h for h in hits if needle not in (h.get("project") or "").lower()]


def _filtered(hits: list[dict], agent: str | None, project: str | None,
              who: str | None, model: str | None, model_soft: bool,
              chat: str | None = None, since_ms: int | None = None,
              until_ms: int | None = None, include_tools: bool = True,
              exclude_session: str | None = None,
              exclude_session_from_turn: int | None = None,
              exclude_sessions: tuple[str, ...] = (),
              exclude_project: str | None = None) -> list[dict]:
    out = _excluding_project(hits, exclude_project)
    if exclude_sessions:
        exact = frozenset(exclude_sessions)
        out = [hit for hit in out if hit.get("session") not in exact]
    if exclude_session:
        if exclude_session_from_turn is None:
            out = [h for h in out if h.get("session") != exclude_session]
        elif type(exclude_session_from_turn) is int:  # noqa: E721
            boundary = exclude_session_from_turn

            def outside_window(hit: dict) -> bool:
                if hit.get("session") != exclude_session:
                    return True
                turn = hit.get("turn")
                return type(turn) is not int or turn < boundary  # noqa: E721

            out = [h for h in out if outside_window(h)]
    if agent:
        ag = agent.lower()
        out = [h for h in out if ag in (h.get("agent") or "").lower()]
    if project:
        pr = project.lower()
        out = [h for h in out if pr in (h.get("project") or "").lower()]
    if chat:  # 8-char id prefix (what the UI shows) or a full session uuid
        c = chat.lower()
        out = [h for h in out if (h.get("session") or "").lower().startswith(c)]
    if who is not None:
        out = [h for h in out
               if surface.speaker_filter_admits(who, h.get("who") or "")]
    elif not include_tools:
        out = [h for h in out if h.get("who") != "tool"]
    if model:
        needle = model.lower()
        if model_soft:
            out = [h for h in out if needle in (h.get("model") or "").lower()]
        else:
            out = [h for h in out if needle == (h.get("model") or "").lower()]
    if since_ms is not None:
        out = [h for h in out if (h.get("ts") or 0) >= since_ms]
    if until_ms is not None:
        out = [h for h in out if (h.get("ts") or 0) < until_ms]
    return out


# who-prior: user phrasing is the best recall anchor; tool echoes must only win
# as sole holder of the queried string.
_WHO_W = {"user": 1.0, "subagent": 0.85, "agent": 0.8, "tool": 0.4}
_SOURCE_SCALE = {"subagent": 0.85, "recap": 0.65, "control": 0.65,
                 "synthetic": 0.60, "harness": 0.60, "tool": 0.55}
_RECENCY_HALF_LIFE_DAYS = 14.0
_META_SCALE = 0.45
_HISTORY_META_LOOKBACK = 4
_HISTORY_META_ROWS_PER_QUERY = 40


def _meta_row(hit: dict) -> bool:
    """A row whose provenance is tooling-about-history, not history.

    Stored machinery roles and spawned task prompts are authoritative here.
    History-command lineage is attached later from its bounded event window."""
    if hit.get("_meta_row") is True:
        return True
    if hit.get("who") in ("harness", "synthetic"):
        return True
    if hit.get("who") == "subagent" and hit.get("turn") == 0:
        return True
    return False


def _meta_query_index(hit: dict) -> int:
    value = hit.get("_recall_query", 0)
    return value if type(value) is int and value >= 0 else 0


def _history_meta_candidates(hits: list[dict]) -> list[dict]:
    selected = []
    for hit in hits:
        who = hit.get("who")
        turn = hit.get("turn")
        session = hit.get("session")
        if (who not in ("agent", "subagent", "tool")
                or type(turn) is not int or type(session) is not str
                or not session):
            continue
        if hit.get("_direct_handle") is True and who != "tool":
            continue
        selected.append(hit)
    return selected


def _history_event_identity(session: str, event: dict) -> str | None:
    turn = event.get("turn")
    ts = event.get("ts")
    if type(turn) is not int or type(ts) is not int:
        return None
    row = common.tool_row_from_event(event, ts, turn)
    if row is None:
        return None
    return common.tool_event_identity(session, turn, ts, row.get("text"))


def _mark_history_meta(
        hits: list[dict], queries: list[str],
) -> dict[tuple[str, int], dict]:
    """Attach bounded command-lineage evidence and return reusable windows."""
    roots = _family_roots_for_hits(hits)
    for hit in hits:
        session = str(hit.get("session") or "")
        if session:
            root = roots.get(session, session)
            hit["_family_root"] = root
            if root != session:
                hit["_sidechain"] = True
            else:
                hit.pop("_sidechain", None)
        if _meta_row(hit):
            hit["_meta_row"] = True
    candidates = _history_meta_candidates(hits)
    requests = []
    request_keys = []
    seen = set()
    for hit in candidates:
        key = (str(hit["session"]), int(hit["turn"]))
        if key in seen:
            continue
        seen.add(key)
        request_keys.append(key)
        requests.append((*key, _HISTORY_META_LOOKBACK))
    if not requests:
        return {}
    try:
        import explore
        windows = explore.get_windows(requests)
    except (OSError, RuntimeError, sqlite3.DatabaseError, TypeError, ValueError) as exc:
        common.dbg(f"history provenance unavailable: {exc}")
        return {}
    cached = {
        key: window for key, window in zip(request_keys, windows)
        if isinstance(window, dict) and "error" not in window
    }
    for hit in candidates:
        key = (str(hit["session"]), int(hit["turn"]))
        window = cached.get(key)
        query_index = _meta_query_index(hit)
        if window is None or not 0 <= query_index < len(queries):
            continue
        query = queries[query_index]
        if hit.get("who") == "tool":
            identity = hit.get("_event_identity")
            if type(identity) is not str:
                continue
            for event in window.get("events") or []:
                if (event.get("turn") == hit["turn"]
                        and _history_event_identity(key[0], event) == identity
                        and display_policy.history_read_invocation(event)):
                    hit["_meta_row"] = True
                    break
            continue
        floor = int(hit["turn"]) - _HISTORY_META_LOOKBACK
        if any(
                type(event.get("turn")) is int
                and floor <= event["turn"] <= hit["turn"]
                and display_policy.history_read_invoked(event, query)
                for event in window.get("events") or []):
            hit["_meta_row"] = True
    return cached


def _filter_meta_rows(hits: list[dict]) -> tuple[list[dict], int, int]:
    """Drop marked rows unless one is a query's only surviving evidence."""
    query_has_lived = {
        _meta_query_index(hit) for hit in hits if not _meta_row(hit)
    }
    retained_queries = set()
    kept = []
    dropped = retained = 0
    for hit in hits:
        if not _meta_row(hit):
            kept.append(hit)
            continue
        query_index = _meta_query_index(hit)
        if (query_index not in query_has_lived
                and query_index not in retained_queries):
            hit["_meta_row"] = True
            kept.append(hit)
            retained_queries.add(query_index)
            retained += 1
        else:
            dropped += 1
    return kept, dropped, retained
# 0.35: above the fresh recap (0.325) and tool (0.22) score ceilings, below fresh agent prose (0.8).
_USER_REC_FLOOR = 0.35
# -w/-E state explicit user intent: an old exact hit must never be buried under fresh weak rows.
_EXPLICIT_REC_FLOOR = 0.5
# Below this candidate count the exhaustive path is as fast or faster; tests force 0.
_BOUNDED_KEYWORD_MIN_CANDIDATES = 1000
# Past this posting-list size, aggregate totals in SQLite and retain only head-eligible
# rows; the bounded result cap keeps an accidental ``-n 100000`` on the exhaustive path.
_BOUNDED_ROW_MIN_CANDIDATES = 5000
_BOUNDED_ROW_MAX_RESULTS = 512


def _prepare_boundary(q: str, mode: str, db=None):
    if mode != "keyword":
        return None
    raw_terms = boundary_rank.query_tokens(q)
    normalized = [boundary_rank.normalize_token(token) for token in raw_terms]
    stats = (corpusdb.boundary_token_stats(db, normalized)
             if db is not None and corpusdb is not None else {})
    prepared = boundary_rank.prepare_query(q, stats)
    if not raw_terms or len(prepared.terms) != len(raw_terms):
        return prepared, None, q, stats
    capture = re.compile(
        r"[\W_]*".join(f"({re.escape(token)})" for token in raw_terms), re.I)
    return prepared, capture, q, stats


_CUT = "…"  # the snipper's ellipsis


def _decut(snippet: str) -> str:
    """Clone each cut marker's neighbor so a snip edge cannot mint a word boundary:
    an occurrence the window truncated grades interior/unknown, never aligned."""
    if _CUT not in snippet:
        return snippet
    chars = list(snippet)
    last = len(chars) - 1
    for i, ch in enumerate(chars):
        if ch != _CUT:
            continue
        left = chars[i - 1] if i else ""
        right = chars[i + 1] if i < last else ""
        if left and left not in (" ", _CUT):
            chars[i] = left
        elif right and right not in (" ", _CUT):
            chars[i] = right
        else:
            chars[i] = " "
    return "".join(chars)


def _boundary_score(h: dict, context):
    prepared, capture = context[:2]
    snippet = h.get("snippet") or ""
    # Grade against the de-cut text: window edges and stitch joints are presentation,
    # not evidence, and must never grade better than the original row would.
    text = _decut(snippet)
    if h.get("matched") == "all-terms":
        if text is not snippet:
            # 0/len always count as boundaries; pad surviving cut ends so a cut cannot grade as one
            text = ((text[0] if snippet.startswith(_CUT) else "") + text
                    + (text[-1] if snippet.endswith(_CUT) else ""))
        return prepared.evaluate(text)
    if h.get("matched") == "content-terms" or capture is None:
        return None
    scored = []
    for match in capture.finditer(snippet):
        spans = tuple(match.span(index) for index in range(1, len(prepared.terms) + 1))
        value = prepared.evaluate(text, spans=spans, validate_spans=False)
        scored.append((value.factor, -(match.end() - match.start()), value))
    return max(scored, key=lambda item: item[:2], default=(0.0, 0, None))[2]


# Rust pins the evaluator tables to Unicode 16; Python still owns tokenization and
# span extraction. A query must never mix the native and Python evaluators.
_NATIVE_BOUNDARY_MIN_ITEMS = 1
_NATIVE_BOUNDARY_BATCH = 100_000
_NATIVE_BOUNDARY_PROTOCOL = 2
_SHORT_BOUNDARY_SEED = 4096
_SHORT_BOUNDARY_REFRESH = 8192
# A conservative skew gate avoids restart churn when broad hits span many families.
_SHORT_DOMINANCE_MIN_CANDIDATES = 32
_SHORT_DOMINANCE_RATIO = 8
_SHORT_FAMILY_TRACK_MAX = 4096
_NATIVE_BOUNDARY_IDENTITY = None
_NATIVE_BOUNDARY_AVAILABLE = None


def _native_boundary_identity(path) -> tuple | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return str(path), stat.st_size, stat.st_mtime_ns


def _native_boundary_items(hits: list[dict], context):
    prepared, capture = context[:2]
    # hoisted once: this generator runs per hit on 50k-row machine surfaces
    span_indexes = range(1, len(prepared.terms) + 1)
    finditer = None if capture is None else capture.finditer
    for hit in hits:
        snippet = hit.get("snippet") or ""
        matched = hit.get("matched")
        if matched == "all-terms":
            yield {"text": snippet}, (hit, 0)
            continue
        if matched == "content-terms" or finditer is None:
            continue
        for match in finditer(snippet):
            span = match.span
            spans = [list(span(index)) for index in span_indexes]
            yield ({"text": snippet, "spans": spans, "validate_spans": False},
                   (hit, match.end() - match.start()))


def _certifiable_ascii_boundary(text: str, index: int) -> bool:
    if not boundary_rank._ascii_boundary(text, index):
        return False
    return (index in (0, len(text))
            or not (text[index - 1].isalnum() and text[index].isalnum()))


def _certify_ascii_aligned_phrases(
        hits: list[dict], context,
) -> tuple[list[dict], int]:
    """Set the maximum boundary score only when every token edge is plain ASCII."""
    if context is None or len(context) < 2:
        return hits, 0
    prepared, capture = context[:2]
    if (capture is None or not prepared.terms
            or any(not term.folded.isascii() for term in prepared.terms)):
        return hits, 0
    indexes = range(1, len(prepared.terms) + 1)
    unresolved = []
    certified = 0
    for hit in hits:
        text = hit.get("snippet") or ""
        aligned = False
        if (text.isascii()
                and hit.get("matched") not in ("all-terms", "content-terms")):
            aligned = any(
                all(
                    _certifiable_ascii_boundary(text, edge)
                    for index in indexes
                    for edge in match.span(index)
                )
                for match in capture.finditer(text)
            )
        if not aligned:
            unresolved.append(hit)
            continue
        hit["_boundary_class"] = "aligned"
        hit["_boundary_score_factor"] = 1.0
        hit["_boundary_factor"] = 1.0
        certified += 1
    return unresolved, certified


def _boundary_worker_request(worker, payload: str) -> dict:
    expired = threading.Event()

    def stop_worker() -> None:
        expired.set()
        try:
            worker.kill()
        except OSError:
            pass

    timer = threading.Timer(10.0, stop_worker)
    timer.daemon = True
    timer.start()
    try:
        if worker.stdin is None or worker.stdout is None:
            raise RuntimeError("boundary-rank worker has no pipes")
        worker.stdin.write(payload + "\n")
        worker.stdin.flush()
        raw = worker.stdout.readline()
    finally:
        timer.cancel()
        timer.join()
    if not raw:
        reason = "timed out" if expired.is_set() else "closed its output"
        raise RuntimeError(f"boundary-rank worker {reason}")
    return json.loads(raw)


def _start_boundary_worker(path):
    return subprocess.Popen(
        [str(path), "boundary-rank", "--serve"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="strict", bufsize=1,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if common.WIN else {}))


def _close_boundary_worker(state: list | None) -> None:
    if not state or len(state) < 3 or state[2] is None:
        return
    worker = state[2]
    state[2] = None
    try:
        if worker.stdin is not None:
            worker.stdin.close()
        worker.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        try:
            worker.kill()
            worker.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass
    finally:
        for stream in (worker.stdout, worker.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass


def _native_boundary_scores(hits: list[dict], context, *, worker=None) -> bool:
    """Attach certified or batch-evaluated factors; unresolved failures stay clean."""
    global _NATIVE_BOUNDARY_IDENTITY, _NATIVE_BOUNDARY_AVAILABLE
    if context is None or len(context) < 4:
        return False
    unresolved, certified = _certify_ascii_aligned_phrases(hits, context)
    if not unresolved:
        return bool(certified)
    path = common.ingest_bin()
    identity = _native_boundary_identity(path)
    if identity != _NATIVE_BOUNDARY_IDENTITY:
        _NATIVE_BOUNDARY_IDENTITY = identity
        _NATIVE_BOUNDARY_AVAILABLE = None
    if identity is None or _NATIVE_BOUNDARY_AVAILABLE is False:
        return False
    stream = iter(_native_boundary_items(unresolved, context))
    chunk_size = max(_NATIVE_BOUNDARY_MIN_ITEMS, _NATIVE_BOUNDARY_BATCH)
    chunk = list(itertools.islice(stream, chunk_size))
    if len(chunk) < _NATIVE_BOUNDARY_MIN_ITEMS:
        return False

    _prepared, _capture, query, stats = context
    best: dict[int, tuple] = {}
    total_items = 0
    started = time.perf_counter()
    try:
        while chunk:
            items = [item for item, _owner in chunk]
            owners = [owner for _item, owner in chunk]
            request = {
                "protocol": _NATIVE_BOUNDARY_PROTOCOL,
                "query": query,
                "stats": stats,
                "compact": True,
                "decut": True,
                "items": items,
            }
            payload = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
            if worker is None:
                proc = subprocess.run(
                    [str(path), "boundary-rank"], input=payload,
                    capture_output=True, text=True, encoding="utf-8", errors="strict",
                    timeout=10,
                    **({"creationflags": subprocess.CREATE_NO_WINDOW} if common.WIN else {}))
                if proc.returncode != 0:
                    raise RuntimeError(proc.stderr.strip() or f"exit {proc.returncode}")
                response = json.loads(proc.stdout)
            else:
                response = _boundary_worker_request(worker, payload)
            batch = (response.get("results")
                     if response.get("protocol") == _NATIVE_BOUNDARY_PROTOCOL else None)
            if not isinstance(batch, list) or len(batch) != len(request["items"]):
                raise ValueError("invalid boundary-rank response")
            for owner, result in zip(owners, batch):
                hit, match_length = owner
                if not isinstance(result, dict) or "error" in result:
                    raise ValueError("boundary-rank item error")
                factor = float(result.get("factor"))
                match_class = result.get("match_class")
                if (not math.isfinite(factor) or not 0.0 <= factor <= 1.0
                        or match_class not in ("aligned", "partial", "interior")):
                    raise ValueError("invalid boundary-rank score")
                key = factor, -match_length
                current = best.get(id(hit))
                if current is None or key > current[:2]:
                    best[id(hit)] = (factor, -match_length, match_class, hit)
            total_items += len(chunk)
            del request, items, owners, batch, response, payload
            chunk.clear()
            chunk = list(itertools.islice(stream, chunk_size))
        for factor, _length, match_class, hit in best.values():
            hit["_boundary_class"] = match_class
            hit["_boundary_score_factor"] = factor
            hit["_boundary_factor"] = round(factor, 6)
        _NATIVE_BOUNDARY_AVAILABLE = True
        common.dbg(
            f"boundary: Rust scored {total_items} occurrence(s) in "
            f"{(time.perf_counter() - started) * 1000:.1f}ms")
        return True
    except (OSError, subprocess.SubprocessError, RuntimeError, TypeError, ValueError,
            AttributeError, json.JSONDecodeError) as exc:
        _NATIVE_BOUNDARY_AVAILABLE = False
        common.dbg(f"boundary: native scorer unavailable ({exc}); using Python", "!")
        return False


class _NativeBoundaryRestart(RuntimeError):
    pass


def _boundary_batch(rows: list[dict], context, state: list) -> bool:
    global _NATIVE_BOUNDARY_AVAILABLE, _NATIVE_BOUNDARY_IDENTITY
    if context is None or state[0] == "python":
        return False
    unresolved, certified = _certify_ascii_aligned_phrases(rows, context)
    if not unresolved:
        return bool(certified)
    if len(state) < 3:
        state.append(None)
    if state[2] is None:
        path = common.ingest_bin()
        identity = _native_boundary_identity(path)
        if identity != _NATIVE_BOUNDARY_IDENTITY:
            _NATIVE_BOUNDARY_IDENTITY = identity
            _NATIVE_BOUNDARY_AVAILABLE = None
        if identity is None or _NATIVE_BOUNDARY_AVAILABLE is False:
            state[:] = ["python", None, None]
            return False
        try:
            state[2] = _start_boundary_worker(path)
        except OSError:
            state[:] = ["python", None, None]
            return False
    native = _native_boundary_scores(rows, context, worker=state[2])
    if native:
        identity = _NATIVE_BOUNDARY_IDENTITY
        if state[0] == "native" and state[1] != identity:
            _close_boundary_worker(state)
            raise _NativeBoundaryRestart("native boundary binary changed mid-query")
        state[0], state[1] = "native", identity
        return True
    _close_boundary_worker(state)
    if state[0] == "native":
        raise _NativeBoundaryRestart("native boundary scorer failed mid-query")
    state[:] = ["python", None, None]
    return False


def _terms_proximity(snippet: str, terms: list[str], qlen: int) -> float:
    """Match strength for scattered-terms rows. The stitched snippet projects the
    engine's per-term spans, so grade their spread: clustered terms outrank a
    row-wide scatter. Every cut marker stands in for >=80 clipped characters."""
    found = 0
    first = last = 0
    for t in terms:
        span = common.insensitive_span(snippet, t)
        if span is None:
            continue
        p, end = span
        if not found:
            first, last = p, end
        else:
            first = min(first, p)
            last = max(last, end)
        found += 1
    frac = found / len(terms)
    if found < 2:
        return frac
    spread = last - first + 80 * snippet.count(_CUT, first, last)
    return frac * (0.5 + 0.5 * min(1.0, qlen / spread))


def _score(h: dict, pat: re.Pattern | None, qlen: int, now_ms: float,
           terms: list[str] | None = None, boundary=None,
           rec_floor: float = 0.0) -> float:
    """Bounded tightness, recency, speaker, source, and boundary evidence."""
    match = 1.0  # semantic: the vector engine already judged relevance
    if pat is not None:
        snippet = h.get("snippet") or ""
        best = n = 0
        if len(snippet) >= qlen:
            for m in pat.finditer(snippet):
                n += 1
                # span arithmetic == len(m.group(0)) without materializing the
                # matched substring; this loop runs once per hit at corpus scale
                length = m.end() - m.start()
                if not best or length < best:
                    best = length
        # tightness = minimal-possible-length / tightest actual match: compact exact = 1.0, gappy scores lower
        tight = min(1.0, qlen / best) if (n and qlen and best) else (1.0 if n else 0.0)
        match = tight * (1.0 - 0.5 ** n)
        if terms and h.get("matched") in ("all-terms", "content-terms"):
            # Fallback hits: the phrase pattern usually re-finds nothing; the signal is
            # engine coverage (idf) or the projected spread of the engine's term spans.
            # max() keeps in-snippet phrase re-finds at full strength.
            if h.get("coverage") is not None:
                match = max(match, h["coverage"])
            else:
                match = max(match, _terms_proximity(snippet, terms, qlen))
    age_days = max(0.0, now_ms - (h.get("ts") or 0)) / 86_400_000
    rec = 0.5 ** (age_days / _RECENCY_HALF_LIFE_DAYS)
    speaker = h.get("who") or ""
    if speaker == "user":
        rec = max(rec, _USER_REC_FLOOR)  # human evidence never ages below generated rows
    if rec_floor:
        rec = max(rec, rec_floor)
    who = _WHO_W.get(speaker, 0.5)
    boundary_factor = 1.0
    if "_boundary_score_factor" in h:
        boundary_factor = float(h["_boundary_score_factor"])
    elif "_boundary_factor" in h:
        boundary_factor = float(h["_boundary_factor"])
    elif boundary is not None:
        quality = _boundary_score(h, boundary)
        if quality is not None:
            boundary_factor = quality.factor
            h["_boundary_class"] = quality.match_class
            h["_boundary_factor"] = round(quality.factor, 6)
    meta = _META_SCALE if _meta_row(h) else 1.0
    if meta != 1.0:
        h["_meta_row"] = True
    # The lexical half of the score, kept separate from the recency/speaker
    # weighting: only this half says anything about relevance, and a one-line
    # pointer has to judge relevance without a page to hedge on.
    h["_evidence"] = round(match * boundary_factor, 6)
    return (match * rec * who * boundary_factor
            * _SOURCE_SCALE.get(speaker, 1.0) * meta)


_RANK_CLASS = {"all-terms": 1, "content-terms": 2}


def _rank_key(h: dict) -> tuple:
    """The exact structural/relevance/tie order shared by exhaustive and bounded paths."""
    turn = h.get("turn")
    return (_RANK_CLASS.get(h.get("matched") or "", 0), -h["score"],
            -(h.get("ts") or 0), h["session"], -1 if turn is None else turn,
            h.get("who") or "")


def _rank(hits: list[dict], q: str, mode: str, sort: str, boundary=None,
          *, refine_all: bool = False, top_k: int | None = 40,
          now_ms: float | None = None,
          try_native_boundary: bool = True) -> list[dict]:
    """Order hits for display and attach h['score'] (bounded; --json consumers that
    ignore it are unaffected). score: the heuristic, desc. time: pure recency.
    position: the engine's (session, turn) order untouched - the pre-ranking behavior.
    Semantic keeps engine order except under time (the engine already ranks by meaning).

    all-terms (bag-of-words fallback) hits sort as a class BELOW every phrase hit under
    score - a structural guarantee (not a probabilistic score margin) that any exact
    phrase match always outranks any scattered-terms one, held for the 'or few' future
    when the two would mix. Time sort respects pure recency (the user asked for it)."""
    pat = _match_pat(q, mode)
    # the shortest string the query could match: separators squeezed out
    qlen = len("".join(re.split(r"[\s\-_]+", q.strip()))) if mode in ("keyword", "word") else 0
    terms = (list(dict.fromkeys(
                t.lower() for t in re.split(r"[\s\-_]+", q.strip()) if t))
             if mode == "keyword" else None)
    if boundary is None:
        boundary = _prepare_boundary(q, mode)
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    # -w/-E carry explicit user intent; age alone must not bury an exact match
    rec_floor = _EXPLICIT_REC_FLOOR if mode in ("word", "regex") else 0.0
    for hit in hits:
        hit.pop("_boundary_class", None)
        hit.pop("_boundary_factor", None)
        hit.pop("_boundary_score_factor", None)
        hit.pop("_evidence", None)

    def score_rows(rows: list[dict], evidence, *, try_native: bool = True) -> bool:
        native = bool(evidence is not None and try_native and try_native_boundary
                      and _native_boundary_scores(rows, evidence))
        scorer = None if native else evidence
        for hit in rows:
            hit["score"] = round(
                _score(hit, pat, qlen, now_ms, terms=terms, boundary=scorer,
                       rec_floor=rec_floor), 4)
        return native

    if boundary is None:
        score_rows(hits, None)
    elif refine_all or not top_k:
        score_rows(hits, boundary)
    elif sort != "score":
        score_rows(hits, None)
        if sort == "time":
            visible = heapq.nsmallest(top_k, hits, key=lambda hit: (
                -(hit.get("ts") or 0), hit["session"],
                -1 if hit.get("turn") is None else hit["turn"], hit.get("who") or ""))
        else:
            visible = hits[:top_k]
        score_rows(visible, boundary)
    else:
        score_rows(hits, None)
        native = bool(try_native_boundary
                      and _native_boundary_scores(hits, boundary))
        if native:
            score_rows(hits, None, try_native=False)
            refined = len(hits)
        else:
            remaining = top_k
            refined = 0
            for lane in (0, 1, 2):
                lane_hits = [hit for hit in hits if _rank_key(hit)[0] == lane]
                if not lane_hits or remaining <= 0:
                    continue
                target = min(remaining, len(lane_hits))
                ordered = sorted(lane_hits, key=_rank_key)
                offset = 0
                while offset < len(ordered):
                    batch = ordered[offset:offset + _BOUNDARY_REFINE_POOL]
                    score_rows(batch, boundary, try_native=False)
                    refined += len(batch)
                    offset += len(batch)
                    if offset >= target:
                        kth = heapq.nsmallest(target, ordered[:offset], key=_rank_key)[-1]
                        if (offset == len(ordered)
                                or ordered[offset]["score"] < kth["score"]):
                            break
                remaining -= target
        engine = "native" if native else "Python"
        common.dbg(f"boundary: adaptive {engine} refinement covered "
                   f"{refined}/{len(hits)} row(s)")

    def tie(h):  # deterministic: ts desc, then session, turn, who
        t = h.get("turn")
        return (-(h.get("ts") or 0), h["session"], -1 if t is None else t, h.get("who") or "")

    if sort == "time":
        hits.sort(key=tie)
    elif sort == "score" and mode != "semantic":
        hits.sort(key=_rank_key)
    return hits


# phrase hits confirmed before early termination may activate;
# pure perf knob - completeness never depends on it.
_EARLY_STOP_MIN_PHRASES = 4

# Boundary scoring advances in bounded waves until the next B=1 ceiling is strictly
# below the exact page frontier; diagnostics still request full refinement explicitly.
_BOUNDARY_REFINE_POOL = 2000

# High-frequency connective terms used only by the last-resort content fallback.
_STOP = frozenset(
    "a an and are as at be been but by can could did do does for from had has have how"
    " i if in is it its me my not of on or our should so that the their them they this"
    " these those to was we were what when where which who why will with would you your"
    " whats what's im i'm dont don't isnt isn't didnt didn't".split())


def _content_terms(q: str) -> list[str]:
    """Terms for the last-resort tier serving natural-language queries ("What
    degree did I graduate with?"): both stricter tiers AND every raw token, so
    one stopword or a trailing '?' zeroes the whole query. When both come back
    empty, retry with these content words only - still AND, still exact - and
    demote the results a class below all-terms."""
    import string
    toks = [t.strip(string.punctuation) for t in re.split(r"[\s\-_]+", q.strip())]
    return [t for t in toks if len(t) >= 2 and t.lower() not in _STOP]


def _nl_query(q: str, ct: list[str]) -> bool:
    """Should the content-terms tier fire? Only for something shaped like language:
    real whitespace (an identifier like zzqx_no_such_term splits on _ but is ONE
    grep pattern to its author - it must stay exit-1) and evidence the stripping
    did something (a stopword or punctuation removed). Bare keyword bags keep
    strict AND semantics - that's the grep contract."""
    raw = [t for t in re.split(r"[\s\-_]+", q.strip()) if t]
    return " " in q.strip() and bool(ct) and [t.lower() for t in ct] != [t.lower() for t in raw]


def _auto_semantic_query(query: str) -> bool:
    """Admit prose while leaving identifier-shaped grep requests lexical."""
    if common.setting("embeddings") == "off":
        return False
    if not semantic_query_policy(query)["eligible"]:
        return False
    natural = support = 0
    for token in query.split():
        cleaned = token.strip(".,;:!?()[]{}<>\"'")
        letters = "".join(re.findall(r"[A-Za-z]", cleaned))
        if not cleaned or cleaned.lower() in _STOP:
            continue
        token_policy = semantic_query_policy(cleaned)
        if token_policy["eligible"] and len(letters) >= 4:
            natural += 1
        elif ((letters and letters.isupper() and 2 <= len(letters) <= 6)
              or token_policy.get("reason") in
              ("identifier-query", "non-language-query")):
            support += 1
    return natural >= 2 or (natural >= 1 and support >= 1)


_AUTO_SEMANTIC_ROWS = 3
_AUTO_SEMANTIC_FETCH = 8
# Windows resident work reaches 1.04s and cold worker discovery reaches 0.43s.
# Headroom preserves meaning without charging the faster macOS failure path.
_AUTO_SEMANTIC_TIMEOUT_S = 1.25 if common.WIN else 0.75
_AUTO_SEMANTIC_START_S = 0.50 if common.WIN else 0.35


def _semantic_worker_start_timeout(available_s: float) -> float:
    """Keep the first automatic attempt cheap, but let recovery join startup.

    The initial optional lane owns only ``_AUTO_SEMANTIC_TIMEOUT_S`` and keeps
    the reviewed short discovery budget.  A typed pre-acceptance miss opens the
    separately bounded recovery pass; that pass must be able to wait for the
    resident worker it just launched instead of racing it again for 350 ms and
    then contending with it through the guarded local fallback.
    """
    available = max(0.0, float(available_s))
    if available <= _AUTO_SEMANTIC_TIMEOUT_S + 0.10:
        return min(available, _AUTO_SEMANTIC_START_S)
    import semworker
    return min(available, semworker.START_TIMEOUT_S)


def _hybrid_text_key(hit: dict) -> str:
    raw = hit.get("snippet") or hit.get("text") or ""
    return " ".join(str(raw).lower().split())


def _weak_lexical_hit(hit: dict) -> bool:
    return (hit.get("matched") in ("all-terms", "content-terms")
            or hit.get("_boundary_class") == "interior")


def _family_roots_for_hits(hits: list[dict]) -> dict[str, str]:
    """Resolve only families present in a result set, preserving pinned roots."""
    roots = {
        str(hit.get("session") or ""): str(hit["_family_root"])
        for hit in hits
        if hit.get("session") and hit.get("_family_root")
    }
    missing = {
        str(hit.get("session") or "") for hit in hits
        if hit.get("session") and str(hit.get("session")) not in roots
    }
    indexed = common.indexed_family_roots(missing)
    if indexed is not None:
        roots.update(indexed)
    for session in missing:
        roots.setdefault(session, session)
    return roots


def _merge_auto_semantic_hits(keyword_hits: list[dict], semantic_hits: list[dict],
                              limit: int, *, family_diverse: bool = True) -> list[dict]:
    """Interleave labeled lanes from lexical quality, never from hit count."""
    cap = max(0, int(limit))
    lexical = list(keyword_hits)
    if cap == 0:
        return lexical
    roots = (_family_roots_for_hits([*lexical, *semantic_hits])
             if family_diverse else {})

    def family(hit: dict) -> str:
        session = str(hit.get("session") or "")
        return roots.get(session, session)

    lexical_families = {family(hit) for hit in lexical if hit.get("session")}
    visible_families = {family(hit) for hit in lexical[:cap] if hit.get("session")}
    seen_turns = {(str(hit.get("session") or ""), hit.get("turn")): hit
                  for hit in lexical if hit.get("session") and hit.get("turn") is not None}
    seen_text = {key for hit in lexical if (key := _hybrid_text_key(hit))}
    meaning = []
    raw_meaning = list(semantic_hits)[:_AUTO_SEMANTIC_ROWS]
    if lexical and not any(
            not _weak_lexical_hit(hit) for hit in lexical[:cap]):
        # Weak neighbors may supplement an exact anchor but never lead a page
        # whose lexical lane is itself only scatter; strong semantic rows can
        # still rescue it, and sub-strong rows stay reachable through `-s`.
        raw_meaning = [
            hit for hit in raw_meaning
            if hit.get("sem_score") is not None
            and float(hit["sem_score"]) >= _RECALL_STRONG_SEM
        ]
        if not raw_meaning:
            return lexical[:cap]
    overlaps = [index for index, hit in enumerate(raw_meaning)
                if hit.get("session") and family(hit) in lexical_families]
    overlap_at = max(overlaps) if overlaps else None
    # A weak scattered cousin is corroboration, not coverage: only a strong
    # visible row of the same family may suppress the semantic lead.
    if overlaps and overlaps[0] == 0 and raw_meaning[0].get("session") \
            and family(raw_meaning[0]) in visible_families \
            and any(not _weak_lexical_hit(hit) for hit in lexical[:cap]
                    if family(hit) == family(raw_meaning[0])):
        later_hidden = [index for index in overlaps[1:]
                        if family(raw_meaning[index]) not in visible_families]
        if not later_hidden:
            return lexical[:cap]
        overlap_at = max(later_hidden)
    elif overlap_at == 0 and len(raw_meaning) > 1:
        overlap_at = 1
    for index, raw in enumerate(raw_meaning):
        turn_key = (str(raw.get("session") or ""), raw.get("turn"))
        text_key = _hybrid_text_key(raw)
        twin = (seen_turns.get(turn_key)
                if turn_key[0] and turn_key[1] is not None else None)
        if twin is not None:
            if not _weak_lexical_hit(twin):
                continue
            # A weak lexical copy of the same row must not veto its
            # semantic-labeled twin; show the confident copy once.
            lexical.remove(twin)
        elif text_key and text_key in seen_text:
            continue
        hit = {**raw, "lane": "semantic"}
        meaning.append(hit)
        if turn_key[0] and turn_key[1] is not None:
            seen_turns[turn_key] = hit
        if text_key:
            seen_text.add(text_key)
        if overlap_at is not None and index >= overlap_at:
            break
    if not lexical:
        return meaning[:min(cap, _AUTO_SEMANTIC_ROWS)]
    if not meaning or cap == 1:
        return lexical[:cap]
    if overlap_at is not None:
        used = {family(hit) for hit in meaning if hit.get("session")}
        remainder = [hit for hit in lexical
                     if not hit.get("session") or family(hit) not in used]
        return [*meaning, *remainder][:cap]
    strong = [hit for hit in lexical if not _weak_lexical_hit(hit)]
    if not strong:
        reserve = min(len(meaning), _AUTO_SEMANTIC_ROWS, cap)
        return [*meaning[:reserve], *lexical][:cap]
    lead = min(3, next(
        (index for index, hit in enumerate(lexical) if _weak_lexical_hit(hit)),
        len(lexical)))
    lead = max(1, min(lead, cap - 1))
    return [*lexical[:lead], meaning[0], *lexical[lead:]][:cap]


def _semantic_runtime_installed() -> bool:
    import importlib.util
    try:
        return all(importlib.util.find_spec(name) is not None
                   for name in ("numpy", "onnxruntime", "tokenizers"))
    except (ImportError, AttributeError, ValueError):
        return False


def _start_semantic_query(
        query: str, kwargs: dict, *, _allow_recovery: bool = True,
        _absolute_deadline: float | None = None):
    """Start an independent meaning lane while lexical retrieval runs."""
    import threading
    raw_timeout = kwargs.get("semantic_timeout_s")
    timeout_s = float(raw_timeout if raw_timeout is not None
                      else _AUTO_SEMANTIC_TIMEOUT_S)
    timeout_s = max(0.0, timeout_s)
    state: dict = {
        "query": query,
        "kwargs": dict(kwargs),
        "timeout_s": timeout_s,
        "allow_recovery": bool(_allow_recovery),
    }
    # A verified live publisher already extends the keyword horizon. Give meaning
    # that wait plus its original compute headroom; never charge ordinary searches
    # or a merely stale/unavailable lane this larger deadline.
    if timeout_s > 0 and _semantic_index_update_active():
        timeout_s = max(
            timeout_s, _QUERY_PUBLICATION_WAIT_S + timeout_s)
    deadline = time.monotonic() + timeout_s
    if _absolute_deadline is not None:
        absolute_deadline = float(_absolute_deadline)
        if not math.isfinite(absolute_deadline):
            raise ValueError("semantic deadline must be finite")
        deadline = min(deadline, absolute_deadline)

    def work() -> None:
        try:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                return
            call_kwargs = {
                **kwargs, "semantic_timeout_s": remaining,
                "allow_model_download": False,
            }
            state["result"] = run_query(
                query, mode="semantic", **call_kwargs)
        except Exception as exc:  # noqa: BLE001 -- optional meaning cannot wound grep
            state["error"] = exc

    try:
        thread = threading.Thread(
            target=work,
            name="agrep-semantic-query", daemon=True)
        thread.start()
    except Exception as exc:  # noqa: BLE001 -- optional lane fails closed
        common.dbg(
            f"automatic semantic lane could not start: {type(exc).__name__}",
            "!")
        return None
    return thread, state, deadline


def _safe_start_semantic_query(query: str, kwargs: dict, **internal):
    """Start only the optional lane; every authoritative query stays strict."""
    try:
        return _start_semantic_query(query, kwargs, **internal)
    except Exception as exc:  # noqa: BLE001 -- keyword output remains authoritative
        common.dbg(
            f"automatic semantic lane start failed: {type(exc).__name__}",
            "!")
        return None


def _finish_semantic_query(pending) -> dict | None:
    """Finish an optional meaning lane without wounding keyword retrieval."""
    try:
        return _finish_semantic_query_inner(pending)
    except Exception as exc:  # noqa: BLE001 -- optional lane fails closed
        common.dbg(
            f"automatic semantic lane could not finish: {type(exc).__name__}",
            "!")
        return None


def _safe_finish_semantic_query(pending) -> dict | None:
    """Contain a replacement/test-double failure at the optional-lane edge."""
    try:
        return _finish_semantic_query(pending)
    except Exception as exc:  # noqa: BLE001 -- keyword output remains authoritative
        common.dbg(
            f"automatic semantic lane finish failed: {type(exc).__name__}",
            "!")
        return None


def _finish_semantic_query_inner(pending) -> dict | None:
    if pending is None:
        return None
    thread, state, deadline = pending
    thread.join(timeout=max(0.0, deadline - time.monotonic()))
    timed_out = thread.is_alive()
    if timed_out:
        common.dbg("automatic semantic lane exceeded its deadline; keeping keyword results", "!")
        # The serving deadline is a hard boundary; transport owns later cleanup.
        # Retain typed failures returned by that deadline, but never delay lexical
        # output for a still-running optional meaning call.
        return None
    if state.get("error") is not None:
        common.dbg(
            f"automatic semantic lane failed: {type(state['error']).__name__}", "!")
    result = state.get("result")
    if not isinstance(result, dict):
        return None
    status = result.get("semantic_status")
    if (not state.get("allow_recovery")
            or not result.get("fallback_recommended")
            or not surface.semantic_status_retryable(status)):
        return result

    # The fast attempt proved an exact transient. Convergence, owner handoff,
    # cached-model load, and the one retry share one absolute recovery deadline;
    # no phase gets to restart the clock. Every query/filter option is preserved.
    try:
        import semantic
        recovery_window = semantic.bounded_query_recovery_wait_s(
            semantic.SEMANTIC_QUERY_RECOVERY_WAIT_S)
        recovery_deadline = (
            time.monotonic() + recovery_window)
        recovery_remaining = max(
            0.0, recovery_deadline - time.monotonic())
        if recovery_remaining <= 0.0:
            return result
        recovered = semantic.wait_for_query_recovery(
            timeout_s=recovery_remaining)
    except Exception as exc:  # noqa: BLE001 -- retain the first typed failure
        common.dbg(
            f"semantic recovery failed: {type(exc).__name__}", "!")
        return result
    if recovered.get("state") != "ready":
        common.dbg(
            f"semantic recovery ended {recovered.get('state')}", "!")
        return result
    recovery_remaining = max(
        0.0, recovery_deadline - time.monotonic())
    if recovery_remaining <= 0.0:
        common.dbg("semantic recovery deadline exhausted before retry", "!")
        return result
    common.dbg("semantic generation ready; retrying meaning lane")
    retry_kwargs = dict(state.get("kwargs") or {})
    retry_kwargs["semantic_timeout_s"] = recovery_remaining
    retry = _safe_start_semantic_query(
        str(state.get("query") or ""), retry_kwargs,
        _allow_recovery=False, _absolute_deadline=recovery_deadline)
    retried = _safe_finish_semantic_query(retry)
    return retried if isinstance(retried, dict) else result


def _want_terms_fallback(mode: str, q: str) -> bool:
    """Should the bag-of-words (any-order) lane run? Always, for a multi-word keyword
    search: the all-terms set is a superset of any phrase match, and _score keeps tight
    phrase hits ranked above scattered ones. Unconditional keeps the answer independent
    of corpus history - searching a phrase plants adjacent copies of it in your own
    transcripts, so any results-are-thin threshold lets those echoes accumulate until
    they permanently shadow the scattered hit being hunted."""
    if mode != "keyword":
        return False
    return len([t for t in re.split(r"[\s\-_]+", q.strip()) if t]) >= 2


def _session_heads(hits: list[dict], limit: int) -> list[dict]:
    """Best-ranked hit from each session, preserving the ranking already applied.

    `limit=0` means every distinct session, mirroring run_query's row-limit
    convention. Keeping this at the query layer matters: callers asking for chats
    must not guess how many turn rows they need to fetch before deduplicating.
    """
    counts: dict[str, int] = {}
    for h in hits:
        session = h.get("session")
        if session:
            counts[session] = counts.get(session, 0) + 1
    out, seen = [], set()
    for h in hits:
        session = h.get("session")
        if not session or session in seen:
            continue
        seen.add(session)
        out.append({**h, "session_hits": counts[session]})
        if limit and len(out) >= limit:
            break
    return out


def _family_heads(hits: list[dict], limit: int,
                  parents: dict | None = None) -> list[dict]:
    """Keep the best-ranked raw chat from each root conversation family.

    This is for ranked recall/meaning surfaces only. Literal grep deliberately
    keeps every child hit: a subagent's answer is unique evidence, not a duplicate.
    """
    roots = _family_roots_for_hits(hits) if parents is None else {}
    out, seen, memo = [], set(), {}
    for hit in hits:
        session = str(hit.get("session") or "")
        family = (common.family_root(session, parents, memo)
                  if parents is not None else roots.get(session, session))
        if family in seen:
            continue
        seen.add(family)
        out.append(hit)
        if limit and len(out) >= limit:
            break
    return out


_HIT_ID_FIELDS = ("session", "agent", "project", "concept", "model",
                  "model_source", "turn", "ts", "who")


def _augment_phrase_hits(phrase: list[dict], terms: list[dict]) -> list[dict]:
    """Merge an all-terms superset without relabeling or losing exact phrases.

    The combined SQLite walk and paired JSONL walks attach a private per-row key,
    so metadata-identical tool rows remain distinct. Iterate in the terms engine's
    corpus order and substitute the phrase hit (with its exact-match snippet) for the
    same row. The structural fallback supports older/minimal engine fixtures.
    """
    private = "_agrep_row_key"

    def clean(hit: dict) -> dict:
        # In-place: these rows leave both walks owned by this merge, and a
        # del keeps the remaining key order a fresh copy would have kept.
        hit.pop(private, None)
        return hit

    keyed = {hit[private]: hit for hit in phrase if private in hit}
    if keyed or any(private in hit for hit in terms):
        out = []
        consumed = set()
        for hit in terms:
            key = hit.get(private)
            match = keyed.get(key)
            if match is not None:
                consumed.add(key)
                out.append(clean(match))
            else:
                out.append({**clean(hit), "matched": "all-terms"})
        # Defensive only: never discard an exact hit the terms walk did not surface.
        out.extend(clean(hit) for key, hit in keyed.items() if key not in consumed)
        return out

    exact: dict[tuple, list[dict]] = {}
    for hit in phrase:
        exact.setdefault(tuple(hit.get(k) for k in _HIT_ID_FIELDS), []).append(hit)
    out = []
    for hit in terms:
        key = tuple(hit.get(k) for k in _HIT_ID_FIELDS)
        matches = exact.get(key)
        if matches:
            out.append(clean(matches.pop()))
        else:
            out.append({**clean(hit), "matched": "all-terms"})
    # Defensive only: terms is contractually a superset, but never discard an exact
    # hit if a future engine's metadata projection changes.
    for matches in exact.values():
        out.extend(clean(hit) for hit in matches)
    return out


def _bounded_single_keyword_rows(db, q: str, limit: int,
                                 flt: dict, boundary=None) -> dict | None:
    """Exact broad-token top rows without materializing the complete posting list.

    Every candidate arrives in a descending *safe* score-ceiling order. Once the worst
    retained row strictly beats the next ceiling, no unseen row can enter the requested
    head. Exact hit/chat/tool totals are then computed by one SQLite aggregate, preserving
    the ordinary CLI contract while avoiding ~100k snippets and Python dictionaries for a
    query such as ``the``. Any unavailable SQLite feature fails open to keyword().
    """
    import math
    import sqlite3
    _load_corpusdb()
    ql = q.strip().lower()
    if (limit <= 0 or limit > _BOUNDED_ROW_MAX_RESULTS or len(ql) < 3
            or not ql.isascii() or not ql.isalnum()):
        return None
    toks = [t for t in re.split(r"[\s\-_]+", q.strip()) if t]
    if len(toks) != 1:
        return None

    own_tx = False
    candidates = None
    try:
        own_tx = not db.in_transaction
        if own_tx:
            db.execute("BEGIN")
        gate = max(0, int(_BOUNDED_ROW_MIN_CANDIDATES))
        if corpusdb.candidate_count_capped(db, [ql], flt, gate + 1) <= gate:
            return None

        score_pat = _match_pat(q, "keyword")
        boundary = boundary or _prepare_boundary(q, "keyword", db)
        now_ms = time.time() * 1000
        best: list[dict] = []
        worst_index = -1
        worst_key = None
        examined = 0
        exact_stats, candidates = corpusdb.bounded_single_keyword_candidates(
            db, ql, flt, now_ms=now_ms, who_weights=_WHO_W,
            source_scales=_SOURCE_SCALE,
            recency_half_life_days=_RECENCY_HALF_LIFE_DAYS,
            user_recency_floor=_USER_REC_FLOOR)
        for ceiling, row in candidates:
            # _score rounds to four decimals. Rounding the mathematical upper bound
            # upward plus a strict comparison preserves every possible score/tie winner.
            max_rounded = math.ceil((ceiling + 1e-12) * 10_000) / 10_000
            if worst_key is not None and max_rounded < -worst_key[1]:
                break
            examined += 1
            text = row[corpusdb._TEXT]
            span = common.insensitive_span(text, q)
            if span is None:
                continue  # external-content FTS is a superset; confirm exact text
            hit = corpusdb._hit(row, *span)
            hit["score"] = round(
                _score(hit, score_pat, len(ql), now_ms, terms=[ql],
                       boundary=boundary), 4)
            key = _rank_key(hit)
            if len(best) < limit:
                best.append(hit)
            elif key < worst_key:
                best[worst_index] = hit
            else:
                continue
            if len(best) == limit:
                worst_index, worst_hit = max(
                    enumerate(best), key=lambda item: _rank_key(item[1]))
                worst_key = _rank_key(worst_hit)

        close = getattr(candidates, "close", None)
        if close is not None:
            close()
        candidates = None
        total, chats, tool_hits = exact_stats
        if len(best) != min(limit, total):
            return None
        best.sort(key=_rank_key)
        common.dbg(f"bounded rows: examined {examined}/{total} exact hit(s) "
                   f"for top {limit}")
        return {"hits": best, "total": total, "chats": chats,
                "tool_hits": tool_hits, "totals_exact": True}
    except (sqlite3.DatabaseError, ValueError, AttributeError) as exc:
        common.dbg(f"bounded rows unavailable ({exc}); using exhaustive ranking", "!")
        return None
    finally:
        if candidates is not None:
            close = getattr(candidates, "close", None)
            if close is not None:
                close()
        if own_tx and getattr(db, "in_transaction", False):
            db.rollback()


def _bounded_short_shape(q: str, limit: int, flt: dict) -> str | None:
    if not isinstance(flt, dict):
        return None
    ql = q.strip().lower()
    if (limit <= 0 or limit > _BOUNDED_ROW_MAX_RESULTS
            or len(ql) not in (1, 2) or not ql.isascii() or not ql.isalnum()):
        return None
    toks = [token for token in re.split(r"[\s\-_]+", q.strip()) if token]
    if len(toks) != 1:
        return None
    if (any(flt.get(name) for name in (
            "agent", "project", "who", "model", "model_soft", "chat"))
            or flt.get("since_ms") is not None
            or flt.get("until_ms") is not None):
        return None
    if not isinstance(flt.get("include_tools", True), bool):
        return None
    return ql


def _short_ascii_score(row: tuple, span: tuple[int, int], ql: str,
                       now_ms: float, ambiguity: float,
                       lowered: str | None = None) -> tuple[float, str, float] | None:
    text = row[corpusdb._TEXT]
    if not text.isascii():
        return None
    start, end = span
    left = max(0, start - 80)
    right = min(len(text), end + 80)
    folded = (text[left:right].lower() if lowered is None
              else lowered[left:right])
    count = 0
    best = 0.0
    at = folded.find(ql)
    while at >= 0:
        count += 1
        absolute = left + at
        match_end = absolute + len(ql)
        starts_word = (False if absolute == left and left > 0
                       else boundary_rank._ascii_boundary(text, absolute))
        ends_word = (False if match_end == right and right < len(text)
                     else boundary_rank._ascii_boundary(text, match_end))
        best = max(best, (float(starts_word) + float(ends_word)) / 2.0)
        at = folded.find(ql, at + len(ql))
    factor = max(0.12, 1.0 - ambiguity * (1.0 - best))
    match = 1.0 - 0.5 ** count
    age_days = max(0.0, now_ms - (row[7] or 0)) / 86_400_000
    recency = 0.5 ** (age_days / _RECENCY_HALF_LIFE_DAYS)
    speaker = row[8] or ""
    if speaker == "user":
        recency = max(recency, _USER_REC_FLOOR)
    score = match * recency * _WHO_W.get(speaker, 0.5) * factor
    score *= _SOURCE_SCALE.get(speaker, 1.0)
    match_class = "aligned" if best == 1.0 else "interior" if best == 0.0 else "partial"
    return round(score, 4), match_class, factor


def _short_boundary_ceiling(db, boundary) -> float:
    try:
        term = boundary[0].terms[0]
        quality = corpusdb.boundary_token_qualities(db, [term.folded]).get(term.folded)
        if quality not in (0, 1, 2):
            return 1.0
        return max(0.12, 1.0 - term.ambiguity * (1.0 - quality / 2.0))
    except (AttributeError, IndexError, TypeError, ValueError):
        return 1.0


def _short_row_key(row: tuple, score: float) -> tuple:
    turn = row[6]
    return (0, -score, -(row[7] or 0), row[0], -1 if turn is None else turn,
            row[8] or "")


def _score_short_pending(pending: list[dict], boundary, boundary_state,
                         score_pat, ql: str, now_ms: float, retain) -> None:
    if not pending:
        return
    _boundary_batch(pending, boundary, boundary_state)
    for hit in pending:
        hit["score"] = round(
            _score(hit, score_pat, len(ql), now_ms, terms=[ql],
                   boundary=boundary), 4)
        retain(hit)
    pending.clear()


def _bounded_short_keyword_rows(db, q: str, limit: int,
                                flt: dict, boundary=None) -> dict | None:
    """Exact short-token heads with explicitly lower-bound aggregate counts."""
    _load_corpusdb()
    ql = _bounded_short_shape(q, limit, flt)
    if ql is None:
        return None

    own_tx = False
    lane = None
    boundary_state = [None, None, None]
    try:
        own_tx = not db.in_transaction
        if own_tx:
            db.execute("BEGIN")
        score_pat = _match_pat(q, "keyword")
        boundary = boundary or _prepare_boundary(q, "keyword", db)
        now_ms = time.time() * 1000
        lane = corpusdb.bounded_short_keyword_candidates(
            db, ql, flt, now_ms=now_ms, who_weights=_WHO_W,
            source_scales=_SOURCE_SCALE,
            recency_half_life_days=_RECENCY_HALF_LIFE_DAYS,
            user_recency_floor=_USER_REC_FLOOR,
            boundary_ceiling=_short_boundary_ceiling(db, boundary))
        best: list[dict] = []
        worst_index = -1
        worst_key = None
        pending: list[dict] = []
        seed_size = max(_SHORT_BOUNDARY_SEED, limit * 2)
        ambiguity = boundary[0].terms[0].ambiguity

        def retain(hit: dict, key: tuple | None = None) -> None:
            nonlocal worst_index, worst_key
            key = _rank_key(hit) if key is None else key
            if len(best) < limit:
                best.append(hit)
            elif key < worst_key:
                best[worst_index] = hit
            else:
                return
            if len(best) == limit:
                worst_index, worst_hit = max(
                    enumerate(best), key=lambda item: _rank_key(item[1]))
                worst_key = _rank_key(worst_hit)

        def score_pending() -> None:
            _score_short_pending(
                pending, boundary, boundary_state, score_pat, ql, now_ms,
                retain)

        for candidate in lane:
            if worst_key is not None and candidate.strictly_below(-worst_key[1]):
                score_pending()
                if worst_key is not None and candidate.strictly_below(-worst_key[1]):
                    lane.stop()
                    break
            if candidate.span is None:
                continue
            fast = _short_ascii_score(
                candidate.row, candidate.span, ql, now_ms, ambiguity,
                candidate.lowered)
            if fast is not None:
                score, match_class, factor = fast
                key = _short_row_key(candidate.row, score)
                if len(best) < limit or key < worst_key:
                    hit = corpusdb._hit(candidate.row, *candidate.span)
                    hit["score"] = score
                    hit["_boundary_class"] = match_class
                    hit["_boundary_score_factor"] = factor
                    hit["_boundary_factor"] = round(factor, 6)
                    retain(hit, key)
                continue
            hit = corpusdb._hit(candidate.row, *candidate.span)
            pending.append(hit)
            refresh = (_NATIVE_BOUNDARY_BATCH
                       if boundary_state[0] == "native"
                       else _SHORT_BOUNDARY_REFRESH)
            if ((worst_key is None and len(pending) >= seed_size)
                    or len(pending) >= refresh):
                score_pending()

        score_pending()
        progress = lane.progress
        lane.close()
        lane = None
        if len(best) != min(limit, progress.observed_total):
            return None
        best.sort(key=_rank_key)
        common.dbg(
            f"bounded short rows: examined {progress.candidates_examined} candidate(s), "
            f"observed {progress.observed_total} hit(s) for top {limit}, "
            f"exact={progress.totals_exact}")
        return {
            "hits": best,
            "total": progress.observed_total,
            "chats": progress.observed_chats,
            "tool_hits": progress.observed_tool_hits,
            "totals_exact": progress.totals_exact,
        }
    except Exception as exc:
        common.dbg(f"bounded short rows unavailable ({exc}); using exhaustive ranking", "!")
        return None
    finally:
        _close_boundary_worker(boundary_state)
        if lane is not None:
            lane.close()
        if own_tx and getattr(db, "in_transaction", False):
            db.rollback()


def _bounded_keyword_rows(db, q: str, limit: int, flt: dict,
                          allow_fallback: bool, boundary=None) -> dict | None:
    """Exact top rows with explicitly lower-bound aggregate counts."""
    import math
    import sqlite3
    _load_corpusdb()
    toks = [token for token in re.split(r"[\s\-_]+", q.strip()) if token]
    if not toks or limit <= 0 or limit > _BOUNDED_ROW_MAX_RESULTS:
        return None
    if not any(len(token) >= 3 for token in toks):
        # No token can use the trigram index, so this lane degenerates into a
        # python row-by-row merge over the whole table (an emoji query measured
        # 492k fetchones, ~5s). The SQL lane scans the same rows in C.
        return None
    lows = list(dict.fromkeys(token.lower() for token in toks))
    gate = max(0, int(_BOUNDED_KEYWORD_MIN_CANDIDATES))
    try:
        if corpusdb.candidate_count_capped(db, lows, flt, gate + 1) <= gate:
            return None
    except sqlite3.OperationalError as exc:
        common.dbg(f"bounded row preflight unavailable ({exc}); using exhaustive ranking", "!")
        return None

    phrase_pat = (None if len(toks) == 1 else
                  re.compile(r"[\W_]*".join(re.escape(token) for token in toks), re.I))
    score_pat = _match_pat(q, "keyword")
    boundary = boundary or _prepare_boundary(q, "keyword", db)
    qlen = len("".join(toks))
    now_ms = time.time() * 1000
    fallback_possible = len(toks) >= 2
    phrase_best: list[dict] = []
    term_best: list[dict] = []
    phrase_worst = term_worst = None
    pending: list[tuple[dict, bool]] = []
    boundary_state = [None, None, None]
    observed_total = observed_tools = examined = scored = 0
    observed_chats: set[str] = set()
    phrase_count = 0
    phrase_absent = False
    phrase_complete = False
    stopped_early = False
    candidates = None
    seed_size = min(_BOUNDARY_REFINE_POOL, max(32, limit * 4))

    def retain(bucket: list[dict], hit: dict, worst):
        key = _rank_key(hit)
        if len(bucket) < limit:
            bucket.append(hit)
        elif key < worst:
            index = max(range(len(bucket)), key=lambda item: _rank_key(bucket[item]))
            bucket[index] = hit
        else:
            return worst
        return max((_rank_key(row) for row in bucket), default=None)

    def score_pending() -> None:
        nonlocal phrase_worst, term_worst, scored
        if not pending:
            return
        rows = [hit for hit, _is_phrase in pending]
        native = _boundary_batch(rows, boundary, boundary_state)
        scorer = None if native else boundary
        for hit, is_phrase in pending:
            hit["score"] = round(
                _score(hit, score_pat, qlen, now_ms, terms=lows, boundary=scorer), 4)
            if is_phrase:
                phrase_worst = retain(phrase_best, hit, phrase_worst)
            else:
                term_worst = retain(term_best, hit, term_worst)
        scored += len(pending)
        pending.clear()

    def queue_for_boundary(hit: dict, is_phrase: bool) -> None:
        bucket = phrase_best if is_phrase else term_best
        worst = phrase_worst if is_phrase else term_worst
        if len(bucket) >= limit and worst is not None:
            hit["score"] = round(
                _score(hit, score_pat, qlen, now_ms, terms=lows, boundary=None), 4)
            if _rank_key(hit) > worst:
                return
        pending.append((hit, is_phrase))

    def term_frontier(target: int):
        if target <= 0 or len(term_best) < target:
            return None
        return heapq.nsmallest(target, (_rank_key(hit) for hit in term_best))[-1]

    own_tx = False
    try:
        own_tx = not db.in_transaction
        if own_tx:
            db.execute("BEGIN")
        dense = corpusdb.dense_candidate_lane(db, lows, flt)
        if fallback_possible and dense:
            phrase_complete, thin_phrase = corpusdb.dense_phrase_preflight(
                db, toks, flt, limit)
            if phrase_complete:
                phrase_absent = not thin_phrase
                for row, start, end in thin_phrase:
                    hit = corpusdb._hit(row, start, end)
                    phrase_count += 1
                    observed_total += 1
                    observed_chats.add(row[0])
                    observed_tools += (row[8] or "") == "tool"
                    queue_for_boundary(hit, True)
                score_pending()
                common.dbg(
                    f"bounded rows: dense phrase lane complete with "
                    f"{phrase_count} match(es)")
        term_target = max(0, limit - len(phrase_best))
        candidates = corpusdb.score_ceiling_candidates(
            db, lows, flt, now_ms=now_ms, who_weights=_WHO_W,
            source_scales=_SOURCE_SCALE,
            recency_half_life_days=_RECENCY_HALF_LIFE_DAYS,
            user_recency_floor=_USER_REC_FLOOR, dense=dense)
        for ceiling, row in candidates:
            max_rounded = math.ceil((ceiling + 1e-12) * 10_000) / 10_000
            upper = max_rounded
            frontier = term_frontier(term_target) if phrase_complete else phrase_worst
            if pending and frontier is not None and upper < -frontier[1]:
                score_pending()
                frontier = (term_frontier(term_target)
                            if phrase_complete else phrase_worst)
            if phrase_complete and frontier is not None and upper < -frontier[1]:
                stopped_early = True
                break
            if (not phrase_complete and phrase_count >= _EARLY_STOP_MIN_PHRASES
                    and phrase_worst is not None and len(phrase_best) >= limit
                    and upper < -phrase_worst[1]):
                stopped_early = True
                break
            examined += 1
            text = row[corpusdb._TEXT]
            lowered = text.lower()
            spans = [common.insensitive_span(text, token, lowered) for token in lows]
            if any(span is None for span in spans):
                continue

            if phrase_absent:
                phrase_match = False
                start = end = -1
            elif phrase_pat is None:
                span = spans[0]
                phrase_match = True
                start, end = span
            else:
                match = phrase_pat.search(text) if len(text) >= qlen else None
                phrase_match = match is not None
                start, end = match.span() if match is not None else (-1, -1)
            if phrase_complete and phrase_match:
                continue
            observed_total += 1
            observed_chats.add(row[0])
            observed_tools += (row[8] or "") == "tool"
            if phrase_match:
                phrase_count += 1
                if phrase_worst is None or upper >= -phrase_worst[1]:
                    queue_for_boundary(corpusdb._hit(row, start, end), True)
            elif fallback_possible and (term_worst is None or upper >= -term_worst[1]):
                hit = corpusdb._spans_hit(row, [span for span in spans if span is not None])
                hit["matched"] = "all-terms"
                queue_for_boundary(hit, False)
            if len(pending) >= seed_size:
                score_pending()
        score_pending()
        if candidates is not None:
            close = getattr(candidates, "close", None)
            if close is not None:
                close()
            candidates = None

        if fallback_possible and not phrase_best and not term_best:
            content = _content_terms(q)
            if allow_fallback and _nl_query(q, content):
                return None
        phrase_best.sort(key=_rank_key)
        term_best.sort(key=_rank_key)
        selected = [*phrase_best, *term_best][:limit]
        if len(selected) != min(limit, observed_total):
            return None
        terms_fallback = fallback_possible and phrase_count == 0
        terms_augmented = bool(phrase_best) and bool(term_best)
        common.dbg(
            f"bounded rows: examined {examined} candidate(s), scored {scored}, "
            f"observed {observed_total} hit(s) for top {limit}, "
            f"exact={not stopped_early}")
        return {
            "hits": selected,
            "total": observed_total,
            "chats": len(observed_chats),
            "tool_hits": observed_tools,
            "terms_fallback": terms_fallback,
            "terms_augmented": terms_augmented,
            "totals_exact": not stopped_early,
        }
    except (sqlite3.DatabaseError, ValueError, AttributeError,
            _NativeBoundaryRestart) as exc:
        common.dbg(f"bounded rows unavailable ({exc}); using exhaustive ranking", "!")
        return None
    finally:
        _close_boundary_worker(boundary_state)
        if candidates is not None:
            close = getattr(candidates, "close", None)
            if close is not None:
                close()
        if own_tx and getattr(db, "in_transaction", False):
            db.rollback()


class _ShortFamilyPartitionState:
    """Account isolated roots and the one disjoint global remainder."""

    __slots__ = (
        "limit", "excluded_roots", "settled", "examined", "observed_total",
        "observed_chats", "observed_tools", "totals_exact",
    )

    def __init__(self, limit: int):
        self.limit = limit
        self.excluded_roots: set[str] = set()
        self.settled: dict[str, tuple[tuple, dict]] = {}
        self.examined = 0
        self.observed_total = 0
        self.observed_chats = 0
        self.observed_tools = 0
        self.totals_exact = True

    def restore(self, best: dict[str, tuple[tuple, dict]]) -> tuple | None:
        best.clear()
        best.update(self.settled)
        if len(best) < self.limit:
            return None
        return max(value[0] for value in best.values())

    def remember(self, best: dict[str, tuple[tuple, dict]]) -> None:
        self.settled = {
            root: value for root, value in best.items()
            if root in self.excluded_roots
        }

    def account_progress(self, progress) -> None:
        self.examined += progress.candidates_examined
        self.observed_total += progress.observed_total
        self.observed_chats += progress.observed_chats
        self.observed_tools += progress.observed_tool_hits
        self.totals_exact = self.totals_exact and progress.totals_exact

class _ShortFamilyPass:
    """Track one global pass and detect a family worth isolating."""

    __slots__ = (
        "stats", "leader_root", "runner_root", "leader_count", "runner_count",
        "disabled",
    )

    def __init__(self):
        self.stats: dict[str, int] = {}
        self.leader_root = ""
        self.runner_root = ""
        self.leader_count = 0
        self.runner_count = 0
        self.disabled = False

    def observe(self, candidate) -> str | None:
        if self.disabled:
            return None
        root = candidate.family_root
        count = self.stats.get(root)
        if count is None:
            if len(self.stats) >= _SHORT_FAMILY_TRACK_MAX:
                self.stats.clear()
                self.leader_root = ""
                self.runner_root = ""
                self.leader_count = 0
                self.runner_count = 0
                self.disabled = True
                return None
            count = 0
        count += 1
        self.stats[root] = count
        if root == self.leader_root:
            self.leader_count = count
        elif root == self.runner_root:
            self.runner_count = count
            if self.runner_count > self.leader_count:
                self._swap_leaders()
        elif count > self.leader_count:
            self.runner_root, self.runner_count = (
                self.leader_root, self.leader_count)
            self.leader_root, self.leader_count = root, count
        elif count > self.runner_count:
            self.runner_root, self.runner_count = root, count
        if (self.leader_count >= _SHORT_DOMINANCE_MIN_CANDIDATES
                and (self.runner_count == 0 or self.leader_count >= (
                    self.runner_count * _SHORT_DOMINANCE_RATIO))):
            return self.leader_root
        return None

    def _swap_leaders(self) -> None:
        self.leader_root, self.runner_root = self.runner_root, self.leader_root
        self.leader_count, self.runner_count = self.runner_count, self.leader_count


def _bounded_short_keyword_sessions(
        db, q: str, limit: int, flt: dict,
        boundary=None, family_diverse: bool = False) -> dict | None:
    """Exact short-token session/family heads with lower-bound aggregate counts."""
    _load_corpusdb()
    ql = _bounded_short_shape(q, limit, flt)
    if ql is None:
        return None

    own_tx = False
    lane = None
    boundary_state = [None, None, None]
    try:
        own_tx = not db.in_transaction
        if own_tx:
            db.execute("BEGIN")
        score_pat = _match_pat(q, "keyword")
        boundary = boundary or _prepare_boundary(q, "keyword", db)
        now_ms = time.time() * 1000
        boundary_ceiling = _short_boundary_ceiling(db, boundary)
        best: dict[str, tuple[tuple, dict]] = {}
        pending: list[dict] = []
        worst_key = None
        seed_size = max(_SHORT_BOUNDARY_SEED, limit * 2)
        ambiguity = boundary[0].terms[0].ambiguity

        def retain(hit: dict, group: str | None = None,
                   key: tuple | None = None) -> None:
            nonlocal worst_key
            if group is None:
                group = (str(hit.get("_family_root") or hit["session"])
                         if family_diverse else hit["session"])
            key = _rank_key(hit) if key is None else key
            current = best.get(group)
            if current is not None:
                if key < current[0]:
                    best[group] = (key, hit)
            elif len(best) < limit:
                best[group] = (key, hit)
            else:
                worst_group, worst = max(best.items(), key=lambda item: item[1][0])
                if key < worst[0]:
                    del best[worst_group]
                    best[group] = (key, hit)
            if len(best) >= limit:
                worst_key = max(value[0] for value in best.values())

        def score_pending() -> None:
            _score_short_pending(
                pending, boundary, boundary_state, score_pat, ql, now_ms,
                retain)

        def score_candidate(candidate, group: str | None = None) -> None:
            if candidate.span is None:
                return
            fast = _short_ascii_score(
                candidate.row, candidate.span, ql, now_ms, ambiguity,
                candidate.lowered)
            if fast is not None:
                score, match_class, factor = fast
                if group is None:
                    group = (candidate.family_root
                             if family_diverse else candidate.row[0])
                key = _short_row_key(candidate.row, score)
                current = best.get(group)
                if (current is None and (len(best) < limit or key < worst_key)
                        or current is not None and key < current[0]):
                    hit = corpusdb._hit(candidate.row, *candidate.span)
                    hit["score"] = score
                    hit["_boundary_class"] = match_class
                    hit["_boundary_score_factor"] = factor
                    hit["_boundary_factor"] = round(factor, 6)
                    retain(hit, group, key)
                return
            hit = corpusdb._hit(candidate.row, *candidate.span)
            if family_diverse:
                hit["_family_root"] = group or candidate.family_root
            pending.append(hit)
            refresh = (_NATIVE_BOUNDARY_BATCH
                       if boundary_state[0] == "native"
                       else _SHORT_BOUNDARY_REFRESH)
            if ((worst_key is None and len(pending) >= seed_size)
                    or len(pending) >= refresh):
                score_pending()

        partitions = _ShortFamilyPartitionState(limit)
        while True:
            worst_key = partitions.restore(best)
            pending.clear()
            family_pass = _ShortFamilyPass()
            lane = corpusdb.bounded_short_keyword_candidates(
                db, ql, flt, now_ms=now_ms, who_weights=_WHO_W,
                source_scales=_SOURCE_SCALE,
                recency_half_life_days=_RECENCY_HALF_LIFE_DAYS,
                user_recency_floor=_USER_REC_FLOOR,
                boundary_ceiling=boundary_ceiling,
                exclude_families=partitions.excluded_roots,
            )
            dominant_root = None
            for candidate in lane:
                if (worst_key is not None
                        and candidate.strictly_below(-worst_key[1])):
                    score_pending()
                    if (worst_key is not None
                            and candidate.strictly_below(-worst_key[1])):
                        lane.stop()
                        break
                score_candidate(candidate)
                if not family_diverse:
                    continue
                dominant_root = family_pass.observe(candidate)
                if dominant_root is not None:
                    break
            score_pending()
            progress = lane.progress
            lane.close()
            lane = None
            if dominant_root is None:
                partitions.account_progress(progress)
                break

            partitions.examined += progress.candidates_examined
            worst_key = partitions.restore(best)
            pending.clear()
            partitions.excluded_roots.add(dominant_root)
            lane = corpusdb.bounded_short_keyword_family_candidates(
                db, ql, dominant_root, flt,
                now_ms=now_ms, who_weights=_WHO_W,
                source_scales=_SOURCE_SCALE,
                recency_half_life_days=_RECENCY_HALF_LIFE_DAYS,
                user_recency_floor=_USER_REC_FLOOR,
                boundary_ceiling=boundary_ceiling,
            )
            for candidate in lane:
                current = best.get(dominant_root)
                threshold_key = current[0] if current is not None else worst_key
                if (threshold_key is not None
                        and candidate.strictly_below(-threshold_key[1])):
                    score_pending()
                    current = best.get(dominant_root)
                    threshold_key = (
                        current[0] if current is not None else worst_key)
                    if (threshold_key is not None
                            and candidate.strictly_below(-threshold_key[1])):
                        lane.stop()
                        break
                score_candidate(candidate, dominant_root)
            score_pending()
            progress = lane.progress
            partitions.account_progress(progress)
            lane.close()
            lane = None
            partitions.remember(best)
            common.dbg(
                f"bounded short sessions: isolated dominant family "
                f"{dominant_root[:12]} "
                f"({len(partitions.excluded_roots)} total)")

        selected = [hit for _key, hit in heapq.nsmallest(
            limit, best.values(), key=lambda value: value[0])]
        sessions = {hit["session"] for hit in selected}
        counts = dict.fromkeys(sessions, 0)
        if sessions:
            filters, params = corpusdb._filter_sql(flt)
            marks = ",".join("?" for _ in sessions)
            filters.append(f"session IN ({marks})")
            where = " WHERE " + " AND ".join(filters)
            for session, text in db.execute(
                    "SELECT session, text FROM msgs" + where,
                    [*params, *sessions]):
                if common.insensitive_span(text, ql) is not None:
                    counts[session] += 1
        for hit in selected:
            hit["session_hits"] = counts[hit["session"]]
        common.dbg(
            f"bounded short sessions: examined {partitions.examined} "
            f"candidate(s), observed {partitions.observed_total} hit(s) "
            f"for top {limit}, exact={partitions.totals_exact}")
        return {
            "hits": selected,
            "total": partitions.observed_total,
            "chats": partitions.observed_chats,
            "tool_hits": partitions.observed_tools,
            "terms_fallback": False,
            "terms_augmented": False,
            "totals_exact": partitions.totals_exact,
        }
    except Exception as exc:
        common.dbg(
            f"bounded short sessions unavailable ({exc}); using exhaustive ranking", "!")
        return None
    finally:
        _close_boundary_worker(boundary_state)
        if lane is not None:
            lane.close()
        if own_tx and getattr(db, "in_transaction", False):
            db.rollback()


def _bounded_keyword_sessions(db, q: str, limit: int, flt: dict,
                              allow_fallback: bool,
                              boundary=None,
                              family_diverse: bool = False) -> dict | None:
    """Exact top session heads without exhaustive hit materialization.

    ``None`` delegates to the ordinary exhaustive path. The returned heads and their
    ordering are exact; aggregate counts are observed lower bounds and are explicitly
    marked inexact by run_query. Both lanes accumulate in one candidate pass, and the
    terms lane is ALWAYS live: early termination stays sound because candidates arrive
    in score-ceiling order, so once the kth phrase hit beats every remaining ceiling,
    no un-walked row - phrase or terms - can crack the top-k merge.
    """
    import math
    import sqlite3
    _load_corpusdb()
    toks = [t for t in re.split(r"[\s\-_]+", q.strip()) if t]
    if not toks or limit <= 0:
        return None
    lows = list(dict.fromkeys(t.lower() for t in toks))
    caller = str(flt.get("exclude_session") or "")
    window_boundary = flt.get("exclude_session_from_turn")
    exclude_family = flt.get("exclude_family", True)
    caller_row = (
        db.execute(
            "SELECT root FROM session_family WHERE session=?", (caller,)
        ).fetchone()
        if caller and window_boundary is None and exclude_family else None
    )
    caller_root = str(caller_row[0]) if caller_row and caller_row[0] else ""
    stream_flt = (
        {key: value for key, value in flt.items() if key != "exclude_session"}
        if window_boundary is None and exclude_family else dict(flt)
    )
    gate = max(0, int(_BOUNDED_KEYWORD_MIN_CANDIDATES))
    try:
        if corpusdb.candidate_count_capped(
                db, lows, stream_flt, gate + 1) <= gate:
            return None
    except sqlite3.OperationalError as exc:
        common.dbg(f"bounded keyword preflight unavailable ({exc}); using exhaustive ranking", "!")
        return None

    phrase_pat = (None if len(toks) == 1 else
                  re.compile(r"[\W_]*".join(re.escape(t) for t in toks), re.I))
    ql = q.strip().lower()
    score_pat = _match_pat(q, "keyword")
    boundary = boundary or _prepare_boundary(q, "keyword", db)
    qlen = len("".join(toks))
    now_ms = time.time() * 1000
    fallback_possible = len(toks) >= 2
    early_stop_at = _EARLY_STOP_MIN_PHRASES if fallback_possible else 0

    phrase_best: dict[str, tuple[tuple, dict]] = {}
    term_best: dict[str, tuple[tuple, dict]] = {}
    phrase_sessions: set[str] = set()
    term_sessions: set[str] = set()
    phrase_count = term_count = phrase_tool_count = term_tool_count = 0
    phrase_absent = False
    phrase_complete = False
    term_lane_confirmed = False
    stopped_early = False
    pending: list[tuple[dict, str, bool]] = []
    boundary_state = [None, None, None]
    seed_size = max(64, limit * 4)
    candidates = None
    own_tx = False
    examined = 0

    def add_best(bucket: dict, family: str, hit: dict) -> None:
        rank_key = _rank_key(hit)
        current = bucket.get(family)
        if current is not None:
            if rank_key < current[0]:
                bucket[family] = (rank_key, hit)
            return
        if len(bucket) < limit:
            bucket[family] = (rank_key, hit)
            return
        worst_family, worst = max(bucket.items(), key=lambda item: item[1][0])
        if rank_key < worst[0]:
            del bucket[worst_family]
            bucket[family] = (rank_key, hit)

    def score_pending() -> None:
        if not pending:
            return
        rows = [hit for hit, _family, _is_phrase in pending]
        native = _boundary_batch(rows, boundary, boundary_state)
        scorer = None if native else boundary
        for hit, family, is_phrase in pending:
            hit["score"] = round(
                _score(hit, score_pat, qlen, now_ms, terms=lows,
                       boundary=scorer), 4)
            add_best(phrase_best if is_phrase else term_best, family, hit)
        pending.clear()

    def bucket_frontier(bucket: dict, target: int):
        if target <= 0 or len(bucket) < target:
            return None
        return heapq.nsmallest(
            target, (value[0] for value in bucket.values()))[-1]

    def term_frontier(target: int):
        values = [value[0] for family, value in term_best.items()
                  if family not in phrase_best]
        if target <= 0 or len(values) < target:
            return None
        return heapq.nsmallest(target, values)[-1]

    def finish_result() -> dict | None:
        terms_fallback = fallback_possible and phrase_count == 0
        terms_augmented = False
        if fallback_possible:
            if not term_best:
                content = _content_terms(q)
                if allow_fallback and _nl_query(q, content):
                    return None
            extras = [value for family, value in term_best.items()
                      if family not in phrase_best]
            terms_augmented = bool(phrase_best) and bool(extras)
            values = [*phrase_best.values(), *extras]
            observed_total = term_count
            observed_chats = len(term_sessions)
            observed_tools = term_tool_count
        else:
            values = list(phrase_best.values())
            observed_total = phrase_count
            observed_chats = len(phrase_sessions)
            observed_tools = phrase_tool_count

        selected = [hit for _key, hit in sorted(
            values, key=lambda value: value[0])[:limit]]
        selected_sessions = {hit["session"] for hit in selected}
        selected_counts = dict.fromkeys(selected_sessions, 0)
        if selected_sessions:
            filters, params = corpusdb._filter_sql(flt)
            marks = ",".join("?" for _ in selected_sessions)
            filters.append(f"session IN ({marks})")
            where = " WHERE " + " AND ".join(filters)
            for session, text in db.execute(
                    "SELECT session, text FROM msgs" + where,
                    [*params, *selected_sessions]):
                lowered = text.lower()
                if all(common.insensitive_span(text, token, lowered) is not None
                       for token in lows):
                    selected_counts[session] += 1
        for hit in selected:
            hit["session_hits"] = selected_counts[hit["session"]]
        common.dbg(f"bounded sessions: examined {examined} candidate(s), "
                   f"observed {observed_total} hit(s), retained {len(selected)} "
                   f"session head(s), exact={not stopped_early}")
        return {"hits": selected, "total": observed_total, "chats": observed_chats,
                "tool_hits": observed_tools, "terms_fallback": terms_fallback,
                "terms_augmented": terms_augmented,
                "totals_exact": not stopped_early}

    try:
        own_tx = not db.in_transaction
        if own_tx:
            db.execute("BEGIN")
        dense = corpusdb.dense_candidate_lane(db, lows, stream_flt)
        if fallback_possible and dense:
            phrase_complete, thin_phrase = corpusdb.dense_phrase_preflight(
                db, toks, stream_flt, max(256, limit * 16))
            if phrase_complete:
                phrase_absent = not thin_phrase
                thin_roots = (
                    corpusdb.session_family_roots(
                        db, (row[0] for row, _start, _end in thin_phrase))
                    if family_diverse or caller_root else {}
                )
                for row, start, end in thin_phrase:
                    hit = corpusdb._hit(row, start, end)
                    session = row[0]
                    carried_root = thin_roots.get(session, session)
                    if ((caller_root and carried_root == caller_root)
                            or (window_boundary is None and caller
                                and not caller_root and session == caller)):
                        continue
                    family = carried_root if family_diverse else session
                    pending.append((hit, family, True))
                    phrase_count += 1
                    phrase_sessions.add(session)
                    phrase_tool_count += hit.get("who") == "tool"
                score_pending()
                term_count = phrase_count
                term_sessions.update(phrase_sessions)
                term_tool_count = phrase_tool_count
                common.dbg(
                    f"bounded sessions: dense phrase lane complete with "
                    f"{phrase_count} match(es)")
        term_target = max(0, limit - len(phrase_best))
        candidates = corpusdb.score_ceiling_candidates(
            db, lows, stream_flt, now_ms=now_ms, who_weights=_WHO_W,
            source_scales=_SOURCE_SCALE,
            recency_half_life_days=_RECENCY_HALF_LIFE_DAYS,
            user_recency_floor=_USER_REC_FLOOR, dense=dense,
            include_family=family_diverse or bool(caller_root))
        for ceiling, row in candidates:
            examined += 1
            # _score rounds to four decimals. Round the mathematical ceiling upward;
            # strict comparisons keep every candidate that could tie and win on ts/id.
            max_rounded = math.ceil((ceiling + 1e-12) * 10_000) / 10_000
            session = row[0]
            carried_root = (
                str(row[corpusdb._DIGEST + 1] or session)
                if family_diverse or caller_root else session)
            if ((caller_root and carried_root == caller_root)
                    or (window_boundary is None and caller
                        and not caller_root and session == caller)):
                continue
            family = carried_root if family_diverse else session
            upper = max_rounded
            if phrase_complete and term_target == 0 and term_lane_confirmed:
                stopped_early = True
                break
            frontier = (term_frontier(term_target)
                        if phrase_complete else bucket_frontier(phrase_best, limit))
            if pending and frontier is not None and upper < -frontier[1]:
                score_pending()
                frontier = (term_frontier(term_target)
                            if phrase_complete else bucket_frontier(phrase_best, limit))
            if phrase_complete and frontier is not None:
                if upper < -frontier[1]:
                    stopped_early = True
                    break
            elif phrase_count >= early_stop_at and frontier is not None:
                if upper < -frontier[1]:
                    stopped_early = True
                    break

            text = row[corpusdb._TEXT]
            low = text.lower()
            term_spans = [common.insensitive_span(text, token, low)
                          for token in lows]
            if any(span is None for span in term_spans):
                continue
            term_lane_confirmed = True

            if phrase_absent:
                phrase_match = False
                start = end = -1
            elif phrase_pat is None:
                span = common.insensitive_span(text, q, low)
                phrase_match = span is not None
                start, end = span if span is not None else (-1, -1)
            else:
                match = phrase_pat.search(text) if len(text) >= qlen else None
                phrase_match = match is not None
                start = match.start() if match else -1
                end = match.end() if match else -1

            if phrase_complete and phrase_match:
                continue

            if phrase_match:
                hit = corpusdb._hit(row, start, end)
                pending.append((hit, family, True))
                phrase_count += 1
                phrase_sessions.add(session)
                phrase_tool_count += hit.get("who") == "tool"

            # Terms lane stays live even amid phrase abundance (a phrase's own echoes
            # would shadow scattered hits); merge drops phrase-hit sessions, early stop bounds cost.
            if fallback_possible:
                hit = corpusdb._spans_hit(
                    row, [span for span in term_spans if span is not None])
                hit["matched"] = "all-terms"
                pending.append((hit, family, False))
                term_count += 1
                term_sessions.add(session)
                term_tool_count += hit.get("who") == "tool"
            if ((not phrase_best and not term_best and len(pending) >= seed_size)
                    or len(pending) >= _BOUNDARY_REFINE_POOL):
                score_pending()
        score_pending()
        if candidates is not None:
            close = getattr(candidates, "close", None)
            if close is not None:
                close()
            candidates = None
        answer = finish_result()
    except (sqlite3.OperationalError, _NativeBoundaryRestart) as exc:
        common.dbg(f"bounded keyword unavailable ({exc}); using exhaustive ranking", "!")
        return None
    finally:
        _close_boundary_worker(boundary_state)
        if candidates is not None:
            close = getattr(candidates, "close", None)
            if close is not None:
                close()
        if own_tx and getattr(db, "in_transaction", False):
            db.rollback()
    return answer


@dataclass(frozen=True, slots=True)
class QuerySpec:
    """Normalized request shared by candidate producers and the finalizer."""

    q: str
    mode: str
    limit: int
    sort: str
    agent: str | None
    project: str | None
    who: str | surface.SpeakerFilter | None
    model: str | None
    model_soft: bool
    chat: str | None
    since_ms: int | None
    until_ms: int | None
    exhaustive: bool
    session_limit: int | None
    include_tools: bool
    exclude_session: str | None
    exclude_session_from_turn: int | None
    allow_fallback: bool
    exact_totals: bool
    family_diverse: bool
    semantic_timeout_s: float | None
    excluded_sessions: tuple[str, ...] = ()
    allow_model_download: bool = False
    exclude_project: str | None = None
    exclude_family: bool = True


@dataclass(slots=True)
class LaneResult:
    """Candidate-lane output before the shared ranking and result contract."""

    hits: list[dict]
    engine: str
    boundary: boundary_rank.PreparedQuery | None = None
    pre_ranked: bool = False
    terms_fallback: bool = False
    terms_augmented: bool = False
    content_fallback: bool = False
    bounded_rows: dict | None = None
    bounded_sessions: dict | None = None
    semantic_meta: dict = field(default_factory=dict)
    semantic_truncated: bool = False
    index_missing: bool = False
    rank_now_ms: float | None = None
    # tool rows were excluded from this lane (FTS build owned elsewhere):
    # every renderer must see the narrowing, not re-derive the window
    tools_excluded: bool = False


class RegexTimeoutError(RuntimeError):
    pass


class RegexWorkerError(RuntimeError):
    pass


class DirectSnapshotQueryError(RuntimeError):
    pass


class DirectSnapshotQueryMoved(DirectSnapshotQueryError):
    pass


class QueryDatabaseBusyError(RuntimeError):
    pass


class QueryDatabaseUnavailableError(RuntimeError):
    pass


class SemanticQueryTimeoutError(RuntimeError):
    pass


class SemanticQueryWorkerError(RuntimeError):
    pass


_REGEX_TIMEOUT_S = 3.0


def _regex_timeout_s() -> float:
    raw = os.environ.get("AGREP_REGEX_TIMEOUT_S")
    if raw is None:
        return _REGEX_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        return _REGEX_TIMEOUT_S
    if not math.isfinite(value):
        return _REGEX_TIMEOUT_S
    return min(30.0, max(0.05, value))


def _arm_regex_worker_deadline(timeout: float) -> None:
    if common.WIN:
        return
    import signal

    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.setitimer(signal.ITIMER_REAL, timeout + 0.5)


def _disarm_regex_worker_deadline() -> None:
    if common.WIN:
        return
    import signal

    signal.setitimer(signal.ITIMER_REAL, 0.0)


def _regex_worker_main(send, spec: QuerySpec, timeout: float) -> None:
    _arm_regex_worker_deadline(timeout)
    try:
        try:
            result = _finalize_query(spec, _keyword_candidates(spec))
            pattern = re.compile(spec.q, re.I)
            for hit in result.get("hits", ()):
                snippet = hit.get("snippet") or ""
                hit["_regex_color_snippet"] = _hl(snippet, pattern, True)
                match = pattern.search(snippet)
                if match is not None:
                    hit["_regex_compact_snippet"] = _snip_at(
                        snippet, match.start(), match.end(), 32)
            payload = ("ok", result)
        except re.error as exc:
            payload = ("regex-error", str(exc))
        except QueryDatabaseBusyError as exc:
            payload = ("query-database-busy", str(exc))
        except QueryDatabaseUnavailableError as exc:
            payload = ("query-database-unavailable", str(exc))
        except Exception as exc:  # noqa: BLE001 -- the parent owns the process boundary
            payload = ("worker-error", f"{type(exc).__name__}: {exc}")
    finally:
        _disarm_regex_worker_deadline()
    try:
        send.send(payload)
    except (BrokenPipeError, EOFError, OSError):
        pass
    finally:
        send.close()


def _stop_guarded_worker(process) -> None:
    if process.is_alive():
        process.terminate()
    process.join(timeout=1.0)
    if process.is_alive():
        kill = getattr(process, "kill", None)
        if kill is not None:
            kill()
        process.join(timeout=1.0)


def _guarded_regex_query(spec: QuerySpec) -> dict:
    import multiprocessing

    if (common.WIN
            and not common.bind_descendants_to_process_lifetime()):
        raise RegexWorkerError(
            "regex worker lifetime boundary is unavailable")
    method = "spawn" if common.WIN else "fork"
    context = multiprocessing.get_context(method)
    receive, send = context.Pipe(duplex=False)
    timeout = _regex_timeout_s()
    process = context.Process(
        target=_regex_worker_main, args=(send, spec, timeout),
        name="agrep-regex", daemon=True)
    process.start()
    send.close()
    try:
        if not receive.poll(timeout):
            raise RegexTimeoutError(surface.regex_timeout_line(timeout))
        try:
            kind, payload = receive.recv()
        except EOFError as exc:
            raise RegexWorkerError(
                f"regex worker exited with status {process.exitcode}") from exc
    finally:
        receive.close()
        _stop_guarded_worker(process)
    if kind == "ok":
        return payload
    if kind == "regex-error":
        raise re.error(payload)
    if kind == "query-database-busy":
        raise QueryDatabaseBusyError(payload)
    if kind == "query-database-unavailable":
        raise QueryDatabaseUnavailableError(payload)
    raise RegexWorkerError(payload)


_SEMANTIC_CHILD_ARG = "--explorer-semantic-child"
_SEMANTIC_FALLBACK_CHILD_ARG = "--semantic-local-fallback-child"
_SEMANTIC_CHILD_INPUT_MAX = 8 * 1024
_SEMANTIC_CHILD_OUTPUT_MAX = 8 * 1024 * 1024
_SEMANTIC_TREE_OPEN_S = 1.0


def _stop_semantic_subprocess(
        process: subprocess.Popen, process_start: str | None,
        wait_s: float, *, windows_tree: object | None = None) -> bool:
    deadline = time.monotonic() + max(0.0, wait_s)
    if windows_tree is not None:
        if windows_tree.terminate_and_wait(
                max(0.0, deadline - time.monotonic())):
            return True
        if (process_start is None
                or not common.terminate_exact_process(
                    process.pid, process_start)):
            return False
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except (OSError, subprocess.TimeoutExpired):
            return False
        return True
    if process_start is not None:
        if common.terminate_exact_process_tree(
                process.pid, process_start,
                wait_s=max(0.0, deadline - time.monotonic()),
                require_bound_tree=True, term_grace_s=0.1):
            return True
        # A verified leader may exit before its private POSIX group drains; that
        # group ID cannot be reused while any original member survives.
        if common.WIN or common.pid_alive(process.pid):
            return False
        active = common._process_group_active(process.pid)
        if active is False:
            return True
        if active is not True:
            return False
        import signal
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return common._process_group_active(process.pid) is False
        except OSError:
            return False
        grace = min(deadline, time.monotonic() + 0.1)
        while time.monotonic() < grace:
            active = common._process_group_active(process.pid)
            if active is False:
                return True
            if active is None:
                return False
            time.sleep(0.01)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return common._process_group_active(process.pid) is False
        except OSError:
            return False
        while time.monotonic() < deadline:
            active = common._process_group_active(process.pid)
            if active is False:
                return True
            if active is None:
                return False
            time.sleep(0.01)
        return common._process_group_active(process.pid) is False
    if process.poll() is not None:
        return True
    try:
        process.kill()
        process.wait(timeout=0.5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return process.poll() is not None


def _open_windows_semantic_tree(
        process: subprocess.Popen, process_start: str,
        deadline: float | None) -> object | None:
    """Retain the child's exact Job before sending work that may spawn."""
    if not common.WIN:
        return None
    import winjob

    if deadline is None:
        deadline = time.monotonic() + _SEMANTIC_TREE_OPEN_S

    while True:
        tree = winjob.open_exact(process.pid, process_start)
        if tree is not None:
            return tree
        if process.poll() is not None:
            return None
        if deadline is not None and time.monotonic() >= deadline:
            return None
        time.sleep(0.005)


def _run_guarded_semantic_child(
        request_obj: dict, *, timeout_s: float | None,
        child_arg: str) -> dict | None:
    timeout = None
    if timeout_s is not None:
        raw_timeout = float(timeout_s)
        if not math.isfinite(raw_timeout):
            raise ValueError("semantic timeout must be finite")
        timeout = max(0.05, min(30.0, raw_timeout))
    request = json.dumps(
        request_obj, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(request) > _SEMANTIC_CHILD_INPUT_MAX:
        raise ValueError("guarded semantic request is too large")
    if child_arg not in (_SEMANTIC_CHILD_ARG, _SEMANTIC_FALLBACK_CHILD_ARG):
        raise ValueError("unknown semantic child entrypoint")
    env = dict(os.environ)
    env["AGREP_DATA_READONLY"] = os.fspath(common.DATA_DIR)
    command = [sys.executable, os.path.abspath(__file__), child_arg]
    kwargs = {
        "stdin": subprocess.PIPE, "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE, "cwd": os.fspath(common.REPO_ROOT),
        "env": env, "close_fds": True,
    }
    if common.WIN:
        kwargs["creationflags"] = common.windows_background_child_flags(
            getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        kwargs["start_new_session"] = True
    started = time.monotonic()
    try:
        process = subprocess.Popen(command, **kwargs)
    except OSError as exc:
        detail = common.terminal_safe(f"{type(exc).__name__}: {exc}")
        raise SemanticQueryWorkerError(
            f"semantic query worker could not start ({detail})") from exc
    process_start = common.process_start_identity(process.pid)
    if process_start is None:
        _stop_semantic_subprocess(process, None, 0.5)
        raise SemanticQueryWorkerError(
            "semantic query worker identity could not be verified")
    cleanup_reserve = (
        None if timeout is None
        else min(1.0, max(0.10, timeout * 0.25)))
    work_deadline = (
        None if timeout is None
        else started + max(0.05, timeout - cleanup_reserve))
    windows_tree = None
    try:
        if common.WIN:
            windows_tree = _open_windows_semantic_tree(
                process, process_start, work_deadline)
            if windows_tree is None:
                _stop_semantic_subprocess(process, process_start, 0.5)
                raise SemanticQueryWorkerError(
                    "semantic query worker lifetime boundary could not be verified")
        try:
            stdout, stderr = process.communicate(
                input=request,
                timeout=(
                    None if work_deadline is None
                    else max(0.001, work_deadline - time.monotonic())))
        except subprocess.TimeoutExpired as exc:
            assert timeout is not None
            remaining = max(0.0, started + timeout - time.monotonic())
            if not _stop_semantic_subprocess(
                    process, process_start, remaining,
                    windows_tree=windows_tree):
                raise SemanticQueryWorkerError(
                    "timed-out semantic query tree could not be drained") from exc
            raise SemanticQueryTimeoutError(
                f"meaning search exceeded its {timeout:g}s limit") from exc
        except BaseException:
            remaining = (
                1.0 if timeout is None
                else max(0.0, started + timeout - time.monotonic()))
            _stop_semantic_subprocess(
                process, process_start, remaining,
                windows_tree=windows_tree)
            raise
        remaining = (
            1.0 if timeout is None
            else max(0.0, started + timeout - time.monotonic()))
        if not _stop_semantic_subprocess(
                process, process_start, remaining,
                windows_tree=windows_tree):
            raise SemanticQueryWorkerError(
                "semantic query tree could not be drained")
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        if windows_tree is not None:
            windows_tree.close()
    if process.returncode != 0:
        detail = stderr.decode("utf-8", "replace").strip()[-500:]
        raise SemanticQueryWorkerError(
            detail or f"semantic query worker exited {process.returncode}")
    if len(stdout) > _SEMANTIC_CHILD_OUTPUT_MAX:
        raise SemanticQueryWorkerError(
            "semantic query worker exceeded its response limit")
    try:
        result = json.loads(stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SemanticQueryWorkerError(
            "semantic query worker returned an invalid response") from exc
    if result is not None and not isinstance(result, dict):
        raise SemanticQueryWorkerError(
            "semantic query worker returned an invalid result")
    return result


def _guarded_semantic_query(spec: QuerySpec) -> dict | None:
    if spec.semantic_timeout_s is None:
        raise ValueError("a guarded semantic query requires a timeout")
    unsupported = (
        spec.sort != "score" or spec.agent is not None
        or spec.project is not None or spec.who is not None
        or spec.model is not None or spec.model_soft or spec.chat is not None
        or spec.since_ms is not None or spec.until_ms is not None
        or spec.exhaustive or spec.session_limit is not None
        or not spec.include_tools or spec.exclude_session is not None
        or spec.exclude_session_from_turn is not None
        or spec.excluded_sessions
        or not spec.exclude_family
        or not spec.allow_fallback or not spec.exact_totals
        or not spec.family_diverse or spec.allow_model_download
        or spec.exclude_project is not None)
    if unsupported:
        raise ValueError("guarded semantic query supports the explorer shape only")
    return _run_guarded_semantic_child(
        {"q": spec.q, "limit": spec.limit},
        timeout_s=spec.semantic_timeout_s,
        child_arg=_SEMANTIC_CHILD_ARG)


def _guarded_semantic_local_fallback(
        query: str, *, level: str, k: int, filters: dict,
        timeout_s: float | None) -> dict:
    parent_start = common.process_start_identity(os.getpid())
    if not parent_start:
        raise SemanticQueryWorkerError(
            "semantic fallback parent identity could not be verified")
    owner_wait_s = 30.0
    if timeout_s is not None:
        timeout = max(0.05, min(30.0, float(timeout_s)))
        cleanup_reserve = min(1.0, max(0.10, timeout * 0.25))
        owner_wait_s = max(
            0.0, timeout - cleanup_reserve - 0.75)
    result = _run_guarded_semantic_child(
        {"query": query, "level": level, "k": k, "filters": filters,
         "owner_wait_s": owner_wait_s, "parent_pid": os.getpid(),
         "parent_start": parent_start},
        timeout_s=timeout_s,
        child_arg=_SEMANTIC_FALLBACK_CHILD_ARG)
    if not isinstance(result, dict):
        raise SemanticQueryWorkerError(
            "bounded local semantic fallback returned no result")
    return result


def _explorer_semantic_child_main() -> int:
    if not common.data_dir_readonly(common.DATA_DIR):
        return 70
    if not common.bind_descendants_to_process_lifetime():
        return 71
    raw = sys.stdin.buffer.read(_SEMANTIC_CHILD_INPUT_MAX + 1)
    if len(raw) > _SEMANTIC_CHILD_INPUT_MAX:
        return 72
    try:
        request = json.loads(raw.decode("utf-8"))
        if not isinstance(request, dict) or set(request) != {"q", "limit"}:
            return 72
        query = request["q"]
        limit = request["limit"]
        if (not isinstance(query, str) or not query.strip()
                or type(limit) is not int or not 1 <= limit <= 100):
            return 72
        result = run_query(
            query, mode="semantic", limit=limit,
            allow_model_download=False)
        encoded = json.dumps(
            result, ensure_ascii=False, separators=(",", ":"),
            allow_nan=False).encode("utf-8")
    except (RecursionError, TypeError, UnicodeError, ValueError,
            json.JSONDecodeError):
        return 73
    if len(encoded) > _SEMANTIC_CHILD_OUTPUT_MAX:
        return 74
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()
    return 0


def _semantic_local_fallback_child_main() -> int:
    """One killable, read-only local query after resident preflight failed."""
    if not common.data_dir_readonly(common.DATA_DIR):
        return 70
    if not common.bind_descendants_to_process_lifetime():
        return 71
    raw = sys.stdin.buffer.read(_SEMANTIC_CHILD_INPUT_MAX + 1)
    if len(raw) > _SEMANTIC_CHILD_INPUT_MAX:
        return 72
    owner = None
    semantic_module = None
    resources_released = False
    try:
        request = json.loads(raw.decode("utf-8"))
        if (not isinstance(request, dict)
                or set(request) != {
                    "query", "level", "k", "filters", "owner_wait_s",
                    "parent_pid", "parent_start"}):
            return 72
        owner_wait_s = float(request.pop("owner_wait_s"))
        if not math.isfinite(owner_wait_s) or not 0.0 <= owner_wait_s <= 30.0:
            return 72
        parent_pid = request.pop("parent_pid")
        parent_start = request.pop("parent_start")
        if (type(parent_pid) is not int or parent_pid <= 0
                or not isinstance(parent_start, str) or not parent_start
                or len(parent_start) > 256):
            return 72
        import ownerfile
        import semworker

        def parent_is_live() -> bool:
            return ownerfile.classify_process(
                parent_pid, parent_start,
                pid_alive=common.pid_alive,
                process_start=common.process_start_identity,
            ) is ownerfile.ProcessOwner.EXACT_LIVE

        query, level, k, filters, _timing = semworker._validate_request({
            **request, "timing": False,
        })
        owner_deadline = time.monotonic() + owner_wait_s
        while owner is None:
            if not parent_is_live():
                break
            owner = semworker.acquire_inprocess_owner()
            if (owner is not None
                    or semworker.removal_fence.background_removal_active()
                    or time.monotonic() >= owner_deadline):
                break
            time.sleep(min(0.02, max(
                0.0, owner_deadline - time.monotonic())))
        if owner is None:
            envelope = {
                "ok": False,
                "reason": (
                    "a safe in-process owner could not be acquired before "
                    "the fallback deadline"),
            }
        elif not parent_is_live():
            resources_released = True
            envelope = {
                "ok": False,
                "reason": "semantic fallback parent exited before inference",
            }
        else:
            import semantic as semantic_module
            try:
                semworker.verify_inprocess_owner(owner)
                data = semantic_module.search(
                    query, level=level, k=k, filters=filters,
                    refresh_if_stale=False, allow_model_download=False,
                    diagnostic_only=True)
                semworker.verify_inprocess_owner(owner)
                envelope = {"ok": True, "data": data}
            except (semantic_module.SemanticUnavailable,
                    semworker.ResidentSemanticUnavailable) as exc:
                envelope = {"ok": False, "reason": str(exc)}
    except (RecursionError, TypeError, UnicodeError, ValueError,
            json.JSONDecodeError):
        return 73
    finally:
        if semantic_module is not None:
            try:
                resources_released = bool(semantic_module.release())
            except Exception:  # noqa: BLE001 -- process exit drops native state
                resources_released = False
        if owner is not None:
            semworker.finish_inprocess_owner(
                owner, resources_released=resources_released)
    encoded = json.dumps(
        envelope, ensure_ascii=False, separators=(",", ":"),
        allow_nan=False).encode("utf-8")
    if len(encoded) > _SEMANTIC_CHILD_OUTPUT_MAX:
        return 74
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()
    return 0


def _semantic_candidates(spec: QuerySpec) -> LaneResult | None:
    # Dense search has no cheap corpus-wide count, so count and unlimited requests stay capped.
    requested = spec.session_limit if spec.session_limit is not None else spec.limit
    k = (SEMANTIC_MAX_RESULTS
         if spec.exhaustive or requested == 0 or spec.sort == "time"
         else min(requested or 40, SEMANTIC_MAX_RESULTS))
    res = _semantic_local(
        spec.q, k, level="hybrid", agent=spec.agent, project=spec.project,
        exclude_project=spec.exclude_project,
        who=spec.who,
        model=spec.model, model_soft=spec.model_soft, chat=spec.chat,
        since_ms=spec.since_ms, until_ms=spec.until_ms,
        exclude_session=spec.exclude_session,
        exclude_session_from_turn=spec.exclude_session_from_turn,
        exclude_sessions=spec.excluded_sessions,
        exclude_family=spec.exclude_family,
        family_diverse=spec.family_diverse,
        timeout_s=spec.semantic_timeout_s,
        allow_model_download=spec.allow_model_download)
    if res is None:
        return None
    hits = _filtered(
        res["hits"], spec.agent, spec.project, spec.who, spec.model,
        spec.model_soft, spec.chat, spec.since_ms, spec.until_ms,
        spec.include_tools, spec.exclude_session,
        spec.exclude_session_from_turn,
        spec.excluded_sessions,
        exclude_project=spec.exclude_project)
    semantic_meta = {
        "score_kind": res.get("score_kind"),
        "semantic_coverage": res.get("semantic_coverage"),
        "semantic_accelerator_coverage": res.get(
            "semantic_accelerator_coverage"),
        "partial": bool(res.get("partial")),
        "semantic_status": res.get("semantic_status"),
        "fallback_recommended": bool(res.get("fallback_recommended")),
        "semantic_integrity": res.get("semantic_integrity"),
    }
    return LaneResult(
        hits=hits,
        engine="semantic:hybrid",
        semantic_truncated=bool(res.get("truncated")) or (
            requested is not None and requested > SEMANTIC_MAX_RESULTS),
        semantic_meta=semantic_meta)


_SLOW_LANE_ANNOUNCED: set[str] = set()


def _announce_slow_lane(key: str, line: str) -> None:
    """One render, one fallback disclosure: recall/pack run several keyword
    queries per invocation and must not repeat the slow-lane line (keyed on
    the cause, not the text - per-query filters vary the lever tail)."""
    if key in _SLOW_LANE_ANNOUNCED:
        return
    _SLOW_LANE_ANNOUNCED.add(key)
    common.log(line)


def _scan_lever_tail(spec: QuerySpec) -> str:
    """The cheapest narrowing the query has not used yet. A measured degraded
    scan fell from 60s to 3.2s under --who, so the banner names the lever
    derived from the query's actual unused filters."""
    levers = [flag for flag, value in (
        ("--who", spec.who), ("--project", spec.project),
        ("--since", spec.since_ms)) if value is None]
    return f"; {'/'.join(levers)} narrows it" if levers else ""


def _count_early_stop(db, spec: QuerySpec, flt: dict, bounded: dict) -> None:
    """Replace a stopped lane's self-measurement with a count of the result set.

    ``observed_total`` is how far the ranked scan read before its frontier
    closed - the same magnitude for every query, and no answer to "how many
    are there". The counting lane answers that, in the one artifact ``-c``
    prints, capped so a headline never costs an unbounded scan. Refused rather
    than paid when counting would leave the index, and never claimed exact when
    the two lanes disagree about what they matched."""
    if bounded.get("totals_exact", True):
        return
    if spec.mode != "keyword" or not corpusdb.count_rides_the_index(spec.q):
        return
    try:
        counted = corpusdb.keyword_count(
            db, spec.q, flt, cap=surface.HEADLINE_COUNT_CAP)
    except sqlite3.DatabaseError as exc:
        common.dbg(f"headline count unavailable ({exc}); keeping the lane's bound", "!")
        return
    if counted["total"] < bounded["total"]:
        return
    bounded.update(total=counted["total"], chats=counted["chats"],
                   tool_hits=counted["tool_hits"],
                   totals_exact=counted["exact"], counted_total=True)
    common.dbg(f"headline count: {counted['total']} row(s), "
               f"exact={counted['exact']}")


def _jsonl_bounded_single_shape(spec: QuerySpec) -> str | None:
    """The initial exact-frontier shape; every other JSONL mode stays exhaustive."""
    query = spec.q.strip().lower()
    if (spec.mode != "keyword" or spec.exhaustive or spec.family_diverse
            or spec.sort != "score" or spec.session_limit is not None
            or spec.exclude_project is not None or spec.limit <= 0
            or spec.limit > _BOUNDED_ROW_MAX_RESULTS
            or len(query) < 3 or not query.isascii() or not query.isalnum()):
        return None
    return query


def _jsonl_bounded_single_keyword_rows(
    spec: QuerySpec, flt: dict, boundary, *, capture_sessions: bool = False,
    now_ms: float | None = None,
) -> dict | None:
    """Exact top rows and totals from one raw scan with lazy hit rendering.

    JSONL has no score-ordered sidecar, so every row is still tested and the
    aggregate counts remain exact.  An entry's full-text occurrence count and
    metadata provide a safe B=1 score ceiling. Candidates pay only for exact
    scoring; digest and event provenance are materialized for retained rows.
    """
    query = _jsonl_bounded_single_shape(spec)
    if query is None:
        return None
    import explore

    now_ms = time.time() * 1000 if now_ms is None else now_ms
    score_pattern = _match_pat(spec.q, "keyword")
    best: list[tuple[tuple, dict, dict, int, int]] = []
    worst_index = -1
    worst_key = None
    total = tool_hits = 0
    chats: set[str] = set()

    def canonical_tail(entry: dict, row_key: int) -> tuple:
        return (
            entry["session"], entry["turn"],
            0 if entry["who"] != "agent" else 1, row_key,
        )

    def retain(
            key: tuple, hit: dict, entry: dict, start: int, end: int,
    ) -> None:
        nonlocal worst_index, worst_key
        if len(best) < spec.limit:
            best.append((key, hit, entry, start, end))
        elif key < worst_key:
            best[worst_index] = (key, hit, entry, start, end)
        else:
            return
        if len(best) == spec.limit:
            worst_index, worst = max(
                enumerate(best), key=lambda item: item[1][0])
            worst_key = worst[0]

    for row_key, entry, start, end in explore.single_keyword_matches(
            spec.q, flt):
        total += 1
        chats.add(entry["session"])
        tool_hits += entry["who"] == "tool"
        occurrences = entry.get("_agrep_occurrences")
        if type(occurrences) is not int:
            occurrences = entry["low"].count(query)
        match_ceiling = 1.0 - 0.5 ** max(1, occurrences)
        age_days = max(0.0, now_ms - (entry.get("ts") or 0)) / 86_400_000
        recency = 0.5 ** (age_days / _RECENCY_HALF_LIFE_DAYS)
        speaker = entry.get("who") or ""
        if speaker == "user":
            recency = max(recency, _USER_REC_FLOOR)
        ceiling = (
            match_ceiling * recency * _WHO_W.get(speaker, 0.5)
            * _SOURCE_SCALE.get(speaker, 1.0)
            * (_META_SCALE if _meta_row(entry) else 1.0)
        )
        rounded_ceiling = math.ceil((ceiling + 1e-12) * 10_000) / 10_000
        turn = entry.get("turn")
        optimistic = (
            0, -rounded_ceiling, -(entry.get("ts") or 0), entry["session"],
            -1 if turn is None else turn, speaker,
            *canonical_tail(entry, row_key),
        )
        if worst_key is not None and optimistic >= worst_key:
            continue
        if start < 0:
            entry, start, end = explore.materialize_single_keyword_match(
                entry, spec.q, provenance=False)
        hit = explore.scan_hit(entry, start, end, provenance=False)
        hit["score"] = round(_score(
            hit, score_pattern, len(query), now_ms, terms=[query],
            boundary=boundary), 4)
        exact = (*_rank_key(hit), *canonical_tail(entry, row_key))
        retain(exact, hit, entry, start, end)

    best.sort(key=lambda item: item[0])
    retained = []
    for _key, rank_hit, entry, start, end in best:
        if "_agrep_tool_event" in entry:
            entry, start, end = explore.materialize_single_keyword_match(
                entry, spec.q)
        hit = explore.scan_hit(entry, start, end)
        for field in (
                "score", "matched", "coverage", "_evidence",
                "_boundary_class", "_boundary_factor",
                "_boundary_score_factor", "_meta_row"):
            if field in rank_hit:
                hit[field] = rank_hit[field]
        retained.append(hit)
    common.dbg(
        f"bounded JSONL rows: rendered {len(best)}/{total} exact hit(s) "
        f"for top {spec.limit}")
    result = {
        "hits": retained, "total": total,
        "chats": len(chats), "phrase_chats": len(chats),
        "tool_hits": tool_hits, "totals_exact": True,
    }
    if capture_sessions:
        result["_matched_sessions"] = chats
    return result


class NativeEventScanError(RuntimeError):
    pass


class NativeEventScanMoved(NativeEventScanError):
    pass


class SnapshotPublicationActive(RuntimeError):
    pass


class SnapshotPublicationTimeout(RuntimeError):
    pass


class NativeEventFallback(RuntimeError):
    pass


_QUERY_PUBLICATION_WAIT_S = 4.0
_QUERY_PUBLICATION_WAIT_MIN_S = 0.02
_QUERY_PUBLICATION_WAIT_MAX_S = 0.25
_QUERY_PUBLICATION_TIMEOUT = (
    "history is still updating after 4s - "
    "rerun the same agrep command")
# ranked rows a --deeper replay resumes past: the frozen chain served them
_DEEPER_SKIP_ROWS: int = 0
# Scan past duplicate/family folds without increasing the 40-row output bound.
_DEEPER_QUERY_MIN_ROWS = 512
_NATIVE_EVENT_CANDIDATE_PAGE = 512
_NATIVE_EVENT_ORPHAN_DETAILS = frozenset({
    "event row identity has no published owner",
    "legacy event filename has no published owner",
})


def _native_frontier_prefix(value: dict, *, exact: bool) -> tuple:
    matched = value.get("matched") or ""
    lane = 1 if matched in ("all-terms", "all_terms") else 0
    score = float(value["score"] if exact else value["upper_score"])
    if not exact:
        scaled = math.nextafter(score * 10_000, -math.inf)
        score = math.ceil(scaled) / 10_000
    return (lane, -float(score), -(value.get("ts") or 0), value["session"])


def _native_frontier_certified(
        response: dict, ranked: list[dict], limit: int,
) -> bool:
    if response.get("envelope_complete"):
        return True
    omitted = response.get("best_omitted")
    if omitted is None or len(ranked) < limit:
        return False
    return (_native_frontier_prefix(omitted, exact=False)
            > _native_frontier_prefix(ranked[limit - 1], exact=True))


def _native_interval_prefix(candidate: dict, field: str) -> tuple:
    lane = 1 if candidate.get("matched") in ("all-terms", "all_terms") else 0
    return (
        lane, -float(candidate[field]), -(candidate.get("ts") or 0),
        candidate["session"],
    )


def _native_hydration_candidates(response: dict, limit: int) -> list[dict]:
    """Keep every protocol-v2 candidate whose score interval can reach top-k."""
    candidates = response["candidates"]
    scanned = response.get("scanned") or {}
    if (len(candidates) <= limit
            or scanned.get("conservative_matches") != 0
            or not all(candidate.get("refined_score") for candidate in candidates)):
        return candidates
    threshold = sorted(
        _native_interval_prefix(candidate, "lower_score")
        for candidate in candidates)[limit - 1]
    count = next((index for index, candidate in enumerate(candidates)
                  if _native_interval_prefix(candidate, "upper_score") > threshold),
                 len(candidates))
    return candidates[:max(limit, count)]


def _native_hydration_frontier(response: dict, hydrated: int) -> dict:
    candidates = response["candidates"]
    if hydrated >= len(candidates):
        return response
    omitted = candidates[hydrated]
    wire_omitted = response.get("best_omitted")
    if (wire_omitted is not None
            and _native_frontier_prefix(wire_omitted, exact=False)
            < _native_frontier_prefix(omitted, exact=False)):
        omitted = wire_omitted
    return {**response, "best_omitted": omitted, "envelope_complete": False}


def _native_verify_refined_scores(
        candidates: list[dict], page_hits: list[dict], spec: QuerySpec,
        boundary, now_ms: float,
) -> bool:
    """Prove native refined intervals against canonical Python scores."""
    ordered = sorted(candidates, key=lambda item: item["ordinal"])
    if len(ordered) != len(page_hits):
        raise NativeEventFallback(
            "native score proof changed the candidate count")
    changed = False
    pat = _match_pat(spec.q, spec.mode)
    qlen = len("".join(re.split(r"[\s\-_]+", spec.q.strip())))
    terms = [
        term.lower() for term in re.split(r"[\s\-_]+", spec.q.strip())
        if term
    ]
    for candidate, hit in zip(ordered, page_hits):
        candidate_lane = (
            "all-terms" if candidate.get("matched") == "all_terms" else "")
        hit_lane = hit.get("matched") or ""
        if ((candidate.get("agent"), candidate.get("session"), candidate.get("ts"),
             candidate_lane)
                != (hit.get("agent"), hit.get("session"), hit.get("ts"), hit_lane)):
            raise NativeEventFallback(
                "native score proof does not identify its hydrated hit")
        if not candidate.get("refined_score"):
            continue
        if ("_boundary_factor" not in hit
                and "_boundary_score_factor" not in hit):
            hit["score"] = round(
                _score(
                    hit, pat, qlen, now_ms, terms=terms,
                    boundary=boundary, rec_floor=0.0),
                4)
            changed = True
        score = hit.get("score")
        if (type(score) not in (int, float) or not math.isfinite(score)
                or not candidate["lower_score"] <= score <= candidate["upper_score"]):
            raise NativeEventFallback(
                "native refined score disagrees with canonical Python ranking")
    return changed


def _native_event_shape(spec: QuerySpec) -> bool:
    tokens = [token for token in re.split(r"[\s\-_]+", spec.q.strip()) if token]
    return bool(
        spec.mode == "keyword" and not spec.exhaustive
        and spec.sort == "score" and spec.session_limit is None
        and not spec.family_diverse and spec.exclude_project is None
        and 0 < spec.limit <= 512 and 0 < len(tokens) <= 16
        and spec.q.isascii()
        and all(value is None or str(value).isascii()
                for value in (spec.agent, spec.project, spec.chat))
        and all(char.isalnum() or char.isspace() or char in "-_"
                for char in spec.q)
        and (len(tokens) > 1 or len(tokens[0]) >= 3))


def _native_bitmap_sessions(response: dict, field: str) -> set[str]:
    owners = response["_owner_order"]
    body = bytes.fromhex(response["matches"][field])
    return {
        owner["session"] for ordinal, owner in enumerate(owners)
        if body[ordinal // 8] & (1 << (ordinal % 8))
    }


def _native_storage_cursor(value: dict) -> dict:
    matched = value.get("matched") or ""
    return {
        "matched": "all_terms" if matched in ("all-terms", "all_terms") else "phrase",
        "upper_score": value["upper_score"], "ts": value["ts"],
        "session": value["session"], "ordinal": value["ordinal"],
    }


def _jsonl_native_keyword_once(
        spec: QuerySpec, flt: dict, boundary, big: int, *, preflight_ok: bool = False,
) -> dict | None:
    if not _native_event_shape(spec):
        return None
    import explore

    generation_snapshot = explore.native_event_scan_snapshot()
    if generation_snapshot is None:
        explore._kick_derived_repair()
        raise explore.NativeEventGenerationMoved(
            "derived generation is not committed")
    expected_generations = generation_snapshot["generations"]
    explore._freshen()
    flt = explore.freeze_native_event_filter(flt)
    if not preflight_ok and not explore.native_event_scan_preflight(flt):
        if not explore.native_event_scan_snapshot_current(generation_snapshot):
            raise explore.NativeEventGenerationMoved(
                "derived generation moved while resolving the caller family")
        return None

    rank_now_ms = time.time() * 1000
    tokens = [token for token in re.split(r"[\s\-_]+", spec.q.strip()) if token]
    prose_filter = {**flt, "_skip_event_rows": True}
    with explore.native_prose_snapshot_attempt(generation_snapshot):
        if len(tokens) == 1:
            prose = _jsonl_bounded_single_keyword_rows(
                spec, prose_filter, boundary, capture_sessions=True,
                now_ms=rank_now_ms)
            if prose is None:
                return None
            phrase_prose = prose["hits"]
            prose_hits = phrase_prose
        else:
            prose = explore.keyword_search(
                spec.q, big, prose_filter, row_keys=True, terms=True)
            phrase_prose = prose["hits"]
            term_hits = prose.get("term_hits")
            if term_hits is None:
                return None
            prose_hits = _augment_phrase_hits(phrase_prose, term_hits)
        owners_match = explore.native_event_owner_census_matches()
    if not owners_match:
        explore._kick_derived_repair()
        raise NativeEventScanError(
            "published session owners disagree with the message generation")

    response = explore.native_event_keyword_scan(
        spec.q, spec.limit, flt, now_ms=rank_now_ms,
        candidate_limit=_NATIVE_EVENT_CANDIDATE_PAGE, after=None,
        expected_generations=expected_generations)
    state = response.get("state")
    if state == "unsupported":
        common.dbg(
            "native event scan unavailable "
            f"({response.get('detail') or 'unsupported'}); using exact JSONL scan",
            "!")
        return None
    if state == "generation_moved":
        raise explore.NativeEventGenerationMoved(
            response.get("detail") or "native event generation moved")
    if state == "integrity_error":
        detail = response.get("detail") or "verified event scan failed integrity"
        try:
            indexd_runtime.kick_background_repair()
        finally:
            if detail in _NATIVE_EVENT_ORPHAN_DETAILS:
                raise NativeEventFallback(detail)
            raise NativeEventScanError(detail)
    if state != "ok":
        return None
    if (response["ingest_generation"], response["event_generation"]) \
            != expected_generations:
        raise explore.NativeEventGenerationMoved(
            "native event scan did not use the pinned generation")

    candidates = _native_hydration_candidates(response, spec.limit)
    hydration_response = (
        response if len(candidates) == len(response["candidates"])
        else {**response, "candidates": candidates})
    page_hits = explore.native_event_candidate_hits(spec.q, hydration_response)
    if page_hits is None:
        raise NativeEventScanError(
            "native event candidates could not be hydrated exactly")
    if len(page_hits) != len(candidates):
        raise NativeEventScanError(
            "native event hydration changed the candidate count")
    if not explore.native_event_scan_snapshot_current(generation_snapshot):
        raise explore.NativeEventGenerationMoved(
            "derived generation moved during native event hydration")
    combined = [*prose_hits, *page_hits]
    _rank(
        combined, spec.q, spec.mode, spec.sort,
        boundary=boundary, top_k=spec.limit, now_ms=rank_now_ms,
        try_native_boundary=False)
    if _native_verify_refined_scores(
            candidates, page_hits, spec, boundary, rank_now_ms):
        combined.sort(key=_rank_key)
    matches = response["matches"]
    common.dbg(
        "native event scan: page=1 "
        f"request={response.get('_request_bytes', 0)}B "
        f"scanned={response.get('scanned', {}).get('bytes', 0)}B "
        f"candidate={response.get('scanned', {}).get('candidate_bytes', 0)}B "
        f"matched={matches['tools']} hydrated={len(page_hits)}")
    frontier = _native_hydration_frontier(response, len(candidates))
    if not _native_frontier_certified(frontier, combined, spec.limit):
        raise NativeEventFallback(
            "native score envelope remained ambiguous after one bounded scan")

    if len(tokens) == 1:
        prose_sessions = prose["_matched_sessions"]
        matched_sessions = _native_bitmap_sessions(
            response, "matched_owner_bitmap")
        chats = len(prose_sessions | matched_sessions)
        return {
            "hits": combined[:spec.limit], "pre_ranked": True,
            "bounded_rows": {
                "hits": combined[:spec.limit],
                "total": prose["total"] + matches["tools"],
                "chats": chats, "phrase_chats": chats,
                "tool_hits": matches["tools"], "totals_exact": True,
            },
            "terms_fallback": False, "terms_augmented": False,
            "rank_now_ms": rank_now_ms,
        }
    phrase_count = len(phrase_prose) + matches["phrase_tools"]
    matched_sessions = _native_bitmap_sessions(
        response, "matched_owner_bitmap")
    phrase_sessions = _native_bitmap_sessions(
        response, "phrase_owner_bitmap")
    prose_sessions = {hit["session"] for hit in prose_hits}
    phrase_prose_sessions = {hit["session"] for hit in phrase_prose}
    total = len(prose_hits) + matches["tools"]
    return {
        "hits": combined[:spec.limit], "pre_ranked": True,
        "bounded_rows": {
            "hits": combined[:spec.limit], "total": total,
            "chats": len(prose_sessions | matched_sessions),
            "phrase_chats": len(phrase_prose_sessions | phrase_sessions),
            "tool_hits": matches["tools"], "totals_exact": True,
        },
        "terms_fallback": not phrase_count and bool(total),
        "terms_augmented": bool(phrase_count) and total > phrase_count,
        "rank_now_ms": rank_now_ms,
    }


def _jsonl_native_keyword(
        spec: QuerySpec, flt: dict, boundary, big: int, *,
        preflight_ok: bool = False, preflight_checked: bool = False,
) -> dict | None:
    import explore

    if preflight_checked and not preflight_ok:
        return None
    try:
        return _jsonl_native_keyword_once(
            spec, flt, boundary, big, preflight_ok=preflight_ok)
    except NativeEventFallback as exc:
        common.dbg(f"{exc}; using exact JSONL scan", "!")
        return None
    except explore.NativeEventGenerationMoved as exc:
        raise NativeEventScanMoved(str(exc)) from exc
    except explore.DirectSnapshotMoved as exc:
        raise DirectSnapshotQueryMoved(str(exc)) from exc
    except explore.DirectSnapshotError as exc:
        raise DirectSnapshotQueryError(str(exc)) from exc


def _keyword_candidates_once(spec: QuerySpec) -> LaneResult:
    if spec.mode == "regex":
        re.compile(spec.q, re.I)
    _load_corpusdb()
    db = corpusdb.connect(allow_stale=True)
    if (db is not None and spec.exhaustive
            and getattr(db, "_source_stamp_current", None) is False):
        if corpusdb.query_publication_active():
            # A busy box republishes every few seconds, invalidating the direct
            # scan until the 4s "rerun" refusal: last-good counts with their
            # freshness disclosure beat that; the quiet path still scans exact.
            common.dbg("exhaustive lane keeping a behind snapshot: "
                       "publisher active, counts are generation-aged")
        else:
            db.close()
            db = None
    search_index_building = bool(
        db is None and corpusdb.query_search_index_build_active())
    if (db is None and not search_index_building
            and corpusdb.query_publication_active()):
        raise SnapshotPublicationActive(
            "a verified publisher is updating the query generation")
    try:
        # Boundary evidence, candidates, and counts are one answer; close releases
        # the pinned generation after every SQL lane has finished reading it.
        if db is not None and getattr(db, "in_transaction", None) is False:
            db.execute("BEGIN")
        boundary = _prepare_boundary(spec.q, spec.mode, db)
    except sqlite3.DatabaseError as exc:
        corpusdb.bind_query_database_error(exc, db)
        if db is not None:
            try:
                db.close()
            except sqlite3.DatabaseError as close_exc:
                corpusdb.bind_query_database_error(close_exc, db)
        raise
    except Exception:
        if db is not None:
            db.close()
        raise
    result = LaneResult(hits=[], engine="", boundary=boundary)
    big = 10_000_000  # engines materialize confirmed hits anyway; rank needs them all
    include_tools = spec.include_tools
    flt = {
        "agent": spec.agent, "project": spec.project, "who": spec.who,
        "model": spec.model, "model_soft": spec.model_soft, "chat": spec.chat,
        "since_ms": spec.since_ms, "until_ms": spec.until_ms,
        "include_tools": include_tools, "exclude_session": spec.exclude_session,
        "exclude_session_from_turn": spec.exclude_session_from_turn,
    }
    if spec.excluded_sessions:
        flt["_exclude_sessions"] = spec.excluded_sessions
    if not spec.exclude_family:
        flt["exclude_family"] = False
    if db is None:
        import explore

        flt = explore.freeze_tool_lane_filter(flt)
    tool_lane_requested = bool(
        db is None and flt.get("_tool_lane_enabled"))

    def tool_lane_only() -> bool:
        return bool(
            tool_lane_requested
            and not any(
                surface.speaker_filter_admits(spec.who, speaker)
                for speaker in surface.SPEAKER_CHOICES
                if speaker != "tool"))

    search_index_pending = bool(
        db is None and corpusdb._trigram_ok() and search_index_building)
    if search_index_pending:
        if tool_lane_only():
            result.tools_excluded = True
            result.engine = "none"
            return result
        if tool_lane_requested:
            include_tools = False
            result.tools_excluded = True
        flt["include_tools"] = include_tools
        flt["_tool_lane_enabled"] = False
    native = None
    native_checked = False
    if db is None and not search_index_pending and _native_event_shape(spec):
        native_checked = True
        if explore._native_event_lane_enabled(flt):
            native = _jsonl_native_keyword(spec, flt, boundary, big)

    common.dbg(f"engine: {'corpusdb (FTS index)' if db else 'explore (JSONL scan)'}")
    if search_index_pending:
        pass
    elif db is None and native_checked:
        if corpusdb.DB_PATH.exists():
            indexd_runtime.kick_background_repair()
    elif (db is None and corpusdb._trigram_ok()
            and not corpusdb.DB_PATH.exists()):
        # One durable fact governs this window: a queued FTS build means the
        # scan serves prose only while tools join later; with nothing queued
        # the drop has no absorber, so the scan serves tool rows itself.
        pending = indexd_runtime.search_index_build_pending()
        if tool_lane_only() and pending:
            # its whole result set is the excluded lane: refuse with the
            # tools-pending story, never a zero claiming the full corpus
            result.tools_excluded = True
            result.engine = "none"
            return result
        if tool_lane_requested and pending:
            # A prose-only spec (recall's primary lane) gave up nothing - no downgrade to announce.
            include_tools = False
            result.tools_excluded = True
            flt["_tool_lane_enabled"] = False
    elif db is None and corpusdb.DB_PATH.exists():
        # An unusable search db is agrep's own damage: repair starts here, not
        # in a sentence (law 1), and the scan serves silently while it runs
        # (law 3). A held foreign claim is the one cause worth a line (law 5).
        kick = indexd_runtime.kick_background_repair()
        if kick.cause in ("held-foreign-owner", "owner-unverifiable"):
            _announce_slow_lane(
                "db-unusable",
                "the search index is held by another agrep - scanning "
                f"sources this query{_scan_lever_tail(spec)}")
    elif db is None and not corpusdb._trigram_ok() and common.MESSAGES_PATH.exists():
        # legacy sqlite: the scan is this build's only engine - announce it
        # (no rebuild promise: a rebuild cannot grow this python's sqlite)
        _announce_slow_lane(
            "no-trigram",
            "this python's sqlite has no trigram FTS - scanning the "
            "published snapshot directly (a full scan; expect tens of "
            f"seconds{_scan_lever_tail(spec)})")
    elif db is None:
        # D2: no index and nobody owns a build. The old path here scanned a
        # corpus-scale JSONL in silence; die honestly with the remedy instead.
        result.index_missing = True
        result.engine = "none"
        return result
    flt["include_tools"] = include_tools
    try:
        if db:
            # the bounded SQL lanes pre-cap inside corpusdb, which never sees
            # --exclude-project: exclusion takes the exhaustive path so the
            # subtraction happens on full candidates, before top-k
            if (spec.mode == "keyword" and spec.exhaustive and spec.exact_totals
                    and spec.limit > 0 and spec.session_limit is None
                    and spec.exclude_project is None):
                counted = corpusdb.keyword_count(db, spec.q, flt)
                content = _content_terms(spec.q)
                if counted["total"] or not (
                        spec.allow_fallback and _nl_query(spec.q, content)):
                    common.dbg(f"count-only keyword: {counted['total']} row(s)")
                    result.bounded_rows = {
                        **counted, "hits": [], "totals_exact": True,
                    }
            if (result.bounded_rows is None
                    and spec.mode == "keyword" and spec.sort == "score"
                    and spec.session_limit is None and spec.limit > 0
                    and spec.exclude_project is None):
                if not spec.exact_totals:
                    result.bounded_rows = _bounded_short_keyword_rows(
                        db, spec.q, spec.limit, flt, boundary=boundary)
                    if result.bounded_rows is None:
                        result.bounded_rows = _bounded_keyword_rows(
                            db, spec.q, spec.limit, flt, spec.allow_fallback,
                            boundary=boundary)
                if result.bounded_rows is None and spec.exact_totals:
                    result.bounded_rows = _bounded_single_keyword_rows(
                        db, spec.q, spec.limit, flt, boundary=boundary)
            if result.bounded_rows is not None:
                _count_early_stop(db, spec, flt, result.bounded_rows)
                result.hits = result.bounded_rows["hits"]
                result.terms_fallback = result.bounded_rows.get(
                    "terms_fallback", False)
                result.terms_augmented = result.bounded_rows.get(
                    "terms_augmented", False)
                result.pre_ranked = True
                result.engine = "corpusdb"
            elif (not spec.exact_totals and spec.mode == "keyword"
                  and spec.sort == "score" and spec.session_limit is not None
                  and spec.session_limit > 0
                  and spec.exclude_project is None):
                result.bounded_sessions = _bounded_short_keyword_sessions(
                    db, spec.q, spec.session_limit, flt, boundary=boundary,
                    family_diverse=spec.family_diverse)
                if result.bounded_sessions is None:
                    result.bounded_sessions = _bounded_keyword_sessions(
                        db, spec.q, spec.session_limit, flt, spec.allow_fallback,
                        boundary=boundary, family_diverse=spec.family_diverse)
            if (result.bounded_rows is None
                    and result.bounded_sessions is not None):
                _count_early_stop(db, spec, flt, result.bounded_sessions)
                result.hits = result.bounded_sessions["hits"]
                result.terms_fallback = result.bounded_sessions["terms_fallback"]
                result.terms_augmented = result.bounded_sessions["terms_augmented"]
                result.pre_ranked = True
                result.engine = "corpusdb"
            elif result.bounded_rows is None:
                if _want_terms_fallback(spec.mode, spec.q):
                    both = corpusdb.keyword_terms(
                        db, spec.q, big, flt,
                        position_order=spec.sort == "position")
                    phrase_hits = both["phrase"]["hits"]
                    result.hits = _augment_phrase_hits(
                        phrase_hits, both["terms"]["hits"])
                    result.terms_fallback = not phrase_hits
                    result.terms_augmented = (
                        bool(phrase_hits) and len(result.hits) > len(phrase_hits))
                else:
                    fn = {"word": corpusdb.word, "regex": corpusdb.regex}.get(
                        spec.mode, corpusdb.keyword)
                    result.hits = fn(
                        db, spec.q, big, flt,
                        position_order=spec.sort == "position")["hits"]
                result.hits = _excluding_project(
                    result.hits, spec.exclude_project)
                if (spec.allow_fallback and not result.hits
                        and spec.mode == "keyword"):
                    ct = _content_terms(spec.q)
                    if _nl_query(spec.q, ct):
                        result.hits = _excluding_project(
                            corpusdb.content(db, " ".join(ct), big, flt)["hits"],
                            spec.exclude_project)
                        result.content_fallback = True
                result.engine = "corpusdb"
        else:
            import explore
            if not native_checked and not search_index_pending:
                native = _jsonl_native_keyword(spec, flt, boundary, big)
            if native is not None:
                bounded = native["bounded_rows"]
                result.hits = native["hits"]
                result.bounded_rows = bounded
                result.pre_ranked = native["pre_ranked"]
                result.terms_fallback = native["terms_fallback"]
                result.terms_augmented = native["terms_augmented"]
                result.rank_now_ms = native["rank_now_ms"]
                result.engine = "jsonl+native-events"
            else:
                try:
                    with explore.direct_snapshot_attempt(
                            include_events=explore._native_event_lane_enabled(flt)):
                        bounded = _jsonl_bounded_single_keyword_rows(
                            spec, flt, boundary)
                        if bounded is not None:
                            result.bounded_rows = bounded
                            result.hits = bounded["hits"]
                            result.pre_ranked = True
                        elif spec.mode == "word":
                            res = _word_scan(spec.q, big, flt)
                        elif spec.mode == "regex":
                            res = _regex_scan(spec.q, big, flt)
                        else:
                            res = explore.keyword_search(
                                spec.q, big, flt,
                                row_keys=_want_terms_fallback(spec.mode, spec.q),
                                terms=_want_terms_fallback(spec.mode, spec.q))
                        if bounded is None:
                            result.hits = res["hits"]
                        if (bounded is None
                                and _want_terms_fallback(spec.mode, spec.q)):
                            phrase_hits = result.hits
                            term_hits = res.get("term_hits")
                            if term_hits is None:
                                term_hits = _terms_scan(
                                    spec.q, big, flt)["hits"]
                            result.hits = _augment_phrase_hits(
                                phrase_hits, term_hits)
                            result.terms_fallback = not phrase_hits
                            result.terms_augmented = (
                                bool(phrase_hits)
                                and len(result.hits) > len(phrase_hits))
                        if bounded is None:
                            result.hits = _excluding_project(
                                result.hits, spec.exclude_project)
                        if (bounded is None and spec.allow_fallback
                                and not result.hits and spec.mode == "keyword"):
                            ct = _content_terms(spec.q)
                            if _nl_query(spec.q, ct):
                                result.hits = _excluding_project(
                                    _content_scan(ct, big, flt)["hits"],
                                    spec.exclude_project)
                                result.content_fallback = True
                        result.engine = "jsonl"
                except explore.DirectSnapshotMoved as exc:
                    raise DirectSnapshotQueryMoved(str(exc)) from exc
                except explore.DirectSnapshotError as exc:
                    raise DirectSnapshotQueryError(str(exc)) from exc
    except sqlite3.DatabaseError as exc:
        corpusdb.bind_query_database_error(exc, db)
        raise
    finally:
        if db:
            try:
                if spec.family_diverse and result.hits:
                    roots = corpusdb.session_family_roots(
                        db, (hit.get("session") for hit in result.hits))
                    for hit in result.hits:
                        session = str(hit.get("session") or "")
                        if session:
                            hit["_family_root"] = roots.get(session, session)
            except sqlite3.DatabaseError as exc:
                corpusdb.bind_query_database_error(exc, db)
                raise
            finally:
                try:
                    db.close()
                except sqlite3.DatabaseError as exc:
                    corpusdb.bind_query_database_error(exc, db)
                    raise

    result.terms_fallback = (
        result.terms_fallback and not result.content_fallback)
    if result.terms_fallback:
        for hit in result.hits:
            hit["matched"] = "all-terms"
        common.dbg(
            f"keyword: no in-order phrase match -> {len(result.hits)} bag-of-words hit(s)")
    elif result.terms_augmented:
        common.dbg(
            "keyword: preserved thin phrase hits and added terms-only superset "
            f"({len(result.hits)} total)")
    if result.content_fallback:
        for hit in result.hits:
            hit["matched"] = "content-terms"
        common.dbg(
            f"keyword: all raw-token tiers empty -> {len(result.hits)} content-terms hit(s)")
    return result


def _keyword_candidates(spec: QuerySpec) -> LaneResult:
    busy_retried = False
    fallback_retried = False
    movement_retried = False
    publication_deadline = None
    publication_delay = _QUERY_PUBLICATION_WAIT_MIN_S
    while True:
        publication_error = None
        try:
            return _keyword_candidates_once(spec)
        except sqlite3.DatabaseError as exc:
            kind = corpusdb.query_database_error_kind(exc)
            corpusdb.record_query_database_error(exc)
            if kind == "transient":
                if busy_retried:
                    raise QueryDatabaseBusyError(
                        "search index is busy updating; retry") from exc
                busy_retried = True
            else:
                if fallback_retried:
                    raise QueryDatabaseUnavailableError(
                        "search index is temporarily unavailable; retry") from exc
                fallback_retried = True
        except SnapshotPublicationActive as exc:
            publication_error = exc
        except (DirectSnapshotQueryMoved, NativeEventScanMoved) as exc:
            publisher_active = corpusdb.query_publication_active()
            if not movement_retried:
                movement_retried = True
                if not publisher_active:
                    continue
            elif not publisher_active:
                raise
            publication_error = exc
        except (DirectSnapshotQueryError, NativeEventScanError):
            raise
        if publication_error is None:
            continue
        now = time.monotonic()
        if publication_deadline is None:
            publication_deadline = now + _QUERY_PUBLICATION_WAIT_S
        remaining = publication_deadline - now
        if remaining <= 0:
            raise SnapshotPublicationTimeout(
                _QUERY_PUBLICATION_TIMEOUT) from publication_error
        time.sleep(min(publication_delay, remaining))
        publication_delay = min(
            publication_delay * 2, _QUERY_PUBLICATION_WAIT_MAX_S)


def _finalize_query(spec: QuerySpec, result: LaneResult) -> dict:
    hits = result.hits
    # Plain counts discard rows, while tier diagnostics still need boundary classes.
    if not result.pre_ranked and not (
            spec.exhaustive and spec.mode != "semantic" and spec.limit > 0):
        hits = _rank(
            hits, spec.q, spec.mode, spec.sort, boundary=result.boundary,
            refine_all=bool(
                (spec.exhaustive and spec.limit == 0)
                or (spec.session_limit is not None and spec.session_limit != 1)),
            top_k=None if spec.limit == 0 else max(40, spec.limit),
            now_ms=result.rank_now_ms)
    phrase_chats = None
    if result.bounded_rows is not None:
        total = result.bounded_rows["total"]
        chats = result.bounded_rows["chats"]
        phrase_chats = result.bounded_rows.get("phrase_chats")
        selected = hits
        tool_hits = result.bounded_rows["tool_hits"]
    elif result.bounded_sessions is not None:
        total = result.bounded_sessions["total"]
        chats = result.bounded_sessions["chats"]
        selected = (
            _family_heads(hits, spec.session_limit or 0)
            if spec.family_diverse else hits)
        tool_hits = result.bounded_sessions["tool_hits"]
    else:
        total, chats = len(hits), len({hit["session"] for hit in hits})
        if spec.mode != "semantic":
            phrase_chats = len({
                hit["session"] for hit in hits
                if hit.get("matched") not in ("all-terms", "content-terms")
            })
        if spec.session_limit is not None or spec.mode == "semantic":
            requested_heads = (
                spec.session_limit
                if spec.session_limit is not None else spec.limit)
            head_limit = (
                requested_heads if spec.mode == "semantic"
                else 0 if spec.family_diverse else requested_heads)
            selected = _session_heads(hits, head_limit)
            if spec.family_diverse and spec.mode != "semantic":
                selected = _family_heads(selected, requested_heads)
        else:
            selected = hits if spec.limit == 0 else hits[:spec.limit]
        tool_hits = sum(1 for hit in hits if hit.get("who") == "tool")
    out = {
        "hits": selected, "total": total, "chats": chats,
        "engine": result.engine, "mode": spec.mode, "tool_hits": tool_hits,
        "returned_chats": len({hit["session"] for hit in selected}),
    }
    if phrase_chats is not None:
        out["phrase_chats"] = phrase_chats
    bounded = result.bounded_rows or result.bounded_sessions
    if bounded is not None:
        out["totals_exact"] = bounded["totals_exact"]
        if bounded.get("counted_total"):
            # only a counted total is a fact about the result set; its absence
            # says the number beside it is still the lane's own stopping point
            out["total_counted"] = True
    elif not spec.exact_totals and spec.mode != "semantic":
        out["totals_exact"] = True
    if spec.mode == "semantic":
        out.update(result.semantic_meta)
        status = result.semantic_meta.get("semantic_status") or {}
        out["totals_exact"] = (
            bool(status.get("complete")) and not result.semantic_truncated)
        if result.semantic_truncated:
            out["truncated"] = True
    if result.terms_fallback:
        out["terms_fallback"] = True
    if result.terms_augmented:
        out["terms_augmented"] = True
    if result.content_fallback:
        out["content_fallback"] = True
    if result.index_missing:
        out["index_missing"] = True
    if result.tools_excluded:
        # a lane that excluded tool rows measured a narrowed corpus: its
        # total is a floor, and every surface downstream sees the narrowing
        out["tools_excluded"] = True
        out["totals_exact"] = False
    return out


def run_query(q: str, *, mode: str = "keyword", limit: int = 40, sort: str = "score",
              agent: str | None = None, project: str | None = None,
              who: str | surface.SpeakerFilter | None = None,
              model: str | None = None, model_soft: bool = False,
              chat: str | None = None, since_ms: int | None = None,
              until_ms: int | None = None,
              exhaustive: bool = False, session_limit: int | None = None,
              include_tools: bool = True,
              exclude_session: str | None = None,
              exclude_session_from_turn: int | None = None,
              _exclude_sessions: tuple[str, ...] = (),
              exclude_family: bool = True,
              allow_fallback: bool = True,
              exact_totals: bool = True,
              family_diverse: bool | None = None,
              semantic_timeout_s: float | None = None,
              semantic_process_guard: bool = False,
              allow_model_download: bool = False,
              exclude_project: str | None = None) -> dict | None:
    """The one query layer all callers share: dispatch to the right engine
    (corpusdb keyword/word/regex, JSONL scans when unavailable, the in-process
    semantic lane), filter, rank, cap. No printing; raises re.error on a bad regex.
    Returns {hits, total, chats, engine, mode} with totals computed PRE-cap over the
    filtered set, or None when semantic can't answer (stale/refreshing embeddings -
    the caller serves keyword instead).
    `session_limit=N` switches only the returned hits to a session-level view: the
    best globally-ranked hit from each of the top N chats. ``exact_totals=False`` is
    an opt-in keyword/session optimization: heads stay exact while aggregate counts
    become observed lower bounds and carry ``totals_exact=False``. Ordinary search
    and probes retain exhaustive totals by default. Caller-family filters run before
    top-k so self echoes cannot mask past hits."""
    if session_limit is not None:
        session_limit = max(0, int(session_limit))
    if family_diverse is None:
        family_diverse = mode == "semantic"
    spec = QuerySpec(
        q=q, mode=mode, limit=limit, sort=sort, agent=agent, project=project,
        who=who, model=model, model_soft=model_soft, chat=chat, since_ms=since_ms,
        until_ms=until_ms, exhaustive=exhaustive, session_limit=session_limit,
        include_tools=include_tools, exclude_session=exclude_session,
        exclude_session_from_turn=exclude_session_from_turn,
        excluded_sessions=tuple(_exclude_sessions),
        exclude_family=exclude_family,
        allow_fallback=allow_fallback,
        exact_totals=exact_totals, family_diverse=bool(family_diverse),
        semantic_timeout_s=semantic_timeout_s,
        allow_model_download=allow_model_download,
        exclude_project=exclude_project)
    if spec.mode == "regex":
        return _guarded_regex_query(spec)
    if spec.mode == "semantic" and semantic_process_guard:
        return _guarded_semantic_query(spec)
    candidates = (
        _semantic_candidates(spec) if spec.mode == "semantic"
        else _keyword_candidates(spec))
    return None if candidates is None else _finalize_query(spec, candidates)


def _group(hits):
    """hits -> OrderedDict[session] = list of hits, preserving first-seen order."""
    from collections import OrderedDict
    g = OrderedDict()
    for h in hits:
        g.setdefault(h["session"], []).append(h)
    return g


def _is_side(hs, roots: dict[str, str] | None = None) -> bool:
    """Is this chat a side session? Sideness lives on the SESSION (sessions.jsonl
    `parent`), not on whichever row happened to match - an agent-reply hit inside a
    subagent chat is still a side chat."""
    if any(h.get("who") == "subagent" for h in hs):
        return True
    session = str(hs[0].get("session") or "")
    roots = roots if roots is not None else _family_roots_for_hits(hs)
    return bool(session and roots.get(session, session) != session)


# prose rows, not raw transcript turns: the corpus median is 1 and p99 is 40,
# so 150+ prose rows is a marathon session whose one family window can't
# represent it
_MEGA_SESSION_ROWS = 150


def _mega_session_hint(q: str, top: dict, args) -> str | None:
    """Family-diversity gives one window per giant session - when the best
    match lives inside one, name the recovery move (narrowing exists but
    nobody discovers --chat + -s on their own)."""
    if args.chat:
        return None  # already narrowed
    sess = top.get("session") or ""
    rows = common.indexed_session_prose_count(sess)
    if rows is None:
        return None
    if rows < _MEGA_SESSION_ROWS:
        return None
    import recall as _recall
    session_index = common.indexed_session_prefix_candidates((sess,))
    target = compact.encode_session_target(sess, session_index=session_index)
    cmd = _recall._command("agrep", q, "-s", "--chat", target)
    return (f"top match is inside a {rows:,}-turn session - narrow: {cmd}")


def _chat_head(hs0, n, color, side=False, session_index=None):
    """A chat's header line: agent, project, topic, handle, and hit count.

    The @session:turn handle is the universal follow-up affordance: paste into
    `agrep around <session> <turn>` (compact's --more accepts it too)."""
    label = terminal_safe(hs0.get("concept") or _proj(hs0["project"]))
    agent = terminal_safe(hs0.get("agent") or "")
    project = terminal_safe(_proj(hs0.get("project") or ""))
    crumbs = f"{agent} · {project}"
    if label and label != project:
        crumbs += f" · {label}"
    if side or hs0.get("who") == "subagent":
        crumbs += " · [side chat]"
    if hs0.get("_self"):
        crumbs += " · ~self"
    turn = hs0.get("turn")
    session = str(hs0.get("session") or "")
    handle = (compact.encode_bound_result_handle(
        hs0, session_index=session_index) if session and turn is not None
        else compact.encode_session_target(session, session_index=session_index))
    handle = terminal_safe(handle)
    cnt = f"{n} hit{'s' if n != 1 else ''}"
    if color:
        s = f"{_C['hd']}{crumbs}{_C['r']}  {_C['d']}{handle} · {cnt}{_C['r']}"
    else:
        s = f"{crumbs}  [{handle} · {cnt}]"
    return s


_ENCODED_RUN = re.compile(r"[A-Za-z0-9+/=_-]{40,}")


def _match_in_encoded_blob(snippet: str, pat: re.Pattern | None) -> bool:
    """Every occurrence sits strictly inside a long mixed-alphabet run.

    Transport encoding fabricates clean camelCase boundaries (any r->D
    transition in base64), so boundary quality alone certifies garbage;
    entropy lives in the enclosing token, not the match edges. A match at a
    run's start survives - grepping a blob by its prefix is deliberate."""
    if pat is None or not snippet:
        return False
    matches = [m.span() for m in pat.finditer(snippet)]
    if not matches:
        return False
    runs = [m.span() for m in _ENCODED_RUN.finditer(snippet)
            if (run := m.group())
            and any(c.islower() for c in run)
            and any(c.isupper() for c in run)
            and any(c.isdigit() for c in run)]
    return all(any(rs < s and e < re_ for rs, re_ in runs)
               for s, e in matches)


def _demote_meta(hits: list[dict]) -> tuple[list[dict], int]:
    """Sink tooling-about-history below lived rows, never drop it.

    Scoring alone cannot fix a corpus that tests its own tooling: when every
    term hit is a fixture or a spawned task turn, a 0.45 factor reorders
    nothing. Display order puts lived prose first and says how many meta rows
    sank, so a reader can still reach them (--json keeps grep parity)."""
    lived = [h for h in hits if not h.get("_meta_row")]
    meta = [h for h in hits if h.get("_meta_row")]
    return ([*lived, *meta], len(meta) if lived else 0)


def _exact_fold_eligible(h: dict) -> bool:
    """Tool/event rows need explicit structural success to fold: a
    byte-identical FAILED call must never hide behind an ok copy."""
    if h.get("who") == "tool" or h.get("event_kind") or h.get("kind"):
        return h.get("ok") is True
    return True


def _collapse_identical(hits: list[dict]) -> list[dict]:
    """Fold byte-identical rows from other chats into the best-ranked copy.

    Fan-out agents repeat one prompt into dozens of sibling sessions; a page
    that spends twenty rows on one sentence buries the other nineteen answers.
    Display lanes only - machine surfaces (-c/--flat/--json) keep every row,
    which is also where the folded chats' identities remain reachable.
    Rows inside the representative's own chat stay: repetition within one
    conversation is structure, not spam.
    A lived copy always represents a group that also has meta copies: the
    fold carries the representative's ~meta flag, and a folded row must not
    be demoted for a fixture echo it merely absorbed."""
    kept: list[dict] = []
    first: dict[tuple, dict] = {}
    pos: dict[int, int] = {}
    folded: dict[int, set] = {}
    for h in hits:
        digest = h.get("content_digest") if _exact_fold_eligible(h) else None
        key = (digest, h.get("snippet"), h.get("who"))
        rep = first.get(key) if digest else None
        if rep is None:
            if digest:
                first[key] = h
                pos[id(h)] = len(kept)
            kept.append(h)
        elif h.get("session") == rep.get("session"):
            kept.append(h)
        elif rep.get("_meta_row") and not h.get("_meta_row"):
            i = pos.pop(id(rep))
            kept[i] = first[key] = h
            pos[id(h)] = i
            folded[id(h)] = folded.pop(id(rep), set()) | {rep.get("session")}
        else:
            folded.setdefault(id(rep), set()).add(h.get("session"))
    for h in kept:
        sessions = folded.get(id(h))
        if sessions:
            # identities, not just a count: the near pass composes by set
            # union, so exact-fold lineage must stay session-exact
            h["_dup_sessions"] = sorted(str(s) for s in sessions if s)
            h["_dup_chats"] = len(h["_dup_sessions"])
    return kept


_NEAR_KEY_FIELDS = ("who", "event_kind", "kind", "name", "status",
                    "agent", "project")


def _near_fold(hits: list[dict], roots: dict[str, str]) -> list[dict]:
    """Lineage near-fold beneath the exact fold - display lanes only.

    The private `_near_dedup_key` is the owner-derived template identity,
    built from structured columns: known-volatile identity fields
    (session/turn/ts) are normalized away, every structured payload column
    stays in. Rows whose columns will not bound stay unkeyed, and
    display_policy refuses to fold anything unkeyed, failed, or lived."""
    for h in hits:
        parts = []
        for field in _NEAR_KEY_FIELDS:
            value = h.get(field) or ""
            if type(value) is not str:
                parts = None
                break
            parts.append(value)
        if parts is None:
            continue
        key = "\x1f".join(parts)
        if key.strip("\x1f") and len(key) <= 256:
            h["_near_dedup_key"] = key
    return display_policy.collapse_display_rows(hits, roots)


def _row_hl_pat(hit, pat, terms_pat):
    """Any-order rows highlight their terms; every other row keeps the mode
    pattern (shared by both classic emitters so they mark identically)."""
    if terms_pat is not None and hit.get("matched") in (
            "all-terms", "content-terms"):
        return terms_pat
    return pat


def _emit_grouped(hits, pat, color, terms_pat=None):
    """ripgrep-style: a header per chat, its matching turns indented beneath."""
    hits = _collapse_identical(hits)
    roots = _family_roots_for_hits(hits)
    # ~meta demotion speaks through the row markers alone (law 7): a banner
    # restating what every affected row already shows is a second spelling.
    hits, _ = _demote_meta(_near_fold(hits, roots))
    session_index = common.indexed_session_prefix_candidates(
        hit.get("session") for hit in hits)
    for i, (_, hs) in enumerate(_group(hits).items()):
        if i:
            print()
        print(_chat_head(hs[0], hs[0].get("session_hits", len(hs)), color,
                         side=_is_side(hs, roots),
                         session_index=session_index))
        for h in hs:
            turn = h.get("turn")
            tn = str(turn) if turn is not None else "·"
            who = h.get("who")
            mark = surface.speaker_glyph(who)
            snip = (
                h["_regex_color_snippet"]
                if color and "_regex_color_snippet" in h
                else _hl(h["snippet"], _row_hl_pat(h, pat, terms_pat), color))
            # The weak mark preserves uncertainty above the acceptance floor.
            weak = " ~weak" if _semantic_row_weak(h) else ""
            self_mark = " ~self" if h.get("_self") else ""
            if h.get("_meta_row"):
                self_mark += " ~meta"
            if _match_in_encoded_blob(h.get("snippet") or "", pat):
                self_mark += " ~encoded"
            dup = h.get("_dup_chats")
            dup_mark = f" ×{dup} chats" if dup else ""
            if color:
                print(f"  {_C['y']}{tn:>4}{_C['r']} {_C['d']}{mark}{_C['r']} {snip}"
                      f"{_C['d']}{weak}{self_mark}{dup_mark}{_C['r']}")
            else:
                print(f"  {tn:>4} {mark} {snip}{weak}{self_mark}{dup_mark}")


def _emit_flat(hits, pat, color, terms_pat=None):
    """One TAB-separated row per hit for piping: session, agent, project, turn, who,
    snippet. Stable columns so awk/cut compose; the piped default outside agent
    shells (agent contexts page compact instead - --flat restores this shape)."""
    # Hot at scale (50k rows): pure per-call memos for the repeating identity
    # columns, direct binding past the terminal_safe compatibility aliases,
    # and batched writes replacing one buffered print per row.
    safe = surface.terminal_safe
    prefixes: dict = {}
    speakers: dict = {}
    lines: list[str] = []
    write = sys.stdout.write
    for h in hits:
        turn = h.get("turn")
        if color:
            snippet = (
                h["_regex_color_snippet"]
                if "_regex_color_snippet" in h
                else _hl(h["snippet"], _row_hl_pat(h, pat, terms_pat), color))
        else:
            snippet = safe(h["snippet"])
        key = (h.get("session"), h.get("agent"), h.get("project"))
        try:
            prefix = prefixes[key]
        except KeyError:
            prefix = prefixes[key] = (
                f"{safe(key[0])}\t{safe(key[1])}\t{safe(_proj(key[2] or ''))}")
        except TypeError:  # unhashable identity value: render without the memo
            prefix = (f"{safe(key[0])}\t{safe(key[1])}"
                      f"\t{safe(_proj(key[2] or ''))}")
        who = h.get("who") or ""
        who_safe = speakers.get(who)
        if who_safe is None:
            who_safe = speakers[who] = safe(who)
        lines.append(f"{prefix}\t{'' if turn is None else safe(turn)}"
                     f"\t{who_safe}\t{snippet}\n")
        if len(lines) >= 4096:
            write("".join(lines))
            lines.clear()
    if lines:
        write("".join(lines))


def public_rows(
        hits: list[dict], *, result_handles: bool = False,
) -> list[dict]:
    """Serialization boundary for machine consumers: rows leave as copies with
    ranking internals stripped. Optional handles use a persisted claim or full
    source text; an elided snippet can never mint a false content identity."""
    rows = [{key: value for key, value in hit.items()
             if not key.startswith("_") and key != "content_digest"}
            for hit in hits]
    if result_handles:
        session_index = common.indexed_session_prefix_candidates(
            hit.get("session") for hit in hits)
        for hit, row in zip(hits, rows):
            text = hit.get("text")
            authoritative = text if isinstance(text, str) and text else None
            try:
                row["handle"] = compact.encode_bound_result_handle(
                    hit, session_index=session_index, text=authoritative)
            except compact.CompactError:
                pass
    return rows


_RANKING_VERSION = "boundary-hybrid-v3-tool"


def _compact_snippet(hit: dict, pat: re.Pattern | None, prepared) -> str:
    # Bounds decision: skip. payload_bounds are offsets into the original
    # event text this row no longer carries; the payload-first cut happened
    # at the real match seam, and the re-cut must never reuse the span.
    text = hit.get("snippet") or ""
    if "_regex_compact_snippet" in hit:
        return hit["_regex_compact_snippet"]
    if hit.get("matched") == "all-terms":
        score = prepared.evaluate(text)
        spans = [span for span in score.spans if span is not None]
        return common.snip_spans(text, spans, 32) if spans else text
    match = pat.search(text) if pat is not None else None
    return _snip_at(text, match.start(), match.end(), 32) if match else text


def _compact_line(hit: dict, session_index: tuple[str, ...]) -> str:
    handle = compact.encode_bound_result_handle(
        hit, session_index=session_index)
    owner = "/".join(filter(None, (terminal_safe(hit.get("agent") or "-"),
                                    terminal_safe(hit.get("who") or "-"))))
    project = terminal_safe(_proj(hit.get("project") or ""))
    sem_score = hit.get("sem_score")
    semantic_hit = sem_score is not None or hit.get("lane") == "semantic"
    quality = (("~semantic-weak" if _semantic_row_weak(hit) else "~semantic")
               if semantic_hit
               else "~all" if hit.get("matched") in ("all-terms", "content-terms")
               else "~substr" if hit.get("_boundary_class") in ("partial", "interior")
               else "")
    dup = hit.get("_dup_chats")
    marker = " ".join(value for value in (
        "~self" if hit.get("_self") else "", quality,
        "~meta" if hit.get("_meta_row") else "",
        "~encoded" if hit.get("_encoded_blob") else "",
        f"×{dup}-chats" if dup else "") if value)
    fields = (handle, owner, project, common.age_label(hit.get("ts")), marker,
              terminal_safe(hit.get("snippet") or ""))
    return " ".join(value for value in fields if value)


_COMPACT_PROSE_TOOL_CAP = 2
_COMPACT_TOOL_ONLY_CAP = 4
_COMPACT_ECHO_MAX_CHARS = 4096


def _compact_tool_echo(
        tool: dict, prose: list[dict], roots: dict[str, str]) -> bool:
    """Whether a successful tool output is exactly duplicated by nearby prose."""
    if (tool.get("ok") is not True
            or tool.get("output_truncated") is not False):
        return False
    output = tool.get("output")
    if (type(output) is not str or not output
            or len(output) > _COMPACT_ECHO_MAX_CHARS):
        return False
    if (type(tool.get("output_chars")) is not int
            or tool["output_chars"] != len(output)):
        return False
    normalized = common.one_line(output)
    if not normalized:
        return False
    session = str(tool.get("session") or "")
    family = roots.get(session, session)
    turn = tool.get("turn")
    if not family or type(turn) is not int:
        return False
    for row in prose:
        other_session = str(row.get("session") or "")
        if roots.get(other_session, other_session) != family:
            continue
        other_turn = row.get("turn")
        if type(other_turn) is not int or abs(other_turn - turn) > 1:
            continue
        snippet = row.get("snippet")
        if (row.get("_snippet_complete") is True
                and type(snippet) is str
                and common.one_line(snippet) == normalized):
            return True
    return False


def _compact_tool_rescue(
        hits: list[dict], roots: dict[str, str], *, explicit_tools: bool,
        requested_rows: int | None,
) -> tuple[list[dict], int | None, bool]:
    """Promote bounded tool evidence on page one without dropping frozen rows."""
    if explicit_tools or requested_rows is not None:
        return hits, None, False
    prose = [hit for hit in hits if hit.get("who") != "tool"]
    tools = [hit for hit in hits if hit.get("who") == "tool"]
    if not tools:
        return hits, None, False
    if not prose:
        ordered_tools = ([hit for hit in tools if not hit.get("_query_echo")]
                         + [hit for hit in tools if hit.get("_query_echo")])
        selected = ordered_tools[:_COMPACT_TOOL_ONLY_CAP]
        selected_ids = {id(hit) for hit in selected}
        marked = [{**hit, "_tool_rescue": True} for hit in selected]
        ordered = [*marked, *(hit for hit in hits if id(hit) not in selected_ids)]
        return ordered, len(selected), True
    selected = [hit for hit in tools
                if not hit.get("_query_echo")
                and not _compact_tool_echo(hit, prose, roots)][
                    :_COMPACT_PROSE_TOOL_CAP]
    if not selected:
        return hits, None, True
    selected_ids = {id(hit) for hit in selected}
    lead = prose[:min(3, len(prose))]
    lead_ids = {id(hit) for hit in lead}
    marked = [{**hit, "_tool_rescue": True} for hit in selected]
    remainder = [hit for hit in hits
                 if id(hit) not in selected_ids and id(hit) not in lead_ids]
    return [*lead, *marked, *remainder], None, True


def _start_compact_page(
        hits: list[dict], q: str, pat: re.Pattern | None, *,
        corpus_more: bool, exact_total: int | None = None,
        deeper_argv: tuple[str, ...] | None = None,
        more_unknown: bool = False,
        requested_rows: int | None = None,
        total_floor: int | None = None,
        total_uncounted: bool = False,
        chat_scoped: bool = False,
        explicit_tool_filter: bool = False):
    hits = _collapse_identical(hits)
    roots = _family_roots_for_hits(hits)
    hits = _near_fold(hits, roots)
    session_index = common.indexed_session_prefix_candidates(
        hit.get("session") for hit in hits)
    prepared_query = boundary_rank.prepare_query(q)

    def family(hit: dict) -> str:
        session = str(hit.get("session") or "")
        return roots.get(session, session)

    frozen_cap = compact.MAX_FROZEN_HITS + _DEEPER_SKIP_ROWS
    source_more = corpus_more
    corpus_more = corpus_more or len(hits) > frozen_cap
    # Demote inside the frozen slice: page membership stays pure rank, so a
    # sunk meta row can never be pushed off the frozen page by rows behind it.
    frozen, _ = _demote_meta(hits[:frozen_cap])
    if _DEEPER_SKIP_ROWS:
        # The served set is the chain's whole ranked prefix (covered
        # accumulates across hops), so deeper-on-deeper skips every row the
        # chain already served, not just the first page.
        def _row_key(row: dict) -> tuple:
            base = (str(row.get("session") or ""), row.get("turn"),
                    str(row.get("who") or ""))
            if row.get("who") != "tool":
                return base
            return (*base, compact._event_identity(row), row.get("ts"),
                    row.get("content_digest"), row.get("snippet"))
        chain_frozen, _ = _demote_meta(hits[:_DEEPER_SKIP_ROWS])
        served = {_row_key(row) for row in chain_frozen}
        frozen = [row for row in frozen if _row_key(row) not in served]
        if not frozen:
            corpus_more = False
            if source_more:
                common.log(
                    f"nothing deeper in the bounded ranked scan after "
                    f"{_DEEPER_SKIP_ROWS} served rows; narrow the query")
            else:
                common.log(
                    f"nothing deeper: the {_DEEPER_SKIP_ROWS} rows already "
                    "served cover every distinct ranked match "
                    "(near-duplicates fold)")
    ordered = compact.diversify_hits(frozen, family)
    ordered, first_page_rows, tool_rescue_page = _compact_tool_rescue(
        ordered, roots, explicit_tools=explicit_tool_filter,
        requested_rows=requested_rows)
    prepared = []
    for hit in ordered:
        row = dict(hit)
        if _match_in_encoded_blob(hit.get("snippet") or "", pat):
            row["_encoded_blob"] = True
        row["snippet"] = _compact_snippet(row, pat, prepared_query)
        prepared.append(row)

    def render(hit: dict) -> str:
        return _compact_line(hit, session_index)

    def generation():
        try:
            return common.transcript_generation() or {"missing": True}
        except RuntimeError as exc:
            # Law 3: her rows are complete and stay available; only the
            # token a *later* page would verify against is missing, and
            # announcing that reports agrep's bookkeeping, not her answer.
            common.dbg(
                "continuation generation unverifiable; frozen rows stand "
                f"({terminal_safe(exc)})", "!")
            return {"unverifiable": True}

    if corpus_more and deeper_argv is None:
        deeper_argv = ("agrep", "--classic", "-n", "80", "--", q)
    return compact.start_compact(
        prepared, render, generation, _RANKING_VERSION, family_key=family,
        preordered=True,
        corpus_more=corpus_more, query=q, exact_total=exact_total,
        deeper_argv=deeper_argv, more_unknown=more_unknown,
        requested_rows=requested_rows, first_page_rows=first_page_rows,
        tool_rescue_page=tool_rescue_page,
        deeper_covered=_DEEPER_SKIP_ROWS,
        total_floor=total_floor,
        total_uncounted=total_uncounted, chat_scoped=chat_scoped)


def _search_argv_base(
        args: argparse.Namespace, *,
        semantic: bool, hybrid: bool = False) -> list[str]:
    """Every filter of this invocation, re-spelled as argv. A continuation
    that drops one of them answers a different question than the page it
    continues, so both callers below build from this one list."""
    argv = ["agrep"]
    if hybrid:
        argv.append("--hybrid")
    elif semantic:
        argv.append("-s")
    elif args.regex:
        argv.append("-E")
    elif args.word:
        argv.append("-w")
    elif args.lexical:
        argv.append("--lexical")
    for option, value in (
            ("--agent", args.agent),
            ("--project", args.project),
            ("--exclude-project", args.exclude_project),
            ("--model", args.model),
            ("--who", args.who),
            ("--no-who", args.no_who),
            ("--chat", args.chat),
            ("--since", args.since),
            ("--until", args.until)):
        if value is not None:
            argv.append(f"{option}={value}")
    if args.model_soft:
        argv.append("--soft")
    if args.no_meta:
        argv.append("--no-meta")
    if args.sort != "score":
        argv.append(f"--sort={args.sort}")
    if args.include_self:
        argv.append("--self")
    elif args.force_no_self:
        argv.append("--no-self")
    if args.all_side_chats:
        argv.append("--all-side-chats")
    if args.strict_semantic:
        argv.append("--strict-semantic")
    if args.no_auto:
        argv.append("--no-auto")
    if args.color != "auto":
        argv.append(f"--color={args.color}")
    return argv


def _deeper_search_argv(
        args: argparse.Namespace, query: str, *,
        semantic: bool, hybrid: bool = False) -> tuple[str, ...]:
    argv = _search_argv_base(args, semantic=semantic, hybrid=hybrid)
    argv.extend(("--classic", "-n", "80", "--", query))
    return tuple(argv)


def _larger_result_argv(
        args: argparse.Namespace, query: str, *,
        semantic: bool, hybrid: bool = False) -> list[str] | None:
    """A bounded larger page as direct process arguments."""
    current = int(args.max or 0)
    if args.max == 0:
        return None
    if args.json:
        target = next((size for size in (80, 160, 200) if current < size), None)
    else:
        target = 80 if current and current < 80 else None
    if target is None:
        return None
    argv = _search_argv_base(args, semantic=semantic, hybrid=hybrid)
    # -l beside --json is a combined surface (one json row per chat), so the
    # larger page must carry both or it returns message rows where the page
    # it continues counted chats.
    if args.chats:
        argv.append("-l")
        if args.json:
            argv.append("--json")
    elif args.json:
        argv.append("--json")
    elif args.flat:
        argv.append("--flat")
    else:
        argv.append("--classic")
    argv.extend(("-n", str(target), "--", query))
    return argv


def _full_result_argv(
        args: argparse.Namespace, query: str, *,
        semantic: bool, hybrid: bool = False) -> list[str] | None:
    """The exhaustive finite-keyword request as direct process arguments."""
    if semantic or hybrid or args.max == 0:
        return None
    argv = _search_argv_base(args, semantic=False)
    if args.chats:
        argv.append("-l")
        if args.json:
            argv.append("--json")
    elif args.json:
        argv.append("--json")
    elif args.flat:
        argv.append("--flat")
    else:
        argv.append("--classic")
    argv.extend(("-n", "0", "--", query))
    return argv


def _result_action_command(argv: list[str] | None) -> str | None:
    if argv is None:
        return None
    return console.shell_command(*argv, fallback="") or None


def _larger_result_command(
        args: argparse.Namespace, query: str, *,
        semantic: bool, hybrid: bool = False) -> str | None:
    """A copyable bounded command when this shell can represent it safely."""
    return _result_action_command(_larger_result_argv(
        args, query, semantic=semantic, hybrid=hybrid))


def _full_result_command(
        args: argparse.Namespace, query: str, *,
        semantic: bool, hybrid: bool = False) -> str | None:
    """A copyable exhaustive command when this shell can represent it safely."""
    return _result_action_command(_full_result_argv(
        args, query, semantic=semantic, hybrid=hybrid))


def _compact_weakness(hits) -> tuple[str | None, str | None]:
    rows = list(hits)
    all_weak_meaning = bool(rows)
    for hit in rows:
        if not isinstance(hit, dict) or hit.get("sem_score") is None:
            all_weak_meaning = False
            break
        try:
            score = float(hit["sem_score"])
        except (TypeError, ValueError):
            all_weak_meaning = False
            break
        if not math.isfinite(score) or score >= _RECALL_STRONG_SEM:
            all_weak_meaning = False
            break
    if all_weak_meaning:
        return "every semantic candidate is below-auto-threshold", None

    coverages = []
    for hit in rows:
        if not isinstance(hit, dict) or hit.get("matched") != "content-terms":
            return None, None
        try:
            coverage = float(hit["coverage"])
        except (KeyError, TypeError, ValueError):
            return None, None
        if not math.isfinite(coverage) or coverage >= _OVERSPEC_MIN_COVERAGE:
            return None, None
        coverages.append(coverage)
    if not coverages:
        return None, None
    best_coverage = max(coverages)
    detail = (f"best covers {best_coverage:.0%} of the query's "
              "informative terms")
    return "every row is a weak partial", detail


def _compact_summary(page) -> None:
    if not (page.more or page.corpus_more or page.more_unknown):
        return
    continuation = None
    action_label = "more"
    if page.more and page.handle:
        continuation = f"agrep --more {page.handle}"
    elif page.corpus_more and page.handle:
        continuation = f"agrep --deeper {page.handle}"
        action_label = "broader rerun (may repeat)"
    argv = page.deeper_argv or ()
    exhaustible = (not page.more_unknown
                   and not ("-s" in argv or "--hybrid" in argv))
    floor = None if page.total_uncounted else page.floor
    common.log(surface.compact_completeness_line(
        exact_total=page.exact_total, floor=floor,
        shown=page.shown_before + len(page.records),
        exhaustible=exhaustible, continuation=continuation,
        action_label=action_label))


def _self_exclusion_query_has_match(
        q: str, mode: str, query_kwargs: dict,
        policy: common.SelfExclusion,
) -> bool | None:
    """Prove that the active policy hid a match, not merely an indexed chat."""
    family = policy.family
    members = family.members or frozenset({family.session})
    sessions = [
        session for session in sorted(members)
        if session == family.session or policy.excludes(session, None)
    ]
    if family.session not in sessions:
        sessions.insert(0, family.session)
    base = {
        key: value for key, value in query_kwargs.items()
        if key not in {
            "chat", "exclude_family", "exclude_session",
            "exclude_session_from_turn", "_exclude_sessions",
            "exhaustive", "exact_totals", "family_diverse", "limit",
            "session_limit", "sort",
        }
    }
    try:
        # Both callers gate on an EMPTY page, so one exclusion-stripped probe
        # answers for the whole family: any excluded hit it finds was hidden.
        # The old per-member walk cost 1,146 queries / 5.5s on a long session.
        probe = run_query(
            q, mode=mode, limit=20, sort="time",
            exhaustive=False, session_limit=None, exact_totals=False,
            family_diverse=False, **base)
        hits = (probe.get("hits") or ()) if probe else ()
        if any(policy.excludes(str(hit.get("session") or ""), hit.get("turn"))
               for hit in hits):
            return True
        if len(hits) < 20:
            # The stripped probe saw the full (tiny) result set; nothing in it
            # was excluded, so nothing was hidden.
            return False
        # A full page of non-excluded hits contradicts the caller's empty page;
        # some lane disagreed with the probe. Fall back to a bounded member
        # walk rather than guessing either way.
        for session in sessions[:8]:
            member = run_query(
                q, mode=mode, limit=1, sort="time", chat=session,
                exhaustive=False, session_limit=None, exact_totals=False,
                family_diverse=False, **base)
            if member and any(
                    policy.excludes(
                        str(hit.get("session") or ""), hit.get("turn"))
                    for hit in member.get("hits") or ()):
                return True
        return None if len(sessions) > 8 else False
    except Exception as exc:  # noqa: BLE001 -- the primary query remains valid
        common.dbg(
            "self-exclusion match proof unavailable "
            f"({terminal_safe(exc)})", "!")
        return None


_SELF_EXCLUSION_COUNT_MAX_SESSIONS = 8


def _self_exclusion_match_keys(
        q: str, mode: str, query_kwargs: dict,
        policy: common.SelfExclusion,
        *, minimum_sem_score: float | None = None,
        drop_meta: bool = False,
) -> list[tuple] | None:
    """Return one occurrence key per matching row hidden by one policy.

    Each probe is scoped to a session the policy can exclude. Automatic
    current-window counts need one probe; large explicit families return an
    unknown count instead of multiplying queries. ``limit=0`` and exact totals
    prevent a top-k page from posing as a count. Duplicate rows retain distinct
    occurrence ordinals. ``None`` means this lane could not prove completeness.
    """
    family = policy.family
    sessions = (
        (family.session,) if policy.windowed
        else tuple(sorted(family.members or frozenset({family.session}))))
    requested_chat = str(query_kwargs.get("chat") or "").lower()
    if requested_chat:
        sessions = tuple(
            session for session in sessions
            if session.lower().startswith(requested_chat))
    if len(sessions) > _SELF_EXCLUSION_COUNT_MAX_SESSIONS:
        return None
    base = {
        key: value for key, value in query_kwargs.items()
        if key not in {
            "chat", "exclude_session", "exclude_session_from_turn",
            "exhaustive", "exact_totals", "family_diverse", "limit",
            "session_limit", "sort",
        }
    }
    hidden: list[tuple] = []
    occurrences: dict[tuple, int] = {}
    try:
        for session in sessions:
            probe = run_query(
                q, mode=mode, limit=0, sort="time", chat=session,
                exhaustive=True, session_limit=None, exact_totals=True,
                family_diverse=False, **base)
            rows = list((probe or {}).get("hits") or ())
            if (probe is None or probe.get("truncated")
                    or not probe.get("totals_exact", True)
                    or probe.get("index_missing")
                    or probe.get("tools_excluded")
                    or probe.get("fallback_recommended")
                    or _semantic_result_incomplete(probe)
                    or int(probe.get("total") or 0) != len(rows)):
                return None
            for hit in rows:
                if (minimum_sem_score is not None
                        and float(hit.get("sem_score") or float("-inf"))
                        < minimum_sem_score):
                    continue
                hit_session = str(hit.get("session") or "")
                # --chat is a prefix filter. Require the exact session here so
                # overlapping prefixes cannot count the same row twice.
                if hit_session != session:
                    continue
                if (drop_meta
                        and (hit.get("_meta_row") or _meta_row(hit))):
                    continue
                if not policy.excludes(hit_session, hit.get("turn")):
                    continue
                key = (
                    hit_session, hit.get("turn"), hit.get("who"),
                    hit.get("content_digest"), hit.get("snippet") or "",
                )
                ordinal = occurrences.get(key, 0)
                occurrences[key] = ordinal + 1
                hidden.append((*key, ordinal))
        return hidden
    except Exception as exc:  # noqa: BLE001 -- the primary answer remains valid
        common.dbg(
            "self-exclusion count unavailable "
            f"({console.terminal_safe(exc)})", "!")
        return None


def _count_tiers(hits: list[dict]) -> dict[str, int]:
    tiers = {"phrase_aligned": 0, "phrase_partial": 0,
             "phrase_interior": 0, "all_terms": 0}
    for hit in hits:
        if hit.get("matched") in ("all-terms", "content-terms"):
            tiers["all_terms"] += 1
            continue
        name = hit.get("_boundary_class")
        if name not in ("aligned", "partial", "interior"):
            raise RuntimeError("phrase hit lacks boundary classification")
        key = f"phrase_{name}"
        tiers[key] += 1
    return tiers


# Over-specification auto-recovery: a wordy natural-language query whose page
# holds no strong row beyond the caller's own echoes retries once with bm25
# term coverage. Corpus document frequencies pick the informative terms.
_OVERSPEC_MIN_TERMS = 5
_OVERSPEC_BLOCK_ROWS = 5
_OVERSPEC_FORCED_ROWS = 20
_OVERSPEC_SCAN_ROWS = 400
_OVERSPEC_DROPPED_SHOWN = 6
# a term this many sessions hold discriminates nothing - corpus-decided narration
_OVERSPEC_DF_UBIQUITOUS = 0.25
# minimum share of the query's idf mass a served row must cover (see the
# floor comment in _overspec_retry_rows; negative_controls.json re-verifies)
_OVERSPEC_MIN_COVERAGE = 0.55
_COVERAGE_NOT_RUN = "not-run"
_COVERAGE_SCANNED = "scanned"
_COVERAGE_BUSY = "busy"
_COVERAGE_UNAVAILABLE = "unavailable"


@dataclass(slots=True)
class _CoverageRetry:
    state: str
    block: list[dict] | None = None


def _overspec_shape(q: str) -> bool:
    """The list-free half of the gate: wordy and whitespace-shaped (a joined
    identifier is ONE grep pattern to its author and never retried)."""
    toks = [t for t in re.split(r"[\s\-_]+", q.strip()) if t]
    return (" " in q.strip()
            and len(dict.fromkeys(t.lower() for t in toks)) >= _OVERSPEC_MIN_TERMS)


def _overspec_query(q: str) -> bool:
    """Wordy natural language only: code-shaped queries stay pure grep (the
    _nl_query carve-out), and a tight query has no narration to shed."""
    return _overspec_shape(q) and _nl_query(q, _content_terms(q))


def _overspec_narration_df(q: str, db) -> bool:
    """Narration evidence for queries the _STOP fast path cannot see: the
    corpus itself marks a term ubiquitous, so the query has filler to shed.
    Document frequency decides here, never a word list (the B4 guard)."""
    toks = [t for t in dict.fromkeys(
        t.lower() for t in re.split(r"[\s\-_]+", q.strip()) if t)
        if len(t) >= 3]
    df = corpusdb.term_session_df(db, toks)
    return any(f >= _OVERSPEC_DF_UBIQUITOUS for f in df.values())


def _coverage_cmd(q: str) -> str:
    """The copyable force command: law 2 - the auto-retry's route has a pin."""
    return console.shell_command(
        "agrep", "--coverage", "--", q,
        fallback="agrep --coverage <query>")


def _overspec_masked(q: str, hits: list[dict], db) -> bool:
    """True when no row is independent strong evidence: every hit is a weak
    tier, a semantic assist, a ~self family row, or a verbatim quote of the
    query itself (echo, judged on row text - never the rendered snippet)."""
    raw = [t for t in re.split(r"[\s\-_]+", q.strip()) if t]
    echo_pat = re.compile(r"[\W_]*".join(re.escape(t) for t in raw), re.I)
    for hit in hits:
        if (_weak_lexical_hit(hit) or hit.get("sem_score") is not None
                or hit.get("lane") == "semantic" or hit.get("_self")):
            continue
        row = db.execute(
            "SELECT text FROM msgs WHERE session = ? AND turn IS ? "
            "AND who IS ? LIMIT 1",
            (hit.get("session"), hit.get("turn"), hit.get("who"))).fetchone()
        if row is None or not echo_pat.search(row[0]):
            return False
    return True


def _overspec_retry_attempt(q: str, fkw: dict, hits: list[dict], self_policy, *,
                            force: bool = False) -> _CoverageRetry:
    if not force and not _overspec_shape(q):
        return _CoverageRetry(_COVERAGE_NOT_RUN)
    _load_corpusdb()
    candidates = None
    for attempt in range(2):
        try:
            db = corpusdb.connect(allow_stale=True)
        except sqlite3.DatabaseError as exc:
            corpusdb.record_query_database_error(exc)
            if attempt == 0:
                continue
            state = (_COVERAGE_BUSY
                     if corpusdb.query_database_error_kind(exc) == "transient"
                     else _COVERAGE_UNAVAILABLE)
            return _CoverageRetry(state)
        if db is None:
            return _CoverageRetry(_COVERAGE_UNAVAILABLE)
        database_error = None
        eligible = True
        try:
            if not force:
                if not _overspec_masked(q, hits, db):
                    eligible = False
                # Corpus DF gets the final say when the cheap stop-word path
                # cannot establish that a masked query contains narration.
                elif (not _overspec_query(q)
                      and not _overspec_narration_df(q, db)):
                    eligible = False
            if eligible:
                flt = {key: fkw.get(key) for key in (
                    "agent", "project", "who", "model", "model_soft", "chat",
                    "since_ms", "until_ms", "exclude_session",
                    "exclude_session_from_turn", "exclude_family",
                    "_exclude_sessions")}
                candidates = corpusdb.coverage_rank(
                    db, q, _OVERSPEC_SCAN_ROWS, flt)
        except sqlite3.DatabaseError as exc:
            database_error = exc
        finally:
            try:
                db.close()
            except sqlite3.DatabaseError as exc:
                database_error = database_error or exc
        if database_error is not None:
            corpusdb.record_query_database_error(database_error, db)
            if attempt == 0:
                continue
            state = (_COVERAGE_BUSY
                     if corpusdb.query_database_error_kind(database_error)
                     == "transient" else _COVERAGE_UNAVAILABLE)
            return _CoverageRetry(state)
        if not eligible:
            return _CoverageRetry(_COVERAGE_NOT_RUN)
        break
    if candidates is None:
        return _CoverageRetry(_COVERAGE_UNAVAILABLE)
    taken = (set() if force
             else {str(hit.get("session") or "") for hit in hits})
    fresh = []
    for hit in candidates:
        session = str(hit.get("session") or "")
        if hit.pop("_query_echo", False) or session in taken:
            continue
        # floor calibrated jul 2026: known-false <=0.465, real recoveries >=0.620
        mass = hit.get("_coverage_mass")
        if not force and mass is not None and mass < _OVERSPEC_MIN_COVERAGE:
            continue
        if self_policy is not None:
            if self_policy.excludes(session, hit.get("turn")):
                continue
            if self_policy.labels(session, hit.get("turn")):
                hit["_self"] = True
        hit["matched"] = "content-terms"
        taken.add(session)
        fresh.append(hit)
    if not fresh:
        return _CoverageRetry(_COVERAGE_SCANNED)
    roots = _family_roots_for_hits(fresh)
    seen_roots: set[str] = set()
    block = []
    cap = _OVERSPEC_FORCED_ROWS if force else _OVERSPEC_BLOCK_ROWS
    for hit in fresh:
        root = roots.get(str(hit["session"]), str(hit["session"]))
        if root in seen_roots:
            continue
        seen_roots.add(root)
        block.append(hit)
        if len(block) == cap:
            break
    return _CoverageRetry(_COVERAGE_SCANNED, block)


def _forced_coverage_error(attempt: _CoverageRetry) -> RuntimeError | None:
    if attempt.state == _COVERAGE_BUSY:
        return QueryDatabaseBusyError("search index is busy updating; retry")
    if attempt.state == _COVERAGE_UNAVAILABLE:
        return QueryDatabaseUnavailableError(
            "search index is temporarily unavailable; retry")
    return None


def _overspec_retry_rows(q: str, fkw: dict, hits: list[dict], self_policy, *,
                         force: bool = False) -> tuple[list[dict] | None, bool]:
    """Return the optional coverage block and whether its scan completed."""
    attempt = _overspec_retry_attempt(
        q, fkw, hits, self_policy, force=force)
    error = _forced_coverage_error(attempt) if force else None
    if error is not None:
        raise error
    return attempt.block, attempt.state == _COVERAGE_SCANNED


def _overspec_disclosure(block: list[dict], q: str = "", *,
                         force: bool = False) -> str:
    """Law-1 route line: name the reformulation the retry actually measured."""
    lead = block[0]
    matched = lead.get("_terms_matched") or []
    missing = lead.get("_terms_missing") or []
    total = len(matched) + len(missing)
    if not missing:
        detail = f"top row matched all {total} terms"
    else:
        dropped = ", ".join(missing[:_OVERSPEC_DROPPED_SHOWN])
        if len(missing) > _OVERSPEC_DROPPED_SHOWN:
            dropped += ", …"
        detail = (f"top row matched {len(matched)}/{total} terms - "
                  f"dropped: {terminal_safe(dropped)}")
    if force:
        return f"coverage lane (forced): {detail}"
    deeper = f" · more: {_coverage_cmd(q)}" if q else ""
    return f"~coverage {detail}{deeper}"


def _emit_overspec_block(q: str, fkw: dict, hits: list[dict], self_policy, *,
                         force: bool = False,
                         attempt: _CoverageRetry | None = None) -> bool:
    attempt = attempt or _overspec_retry_attempt(
        q, fkw, hits, self_policy, force=force)
    error = _forced_coverage_error(attempt) if force else None
    if error is not None:
        raise error
    block = attempt.block
    scanned = attempt.state == _COVERAGE_SCANNED
    if not block:
        if force:
            common.log("coverage lane: no candidate rows"
                       if scanned else
                       "coverage lane unavailable: needs a corpus db and "
                       "a multi-term query")
        elif attempt.state == _COVERAGE_BUSY:
            common.log(
                "coverage retry skipped: search index is busy; "
                "original results unchanged")
        elif attempt.state == _COVERAGE_UNAVAILABLE:
            common.log(
                "coverage retry skipped: search index is unavailable; "
                "original results unchanged")
        elif scanned:
            # law 1: "retried, empty" must not read like "never retried"
            common.log("only echo/weak rows - coverage retry found no new "
                       f"sessions ({_coverage_cmd(q)} rescans without the "
                       "page filter)")
        return scanned
    session_index = common.indexed_session_prefix_candidates(
        hit.get("session") for hit in block)
    common.log(_overspec_disclosure(block, q, force=force))
    for hit in block:
        common.log(_compact_line(hit, session_index))
    return scanned


def _demote_query_echoes(q: str, hits: list[dict]) -> None:
    """Display-lane demotion: a row that verbatim-quotes a wordy query restates
    the question instead of answering it, so it may fill the page but never
    lead it. Stable partition on row text (never the rendered snippet)."""
    if len(hits) < 2 or not _overspec_query(q):
        return
    _load_corpusdb()
    db = corpusdb.connect(allow_stale=True)
    if db is None:
        return
    raw = [t for t in re.split(r"[\s\-_]+", q.strip()) if t]
    echo_pat = re.compile(r"[\W_]*".join(re.escape(t) for t in raw), re.I)
    try:
        flags = []
        for hit in hits:
            row = db.execute(
                "SELECT text FROM msgs WHERE session = ? AND turn IS ? "
                "AND who IS ? LIMIT 1",
                (hit.get("session"), hit.get("turn"), hit.get("who"))).fetchone()
            flags.append(row is not None and echo_pat.search(row[0]) is not None)
    except sqlite3.DatabaseError as exc:
        corpusdb.record_query_database_error(exc, db)
        return
    finally:
        try:
            db.close()
        except sqlite3.DatabaseError as exc:
            corpusdb.record_query_database_error(exc, db)
    for hit, echo in zip(hits, flags):
        if echo:
            hit["_query_echo"] = True
    if any(flags) and not all(flags):
        hits[:] = ([hit for hit, echo in zip(hits, flags) if not echo]
                   + [hit for hit, echo in zip(hits, flags) if echo])


def _emit_chats(hits, color):
    """One line per matching chat (grep -l), with topic + hit count."""
    session_index = common.indexed_session_prefix_candidates(
        hit.get("session") for hit in hits)
    roots = _family_roots_for_hits(hits)
    for _, hs in _group(hits).items():
        print(_chat_head(hs[0], hs[0].get("session_hits", len(hs)), color,
                         side=_is_side(hs, roots), session_index=session_index))


_SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _stream_row_who(row: dict) -> str:
    """Read the Rust normalize-pass classification the streamer now emits.

    The streamed row itself is the shared artifact: rows only ever come from
    the live in-process binary, which stamps `who` on every one. An absent or
    unrecognized role fails closed to `unknown` - never lived user prose.
    """
    who = row.get("who")
    if type(who) is str and who in display_policy.MESSAGE_ROLE_ORIGINS:
        return who
    return "unknown"


def _pipe_gone(exc: OSError) -> bool:
    """Windows reports a vanished pipe reader (`agrep q | head`) as ERROR_NO_DATA,
    which CPython surfaces as plain OSError errno EINVAL - never BrokenPipeError."""
    return (isinstance(exc, BrokenPipeError) or exc.errno == errno.EPIPE
            or (os.name == "nt" and exc.errno == errno.EINVAL))


def _abort_streamed_ingest(process: subprocess.Popen) -> None:
    """Reap the cold ingest so an interrupted reader cannot keep its writer claim live."""
    try:
        process.terminate()
    except OSError:
        pass
    try:
        process.wait(timeout=0.5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=0.5)
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        if process.stdout is not None:
            process.stdout.close()
    except (AttributeError, OSError):
            pass

def _stream_publication_committed() -> bool:
    """Cheaply prove the cold writer published one complete derived generation."""
    try:
        _load_corpusdb()
        return bool(
            corpusdb is not None
            and corpusdb._derived_publication_health(
                routine=True).get("state") == "ready")
    except Exception:  # noqa: BLE001 -- an uncertain publication fails closed
        return False


def _stream_first_run(q: str, mode: str, args, color: bool,
                      since_ms, until_ms) -> int | None:
    """No index yet: run the ingest with --emit-rows and grep the row stream, so hits
    print DURING the cold parse instead of after ~20s of progress lines. Found-order
    flat rows (scrollback can't be re-ranked); one transient status line on stderr
    owns all the meta (spinner/percent/hit count) so progress never interleaves with
    hits. The real index lands as a side effect; ranked search is the next run.

    Only routed to for surfaces the stream can honor: aggregate output
    (--json/-c/-l/--semantic), non-default ordering, and --model (attribution
    needs the finished whole-session normalize pass) go to the blocking build.

    Returns an exit code, or None to fall back to the blocking build (spawn failed,
    or an installed binary too old to know the flag)."""
    try:
        pat = _match_pat(q, mode)
    except re.error as e:
        common.log(f"bad regex: {e}")
        return 2
    if pat is None:
        return None
    cmd = [str(common.ingest_bin()), "index", "--agent", "all", "--emit-rows"]
    try:
        proc = subprocess.Popen(cmd, cwd=str(common.REPO_ROOT), stdout=subprocess.PIPE,
                                **({"creationflags": subprocess.CREATE_NO_WINDOW}
                                   if common.WIN else {}))
    except OSError:
        return None

    status = sys.stderr.isatty()
    perf_phases = os.environ.get("AGREP_PERF_PHASES") == "1"
    # conhost ships with VT off; paint with bare \r + padding when it can't be enabled
    vt = common.enable_vt()
    limit = args.max or 0
    seen: set = set()
    printed: dict[tuple, int] = {}
    n_rows = n_hits = shown = done = frame = total = pct_shown = painted = 0
    row_channel_closed = False

    def wipe() -> None:
        nonlocal painted
        if vt:
            sys.stderr.write("\r\x1b[K")
        else:
            sys.stderr.write("\r" + " " * painted + "\r")
        painted = 0

    def paint(clear: bool = False) -> None:
        nonlocal frame, pct_shown, painted
        if not status:
            return
        wipe()
        if not clear:
            # the denominator accumulates as adapters report in; clamp the percent
            # non-decreasing so it stalls rather than visibly running backwards.
            if total:
                pct_shown = max(pct_shown, min(99, done * 100 // total))
            pct = f" · {pct_shown}%" if total else ""
            n = f" · {n_hits} hit{'s' if n_hits != 1 else ''}" if n_hits else ""
            cap = f" ({shown} shown)" if limit and n_hits > limit else ""
            line = (f"{_SPIN[frame % len(_SPIN)]} indexing your stores "
                    f"(first run){pct}{n}{cap}")
            sys.stderr.write(line)
            painted = len(line)
            frame += 1
        sys.stderr.flush()

    last = 0.0
    try:
        for line in proc.stdout:
            if not line.startswith(b"{"):
                detail = line.decode("utf-8", errors="replace").rstrip()
                common.dbg(f"ingest: {detail}")
                if (perf_phases
                        and detail.lstrip().startswith("phases: source-check ")):
                    sys.stderr.write(f"* [agrep perf] ingest {detail.strip()}\n")
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if "progress" in obj:
                done += 1
                now = time.monotonic()
                if now - last >= 0.1:
                    last = now
                    paint()
                continue
            if "total" in obj:
                total += obj["total"].get("files") or 0
                continue
            r = obj.get("row")
            if not r:
                continue
            key = (r["agent"], r["session"], r["turn"], r.get("text") or "")
            if key in seen:
                continue
            seen.add(key)
            n_rows += 1
            if args.agent and args.agent.lower() not in r["agent"].lower():
                continue
            if args.project and args.project.lower() not in (r.get("project") or "").lower():
                continue
            if (args.exclude_project and args.exclude_project.lower()
                    in (r.get("project") or "").lower()):
                continue
            if args.chat and not r["session"].lower().startswith(args.chat.lower()):
                continue
            ts = r.get("ts") or 0
            if since_ms is not None and ts < since_ms:
                continue
            if until_ms is not None and ts >= until_ms:
                continue
            user_who = _stream_row_who(r)
            # a turn can hit in the user text and the reply; surface each like the engine
            for field, who in (("text", user_who), ("reply", "agent")):
                if args.who_filter is not None and not (
                        surface.speaker_filter_admits(args.who_filter, who)):
                    continue
                val = r.get(field)
                # the engine only indexes non-empty fields; skipping them keeps an
                # empty-matching regex (-E 'foo|') from fabricating phantom hits
                if not val:
                    continue
                m = pat.search(val)
                if not m:
                    continue
                n_hits += 1
                if not limit or shown < limit:
                    shown += 1
                    if status:
                        wipe()
                        sys.stderr.flush()
                    hit = {"session": r["session"], "agent": r["agent"],
                           "project": r.get("project") or "", "turn": r["turn"],
                           "who": who,
                           "snippet": _snip_at(val, m.start(), m.end())}
                    _emit_flat([hit], pat, color)
                    printed_key = (hit["session"], hit["turn"], hit["who"], hit["snippet"])
                    printed[printed_key] = printed.get(printed_key, 0) + 1
                    sys.stdout.flush()
                paint()
            if limit and shown >= limit:
                # The page is final; closing the row channel stops serialization while ingest
                # keeps indexing and publishing the same generation.
                row_channel_closed = True
                proc.stdout.close()
                break
    except OSError as exc:
        if not _pipe_gone(exc):
            raise
        # grep semantics: the consumer hung up (| head), so stop - the index finishes
        # on the next run. Kill the ingest because this caller no longer needs publication.
        # and exit through devnull so the interpreter's flush doesn't traceback.
        _abort_streamed_ingest(proc)
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 141  # 128 + SIGPIPE, what a killed grep reports
    except BaseException:
        _abort_streamed_ingest(proc)
        raise

    try:
        rc = proc.wait()
    except BaseException:
        _abort_streamed_ingest(proc)
        raise
    paint(clear=True)
    if rc != 0 or not common.MESSAGES_PATH.exists():
        if n_rows == 0 and done == 0:
            return None  # old binary without --emit-rows (or instant death): build normally
        common.log(f"first-run ingest failed (exit {rc}); its error is printed above")
        return 2
    if row_channel_closed:
        try:
            published_rows = int(common.INGEST_SIG_PATH.read_text(
                encoding="utf-8").split(":", 1)[0])
            if published_rows >= 0:
                n_rows = published_rows
        except (OSError, UnicodeError, ValueError, IndexError):
            pass
    page_full = bool(limit and shown >= limit)
    page_verified = bool(page_full and _stream_publication_committed())
    t_tail = time.monotonic()
    indexd_runtime.finish_streamed_index(
        allow_inline_fallback=not page_verified)
    tail_ms = (time.monotonic() - t_tail) * 1000
    common.dbg(f"first-run tail: fts-delegate+hooks {tail_ms:.0f}ms")
    if perf_phases:
        sys.stderr.write(
            f"* [agrep perf] first-run tail: fts-delegate+hooks {tail_ms:.0f}ms\n")

    # Tool events and the all-terms/content fallbacks exist only after publication: re-run
    # the query, append unprinted rows - closes first-run false negatives; first hit already streamed.
    complete_total = n_hits
    exhausted = True
    if page_full:
        # page already full from the stream: the exhaustive re-scan could only
        # firm up the footer total (appends cap at limit; tool rows are not in
        # the prose scan) - not worth holding the exit. totals say "at least".
        exhausted = False
        common.dbg("first-run tail: completion re-scan skipped (page full)")
        if not page_verified:
            common.log("first-run results could not verify the published snapshot")
            return 2
    t_tail = time.monotonic()
    try:
        completed = None
        if exhausted:
            completed = run_query(
                q, mode=mode, limit=0, sort=args.sort,
                agent=args.agent, project=args.project,
                exclude_project=args.exclude_project, who=args.who_filter,
                chat=args.chat, since_ms=since_ms, until_ms=until_ms)
            common.dbg(f"first-run tail: completion re-scan "
                       f"{(time.monotonic() - t_tail) * 1000:.0f}ms")
            if (completed is None or completed.get("totals_exact") is False
                    or completed.get("index_missing")
                    or completed.get("tools_excluded")):
                common.log(
                    "first-run completion could not verify the published snapshot")
                return 2
        if completed is not None:
            complete_total = completed["total"]
            for hit in completed["hits"]:
                key = (
                    hit["session"], hit.get("turn"), hit.get("who"), hit["snippet"])
                already = printed.get(key, 0)
                if already:
                    printed[key] = already - 1
                    continue
                if limit and shown >= limit:
                    break
                shown += 1
                _emit_flat([hit], pat, color)
                sys.stdout.flush()
    except Exception as exc:  # noqa: BLE001 -- a built index remains usable next run
        if isinstance(exc, OSError) and _pipe_gone(exc):
            # same teardown as the streaming loop: exit through devnull so the
            # interpreter's shutdown flush doesn't traceback
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
            return 141
        common.dbg(f"first-run completion scan failed: {type(exc).__name__}: {exc}", "!")
        common.log("first-run completion could not verify the published snapshot")
        return 2
    n_hits = complete_total
    if not n_hits:
        # the first run's zero is a zero like any other: a piped caller must
        # be able to tell "no match" from "the invocation broke"
        common.log(f"no hits · index built ({n_rows} messages)")
        common.log("0 exact hits - `-s` runs semantic search")
    elif sys.stderr.isatty():
        count = f"at least {n_hits}" if not exhausted else str(n_hits)
        more = f", showing {shown}" if n_hits > shown or not exhausted else ""
        common.log(f"{count} hit{'s' if n_hits != 1 else ''} in found-order{more} "
                   f"· index built ({n_rows} messages) - rerun for ranked results")
    if n_hits:
        return 0
    return surface.grep_absence_exit(
        exact=True, freshness=indexd_runtime.freshness_story())


def _indexed_corpus_counts() -> dict | None:
    """Proof-bound corpus counts for the zero-hit disclosures, or None."""
    try:
        return common.index_summary()
    except Exception:  # noqa: BLE001 -- the miss line degrades to "unavailable"
        return None


def _indexed_message_total() -> int | None:
    """The corpus size the zero-hit line names.

    The proof-bound summary is preferred; it withdraws whenever the session
    family cannot prove itself, including the ordinary window where the
    family meta has landed and ``.ingest.sig`` has not committed yet. The
    size does not have to withdraw with it - the marker commits it directly.
    Only the size falls back: the dimension proofs behind filter_coverage
    keep riding the census and keep claiming nothing.
    """
    counts = _indexed_corpus_counts()
    if counts is not None:
        return counts.get("messages")
    return common.committed_message_total()


_DIMENSION_CENSUS_BUDGET_S = 0.03
_DIMENSION_CENSUS_OPS = 10_000


def _census_column(column: str) -> tuple[str, ...] | None:
    """Every value one per-message column holds, or None past the budget.

    A truncated census is a floor, and a floor cannot prove a dimension empty,
    so an over-budget scan withdraws the claim instead of shortening it."""
    db = _load_corpusdb().connect(allow_stale=True, quiet=True)
    if db is None:
        return None
    set_progress = getattr(db, "set_progress_handler", None)
    deadline = time.monotonic() + _DIMENSION_CENSUS_BUDGET_S
    try:
        if callable(set_progress):
            set_progress(lambda: int(time.monotonic() >= deadline),
                         _DIMENSION_CENSUS_OPS)
        return tuple(str(row[0]) for row in db.execute(
            f"SELECT DISTINCT {column} FROM msgs "  # noqa: S608 -- fixed set
            f"WHERE {column} IS NOT NULL AND {column} <> ''") if row[0])
    except Exception:  # noqa: BLE001 -- an unprovable domain claims nothing
        return None
    finally:
        if callable(set_progress):
            set_progress(None, 0)
        db.close()


def _indexed_dimension_values(dimension: str) -> tuple[str, ...] | None:
    """Every value this dimension holds in the index, or None when the tool
    cannot prove the whole set. Each takes the cheapest proof it has: agents
    ride the proof-bound summary the zero-hit lines already read, projects the
    session aggregate, and the per-message columns one bounded census."""
    if dimension == "agent":
        summary = _indexed_corpus_counts()
        return None if summary is None else tuple(summary.get("agents") or ())
    if dimension == "project":
        try:
            if indexd_runtime.freshness_story().state != "current":
                return None
            if _indexed_corpus_counts() is None:
                return None
            import explore
            return tuple({str(row.get("project") or "")
                          for row in explore._session_index().values()
                          if row.get("project")})
        except Exception:  # noqa: BLE001 -- see _census_column
            return None
    return _census_column(dimension)


def _dimension_filter_values(args, spec) -> tuple[str, tuple[str, ...]] | None:
    """The active selection under one dimension: what the caller typed, and
    the values it selects. --no-who widens a run and can never explain a zero,
    so only the include half of the speaker filter appears here."""
    raw = getattr(args, spec.dimension, None)
    if not isinstance(raw, str) or not raw.strip():
        return None
    if spec.dimension == "who":
        return raw, surface.parse_speaker_list(raw, surface.SPEAKER_CHOICES)
    return raw, (raw,)


def filter_coverage(args) -> dict:
    """Which of this run's filters select a dimension the index holds nothing
    for. Only a zero reaches here: one hit already proves every filtered
    dimension populated, so the answering path never pays a census."""
    empty: list[dict] = []
    unproven: list[str] = []
    for spec in surface.COVERAGE_DIMENSIONS:
        selection = _dimension_filter_values(args, spec)
        if selection is None:
            continue
        raw, values = selection
        known = _indexed_dimension_values(spec.dimension)
        if known is None:
            unproven.append(spec.flag)
            continue
        soft = spec.flag == "--model" and bool(getattr(args, "model_soft", False))
        if values and all(surface.dimension_selects_nothing(
                spec, value, known, soft=soft) for value in values):
            empty.append(surface.empty_dimension_disclosure(spec, raw, known))
    return surface.filter_coverage_disclosure(
        empty, checked=not unproven,
        reason=surface.unproven_coverage_reason(unproven, gaps=bool(empty)))


def main(argv: list[str] | None = None, *, _force_compact: bool = False) -> int:
    t0 = time.monotonic()
    common.lap("imports")
    common.utf8_stdio()

    ap = surface.ArgumentParser(
        prog="agrep", description="agentic grep: grep your cross-agent chat history",
        allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  agrep \"race condition\"            grep every agent for a phrase\n"
               "  agrep deadlock --agent codex       just codex\n"
               "  agrep bug --model gpt-5            only turns from that exact model\n"
               "  agrep bug --model spark --soft     model contains spark\n"
               "  agrep -w leak                      whole word only\n"
               "  agrep -E \"TODO|FIXME\" --who agent  regex, agent replies only\n"
               "  agrep oom --who user,agent         several speakers, comma list\n"
               "  agrep oom --no-who subagent        hide side-chat turns\n"
               "  agrep fixture --no-meta            drop tooling-about-history rows\n"
               "  agrep -l auth                      which chats mention it\n"
               "  agrep -c oom                       just the count\n"
               "  agrep \"flaky test\" -s              semantic search (starts on demand)\n"
               "  agrep memory --json                structured hits\n"
               "\nsearch is case-insensitive. exit: 0 found, 1 proven none, "
               "2 error or unverified result.\n"
               f"grouped rows mark the speaker: {surface.speaker_legend()}")
    ap.add_argument("pattern", nargs="*", help="text to search for (joined with spaces)")
    ap.add_argument("-n", "--max", type=int, default=None, metavar="N",
                    help="show at most N hits (classic: default 40 keyword or "
                         "10 semantic; 0 = all keyword hits or up to 200 semantic "
                         f"chats. compact: adaptive {compact.MIN_PAGE_HITS}-"
                         f"{compact.MAX_PAGE_HITS} rows within "
                         f"{compact.DEFAULT_BYTE_BUDGET} bytes; explicit positive "
                         f"N requests up to {compact.MAX_FROZEN_HITS} frozen rows, "
                         "still byte-limited)")
    ap.add_argument("-E", "--regex", action="store_true", help="treat pattern as a regex")
    ap.add_argument("-w", "--word", action="store_true", help="match whole words only")
    ap.add_argument("-i", "--ignore-case", action="store_true",
                    help="(default; search is always case-insensitive)")
    ap.add_argument("-l", "--chats", action="store_true",
                    help="list matching chats, not every line (like grep -l)")
    ap.add_argument("-c", "--count", action="store_true",
                    help="print only the match count (like grep -c)")
    ap.add_argument("--count-by-tier", action="store_true",
                    help="exhaustively count phrase boundary classes and all-terms rows")
    ap.add_argument("--more", metavar="HANDLE",
                    help="continue a frozen compact result page")
    ap.add_argument("--deeper", metavar="HANDLE", help=argparse.SUPPRESS)
    ap.add_argument("--classic", action="store_true",
                    help="use the classic renderer even in an agent or compact "
                         "profile (compact hybrid retrieval off; --lexical "
                         "disables semantics entirely, -s forces them)")
    ap.add_argument("--flat", action="store_true",
                    help="one tab-separated row per hit (the piped default "
                         "outside agent shells)")
    ap.add_argument("--agent", help=f"only this agent ({', '.join(common.KNOWN_AGENTS)})")
    ap.add_argument("--project", help="only chats whose project label contains this "
                                      "(usually the workspace folder name)")
    ap.add_argument("--exclude-project",
                    help="hide chats whose project label contains this")
    ap.add_argument("--model", help="only turns from this exact model name")
    ap.add_argument("--soft", "--model-soft", dest="model_soft", action="store_true",
                    help="with --model, substring-match the model name (like *model*)")
    ap.add_argument("--who", metavar="LIST",
                    help="only these speakers, comma-separated "
                         f"({', '.join(surface.SEARCH_SPEAKER_CHOICES)})")
    ap.add_argument("--no-who", dest="no_who", metavar="LIST",
                    help="exclude these speakers (same names; e.g. "
                         "--no-who subagent hides side-chat turns)")
    ap.add_argument("--no-meta", dest="no_meta", action="store_true",
                    help="drop structurally proven ~meta rows; retain one marked "
                         "row when it is the query's only evidence")
    ap.add_argument("--chat", "--session", dest="chat", metavar="ID",
                    help="only this chat: an 8-char id prefix (as shown) or full session uuid")
    ap.add_argument("--since", metavar="WHEN",
                    help="only hits at/after WHEN (7d / 24h / 2w / 30m, or 2026-06-01)")
    ap.add_argument("--until", "--before", dest="until", metavar="WHEN",
                    help="only hits before WHEN (same formats as --since)")
    ap.add_argument("--sort", choices=("score", "time", "position"), default="score",
                    help="hit order: relevance score (default), newest first, or "
                         "corpus position (the pre-ranking order)")
    ap.add_argument("-s", "--semantic", action="store_true",
                    help="semantic search: confident relevant CHATS (short-lived resident "
                         "worker; embeddings auto-maintained in the background)")
    ap.add_argument("--hybrid", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--lexical", action="store_true",
                    help="keyword only: disable compact-profile meaning results")
    ap.add_argument("--coverage", action="store_true",
                    help="force the over-specification recovery lane: bm25 term "
                         "coverage over OR-ed query terms, best row per family "
                         f"(the auto-retry's deeper view, up to "
                         f"{_OVERSPEC_FORCED_ROWS} rows)")
    self_group = ap.add_mutually_exclusive_group()
    self_group.add_argument("--self", dest="include_self", action="store_true",
                            help="include the calling agent's current-window echoes")
    self_group.add_argument("--no-self", dest="force_no_self", action="store_true",
                            help="exclude the calling session and its indexed family, "
                                 "even outside agent shells")
    ap.add_argument("--all-side-chats", action="store_true",
                    help="with -s, show sibling child chats independently instead of "
                         "one best hit per root conversation family")
    ap.add_argument("--strict-semantic", action="store_true",
                    help="compatibility alias: --semantic already exits if meaning is unavailable")
    ap.add_argument("--json", action="store_true",
                    help='one agrep-meta run envelope, then one object per hit '
                         '(any-order hits carry matched: "all-terms")')
    ap.add_argument("--no-auto", action="store_true",
                    help=surface.NO_AUTO_HELP)
    ap.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    args = surface.parse_args_with_presence(ap, argv)

    # a blank filter value is a usage error, not an unfiltered run (surface_policy
    # owns which flags filter and what each empty value would silently widen to)
    blank_filter = surface.filter_value_error(args)
    if blank_filter:
        ap.error(blank_filter)
    if args.agent:
        args.agent = common.normalize_agent_name(args.agent.lower())
    try:
        # one owned parser for --who/--no-who (surface_policy holds the vocabulary)
        args.who_filter = surface.speaker_filter(
            args.who, args.no_who, surface.SEARCH_SPEAKER_CHOICES)
    except ValueError as exc:
        ap.error(str(exc))
    # presence, not truthiness: --more "" is a supplied --more, and reading it
    # as absent silently answered a fresh unpaginated query instead
    more_given = args.more is not None
    deeper_given = args.deeper is not None
    if more_given and deeper_given:
        ap.error("--more and --deeper are mutually exclusive")
    if (more_given or deeper_given) and (args.count or args.count_by_tier):
        # both continuation verbs replay a frozen page; a count would be
        # silently discarded while 14 rows streamed instead of one number
        verb = "--more" if more_given else "--deeper"
        ap.error(f"-c/--count-by-tier cannot be combined with {verb}")
    if args.count and args.count_by_tier:
        ap.error("-c and --count-by-tier are mutually exclusive")
    # an option a surface renders inert is refused, never dropped
    gated = surface.option_gate_error(args, surface.SEARCH_OPTION_GATES)
    if gated:
        ap.error(gated)
    if more_given or deeper_given:
        incompatible = (args.pattern or args.max is not None or args.regex or args.word
                        or args.ignore_case or args.chats or args.flat or args.classic
                        or args.agent or args.project or args.exclude_project
                        or args.model or args.model_soft
                        or args.who or args.no_who or args.no_meta
                        or args.chat or args.since or args.until
                        or args.sort != "score" or args.semantic or args.all_side_chats
                        or args.strict_semantic or args.lexical or args.hybrid or args.json
                        or args.include_self or args.force_no_self or args.coverage
                        or args.no_auto or args.color != "auto")
        if incompatible:
            verb = "--more" if more_given else "--deeper"
            if args.json or args.flat:
                surface_flag = "--json" if args.json else "--flat"
                # Machine surfaces freeze no page; name a bounded larger page
                # without teaching an agent to dump the corpus into context.
                ap.error(
                    f"{verb} continues a frozen compact page; {surface_flag} "
                    f"freezes none - rerun the query with {surface_flag} -n 80 "
                    "for a bounded larger page")
            ap.error(
                f"{verb} cannot be combined with search, filter, "
                "or output options")
    if deeper_given:
        try:
            deeper_argv, covered = compact.load_deeper_context(
                args.deeper, _RANKING_VERSION)
        except compact.CompactError as exc:
            common.log(str(exc))
            return 2
        # Deeper means beyond the frozen chain: the replay reproduces the same
        # ranked order and resumes past the rows already served, instead of
        # re-serving page one under a fresh continuation handle.
        global _DEEPER_SKIP_ROWS
        _DEEPER_SKIP_ROWS = covered
        try:
            return main(list(deeper_argv[1:]), _force_compact=True)
        finally:
            _DEEPER_SKIP_ROWS = 0
    if more_given:
        # compare the frozen page against the live corpus (mirrors the save path):
        # on a changed generation --more still serves the frozen rows, plus a
        # non-fatal stderr staleness note (see compact.continue_compact).
        try:
            generation = common.transcript_generation() or {"missing": True}
        except RuntimeError as exc:
            generation = None
            common.log(
                "could not verify the current corpus generation; "
                f"serving frozen rows ({terminal_safe(exc)})")
        try:
            page = compact.continue_compact(args.more, generation, _RANKING_VERSION)
        except compact.CompactError as exc:
            common.log(str(exc))
            return 2
        for line in page.lines:
            print(line)
        sys.stdout.flush()
        _compact_summary(page)
        return 0 if page.lines else 1
    if args.max is not None and args.max < 0:
        ap.error("--max must be 0 or greater")
    selected_modes = sum(bool(value) for value in
                         (args.semantic, args.regex, args.word, args.lexical,
                          args.hybrid))
    if selected_modes > 1:
        ap.error("search matcher modes are mutually exclusive")
    if args.hybrid and (
            not args.classic or args.who_filter == "tool" or args.sort != "score"
            or args.count or args.count_by_tier or args.chats
            or args.flat or args.json):
        ap.error("invalid internal hybrid replay")
    if args.coverage and (args.json or args.flat or args.count
                          or args.count_by_tier or args.chats):
        # law 3: machine modes never route - the coverage lane is porcelain
        ap.error("--coverage cannot be combined with "
                 "-c/--count-by-tier/-l/--json/--flat")
    if args.coverage and (args.semantic or args.lexical or args.regex
                          or args.word):
        ap.error("--coverage pins the coverage lane; drop the other matcher mode")
    if args.count_by_tier and (args.semantic or args.regex or args.word):
        ap.error("--count-by-tier requires the default keyword matcher; "
                 "drop -s/-E/-w")
    if args.count and args.semantic:
        ap.error("-c cannot be combined with --semantic; semantic totals are bounded")
    if args.no_meta and (args.count or args.count_by_tier):
        ap.error("--no-meta cannot narrow -c/--count-by-tier; "
                 "machine counts keep grep parity")
    if args.semantic and args.who_filter == "tool":
        ap.error("--semantic cannot search --who tool because tool rows are not embedded; "
                 "use --lexical")
    if args.semantic and args.sort == "position":
        ap.error("--sort position is undefined for semantic candidates; "
                 "use score or time")
    if args.strict_semantic and not args.semantic:
        ap.error("--strict-semantic requires --semantic")
    if args.all_side_chats and not args.semantic:
        ap.error("--all-side-chats requires --semantic")

    # Aggregate surfaces stay classic; semantic rows carry message turns and page normally.
    profile_wants = (
        (_force_compact
         or compact.profile_enabled(classic=args.classic, json_mode=args.json))
        and not (args.flat or args.chats or args.count or args.count_by_tier)
    )
    compact_output = profile_wants

    # An explicit -n on a compact page waives the diversity caps up to the
    # frozen limit because diversity is a default, not a ceiling.
    requested_rows = (min(args.max, 40) if compact_output and not _force_compact
                      and args.max is not None and args.max > 0 else None)
    # Compact always freezes at most forty rows; --classic owns truly unlimited
    # output. A --deeper replay widens by the rows already served, or its
    # resumed ranked order would end exactly where the chain did.
    compact_cap = 40 + _DEEPER_SKIP_ROWS
    query_cap = (
        max(compact_cap, _DEEPER_QUERY_MIN_ROWS)
        if _force_compact and _DEEPER_SKIP_ROWS else compact_cap)
    if compact_output and (
            args.max is None or args.max == 0 or args.max > query_cap
            or (_force_compact and args.max < query_cap)):
        args.max = query_cap
    elif args.max is None:
        args.max = 40 if not args.semantic else 10
    if args.count:
        args.max = max(1, args.max)
    if args.count_by_tier:
        args.max = 0

    said_once: set[str] = set()

    def _note_freshness() -> None:
        # every porcelain surface carries the one story line; machine stdout
        # keeps grep parity because the line rides stderr. Four call sites emit
        # it, so "once per render" is enforced here, not by each site's return.
        if "freshness" in said_once:
            return
        said_once.add("freshness")
        notice = indexd_runtime.agent_freshness_notice()
        if notice:
            common.log(notice)

    machine_self_surface = bool(
        args.json or args.count or args.count_by_tier or args.flat)
    # Renderer choice never changes the result set. --self is the explicit
    # opt-in to current-window echoes on every human and machine surface.
    auto_self_exclusion = common.in_agent_context()
    # --chat only waives the automatic exclusion; an explicit --no-self still
    # binds inside the chat filter (teach: "--no-self hides the whole family").
    self_exclusion_requested = (
        not args.include_self
        and (args.force_no_self
             or (auto_self_exclusion and not (args.chat or ""))))
    self_inactive_reason = "caller-unresolved"

    def _machine_fields(
            policy=None, *, excluded_hits: int | None = None,
            semantic_coverage: dict | None = None,
            semantic_accelerator_coverage: dict | None = None,
            semantic_partial: bool | None = None,
            semantic_integrity: dict | None = None,
            exclusion_pending: bool = False,
            index_unavailable: bool = False,
            completeness: dict | None = None,
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
        generation_fields = _load_corpusdb().machine_freshness_fields(
            freshness, publication_converging=publication_converging)
        if isinstance(generation_fields.get("freshness"), dict):
            # honest counts: failing derives from the recorded failure, so a
            # zero streak is omitted rather than contradicting it (F7)
            generation_fields["freshness"] = surface.row_freshness_disclosure(
                generation_fields["freshness"], first=True)
        if completeness is None:
            # Error/early-return callers never searched a result set. A zero
            # floor is explicit non-proof; the adjacent error names the cause.
            completeness = surface.completeness_disclosure(
                shown=0, total=0, unit="chat" if args.chats else "row",
                totals_exact=False, truncated=True,
                action_unavailable_reason=(
                    "the query did not produce a result set"))
        return {
            "self_exclusion": surface.self_exclusion_disclosure(
                policy, inactive_reason=inactive_reason,
                excluded_hits=excluded_hits),
            "completeness": completeness,
            # never omitted: an absent field would read as "no dimension is
            # empty", the exact inference an unexplained zero already invites
            "filter_coverage": filter_coverage or (
                surface.filter_coverage_disclosure(
                    [], checked=False,
                    reason="the query returned before the index was filtered")),
            **generation_fields,
            "semantic_coverage": semantic_coverage,
            **({"semantic_accelerator_coverage": semantic_accelerator_coverage}
               if semantic_accelerator_coverage is not None else {}),
            **({"semantic_partial": semantic_partial}
               if semantic_partial is not None else {}),
            **({"semantic_integrity": semantic_integrity}
               if semantic_integrity else {}),
        }

    def _json_error(
            code: str, query: str, *, engine: str = "query:error",
            reason: str | None = None, policy=None,
            semantic_coverage: dict | None = None,
            exclusion_pending: bool = False,
            index_unavailable: bool = False) -> None:
        if not args.json:
            return
        error = {"code": code}
        if reason:
            error["reason"] = terminal_safe(reason)
        print(json.dumps({
            "kind": "agrep-meta", "query": query,
            "engine": engine, "hits": [], "error": error,
            **_machine_fields(
                policy, semantic_coverage=semantic_coverage,
                exclusion_pending=exclusion_pending,
                index_unavailable=index_unavailable),
        }, ensure_ascii=False, separators=(",", ":")))

    q = " ".join(args.pattern).strip()
    if not q:
        # `agrep search` also lands here when a bare `agrep <word>` consumed a
        # command word as the verb - name the escape hatch, not just the miss
        common.log("empty pattern - give me something to grep for "
                   "(a word that is also a command searches with: "
                   'agrep search "search")')
        _json_error("empty-query", q)
        return 2
    mode = "regex" if args.regex else "word" if args.word else "keyword"
    if mode == "regex":
        try:
            re.compile(q, re.I)
        except re.error as exc:
            common.log(f"bad regex: {exc}")
            _json_error("invalid-regex", q, reason=str(exc))
            return 2
    common.dbg(f"search start: q={q!r} max={args.max} mode="
               f"{'semantic' if args.semantic else mode} "
               f"no_auto={args.no_auto} data_dir={common.DATA_DIR}")
    color = _color_on(args.color)
    try:
        since_ms = _parse_when(args.since) if args.since else None
        until_ms = _parse_when(args.until) if args.until else None
    except SystemExit:
        _json_error("invalid-time-filter", q)
        return 2
    # an inverted window is empty by construction, so its zero is a usage
    # error the tool can name from the bounds alone (surface_policy owns it)
    inverted = surface.window_bounds_error(
        args.since, since_ms, args.until, until_ms)
    if inverted:
        common.log(inverted)
        _json_error("empty-time-window", q, reason=inverted)
        return 2
    output_limit = int(args.max or 0)
    query_limit = (
        max(output_limit, _HISTORY_META_ROWS_PER_QUERY)
        if args.no_meta and output_limit else output_limit
    )
    # `-l` is a session-level surface: -n N must mean N chats, not N turn rows
    # which happen to collapse to an arbitrary smaller number after retrieval.
    session_limit = (query_limit
                     if (args.chats or args.semantic) and not args.count else None)
    machine_keyword_surface = bool(
        args.count or args.count_by_tier or args.json or args.flat
        or args.chats or args.lexical
        or (not compact_output and not sys.stdout.isatty()))
    fkw = dict(limit=query_limit, session_limit=session_limit,
               sort=args.sort, agent=args.agent, project=args.project,
               exclude_project=args.exclude_project,
               who=args.who_filter, model=args.model, model_soft=args.model_soft,
               chat=args.chat, since_ms=since_ms, until_ms=until_ms,
               exhaustive=bool(args.count or args.count_by_tier),
               # Content-term recovery is display UX, not grep-shaped plumbing.
               allow_fallback=not machine_keyword_surface,
               exact_totals=bool(args.count or args.count_by_tier),
               # a keyword fallback keeps meaning search's family-level chat shape
               family_diverse=bool(args.semantic and not args.all_side_chats),
               allow_model_download=bool(args.semantic))

    # Fresh install + plain grep: stream hits out of the ingest itself (--emit-rows) so
    # the first answer lands in seconds; unstreamable surfaces (see _stream_first_run) block-build.
    if (not args.no_auto and not common.MESSAGES_PATH.exists()
            and common.ingest_bin().exists()
            and not compact_output
            and not self_exclusion_requested
            and not (args.json or args.count or args.count_by_tier
                     or args.semantic or args.chats)
            and not args.regex and args.sort == "score"
            and not args.model and not args.chat):
        rc = _stream_first_run(q, mode, args, color, since_ms, until_ms)
        if rc is not None:
            if rc in (0, 1):
                _note_freshness()
            return rc

    # Build on first use; when already indexed, a stale check re-runs the cheap
    # ingest so search sees new sessions. Semantic needs the same current transcript.
    machine_stdout = bool(
        machine_self_surface or args.chats or not sys.stdout.isatty())
    if not indexd_runtime.ensure_index(
            auto=not args.no_auto, quiet=machine_stdout):
        _json_error(
            "index-unavailable", q, engine="index:unavailable",
            exclusion_pending=True, index_unavailable=True)
        _note_freshness()
        return 2
    if not args.no_auto:
        indexd_runtime.wait_for_delegated_publication()
    common.lap("freshen")

    if args.chat:
        # needs the built session index, which is why --chat skips the streamed first run
        resolved = _resolve_chat(args.chat)
        if resolved is None:
            _json_error("chat-unresolved", q)
            return 2
        args.chat = fkw["chat"] = resolved

    self_dropped = 0
    self_excluded_count: int | None = None
    current_family = None
    self_policy = None
    if self_exclusion_requested:
        # Pre-top-k exclusion keeps the caller's echo-heavy family from
        # masking a past hit or turning a real match into exit 1.
        self_policy = common.calling_self_exclusion(
            conservative=args.force_no_self)
        current_family = self_policy.family if self_policy is not None else None
        calling_session = current_family.session if current_family else "none"
        common.dbg(f"self-exclusion: calling_session={calling_session}")
        if self_policy is not None:
            fkw.update(self_policy.query_filters())
        elif not args.json:
            identity = common.calling_identity()
            self_inactive_reason = (
                "window-unresolved" if identity.session else identity.reason)
            if args.force_no_self and not identity.session:
                common.log(
                    "--no-self was not applied: "
                    + common.self_exclusion_unavailable_notice(identity.reason))
        elif self_policy is None:
            identity = common.calling_identity()
            self_inactive_reason = (
                "window-unresolved" if identity.session else identity.reason)

    auto_hybrid = bool(
        (compact_output or args.hybrid) and not args.lexical and not args.semantic
        and not args.coverage
        and args.who_filter != "tool"
        and mode == "keyword" and args.sort == "score"
        and _auto_semantic_query(q))
    semantic_pending = None
    if auto_hybrid and _semantic_runtime_installed():
        semantic_pending = _safe_start_semantic_query(
            q, {**fkw, "limit": _AUTO_SEMANTIC_FETCH,
                "session_limit": _AUTO_SEMANTIC_FETCH,
                "family_diverse": True, "exact_totals": False,
                "semantic_timeout_s": _AUTO_SEMANTIC_TIMEOUT_S})

    sem_used = False
    semantic_status: dict | None = None
    res = None
    if args.semantic:
        res = run_query(q, mode="semantic", **fkw)
        unavailable_semantic_coverage = (
            res.get("semantic_coverage") if res is not None else None)
        unavailable_semantic_accelerator_coverage = (
            res.get("semantic_accelerator_coverage")
            if res is not None else None)
        unavailable_semantic_partial = (
            bool(res.get("partial")) if res is not None else None)
        unavailable_semantic_integrity = (
            res.get("semantic_integrity") if res is not None else None)
        if res is not None:
            semantic_status = res.get("semantic_status")
            if (res.get("fallback_recommended")
                    and not surface.semantic_lane_answered(semantic_status)):
                res = None
        if res is None:
            # An explicit matcher failure cannot silently change matcher semantics.
            if semantic_status is None:
                semantic_status = {
                    "state": "unavailable", "eligible": True,
                    "fallback_recommended": True, "complete": False,
                }
            if args.json:
                strict_engine = ("semantic:policy"
                                 if semantic_status.get("state") == "query-rejected"
                                 else "semantic:unavailable")
                print(json.dumps({"kind": "agrep-meta", "query": q,
                                  "engine": strict_engine, "hits": [],
                                  "error": {
                                      "code": "semantic-unavailable"},
                                  "semantic": semantic_status,
                                  **_machine_fields(
                                      self_policy,
                                      semantic_coverage=(
                                          unavailable_semantic_coverage),
                                      semantic_accelerator_coverage=(
                                          unavailable_semantic_accelerator_coverage),
                                      semantic_partial=(
                                          unavailable_semantic_partial),
                                      semantic_integrity=(
                                          unavailable_semantic_integrity))},
                                 ensure_ascii=False, separators=(",", ":")))
                return 2
            common.log(surface.semantic_unavailable_notice(semantic_status))
            integrity_notice = surface.semantic_integrity_notice(
                unavailable_semantic_integrity)
            if integrity_notice:
                common.log(integrity_notice)
            return 2
        else:
            sem_used = True
    if res is None:
        try:
            res = run_query(q, mode=mode, **fkw)
        except re.error as e:
            common.log(f"bad regex: {e}")
            _json_error("invalid-regex", q, reason=str(e))
            return 2
        except RegexTimeoutError as exc:
            common.log(str(exc))
            _json_error("regex-timeout", q, reason=str(exc))
            return 2
        except RegexWorkerError as exc:
            common.log(f"regex failed: {terminal_safe(exc)}")
            _json_error("regex-failed", q, reason=str(exc))
            return 2
        except QueryDatabaseBusyError as exc:
            common.log(str(exc))
            _json_error(
                "search-index-busy", q, engine="corpusdb:busy",
                reason=str(exc))
            return 2
        except QueryDatabaseUnavailableError as exc:
            common.log(str(exc))
            _json_error(
                "search-index-unavailable", q,
                engine="corpusdb:unavailable", reason=str(exc))
            return 2
        except SnapshotPublicationTimeout as exc:
            if not args.json:
                common.log(str(exc))
            _json_error("snapshot-publication-timeout", q, reason=str(exc))
            return 2
        except NativeEventScanError as exc:
            if not args.json:
                common.log(
                    "tool-event search could not verify a complete snapshot")
            _json_error("event-scan-failed", q, reason=str(exc))
            return 2
        except DirectSnapshotQueryError as exc:
            if not args.json:
                common.log(
                    "the published transcript snapshot could not be verified")
            _json_error("direct-snapshot-unverified", q, reason=str(exc))
            return 2
        except SystemExit:
            if not args.regex:
                raise
            _json_error("invalid-regex", q)
            return 2

    common.lap("query", str(res.get("engine") or mode))
    hits, n_total, n_chats = res["hits"], res["total"], res["chats"]
    def _drop_self(result: dict) -> dict:
        nonlocal self_dropped
        if self_policy is None or not result.get("hits"):
            return result
        family_kept = []
        for hit in result["hits"]:
            session = str(hit.get("session") or "")
            hit.pop("_self", None)
            if self_policy.excludes(session, hit.get("turn")):
                continue
            if self_policy.labels(session, hit.get("turn")):
                hit["_self"] = True
            family_kept.append(hit)
        dropped = len(result["hits"]) - len(family_kept)
        kept = family_kept[:args.max] if args.max else family_kept
        if not dropped and len(kept) == len(result["hits"]):
            return result
        self_dropped += dropped
        updated = {
            **result,
            "hits": kept,
            "chats": len({h["session"] for h in kept}),
        }
        if dropped:
            # A post-filtered page cannot bound unseen excluded rows from above.
            updated.update(
                total=len(family_kept),
                tool_hits=sum(hit.get("who") == "tool" for hit in family_kept),
                totals_exact=False,
                truncated=len(family_kept) > len(kept),
                self_exclusion_more_unknown=True,
            )
        return updated

    def _drop_meta(result: dict | None) -> dict | None:
        # secondary meaning merges honor --no-meta too; the primary res filter
        # owns the counts and the one disclosure line
        if not args.no_meta or not result or not result.get("hits"):
            return result
        rows = result["hits"]
        _mark_history_meta(rows, [q])
        kept = [hit for hit in rows if not _meta_row(hit)]
        retained = 0
        if not kept and not hits:
            kept, _dropped, retained = _filter_meta_rows(rows)
        if retained and "meta-filter" not in said_once:
            said_once.add("meta-filter")
            common.log(surface.meta_filter_notice(
                len(rows) - len(kept), retained))
        if len(kept) == len(result["hits"]):
            return result
        return {**result, "hits": kept,
                "chats": len({h["session"] for h in kept})}

    def _note_lane_change_rebuild(state: dict | None = None) -> None:
        """Rides the coverage line: WHY history went partial.

        Read here rather than threaded through the semantic payload, which is
        re-projected through three explicit field allowlists on the way to this
        render - a key absent from any one of them is dropped in silence. Where
        the caller already read the embed state for the freshness stamp it hands
        it over, so an incomplete-coverage render still reads that record once.
        """
        try:
            import semantic
            notice = surface.semantic_lane_change_notice(
                semantic.lane_change_rebuild(state))
        except Exception:  # noqa: BLE001 -- attribution never breaks a render
            return
        if notice:
            common.log(notice)

    def _note_self_exclusion() -> None:
        # JSON carries the structured count in its envelope. Every prose/flat
        # surface stays silent unless an exact proof found omitted matches.
        if (self_policy is None or current_family is None
                or args.json or not self_excluded_count):
            return
        common.log(surface.self_exclusion_notice(
            resolved=current_family.resolved, dropped=self_excluded_count,
            windowed=self_policy.windowed))

    def _emit_main_overspec_block(*, force: bool = False) -> None:
        nonlocal self_excluded_count
        scanned = _emit_overspec_block(
            q, fkw, hits, self_policy, force=force)
        # Coverage is a bounded, separately ranked lane. It can prove which
        # rows it rendered, but not an exact cross-lane union before its cap.
        if scanned and self_policy is not None:
            self_excluded_count = None

    if current_family is not None:
        res = _drop_self(res)
        hits, n_total, n_chats = res["hits"], res["total"], res["chats"]
    if args.no_meta and hits:
        _mark_history_meta(hits, [q])
        kept_rows, meta_dropped, meta_retained = _filter_meta_rows(hits)
        shown_rows = kept_rows[:output_limit] if output_limit else kept_rows
        if meta_dropped or meta_retained:
            # a post-filtered page only bounds what it saw: totals stay exact
            # only when the engine had already returned the whole corpus
            complete = (res.get("totals_exact", True)
                        and not res.get("truncated")
                        and int(res.get("total") or 0) <= len(hits))
            res = {**res, "hits": shown_rows, "total": len(kept_rows),
                   "chats": len({h["session"] for h in shown_rows}),
                   "tool_hits": sum(h.get("who") == "tool"
                                    for h in shown_rows),
                   "totals_exact": complete,
                   "truncated": (len(kept_rows) > len(shown_rows)
                                 or bool(res.get("truncated")))}
            if not complete:
                res["self_exclusion_more_unknown"] = True
            hits, n_total, n_chats = res["hits"], res["total"], res["chats"]
            if meta_dropped or meta_retained:
                said_once.add("meta-filter")
                common.log(surface.meta_filter_notice(
                    meta_dropped, meta_retained))
        elif len(shown_rows) != len(res.get("hits") or []):
            res = {**res, "hits": shown_rows, "truncated": True}
            hits, n_total, n_chats = res["hits"], res["total"], res["chats"]
    if (compact_output and not sem_used and not args.lexical
            and mode == "keyword" and args.sort == "score"):
        _demote_query_echoes(q, hits)
    forced_coverage_attempt = None
    if args.coverage:
        forced_coverage_attempt = _overspec_retry_attempt(
            q, fkw, hits, self_policy, force=True)
        coverage_error = _forced_coverage_error(forced_coverage_attempt)
        if coverage_error is not None:
            common.log(str(coverage_error))
            return 2
    auto_semantic_failed = False
    auto_semantic_failure_status: dict | None = None
    semantic_empty_disclosed = False
    auto_semantic_zero: dict | None = None
    auto_semantic_weak: dict | None = None
    semantic_lane_participated = False
    secondary_semantic_result = None
    if semantic_pending is not None:
        sem_res = _safe_finish_semantic_query(semantic_pending)
        semantic_lane_participated = bool(
            sem_res is not None
            and not sem_res.get("fallback_recommended"))
        if sem_res is not None:
            sem_res = _drop_meta(_drop_self(sem_res))
        if semantic_lane_participated:
            secondary_semantic_result = sem_res
        auto_semantic_failed = (sem_res is None
                                or bool(sem_res.get("fallback_recommended")))
        if auto_semantic_failed and sem_res is not None:
            status = sem_res.get("semantic_status")
            if isinstance(status, dict):
                auto_semantic_failure_status = status
        answered_status = (
            sem_res.get("semantic_status") if sem_res is not None else None)
        if (sem_res is not None
                and surface.semantic_lane_answered(answered_status)):
            res = {
                **res,
                "totals_exact": (
                    bool(res.get("totals_exact", True))
                    and bool(sem_res.get("totals_exact", True))
                    and bool((answered_status or {}).get("complete"))),
                "semantic_status": answered_status,
                "semantic_coverage": sem_res.get("semantic_coverage"),
                "semantic_accelerator_coverage": sem_res.get(
                    "semantic_accelerator_coverage"),
            }
        if (sem_res is not None and not sem_res.get("fallback_recommended")
                and sem_res.get("hits")):
            if not hits and _weak_only_semantic_hits(sem_res["hits"]):
                # Keep the completed lane's coverage/status diagnostics, but a
                # nearest-neighbor-only page below the strong band is a miss.
                # Explicit -s remains the path to inspect those candidates.
                auto_semantic_weak = sem_res
            else:
                keyword_more = (bool(res.get("truncated"))
                                or int(res.get("total") or 0) > len(hits))
                semantic_more = (bool(sem_res.get("truncated"))
                                 or int(sem_res.get("total") or 0)
                                 > len(sem_res.get("hits") or []))
                merged = _merge_auto_semantic_hits(
                    hits, sem_res["hits"], int(args.max or 0))
                if merged != hits:
                    res = {**res, "hits": merged,
                           "total": max(int(res.get("total") or 0), len(merged)),
                           "chats": len({hit["session"] for hit in merged}),
                           "totals_exact": False, "hybrid_semantic": True,
                           "hybrid_more": keyword_more or semantic_more,
                           "semantic_status": sem_res.get("semantic_status"),
                           "semantic_coverage": sem_res.get("semantic_coverage"),
                           "semantic_accelerator_coverage": sem_res.get(
                               "semantic_accelerator_coverage"),
                           "engine": f"{res.get('engine', 'keyword')}+{sem_res.get('engine', 'semantic')}"}
                    hits, n_total, n_chats = res["hits"], res["total"], res["chats"]
        elif sem_res is not None and not sem_res.get("fallback_recommended"):
            if hits:
                if not compact_output:
                    coverage_line = display_policy.semantic_coverage_line(
                        sem_res.get("semantic_coverage"))
                    if coverage_line:
                        common.log(coverage_line)
                    common.log(display_policy.semantic_empty_line(
                        sem_res.get("semantic_coverage")))
                    semantic_empty_disclosed = True
            else:
                # a total miss: the unified zero verdict below owns disclosure
                auto_semantic_zero = sem_res
    if (not hits and not sem_used and not args.semantic and not auto_hybrid
            and not args.lexical and mode == "keyword"
            and args.who_filter != "tool" and not res.get("tools_excluded")
            and _auto_semantic_query(q) and _semantic_runtime_installed()
            and sys.stdout.isatty() and not args.json
            and not args.count and not args.count_by_tier):
        # a human with zero exact hits wants the semantic lane run, not a hint
        # to retype with -s - and run with -s's one-hit-per-family shape
        # (fkw carries family_diverse=False from the keyword invocation)
        sem_res = run_query(q, mode="semantic",
                            **{**fkw, "family_diverse": not args.all_side_chats})
        semantic_lane_participated = bool(
            sem_res is not None
            and not sem_res.get("fallback_recommended"))
        if sem_res is not None:
            sem_res = _drop_meta(_drop_self(sem_res))
        answered_status = (
            sem_res.get("semantic_status") if sem_res is not None else None)
        if (sem_res is not None
                and surface.semantic_lane_answered(answered_status)):
            res = {
                **res,
                "totals_exact": (
                    bool(res.get("totals_exact", True))
                    and bool(sem_res.get("totals_exact", True))
                    and bool((answered_status or {}).get("complete"))),
                "semantic_status": answered_status,
                "semantic_coverage": sem_res.get("semantic_coverage"),
                "semantic_accelerator_coverage": sem_res.get(
                    "semantic_accelerator_coverage"),
            }
        if semantic_lane_participated:
            secondary_semantic_result = sem_res
        if (sem_res is not None and not sem_res.get("fallback_recommended")
                and sem_res.get("hits")):
            if _weak_only_semantic_hits(sem_res["hits"]):
                auto_semantic_weak = sem_res
                res = {
                    **res,
                    "semantic_status": sem_res.get("semantic_status"),
                    "semantic_coverage": sem_res.get("semantic_coverage"),
                    "semantic_accelerator_coverage": sem_res.get(
                        "semantic_accelerator_coverage"),
                }
            else:
                common.log("0 exact hits - showing semantic matches:")
                res, sem_used = sem_res, True
                res["auto_meaning"] = True
                hits, n_total, n_chats = res["hits"], res["total"], res["chats"]
        elif sem_res is None:
            # the refused lane must say so: silence reads as "auto meaning
            # search doesn't exist". core-only installs stay silent - the lane
            # was never promised there
            import importlib.util
            if importlib.util.find_spec("onnxruntime") is not None:
                common.log("0 exact hits - semantic search would run here but is "
                           f"{surface.SEMANTIC_LANE_POLICY.warming} "
                           "(bare `agrep` shows progress)")
    if self_policy is not None:
        measured_modes = []
        if not args.semantic or not sem_used:
            measured_modes.append(mode)
        if sem_used or res.get("hybrid_semantic") or semantic_lane_participated:
            measured_modes.append("semantic")
        measured_modes = list(dict.fromkeys(measured_modes))
        # A single complete lane has an exact row count. Hybrid unions do not.
        if len(measured_modes) == 1:
            measured = _self_exclusion_match_keys(
                q, measured_modes[0], fkw, self_policy,
                drop_meta=args.no_meta)
            if measured is not None:
                self_excluded_count = len(measured)
    # A legal filter over an empty dimension answers zero for a known reason.
    coverage = (filter_coverage(args) if not hits and not n_total
                else surface.filter_coverage_disclosure([], checked=True))
    zero_verdict: surface.MissVerdict | None = None
    zero_line: str | None = None
    zero_owns_freshness = False
    if (not hits and not sem_used and mode == "keyword"
            and not args.count and not args.count_by_tier
            and not coverage["empty_dimensions"]
            and not res.get("index_missing")
            and not res.get("tools_excluded")
            and (self_policy is None or self_excluded_count == 0)
            and not self_dropped
            and (auto_semantic_zero is not None or auto_semantic_failed)
            and not any(getattr(args, name, None) for name in (
                "chat", "agent", "project", "model", "since", "until",
                "who", "no_who"))):
        counts = _indexed_corpus_counts()
        if counts and counts.get("sessions") is not None:
            zero_verdict = surface.miss_verdict(
                indexd_runtime.freshness_story(),
                meaning_served=auto_semantic_zero is not None,
                meaning_coverage=(auto_semantic_zero or {}).get(
                    "semantic_coverage"),
                meaning_accelerator=(auto_semantic_zero or {}).get(
                    "semantic_accelerator_coverage"),
                sessions=int(counts["sessions"]))
            zero_line, zero_owns_freshness = surface.miss_zero_render(
                int(counts["sessions"]), zero_verdict)
    porcelain_zero = zero_line is not None and not args.json
    if (auto_semantic_failed and not porcelain_zero
            and not res.get("tools_excluded")):
        common.log(surface.semantic_keyword_only_notice(
            auto_semantic_failure_status))
    if (sem_used and not hits and not args.json and not args.count
            and not args.count_by_tier and not args.chat):
        # Before the render dispatch so every porcelain path gets it: no
        # semantic candidate and no prose match anywhere are different facts.
        try:
            prose = run_query(q, **{**fkw, "limit": 1, "exact_totals": True})
        except Exception:  # noqa: BLE001 -- the page stands without the count
            prose = None
        if prose and prose.get("total"):
            common.log(
                f"no confident meaning match; {prose['total']} prose "
                f"match(es) exist - "
                f"{console.shell_command('agrep', q, '--lexical')}")
        else:
            common.log("no confident meaning match, and no prose match either")
    semantic_evidence = secondary_semantic_result or res
    if ((sem_used or semantic_lane_participated) and not porcelain_zero
            and not hits and not res.get("tools_excluded") and not args.json
            and not args.count and not args.count_by_tier):
        zero_coverage = semantic_evidence.get("semantic_coverage")
        coverage_line = display_policy.semantic_coverage_line(zero_coverage)
        if coverage_line:
            common.log(coverage_line)
        common.log(display_policy.semantic_empty_line(zero_coverage))
        semantic_empty_disclosed = True
    if res.get("index_missing") and not args.json:
        # D2: the refused unindexed lane names its remedy instead of scanning
        common.log("no search index exists yet - `agrep index` builds it")
    if porcelain_zero:
        common.log(zero_line)
        semantic_empty_disclosed = True
        if zero_owns_freshness:
            said_once.add("freshness")
    elif (auto_semantic_zero is not None and not semantic_empty_disclosed
          and not res.get("tools_excluded")):
        zero_coverage = auto_semantic_zero.get("semantic_coverage")
        coverage_line = display_policy.semantic_coverage_line(zero_coverage)
        if coverage_line:
            common.log(coverage_line)
        common.log(display_policy.semantic_empty_line(zero_coverage))
        semantic_empty_disclosed = True
    if (not hits and not sem_used and not porcelain_zero
            and not coverage["empty_dimensions"]
            and not args.json and not args.count and not args.count_by_tier):
        # rc 1 may mean no match, but it must no longer mean no output - on any
        # matcher or profile. When a coverage gap already owns the zero, that
        # line IS the zero's one cause (law 5); a miss count beside it is a second.
        if auto_semantic_weak is not None:
            common.log(_weak_semantic_neighbors_notice(q))
        elif res.get("tools_excluded"):
            # the zero's scope claim forfeits: this lane searched prose only
            common.log(surface.TOOL_QUERY_PENDING_LINE
                       if args.who_filter == "tool"
                       else surface.tools_pending_zero_line(mode))
        else:
            common.log(display_policy.matcher_empty_line(
                mode, _indexed_message_total()))
    if not args.json:
        for empty_dimension in coverage["empty_dimensions"]:
            common.log(surface.empty_dimension_line(empty_dimension))
    common.dbg(f"search done: {n_total} hit(s) in {n_chats} chat(s)"
               f" via {res['engine']}; showing {len(hits)}")
    shown = len(hits)
    hybrid_used = bool(res.get("hybrid_semantic"))
    semantic_incomplete = bool(
        (sem_used or hybrid_used or semantic_lane_participated
         or args.semantic or args.hybrid)
        and (_semantic_result_incomplete(res)
             or _semantic_result_incomplete(semantic_evidence)))
    result_totals_exact = bool(
        res.get("totals_exact", True)) and not semantic_incomplete
    # -l/-s page by chat: a hits-vs-rows compare would claim truncation on full pages
    more_exist = ((n_chats if session_limit is not None else n_total) > shown
                  or bool(res.get("truncated")) or not result_totals_exact)
    # one completeness judgement behind every surface below: the json field,
    # the piped stderr line and the count basis cannot drift apart
    exhaustible = not (sem_used or hybrid_used or args.semantic or args.hybrid)
    rerun_semantic = bool(sem_used or args.semantic)
    larger_argv = (
        _larger_result_argv(
            args, q, semantic=rerun_semantic, hybrid=hybrid_used)
        if exhaustible or args.json else None)
    full_argv = (
        _full_result_argv(
            args, q, semantic=rerun_semantic, hybrid=hybrid_used)
        if args.json and exhaustible else None)
    larger_command = _result_action_command(larger_argv)
    full_command = _result_action_command(full_argv)
    direct_larger = larger_argv if larger_argv and not larger_command else None
    direct_full = full_argv if full_argv and not full_command else None
    action_unavailable = (
        "this invocation already requested uncapped keyword results"
        if args.json and exhaustible and args.max == 0 else None)
    completeness = surface.completeness_disclosure(
        shown=shown,
        total=n_chats if session_limit is not None else n_total,
        unit="chat" if session_limit is not None else "matching row",
        totals_exact=result_totals_exact,
        truncated=more_exist,
        more_command=larger_command,
        more_argv=direct_larger,
        more_command_kind=("broader-rerun" if larger_argv else None),
        full_command=full_command,
        full_argv=direct_full,
        action_unavailable_reason=action_unavailable,
        no_exhaustive_form=(None if exhaustible
                            else surface.SEMANTIC_NO_EXHAUSTIVE_FORM))

    # highlight pattern mirrors the search mode (none for semantic chat titles)
    pat = (None if mode == "regex" and not sem_used
           else _match_pat(q, "semantic" if sem_used else mode))
    # any-order rows carry scattered terms the in-order pattern cannot mark
    terms_pat = _terms_hl_pat(q) if mode == "keyword" and not sem_used else None

    if (res.get("tools_excluded") and hits and not args.json
            and not args.count and not args.count_by_tier):
        common.log(surface.SCAN_TOOLS_PENDING_LINE)

    if args.count:
        print(f">={n_total}" if not result_totals_exact else f"{n_total}")
        if completeness["total_basis"] == "floor":
            # the ">=" on stdout says the number is short; only stderr has room
            # to say why, and a piped caller reading it must not need a tty
            common.log(surface.count_floor_tools_pending(n_total)
                       if res.get("tools_excluded") else
                       f">={n_total} is a floor, not the total - the counting "
                       "lane stopped early on this query")
        # scripts read stdout; the honesty marker for a widened count goes to stderr
        if sys.stderr.isatty():
            if res.get("terms_fallback"):
                common.log("keyword (any-order; no adjacent phrase match)")
            elif res.get("terms_augmented"):
                common.log("keyword (+ any-order hits)")
            elif res.get("content_fallback"):
                common.log("keyword (related terms; no exact match)")
    elif args.count_by_tier:
        if res.get("tools_excluded"):
            common.log("tier counts are unavailable while tool output is "
                       "still indexing")
        else:
            tiers = _count_tiers(hits)
            fields = [f"{name}={value}" for name, value in tiers.items()]
            print(" ".join([*fields, f"total={sum(tiers.values())}"]))
            if completeness["truncated"]:
                # An incomplete tier diagnostic must never look exhaustive.
                floor = "+" if completeness["total_basis"] == "floor" else ""
                common.log(f"tier counts classify {completeness['shown']} of "
                           f"{completeness['total']}{floor} matching rows - this "
                           "run is not the exhaustive count it reads as")
    elif args.json:
        machine_fields = _machine_fields(
            self_policy, excluded_hits=self_excluded_count,
            semantic_coverage=res.get("semantic_coverage"),
            semantic_accelerator_coverage=res.get(
                "semantic_accelerator_coverage"),
            semantic_partial=(
                bool(res.get("partial")) if args.semantic else None),
            semantic_integrity=res.get("semantic_integrity"),
            index_unavailable=bool(res.get("index_missing")),
            completeness=completeness, filter_coverage=coverage)
        if res.get("tools_excluded"):
            # the same narrowing every other surface reads: the lane served
            # prose only, so the totals beside it are floors of the corpus
            machine_fields = {**machine_fields, "tools_excluded": {
                "reason": surface.TOOLS_PENDING_ERROR_CODE}}
        if hits:
            self_family = current_family
            if self_family is None and common.in_agent_context():
                self_family = common.calling_family()

            def _row_self(hit: dict) -> bool | None:
                # `self` means the exact caller transcript, never a family
                # relationship. Child and sibling chats are ordinary history.
                if hit.get("_self"):
                    return True
                if self_family is not None:
                    session = str(hit.get("session") or "")
                    return session == self_family.session
                return None if common.in_agent_context() else False

            # Run-level state leads once in the same agrep-meta shape used by
            # misses; result rows contain only row-level evidence.
            head = {"kind": "agrep-meta", "query": q,
                    "engine": res.get("engine"), **machine_fields}
            if args.semantic:
                head["semantic"] = (
                    semantic_status or res.get("semantic_status"))
            print(json.dumps(
                head, ensure_ascii=False, separators=(",", ":")))
            for hit, row in zip(hits, public_rows(hits, result_handles=True)):
                row["self"] = _row_self(hit)
                for field in (
                        "semantic_coverage", "semantic_accelerator_coverage",
                        "semantic_partial"):
                    row.pop(field, None)
                print(json.dumps(
                    row, ensure_ascii=False, separators=(",", ":")))
        else:
            meta = {"kind": "agrep-meta", "query": q,
                    "engine": res.get("engine"), "hits": [],
                    **machine_fields}
            if res.get("index_missing"):
                # structured disclosure, not prose: the errorcode carries the remedy
                meta["error"] = {"code": "index-missing",
                                 "remedy": "agrep index"}
            elif res.get("tools_excluded") and args.who_filter == "tool":
                # a tool-only query in the build window ran nothing at all
                meta["error"] = {"code": surface.TOOLS_PENDING_ERROR_CODE,
                                 "reason": surface.TOOL_QUERY_PENDING_LINE}
            if args.semantic:
                meta["semantic"] = (
                    semantic_status or res.get("semantic_status"))
            print(json.dumps(
                meta, ensure_ascii=False, separators=(",", ":")))
    elif compact_output:
        observed_more = (
            bool(res.get("truncated")) or n_total > len(hits))
        more_unknown = bool(
            res.get("self_exclusion_more_unknown")
            or (semantic_incomplete and not observed_more))
        corpus_more = (
            bool(res.get("hybrid_more")) if res.get("hybrid_semantic")
            else (observed_more
                  or (not result_totals_exact and not more_unknown))
        )
        hybrid = bool(res.get("hybrid_semantic"))
        exact_total = (
            n_total if result_totals_exact and not hybrid
            and not more_unknown else None)
        # the same number every other surface reports - but only once it
        # describes the result set: a keyword lane that stopped early and went
        # uncounted measured its own scan, which is no headline (F1)
        counted = bool(res.get("total_counted"))
        result_set_floor = (
            counted or hybrid or more_unknown or bool(sem_used))
        total_floor = (
            n_total if exact_total is None and result_set_floor else None)
        total_uncounted = exact_total is None and total_floor is None
        deeper_argv = _deeper_search_argv(
            args, q, semantic=bool(args.semantic or sem_used),
            hybrid=hybrid)
        try:
            page = _start_compact_page(
                hits, q, pat, corpus_more=corpus_more,
                exact_total=exact_total, deeper_argv=deeper_argv,
                more_unknown=more_unknown, requested_rows=requested_rows,
                total_uncounted=total_uncounted,
                chat_scoped=bool(args.chat),
                total_floor=total_floor,
                explicit_tool_filter=args.who_filter == "tool")
        except (OSError, compact.CompactError) as exc:
            # Classic output is complete and correct, so falling back is not
            # the reader's situation (law 3); the invariant that broke is a
            # developer's fact and stays on the debug channel.
            common.dbg(
                "compact continuation unavailable; using classic output "
                f"({terminal_safe(exc)})")
            weak_label, weak_detail = _compact_weakness(hits)
            if weak_label is not None:
                notice = f"{weak_label} in this output"
                if weak_detail is not None:
                    notice = f"{notice} ({weak_detail})"
                common.log(notice)
            if args.chats:
                _emit_chats(hits, color)
            elif args.flat or not color:
                _emit_flat(hits, pat, color, terms_pat)
            else:
                _emit_grouped(hits, pat, color, terms_pat)
            if args.coverage:
                _emit_main_overspec_block(force=True)
        else:
            for line in page.lines:
                print(line)
            sys.stdout.flush()
            _compact_summary(page)
            if args.coverage or (not sem_used and not res.get("hybrid_semantic")
                                 and not args.lexical
                                 and mode == "keyword" and args.sort == "score"):
                _emit_main_overspec_block(force=args.coverage)
            coverage = semantic_evidence.get("semantic_coverage")
            accelerator = semantic_evidence.get(
                "semantic_accelerator_coverage")
            coverage_notice = surface.semantic_coverage_notice(
                coverage, accelerator, suppress_trivial=True)
            if coverage_notice and not semantic_empty_disclosed:
                common.log(coverage_notice)
                _note_lane_change_rebuild()
            integrity_notice = surface.semantic_integrity_notice(
                semantic_evidence.get("semantic_integrity"))
            if integrity_notice:
                common.log(integrity_notice)
            anchor_notice = surface.semantic_anchor_notice(
                res.get("semantic_status"))
            if anchor_notice:
                common.log(anchor_notice)
            _note_self_exclusion()
            _note_freshness()
            if hits:
                return 0
            exact = result_totals_exact and not any(
                res.get(key) for key in (
                    "truncated", "index_missing", "tools_excluded",
                    "self_exclusion_more_unknown"))
            return surface.grep_absence_exit(
                exact=exact, freshness=indexd_runtime.freshness_story())
    else:
        if args.chats:
            _emit_chats(hits, color)
        elif args.flat or not color:
            _emit_flat(hits, pat, color, terms_pat)  # flat TSV machine default (piped/--color never)
        else:
            _emit_grouped(hits, pat, color, terms_pat)
        if args.coverage:
            # the pin works from any porcelain render, tty footer or not
            _emit_main_overspec_block(force=True)

    # not in the tty footer below: a short page is wrong the same way when
    # piped, and machine callers read this from the json field
    if not args.json and not args.count and not args.count_by_tier:
        integrity_notice = surface.semantic_integrity_notice(
            semantic_evidence.get("semantic_integrity"))
        if integrity_notice:
            common.log(integrity_notice)
        anchor_notice = surface.semantic_anchor_notice(
            res.get("semantic_status"))
        if anchor_notice:
            common.log(anchor_notice)

    if not args.json and not args.count and not args.count_by_tier and sys.stderr.isatty():
        more = f", showing {args.max}" if args.max and n_total > args.max else ""
        mode = "semantic" if sem_used else "regex" if args.regex else \
            "word" if args.word else "keyword"
        # be honest when the phrase didn't match adjacently and we widened to any-order,
        # or when any-order hits ride below the phrase matches
        if res.get("terms_fallback"):
            mode = "keyword (any-order; no adjacent phrase match)"
        elif res.get("terms_augmented"):
            mode = "keyword (+ any-order hits)"
        elif res.get("content_fallback"):
            mode = "keyword (related terms; no exact match)"
        kind = "chat" if sem_used else "hit"
        ntool = res.get("tool_hits") or 0
        tool = f" ({ntool} of them in tool output)" if ntool else ""
        count = (f"{n_total}+" if not result_totals_exact
                 else str(n_total))
        key = (f" · {_C['d']}key: {surface.speaker_legend()}{_C['r']}"
               if hits and not args.chats and not args.flat and color else "")
        elapsed = time.monotonic() - t0
        took = f"{elapsed * 1000:.0f}ms" if elapsed < 1 else f"{elapsed:.1f}s"
        # semantic counts chats as its unit - "3 chats in 3 chats" says nothing
        where = ("" if sem_used else
                 f" in {n_chats} chat{'s' if n_chats != 1 else ''}")
        coverage = (semantic_evidence.get("semantic_coverage")
                    if sem_used or semantic_lane_participated else None)
        accelerator = (
            semantic_evidence.get("semantic_accelerator_coverage")
            if sem_used or semantic_lane_participated else None)
        embed_state: dict | None = None
        if sem_used and coverage and not coverage.get("complete", True):
            # keyword sees writes in ~12s; the semantic lane sees the last
            # completed embed pass - stamp the horizon when it visibly lags
            try:
                import semantic
                # kept for the lane-change attribution below: one render reads
                # this record once, under the same incomplete-coverage guard
                embed_state = semantic.read_embed_state()
                fin = float(embed_state.get("finished_at") or 0)
                if fin and time.time() - fin > 600:
                    mode += f" (current to {time.strftime('%H:%M', time.localtime(fin))})"
            except Exception:  # noqa: BLE001 -- the stamp must never wound the footer
                pass
        common.log(f"{count} {kind}{'s' if n_total != 1 else ''}{tool}{where}"
                   f" · {mode}{more} · {took}{key}")
        if sem_used and hits:
            for top in hits[:3]:
                big = _mega_session_hint(q, top, args)
                if big:
                    common.log(big)
                    break
        coverage_notice = surface.semantic_coverage_notice(
            coverage, accelerator, suppress_trivial=True)
        if coverage_notice and not semantic_empty_disclosed:
            common.log(coverage_notice)
            _note_lane_change_rebuild(embed_state)
        if not (common.DATA_DIR / "teach.json").exists():
            # self-silencing once setup runs: agents have no agrep prior, so
            # until their instructions name it this history goes unused by them
            tip = ("tip: `agrep setup` lets your agents search this history too "
                   "- none of them will find agrep on their own")
            common.log(f"{_C['d']}{tip}{_C['r']}" if color else tip)
        weak_exact = (res.get("tool_hits") or 0) == n_total or bool(
            res.get("terms_fallback") or res.get("content_fallback"))
        # args, not `mode`: the footer just rebound mode to its display string
        if (hits and not sem_used and not args.regex and not args.word
                and not args.semantic and not args.lexical and weak_exact
                and len(_content_terms(q)) >= 3):
            # keyword is admitting weakness (tool spew only, or fallback tiers
            # with no adjacent phrase match) on a prose query: junk rows must
            # not suppress semantic. Separate labeled block - scores don't mix.
            if self_policy is not None:
                # A second lane can hide additional rows. Without a durable
                # cross-lane identity, the combined omitted count is unknown.
                self_excluded_count = None
            sem_extra = run_query(q, mode="semantic",
                                  **{**fkw, "family_diverse": not args.all_side_chats})
            if sem_extra is not None:
                sem_extra = _drop_meta(_drop_self(sem_extra))
            if sem_extra and not sem_extra.get("fallback_recommended"):
                seen_chats: list[dict] = []
                have = set()
                for sh in sem_extra.get("hits") or []:
                    if sh["session"] not in have:
                        have.add(sh["session"])
                        seen_chats.append(sh)
                    if len(seen_chats) == 5:
                        break
                if seen_chats:
                    why = ("all exact hits are tool output"
                           if (res.get("tool_hits") or 0) == n_total
                           else "no exact phrase match")
                    common.log(f"{why} - chats about this semantically:")
                    for sh in seen_chats:
                        common.log(_compact_line(sh, ()))
        if (not sem_used and not args.regex and not args.word
                and not args.semantic and not args.lexical
                and not args.flat and not args.chats and not args.coverage
                and args.sort == "score"):
            _emit_main_overspec_block()
    elif more_exist and not args.json and not args.count and not args.count_by_tier:
        # piped consumers get no tty footer; disclose the cut without touching stdout
        common.log(surface.completeness_line(
            completeness, tool_hits=res.get("tool_hits") or 0))
    _note_self_exclusion()
    _note_freshness()
    if res.get("tools_excluded") and (args.count or args.count_by_tier):
        return 2
    if hits or (args.count and n_total):
        return 0
    exact = result_totals_exact and not any(res.get(key) for key in (
        "truncated", "index_missing", "tools_excluded",
        "self_exclusion_more_unknown",
    ))
    return surface.grep_absence_exit(
        exact=exact, freshness=indexd_runtime.freshness_story())


def _chat_identity_row(
        session: str, row: dict, concept: str, *, side: bool = False,
) -> dict:
    return {
        "session": session,
        "agent": str(row.get("agent") or ""),
        "project": str(row.get("project") or ""),
        "turns": int(row.get("n") or 0),
        "first_ts": row.get("first_ts"),
        "last_ts": row.get("last_ts"),
        "first_text": common.one_line(row.get("first_text") or ""),
        "concept": concept,
        "side": side,
    }


def _chat_identity_matches(row: dict, tokens: list[str]) -> bool:
    haystack = " ".join((
        row["session"], row["agent"], _proj(row["project"]),
        row["project"], row["first_text"], row["concept"])).lower()
    return all(token in haystack for token in tokens)


def _chat_content_heads(
        query: str, *, agent: str | None, requested: int,
        query_filters: dict | None = None,
        hidden_sessions: set[str] | frozenset[str] = frozenset(),
) -> tuple[dict[str, dict], bool]:
    """Best matching turn per chat; bool says the candidate set is complete."""
    candidate_limit = 0 if requested == 0 else max(20, requested * 2)
    filters = dict(query_filters or {})
    excluded = set(filters.pop("_exclude_sessions", ()))
    excluded.update(hidden_sessions)
    if excluded:
        filters["_exclude_sessions"] = tuple(sorted(excluded))
    try:
        result = run_query(
            query, mode="keyword", limit=candidate_limit, sort="score",
            agent=agent, exhaustive=False, session_limit=candidate_limit,
            allow_fallback=False, exact_totals=False, family_diverse=False,
            **filters)
    except (DirectSnapshotQueryError, OSError, sqlite3.DatabaseError,
            RuntimeError, TypeError, ValueError):
        return {}, False
    if result is None:
        return {}, False
    heads = {}
    for hit in result.get("hits") or ():
        session = str(hit.get("session") or "")
        if session and session not in heads:
            heads[session] = hit
    exact = (
        bool(result.get("totals_exact", True))
        and not any(result.get(key) for key in (
            "truncated", "index_missing", "tools_excluded"))
        and int(result.get("chats") or 0) <= len(heads)
    )
    return heads, exact


def _chat_latest_claims(sessions: list[str]) -> dict[str, dict]:
    """Return one digest-bound latest turn per chat from the indexed corpus."""
    if not sessions:
        return {}
    try:
        db = _load_corpusdb().connect(allow_stale=True)
    except (OSError, sqlite3.DatabaseError, RuntimeError, TypeError, ValueError):
        db = None
    picked: dict[str, tuple[int, str, str | None]] = {}
    if db is not None:
        try:
            for start in range(0, len(sessions), 400):
                page = sessions[start:start + 400]
                marks = ",".join("?" for _ in page)
                query = (
                    "WITH ranked AS (SELECT session,turn,text,content_digest,"
                    "row_number() OVER (PARTITION BY session ORDER BY turn DESC,"
                    "ts DESC,rowid DESC) AS choice FROM msgs WHERE session IN ("
                    f"{marks}) AND text<>'') SELECT session,turn,text,content_digest "
                    "FROM ranked WHERE choice=1"
                )
                for session, turn, text, digest in db.execute(query, page):
                    picked[str(session)] = (int(turn), str(text or ""), digest)
        except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
            picked.clear()
        finally:
            db.close()
    candidates = common.indexed_session_prefix_candidates(sessions)
    out = {}
    for session, (turn, text, digest) in picked.items():
        try:
            claim = digest or compact.content_digest(text)
            handle = compact.encode_bound_result_handle(
                {"session": session, "turn": turn, "content_digest": claim},
                session_index=candidates)
        except (compact.CompactError, TypeError, ValueError):
            continue
        out[session] = {"last_turn": turn, "latest_handle": handle}
    return out


def chats_main(argv: list[str] | None = None) -> int:
    """`agrep chats [pattern]` - find a chat by identity or indexed content.

    Unfiltered rows stay newest-first. A pattern matches chat identity and
    conversation content, ranks content hits, and points `open` at the matching
    turn rather than making the caller rediscover it."""
    common.utf8_stdio()
    ap = surface.ArgumentParser(
        prog="agrep chats",
        description="find indexed chats by project, id, opening line, or "
                    "conversation content",
        allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  agrep chats                      newest indexed chats\n"
               "  agrep chats apartment            project / opening match\n"
               "  agrep chats drafting             topic match inside chats\n"
               "  agrep chats 0199fa               exact id prefixes include side chats\n"
               "  agrep chats webapp --agent codex --json\n"
               "\nA leading @session opens its latest indexed turn with `agrep around` "
               "and also works as a `--chat` selector; `agrep around @...` pins "
               "an exact turn when available.\nexit: 0 found, 1 proven none, "
               "2 no index or unverified result.")
    ap.add_argument("pattern", nargs="*",
                    help="words matched against chat identity and indexed "
                         "conversation content (all must hit)")
    ap.add_argument("-n", "--max", type=int, default=20, metavar="N",
                    help="show at most N chats (default 20; 0 = all)")
    ap.add_argument("--agent", help=f"only this agent ({', '.join(common.KNOWN_AGENTS)})")
    ap.add_argument("--side", action="store_true",
                    help="include side chats (spawned subagent sessions)")
    ap.add_argument("--json", action="store_true",
                    help="one metadata object, then one JSON object per chat")
    ap.add_argument("--no-auto", action="store_true",
                    help=surface.NO_AUTO_HELP)
    ap.add_argument("--color", choices=("auto", "always", "never"),
                    default="auto")
    args = ap.parse_args(argv)
    if any(not token.strip() for token in args.pattern):
        ap.error("pattern arguments must not be empty")
    blank_filter = surface.filter_value_error(args)
    if blank_filter:
        ap.error(blank_filter)
    if args.agent:
        args.agent = common.normalize_agent_name(args.agent.lower())
    if args.max < 0:
        ap.error("--max must be 0 or greater")

    import explore
    machine_stdout = bool(args.json or not sys.stdout.isatty())
    if not indexd_runtime.ensure_index(
            auto=not args.no_auto, quiet=machine_stdout):
        return 2
    index = explore._session_index()
    side_sessions = {
        session for session, raw in index.items()
        if explore._indexed_chat_is_side({**raw, "session": session})
    }
    concepts = explore._session_concept()
    tokens = [t.lower() for t in args.pattern]
    agent = (args.agent or "").lower()
    identity_token = (
        tokens[0].lstrip("@") if len(tokens) == 1
        and not any(char.isspace() for char in tokens[0]) else "")
    direct_sessions = {
        session for session in index
        if identity_token and session.lower().startswith(identity_token)
    }
    match_tokens = [identity_token] if direct_sessions else tokens
    content_lookup = bool(tokens and not direct_sessions)
    self_policy = None
    if content_lookup and common.in_agent_context():
        self_policy = common.calling_self_exclusion()
    content_heads, content_exact = (
        _chat_content_heads(
            " ".join(args.pattern), agent=args.agent, requested=args.max,
            query_filters=(self_policy.query_filters()
                           if self_policy is not None else None),
            hidden_sessions=frozenset() if args.side else side_sessions)
        if content_lookup else ({}, True)
    )
    content_rank = {
        session: rank for rank, session in enumerate(content_heads)
    }
    rows = []
    for session, raw in index.items():
        row = _chat_identity_row(
            session, raw, concepts.get(session, ""),
            side=session in side_sessions)
        if row["side"] and not args.side and session not in direct_sessions:
            continue
        if agent and agent not in row["agent"].lower():
            continue
        identity_match = (
            not match_tokens or _chat_identity_matches(row, match_tokens))
        content_hit = content_heads.get(session)
        if (self_policy is not None and session not in direct_sessions
                and content_hit is None
                and self_policy.excludes(session, None)):
            continue
        if match_tokens and not identity_match and content_hit is None:
            continue
        row["_content_hit"] = content_hit
        row["_content_rank"] = content_rank.get(session)
        rows.append(row)
    if content_lookup:
        rows.sort(key=lambda row: (
            0 if row["_content_rank"] is not None else 1,
            row["_content_rank"] if row["_content_rank"] is not None
            else -(row["last_ts"] or 0)))
    else:
        rows.sort(key=lambda row: -(row["last_ts"] or 0))
    matched = len(rows)
    matches_exact = not explore._session_index_skipped() and content_exact
    shown = rows[:args.max] if args.max else rows
    latest = _chat_latest_claims([row["session"] for row in shown])
    session_index = common.indexed_session_prefix_candidates(
        row["session"] for row in shown)
    for row in shown:
        row["session_handle"] = compact.encode_session_handle(
            row["session"], session_index=session_index)
        if row["session_handle"] is None:
            row["session_handle_unavailable_reason"] = (
                "session id is outside the public handle grammar")
        row.update(latest.get(row["session"], {}))
        content_hit = row.pop("_content_hit")
        row.pop("_content_rank")
        row["match_source"] = "content" if content_hit is not None else "identity"
        if content_hit is not None:
            row["match_turn"] = int(content_hit.get("turn") or 0)
            row["match_text"] = common.one_line(
                content_hit.get("snippet") or content_hit.get("text") or "")
            try:
                row["match_handle"] = compact.encode_bound_result_handle(
                    content_hit, session_index=session_index)
            except (compact.CompactError, TypeError, ValueError):
                pass
    more_command = None
    truncated = len(shown) < matched or not matches_exact
    if args.max and truncated:
        pattern_args = ("--", *args.pattern) if args.pattern else ()
        next_max = max(80, args.max * 4)
        more_command = console.shell_command(
            "agrep", "chats",
            *(("--agent", args.agent) if args.agent else ()),
            *(("--side",) if args.side else ()),
            *(("--json",) if args.json else ()),
            *(("--no-auto",) if args.no_auto else ()),
            "-n", str(next_max), *pattern_args,
            fallback="") or None
    completeness = surface.completeness_disclosure(
        shown=len(shown), total=matched, unit="matching chat",
        totals_exact=matches_exact,
        truncated=truncated,
        more_command=more_command)
    # a matched chat proves --agent is populated; only a zero needs the census
    coverage = (filter_coverage(args) if not rows
                else surface.filter_coverage_disclosure([], checked=True))
    story = (
        surface.FreshnessStory(
            "unverified", code="freshness-unchecked",
            detail=indexd_runtime.NO_AUTO_REFRESH_REASON)
        if args.no_auto else indexd_runtime.freshness_story())
    machine_fields = None
    if args.json:
        publication_converging = indexd_runtime.foreground_refresh_converging(
            checked=not args.no_auto)
        freshness = indexd_runtime.machine_freshness(
            checked=not args.no_auto,
            publication_converging=publication_converging)
        machine_fields = _load_corpusdb().machine_freshness_fields(
            freshness, publication_converging=publication_converging)
        fields = dict(machine_fields)
        fields["freshness"] = surface.row_freshness_disclosure(
            machine_fields["freshness"], first=True)
        meta = {"kind": "agrep-meta", "completeness": completeness,
                "filter_coverage": coverage, **fields}
        if not shown:
            meta["hits"] = []
        print(json.dumps(meta, ensure_ascii=False, separators=(",", ":")))
        for row in shown:
            print(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
    elif shown:
        color = _color_on(args.color)
        for row in shown:
            handle = (row["session_handle"] or
                      f"session={terminal_safe(row['session'])}")
            side = " [side chat]" if row["side"] else ""
            label = row["concept"] or _proj(row["project"])
            head = " ".join(value for value in (
                terminal_safe(handle), terminal_safe(row["agent"]),
                terminal_safe(label),
                common.age_label(row["last_ts"]),
                f"{row['turns']}t") if value)
            matched_text = row.get("match_text")
            preview = terminal_safe(
                matched_text if matched_text is not None else row["first_text"])[:96]
            if matched_text is not None:
                preview = f"match: {preview}"
            open_handle = row.get("match_handle") or row.get("latest_handle")
            followup = (console.shell_command(
                "agrep", "around", open_handle, fallback="") if open_handle else "")
            exact = f" · {followup}" if followup else ""
            if color:
                print(f"{_C['hd']}{head}{_C['r']}{side}  "
                      f"{_C['d']}{preview}{exact}{_C['r']}")
            else:
                print(f"{head}{side}  {preview}{exact}")
    what = "chat" if matched == 1 else "chats"
    if shown:
        sys.stdout.flush()
    scope = ("" if args.side or direct_sessions else
             " (side chats hidden; --side shows them)")
    named_page = completeness.get("more_command")
    cut = f" · larger page: {named_page}" if named_page else ""
    floor = "" if completeness["total_basis"] == "exact" else "+"
    if not args.json:
        order = "best match first" if content_lookup else "newest first"
        common.log(f"showing {len(shown)} of {matched}{floor} matching {what}, "
                   f"{order}{scope}{cut}")
        for empty_dimension in coverage["empty_dimensions"]:
            common.log(surface.empty_dimension_line(empty_dimension))
        notice = surface.freshness_story_line(story)
        if notice:
            common.log(notice)
    if shown:
        return 0
    return surface.grep_absence_exit(
        exact=completeness["total_basis"] == "exact", freshness=story)


if __name__ == "__main__":
    entrypoint = {
        _SEMANTIC_CHILD_ARG: _explorer_semantic_child_main,
        _SEMANTIC_FALLBACK_CHILD_ARG: _semantic_local_fallback_child_main,
    }.get(sys.argv[1] if len(sys.argv) == 2 else "", main)
    raise SystemExit(entrypoint())
