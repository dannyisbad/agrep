"""`agrep why <reference> [--json]` - explain why a chat is, or is not, indexed.

Read-only by construction: never indexes, writes the data dir or wakes the daemon. "Searchable"
is what `agrep search` would serve, decided by corpusdb's own reader predicates. A reference
resolves like `agrep resume` after a path lane; every evidence line names the file it came from.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import common
import compact
import explore
import resume
import surface_policy as surface

VERSION = 1
_SOURCE_TIMEOUT_S = 60.0
_SOURCE_OUTPUT_MAX_BYTES = 64 * 1024 * 1024
_EVIDENCE_LINES = 4
_CANDIDATE_LINES = 20
_INDEXED = ("indexed", "indexed-under-alias", "indexed-as-side-chat")
_UNPROVABLE = ("ambiguous", "not-provable")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_HEX = re.compile(r"[0-9a-f]{6,}", re.I)
_SKIP_ORDER = ("wrapper", "meta", "sidechain", "non_message", "non_human", "empty_text",
               "replay", "unreferenced", "throwaway")
_TRANSCRIPT_EXTENSIONS = frozenset({".jsonl", ".json", ".db", ".sqlite", ".sqlite3", ".vscdb"})


# --------------------------------------------------------------------------- evidence readers

def source_projection() -> tuple[dict | None, str | None]:
    """`agrep-rs why-source` payload, or (None, why it cannot be trusted)."""
    binp = common.ingest_bin()
    if not binp.exists():
        return None, f"ingest binary is unavailable at {binp}"
    kw: dict = {"capture_output": True, "text": True, "encoding": "utf-8",
                "errors": "replace", "timeout": _SOURCE_TIMEOUT_S}
    if common.WIN:
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run([str(binp), "why-source"], **kw)
    except subprocess.TimeoutExpired:
        return None, f"source census timed out after {_SOURCE_TIMEOUT_S:.0f}s"
    except OSError as exc:
        return None, f"cannot launch the source census: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        tail = f": {detail[-1][:200]}" if detail else ""
        return None, f"source census exited {result.returncode}{tail}"
    stdout = result.stdout or ""
    if len(stdout.encode("utf-8")) > _SOURCE_OUTPUT_MAX_BYTES:
        return None, f"source census output exceeded {_SOURCE_OUTPUT_MAX_BYTES} bytes"
    try:
        payload = json.loads(stdout)
    except ValueError:
        return None, "source census output is not JSON"
    if not isinstance(payload, dict) or payload.get("version") != VERSION:
        return None, "source census protocol version mismatch"
    for key in ("adapters", "detected", "sources", "issues", "tokens"):
        if not isinstance(payload.get(key), list):
            return None, f"source census payload lacks {key}"
    for key in ("cache", "intake"):
        if not isinstance(payload.get(key), dict):
            return None, f"source census payload lacks {key}"
    return payload, None


def _index_rows() -> tuple[list[dict], bool, int]:
    """sessions.jsonl rows (newest first), whether the file exists, corrupt lines skipped."""
    explore._session_index_read.cache_clear()
    present = (common.DATA_DIR / "sessions.jsonl").exists()
    rows, skipped = explore._session_index_read()
    ordered = sorted(rows.values(), key=lambda row: row.get("last_ts", 0), reverse=True)
    return ordered, present, skipped


def _ingest_sig() -> dict:
    try:
        stat = common.INGEST_SIG_PATH.stat()
    except OSError:
        return {"present": False}
    return {"present": True, "mtime_ms": stat.st_mtime_ns // 1_000_000,
            "total": common.committed_message_total()}


def _reader_lane(meta: dict | None) -> tuple[str, str | None]:
    """The lane the interactive search reader takes for the published corpus.db whose meta table
    reads `meta` (None: no database), from the reader's own read-only facts and predicates."""
    import corpusdb
    import indexd_runtime
    if corpusdb._query_failure_matches_current() or corpusdb.query_rebuild_required():
        return "scan", "marked for rebuild after a query failure"
    if not corpusdb._trigram_ok():
        return "scan", "this sqlite lacks trigram FTS5"
    if meta is None:
        pending = indexd_runtime.search_index_build_pending()
        return "scan", "not built yet, build queued" if pending else "missing"
    ownership = corpusdb._derived_write_ownership(for_write=True)
    if ownership.replace_retained_db:
        expected = ownership.retained_build_id
        try:
            identity = corpusdb._optional_sqlite_identity(corpusdb.DB_PATH)
        except OSError:
            identity = None
        if identity != ownership.retained_reader_identity:
            return "scan", "the retained publication moved"
    elif not ownership.writable:
        expected = None
    else:
        try:
            expected = indexd_runtime.derived_writer_build_id(require_binary=True)
        except OSError as exc:
            return "scan", f"writer identity unavailable ({exc})"
    if not corpusdb._publication_compatible(meta, expected):
        if meta.get("schema") != corpusdb._SCHEMA:
            return "scan", f"schema {meta.get('schema')}, this build reads {corpusdb._SCHEMA}"
        return "scan", "published by another agrep build"
    lane = corpusdb.interactive_snapshot_lane(
        meta.get("stamp"), corpusdb._stamp(),
        build_pending=indexd_runtime.search_index_build_pending)
    return lane, "stamp behind the published sources, rebuild queued" if lane == "scan" else None


def _stored_rows(db: sqlite3.Connection, session: str, facts: dict) -> list[tuple]:
    """Fill `facts` with what corpus.db stores for `session`; returns the indexed rows."""
    import corpusdb
    rows = db.execute(f"SELECT {corpusdb._ROW_COLS} FROM msgs WHERE session=?",
                      (session,)).fetchall()
    sig = db.execute("SELECT sig FROM session_sig WHERE session=?", (session,)).fetchone()
    family = db.execute("SELECT root, side FROM session_family WHERE session=?",
                        (session,)).fetchone()
    facts.update({"rows": len(rows), "session_sig": sig is not None,
                  "root": family[0] if family else None,
                  "side": bool(family[1]) if family else None})
    return [tuple(row) for row in rows]


def _row_diff(stored: list[tuple], published: list[tuple]) -> dict:
    """Stored rows against the rows the sources publish, as multisets ignoring the concept column
    the way corpusdb's incremental diff pairs them; `who == tool` rows come from the event store."""
    def shape(row: tuple) -> tuple:
        return row[:5] + row[6:]

    def split(counter: Counter) -> dict:
        tool = sum(n for row, n in counter.items() if row[7] == "tool")
        return {"text": sum(counter.values()) - tool, "tool": tool}

    kept, want = Counter(map(shape, stored)), Counter(map(shape, published))
    missing, extra = want - kept, kept - want
    relabelled = bool(stored and published) and (
        {r[5] for r in stored} != {r[5] for r in published})
    return {"proof": "rows", "current": not missing and not extra,
            "published_rows": len(published), "published": split(want),
            "missing": split(missing), "extra": split(extra),
            "concept_differs": relabelled, "tools": common.setting("tools")}


def _corpus_facts(session: str) -> dict:
    """Which engine search serves `session` from - corpus.db or the direct scan of messages.jsonl -
    decided by the interactive reader's own predicates, and whether that copy is current."""
    import corpusdb
    import events
    # The scan validates event payloads; damage found there must not schedule indexd from `why`.
    events.set_event_repair_callback(lambda: False)
    path = common.DATA_DIR / "corpus.db"
    db, meta = None, None
    try:
        if path.exists():
            try:
                uri = Path(os.path.abspath(os.fspath(path))).as_uri() + "?mode=ro"
                db = sqlite3.connect(uri, uri=True, timeout=1.0)
                meta = dict(db.execute("SELECT key, value FROM meta "
                                       "WHERE key IN ('schema', 'build_id', 'stamp')"))
            except (sqlite3.Error, OSError, ValueError) as exc:
                return {"state": "unreadable", "reason": str(exc)}
        lane, scan_reason = _reader_lane(meta)
        facts: dict = {"state": "ok" if meta is not None else "missing",
                       "served": "messages.jsonl" if lane == "scan" else "corpus.db",
                       "scan_reason": scan_reason, "stamp_current": lane == "current"}
        stored: list[tuple] = []
        if meta is not None and meta.get("schema") == corpusdb._SCHEMA:
            try:
                stored = _stored_rows(db, session, facts)
            except sqlite3.Error as exc:
                return {"state": "unreadable", "reason": str(exc)}
        if lane == "current":
            facts.update({"proof": "stamp", "current": True})
            return facts
        try:
            published = corpusdb._scan(only={session}).get(session, [])
        except (OSError, RuntimeError, ValueError) as exc:
            facts.update({"proof": None, "current": None, "reason": str(exc)})
            return facts
        if lane == "scan":
            facts.update({"proof": "scan", "current": bool(published),
                          "published_rows": len(published)})
            return facts
        facts.update(_row_diff(stored, published))
        return facts
    finally:
        if db is not None:
            db.close()


# --------------------------------------------------------------------------- formatting

def _when(ms: int | None) -> str:
    if not ms:
        return "unknown time"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ms / 1000))


def _key_mtime_ms(key: str | None) -> int | None:
    parts = str(key or "").split(":")
    if len(parts) == 3 and parts[0] == "s" and parts[1].isdigit():
        return int(parts[1])
    return None


def _skips_text(skips: dict | None) -> str:
    skips = skips if isinstance(skips, dict) else {}
    ordered = [k for k in _SKIP_ORDER if skips.get(k)] + sorted(
        k for k in skips if k not in _SKIP_ORDER and skips.get(k))
    return " ".join(f"{k}:{skips[k]}" for k in ordered) or "none"


def _quote(text: object, limit: int = 60) -> str:
    one = common.one_line(str(text or ""))
    return "«" + (one[: limit - 1] + "…" if len(one) > limit else one) + "»"


def _short(session: object) -> str:
    return str(session or "")[:8]


def _index_line(row: dict) -> str:
    agent = row.get("agent") or "?"
    text = f"sessions.jsonl: {agent} chat {row.get('session')}, {_plural(int(row.get('n') or 0), 'message')}"
    if row.get("project"):
        text += f", project {row['project']}"
    if row.get("first_text"):
        text += f", first line {_quote(row['first_text'])}"
    if row.get("alias"):
        text += f", alias {row['alias']}"
    if row.get("parent"):
        text += f", parent {row['parent']}"
    return text


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _intake_line(entry: dict) -> str:
    text = (f"intake_stats.json: seen {entry.get('seen', 0)}, rows {entry.get('rows', 0)}, "
            f"skips {_skips_text(entry.get('skips'))}")
    if entry.get("errors"):
        text += f", errors {entry['errors']}"
        if entry.get("first_error"):
            text += f" (first: {_quote(entry['first_error'], 80)})"
    fresh = entry.get("fresh")
    text += (", source unchanged since that parse" if fresh is True
             else ", source changed since that parse" if fresh is False
             else "")
    return text


def _candidate_from_row(row: dict, *, via: str) -> dict:
    return {"agent": row.get("agent"), "session": row.get("session"),
            "project": row.get("project"), "first_text": row.get("first_text"),
            "alias": row.get("alias"), "parent": row.get("parent"), "via": via}


def _candidate_label(candidate: dict) -> str:
    if candidate.get("path"):
        text = f"{candidate.get('agent') or '?'} file {candidate['path']}"
        if candidate.get("session"):
            text += f" (session {candidate['session']})"
        return text
    text = f"{candidate.get('agent') or '?'} · {candidate.get('project') or '-'}  {candidate.get('session')}"
    if candidate.get("alias"):
        text += f" (alias {candidate['alias']})"
    if candidate.get("first_text"):
        text += f"  {_quote(candidate['first_text'])}"
    return text


# --------------------------------------------------------------------------- verdicts

class _Context:
    def __init__(self, reference: str) -> None:
        self.reference = reference
        self.rows, self.index_present, self.index_skipped = _index_rows()
        self.payload, self.census_error = source_projection()
        self.sig = _ingest_sig()
        self._real: dict[str, str] = {}
        self._by_real: dict[str, dict] | None = None

    def real(self, path: object) -> str:
        text = str(path or "")
        if text not in self._real:
            self._real[text] = os.path.realpath(text) if text else ""
        return self._real[text]

    @property
    def entries_by_real(self) -> dict[str, dict]:
        """Discovered files and issues keyed by resolved path: a symlink alias finds its census entry."""
        if self._by_real is None:
            self._by_real = {}
            for entry in self.sources + self.issues:
                if entry.get("path"):
                    self._by_real.setdefault(self.real(entry["path"]), entry)
        return self._by_real

    @property
    def sources(self) -> list[dict]:
        return self.payload["sources"] if self.payload else []

    @property
    def issues(self) -> list[dict]:
        return self.payload["issues"] if self.payload else []

    @property
    def cache_sessions(self) -> list[dict]:
        return list(self.payload["cache"].get("sessions") or []) if self.payload else []

    @property
    def intake_files(self) -> list[dict]:
        return list(self.payload["intake"].get("files") or []) if self.payload else []

    def row_for(self, session: str) -> dict | None:
        return next((r for r in self.rows if r.get("session") == session), None)

    def display(self, path: object) -> str:
        """A path with the Rust-reported home shortened to `~`, for evidence lines."""
        text = str(path or "")
        home = str((self.payload or {}).get("home") or "")
        return "~" + text[len(home):] if home and _under(text, home) else text

    def fingerprint(self, agent: str) -> str | None:
        return next((a.get("fingerprint") for a in (self.payload or {}).get("adapters", [])
                     if a.get("name") == agent), None)

    def census_line(self) -> str:
        if self.payload is None:
            return f"store census: unavailable ({self.census_error})"
        agents = sorted({s.get("agent") for s in self.sources if s.get("agent")})
        return (f"store census: {len(self.sources)} discovered file(s)"
                + (f" across {', '.join(agents)}" if agents else "")
                + (f", {len(self.issues)} source issue(s)" if self.issues else ""))

    def sig_line(self) -> str:
        if not self.sig.get("present"):
            return ".ingest.sig: absent, nothing has been indexed yet"
        total = self.sig.get("total")
        return (f".ingest.sig: last index published {_when(self.sig.get('mtime_ms'))}"
                + (f", {_plural(total, 'message')}" if isinstance(total, int) else ""))


def _report(ctx: _Context, verdict: str, summary: str, lines: list[str], *,
            facts: dict | None = None, candidates: list[dict] | None = None,
            next_action: str | None = None) -> dict:
    code = 0 if verdict in _INDEXED else 2 if verdict in _UNPROVABLE else 1
    evidence = {"lines": lines}
    evidence.update(facts or {})
    return {"version": VERSION, "reference": ctx.reference, "verdict": verdict,
            "exit": code, "summary": summary, "candidates": candidates or [],
            "evidence": evidence, "next_action": next_action}


def _ambiguous(ctx: _Context, candidates: list[dict], what: str, origin: str) -> dict:
    return _report(
        ctx, "ambiguous",
        f"ambiguous: '{ctx.reference}' matches {len(candidates)} {what}",
        [f"{origin}: {len(candidates)} {what} match '{ctx.reference}'; pass a full id, "
         "a @handle from a search hit, or the transcript path"],
        candidates=candidates)


def _judge_rows(ctx: _Context, rows: list[dict], via: str) -> dict:
    if len(rows) > 1:
        return _ambiguous(ctx, [_candidate_from_row(r, via=via) for r in rows], "chats",
                          "sessions.jsonl")
    return _judge_indexed(ctx, rows[0], via)


def _judge_indexed(ctx: _Context, row: dict, via: str) -> dict:
    session = str(row.get("session") or "")
    agent = row.get("agent") or "?"
    count = _plural(int(row.get("n") or 0), "message")
    corpus = _corpus_facts(session)
    claims = [c for c in ctx.cache_sessions if c.get("session") == session]
    paths = [c["path"] for c in claims if c.get("path")]
    entries = [e for e in ctx.intake_files if e.get("path") in paths and not e.get("session")]
    entries += [e for e in ctx.intake_files if e.get("session") == session]
    stale = [e for e in entries if e.get("fresh") is False]
    issue = next((i for i in (_issue_covering(ctx, p) for p in paths) if i), None)
    facts = {"index_row": _candidate_from_row(row, via=via), "corpus": corpus,
             "sources": paths, "intake": entries, "issue": issue}
    lines = [_index_line(row), _corpus_line(corpus)]
    if ctx.payload is None:
        lines.append(f"store census: unavailable ({ctx.census_error}); freshness unverified")
    elif paths:
        lines.append("parse cache: " + ", ".join(ctx.display(p) for p in paths))
        lines.extend(_intake_line(e) for e in entries[:1])
    elif entries and entries[0].get("session"):
        lines.append(f"intake_stats.json: conversation keyed by store token in "
                     f"{ctx.display(entries[0].get('path'))}")
        lines.append(_intake_line(entries[0]))
    elif ctx.fingerprint(agent) == "always":
        lines.append(f"parse cache: {agent} reparses its whole store on every index, "
                     "so no single file is claimed")
    else:
        state = ctx.payload["cache"].get("state")
        detail = ctx.payload["cache"].get("reason") or state
        lines.append("parse cache: " + (f"no file claims this session ({state})"
                                        if state == "ok" else f"{detail}; freshness unverified"))

    if issue:
        lines.insert(1, _issue_line(ctx, issue))
        lines.insert(2, f"sessions.jsonl still serves the last good parse ({count})")
        return _report(
            ctx, "source-unreadable",
            f"not fully indexed: agrep cannot read the transcript of {agent} chat "
            f"{_short(session)} any more",
            lines, facts=facts, next_action=_READABLE_ACTION)
    if stale:
        changed = stale[0]
        then, now = changed.get("key"), changed.get("current_key")
        lines.insert(1, f"intake_stats.json: {ctx.display(changed.get('path'))} parsed at "
                        f"{_when(_key_mtime_ms(then))} ({then}), now {_when(_key_mtime_ms(now))} ({now})")
        lines.insert(2, ctx.sig_line())
        return _report(
            ctx, "written-after-last-index",
            f"not fully indexed: the transcript of {agent} chat {_short(session)} "
            "was written after the last index",
            lines, facts=facts, next_action="agrep index")
    if corpus.get("state") == "unreadable":
        lines.append(ctx.sig_line())
        return _report(
            ctx, "not-provable",
            f"unprovable: the search database cannot be read, so whether {agent} chat "
            f"{_short(session)} is searchable is unknown",
            lines, facts=facts, next_action="agrep doctor")
    if corpus.get("current") is False or (corpus["served"] == "corpus.db" and not corpus["rows"]):
        lines.append(ctx.sig_line())
        return _report(
            ctx, "corpus-behind-transcripts", _corpus_behind_summary(corpus, agent, session),
            lines, facts=facts, next_action="agrep index")
    if corpus.get("current") is None:
        lines.append(ctx.sig_line())
        return _report(
            ctx, "not-provable",
            f"unprovable: the search database is behind the published sources and {agent} "
            f"chat {_short(session)} could not be compared",
            lines, facts=facts, next_action="agrep index")
    if via == "alias" or (via == "path" and row.get("alias")):
        verdict, summary = "indexed-under-alias", (
            f"indexed under an alias: {agent} chat {row.get('alias')} is stored as "
            f"session {session} ({count})")
    elif row.get("parent") or (corpus.get("side") and corpus.get("root") != session):
        verdict, summary = "indexed-as-side-chat", (
            f"indexed as a side chat: {agent} chat {_short(session)} belongs to "
            f"{row.get('parent') or corpus.get('root')} ({count})")
    else:
        verdict, summary = "indexed", f"indexed: {agent} chat {session} is searchable ({count})"
    return _report(ctx, verdict, summary, lines, facts=facts)


def _diff_text(corpus: dict) -> str:
    """What differs between the stored rows and the published ones, each named by its source."""
    published, missing, extra = corpus["published"], corpus["missing"], corpus["extra"]
    tools = f"settings.json tools={corpus['tools']}"
    if corpus["current"]:
        source = ("messages.jsonl and the event store publish" if published["tool"]
                  else "messages.jsonl publishes")
        text = f"stored rows match the {_plural(corpus['published_rows'], 'row')} {source}"
        if corpus["concept_differs"]:
            text += " (only the concept label from session_concepts.jsonl differs)"
        return text
    parts = []
    if missing["text"]:
        parts.append(f"{_plural(missing['text'], 'row')} messages.jsonl publishes not stored")
    if missing["tool"]:
        parts.append(f"{_plural(missing['tool'], 'tool row')} the event store publishes "
                     f"({tools}) not stored")
    if extra["text"]:
        parts.append(f"{_plural(extra['text'], 'stored row')} messages.jsonl no longer publishes")
    if extra["tool"]:
        parts.append(f"{_plural(extra['tool'], 'stored tool row')} the event store no longer "
                     f"publishes ({tools})")
    return "; ".join(parts)


def _corpus_line(corpus: dict) -> str:
    """The corpus.db evidence line: the engine search serves and the proof of currency used."""
    if corpus.get("state") == "unreadable":
        return f"corpus.db: unreadable ({corpus['reason']})"
    rows = corpus.get("rows")
    if corpus["served"] == "messages.jsonl":
        held = f"{_plural(rows, 'row')}, " if rows is not None else ""
        text = f"corpus.db: {held}{corpus['scan_reason']}; search scans messages.jsonl directly"
        if corpus["proof"] == "scan":
            return text + f" ({_plural(corpus['published_rows'], 'row')} published for this chat)"
        return text + f", unverifiable ({corpus['reason']})"
    sig = "present" if corpus["session_sig"] else "absent"
    if corpus["proof"] == "rows":
        detail = _diff_text(corpus) + ", stamp behind the published sources"
    elif corpus["proof"] is None:
        detail = (f"session_sig {sig}, stamp behind the published sources, "
                  f"messages.jsonl unverifiable ({corpus['reason']})")
    else:
        detail = f"session_sig {sig}"
    family = (f", family root {corpus['root']}" + (" (side chat)" if corpus["side"] else "")
              if corpus.get("root") else "")
    return f"corpus.db: {_plural(rows, 'row')}, {detail}{family}"


def _corpus_behind_summary(corpus: dict, agent: str, session: str) -> str:
    chat = f"{agent} chat {_short(session)}"
    if corpus["served"] == "messages.jsonl" or corpus.get("published_rows") == 0:
        return f"not searchable yet: transcripts list {chat} but messages.jsonl publishes no rows for it"
    if not corpus["rows"]:
        return f"not searchable yet: transcripts list {chat} but the search database does not hold it"
    missing, extra = corpus["missing"], corpus["extra"]
    gone, kept = sum(missing.values()), sum(extra.values())
    if gone and not kept:
        source = "messages.jsonl" if missing["text"] else "the event store"
        return (f"not fully searchable: the search database holds an older copy of {chat} than "
                f"{source} publishes ({_plural(gone, 'row')} not stored)")
    if kept and not gone:
        source = ("messages.jsonl" if extra["text"]
                  else f"the event store (settings.json tools={corpus['tools']})")
        return (f"not current: the search database still holds {_plural(kept, 'row')} of {chat} "
                f"that {source} no longer publishes")
    return (f"not fully searchable: the search database holds a different copy of {chat} than "
            f"the sources publish ({_plural(gone, 'row')} not stored, {_plural(kept, 'stored row')} "
            "no longer published)")


_READABLE_ACTION = "make the file readable, then agrep index"


def _issue_covering(ctx: _Context, path: str) -> dict | None:
    """The first live or durable issue naming `path` or a directory above it, symlinks resolved."""
    real = ctx.real(path)
    return next((i for i in ctx.issues if i.get("path") and (
        _under(path, i["path"]) or _under(real, ctx.real(i["path"])))), None)


def _issue_line(ctx: _Context, issue: dict) -> str:
    origin = ".source-health.json" if issue.get("durable") else "store census"
    return (f"{origin}: {issue.get('kind')} on {ctx.display(issue.get('path'))} "
            f"- {issue.get('reason')}")


def _judge_source(ctx: _Context, agent: str | None, path: str, session: str | None = None) -> dict:
    """A discovered file (or one tallied conversation in it) that no index row answered."""
    issue = _issue_covering(ctx, path)
    claims = [c for c in ctx.cache_sessions if c.get("path") == path
              and (session is None or c.get("session") == session)]
    indexed = [r for r in (ctx.row_for(c["session"]) for c in claims) if r]
    if indexed:
        return _judge_rows(ctx, indexed, "path")
    entries = [e for e in ctx.intake_files if e.get("path") == path
               and (session is None or e.get("session") == session)]
    label = (f"{agent or 'the store'} file {ctx.display(path)}"
             + (f" conversation {session}" if session else ""))
    facts = {"path": path, "agent": agent, "session": session, "issue": issue,
             "intake": entries, "cache_claims": claims}
    if issue:
        return _report(
            ctx, "source-unreadable",
            f"not indexed: agrep cannot read {label}",
            [_issue_line(ctx, issue), ctx.sig_line()], facts=facts, next_action=_READABLE_ACTION)
    if claims:
        return _report(
            ctx, "not-provable",
            f"unprovable: the parse cache attributes {label} to a session the index does not list",
            ["parse cache: session " + ", ".join(c["session"] for c in claims),
             f"sessions.jsonl: {len(ctx.rows)} chats, none with that id", ctx.sig_line()],
            facts=facts, next_action="agrep index")
    if session is None and len(entries) > 1:
        candidates = [{"agent": e.get("agent"), "path": path, "session": e.get("session"),
                       "rows": e.get("rows")} for e in entries[:_CANDIDATE_LINES]]
        return _ambiguous(ctx, candidates, "conversations in that database", "intake_stats.json")
    entry = entries[0] if entries else None
    if entry is None:
        if not ctx.sig.get("present"):
            return _report(
                ctx, "not-provable",
                f"unprovable: {label} was discovered but nothing has been indexed yet",
                [ctx.census_line(), ctx.sig_line()], facts=facts, next_action="agrep index")
        source = next((s for s in ctx.sources if s.get("path") == path), None)
        modified = _key_mtime_ms(source.get("stat_key")) if source else None
        return _report(
            ctx, "written-after-last-index",
            f"not indexed yet: {label} appeared after the last index",
            ["intake_stats.json: no record of this file, so no index has parsed it",
             f"store census: file modified {_when(modified)}", ctx.sig_line()],
            facts=facts, next_action="agrep index")
    if entry.get("fresh") is False:
        then, now = entry.get("key"), entry.get("current_key")
        return _report(
            ctx, "written-after-last-index",
            f"not indexed yet: {label} was written after the last index",
            [f"intake_stats.json: parsed at {_when(_key_mtime_ms(then))} ({then}), "
             f"now {_when(_key_mtime_ms(now))} ({now})", _intake_line(entry), ctx.sig_line()],
            facts=facts, next_action="agrep index")
    if entry.get("fresh") is None:
        return _report(
            ctx, "not-provable",
            f"unprovable: {label} has an intake record but its current state cannot be read",
            [_intake_line(entry), ctx.sig_line()], facts=facts)
    if not entry.get("rows"):
        return _report(
            ctx, "discovered-no-rows",
            f"discovered but empty: every record in {label} was skipped, so no row was indexed",
            [_intake_line(entry), ctx.sig_line()], facts=facts,
            next_action="agrep audit" if entry.get("errors") else None)
    return _report(
        ctx, "not-provable",
        f"unprovable: {label} yielded {entry.get('rows')} rows that no published session references",
        [_intake_line(entry), ctx.sig_line()], facts=facts, next_action="agrep index")


def _under(path: str, root: str) -> bool:
    root = root.rstrip(os.sep)
    return bool(root) and (path == root or path.startswith(root + os.sep))


def _not_discovered(ctx: _Context, path: str) -> dict:
    payload = ctx.payload or {}
    exists, is_dir = os.path.exists(path), os.path.isdir(path)
    shown = ctx.display(path)
    lines = [ctx.census_line()]
    real = ctx.real(path)

    def within(root: object) -> bool:
        root = str(root or "")
        return bool(root) and (_under(path, root) or _under(real, ctx.real(root)))

    adapter = next(((a["name"], root) for a in payload.get("adapters", [])
                    for root in a.get("roots", []) if within(root)), None)
    detected = next(((d["name"], d["root"]) for d in payload.get("detected", [])
                     if within(d.get("root"))), None)
    if adapter:
        lines.append(f"{adapter[0]}: the path sits under its store root "
                     f"{ctx.display(adapter[1])} but is "
                     + ("a directory, not a transcript" if is_dir
                        else f"not a transcript {adapter[0]} parses"))
    elif detected:
        lines.append(f"{detected[0]}: detected-only store {ctx.display(detected[1])}; "
                     f"agrep does not index {detected[0]} yet")
    else:
        names = [a["name"] for a in payload.get("adapters", [])]
        present = [f"{a['name']} ({ctx.display(a['roots'][0])})"
                   for a in payload.get("adapters", [])
                   if any(os.path.isdir(root) for root in a.get("roots", []))]
        lines.append("agrep searches " + ", ".join(names) + " stores; "
                     + ("present here: " + ", ".join(present) if present
                        else "none is present on this machine"))
        lines.append("the path is outside every one of them")
    if not exists:
        lines.append(f"filesystem: no file at {shown}")
    return _report(
        ctx, "source-not-discovered",
        f"not indexed: {shown} is not a transcript agrep discovers", lines,
        facts={"path": path, "exists": exists, "under_adapter": adapter,
               "under_detected": detected})


def _no_match(ctx: _Context) -> dict:
    lines = [f"sessions.jsonl: {len(ctx.rows)} chats, none match by id, alias, project or first line",
             ctx.census_line(),
             "parse cache and intake_stats.json: no file or conversation named like it"]
    if ctx.index_skipped:
        lines.append(f"sessions.jsonl: {ctx.index_skipped} corrupt line(s) were skipped")
    return _report(ctx, "source-not-discovered",
                   f"not indexed: nothing agrep discovered matches '{ctx.reference}'", lines)


# --------------------------------------------------------------------------- resolution

def _expand(reference: str, home: str | None) -> str:
    text = reference.strip()
    if text == "~" or text.startswith("~/") or text.startswith("~" + os.sep):
        base = home or os.path.expanduser("~")
        text = base + text[1:]
    return os.path.normpath(os.path.abspath(text)) if text else text


def _looks_like_path(reference: str) -> bool:
    text = reference.strip()
    return text.startswith(("/", "~", "./", "../", os.sep)) or (
        os.sep in text or "/" in text) and not compact.is_result_handle(text)


def _names_a_file(reference: str) -> bool:
    """Path-shaped or carrying a store file extension; a bare word never names a file in cwd."""
    return _looks_like_path(reference) or (
        os.path.splitext(reference.strip())[1].lower() in _TRANSCRIPT_EXTENSIONS)


def _path_lane(ctx: _Context, reference: str) -> tuple[str, list[dict]]:
    expanded = _expand(reference, (ctx.payload or {}).get("home"))
    raw = reference.strip()
    suffix = "" if raw.startswith(("/", "~", os.sep)) else raw
    while suffix.startswith("./"):
        suffix = suffix[2:]
    found: dict[str, dict] = {}

    def consider(agent: object, path: object) -> None:
        path = str(path or "")
        if not path or path in found:
            return
        if path == expanded or (suffix and path.endswith(os.sep + suffix)):
            found[path] = {"agent": agent, "path": path}

    for source in ctx.sources:
        consider(source.get("agent"), source.get("path"))
    for issue in ctx.issues:
        consider(issue.get("agent"), issue.get("path"))
    if not found and _names_a_file(reference):
        alias = ctx.entries_by_real.get(ctx.real(expanded))
        if alias:
            found[alias["path"]] = {"agent": alias.get("agent"), "path": alias["path"]}
    return expanded, list(found.values())


def _alias_rows(rows: list[dict], identity: str) -> list[dict]:
    needle = identity.strip().lower()
    if not needle:
        return []
    exact = [r for r in rows if r.get("alias") == identity]
    if exact:
        return exact
    prefix = [r for r in rows if str(r.get("alias") or "").startswith(identity)]
    if prefix or not _HEX.fullmatch(needle):
        return prefix
    return [r for r in rows if needle in str(r.get("alias") or "").lower()]


def _id_like(identity: str) -> bool:
    return bool(_UUID.fullmatch(identity) or identity.startswith("ses_")
                or _HEX.fullmatch(identity))


def _source_lane(ctx: _Context, identity: str) -> list[dict]:
    """Discovered files or tallied conversations whose own name carries the identity."""
    needle = identity.strip().lower()
    if len(needle) < 6:
        return []
    found: dict[tuple, dict] = {}

    def named(value: object) -> bool:
        text = str(value or "")
        return text == identity or text.startswith(identity) or (
            bool(_HEX.fullmatch(needle)) and needle in text.lower())

    for claim in ctx.cache_sessions:
        if named(claim.get("session")) or named(claim.get("alias")):
            found.setdefault((claim.get("path"), None),
                             {"agent": claim.get("agent"), "path": claim.get("path")})
    for entry in ctx.intake_files:
        if entry.get("session") and named(entry["session"]):
            found.setdefault((entry.get("path"), entry["session"]),
                             {"agent": entry.get("agent"), "path": entry.get("path"),
                              "session": entry["session"]})
    for source in ctx.sources + ctx.issues:
        path = str(source.get("path") or "")
        if needle in os.path.basename(path).lower():
            found.setdefault((path, None), {"agent": source.get("agent"), "path": path})
    return sorted(found.values(),
                  key=lambda c: (str(c.get("path") or ""), str(c.get("session") or "")))


def diagnose(reference: str) -> dict:
    """The verdict envelope for one reference; never mutates the data dir."""
    ctx = _Context(reference)
    expanded, matches = _path_lane(ctx, reference)
    if len(matches) > 1:
        return _ambiguous(ctx, matches, "discovered files", "store census")
    if matches:
        return _judge_source(ctx, matches[0]["agent"], matches[0]["path"])
    if _looks_like_path(reference):
        covering = _issue_covering(ctx, expanded)
        if covering:
            return _judge_source(ctx, covering.get("agent"), expanded)
    if ctx.payload is not None and _names_a_file(reference) and os.path.isfile(expanded):
        return _not_discovered(ctx, expanded)

    matched, human = resume._resolve_reference(ctx.rows, reference)
    if matched and not human:
        return _judge_rows(ctx, matched, "session")
    identity = resume._session_identity(reference)
    aliased = _alias_rows(ctx.rows, identity)
    if aliased:
        return _judge_rows(ctx, aliased, "alias")
    id_like = _id_like(identity)
    if matched and not id_like:
        return _judge_rows(ctx, matched, "human")
    sources = _source_lane(ctx, identity)
    if len(sources) > 1:
        return _ambiguous(ctx, sources, "discovered files",
                          "parse cache, intake_stats.json and store census")
    if sources:
        return _judge_source(ctx, sources[0]["agent"], sources[0]["path"],
                             sources[0].get("session"))
    if matched:
        return _judge_rows(ctx, matched, "human")
    if ctx.payload is None:
        return _report(
            ctx, "not-provable",
            f"unprovable: no indexed chat matches '{reference}' and the source census is unavailable",
            [f"sessions.jsonl: {len(ctx.rows)} chats, none match", ctx.census_line()])
    if _looks_like_path(reference):
        return _not_discovered(ctx, expanded)
    return _no_match(ctx)


# --------------------------------------------------------------------------- surface

_OPTIONS = ("--json", "-h", "--help")


def _positional_dashes(argv: list[str]) -> list[str]:
    """Store slugs begin with `-` (`-projects-cedar/x.jsonl`); only the named options are flags."""
    if "--" in argv:
        return argv
    flags = [token for token in argv if token in _OPTIONS]
    rest = [token for token in argv if token not in _OPTIONS]
    if any(token.startswith("-") for token in rest):
        return flags + ["--"] + rest
    return argv


def render(report: dict, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False))
        return
    print(common.terminal_safe(report["summary"]))
    for line in report["evidence"]["lines"][:_EVIDENCE_LINES]:
        print("  " + common.terminal_safe(line))
    for candidate in report["candidates"][:_CANDIDATE_LINES]:
        print("  " + common.terminal_safe(_candidate_label(candidate)))
    hidden = len(report["candidates"]) - _CANDIDATE_LINES
    if hidden > 0:
        print(f"  … {hidden} more")
    if report.get("next_action"):
        print("next: " + common.terminal_safe(report["next_action"]))


def main(argv: list[str] | None = None) -> int:
    common.utf8_stdio()
    ap = surface.ArgumentParser(
        prog="agrep why",
        description="explain why a chat is, or is not, indexed - without indexing anything",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="A transcript path (absolute, ~, or a trailing part of a discovered path)\n"
               "is checked first; then the reference resolves like `agrep resume`: session\n"
               "id or prefix, @handle, store alias, uuid or ses_ id, a hex fragment of six\n"
               "or more characters, a project label, then a first-line substring.\n"
               "\nverdicts: indexed, indexed-under-alias, indexed-as-side-chat,\n"
               "  written-after-last-index, corpus-behind-transcripts, source-not-discovered,\n"
               "  discovered-no-rows, source-unreadable, ambiguous, not-provable\n"
               "\nexamples:\n"
               "  agrep why 11111111                        a session id prefix\n"
               "  agrep why ~/.claude/projects/x/y.jsonl    a transcript path\n"
               "  agrep why 'retry backoff' --json          a first-line fragment, machine form\n"
               "\nexit: 0 indexed; 1 not indexed (explained); 2 ambiguous, unprovable, or "
               "invalid arguments.",
        allow_abbrev=False)
    ap.add_argument("reference", metavar="REFERENCE",
                    help="session id/prefix, @handle, alias, project label, first-line "
                         "substring, or transcript path")
    ap.add_argument("--json", action="store_true",
                    help='one {"version":1,"verdict":...} object with every evidence field')
    args = ap.parse_args(_positional_dashes(sys.argv[1:] if argv is None else list(argv)))
    if not args.reference.strip():
        ap.error("REFERENCE must not be empty")
    try:
        compact.normalize_session_arg(args.reference)
    except compact.CompactError as exc:
        common.log(str(exc))
        return 2
    report = diagnose(args.reference)
    render(report, as_json=args.json)
    return int(report["exit"])


if __name__ == "__main__":
    raise SystemExit(main())
