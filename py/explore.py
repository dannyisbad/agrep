"""Read-only transcript access for keyword fallback, recall windows, and enrichment.

Primary search normally uses corpusdb.py. This module supplies the generation-aware
JSONL fallback and conversation-window reads; optional summaries, concepts, affect,
and vibe data are attached only when their sidecars exist.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import OrderedDict
from contextlib import contextmanager
import functools
import json
import os
import re
import sqlite3
import threading
from pathlib import Path

import common
import compact
import conceptpair
import display_policy
import surface_policy

HOT_T = 0.15  # A message above this threshold reads as emotionally hot.
EVENTS_DIR_NAME = "events"
# --------------------------------------------------------------------------- caches
# Long-lived CLI workers can span reindexes, so each public entry point checks a
# generation stamp and drops caches when it moves.

_GEN_FILES = ("messages.jsonl", "summaries.jsonl", "emotions.jsonl", "replies.jsonl",
              "concepts.json", "session_concepts.jsonl", conceptpair.MANIFEST_NAME,
              "vibe/index.json", "events/.generation", "settings.json",
              "sessions.jsonl", ".derived_generation.json", ".ingest.sig", "corpus.db")
_GEN: tuple | None = None
# A timeline is a few integers per real turn, even when the same session has tens of
# thousands of tool-search rows. Reuse it across recall's expansion windows without
# retaining any transcript/event bodies.
_SESSION_CONTEXTS: OrderedDict[tuple, dict | None] = OrderedDict()
_SESSION_CONTEXT_MAX = 32
_SESSION_CONTEXT_LOCK = threading.Lock()
_DIRECT_SNAPSHOT_LOCAL = threading.local()


class DirectSnapshotError(RuntimeError):
    """The materialized transcript cannot support a verified direct result."""


class DirectSnapshotMoved(DirectSnapshotError):
    """The committed materialized generation moved during one scan attempt."""


def _record_direct_snapshot_damage(name: str, skipped: int) -> None:
    damage = getattr(_DIRECT_SNAPSHOT_LOCAL, "damage", None)
    if damage is not None and skipped:
        damage[name] = damage.get(name, 0) + int(skipped)


def _freshen() -> None:
    global _GEN
    gen = []
    for name in _GEN_FILES:
        try:
            path = common.DATA_DIR / name
            if name == "events/.generation":
                gen.append(common._event_file_stamp(path))
            else:
                st = path.stat()
                gen.append((st.st_size, st.st_mtime_ns, st.st_ctime_ns,
                            st.st_dev, st.st_ino))
        except OSError:
            gen.append(None)
    gen = tuple(gen)
    if gen == _GEN:
        return
    for fn in (_vibe_index, _summaries, _concept_pair, _concept_names, _session_concept,
               _summary_by_session, _messages_by_session_read, _messages_by_session,
               _primary_models, _emotions_by_id, _reply_records_by_id_read,
               _reply_records_by_id, _replies_by_id,
               _session_index, _session_index_read):
        fn.cache_clear()
    with _SESSION_CONTEXT_LOCK:
        _SESSION_CONTEXTS.clear()
    _GEN = gen


@functools.lru_cache(maxsize=1)
def _vibe_index() -> dict[str, dict]:
    """session -> its vibe index entry (peak_turn, juice, verdict, ...)."""
    p = common.DATA_DIR / "vibe" / "index.json"
    if not p.exists():
        return {}
    return {e["session"]: e for e in json.loads(p.read_text(encoding="utf-8"))}


@functools.lru_cache(maxsize=1)
def _summaries() -> list[dict]:
    p = common.DATA_DIR / "summaries.jsonl"
    out = []
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


@functools.lru_cache(maxsize=1)
def _concept_pair() -> tuple[list[dict], list[dict]]:
    try:
        concepts, sessions, _ = conceptpair.read(common.DATA_DIR)
        return concepts, sessions
    except conceptpair.IncoherentConceptPair as exc:
        # a missing pair is the normal cold state (concept stage never run);
        # only a present-but-unreadable pair deserves a line
        if not isinstance(exc.__cause__, FileNotFoundError):
            common.log(f"concept pair unavailable: {exc}")
        return [], []


@functools.lru_cache(maxsize=1)
def _concept_names() -> dict[int, str]:
    """concept_id -> clean display name (Gemma 4 'name', falling back to the raw label)."""
    out: dict[int, str] = {}
    for r in _concept_pair()[0]:
        out[int(r["concept_id"])] = (r.get("name") or r.get("label") or "").strip()
    return out


@functools.lru_cache(maxsize=1)
def _session_concept() -> dict[str, str]:
    """session -> clean concept name (the cwd-independent thread it belongs to)."""
    names = _concept_names()
    out: dict[str, str] = {}
    for row in _concept_pair()[1]:
        out[row["session"]] = names.get(int(row.get("concept_id", -1))) or row.get("label", "")
    return out


@functools.lru_cache(maxsize=1)
def _summary_by_session() -> dict[str, dict]:
    return {o["session"]: o for o in _summaries()}


@functools.lru_cache(maxsize=1)
def _session_index_read() -> tuple[dict[str, dict], int]:
    """sessions.jsonl parsed damage-tolerant: (rows, corrupt lines skipped).
    A torn line is agrep's own derived damage, never a caller's crash."""
    p = common.DATA_DIR / "sessions.jsonl"
    rows: dict[str, dict] = {}
    skipped = 0
    try:
        with p.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    o = json.loads(line)
                    session = o["session"]
                except (json.JSONDecodeError, TypeError, KeyError):
                    skipped += 1
                    continue
                # schema-mutant rows (valid JSON, wrong shape: {"session": []},
                # null/number ids) are the same damage class as torn bytes
                if not isinstance(session, str):
                    skipped += 1
                    continue
                # so are non-numeric aggregate fields ("last_ts":"yesterday",
                # "n":"lots"): consumers sort and count by them, so a mutant
                # here crashed every listing until the row was hand-removed
                try:
                    for field in ("n", "first_ts", "last_ts"):
                        o[field] = int(o.get(field) or 0)
                except (TypeError, ValueError):
                    skipped += 1
                    continue
                rows[session] = o
    except OSError:
        return {}, 0
    return rows, skipped


def _session_index_skipped() -> int:
    """Corrupt sessions.jsonl lines the served index had to skip (0 healthy);
    a surface counting from the index owes its reader this floor mark."""
    return (_session_index_read()[1]
            if (common.DATA_DIR / "sessions.jsonl").exists() else 0)


def _kick_derived_repair() -> None:
    """Law 1: a damaged derived aggregate is a repair task, not a report.
    Fire-and-forget; the beat gives the revived daemon work to run now."""
    try:
        import indexd_runtime
        if indexd_runtime.kick_background_repair().in_flight:
            indexd_runtime.SEARCH_BEAT_PATH.touch()
    except Exception:  # noqa: BLE001 -- a kick never fails a read
        pass


@functools.lru_cache(maxsize=1)
def _session_index() -> dict[str, dict]:
    """session -> tiny aggregate {agent, project, n, first_ts, last_ts, first_text}.
    Materialized by `agrep index` (data/sessions.jsonl, small) so session listings never
    parse the much larger messages.jsonl. Falls back to deriving the same
    shape from the big file when sessions.jsonl is absent or holds no
    parseable row - a corrupt or truncated aggregate beside a populated
    corpus must never crash a listing or serve a confident zero."""
    p = common.DATA_DIR / "sessions.jsonl"
    materialized = p.exists()
    skipped = 0
    if materialized:
        out, skipped = _session_index_read()
        if out:
            if skipped:
                _kick_derived_repair()
            return out
    out = {}
    for s, rows in _messages_by_session().items():
        first = next((
            r.get("text", "")
            for r in sorted(rows, key=lambda r: r.get("turn", 0))
            if r.get("text", "").strip() and r.get("who") != "recap"
        ), "")
        out[s] = {"session": s, "agent": rows[0].get("agent", ""),
                  "project": rows[0].get("project", ""), "n": len(rows),
                  "first_ts": min((r.get("ts", 0) for r in rows if r.get("ts", 0)), default=0),
                  "last_ts": max((r.get("ts", 0) for r in rows), default=0),
                  "first_text": common.one_line(first)[:120]}
    if materialized and (out or skipped):
        # a materialized aggregate that lists nothing the corpus holds is torn
        _kick_derived_repair()
    return out


def _indexed_chat_is_side(row: dict) -> bool:
    return common.is_side_session(row)


def list_chats(limit: int = 80, *, include_side: bool = False) -> dict:
    """Recent published chats for read-only browsing."""
    _freshen()
    rows = _session_index()
    totals_exact = _session_index_skipped() == 0
    summaries = _summary_by_session()
    concepts = _session_concept()
    ordered = sorted(
        rows.values(),
        key=lambda row: (int(row.get("last_ts") or 0),
                         str(row.get("session") or "")),
        reverse=True)
    visible = [
        row for row in ordered
        if include_side or not _indexed_chat_is_side(row)
    ]
    chats = []
    for row in visible[:max(0, int(limit))]:
        session = str(row.get("session") or "")
        summary = summaries.get(session) or {}
        chats.append({
            "session": session,
            "agent": row.get("agent") or summary.get("agent") or "",
            "project": row.get("project") or summary.get("cwd_project") or "",
            "n_msgs": int(row.get("n") or 0),
            "first_ts": int(row.get("first_ts") or 0),
            "last_ts": int(row.get("last_ts") or 0),
            "first_text": row.get("first_text") or "",
            "title": summary.get("title") or "",
            "summary": summary.get("summary") or "",
            "concept": concepts.get(session) or "",
            "side": _indexed_chat_is_side(row),
        })
    return {
        "total": len(visible), "totals_exact": totals_exact,
        "chats": chats,
    }


@functools.lru_cache(maxsize=1)
def _messages_by_session_read() -> tuple[dict[str, list[dict]], int]:
    """Parse the message publication once while retaining its damage count."""
    out: dict[str, list[dict]] = {}
    skipped = 0
    p = common.MESSAGES_PATH
    if p.exists():
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    skipped += 1
                    continue
                if not isinstance(o, dict):
                    skipped += 1
                    continue
                s = o.get("session")
                if not isinstance(s, str) or not s:
                    skipped += 1
                    continue
                text = o.get("text", "")
                string_fields = ("id", "agent", "project", "model",
                                 "model_source", "who")
                if (not isinstance(text, str)
                        or any(name in o and not isinstance(o[name], str)
                               for name in string_fields)
                        or any(name in o and type(o[name]) is not int
                               for name in ("turn", "ts"))):
                    skipped += 1
                    continue
                out.setdefault(s, []).append(o)
    if skipped:
        _kick_derived_repair()
    return out, skipped


@functools.lru_cache(maxsize=1)
def _messages_by_session() -> dict[str, list[dict]]:
    """session -> message rows, with damage retained by the underlying read."""
    rows, skipped = _messages_by_session_read()
    _record_direct_snapshot_damage("messages.jsonl", skipped)
    return rows


def _native_messages_for_sessions(
        sessions: set[str], expected_ingest_generation: str,
) -> dict[str, list[dict]] | None:
    """Read candidate timelines from the committed derived generation."""
    import hashlib
    import corpusdb

    root = common.DATA_DIR
    messages_path = common.MESSAGES_PATH
    proof_path = root / ".derived_generation.json"
    signal_path = root / ".ingest.sig"
    try:
        _signal_identity, signal_before = corpusdb._read_derived_file(
            signal_path, corpusdb._INGEST_SIGNATURE_MAX_BYTES)
        _proof_identity, proof_before = corpusdb._read_derived_file(
            proof_path, corpusdb._DERIVED_PROOF_MAX_BYTES)
        if hashlib.sha256(signal_before).hexdigest() != expected_ingest_generation:
            return None
        proof = json.loads(proof_before)
        files = proof.get("files") if isinstance(proof, dict) else None
        committed = signal_before.decode("utf-8").strip()
        if (not isinstance(proof, dict)
                or proof.get("version") != corpusdb._DERIVED_PROOF_VERSION
                or not committed or proof.get("signature") != committed
                or not isinstance(files, list)
                or len(files) != len(corpusdb._DERIVED_PROOF_NAMES)):
            return None
        by_name = {
            row.get("name"): row for row in files if isinstance(row, dict)
        }
        if set(by_name) != set(corpusdb._DERIVED_PROOF_NAMES):
            return None
        expected = by_name["messages.jsonl"]
        identity = corpusdb._proof_file_identity(messages_path)
        if (identity[0] != expected.get("len")
                or identity[1] != expected.get("modified_ns")):
            return None
        digest = hashlib.sha256()
        out: dict[str, list[dict]] = {}
        with corpusdb._open_derived_file(messages_path, identity) as source:
            for raw in source:
                digest.update(raw)
                if not raw.strip():
                    continue
                row = json.loads(raw)
                if not isinstance(row, dict):
                    return None
                session = row.get("session")
                if session in sessions:
                    out.setdefault(session, []).append(row)
        problem = corpusdb._validate_derived_file(
            root, expected, routine=True)
        if problem == corpusdb.GENERATION_VERIFICATION_DEFERRED:
            token = expected.get("change_token")
            wanted = token.get("ContentSha256") if isinstance(token, dict) else None
            if wanted != list(digest.digest()):
                return None
        elif problem is not None:
            return None
        _signal_after_identity, signal_after = corpusdb._read_derived_file(
            signal_path, corpusdb._INGEST_SIGNATURE_MAX_BYTES)
        _proof_after_identity, proof_after = corpusdb._read_derived_file(
            proof_path, corpusdb._DERIVED_PROOF_MAX_BYTES)
        if signal_before != signal_after or proof_before != proof_after:
            return None
        return out
    except (FileNotFoundError, OSError, RecursionError, UnicodeError,
            ValueError, TypeError, json.JSONDecodeError):
        _kick_derived_repair()
        return None


def _models_for_rows(rows: list[dict]) -> list[dict]:
    counts: dict[str, int] = {}
    for row in rows:
        model = row.get("model", "")
        if model and not (model.startswith("<") and model.endswith(">")):
            counts[model] = counts.get(model, 0) + 1
    return [{"name": model, "n": count}
            for model, count in sorted(
                counts.items(), key=lambda item: (-item[1], item[0]))]


@functools.lru_cache(maxsize=16)
def _primary_models(sessions: tuple[str, ...]) -> dict[str, str]:
    """Read model counts only for requested sessions."""
    if not sessions:
        return {}
    db = None
    try:
        import corpusdb

        path = common.DATA_DIR / "corpus.db"
        try:
            canonical = path.resolve(strict=False)
            owned_path = Path(corpusdb.DB_PATH).resolve(strict=False)
        except OSError:
            canonical, owned_path = path, Path(corpusdb.DB_PATH)
        if canonical == owned_path:
            ownership = corpusdb._derived_write_ownership(
                for_write=common.data_dir_readonly(common.DATA_DIR))
            if not ownership.writable:
                return {}
        db = corpusdb._connect_read_snapshot(
            path, 1.0,
            max_clone_bytes=corpusdb._ROUTINE_ALIAS_CLONE_MAX_BYTES)
        meta = dict(db.execute(
            "SELECT key, value FROM meta WHERE key IN ('stamp', 'schema')"))
        if (meta.get("schema") != corpusdb._SCHEMA
                or not corpusdb._stamps_equal(
                    meta.get("stamp", ""), corpusdb._stamp())):
            return {}
        marks = ",".join("?" for _ in sessions)
        rows = db.execute(
            "SELECT session, model, COUNT(DISTINCT turn) AS n FROM msgs "
            f"WHERE model <> '' AND session IN ({marks}) "
            "GROUP BY session, model ORDER BY session, n DESC, model ASC",
            sessions)
        out: dict[str, str] = {}
        for session, model, _count in rows:
            if (isinstance(session, str) and session
                    and isinstance(model, str) and model
                    and not (model.startswith("<") and model.endswith(">"))):
                out.setdefault(session, model)
        source_stable = getattr(db, "source_stable", None)
        if source_stable is not None and not source_stable():
            return {}
        return out
    except (OSError, sqlite3.Error):
        return {}
    finally:
        if db is not None:
            db.close()


def session_models(sessions: list[str]) -> dict[str, str]:
    """Most-used real model for each requested session."""
    _freshen()
    unique = tuple(dict.fromkeys(
        session for session in sessions if isinstance(session, str) and session))
    models = _primary_models(unique)
    return {session: models.get(session, "") for session in unique}


@functools.lru_cache(maxsize=1)
def _emotions_by_id() -> dict[str, dict]:
    """message id -> affect row, parsed once (mirrors the messages cache)."""
    out: dict[str, dict] = {}
    p = common.DATA_DIR / "emotions.jsonl"
    if p.exists():
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if o.get("id"):
                    out[o["id"]] = o
    return out


@functools.lru_cache(maxsize=1)
def _reply_records_by_id_read() -> tuple[dict[str, dict], int]:
    """Message id to reply text and ingest-cap provenance, parsed once.
    Damage-tolerant like the session aggregate: one mutant row must never
    poison every reply expansion (this cache serves around AND recall)."""
    out: dict[str, dict] = {}
    skipped = 0
    p = common.DATA_DIR / "replies.jsonl"
    if p.exists():
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    skipped += 1
                    continue
                if not isinstance(o, dict):
                    skipped += 1
                    continue
                row_id = o.get("id")
                reply = o.get("reply", "") or ""
                if not isinstance(row_id, str) or not row_id or not isinstance(reply, str):
                    skipped += 1
                    continue
                if row_id:
                    try:
                        chars = max(len(reply), int(o.get("reply_chars", len(reply))))
                    except (TypeError, ValueError):
                        chars = len(reply)
                    try:
                        digest = (
                            compact.content_digest(reply)
                            if "content_digest" not in o else
                            compact.require_content_digest(o["content_digest"])
                        )
                    except compact.CompactError:
                        # an invalid stored digest is unverifiable provenance:
                        # the row is skipped and healing, never a crash
                        skipped += 1
                        continue
                    out[row_id] = {
                        "reply": reply,
                        "content_digest": digest,
                        "reply_chars": chars,
                        "reply_truncated": bool(o.get("reply_truncated"))
                                           or chars > len(reply),
                    }
    if skipped:
        _kick_derived_repair()
    return out, skipped


@functools.lru_cache(maxsize=1)
def _reply_records_by_id() -> dict[str, dict]:
    rows, skipped = _reply_records_by_id_read()
    _record_direct_snapshot_damage("replies.jsonl", skipped)
    return rows


@functools.lru_cache(maxsize=1)
def _replies_by_id() -> dict[str, str]:
    """Message id -> agent reply text compatibility view."""
    return {key: value["reply"] for key, value in _reply_records_by_id().items()}

def _native_family_members(
        caller: str,
) -> tuple[frozenset[str], frozenset[str]]:
    indexed = common.indexed_calling_family_with_sides(caller)
    if indexed is not None:
        return indexed[1], indexed[2]
    census = common.read_session_family_census()
    if census is None or caller not in census.sessions:
        return frozenset({caller}), frozenset()
    memo: dict[str, str] = {}
    root = common.family_root(caller, census.parents, memo)
    members = frozenset(
        session for session in census.sessions
        if common.family_root(session, census.parents, memo) == root)
    return members, frozenset(
        session for session in members if session in census.parents)


def _iter_kw_corpus(flt: dict | None = None):
    """Yield canonical JSONL-fallback rows, applying cheap filters first.

    This is the JSONL fallback for moments when the published SQLite corpus cannot be
    opened. It does not retain a second, pre-lowercased copy of the entire transcript.
    In particular, prose-only recall never constructs event rows just to discard
    them later. Tool rows likewise carry no ``content_digest``: theirs derives from
    the row's own text, so ``_entry_content_digest`` mints one for the few rows that
    become hits instead of hashing every event payload on every query.

    ``flt`` uses the same keys and semantics as corpusdb's filter mapping.  Metadata
    filters run before text is copied/lowercased; the speaker filter runs before the
    corresponding prose/reply/tool lane is even visited where possible.
    """
    flt = flt or {}
    agent = (flt.get("agent") or "").lower()
    project = (flt.get("project") or "").lower()
    chat = (flt.get("chat") or "").lower()
    who = flt.get("who")
    model = (flt.get("model") or "").lower()
    model_soft = bool(flt.get("model_soft"))
    since_ms = flt.get("since_ms")
    until_ms = flt.get("until_ms")
    exclude_session = str(flt.get("exclude_session") or "")
    exclude_from_turn = flt.get("exclude_session_from_turn")
    exclude_family = flt.get("exclude_family", True)
    required_event_literals = tuple(
        token for token in (flt.get("_required_event_literals") or ())
        if isinstance(token, str) and token.isascii() and token)
    lazy_tool_literal = flt.get("_single_keyword_tool_literal")
    if (not isinstance(lazy_tool_literal, str)
            or not lazy_tool_literal.isascii()
            or not lazy_tool_literal.isalnum()):
        lazy_tool_literal = None
    exclude_sessions = frozenset(flt.get("_exclude_sessions") or ())
    if flt.get("_native_family_frozen"):
        exclude_sessions |= frozenset(
            flt.get("_native_excluded_sessions") or ())
    elif exclude_session and exclude_from_turn is None:
        if exclude_family:
            members, _ = _native_family_members(exclude_session)
            exclude_sessions |= members
        else:
            exclude_sessions |= frozenset({exclude_session})
    # Match search._filtered exactly: an explicit --who takes precedence over
    # include_tools, so who=tool remains meaningful even with include_tools=False.
    _admits = surface_policy.speaker_filter_admits
    who_explicit = who is not None and getattr(who, "include", who) is not None
    if "_tool_lane_enabled" in flt:
        want_tools = (bool(flt["_tool_lane_enabled"])
                      and not flt.get("_skip_event_rows"))
    else:
        want_tools = (_admits(who, "tool")
                      and (who_explicit or flt.get("include_tools", True))
                      and common.setting("tools") != "off"
                      and not flt.get("_skip_event_rows"))
    want_replies = _admits(who, "agent")
    include_set = getattr(who, "include", None)
    want_messages = (include_set is None or bool(set(include_set)
                                                 - {"agent", "tool"})
                     if not isinstance(who, str)
                     else who not in ("agent", "tool"))

    def metadata_ok(session: str, row_agent: str, row_project: str,
                    row_model: str, ts: int | None, turn: int | None) -> bool:
        if session in exclude_sessions:
            return False
        if (exclude_session and exclude_from_turn is not None
                and session == exclude_session):
            if (type(exclude_from_turn) is int and type(turn) is int  # noqa: E721
                    and turn >= exclude_from_turn):
                return False
        if chat and not session.lower().startswith(chat):
            return False
        if agent and agent not in (row_agent or "").lower():
            return False
        if project and project not in (row_project or "").lower():
            return False
        if model:
            actual = (row_model or "").lower()
            if (model not in actual) if model_soft else (model != actual):
                return False
        # null ts filters as 0, matching corpusdb's coalesce(ts,0) - gating on
        # `ts is not None` let timestampless rows through explicit --since
        if since_ms is not None and (ts or 0) < since_ms:
            return False
        if until_ms is not None and (ts or 0) >= until_ms:
            return False
        return True

    msgs = _messages_by_session()
    # Replies can be large. Tool-only/user-only filters should not populate that cache.
    if want_replies:
        reps = _reply_records_by_id()
    else:
        reps = {}
    concept = _session_concept()
    for session, rows in msgs.items():
        if session in exclude_sessions:
            continue
        if chat and not session.lower().startswith(chat):
            continue
        c = concept.get(session, "")
        for o in rows:
            turn, ts = o.get("turn", 0), o.get("ts", 0)
            row_agent, row_project = o.get("agent", ""), o.get("project", "")
            row_model = o.get("model", "")
            if not metadata_ok(
                    session, row_agent, row_project, row_model, ts, turn):
                continue
            model_source = o.get(
                "model_source", "explicit" if row_model else "unknown")
            if want_messages:
                t = o.get("text", "") or ""
                row_who = o.get("who", "user")
                if t and _admits(who, row_who):
                    digest = (
                        compact.content_digest(t)
                        if "content_digest" not in o else
                        compact.require_content_digest(o["content_digest"])
                    )
                    yield {"session": session, "turn": turn, "ts": ts,
                           "agent": row_agent, "project": row_project, "concept": c,
                           "model": row_model, "model_source": model_source,
                           "who": row_who, "text": t, "low": t.lower(),
                           "content_digest": digest}
            if want_replies:
                record = reps.get(o.get("id", ""))
                if record and record["reply"]:
                    r = record["reply"]
                    yield {"session": session, "turn": turn, "ts": ts,
                           "agent": row_agent, "project": row_project, "concept": c,
                           "model": row_model, "model_source": model_source,
                           "who": "agent", "text": r, "low": r.lower(),
                           "content_digest": record["content_digest"]}
    if want_tools:
        strict_events = getattr(_DIRECT_SNAPSHOT_LOCAL, "damage", None) is not None

        def event_keys():
            for event_session, event_rows in msgs.items():
                if not event_rows:
                    continue
                first = event_rows[0]
                first_agent = first.get("agent", "")
                if metadata_ok(
                        event_session, first_agent, first.get("project", ""),
                        "", None, first.get("turn")):
                    yield first_agent, event_session

        filtered = any((agent, project, chat, model, since_ms, until_ms))
        for first_agent, session, payload in common.event_blobs_bulk(
                event_keys(), full=not filtered and len(msgs) > 500,
                required_literals=required_event_literals):
            rows = msgs.get(session)
            if not rows or rows[0].get("agent", "") != first_agent:
                continue
            first = rows[0]
            first_project = first.get("project", "")
            if not metadata_ok(
                    session, first_agent, first_project, "",
                    None, first.get("turn")):
                continue
            turns = [(row.get("ts", 0), row.get("turn", 0)) for row in rows]
            if lazy_tool_literal is not None:
                for event, ts, turn, occurrences in \
                        common.tool_literal_matches_from_payload(
                            payload, turns, lazy_tool_literal,
                            required_event_literals, strict=strict_events):
                    if not metadata_ok(
                            session, first_agent, first_project, "", ts, turn):
                        continue
                    yield {
                        "session": session, "turn": turn, "ts": ts,
                        "agent": first_agent, "project": first_project,
                        "concept": concept.get(session, ""), "model": "",
                        "model_source": "tool", "who": "tool",
                        "_agrep_tool_event": event,
                        "_agrep_occurrences": occurrences,
                    }
                continue
            for tool in common.tool_rows_from_payload(
                    payload, turns, required_event_literals,
                    strict=strict_events):
                if not metadata_ok(
                        session, first_agent, first_project, "",
                        tool["ts"], tool["turn"]):
                    continue
                text = tool["text"]
                yield {"session": session, "turn": tool["turn"], "ts": tool["ts"],
                       "agent": first_agent, "project": first_project,
                       "concept": concept.get(session, ""), "model": "",
                       "model_source": "tool", "who": "tool", "text": text,
                       "low": text.lower(),
                       # Preserve canonical event provenance below the owner seam.
                       # Exact output spans let F7 spend snippet budget on the
                       # payload instead of flattened input scaffolding.
                       "event_kind": tool.get("kind", ""),
                       "kind": tool.get("kind", ""),
                       "name": tool.get("name", ""),
                       "input": tool.get("input", ""),
                       "output": tool.get("output", ""),
                       "input_chars": tool.get("input_chars"),
                       "output_chars": tool.get("output_chars"),
                       "output_bytes": tool.get("output_bytes"),
                       "input_truncated": tool.get("input_truncated"),
                       "output_truncated": tool.get("output_truncated"),
                       "ok": tool.get("ok"),
                       "call_id": tool.get("call_id", ""),
                       "child": tool.get("child", ""),
                       "payload_bounds": tool.get("payload_bounds")}


_NATIVE_EVENT_SCAN_PROTOCOL = 2
_NATIVE_EVENT_REQUEST_MAX = 16 * 1024 * 1024
_NATIVE_EVENT_RESPONSE_MAX = 20 * 1024 * 1024
_NATIVE_EVENT_CANDIDATE_PAGE = 4096


class NativeEventGenerationMoved(RuntimeError):
    pass


def _native_event_lane_enabled(flt: dict) -> bool:
    if "_tool_lane_enabled" in flt:
        return bool(flt["_tool_lane_enabled"])
    who = flt.get("who")
    explicit = who is not None and getattr(who, "include", who) is not None
    return bool(
        surface_policy.speaker_filter_admits(who, "tool")
        and (explicit or flt.get("include_tools", True))
        and common.setting("tools") != "off")


def freeze_tool_lane_filter(flt: dict) -> dict:
    """Freeze whether this query may consume the published event generation."""
    frozen = dict(flt)
    if "_tool_lane_enabled" not in frozen:
        frozen["_tool_lane_enabled"] = _native_event_lane_enabled(frozen)
    return frozen


def _native_excluded_sessions(flt: dict) -> frozenset[str]:
    exact = frozenset(flt.get("_exclude_sessions") or ())
    if flt.get("_native_family_frozen"):
        return exact | frozenset(flt.get("_native_excluded_sessions") or ())
    caller = str(flt.get("exclude_session") or "")
    if not caller:
        return exact
    members, _ = _native_family_members(caller)
    if flt.get("exclude_session_from_turn") is None:
        return exact | (members if flt.get("exclude_family", True)
                        else frozenset({caller}))
    return exact


def _native_caller_event_window(flt: dict) -> dict | None:
    if flt.get("_native_family_frozen"):
        return flt.get("_native_caller_event_window")
    caller = str(flt.get("exclude_session") or "")
    boundary = flt.get("exclude_session_from_turn")
    if not caller or type(boundary) is not int:  # noqa: E721
        return None
    rows = _messages_by_session().get(caller, ())
    marks = []
    limit = (1 << 63) - 1
    for row in rows:
        ts, turn = row.get("ts", 0), row.get("turn", 0)
        if type(ts) is not int or type(turn) is not int:
            raise ValueError("caller timeline contains non-integer marks")
        if not -limit <= ts <= limit or not -limit <= turn <= limit:
            raise ValueError("caller timeline exceeds the native integer range")
        if ts:
            marks.append({"ts": ts, "turn": turn})
    return {"session": caller, "boundary": boundary, "marks": marks}


def freeze_native_event_filter(flt: dict) -> dict:
    if flt.get("_native_family_frozen"):
        return dict(flt)
    frozen = freeze_tool_lane_filter(flt)
    frozen["_native_excluded_sessions"] = _native_excluded_sessions(flt)
    frozen["_native_caller_event_window"] = _native_caller_event_window(flt)
    frozen["_native_family_frozen"] = True
    return frozen


def _native_event_owners(flt: dict) -> list[dict] | None:
    """Resolve the exact session owners admitted by the tool-event lane."""
    if not _native_event_lane_enabled(flt):
        return None
    if flt.get("model"):
        return []
    agent = str(flt.get("agent") or "").lower()
    project = str(flt.get("project") or "").lower()
    chat = str(flt.get("chat") or "").lower()
    excluded_sessions = _native_excluded_sessions(flt)
    try:
        session_rows = _native_session_owner_rows()
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    owners = []
    for first in session_rows:
        session = first["session"]
        if session in excluded_sessions:
            continue
        row_agent = first.get("agent")
        row_project = first.get("project", "")
        if (not isinstance(row_agent, str) or not row_agent
                or not isinstance(row_project, str)):
            return None
        if (chat and not session.lower().startswith(chat)):
            continue
        if agent and agent not in row_agent.lower():
            continue
        if project and project not in row_project.lower():
            continue
        owners.append({
            "agent": row_agent, "session": session, "project": row_project,
        })
    owners.sort(key=lambda owner: (owner["agent"], owner["session"]))
    return owners


def _native_session_owner_rows() -> list[dict]:
    path = common.DATA_DIR / "sessions.jsonl"
    rows = []
    seen = set()
    total = 0
    with common.open_regular_binary(path) as source:
        for line in source:
            total += len(line)
            if total > 1024 * 1024 * 1024 or len(line) > 1024 * 1024:
                raise ValueError("published sessions exceed the native owner limit")
            if not line.strip():
                continue
            row = json.loads(line)
            session = row.get("session") if isinstance(row, dict) else None
            agent = row.get("agent") if isinstance(row, dict) else None
            project = row.get("project", "") if isinstance(row, dict) else None
            if (not isinstance(session, str) or not session
                    or len(session.encode()) > 4096
                    or not isinstance(agent, str) or not agent
                    or len(agent.encode()) > 64
                    or not isinstance(project, str)
                    or session in seen):
                raise ValueError("published sessions contain invalid owners")
            seen.add(session)
            rows.append({"session": session, "agent": agent, "project": project})
            if len(rows) > 2_000_000:
                raise ValueError("published sessions exceed the native owner count limit")
    return rows


def native_event_owner_census_matches() -> bool:
    try:
        owners = _native_session_owner_rows()
        messages = _messages_by_session()
    except (FileNotFoundError, OSError, TypeError, ValueError,
            json.JSONDecodeError):
        return False
    by_session = {owner["session"]: owner for owner in owners}
    if set(by_session) != set(messages):
        return False
    for session, owner in by_session.items():
        rows = messages.get(session)
        if (not rows
                or str(rows[0].get("agent") or "") != owner["agent"]
                or str(rows[0].get("project") or "") != owner["project"]):
            return False
    return True


def _read_native_generation(path: Path, limit: int) -> bytes:
    with common.open_regular_binary(path) as source:
        body = source.read(limit + 1)
    if not body or len(body) > limit:
        raise OSError(f"generation marker is missing or too large: {path}")
    return body


def _capture_direct_snapshot(*, include_events: bool = False) -> dict:
    """Capture one fully verified committed materialized generation."""
    import corpusdb

    root = common.DATA_DIR
    signal_path = root / ".ingest.sig"
    proof_path = root / ".derived_generation.json"
    try:
        signal = corpusdb._read_derived_file(
            signal_path, corpusdb._INGEST_SIGNATURE_MAX_BYTES)
        proof_record = corpusdb._read_derived_file(
            proof_path, corpusdb._DERIVED_PROOF_MAX_BYTES)
        proof = json.loads(proof_record[1])
        files = proof.get("files") if isinstance(proof, dict) else None
        committed = signal[1].decode("utf-8").strip()
        if (not isinstance(proof, dict)
                or proof.get("version") != corpusdb._DERIVED_PROOF_VERSION
                or not committed or proof.get("signature") != committed
                or not isinstance(files, list)
                or len(files) != len(corpusdb._DERIVED_PROOF_NAMES)):
            raise DirectSnapshotError(
                "the committed transcript generation proof is invalid")
        by_name = {
            row.get("name"): row for row in files if isinstance(row, dict)
        }
        if set(by_name) != set(corpusdb._DERIVED_PROOF_NAMES):
            raise DirectSnapshotError(
                "the committed transcript generation census is invalid")
        validated = {}
        for name in corpusdb._DERIVED_PROOF_NAMES:
            problem = corpusdb._validate_derived_file(
                root, by_name[name], validated, routine=False)
            if problem:
                raise DirectSnapshotError(str(problem))
        snapshot = {
            "signal": signal[1],
            "proof": proof_record[1],
            "files": tuple(
                (name, *validated[name])
                for name in corpusdb._DERIVED_PROOF_NAMES),
            "event": None,
        }
        if include_events:
            event_path = root / EVENTS_DIR_NAME / common.EVENT_GENERATION_NAME
            event = corpusdb._read_derived_file(event_path, 4096)
            if not event[1] or not event[1].decode("utf-8"):
                raise DirectSnapshotError(
                    "the committed tool-event generation is invalid")
            snapshot["event"] = event[1]
        if not _direct_snapshot_current(snapshot):
            raise DirectSnapshotMoved(
                "the committed transcript generation moved before scanning")
        return snapshot
    except DirectSnapshotError:
        _kick_derived_repair()
        raise
    except (FileNotFoundError, OSError, RecursionError, UnicodeError,
            TypeError, ValueError, json.JSONDecodeError) as exc:
        _kick_derived_repair()
        raise DirectSnapshotError(
            "the committed transcript generation cannot be verified") from exc


def _direct_snapshot_current(snapshot: object) -> bool:
    import corpusdb

    if not isinstance(snapshot, dict):
        return False
    try:
        signal = corpusdb._read_derived_file(
            common.DATA_DIR / ".ingest.sig",
            corpusdb._INGEST_SIGNATURE_MAX_BYTES)
        proof = corpusdb._read_derived_file(
            common.DATA_DIR / ".derived_generation.json",
            corpusdb._DERIVED_PROOF_MAX_BYTES)
        files = snapshot.get("files")
        if (signal[1] != snapshot.get("signal")
                or proof[1] != snapshot.get("proof")
                or not isinstance(files, tuple)):
            return False
        expected_event = snapshot.get("event")
        if expected_event is not None:
            event = corpusdb._read_derived_file(
                common.DATA_DIR / EVENTS_DIR_NAME / common.EVENT_GENERATION_NAME,
                4096)
            if event[1] != expected_event:
                return False
        for name, identity, windows_state in files:
            path = common.DATA_DIR / name
            if corpusdb._proof_file_identity(path) != identity:
                return False
            if windows_state is not None:
                include_usn = windows_state[1] is not None
                if corpusdb._windows_file_state(
                        path, include_usn=include_usn) != windows_state:
                    return False
        return True
    except (FileNotFoundError, OSError, RecursionError, TypeError, ValueError):
        return False


@contextmanager
def _snapshot_parse_attempt(snapshot: object, current, moved_error):
    if not current(snapshot):
        raise moved_error("the committed generation moved before scanning")
    previous = getattr(_DIRECT_SNAPSHOT_LOCAL, "damage", None)
    damage: dict[str, int] = {}
    _DIRECT_SNAPSHOT_LOCAL.damage = damage
    try:
        yield snapshot
    except (DirectSnapshotError, NativeEventGenerationMoved):
        raise
    except (AttributeError, KeyError, OSError, RecursionError, RuntimeError,
            TypeError, UnicodeError, ValueError, compact.CompactError) as exc:
        if not current(snapshot):
            raise moved_error(
                "the committed generation moved while scanning") from exc
        _kick_derived_repair()
        raise DirectSnapshotError(
            "the committed transcript generation could not be read") from exc
    else:
        if not current(snapshot):
            raise moved_error("the committed generation moved while scanning")
        if damage:
            _kick_derived_repair()
            detail = ", ".join(
                f"{name}: {count}" for name, count in sorted(damage.items()))
            raise DirectSnapshotError(
                f"the committed transcript generation has malformed rows ({detail})")
    finally:
        if previous is None:
            try:
                del _DIRECT_SNAPSHOT_LOCAL.damage
            except AttributeError:
                pass
        else:
            _DIRECT_SNAPSHOT_LOCAL.damage = previous


@contextmanager
def direct_snapshot_attempt(*, include_events: bool = False):
    """Fence one Python scan to a committed generation and its parse health."""
    snapshot = _capture_direct_snapshot(include_events=include_events)
    _freshen()
    with _snapshot_parse_attempt(
            snapshot, _direct_snapshot_current, DirectSnapshotMoved):
        yield snapshot


@contextmanager
def native_prose_snapshot_attempt(snapshot: object):
    """Bind native-lane prose parsing to its already captured generation."""
    with _snapshot_parse_attempt(
            snapshot, native_event_scan_snapshot_current,
            NativeEventGenerationMoved):
        yield snapshot


def native_event_scan_snapshot() -> dict | None:
    """Capture the committed derived and event generations used by both lanes."""
    import hashlib
    import corpusdb

    root = common.DATA_DIR
    signal = root / ".ingest.sig"
    proof_path = root / ".derived_generation.json"
    event_path = root / EVENTS_DIR_NAME / common.EVENT_GENERATION_NAME
    try:
        _signal_identity, signal_body = corpusdb._read_derived_file(
            signal, corpusdb._INGEST_SIGNATURE_MAX_BYTES)
        _proof_identity, proof_body = corpusdb._read_derived_file(
            proof_path, corpusdb._DERIVED_PROOF_MAX_BYTES)
        _event_identity, event_body = corpusdb._read_derived_file(event_path, 4096)
        proof = json.loads(proof_body)
        files = proof.get("files") if isinstance(proof, dict) else None
        committed = signal_body.decode("utf-8").strip()
        if (not isinstance(proof, dict)
                or proof.get("version") != corpusdb._DERIVED_PROOF_VERSION
                or not committed or proof.get("signature") != committed
                or not isinstance(files, list)
                or len(files) != len(corpusdb._DERIVED_PROOF_NAMES)):
            return None
        by_name = {
            row.get("name"): row for row in files if isinstance(row, dict)
        }
        if set(by_name) != set(corpusdb._DERIVED_PROOF_NAMES):
            return None
        validated = {}
        for name in corpusdb._DERIVED_PROOF_NAMES:
            row = by_name[name]
            problem = corpusdb._validate_derived_file(
                root, row, validated, routine=False)
            if problem is not None:
                return None
        signal_after = corpusdb._read_derived_file(
            signal, corpusdb._INGEST_SIGNATURE_MAX_BYTES)
        proof_after = corpusdb._read_derived_file(
            proof_path, corpusdb._DERIVED_PROOF_MAX_BYTES)
        event_after = corpusdb._read_derived_file(event_path, 4096)
        if (signal_body != signal_after[1]
                or proof_body != proof_after[1]
                or event_body != event_after[1]
                or any(corpusdb._proof_file_identity(root / name) != identity
                       for name, identity, _windows_state in (
                           (name, *validated[name])
                           for name in corpusdb._DERIVED_PROOF_NAMES))):
            return None
        event_generation = event_body.decode("utf-8")
        if not event_generation:
            return None
        return {
            "generations": (
                hashlib.sha256(signal_body).hexdigest(), event_generation),
            "signal": signal_body,
            "proof": proof_body,
            "event": event_body,
            "files": tuple(
                (name, *validated[name])
                for name in corpusdb._DERIVED_PROOF_NAMES),
        }
    except (FileNotFoundError, OSError, RecursionError, UnicodeError,
            ValueError, TypeError, json.JSONDecodeError):
        _kick_derived_repair()
        return None


def native_event_scan_snapshot_current(snapshot: object) -> bool:
    import corpusdb

    if not isinstance(snapshot, dict):
        return False
    try:
        signal = corpusdb._read_derived_file(
            common.DATA_DIR / ".ingest.sig",
            corpusdb._INGEST_SIGNATURE_MAX_BYTES)
        proof = corpusdb._read_derived_file(
            common.DATA_DIR / ".derived_generation.json",
            corpusdb._DERIVED_PROOF_MAX_BYTES)
        event = corpusdb._read_derived_file(
            common.DATA_DIR / EVENTS_DIR_NAME / common.EVENT_GENERATION_NAME,
            4096)
        files = snapshot.get("files")
        return bool(
            signal[1] == snapshot.get("signal")
            and proof[1] == snapshot.get("proof")
            and event[1] == snapshot.get("event")
            and isinstance(files, tuple)
            and all(
                corpusdb._proof_file_identity(common.DATA_DIR / name) == identity
                and (windows_state is None or corpusdb._windows_file_state(
                    common.DATA_DIR / name,
                    include_usn=windows_state[1] is not None) == windows_state)
                for name, identity, windows_state in files)
        )
    except (FileNotFoundError, OSError, RecursionError, TypeError, ValueError):
        return False


def _native_scan_unavailable(detail: str) -> dict:
    return {
        "protocol": _NATIVE_EVENT_SCAN_PROTOCOL,
        "state": "unsupported",
        "candidates": [],
        "detail": detail,
        "_native_unavailable": True,
    }


def _native_scan_started_failure(detail: str) -> dict:
    response = _native_scan_unavailable(detail)
    response["_native_started"] = True
    return response


def _native_v10_proof_agents() -> set[str] | None:
    paths = list(common.DATA_DIR.glob(".events_complete.*.json"))
    if not paths:
        return None
    agents = set()
    for path in paths:
        with common.open_regular_binary(path) as source:
            body = source.read(256 * 1024 + 1)
        if len(body) > 256 * 1024:
            return None
        proof = json.loads(body)
        represented = proof.get("agents")
        if (proof.get("version") != 10 or not isinstance(represented, list)
                or len(represented) != 1 or not isinstance(represented[0], str)):
            return None
        agents.add(represented[0])
    return agents


def native_event_scan_preflight(flt: dict) -> bool:
    """Reject known cold-authority shapes before either corpus lane starts."""
    if not _native_event_lane_enabled(flt):
        return False
    if flt.get("model"):
        return True
    if not common.ingest_bin().exists():
        return False
    try:
        proof_agents = _native_v10_proof_agents()
        if proof_agents is None:
            raise ValueError("event proofs need an authority upgrade")
        owners = _native_event_owners(flt)
        _native_caller_event_window(flt)
        if owners is None:
            return False
        agents = sorted({owner["agent"] for owner in owners})
        if any(
                not agent or len(agent) > 64
                or re.fullmatch(r"[A-Za-z0-9_.-]+", agent) is None
                or agent not in proof_agents
                for agent in agents):
            return False
        _read_native_generation(common.INGEST_SIG_PATH, 4096)
        _read_native_generation(
            common.DATA_DIR / EVENTS_DIR_NAME / common.EVENT_GENERATION_NAME,
            4096)
        return True
    except (AttributeError, FileNotFoundError, OSError, TypeError, ValueError,
            json.JSONDecodeError):
        _kick_derived_repair()
        return False


def _native_owner_filter(flt: dict) -> dict | None:
    if not _native_event_lane_enabled(flt):
        return None
    return {
        "agent_contains": str(flt.get("agent") or ""),
        "project_contains": str(flt.get("project") or ""),
        "chat_prefix": str(flt.get("chat") or ""),
        "excluded_sessions": sorted(_native_excluded_sessions(flt)),
    }


def _decode_native_bitmap(value: object, owner_count: int) -> bytes | None:
    if not isinstance(value, str) or len(value) != ((owner_count + 7) // 8) * 2:
        return None
    try:
        body = bytes.fromhex(value)
    except ValueError:
        return None
    unused = len(body) * 8 - owner_count
    if unused and body and body[-1] & (((1 << unused) - 1) << (8 - unused)):
        return None
    return body


def _native_owner_order_digest(owners: list[dict]) -> str:
    import hashlib
    digest = hashlib.sha256()
    for owner in owners:
        for field in ("agent", "session", "project"):
            body = owner[field].encode("utf-8")
            digest.update(len(body).to_bytes(8, "little"))
            digest.update(body)
    return digest.hexdigest()


def _native_cursor_key(cursor: object) -> tuple | None:
    import math

    if not isinstance(cursor, dict):
        return None
    lane = cursor.get("matched")
    upper = cursor.get("upper_score")
    ts = cursor.get("ts")
    session = cursor.get("session")
    ordinal = cursor.get("ordinal")
    if (lane not in ("phrase", "all_terms")
            or type(upper) not in (int, float) or not math.isfinite(upper)
            or not 0 <= upper <= 1 or type(ts) is not int
            or not isinstance(session, str) or not session
            or type(ordinal) is not int or ordinal < 0):
        return None
    return (0 if lane == "phrase" else 1, -float(upper), -ts,
            session, ordinal)


def _valid_native_event_response(
        response: object, query: str,
        owners: list[dict] | None = None,
        *, candidate_limit: int | None = None,
        after: dict | None = None,
) -> bool:
    import math

    if (not isinstance(response, dict)
            or response.get("protocol") != _NATIVE_EVENT_SCAN_PROTOCOL):
        return False
    state = response.get("state")
    if state not in ("ok", "unsupported", "generation_moved", "integrity_error"):
        return False
    candidates = response.get("candidates")
    if not isinstance(candidates, list):
        return False
    scanned = response.get("scanned")
    if (not isinstance(scanned, dict)
            or any(type(scanned.get(field)) is not int or scanned[field] < 0
                   for field in ("sessions", "events", "bytes"))):
        return False
    candidate_fields = ("candidate_sessions", "candidate_events", "candidate_bytes")
    if any(
            field in scanned
            and (type(scanned[field]) is not int
                 or not 0 <= scanned[field] <= scanned[parent])
            for field, parent in zip(
                candidate_fields, ("sessions", "events", "bytes"))):
        return False
    if any(type(scanned.get(field)) is not int or scanned[field] < 0
           for field in ("refined_matches", "conservative_matches")):
        return False
    if state != "ok":
        return (not candidates and response.get("best_omitted") is None
                and response.get("next_after") is None)
    matches = response.get("matches")
    if not isinstance(matches, dict):
        return False
    if (candidate_limit is not None
            and (type(candidate_limit) is not int or candidate_limit < 1
                 or len(candidates) > candidate_limit)):
        return False
    after_key = None if after is None else _native_cursor_key(after)
    if after is not None and after_key is None:
        return False
    omitted = response.get("best_omitted")
    next_after = response.get("next_after")
    omitted_key = None if omitted is None else _native_cursor_key(omitted)
    next_key = None if next_after is None else _native_cursor_key(next_after)
    complete = response.get("envelope_complete")
    if (type(complete) is not bool
            or complete != (omitted is None)
            or (complete and next_after is not None)
            or (not complete and (omitted_key is None or next_key is None))):
        return False
    for field in (
            "tools", "phrase_tools", "all_terms_tools", "all_terms_additions",
            "matched_sessions", "eligible_sessions"):
        if type(matches.get(field)) is not int or matches[field] < 0:
            return False
    owner_count = matches["eligible_sessions"]
    order_digest = matches.get("owner_order_sha256")
    if (not isinstance(order_digest, str) or len(order_digest) != 64
            or any(char not in "0123456789abcdef" for char in order_digest)):
        return False
    matched = _decode_native_bitmap(matches.get("matched_owner_bitmap"), owner_count)
    phrase = _decode_native_bitmap(matches.get("phrase_owner_bitmap"), owner_count)
    if matched is None or phrase is None:
        return False
    phrase_sessions = sum(byte.bit_count() for byte in phrase)
    if (sum(byte.bit_count() for byte in matched) != matches["matched_sessions"]
            or any(phrase_byte & ~matched_byte
                   for phrase_byte, matched_byte in zip(phrase, matched))
            or phrase_sessions > matches["phrase_tools"]
            or matches["phrase_tools"] > matches["tools"]
            or matches["all_terms_additions"] > matches["all_terms_tools"]
            or matches["all_terms_tools"] > matches["tools"]
            or matches["matched_sessions"] > matches["tools"]):
        return False
    if scanned["refined_matches"] + scanned["conservative_matches"] != matches["tools"]:
        return False
    tokens = [token for token in re.split(r"[\s\-_]+", query.strip()) if token]
    single = len(tokens) == 1
    if single:
        if (matches["phrase_tools"] != matches["tools"]
                or matches["all_terms_tools"]
                or matches["all_terms_additions"]):
            return False
    elif (matches["all_terms_tools"] != matches["tools"]
          or matches["phrase_tools"] + matches["all_terms_additions"]
          != matches["tools"]):
        return False
    owner_ordinals = None
    if owners is not None:
        owner_ordinals = {
            (owner["agent"], owner["session"]): ordinal
            for ordinal, owner in enumerate(owners)
        }
        if len(owner_ordinals) != len(owners):
            return False
    ordinals = set()
    candidate_keys = []
    timestamp_limit = (1 << 63) - 1
    for candidate in candidates:
        if not isinstance(candidate, dict):
            return False
        lower, upper = candidate.get("lower_score"), candidate.get("upper_score")
        ordinal = candidate.get("ordinal")
        event_ordinal = candidate.get("event_ordinal")
        occurrences = candidate.get("occurrences")
        refined_score = candidate.get("refined_score")
        lane = candidate.get("matched")
        event = candidate.get("event")
        inline_event = isinstance(event, dict)
        raw_ts = event.get("ts", 0) if inline_event else None
        canonical_ts = (raw_ts if type(raw_ts) is int
                        and -timestamp_limit <= raw_ts <= timestamp_limit else 0)
        if (not isinstance(candidate.get("agent"), str)
                or not isinstance(candidate.get("session"), str)
                or type(ordinal) is not int or not 0 <= ordinal < matches["tools"]
                or ordinal in ordinals
                or type(candidate.get("ts")) is not int
                or (inline_event and candidate["ts"] != canonical_ts)
                or (not inline_event and event is not None)
                or (not inline_event
                    and (type(event_ordinal) is not int or event_ordinal < 0))
                or lane not in ("phrase", "all_terms")
                or type(occurrences) is not int or occurrences < 0
                or type(refined_score) is not bool
                or (lane == "phrase" and occurrences < 1)
                or (lane == "all_terms" and occurrences != 0)
                or (single and lane != "phrase")
                or type(lower) not in (int, float)
                or type(upper) not in (int, float)
                or not math.isfinite(lower) or not math.isfinite(upper)
                or not 0 <= lower <= upper <= 1):
            return False
        cursor_key = _native_cursor_key(candidate)
        if (cursor_key is None
                or (after_key is not None and cursor_key <= after_key)
                or (candidate_keys and cursor_key <= candidate_keys[-1])):
            return False
        candidate_keys.append(cursor_key)
        ordinals.add(ordinal)
        if owner_ordinals is not None:
            owner = (candidate["agent"], candidate["session"])
            owner_ordinal = owner_ordinals.get(owner)
            if (owner_ordinal is None
                    or not matched[owner_ordinal // 8] & (1 << (owner_ordinal % 8))
                    or (lane == "phrase" and not (
                        phrase[owner_ordinal // 8] & (1 << (owner_ordinal % 8))))):
                return False
    phrase_candidates = sum(
        candidate.get("matched") == "phrase" for candidate in candidates)
    all_terms_candidates = len(candidates) - phrase_candidates
    if (phrase_candidates > matches["phrase_tools"]
            or all_terms_candidates > matches["all_terms_additions"]):
        return False
    if complete:
        return True
    return (bool(candidate_keys) and next_key == candidate_keys[-1]
            and omitted_key > next_key)


def _native_empty_event_response(ingest_body: bytes, event_generation: str) -> dict:
    import hashlib
    return {
        "protocol": _NATIVE_EVENT_SCAN_PROTOCOL, "state": "ok", "candidates": [],
        "ingest_generation": hashlib.sha256(ingest_body).hexdigest(),
        "event_generation": event_generation,
        "scanned": {
            "sessions": 0, "events": 0, "bytes": 0,
            "refined_matches": 0, "conservative_matches": 0,
        },
        "best_omitted": None, "next_after": None, "envelope_complete": True,
        "matches": {
            "tools": 0, "phrase_tools": 0, "all_terms_tools": 0,
            "all_terms_additions": 0, "matched_sessions": 0,
            "eligible_sessions": 0, "matched_owner_bitmap": "",
            "phrase_owner_bitmap": "",
            "owner_order_sha256": hashlib.sha256().hexdigest(),
        },
        "_request_bytes": 0, "_native_empty": True, "_owner_order": [],
    }


def native_event_keyword_scan(
        query: str, limit: int, flt: dict, *, now_ms: float | None = None,
        candidate_limit: int = _NATIVE_EVENT_CANDIDATE_PAGE,
        after: dict | None = None,
        expected_generations: tuple[str, str] | None = None,
) -> dict:
    """Run the generation-pinned Rust event matcher or report exact fallback."""
    if (type(candidate_limit) is not int or not 1 <= candidate_limit <= 32_768
            or (after is not None and _native_cursor_key(after) is None)
            or (expected_generations is not None
                and (not isinstance(expected_generations, tuple)
                     or len(expected_generations) != 2
                     or not all(isinstance(value, str) and value
                                for value in expected_generations)))):
        return _native_scan_unavailable("the native event page request is invalid")
    if not _native_event_lane_enabled(flt):
        return _native_scan_unavailable("the tool-event lane is outside the native shape")
    try:
        import hashlib
        ingest_body = _read_native_generation(common.INGEST_SIG_PATH, 4096)
        event_body = _read_native_generation(
            common.DATA_DIR / EVENTS_DIR_NAME / common.EVENT_GENERATION_NAME,
            4096)
        event_generation = event_body.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        return _native_scan_unavailable(str(exc))
    current_ingest_generation = hashlib.sha256(ingest_body).hexdigest()
    if expected_generations is None:
        expected_ingest_generation = current_ingest_generation
        expected_event_generation = event_generation
    else:
        expected_ingest_generation, expected_event_generation = expected_generations
        if (current_ingest_generation != expected_ingest_generation
                or event_generation != expected_event_generation):
            return {
                "protocol": _NATIVE_EVENT_SCAN_PROTOCOL,
                "state": "generation_moved", "candidates": [],
                "detail": "native event generations moved before continuation",
            }
    if flt.get("model"):
        return _native_empty_event_response(ingest_body, event_generation)
    binary = common.ingest_bin()
    if not binary.exists():
        return _native_scan_unavailable("the native event scanner is not installed")
    owner_filter = _native_owner_filter(flt)
    if owner_filter is None:
        return _native_scan_unavailable("the tool-event lane is outside the native shape")
    owners = _native_event_owners(flt)
    if owners is None:
        return _native_scan_unavailable("published event owners are unavailable")
    try:
        owner_ingest = _read_native_generation(common.INGEST_SIG_PATH, 4096)
    except OSError as exc:
        return _native_scan_unavailable(str(exc))
    if owner_ingest != ingest_body:
        return {
            "protocol": _NATIVE_EVENT_SCAN_PROTOCOL,
            "state": "generation_moved", "candidates": [],
            "detail": "transcript generation moved while deriving event owners",
        }
    try:
        caller_event_window = _native_caller_event_window(flt)
    except (TypeError, ValueError) as exc:
        return _native_scan_unavailable(str(exc))
    request = {
        "protocol": _NATIVE_EVENT_SCAN_PROTOCOL,
        "expected_ingest_generation": expected_ingest_generation,
        "expected_event_generation": expected_event_generation,
        "query": query,
        "boundary_context": "cold_prior",
        "now_ms": __import__("time").time() * 1000 if now_ms is None else now_ms,
        "limit": limit,
        "candidate_limit": candidate_limit,
        "after": after,
        "eligible_sessions": [],
        "eligibility": "published_sessions",
        "owner_filter": owner_filter,
        "caller_event_window": caller_event_window,
        "since_ms": flt.get("since_ms"),
        "until_ms": flt.get("until_ms"),
        "score_contract": {
            "half_life_days": 14.0, "who_tool": 0.4,
            "source_tool": 0.55, "meta_min": 0.45,
            "boundary_min": 0.12,
        },
    }
    wire = json.dumps(request, separators=(",", ":")).encode("utf-8")
    if len(wire) > _NATIVE_EVENT_REQUEST_MAX:
        return _native_scan_unavailable("the eligible-session request exceeds 16 MiB")
    started = False
    try:
        import subprocess
        import tempfile
        env = {**os.environ, "AGREP_DATA_DIR": os.fspath(common.DATA_DIR)}
        with tempfile.SpooledTemporaryFile(max_size=1024 * 1024) as output:
            proc = subprocess.Popen(
                [os.fspath(binary), "__fallback-event-scan"],
                stdin=subprocess.PIPE, stdout=output, stderr=subprocess.DEVNULL,
                env=env)
            started = True
            try:
                proc.communicate(input=wire, timeout=3.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                return _native_scan_started_failure(
                    "native event scanner timed out")
            output.seek(0, os.SEEK_END)
            output_size = output.tell()
            if output_size > _NATIVE_EVENT_RESPONSE_MAX:
                return _native_scan_started_failure(
                    "native event scanner response exceeds 20 MiB")
            output.seek(0)
            response_body = output.read()
    except (OSError, subprocess.SubprocessError) as exc:
        failure = _native_scan_started_failure if started else _native_scan_unavailable
        return failure(f"native event scanner failed: {exc}")
    if proc.returncode != 0:
        return _native_scan_started_failure(
            f"native event scanner exited with status {proc.returncode}")
    try:
        response = json.loads(response_body)
    except (UnicodeError, json.JSONDecodeError):
        return _native_scan_started_failure(
            "native event scanner returned malformed JSON")
    if not _valid_native_event_response(
            response, query, candidate_limit=candidate_limit, after=after):
        return _native_scan_started_failure(
            "native event scanner returned an invalid response")
    if (response.get("state") == "ok"
            and (response.get("ingest_generation")
                 != request["expected_ingest_generation"]
                 or response.get("event_generation")
                 != request["expected_event_generation"])):
        return _native_scan_started_failure(
            "native event scanner returned unpinned generations")
    if response["state"] == "ok":
        owners = _native_event_owners(flt)
        if (owners is None
                or len(owners) != response["matches"]["eligible_sessions"]
                or _native_owner_order_digest(owners)
                != response["matches"]["owner_order_sha256"]):
            return {
                "protocol": _NATIVE_EVENT_SCAN_PROTOCOL,
                "state": "generation_moved", "candidates": [],
                "detail": "published owner metadata moved after the native scan",
            }
        try:
            confirmed = _read_native_generation(common.INGEST_SIG_PATH, 4096)
        except OSError:
            confirmed = b""
        if hashlib.sha256(confirmed).hexdigest() != request["expected_ingest_generation"]:
            return {
                "protocol": _NATIVE_EVENT_SCAN_PROTOCOL,
                "state": "generation_moved", "candidates": [],
                "detail": "transcript generation moved after the native scan",
            }
        if not _valid_native_event_response(
                response, query, owners, candidate_limit=candidate_limit,
                after=after):
            return _native_scan_started_failure(
                "native event candidates do not belong to the pinned owners")
        response["_owner_order"] = owners
    response["_native_started"] = True
    if response["state"] == "unsupported":
        scanned = response.get("scanned") or {}
        response["_native_no_payload_work"] = not any(
            scanned.get(field) for field in ("sessions", "events", "bytes"))
    response["_request_bytes"] = len(wire)
    return response


def _native_candidate_events(response: dict) -> dict[int, dict] | None:
    """Resolve compact event references against the pinned SQLite generation."""
    referenced = [
        candidate for candidate in response["candidates"]
        if candidate.get("event") is None
    ]
    if not referenced:
        return {id(candidate): candidate["event"]
                for candidate in response["candidates"]}
    marker = common.DATA_DIR / EVENTS_DIR_NAME / common.EVENT_GENERATION_NAME
    try:
        before = _read_native_generation(marker, 4096).decode("utf-8")
    except (OSError, UnicodeError):
        return None
    if before != response["event_generation"]:
        return None
    grouped: dict[tuple[str, str], list[dict]] = {}
    resolved = {
        id(candidate): candidate["event"]
        for candidate in response["candidates"]
        if candidate.get("event") is not None
    }
    for candidate in referenced:
        grouped.setdefault(
            (candidate["agent"], candidate["session"]), []).append(candidate)
    try:
        payloads = {
            (agent, session): payload
            for agent, session, payload in common.event_blobs_bulk(
                grouped, full=False)
        }
        if set(payloads) != set(grouped):
            return None
        for identity, candidates in grouped.items():
            payload = payloads.get(identity)
            if payload is None:
                return None
            wanted = {candidate["event_ordinal"]: candidate for candidate in candidates}
            for event_ordinal, raw in enumerate(
                    line for line in payload.split(b"\n") if line):
                candidate = wanted.get(event_ordinal)
                if candidate is None:
                    continue
                event = json.loads(raw)
                if not isinstance(event, dict):
                    return None
                raw_ts = event.get("ts", 0)
                timestamp_limit = (1 << 63) - 1
                canonical_ts = (raw_ts if type(raw_ts) is int
                                and -timestamp_limit <= raw_ts <= timestamp_limit else 0)
                if canonical_ts != candidate["ts"]:
                    return None
                resolved[id(candidate)] = event
            if any(id(candidate) not in resolved for candidate in candidates):
                return None
        after = _read_native_generation(marker, 4096).decode("utf-8")
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return resolved if after == before else None


def native_event_candidate_hits(query: str, response: dict) -> list[dict] | None:
    """Hydrate native candidates through the canonical Python event renderer."""
    if not response["candidates"]:
        return []
    tokens = [token for token in re.split(r"[\s\-_]+", query.strip()) if token]
    phrase = _kw_pattern(query)
    candidate_sessions = {
        candidate["session"] for candidate in response["candidates"]
    }
    candidate_events = _native_candidate_events(response)
    if candidate_events is None:
        return None
    messages = _native_messages_for_sessions(
        candidate_sessions, response["ingest_generation"])
    if messages is None:
        return None
    concepts = _session_concept()
    owner_projects = {
        (owner["agent"], owner["session"]): owner["project"]
        for owner in response["_owner_order"]
    }
    timelines: dict[tuple[str, str], list[tuple[int, int]]] = {}
    hits = []
    for candidate in sorted(response["candidates"], key=lambda item: item["ordinal"]):
        session = candidate["session"]
        agent = candidate["agent"]
        rows = messages.get(session)
        project = str(rows[0].get("project") or "") if rows else ""
        if (not rows or str(rows[0].get("agent") or "") != agent
                or owner_projects.get((agent, session)) != project):
            return None
        key = (agent, session)
        marks = timelines.get(key)
        if marks is None:
            try:
                marks = sorted(
                    (int(row.get("ts") or 0), int(row.get("turn") or 0))
                    for row in rows if row.get("ts"))
            except (TypeError, ValueError):
                return None
            timelines[key] = marks
        ts = candidate["ts"]
        index = bisect_right(marks, (ts, float("inf"))) - 1 if ts else -1
        turn = marks[index][1] if index >= 0 else (marks[0][1] if marks else 0)
        tool = common.tool_row_from_event(candidate_events[id(candidate)], ts, turn)
        if tool is None:
            return None
        text = tool["text"]
        entry = {
            "session": session, "turn": turn, "ts": ts, "agent": agent,
            "project": project,
            "concept": concepts.get(session, ""), "model": "",
            "model_source": "tool", "who": "tool", "text": text,
            "low": text.lower(),
            **{field: tool.get(field) for field in _SCAN_EVENT_FIELDS},
        }
        entry["event_kind"] = tool.get("kind", "")
        if tool.get("payload_bounds") is not None:
            entry["payload_bounds"] = tool["payload_bounds"]
        if candidate["matched"] == "phrase":
            match = phrase.search(text) if phrase is not None else None
            if match is None:
                return None
            hit = scan_hit(entry, match.start(), match.end())
        else:
            spans = [common.insensitive_span(text, token, entry["low"])
                     for token in tokens]
            if any(span is None for span in spans):
                return None
            hit = {
                **{field: entry[field] for field in _SCAN_HIT_FIELDS},
                "content_digest": _entry_content_digest(entry),
                **scan_event_columns(entry),
                "snippet": _snip_spans(
                    text, [span for span in spans if span is not None]),
                "matched": "all-terms",
                "_match_span": min(
                    (span for span in spans if span is not None),
                    key=lambda span: (span[0], span[1])),
            }
            identity = common.tool_event_identity(session, turn, ts, text)
            if identity is not None:
                hit["_event_identity"] = identity
        hits.append(hit)
    try:
        import hashlib
        generation = _read_native_generation(common.INGEST_SIG_PATH, 4096)
    except OSError as exc:
        raise NativeEventGenerationMoved(str(exc)) from exc
    if hashlib.sha256(generation).hexdigest() != response.get("ingest_generation"):
        raise NativeEventGenerationMoved(
            "transcript generation moved while hydrating native event hits")
    return hits


# --------------------------------------------------------------------------- endpoints

# Canonical renderers live in common: every engine path must emit byte-identical snippets.
_snip_at = common.snip_at
_snip_spans = common.snip_spans


def _entry_content_digest(entry: dict) -> str:
    if "content_digest" not in entry:
        return compact.content_digest(entry.get("text") or "")
    return compact.require_content_digest(entry["content_digest"])


_SCAN_HIT_FIELDS = ("session", "agent", "project", "concept", "model",
                    "model_source", "turn", "ts", "who")
_SCAN_EVENT_FIELDS = (
    "event_kind", "kind", "name", "input", "output",
    "input_chars", "output_chars", "output_bytes",
    "input_truncated", "output_truncated", "ok", "call_id", "child",
)


def scan_event_columns(entry: dict) -> dict:
    """Canonical event columns a scanned tool entry carries below the seam."""
    return {field: entry[field] for field in _SCAN_EVENT_FIELDS
            if field in entry}


def scan_hit(
        entry: dict, start: int, end: int, *, provenance: bool = True,
) -> dict:
    """One published hit row from a scanned corpus entry.

    Published hits carry canonical provenance; rank-only hits defer it until
    selection. Both spend snippet budget payload-first when bounds are known."""
    hit = {**{field: entry[field] for field in _SCAN_HIT_FIELDS},
           "_match_span": (start, end)}
    if provenance:
        hit.update(content_digest=_entry_content_digest(entry),
                   **scan_event_columns(entry))
    if provenance and entry.get("who") == "tool":
        identity = common.tool_event_identity(
            entry.get("session"), entry.get("turn"), entry.get("ts"),
            entry.get("text"))
        if identity is not None:
            hit["_event_identity"] = identity
    bounds = entry.get("payload_bounds")
    if type(bounds) is tuple:
        try:
            hit["snippet"] = display_policy.payload_snip_at(
                entry["text"], start, end, payload_bounds=bounds)
            hit["payload_bounds"] = bounds
            return hit
        except ValueError:
            # Malformed private bounds must not suppress valid matches;
            # fall back without claiming payload authority.
            pass
    hit["snippet"] = _snip_at(entry["text"], start, end)
    if start <= 80 and end + 80 >= len(entry["text"]):
        hit["_snippet_complete"] = True
    return hit


def _kw_pattern(q: str):
    """Compile a search pattern where the query's words match in order across any run of
    non-alphanumerics in the text - so "cyber filter" also finds "cyber-filter",
    "cyber_filter", "cyberfilter", and a phrase matches across em-dashes/parens/commas in
    prose. [\\W_] keeps underscore in the gap class so identifier hits still work. This is
    what makes keyword search surface every real instance, not just the exact-spacing ones."""
    import re
    toks = [re.escape(t) for t in re.split(r"[\s\-_]+", q.strip()) if t]
    if not toks:
        return None
    return re.compile(r"[\W_]*".join(toks), re.I)


def _keyword_scan_filter(tokens: list[str], flt: dict | None) -> dict | None:
    """Attach reject-safe event literals without mutating caller filters."""
    safe = list(dict.fromkeys(token for token in tokens
                              if token.isascii()))
    if not safe:
        return flt
    scan_flt = dict(flt or {})
    scan_flt["_required_event_literals"] = tuple(sorted(
        safe, key=lambda token: (-len(token), token)))
    return scan_flt


def single_keyword_matches(q: str, flt: dict | None = None):
    """Yield row, entry, and exact span without constructing published hits."""
    import re

    _freshen()
    q = q.strip()
    if not q:
        return
    lowered = q.lower()
    fallback = re.compile(re.escape(q), re.I)
    exceptional = "i" in lowered or "s" in lowered
    scan_flt = dict(_keyword_scan_filter([q], flt) or {})
    scan_flt["_single_keyword_tool_literal"] = lowered
    corpus = _iter_kw_corpus(scan_flt)
    for row_key, entry in enumerate(corpus):
        if "_agrep_tool_event" in entry:
            yield row_key, entry, -1, -1
            continue
        start = entry["low"].find(lowered)
        match = None if start >= 0 else fallback.search(entry["text"])
        if start >= 0:
            span = common.original_span_for_lowered(
                entry["text"], entry["low"], start, start + len(lowered))
        elif match is not None:
            span = match.span()
        else:
            continue
        entry["_agrep_occurrences"] = (
            sum(1 for _match in fallback.finditer(entry["text"]))
            if exceptional else entry["low"].count(lowered)
        )
        yield row_key, entry, span[0], span[1]


def materialize_single_keyword_match(
        entry: dict, q: str, *, provenance: bool = True,
) -> tuple[dict, int, int]:
    """Turn a lightweight tool candidate into its canonical search entry."""
    event = entry.get("_agrep_tool_event")
    if provenance:
        tool = common.tool_row_from_event(event, entry["ts"], entry["turn"])
        text = "" if tool is None else tool["text"]
        payload_bounds = None if tool is None else tool.get("payload_bounds")
    else:
        text, payload_bounds = common.tool_search_record(event)
        tool = None
    if not text:
        raise ValueError("selected tool candidate is not searchable")
    full = {
        **{key: entry[key] for key in (
            "session", "turn", "ts", "agent", "project", "concept",
            "model", "model_source", "who")},
        "text": text, "low": text.lower(),
    }
    if provenance:
        full.update({
            "event_kind": tool.get("kind", ""),
            **{key: tool.get(key) for key in (
                "kind", "name", "input", "output", "input_chars",
                "output_chars", "output_bytes", "input_truncated",
                "output_truncated", "ok", "call_id", "child")},
        })
    else:
        full["_agrep_tool_event"] = event
    if payload_bounds is not None:
        full["payload_bounds"] = payload_bounds
    span = common.insensitive_span(text, q, full["low"])
    if span is None:
        raise ValueError("selected tool candidate lost its literal")
    return full, span[0], span[1]


def keyword_search(q: str, k: int = 300, flt: dict | None = None, *,
                   row_keys: bool = False, terms: bool = False) -> dict:
    """Case-insensitive, separator-flexible JSONL fallback over transcript rows.

    Returns one row per matching message or reply. A single token uses a plain
    substring confirmation; multi-token queries use the shared bridged matcher.

    ``terms`` asks the same pass to collect the any-order, all-token additions
    used by recall/search's fallback tier.  JSONL is the degraded availability
    lane: walking a corpus once for the phrase and again for the terms made a
    five-token recall miss pay ten full-text regex scans per row across its prose
    and tool lanes.  Phrase rows are deliberately absent from ``term_hits``:
    callers prepend the phrase set before merging, so returning only additions
    preserves the public result while avoiding redundant span construction.
    """
    import compact
    import re

    _freshen()
    q = q.strip()
    if not q:
        return {"hits": [], "total": 0, "chats": 0}
    toks = [t for t in re.split(r"[\s\-_]+", q) if t]
    corpus = _iter_kw_corpus(_keyword_scan_filter(toks, flt))
    hits = []
    if len(toks) <= 1:  # single token -> plain substring (fastest)
        ql = q.lower()
        pat = re.compile(re.escape(q), re.I)
        for row_key, e in enumerate(corpus):
            i = e["low"].find(ql)
            match = None if i >= 0 else pat.search(e["text"])
            if i >= 0 or match is not None:
                if match is not None:
                    start, end = match.span()
                else:
                    start, end = common.original_span_for_lowered(
                        e["text"], e["low"], i, i + len(ql))
                hit = scan_hit(e, start, end)
                if row_keys:
                    hit["_agrep_row_key"] = row_key
                hits.append(hit)
    else:  # multi-token -> punctuation-flexible regex (matches cyber-filter / cyber_filter,
           # and phrases spanning em-dashes/parens). re.I: tokens keep query case, corpus is low.
        pat = re.compile(r"[\W_]*".join(re.escape(t) for t in toks), re.I)
        term_hits = []
        term_fields = (
            "session", "agent", "project", "concept", "model",
            "model_source", "turn", "ts", "who",
        )
        # Compile the Python-re.I compatibility seam once per token; lower/find
        # handles ordinary text, while the regex covers exceptional Unicode.
        def term_spec(token: str):
            lowered = token.lower()
            needs_regex = (
                not token.isascii() or "i" in lowered or "s" in lowered)
            return (
                lowered,
                re.compile(re.escape(token), re.I) if needs_regex else None,
            )

        term_specs = [term_spec(token) for token in toks]

        def term_span(e: dict, spec) -> tuple[int, int] | None:
            lowered, fallback = spec
            start = e["low"].find(lowered)
            if start >= 0:
                return common.original_span_for_lowered(
                    e["text"], e["low"], start, start + len(lowered))
            match = fallback.search(e["text"]) if fallback is not None else None
            return match.span() if match is not None else None

        for row_key, e in enumerate(corpus):
            # Every phrase and every all-terms row contains token zero.  This
            # cheap anchor rejects the overwhelmingly common miss before the
            # punctuation-flexible phrase regex walks a long transcript row.
            first_span = term_span(e, term_specs[0])
            if first_span is None:
                continue
            m = pat.search(e["text"])
            if m:
                hit = scan_hit(e, m.start(), m.end())
                if row_keys or terms:
                    hit["_agrep_row_key"] = row_key
                hits.append(hit)
                if terms:
                    # Preserve all-terms corpus order without rendering the
                    # phrase row's snippet and digest a second time.
                    term_hits.append({
                        **{field: e[field] for field in term_fields},
                        "_agrep_row_key": row_key,
                    })
                continue
            if not terms:
                continue
            spans = [first_span]
            for spec in term_specs[1:]:
                span = term_span(e, spec)
                if span is None:
                    break
                spans.append(span)
            else:
                term_hits.append({
                    **{field: e[field] for field in term_fields},
                    "content_digest": _entry_content_digest(e),
                    **scan_event_columns(e),
                    "snippet": _snip_spans(e["text"], spans),
                    "_match_span": min(
                        spans, key=lambda span: (span[0], span[1])),
                    "_agrep_row_key": row_key,
                })
                if e.get("who") == "tool":
                    identity = common.tool_event_identity(
                        e.get("session"), e.get("turn"), e.get("ts"),
                        e.get("text"))
                    if identity is not None:
                        term_hits[-1]["_event_identity"] = identity
    hits.sort(key=lambda h: (h["session"], h["turn"], 0 if h["who"] != "agent" else 1))
    result = {
        "hits": hits[:k], "total": len(hits),
        "chats": len({h["session"] for h in hits}),
    }
    if terms and len(toks) > 1:
        term_hits.sort(
            key=lambda h: (
                h["session"], h["turn"], 0 if h["who"] != "agent" else 1))
        result["term_hits"] = term_hits[:k]
    return result


def resolve_session(q: str) -> list[str]:
    """Resolve a session id query to full ids: exact match wins, else prefix (covers the
    8-char short ids search/around print). The primitive is shared with resume through
    ``common.match_session_ids`` so every verb accepts the same spellings."""
    _freshen()
    servable = _session_index()
    indexed = common.indexed_session_matches(q)
    if indexed is not None:
        return [session for session in indexed if session in servable]
    return common.match_session_ids(servable, q)


def _session_context(db, generation: tuple[str, str], session: str) -> dict | None:
    """Cached structural timeline for bounded windows; never contains message text."""
    key = (str(common.DATA_DIR), *generation, session)
    with _SESSION_CONTEXT_LOCK:
        if key in _SESSION_CONTEXTS:
            context = _SESSION_CONTEXTS[key]
            _SESSION_CONTEXTS.move_to_end(key)
            return context
    import corpusdb
    context = corpusdb.session_context(db, session)
    with _SESSION_CONTEXT_LOCK:
        _SESSION_CONTEXTS[key] = context
        _SESSION_CONTEXTS.move_to_end(key)
        if len(_SESSION_CONTEXTS) > _SESSION_CONTEXT_MAX:
            _SESSION_CONTEXTS.popitem(last=False)
    return context


def _merge_transcript_rows(rows: list[dict]) -> list[dict]:
    """Join prompt/control and reply rows without allowing event-search rows to clobber text."""
    merged: dict[int, dict] = {}
    for o in rows:
        who = o.get("who", "user")
        if who == "tool":
            continue
        turn = int(o.get("turn", 0))
        item = merged.setdefault(turn, {
            "turn": turn,
            "ts": int(o.get("ts", 0) or 0),
            "agent": o.get("agent", ""),
            "project": o.get("project", ""),
            "who": "user",
            "text": "",
            "reply": "",
        })
        if who == "agent":
            if not item["reply"]:
                item["reply"] = o.get("text", "") or ""
            continue
        # There is canonically one initiating row per turn. First-wins is deliberate:
        # a malformed duplicate must not silently replace the real prompt either.
        if not item["text"]:
            text = o.get("text", "") or ""
            item["text"] = text
            item["who"] = who
            item["ts"] = int(o.get("ts", 0) or 0)
            item["agent"] = item["agent"] or o.get("agent", "")
            item["project"] = item["project"] or o.get("project", "")
    return [merged[t] for t in sorted(merged)]


def _legacy_window_source(session: str) -> tuple[dict | None, list[dict]]:
    """Compatibility path for Python/sqlite builds without trigram FTS5."""
    rep = _replies_by_id()
    rows = []
    timeline = []
    for o in sorted(_messages_by_session().get(session, []),
                    key=lambda row: row.get("turn", 0)):
        text = o.get("text", "") or ""
        who = o.get("who", "user")
        turn = int(o.get("turn", 0))
        ts = int(o.get("ts", 0) or 0)
        rows.append({"turn": turn, "ts": ts, "agent": o.get("agent", ""),
                     "project": o.get("project", ""), "who": who, "text": text,
                     "reply": rep.get(o.get("id", ""), "")})
        timeline.append({"turn": turn, "ts": ts})
    if not rows:
        return None, []
    turns = [r["turn"] for r in timeline]
    return {
        "agent": rows[0].get("agent", ""),
        "project": rows[0].get("project", ""),
        "n_turns": len(set(turns)),
        "first_turn": min(turns),
        "last_turn": max(turns),
        "timeline": timeline,
    }, rows


def _nearest_turn(context: dict, requested: int) -> int:
    turns = {row["turn"] for row in context["timeline"]}
    return min(turns, key=lambda turn: (abs(turn - requested), turn))


def _pack_window(session: str, context: dict, center: int, turns: list[dict], *,
                 include_events: bool = True) -> dict:
    summ = _summary_by_session().get(session)
    agent = context.get("agent", "") or (summ.get("agent", "") if summ else "")
    events = (_events_for_turns(agent, session, turns, context["timeline"])
              if include_events and turns and has_events(agent, session) else [])
    packed_turns = []
    records = _reply_records_by_id()
    for row in turns:
        turn = {"turn": row["turn"], "ts": row["ts"], "text": row["text"],
                "who": row["who"], "reply": row["reply"]}
        if row["reply"]:
            reply_id = f"{row.get('agent') or agent}:{session}:{row['turn']}"
            meta = records.get(reply_id, {})
            reply_chars = max(len(row["reply"]), int(meta.get("reply_chars", len(row["reply"]))))
            turn["reply_chars"] = reply_chars
            turn["reply_truncated"] = bool(meta.get("reply_truncated"))
        packed_turns.append(turn)
    return {
        "session": session,
        "agent": agent,
        "project": context.get("project", ""),
        "concept": _session_concept().get(session, ""),
        "title": summ.get("title", "") if summ else "",
        "n_msgs": context["n_turns"],
        "first_turn": context["first_turn"],
        "last_turn": context["last_turn"],
        "center": center,
        "turns": packed_turns,
        "events": events,
    }


def _legacy_windows(requests: list[tuple[str, int, int]], *,
                    include_events: bool = True) -> list[dict]:
    out = []
    for session, requested, radius in requests:
        context, all_rows = _legacy_window_source(session)
        if context is None:
            out.append({"error": f"session {session} not found"})
            continue
        center = _nearest_turn(context, int(requested))
        radius = max(0, int(radius))
        turns = [
            row for row in all_rows
            if center - radius <= row["turn"] <= center + radius
        ]
        out.append(_pack_window(
            session, context, center, turns, include_events=include_events))
    return out


def _verified_legacy_windows(
        requests: list[tuple[str, int, int]]) -> list[dict]:
    for attempt in range(2):
        event_generation = (
            common.DATA_DIR / EVENTS_DIR_NAME / common.EVENT_GENERATION_NAME)
        include_events = event_generation.exists()
        try:
            with direct_snapshot_attempt(include_events=include_events):
                return _legacy_windows(
                    requests, include_events=include_events)
        except DirectSnapshotMoved as exc:
            if attempt == 0:
                continue
            raise DirectSnapshotError(
                "the committed transcript generation kept moving") from exc
    raise DirectSnapshotError(
        "the committed transcript generation could not be read")


def get_windows(requests: list[tuple[str, int, int]]) -> list[dict]:
    """Build several bounded windows with one corpus connection and shared timelines.

    This is the expansion-friendly form of :func:`get_window`. A request is
    ``(session, center, radius)``. Only rows inside each numeric turn range are read;
    compact timelines and sparse event-file checkpoints are reused across requests.
    """
    if not requests:
        return []
    _freshen()
    import corpusdb
    # A BUSY read is transient; one fresh connection is cheaper and safer than
    # declaring the derived index damaged or mixing an unfenced JSONL fallback.
    for attempt in range(2):
        prepared: list[dict | tuple[str, dict, int, list[dict]]] = []
        database_error = None
        try:
            db = corpusdb.connect(allow_stale=True)
        except sqlite3.DatabaseError as exc:
            db = None
            database_error = exc
        if db is None:
            if database_error is not None:
                corpusdb.record_query_database_error(database_error)
            break
        try:
            # Every row and metadata lookup in this batch owns one generation.
            db.execute("BEGIN")
            generation = corpusdb.generation(db)
            for session, requested, radius in requests:
                context = _session_context(db, generation, session)
                if context is None:
                    prepared.append({"error": f"session {session} not found"})
                    continue
                center = _nearest_turn(context, int(requested))
                radius = max(0, int(radius))
                rows = corpusdb.session_rows(
                    db, session, lo=center - radius, hi=center + radius)
                prepared.append((
                    session, context, center, _merge_transcript_rows(rows)))
        except sqlite3.DatabaseError as exc:
            database_error = exc
        finally:
            try:
                db.rollback()
            except sqlite3.DatabaseError as exc:
                database_error = database_error or exc
            try:
                db.close()
            except sqlite3.DatabaseError as exc:
                database_error = database_error or exc
        if database_error is None:
            # Event ranges can be large; parse them after releasing the read lock.
            return [item if isinstance(item, dict) else _pack_window(*item)
                    for item in prepared]
        corpusdb.record_query_database_error(database_error, db)
        if (attempt == 0
                and corpusdb.query_database_error_kind(database_error)
                == "transient"):
            continue
        break
    return _verified_legacy_windows(requests)


def get_window(session: str, center: int, n: int = 4) -> dict:
    """One bounded transcript window with replies and timestamp-attributed events."""
    return get_windows([(session, center, n)])[0]


# --------------------------------------------------------------------------- events

def events_path(agent: str, session: str):
    """Prefer the collision-safe event path while old stores finish migrating."""
    candidates = common.event_path_candidates(agent, session)
    return next((path for path in candidates if path.exists()), candidates[0])


def has_events(agent: str, session: str) -> bool:
    return common.event_exists(agent, session)


_EVENT_CHECKPOINT_STRIDE = 64


def _raw_event_ts(raw: bytes) -> int | None:
    """Read the leading JSON ``ts`` integer without decoding a potentially huge row."""
    marker = b'"ts":'
    start = raw.find(marker, 0, 128)
    if start < 0:
        return None
    start += len(marker)
    while start < len(raw) and raw[start] in b" \t":
        start += 1
    end = start
    if end < len(raw) and raw[end] == 45:  # '-'
        end += 1
    while end < len(raw) and 48 <= raw[end] <= 57:
        end += 1
    if end == start:
        return None
    try:
        return int(raw[start:end])
    except ValueError:
        return None


@functools.lru_cache(maxsize=32)
def _event_checkpoints(
        path_s: str,
        generation: tuple[int, int, int, int, int]) -> tuple[tuple, bool]:
    """Sparse ``(ts, byte_offset, line_no)`` index plus timestamp-order verdict.

    The file stamp is part of the cache key, so a live ingest creates a fresh index.
    Only one tuple per 64 physical lines is retained; even a large event stream
    costs kilobytes of memory rather than caching megabytes of decoded tool output.
    """
    del generation  # cache-key material; path is the only value needed below
    checkpoints = []
    ordered = True
    last_ts: int | None = None
    with common.open_regular_binary(Path(path_s)) as f:
        line_no = 0
        while True:
            offset = f.tell()
            raw = f.readline()
            if not raw:
                break
            if not raw.strip():
                line_no += 1
                continue
            ts = _raw_event_ts(raw)
            if ts is None:
                try:
                    ts = int(json.loads(raw).get("ts", 0) or 0)
                except (json.JSONDecodeError, TypeError, ValueError):
                    ordered = False
                    line_no += 1
                    continue
            if last_ts is not None and ts < last_ts:
                ordered = False
            last_ts = ts
            if line_no % _EVENT_CHECKPOINT_STRIDE == 0:
                checkpoints.append((ts, offset, line_no))
            line_no += 1
    return tuple(checkpoints), ordered


def get_events(agent: str, session: str, start_ts: int | None = None,
               end_ts: int | None = None) -> list[dict]:
    """The session event stream, optionally restricted to ``[start_ts, end_ts)``.

    SQLite rows are generation-pinned BLOBs; legacy JSONL files keep the sparse
    byte-offset path until a complete ingest migrates them.
    """
    current, payload = common.event_store_blob(agent, session)
    if current:
        if payload is None:
            return []
        import io

        out = []
        for i, raw in enumerate(io.BytesIO(payload)):
            if not raw.strip():
                continue
            ts = _raw_event_ts(raw)
            parsed = None
            if ts is None:
                try:
                    parsed = json.loads(raw)
                    ts = int(parsed.get("ts", 0) or 0)
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
            if start_ts is not None and ts < start_ts:
                continue
            if end_ts is not None and ts >= end_ts:
                break
            if parsed is None:
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    continue
            parsed["i"] = i
            out.append(parsed)
        return out
    p = events_path(agent, session)
    if not p.exists():
        return []
    offset = 0
    first_line = 0
    ordered = True
    checkpoint_generation: tuple[int, int, int, int, int] | None = None
    if start_ts is not None:
        try:
            checkpoint_generation = common._event_file_stamp(p)
            checkpoints, ordered = _event_checkpoints(str(p), checkpoint_generation)
        except OSError:
            return []
        if ordered and checkpoints:
            # Step one checkpoint back from the first >= start. Besides providing
            # scan overlap, this preserves all events when several share start_ts.
            pos = bisect_left([row[0] for row in checkpoints], start_ts) - 1
            if pos >= 0:
                _, offset, first_line = checkpoints[pos]
    out = []
    try:
        with common.open_regular_binary(p) as f:
            # If publication raced the checkpoint stat, rescan instead of omitting rows.
            if checkpoint_generation is not None:
                if common._event_fd_stamp(f.fileno()) != checkpoint_generation:
                    offset, first_line, ordered = 0, 0, False
            f.seek(offset)
            for i, raw in enumerate(f, start=first_line):
                if not raw.strip():
                    continue
                ts = _raw_event_ts(raw)
                parsed = None
                if ts is None:
                    try:
                        parsed = json.loads(raw)
                        ts = int(parsed.get("ts", 0) or 0)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        continue
                if start_ts is not None and ts < start_ts:
                    continue
                if end_ts is not None and ts >= end_ts:
                    if ordered:
                        break
                    continue
                if parsed is None:
                    try:
                        parsed = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                o = parsed
                o["i"] = i
                out.append(o)
    except OSError:
        return []
    return out


def _event_intervals(timeline: list[dict], selected: set[int]) -> list[tuple[int, int | None]]:
    """Chronological event ranges owned by selected turns, merged when adjacent."""
    chronological = sorted((int(row.get("ts", 0) or 0), int(row["turn"]))
                           for row in timeline if row.get("ts"))
    ranges = []
    for i, (start, turn) in enumerate(chronological):
        if turn not in selected:
            continue
        end = chronological[i + 1][0] if i + 1 < len(chronological) else None
        # Equal prompt timestamps are indistinguishable. The latest turn at that
        # timestamp owns subsequent events; the earlier interval is correctly empty.
        if end is not None and end <= start:
            continue
        ranges.append((start, end))
    if not ranges:
        return []
    merged: list[list[int | None]] = []
    for start, end in ranges:
        if not merged or (merged[-1][1] is not None and start > merged[-1][1]):
            merged.append([start, end])
            continue
        previous_end = merged[-1][1]
        merged[-1][1] = (None if previous_end is None or end is None
                         else max(previous_end, end))
    return [(int(start), int(end) if end is not None else None) for start, end in merged]


def _events_for_turns(agent: str, session: str, turns: list[dict],
                      timeline: list[dict]) -> list[dict]:
    """Read and attribute only events whose latest chronological prompt is selected."""
    selected = {int(row["turn"]) for row in turns}
    chronological = sorted((int(row.get("ts", 0) or 0), int(row["turn"]))
                           for row in timeline if row.get("ts"))
    if not chronological:
        return []
    events = []
    for start, end in _event_intervals(timeline, selected):
        for event in get_events(agent, session, start, end):
            ts = int(event.get("ts", 0) or 0)
            pos = bisect_right(chronological, (ts, float("inf"))) - 1
            if pos < 0:
                continue
            owner = chronological[pos][1]
            if owner not in selected:
                continue
            raw_input = event.get("input", "")
            raw_output = event.get("output", "")
            input_valid = type(raw_input) is str
            output_valid = type(raw_output) is str
            input_text = raw_input if input_valid else ""
            output = raw_output if output_valid else ""
            max_count = (1 << 63) - 1

            def declared_chars(name: str, fallback: str) -> tuple[int, bool]:
                value = event.get(name)
                valid = (
                    type(value) is int
                    and len(fallback) <= value <= max_count
                )
                return (value if valid else len(fallback), valid)

            input_chars, input_chars_valid = (
                declared_chars("input_chars", input_text)
                if input_valid else (0, False)
            )
            output_chars, output_chars_valid = (
                declared_chars("output_chars", output)
                if output_valid else (0, False)
            )
            marker_present = "output_truncated" in event
            marker = event.get("output_truncated", False)
            marker_valid = type(marker) is bool
            marker_true = marker_valid and marker is True
            inferred_truncated = output_chars > len(output)
            impossible_complete_shape = len(output) > 800
            output_truncated = (
                output_valid
                and (
                    marker_true
                    or inferred_truncated
                    or not marker_valid
                    or impossible_complete_shape
                )
            )
            canonical_truncated_excerpt = (
                output_valid
                and output_chars_valid
                and output_chars > 800
                and len(output) == 801
                and output.endswith("…")
                and (
                    marker_true
                    or (not marker_present and inferred_truncated)
                )
            )
            declared_bytes = event.get("output_bytes")
            declared_bytes = (
                declared_bytes
                if type(declared_bytes) is int
                and 0 <= declared_bytes <= max_count
                else None
            )
            try:
                stored_bytes = (
                    len(output.encode("utf-8")) if output_valid else None)
            except UnicodeEncodeError:
                stored_bytes = None
            output_bytes = None
            if stored_bytes is not None and not output_truncated:
                output_bytes = stored_bytes
            elif (stored_bytes is not None and declared_bytes is not None
                  and output_truncated and canonical_truncated_excerpt):
                retained_chars = len(output)
                retained_bytes = stored_bytes
                retained_chars -= 1
                retained_bytes -= len("…".encode("utf-8"))
                remaining = output_chars - retained_chars
                lower = retained_bytes + remaining
                upper = retained_bytes + 4 * remaining
                if lower <= declared_bytes <= upper:
                    output_bytes = declared_bytes
            if stored_bytes is None and output_valid:
                # A lone surrogate cannot have come from the Rust writer's
                # valid UTF-8 JSON. Keep only honest source-size disclosure;
                # do not pass an unencodable string to rendering.
                output = ""
                output_truncated = True
            input_marker = event.get("input_truncated", False)
            input_truncated = (
                bool(input_marker or input_chars > len(input_text))
                if (
                    input_valid
                    and input_chars_valid
                    and type(input_marker) is bool
                )
                else None
            )
            events.append({
                "turn": owner,
                "ts": ts,
                "kind": (
                    event.get("kind")
                    if type(event.get("kind")) is str else ""
                ),
                "name": (
                    event.get("name")
                    if type(event.get("name")) is str else ""
                ),
                "input": input_text,
                "ok": (
                    event.get("ok")
                    if type(event.get("ok")) is bool else None
                ),
                "output": output,
                "input_chars": input_chars,
                "output_chars": output_chars,
                "output_bytes": output_bytes,
                "input_truncated": input_truncated,
                "output_truncated": output_truncated,
                "_i": event.get("i", 0),
            })
    events.sort(key=lambda event: (event["ts"], event["_i"]))
    for event in events:
        event.pop("_i", None)
    return events


def get_chat(session: str) -> dict:
    """Full detail for one chat: header + per-turn transcript annotated with affect, plus the
    vibe arc if one was built. Reads from the in-process caches (messages-by-session, affect,
    replies), so each open is a dict lookup over this session's rows rather than a scan of
    messages.jsonl (+ emotions + replies) on every request."""
    _freshen()
    emo = _emotions_by_id()
    rep = _reply_records_by_id()
    rows = _messages_by_session().get(session, [])

    turns = []
    agent = project = ""
    for o in rows:
        agent = agent or o.get("agent", "")
        project = project or o.get("project", "")
        e = emo.get(o["id"], {})
        rage = float(e.get("rage_raw", 0.0))
        hype = float(e.get("hype_raw", 0.0))
        txt = o.get("text", "")
        reply = rep.get(o["id"], {})
        turns.append({
            "turn": o.get("turn", 0),
            "ts": o.get("ts", 0),
            "text": txt,
            "rage": round(rage, 4),
            "hype": round(hype, 4),
            "hot": rage > HOT_T,
            "top": e.get("top", []),
            "model": o.get("model", ""),
            "reply": reply.get("reply", ""),
            "reply_chars": reply.get("reply_chars", 0),
            "reply_truncated": bool(reply.get("reply_truncated")),
            "recap": o.get("who") == "recap",
        })
    turns.sort(key=lambda t: t["turn"])

    models = _models_for_rows(turns)
    session_model = models[0]["name"] if models else ""

    summ = _summary_by_session().get(session)
    hot_n = sum(1 for t in turns if t["hot"])
    return {
        "session": session,
        "agent": agent or (summ.get("agent", "") if summ else ""),
        "project": project or (summ.get("cwd_project", "") if summ else ""),
        "model": session_model,
        "models": models,
        "first_ts": min((t["ts"] for t in turns if t["ts"]), default=0),
        "last_ts": max((t["ts"] for t in turns if t["ts"]), default=0),
        "concept": _session_concept().get(session, ""),
        "title": summ.get("title", "") if summ else "",
        "summary": summ.get("summary", "") if summ else "",
        "tags": summ.get("tags", []) if summ else [],
        "n_msgs": len(turns),
        "hot_n": hot_n,
        "hot_pct": round(hot_n / max(1, len(turns)) * 100, 1),
        "has_events": has_events(agent or (summ.get("agent", "") if summ else ""), session),
        "turns": turns,
        "vibe": get_vibe(session),
    }


def get_vibe(session: str) -> dict | None:
    """Return a published vibe trace only when its file proves the requested identity."""
    _freshen()
    if (not session or session in (".", "..") or "\0" in session
            or "/" in session or "\\" in session or Path(session).name != session
            or session not in _vibe_index()):
        return None
    root = (common.DATA_DIR / "vibe").resolve()
    try:
        candidate = root / f"{session}.json"
        if candidate.is_symlink():
            return None
        path = candidate.resolve(strict=True)
        if path.parent != root:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("session") != session:
        return None
    return payload
