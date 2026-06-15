"""Derived sqlite/FTS5 index over the search corpus - why cold CLI calls are fast.

The materialized jsonl stays the source of truth (debuggable, the server's warm caches
still read it). This module mirrors it into data/corpus.db with a trigram FTS5 index,
so a cold `agrep <pattern>` is an indexed lookup instead of a parse of ~50 MB of jsonl
plus a linear scan. Staleness is checked against the source files' (mtime, size) every
connect; a stale db rebuilds in one shot and swaps in atomically.

Search semantics are IDENTICAL to the legacy scans (explore.keyword_search and
search.py's word/regex paths): FTS narrows to candidate rows, then the same python
matchers confirm and place snippets. Trigram needs >=3-char tokens - shorter ones fall
back to an indexed-table LIKE scan, still no jsonl parse. sqlite without trigram
support returns None from connect() and callers use the legacy path.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path

import common

DB_PATH = common.DATA_DIR / "corpus.db"
_SCHEMA = "3"
# corpus inputs; a change in any (mtime_ns, size) invalidates the db
_SOURCES = ("messages.jsonl", "replies.jsonl", "session_concepts.jsonl", "concepts.json")
RECAP_PREFIX = "This session is being continued from a previous conversation"
_FIELDS = ("session", "agent", "project", "concept", "model", "model_source",
           "turn", "ts", "who")
_TEXT = len(_FIELDS)


def _snip_at(text: str, start: int, end: int, pad: int = 80) -> str:
    """Same one-line window explore._snip_at produces (kept local: explore imports us)."""
    a, b = max(0, start - pad), min(len(text), end + pad)
    s = ("…" if a > 0 else "") + text[a:b] + ("…" if b < len(text) else "")
    return " ".join(s.split())


def _stamp() -> str:
    gen = []
    for name in _SOURCES:
        try:
            st = (common.DATA_DIR / name).stat()
            gen.append([st.st_mtime_ns, st.st_size])
        except OSError:
            gen.append(None)
    return json.dumps(gen)


_TRIGRAM_OK: bool | None = None


def _trigram_ok() -> bool:
    # probed once per process: the answer is a property of the linked sqlite,
    # and the throwaway :memory: FTS table isn't free on the cold-search path
    global _TRIGRAM_OK
    if _TRIGRAM_OK is None:
        try:
            db = sqlite3.connect(":memory:")
            db.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='trigram')")
            _TRIGRAM_OK = True
        except sqlite3.OperationalError:
            _TRIGRAM_OK = False
    return _TRIGRAM_OK


def _open(path) -> sqlite3.Connection:
    """Connect for reading with pragmas sized for a corpus-scale db: default
    cache (~2 MB) and no mmap make the first cold query pay random-read I/O."""
    db = sqlite3.connect(path)
    db.executescript("""
        PRAGMA mmap_size=268435456;
        PRAGMA cache_size=-65536;
        PRAGMA temp_store=MEMORY;
    """)
    return db


def _build(dst) -> None:
    """One-shot rebuild: stream the jsonl sources into msgs + its FTS mirror. Mirrors
    explore._kw_corpus row-for-row (one row per user turn, one per agent reply) so
    every engine reports identical hits."""
    db = sqlite3.connect(dst)
    db.executescript("""
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=OFF;
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE msgs(
            id INTEGER PRIMARY KEY,
            session TEXT NOT NULL, turn INTEGER, ts INTEGER,
            agent TEXT, project TEXT, concept TEXT, model TEXT, model_source TEXT,
            who TEXT, text TEXT);
        CREATE INDEX msgs_session ON msgs(session, turn);
        CREATE VIRTUAL TABLE msgs_fts USING fts5(
            text, content='msgs', content_rowid='id', tokenize='trigram');
    """)

    names: dict[int, str] = {}
    p = common.DATA_DIR / "concepts.json"
    if p.exists():
        for r in json.loads(p.read_text(encoding="utf-8")):
            names[int(r["concept_id"])] = (r.get("name") or r.get("label") or "").strip()
    concept: dict[str, str] = {}
    p = common.DATA_DIR / "session_concepts.jsonl"
    if p.exists():
        for line in p.open(encoding="utf-8"):
            if line.strip():
                o = json.loads(line)
                concept[o["session"]] = (names.get(int(o.get("concept_id", -1)))
                                         or o.get("label", ""))

    reps: dict[str, str] = {}
    p = common.DATA_DIR / "replies.jsonl"
    if p.exists():
        for line in p.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if o.get("id"):
                reps[o["id"]] = o.get("reply", "")

    ins = ("INSERT INTO msgs(session, turn, ts, agent, project, concept, model, "
           "model_source, who, text) VALUES(?,?,?,?,?,?,?,?,?,?)")
    rows = []
    with common.MESSAGES_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            s = o.get("session")
            if not s:
                continue
            model = o.get("model", "")
            model_source = o.get("model_source") or ("explicit" if model else "unknown")
            base = (s, o.get("turn", 0), o.get("ts", 0), o.get("agent", ""),
                    o.get("project", ""), concept.get(s, ""), model, model_source)
            t = o.get("text", "") or ""
            if t:
                who = "recap" if t.startswith(RECAP_PREFIX) else o.get("who", "user")
                rows.append((*base, who, t))
            r = reps.get(o.get("id", ""), "")
            if r:
                rows.append((*base, "agent", r))
            if len(rows) >= 5000:
                db.executemany(ins, rows)
                rows = []
    if rows:
        db.executemany(ins, rows)
    db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
    db.execute("INSERT INTO meta VALUES('stamp', ?)", (_stamp(),))
    db.execute("INSERT INTO meta VALUES('schema', ?)", (_SCHEMA,))
    db.commit()
    db.close()


def _valid_db(stamp: str) -> sqlite3.Connection | None:
    if not DB_PATH.exists():
        return None
    try:
        db = _open(DB_PATH)
        meta = dict(db.execute(
            "SELECT key, value FROM meta WHERE key IN ('stamp', 'schema')"))
        if meta.get("stamp") == stamp and meta.get("schema") == _SCHEMA:
            return db
        db.close()
    except (sqlite3.DatabaseError, TypeError, OSError):
        pass
    return None


def _tmp_db_path() -> Path:
    return DB_PATH.with_name(f"{DB_PATH.name}.{os.getpid()}.{time.time_ns()}.tmp")


def _cleanup_tmp(path) -> None:
    for p in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def connect(quiet: bool = False) -> sqlite3.Connection | None:
    """A connection to a FRESH corpus db, building/rebuilding first if the sources
    moved. None when there's nothing to index yet or sqlite lacks trigram fts5."""
    if not common.MESSAGES_PATH.exists() or not _trigram_ok():
        return None
    stamp = _stamp()
    db = _valid_db(stamp)
    if db is not None:
        return db
    if not quiet:
        common.log("(re)building search index - one-time after each reindex…")
    with common.IndexLock("corpusdb"):
        stamp = _stamp()
        db = _valid_db(stamp)
        if db is not None:
            return db
        tmp = _tmp_db_path()
        _cleanup_tmp(tmp)
        _build(tmp)
        try:
            common.replace_with_retry(tmp, DB_PATH)
        except OSError:
            # On Windows a live reader can still hold corpus.db. Keep the old
            # published db serving and let the next connect retry the rebuild.
            _cleanup_tmp(tmp)
            return _open(DB_PATH) if DB_PATH.exists() else None
        return _open(DB_PATH)


# ------------------------------------------------------------------ query engines

def _fts_quote(tok: str) -> str:
    return '"' + tok.replace('"', '""') + '"'


def _like_quote(tok: str) -> str:
    esc = tok.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


def _candidates(db: sqlite3.Connection, toks: list[str]):
    """Rows that contain every token, via the cheapest applicable index: trigram FTS
    for >=3-char tokens, indexed LIKE for the stubs. Yields msgs rows."""
    fts = [t for t in toks if len(t) >= 3]
    likes = [t for t in toks if len(t) < 3]
    sel = ("SELECT session, agent, project, concept, model, model_source, "
           "turn, ts, who, text FROM msgs")
    if fts:
        where = ["id IN (SELECT rowid FROM msgs_fts WHERE msgs_fts MATCH ?)"]
        params: list = [" AND ".join(_fts_quote(t) for t in fts)]
    else:
        where, params = [], []
    for t in likes:
        where.append("text LIKE ? ESCAPE '\\'")
        params.append(_like_quote(t))
    q = sel + (" WHERE " + " AND ".join(where) if where else "")
    return db.execute(q, params)


def _hit(row, start: int, end: int) -> dict:
    h = dict(zip(_FIELDS, row[:_TEXT]))
    h["snippet"] = _snip_at(row[_TEXT], start, end)
    return h


def _pack(hits: list[dict], k: int) -> dict:
    hits.sort(key=lambda h: (h["session"], h["turn"], 0 if h["who"] != "agent" else 1))
    return {"hits": hits[:k], "total": len(hits), "chats": len({h["session"] for h in hits})}


def keyword(db: sqlite3.Connection, q: str, k: int) -> dict:
    """Separator-flexible keyword search, same semantics as explore.keyword_search:
    FTS candidates (superset: tokens anywhere in the row), then the exact matcher
    confirms adjacency and places the snippet."""
    toks = [t for t in re.split(r"[\s\-_]+", q.strip()) if t]
    if not toks:
        return {"hits": [], "total": 0, "chats": 0}
    hits = []
    if len(toks) == 1:  # no separators in q, so the token IS the query
        ql = q.strip().lower()
        for row in _candidates(db, [ql]):
            i = row[_TEXT].lower().find(ql)
            if i >= 0:
                hits.append(_hit(row, i, i + len(ql)))
    else:
        pat = re.compile(r"[\s\-_]*".join(re.escape(t) for t in toks), re.I)
        for row in _candidates(db, toks):
            m = pat.search(row[_TEXT])
            if m:
                hits.append(_hit(row, m.start(), m.end()))
    return _pack(hits, k)


def word(db: sqlite3.Connection, q: str, k: int) -> dict:
    """Whole-word search: FTS prefilter, boundary check only on candidates."""
    ql = q.lower()
    n = len(ql)
    wc = re.compile(r"[\w]")
    hits = []
    for row in _candidates(db, [q]):
        low = row[_TEXT].lower()
        i = low.find(ql)
        while i >= 0:
            j = i + n
            if (i == 0 or not wc.match(low[i - 1])) and \
               (j >= len(low) or not wc.match(low[j])):
                hits.append(_hit(row, i, j))
                break
            i = low.find(ql, i + 1)
    return _pack(hits, k)


def _required_literal(pattern: str) -> str | None:
    """Longest ASCII literal every match must contain, or None. Only top-level
    LITERAL runs count: alternations, classes, groups, and repeats break the run,
    so `TODO|FIXME` correctly yields None (no single literal is required) while
    `memory leak.*free` yields "memory leak". Sound = may return None when a
    literal exists, never returns one a match could lack."""
    try:
        import re._parser as sre  # 3.11+
    except ImportError:  # 3.10
        import sre_parse as sre
    try:
        seq = sre.parse(pattern)
    except Exception:  # noqa: BLE001 -- bad pattern; let re.compile report it
        return None
    best, run = "", ""
    for op, arg in seq:
        name = str(op)
        if name == "LITERAL" and isinstance(arg, int) and 0x20 <= arg < 0x7F:
            run += chr(arg)
        elif name == "AT":  # zero-width anchor (\b, ^, $): transparent
            continue
        else:
            best, run = max(best, run, key=len), ""
    best = max(best, run, key=len)
    return best if len(best) >= 3 else None


def regex(db: sqlite3.Connection, pattern: str, k: int) -> dict:
    """Regex can't use the index directly, but when the pattern demands a literal
    (most real ones do) the trigram FTS narrows candidates first; otherwise stream
    the table, which still skips the jsonl parse the legacy path paid."""
    rx = re.compile(pattern, re.I)
    hits = []
    lit = _required_literal(pattern)
    cur = (_candidates(db, [lit.lower()]) if lit else db.execute(
        "SELECT session, agent, project, concept, model, model_source, "
        "turn, ts, who, text FROM msgs"))
    for row in cur:
        m = rx.search(row[_TEXT])
        if m:
            hits.append(_hit(row, m.start(), m.end()))
    return _pack(hits, k)


def session_rows(db: sqlite3.Connection, session: str) -> list[dict]:
    """One session's rows in turn order, for explore.get_window's fast path."""
    cur = db.execute(
        "SELECT session, agent, project, concept, model, model_source, turn, ts, who, text FROM msgs "
        "WHERE session = ? ORDER BY turn", (session,))
    return [dict(zip((*_FIELDS, "text"), row)) for row in cur]
