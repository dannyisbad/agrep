"""`agrep why <reference> [--json]` - explain why a chat is, or is not, indexed.

Read-only by construction: it never indexes, never writes the data dir and never wakes
the daemon. A reference resolves like `agrep resume` (id, handle, alias, project label,
first-line fragment) after a path lane; every evidence line names the file it came from.
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


def _corpus_facts(session: str) -> dict:
    """What the search database holds for one session, read in mode=ro, and whether that
    copy is what messages.jsonl now publishes."""
    path = common.DATA_DIR / "corpus.db"
    if not path.exists():
        return {"state": "missing"}
    try:
        uri = Path(os.path.abspath(os.fspath(path))).as_uri() + "?mode=ro"
        db = sqlite3.connect(uri, uri=True, timeout=1.0)
    except (sqlite3.Error, OSError, ValueError) as exc:
        return {"state": "unreadable", "reason": str(exc)}
    try:
        rows = db.execute("SELECT count(*) FROM msgs WHERE session=?", (session,)).fetchone()[0]
        sig = db.execute("SELECT sig FROM session_sig WHERE session=?", (session,)).fetchone()
        family = db.execute("SELECT root, side FROM session_family WHERE session=?",
                            (session,)).fetchone()
        stamp = db.execute("SELECT value FROM meta WHERE key='stamp'").fetchone()
    except sqlite3.Error as exc:
        return {"state": "unreadable", "reason": str(exc)}
    finally:
        db.close()
    facts = {"state": "ok", "rows": int(rows), "session_sig": sig is not None,
             "root": family[0] if family else None,
             "side": bool(family[1]) if family else None}
    facts.update(_corpus_currency(session, sig[0] if sig else None, stamp[0] if stamp else ""))
    return facts


def _corpus_currency(session: str, stored_sig: str | None, stamp: str) -> dict:
    """Is the stored copy of `session` current? A source stamp equal to the one the database
    recorded proves every session current without a scan; otherwise this session's published
    rows are fingerprinted with corpusdb's own signature and compared to the stored one."""
    import corpusdb
    import events
    # The scan validates event payloads; damage found there must not schedule indexd from `why`.
    events.set_event_repair_callback(lambda: False)
    try:
        if corpusdb._stamps_equal(stamp, corpusdb._stamp()):
            return {"stamp_current": True, "proof": "stamp", "current": True}
        rows = corpusdb._scan(only={session}).get(session, [])
    except (OSError, RuntimeError, ValueError) as exc:
        return {"stamp_current": False, "proof": None, "current": None, "reason": str(exc)}
    published = corpusdb._session_sig(rows) if rows else None
    return {"stamp_current": False, "proof": "session_sig",
            "current": bool(rows) and published == stored_sig, "published_rows": len(rows)}


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


def _ambiguous(ctx: _Context, candidates: list[dict], what: str) -> dict:
    return _report(
        ctx, "ambiguous",
        f"ambiguous: '{ctx.reference}' matches {len(candidates)} {what}",
        ["pass a full id, a @handle from a search hit, or the transcript path"],
        candidates=candidates)


def _judge_rows(ctx: _Context, rows: list[dict], via: str) -> dict:
    if len(rows) > 1:
        return _ambiguous(ctx, [_candidate_from_row(r, via=via) for r in rows], "chats")
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
    if corpus.get("state") != "ok" or not corpus.get("rows") or corpus.get("current") is False:
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


def _corpus_line(corpus: dict) -> str:
    """The corpus.db evidence line, naming which proof of currency was used."""
    if corpus.get("state") != "ok":
        return (f"corpus.db: {corpus.get('state')}"
                + (f" ({corpus['reason']})" if corpus.get("reason") else ""))
    sig = "present" if corpus["session_sig"] else "absent"
    if corpus["proof"] == "session_sig":
        published = _plural(corpus["published_rows"], "row")
        if corpus["current"]:
            sig = f"matches the {published} messages.jsonl publishes"
        elif corpus["session_sig"]:
            sig = f"differs from the {published} messages.jsonl publishes"
        else:
            sig = f"absent, messages.jsonl publishes {published}"
        sig += ", stamp behind the published sources"
    elif corpus["proof"] is None:
        sig += (", stamp behind the published sources, messages.jsonl unverifiable "
                f"({corpus['reason']})")
    family = (f", family root {corpus['root']}" + (" (side chat)" if corpus["side"] else "")
              if corpus.get("root") else "")
    return f"corpus.db: {_plural(corpus['rows'], 'row')}, session_sig {sig}{family}"


def _corpus_behind_summary(corpus: dict, agent: str, session: str) -> str:
    if corpus.get("state") != "ok" or not corpus.get("rows"):
        return (f"not searchable yet: transcripts list {agent} chat {_short(session)} "
                "but the search database does not hold it")
    return (f"not fully searchable: the search database holds an older copy of {agent} chat "
            f"{_short(session)} than messages.jsonl publishes (session_sig differs)")


_READABLE_ACTION = "make the file readable, then agrep index"


def _issue_covering(ctx: _Context, path: str) -> dict | None:
    """The first live or durable issue naming `path` or a directory above it."""
    return next((i for i in ctx.issues
                 if i.get("path") and (i["path"] == path or _under(path, i["path"]))), None)


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
        return _ambiguous(ctx, candidates, "conversations in that database")
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
    adapter = next(((a["name"], root) for a in payload.get("adapters", [])
                    for root in a.get("roots", []) if _under(path, root)), None)
    detected = next(((d["name"], d["root"]) for d in payload.get("detected", [])
                     if _under(path, d.get("root") or "")), None)
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
        return _ambiguous(ctx, matches, "discovered files")
    if matches:
        return _judge_source(ctx, matches[0]["agent"], matches[0]["path"])
    if _looks_like_path(reference):
        covering = _issue_covering(ctx, expanded)
        if covering:
            return _judge_source(ctx, covering.get("agent"), expanded)
    if ctx.payload is not None and os.path.isfile(expanded):
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
        return _ambiguous(ctx, sources, "discovered files")
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
