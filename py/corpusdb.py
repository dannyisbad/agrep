"""Derived sqlite/FTS5 index over the search corpus - why cold CLI calls are fast.

The materialized jsonl stays the source of truth (debuggable, the server's warm caches
still read it). This module mirrors it into data/corpus.db with a trigram FTS5 index,
so a cold `agrep <pattern>` is an indexed lookup instead of a parse of ~50 MB of jsonl
plus a linear scan. Staleness is checked against the source files' (mtime, size) every
connect; an unchanged stamp reuses the db untouched. When the stamp moves, the db is
refreshed INCREMENTALLY: a per-session content fingerprint (session_sig) tells us which
sessions actually changed, and only those are re-indexed (DELETE + re-INSERT, with FTS5
triggers keeping the index in sync) - in place, under the index lock, with a busy_timeout
so concurrent searches just briefly wait rather than fail. A full one-shot rebuild + atomic
swap is the fallback for a cold start, a schema bump, or a corrupt db.

Search semantics are IDENTICAL to the legacy scans (explore.keyword_search and
search.py's word/regex paths): FTS narrows to candidate rows, then the same python
matchers confirm and place snippets. Trigram needs >=3-char tokens - shorter ones fall
back to an indexed-table LIKE scan, still no jsonl parse. sqlite without trigram
support returns None from connect() and callers use the legacy path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path

import common

DB_PATH = common.DATA_DIR / "corpus.db"
_SCHEMA = "4"
# corpus inputs; a change in any (mtime_ns, size) invalidates the cached stamp and triggers
# a refresh (incremental when possible, full rebuild on schema bump / corruption / cold).
_SOURCES = ("messages.jsonl", "replies.jsonl", "session_concepts.jsonl", "concepts.json")
# positions of session_concepts.jsonl / concepts.json in _SOURCES: a concept relabel can touch
# many sessions and isn't reflected in .changed_sessions, so it forces the full-scan path.
_CONCEPT_IDX = (2, 3)
# The Rust ingest's changed-session delta (newline ids, or "*" = everything). The incremental
# refresh re-indexes only these instead of rescanning the whole corpus, then deletes it.
CHANGED_PATH = common.DATA_DIR / ".changed_sessions"
_FAST_MAX = 500  # more changed sessions than this -> the prefilter scan isn't worth it; full
RECAP_PREFIX = "This session is being continued from a previous conversation"
_FIELDS = ("session", "agent", "project", "concept", "model", "model_source",
           "turn", "ts", "who")
_TEXT = len(_FIELDS)

# Insert shared by the full build and the incremental update so both write identical rows.
_INS = ("INSERT INTO msgs(session, turn, ts, agent, project, concept, model, "
        "model_source, who, text) VALUES(?,?,?,?,?,?,?,?,?,?)")

_SCHEMA_SQL = """
    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE msgs(
        id INTEGER PRIMARY KEY,
        session TEXT NOT NULL, turn INTEGER, ts INTEGER,
        agent TEXT, project TEXT, concept TEXT, model TEXT, model_source TEXT,
        who TEXT, text TEXT);
    CREATE INDEX msgs_session ON msgs(session, turn);
    -- per-session content fingerprint: how the incremental update knows which sessions moved
    CREATE TABLE session_sig(session TEXT PRIMARY KEY, sig TEXT);
    CREATE VIRTUAL TABLE msgs_fts USING fts5(
        text, content='msgs', content_rowid='id', tokenize='trigram');
"""

# External-content FTS5 sync triggers. Created AFTER the cold build's bulk insert + one-shot
# 'rebuild' (so the cold path doesn't double-index), they then maintain the FTS automatically
# for the incremental DELETE/INSERT of just the sessions that changed.
_TRIGGERS_SQL = """
    CREATE TRIGGER msgs_ai AFTER INSERT ON msgs BEGIN
        INSERT INTO msgs_fts(rowid, text) VALUES (new.id, new.text);
    END;
    CREATE TRIGGER msgs_ad AFTER DELETE ON msgs BEGIN
        INSERT INTO msgs_fts(msgs_fts, rowid, text) VALUES('delete', old.id, old.text);
    END;
    CREATE TRIGGER msgs_au AFTER UPDATE ON msgs BEGIN
        INSERT INTO msgs_fts(msgs_fts, rowid, text) VALUES('delete', old.id, old.text);
        INSERT INTO msgs_fts(rowid, text) VALUES (new.id, new.text);
    END;
"""


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
    cache (~2 MB) and no mmap make the first cold query pay random-read I/O. The
    busy_timeout lets a read coexist with an in-place incremental update - both are
    sub-second, so SQLite's file locks just make the loser wait, not fail."""
    db = sqlite3.connect(path)
    db.executescript("""
        PRAGMA busy_timeout=5000;
        PRAGMA mmap_size=268435456;
        PRAGMA cache_size=-65536;
        PRAGMA temp_store=MEMORY;
    """)
    return db


def _scan(only: set[str] | None = None) -> dict[str, list[tuple]]:
    """Parse the materialized corpus into per-session row lists - the exact rows the msgs
    table holds (one per user turn, one per agent reply), mirroring explore._kw_corpus so
    every engine reports identical hits. Shared by the full build and the incremental update
    so both index byte-identical content. The session concept rides in each row, so a concept
    relabel changes that session's fingerprint and re-indexes it like any other content move.

    `only` restricts the (expensive) JSON parse to a small set of session ids: each line's
    session is pulled out with one cheap `find` (the Rust writer emits compact JSON, so the
    keys are literal `"session":"` / `"id":"`), and non-candidate lines are skipped before
    json.loads. So the incremental refresh parses just the changed sessions, not the whole
    ~50MB corpus. (A per-line O(1) field-extract, not an any(id in line) scan over the whole
    candidate set - that was slower than just parsing.)"""
    def _field(line: str, key: str) -> "str | None":
        # value of a top-level string field via cheap scan. `"key"` (with quotes) only occurs
        # as a key - inside a JSON string a quote is escaped - so this never matches a value.
        # Tolerates optional whitespace after the colon (compact or pretty JSON).
        k = line.find('"' + key + '"')
        if k < 0:
            return None
        i = line.find(":", k + len(key) + 2)
        if i < 0:
            return None
        i += 1
        while i < len(line) and line[i] in " \t":
            i += 1
        if i >= len(line) or line[i] != '"':
            return None
        i += 1
        b = line.find('"', i)
        return line[i:b] if b >= 0 else None
    # concepts are an enrichment layer; a half-written/malformed file (mid-reindex) must
    # degrade to "no labels", never raise - _scan runs on the hot per-search refresh path.
    names: dict[int, str] = {}
    p = common.DATA_DIR / "concepts.json"
    if p.exists():
        try:
            for r in json.loads(p.read_text(encoding="utf-8")):
                names[int(r["concept_id"])] = (r.get("name") or r.get("label") or "").strip()
        except (OSError, ValueError, KeyError, TypeError):
            names = {}
    concept: dict[str, str] = {}
    p = common.DATA_DIR / "session_concepts.jsonl"
    if p.exists():
        try:
            with p.open(encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        o = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    concept[o["session"]] = (names.get(int(o.get("concept_id", -1)))
                                             or o.get("label", ""))
        except OSError:
            pass

    reps: dict[str, str] = {}
    p = common.DATA_DIR / "replies.jsonl"
    if p.exists():
        for line in p.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            if only is not None:
                idv = _field(line, "id")  # id = agent:session:turn -> session is the middle
                parts = idv.split(":") if idv else []
                if len(parts) < 3 or parts[1] not in only:
                    continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if o.get("id"):
                reps[o["id"]] = o.get("reply", "")

    by: dict[str, list[tuple]] = {}
    with common.MESSAGES_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if only is not None and _field(line, "session") not in only:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            s = o.get("session")
            if not s or (only is not None and s not in only):
                continue
            model = o.get("model", "")
            # absent-key fallback only (NOT `or`): an explicit "" must stay "", matching
            # explore._kw_corpus so the FTS and legacy engines report identical model_source.
            model_source = o.get("model_source", "explicit" if model else "unknown")
            base = (s, o.get("turn", 0), o.get("ts", 0), o.get("agent", ""),
                    o.get("project", ""), concept.get(s, ""), model, model_source)
            rows = by.setdefault(s, [])
            t = o.get("text", "") or ""
            if t:
                who = "recap" if t.startswith(RECAP_PREFIX) else o.get("who", "user")
                rows.append((*base, who, t))
            r = reps.get(o.get("id", ""), "")
            if r:
                rows.append((*base, "agent", r))
    return by


def _session_sig(rows: list[tuple]) -> str:
    """Order-independent fingerprint of one session's indexed rows. Sorted before hashing so a
    reordering in messages.jsonl never reads as a change; covers every column the msgs table
    stores, so any content/metadata move (edit, model backfill, concept relabel) flips the sig."""
    h = hashlib.md5()
    for row in sorted(rows, key=lambda r: (r[1], r[8], r[9])):  # (turn, who, text)
        h.update(repr(row).encode("utf-8", "replace"))
    return h.hexdigest()


def _build(dst) -> None:
    """One-shot rebuild: stream every session into msgs + its FTS mirror, then arm the sync
    triggers and record per-session fingerprints so subsequent refreshes go incremental."""
    db = sqlite3.connect(dst)
    db.executescript("PRAGMA journal_mode=DELETE; PRAGMA synchronous=OFF;" + _SCHEMA_SQL)
    by = _scan()
    for rows in by.values():
        db.executemany(_INS, rows)
    # bulk FTS build in one shot (no triggers armed yet, so rows aren't double-indexed)
    db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
    db.executescript(_TRIGGERS_SQL)
    db.executemany("INSERT INTO session_sig(session, sig) VALUES(?, ?)",
                   [(s, _session_sig(rows)) for s, rows in by.items()])
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


def _read_changed() -> "set[str] | str | None":
    """The Rust ingest's changed-session delta: "*" (re-index everything), a set of session
    ids, or None when the file is absent/unreadable (older binary, or first run -> full scan)."""
    try:
        txt = CHANGED_PATH.read_text(encoding="utf-8")
    except OSError:
        return None
    ids = {ln.strip() for ln in txt.splitlines() if ln.strip()}
    return "*" if "*" in ids else ids


def _consume_changed() -> None:
    """Delete the delta once applied. Until then it accumulates across ingests, so a skipped
    corpus refresh never silently drops a session (we re-apply it next time)."""
    try:
        CHANGED_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def _current_sessions() -> "set[str] | None":
    """Every session currently in the corpus, read from the small sessions.jsonl (one tiny row
    per session) rather than the ~50MB messages.jsonl - for detecting removals on the fast path.
    None when it can't be read (caller then takes the full-scan path)."""
    p = common.DATA_DIR / "sessions.jsonl"
    if not p.exists():
        return None
    out: set[str] = set()
    try:
        with p.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    s = json.loads(line).get("session")
                except json.JSONDecodeError:
                    continue
                if s:
                    out.add(s)
    except OSError:
        return None
    return out


def _concepts_moved(old_stamp: str, new_stamp: str) -> bool:
    """Did a concept source change between the indexed stamp and now? Such a relabel can touch
    many sessions and isn't in .changed_sessions, so it must take the full-scan path. Unknown
    -> True (be safe)."""
    try:
        o, n = json.loads(old_stamp), json.loads(new_stamp)
        return any(o[i] != n[i] for i in _CONCEPT_IDX)
    except (ValueError, IndexError, TypeError):
        return True


def _incremental(stamp: str) -> sqlite3.Connection | None:
    """Refresh an existing current-schema db in place, re-indexing ONLY the sessions the Rust
    delta named (confirmed per-session by sig, removals reconciled against sessions.jsonl). Re-
    parses just those via _scan's prefilter, so the common "a few new turns" refresh is ~tens of
    ms instead of rescanning the whole corpus. Returns None - so the caller does a bulk _build -
    whenever that fast path doesn't apply: no db / schema bump / a half-written source / no usable
    delta / a concept relabel / a change set big enough that bulk rebuild beats row-by-row trigger
    updates. (Re-indexing most of the corpus through the FTS triggers is slower than _build's one-
    shot 'rebuild', so we hand those back.) Runs under the caller's IndexLock; busy_timeout lets an
    in-flight search wait out the sub-second update rather than error."""
    if not DB_PATH.exists():
        return None
    changed = _read_changed()
    current = _current_sessions()
    if not (isinstance(changed, set) and len(changed) <= _FAST_MAX and current is not None):
        return None  # not a small named delta -> bulk _build is the right tool
    db = None
    try:
        db = sqlite3.connect(DB_PATH)
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA synchronous=NORMAL")
        meta = dict(db.execute("SELECT key, value FROM meta WHERE key IN ('schema', 'stamp')"))
        if meta.get("schema") != _SCHEMA:
            return None  # schema bump -> caller rebuilds from scratch
        if _concepts_moved(meta.get("stamp", ""), stamp):
            return None  # concept relabel touches ~every session -> bulk _build beats triggers
        old = dict(db.execute("SELECT session, sig FROM session_sig"))
        by = _scan(only=changed)  # prefiltered: parses only the changed sessions' rows
        db.execute("BEGIN")
        # re-index just the changed candidates; the per-session sig confirms each one really
        # moved (the delta is a superset). FTS triggers mirror every DELETE/INSERT, so the
        # result is byte-identical to rebuilding these rows from scratch.
        for s in changed:
            rows = by.get(s, [])
            sig = _session_sig(rows) if rows else None
            if sig == old.get(s):
                continue  # candidate didn't really change
            db.execute("DELETE FROM msgs WHERE session = ?", (s,))
            if rows:
                db.executemany(_INS, rows)
                db.execute("INSERT OR REPLACE INTO session_sig(session, sig) VALUES(?, ?)", (s, sig))
            else:
                db.execute("DELETE FROM session_sig WHERE session = ?", (s,))
        # removals the delta didn't name (e.g. a deleted session file): anything indexed that
        # sessions.jsonl no longer lists.
        for s in old:
            if s not in changed and s not in current:
                db.execute("DELETE FROM msgs WHERE session = ?", (s,))
                db.execute("DELETE FROM session_sig WHERE session = ?", (s,))
        db.execute("UPDATE meta SET value = ? WHERE key = 'stamp'", (stamp,))
        db.commit()
        _consume_changed()  # applied -> clear the delta
        db.close()
        db = None
        return _open(DB_PATH)
    except Exception:  # noqa: BLE001 -- ANY failure (db corruption, a half-written source's
        # OSError/JSONDecodeError) drops to a clean full rebuild, never escapes to crash the
        # search. finally closes db so no write-locked handle blocks the rebuild swap.
        return None
    finally:
        if db is not None:
            try:
                db.close()
            except sqlite3.DatabaseError:
                pass


def connect(quiet: bool = False) -> sqlite3.Connection | None:
    """A connection to a FRESH corpus db, refreshing first if the sources moved. None when
    there's nothing to index yet or sqlite lacks trigram fts5. The common case - unchanged
    stamp - returns the live db untouched; a moved stamp goes incremental (only the changed
    sessions re-indexed), full rebuild only on a cold start / schema bump / corruption."""
    if not common.MESSAGES_PATH.exists() or not _trigram_ok():
        return None
    stamp = _stamp()
    db = _valid_db(stamp)
    if db is not None:
        return db
    if not quiet:
        common.log("refreshing search index…")
    with common.IndexLock("corpusdb"):
        stamp = _stamp()
        db = _valid_db(stamp)
        if db is not None:
            return db
        db = _incremental(stamp)
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
        _consume_changed()  # a full rebuild indexed everything -> the delta is superseded
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
