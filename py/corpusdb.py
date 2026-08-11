"""Derived SQLite/FTS5 index over the materialized search corpus.

The materialized jsonl stays the debuggable source of truth. This module mirrors it into
data/corpus.db with a trigram FTS5 index, so `agrep <pattern>` avoids reparsing and
linearly scanning the JSONL corpus. Staleness is checked against source generations every
connect; an unchanged stamp reuses the db untouched. When the stamp moves, the db is
refreshed INCREMENTALLY: a per-session content fingerprint (session_sig) tells us which
sessions changed, then a duplicate-preserving row diff fires FTS5 triggers only for the
actual additions/removals. Interactive readers never wait behind that writer: they serve a
schema-compatible stale snapshot or fall back to the materialized JSONL engine. A full
one-shot rebuild + atomic swap is the fallback for a cold start, schema bump, or corrupt db.

Search semantics match the JSONL fallback (explore.keyword_search and search.py's
word/regex paths): FTS narrows to candidate rows, then the same Python
matchers confirm and place snippets. Trigram needs >=3-char tokens - shorter ones fall
back to an indexed-table LIKE scan, still no jsonl parse. sqlite without trigram
support returns None from connect() and callers use the JSONL fallback.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
import hashlib
import heapq
import json
import math
import os
import re
import sqlite3
import stat as statmod
import sys
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import chain
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping, NamedTuple

import common
import compact
import fileops
import index_lock
import indexd_runtime
import surface_policy as surface

DB_PATH = common.DATA_DIR / "corpus.db"
BOUNDARY_STATS_PATH = common.DATA_DIR / "boundary_stats.json"
INGEST_SIG_PATH = common.DATA_DIR / ".ingest.sig"
_SCHEMA = "15"  # 15: structural side-session provenance in family relations
_TRIGGER_SCHEMA = "4"  # 4: FTS triggers consume the normalized text sidecar
# The compact event generation moves only after its DB transaction commits.
_SOURCES = ("messages.jsonl", "replies.jsonl", "session_concepts.jsonl", "concepts.json",
            "concept_pair.manifest.json", "events/.generation", "settings.json",
            "boundary_stats.json", "sessions.jsonl")
# _SOURCES positions whose change can touch ~every session without appearing in
# .changed_sessions: concept publications force a full parse + signature diff
# (event-file changes ride the ingest's normal delta).
_MANIFEST_IDX = 4
_EXACT_SESSION_EXCLUSION_INLINE_MAX = 4
_EXACT_SESSION_EXCLUSION_MAX = 65_536
_CONCEPT_IDX = (2, 3, 4)
_BULK_IDX = (6,)  # the tools toggle can add/remove rows across the whole corpus
_BOUNDARY_IDX = 7
_DERIVED_PROOF_VERSION = 6
_PLATFORM_NAME = os.name
_DERIVED_PROOF_NAMES = (
    "messages.jsonl", "replies.jsonl", "sessions.jsonl",
    common.SESSION_FAMILY_META_FILE, "boundary_stats.json",
    ".boundary_stats.bin", "event_stats.json",
)
_INGEST_SIGNATURE_MAX_BYTES = 4096
_DERIVED_PROOF_MAX_BYTES = 1024 * 1024
# Rust ingest's changed-session delta: newline ids or "*" = scan everything; deleted once applied.
CHANGED_PATH = common.DATA_DIR / ".changed_sessions"
_FAST_MAX = 500  # above this, one full parse is cheaper than per-session prefilter bookkeeping
_FIELDS = ("session", "agent", "project", "concept", "model", "model_source",
           "turn", "ts", "who")
_TEXT = len(_FIELDS)
_DIGEST = _TEXT + 1

_ROW_COLS = ("session, turn, ts, agent, project, concept, model, "
             "model_source, who, text, content_digest")
# Kept for focused fixtures and callers that intentionally exercise legacy rows.
_INS = ("INSERT INTO msgs(session, turn, ts, agent, project, concept, model, "
        "model_source, who, text) VALUES(?,?,?,?,?,?,?,?,?,?)")
_INS_DIGEST = ("INSERT INTO msgs(session, turn, ts, agent, project, concept, model, "
               "model_source, who, text, content_digest) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?)")
_INS_INDEXED = ("INSERT INTO msgs(session, turn, ts, agent, project, concept, model, "
                "model_source, who, text, fts_text, content_digest) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)")

_SCHEMA_SQL = """
    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE msgs(
        id INTEGER PRIMARY KEY,
        session TEXT NOT NULL, turn INTEGER, ts INTEGER,
        agent TEXT, project TEXT, concept TEXT, model TEXT, model_source TEXT,
        who TEXT, text TEXT,
        fts_text TEXT CHECK(
            fts_text IS NULL OR instr(fts_text, char(0)) = 0),
        content_digest TEXT CHECK(
            content_digest IS NULL OR
            (length(content_digest) = 4 AND
             content_digest NOT GLOB '*[^0-9a-f]*')));
    CREATE INDEX msgs_session ON msgs(session, turn);
    -- Context windows never treat tool calls as transcript rows. Keep their lookup
    -- proportional to the conversation, not to a tool-heavy session's event volume.
    CREATE INDEX msgs_transcript_session_turn ON msgs(session, turn)
        WHERE who <> 'tool';
    -- within one speaker class the score ceiling is monotonic in recency: k-way merge a few streams instead of sorting the posting list.
    CREATE INDEX msgs_who_ts ON msgs(who, coalesce(ts, 0) DESC);
    CREATE INDEX msgs_re_i_exceptions ON msgs(id) WHERE
        instr(text, 'İ') > 0 OR instr(text, 'ı') > 0
        OR instr(text, 'ſ') > 0 OR instr(text, 'K') > 0;
    -- per-session content fingerprint: how the incremental update knows which sessions moved
    CREATE TABLE session_sig(session TEXT PRIMARY KEY, sig TEXT);
    CREATE TABLE session_family(
        session TEXT PRIMARY KEY, root TEXT NOT NULL,
        side INTEGER NOT NULL CHECK(side IN (0, 1))
    ) WITHOUT ROWID;
    CREATE INDEX session_family_root ON session_family(root);
    CREATE TABLE boundary_stats(
        token TEXT PRIMARY KEY, n INTEGER NOT NULL, s INTEGER NOT NULL,
        q INTEGER NOT NULL
    ) WITHOUT ROWID;
    CREATE VIEW msgs_fts_content AS
        SELECT id, coalesce(fts_text, text) AS text FROM msgs;
    CREATE VIEW msgs_prose_fts_content AS
        SELECT id, coalesce(fts_text, text) AS text FROM msgs WHERE who <> 'tool';
    -- Positions stay (detail=full): multi-token queries walk one phrase instead of an
    -- AND-of-trigrams merge, and queries (not the indexd-delegated build) are the hot path.
    CREATE VIRTUAL TABLE msgs_fts USING fts5(
        text, content='msgs_fts_content', content_rowid='id', tokenize='trigram');
    -- Recall normally searches conversation prose first. A separate, much smaller
    -- posting list avoids walking every matching tool-output row merely to discard it.
    CREATE VIRTUAL TABLE msgs_prose_fts USING fts5(
        text, content='msgs_prose_fts_content', content_rowid='id', tokenize='trigram');
"""

# External-content FTS5 sync triggers. Created AFTER the cold build's bulk insert + one-shot
# 'rebuild' (so the cold path doesn't double-index), they then maintain the FTS automatically
# for the incremental DELETE/INSERT of just the sessions that changed.
_TRIGGER_DEFS = (
    """CREATE TRIGGER msgs_ai AFTER INSERT ON msgs BEGIN
        INSERT INTO msgs_fts(rowid, text)
            VALUES (new.id, coalesce(new.fts_text, new.text));
    END""",
    """CREATE TRIGGER msgs_ad AFTER DELETE ON msgs BEGIN
        INSERT INTO msgs_fts(msgs_fts, rowid, text)
            VALUES('delete', old.id, coalesce(old.fts_text, old.text));
    END""",
    """CREATE TRIGGER msgs_au AFTER UPDATE OF text, fts_text ON msgs
    WHEN coalesce(old.fts_text, old.text) IS NOT coalesce(new.fts_text, new.text) BEGIN
        INSERT INTO msgs_fts(msgs_fts, rowid, text)
            VALUES('delete', old.id, coalesce(old.fts_text, old.text));
        INSERT INTO msgs_fts(rowid, text)
            VALUES (new.id, coalesce(new.fts_text, new.text));
    END""",
    """CREATE TRIGGER msgs_prose_ai AFTER INSERT ON msgs WHEN new.who <> 'tool' BEGIN
        INSERT INTO msgs_prose_fts(rowid, text)
            VALUES (new.id, coalesce(new.fts_text, new.text));
    END""",
    """CREATE TRIGGER msgs_prose_ad AFTER DELETE ON msgs WHEN old.who <> 'tool' BEGIN
        INSERT INTO msgs_prose_fts(msgs_prose_fts, rowid, text)
            VALUES('delete', old.id, coalesce(old.fts_text, old.text));
    END""",
    """CREATE TRIGGER msgs_prose_au_old AFTER UPDATE OF text, fts_text, who ON msgs
    WHEN old.who <> 'tool'
         AND (coalesce(old.fts_text, old.text) IS NOT
              coalesce(new.fts_text, new.text) OR new.who = 'tool') BEGIN
        INSERT INTO msgs_prose_fts(msgs_prose_fts, rowid, text)
            VALUES('delete', old.id, coalesce(old.fts_text, old.text));
        INSERT INTO msgs_prose_fts(rowid, text)
            SELECT new.id, coalesce(new.fts_text, new.text)
            WHERE new.who <> 'tool';
    END""",
    """CREATE TRIGGER msgs_prose_au_new AFTER UPDATE OF text, fts_text, who ON msgs
    WHEN old.who = 'tool' AND new.who <> 'tool' BEGIN
        INSERT INTO msgs_prose_fts(rowid, text)
            VALUES (new.id, coalesce(new.fts_text, new.text));
    END""",
)
_TRIGGER_NAMES = ("msgs_ai", "msgs_ad", "msgs_au", "msgs_prose_ai",
                  "msgs_prose_ad", "msgs_prose_au_old", "msgs_prose_au_new")
_TRIGGERS_SQL = ";\n".join(_TRIGGER_DEFS) + ";\n"


def _fts_text_sidecar(text: str) -> str | None:
    return text.replace("\0", " ") if "\0" in text else None


def _indexed_row(row: tuple) -> tuple:
    text = row[-2]
    return (*row[:-1], _fts_text_sidecar(text), row[-1])


def _insert_index_rows(db: sqlite3.Connection, rows: Iterable[tuple]) -> None:
    db.executemany(_INS_INDEXED, (_indexed_row(row) for row in rows))


# Canonical renderers live in common: hydration must render the bytes explore's scan shows.
_snip_at = common.snip_at
_snip_spans = common.snip_spans


def _stamp() -> str:
    gen = []
    for name in _SOURCES:
        if name == "settings.json":
            # Only the tool-row toggle changes searchable rows.
            # Hook registry edits must not invalidate the transcript-derived index.
            gen.append(["tools", common.setting("tools")])
            continue
        try:
            path = common.DATA_DIR / name
            if name == "events/.generation":
                gen.append(list(common._event_file_stamp(path)))
            elif name == "sessions.jsonl":
                gen.append(common.session_family_source_stamp())
            else:
                st = path.stat()
                gen.append([st.st_mtime_ns, st.st_size])
        except OSError:
            gen.append(
                common.SESSION_FAMILY_MISSING_STAMP
                if name == "sessions.jsonl" else None
            )
    return json.dumps(gen)


def _stamp_parts(raw: str) -> "list | None":
    """Parse a source stamp into the current layout.

    The six-field layout predates the concept manifest; later layouts append
    boundary statistics and then the session-family proof. Missing historical
    slots normalize to ``None``, so absent artifacts compare equal while their
    first real publication still registers as a source move.
    """
    try:
        parts = json.loads(raw)
    except (RecursionError, TypeError, ValueError):
        return None
    if not isinstance(parts, list):
        return None
    if len(parts) == 6:
        parts = list(parts)
        parts.insert(_MANIFEST_IDX, None)
    if len(parts) == 7:
        parts = [*parts, None]
    if len(parts) == len(_SOURCES) - 1:
        parts = [*parts, None]
    return parts if len(parts) == len(_SOURCES) else None


def _stat_file_identity(st: os.stat_result) -> tuple[int, int, int, int, int]:
    return (int(st.st_size), int(st.st_mtime_ns), int(st.st_ctime_ns),
            int(st.st_dev), int(st.st_ino))


def _native_file_identity(
        identity: fileops.FileIdentity) -> tuple[int, int, int, int, int]:
    device, inode, size, modified, changed = identity
    return size, modified, changed, device, inode


def _proof_file_identity(path: Path) -> tuple[int, int, int, int, int]:
    try:
        return _native_file_identity(fileops.file_identity(path))
    except (FileNotFoundError, PermissionError):
        raise
    except OSError as error:
        raise OSError(
            f"derived artifact is not a plain regular file: {path}") from error


@contextmanager
def _open_derived_file(
        path: Path, expected: tuple[int, int, int, int, int]):
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(path, flags)
    stream = None
    try:
        opened = os.fstat(fd)
        reparse = getattr(statmod, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (not statmod.S_ISREG(opened.st_mode)
                or bool(getattr(opened, "st_file_attributes", 0) & reparse)
                or _native_file_identity(
                    fileops.file_identity_fd(fd)) != expected):
            raise OSError(f"derived artifact changed before reading: {path}")
        stream = os.fdopen(fd, "rb")
        fd = -1
        yield stream
        if (_native_file_identity(
                fileops.file_identity_fd(stream.fileno())) != expected
                or _proof_file_identity(path) != expected):
            raise OSError(f"derived artifact changed while reading: {path}")
    finally:
        if stream is not None:
            stream.close()
        elif fd >= 0:
            os.close(fd)


def _edge_hash(
        path: Path, size: int,
        identity: tuple[int, int, int, int, int] | None = None) -> int:
    expected = identity or _proof_file_identity(path)
    if expected[0] != size:
        raise OSError(f"derived artifact size changed before reading: {path}")
    edge = 512
    body = bytearray(int(size).to_bytes(8, "little"))
    with _open_derived_file(path, expected) as stream:
        body.extend(stream.read(min(size, edge)))
        if size > edge:
            tail = min(size, edge)
            stream.seek(-tail, os.SEEK_END)
            body.extend(stream.read(tail))
    value = 0xCBF29CE484222325
    for byte in body:
        value ^= byte
        value = (value * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return value


@dataclass(frozen=True)
class _DerivedWriteOwnership:
    state: str
    reason: str = ""
    adopt_legacy_db: bool = False
    sqlite_failure: sqlite3.Error | None = None
    journal_blocked: bool = False
    replace_retained_db: bool = False
    retained_reader_identity: tuple[int, int, int, int, int] | None = None
    retained_build_id: str | None = None

    @property
    def writable(self) -> bool:
        return self.state in {"current", "adoption"}


class _NonRegularSQLiteSource(OSError):
    """A path observed as a stable non-file before SQLite inspection."""


class _SQLitePublicationMoved(OSError):
    """The published main file changed across one read-open bracket."""


def _database_build_id(
        db: sqlite3.Connection | None = None, *, path: Path | None = None,
) -> tuple[str, str | None, str | sqlite3.Error]:
    """Read only the DB owner, preserving the publication path throughout."""
    if db is None:
        database = DB_PATH if path is None else Path(path)
        opened = None
        sources = tuple(
            Path(f"{database}{suffix}")
            for suffix in ("", "-journal", "-wal", "-shm")
        )
        private_snapshot = False
        try:
            try:
                main_before = _optional_sqlite_identity(sources[0])
            except _NonRegularSQLiteSource as exc:
                # A non-file main path is damaged publication shape, not evidence
                # of a moving SQLite family. The current owner may repair it atomically;
                # later main changes and non-file sidecars stay uncertain.
                return "unavailable", None, str(exc)
            if main_before is None:
                return "absent", None, ""
            before = (main_before,) + tuple(
                _optional_sqlite_identity(source) for source in sources[1:])
            if before[1] is not None and before[1][0] != 0:
                # Writer authority refuses any mid-transaction database; only the
                # lock-holding Rust writer may classify and act on this state.
                return (
                    "journal",
                    None,
                    f"corpus.db has a live rollback journal at {sources[1]}",
                )
            # Query checkpointed DELETE publication with normal read-only locking;
            # avoid corpus-scale clones. WAL may hold the only committed owner and
            # mutate -shm, so inspect it from a private main+journal/WAL snapshot.
            if before[2] is None or before[2][0] == 0:
                try:
                    opened = _open(database, 0)
                except OSError:
                    opened = _connect_read_alias(database, 0)
                    private_snapshot = True
            else:
                opened = _connect_read_alias(database, 0)
                private_snapshot = True
            rows = opened.execute(
                "SELECT value FROM meta WHERE key = 'build_id'").fetchmany(2)
            if private_snapshot and (
                    not isinstance(opened, _AliasedConnection)
                    or not opened.source_stable()):
                raise OSError(
                    "corpus.db changed while reading ownership")
            if not private_snapshot:
                after = tuple(
                    _optional_sqlite_identity(source) for source in sources)
                if not _sqlite_sources_match(before, after):
                    return (
                        "uncertain",
                        None,
                        "corpus.db changed while reading ownership",
                    )
        except FileNotFoundError:
            return "absent", None, ""
        except sqlite3.Error as exc:
            code = getattr(exc, "sqlite_errorcode", None)
            # Python 3.10 lacks sqlite_errorcode; unattributed failures cannot
            # grant repair authority from "locked" prose. Preserve the exception
            # so doctor reports unavailable instead of laundering it as corrupt.
            if not isinstance(code, int):
                message = str(exc).lower()
                if any(mark in message for mark in (
                        "file is not a database", "not a database",
                        "database disk image is malformed")):
                    # NOTADB/CORRUPT prose is decades-stable; 3.10 classifies
                    # damage the same way 3.11's numeric codes do.
                    return "unavailable", None, str(exc)
                return "uncertain", None, exc
            if (code & 0xFF) in {
                    getattr(sqlite3, "SQLITE_BUSY", 5),
                    getattr(sqlite3, "SQLITE_LOCKED", 6),
            }:
                # Preserve the coded exception for doctor's _sqlite_failure
                # classifier; string-wrapping it launders BUSY into corruption.
                return "uncertain", None, exc
            return "unavailable", None, str(exc)
        except OSError as exc:
            return "uncertain", None, str(exc)
        except (TypeError, ValueError) as exc:
            return "unavailable", None, str(exc)
        finally:
            if opened is not None:
                try:
                    opened.close()
                except sqlite3.DatabaseError:
                    pass
    else:
        try:
            rows = db.execute(
                "SELECT value FROM meta WHERE key = 'build_id'").fetchmany(2)
        except (OSError, sqlite3.DatabaseError, TypeError, ValueError) as exc:
            return "unavailable", None, str(exc)
    if not rows:
        return "unowned", None, ""
    if len(rows) != 1 or not isinstance(rows[0][0], str):
        return "unavailable", None, "corpus.db has an invalid build owner"
    owner = rows[0][0]
    if indexd_runtime._BUILD_ID_RE.fullmatch(owner) is None:
        return "unavailable", None, "corpus.db has an invalid build owner"
    return "owned", owner, ""


def _legacy_corpus_proof_matches(proof: Mapping[str, object] | None) -> bool:
    if (proof is None or set(proof) != {
            "name", "len", "modified_ns", "change_token", "edge_hash"}):
        return False
    try:
        before = _proof_file_identity(DB_PATH)
        size, modified_ns, changed, _device, _inode = before
        if (proof["name"] != "corpus.db"
                or type(proof["len"]) is not int
                or type(proof["modified_ns"]) is not int
                or type(proof["edge_hash"]) is not int
                or proof["len"] != size
                or proof["modified_ns"] != modified_ns
                or proof["edge_hash"] != _edge_hash(DB_PATH, size, before)):
            return False
        token = proof["change_token"]
        windows_before = None
        windows_metadata = False
        if _PLATFORM_NAME == "posix":
            if token != {"Metadata": _unix_change_token(changed)}:
                return False
        elif _PLATFORM_NAME == "nt":
            if (isinstance(token, dict) and set(token) == {"Metadata"}
                    and type(token["Metadata"]) is int
                    and 0 <= token["Metadata"] <= 0xFFFFFFFFFFFFFFFF):
                windows_metadata = True
                windows_before = _windows_file_state(
                    DB_PATH, include_usn=True)
                if windows_before[1] != token["Metadata"]:
                    return False
            elif (isinstance(token, dict)
                  and set(token) == {"ContentSha256"}
                  and isinstance(token["ContentSha256"], list)
                  and len(token["ContentSha256"]) == 32
                  and all(type(value) is int and 0 <= value <= 255
                          for value in token["ContentSha256"])):
                windows_before = _windows_file_state(
                    DB_PATH, include_usn=False)
                if list(_content_sha256(DB_PATH, before)) != token[
                        "ContentSha256"]:
                    return False
            else:
                return False
        elif token != {"Metadata": 0}:
            return False
        after = _proof_file_identity(DB_PATH)
        if before != after:
            return False
        if _PLATFORM_NAME == "nt":
            windows_after = _windows_file_state(
                DB_PATH, include_usn=windows_metadata)
            if windows_before != windows_after:
                return False
        return True
    except (KeyError, OSError, TypeError, ValueError):
        return False


def _retained_corpus_identity(
        retained: Mapping[str, object] | None, owner: str | None, *,
        exact: bool = False,
) -> tuple[int, int, int, int, int] | None:
    if (not indexd_runtime._valid_retained_corpus(retained)
            or retained.get("build_id") != owner):
        return None
    proof = retained["proof"]
    reader = retained["reader_identity"]
    expected = tuple(reader[field] for field in (
        "len", "modified_ns", "changed_ns", "device", "inode"))
    try:
        before = _proof_file_identity(DB_PATH)
        if (before != expected
                or proof["edge_hash"] != _edge_hash(DB_PATH, before[0], before)
                or _proof_file_identity(DB_PATH) != expected):
            return None
    except (KeyError, OSError, TypeError, ValueError):
        return None
    if exact and not _legacy_corpus_proof_matches(proof):
        return None
    return expected


def _derived_write_ownership(
        db: sqlite3.Connection | None = None, *, for_write: bool = False,
        exact_retained: bool = False,
) -> _DerivedWriteOwnership:
    """Compose the exact binary, Rust anchor, parse cache, and corpus.db owner."""
    try:
        if for_write:
            indexd_runtime.assert_python_runtime_unchanged()
        current_build = indexd_runtime.derived_writer_build_id(
            require_binary=True)
    except OSError as exc:
        return _DerivedWriteOwnership(
            "refused",
            "derived writer identity is unavailable because the ingest "
            f"binary cannot be verified ({exc})")
    anchor = indexd_runtime.derived_owner_info(current_build)
    cache = indexd_runtime.ingest_cache_owner_info(current_build)
    if anchor.state in {"foreign", "unavailable"}:
        return _DerivedWriteOwnership("refused", anchor.reason)
    if cache.state == "foreign":
        return _DerivedWriteOwnership("refused", cache.reason)
    if anchor.state == "current":
        # The anchor proves family ownership, but an ownerless or unreadable cache
        # means an older writer resumed. Only absence is the publication crash window;
        # a matching owner prefix may retain corrupt bytes for same-build repair.
        if cache.state not in {"absent", "current"}:
            reason = cache.reason or (
                f"derived stores owned-by {anchor.build_id}, but the existing "
                f"parse cache is {cache.state} and has no matching writing-build "
                "identity")
            return _DerivedWriteOwnership("refused", reason)
        # Foreground reads trust the anchor without an ownership probe. Writer
        # preflight must inspect the DB owner: anchor and cache may both be current
        # after a foreign writer replaced corpus.db, including via WAL.
        if not for_write and anchor.retained_corpus_db is None:
            return _DerivedWriteOwnership("current")
        owner_state = "current"
    else:
        # Without Rust's anchor, only a current owner-bearing cache may authorize
        # a new DB. Ownerless or unreadable cache bytes are not proof; incomplete
        # migration must not leave a DB for a later build to split from.
        if cache.state != "current":
            reason = cache.reason or (
                "derived-store ownership is not established: no current "
                "writing-build anchor or parse-cache identity")
            return _DerivedWriteOwnership("refused", reason)
        owner_state = "adoption"
    db_state, db_owner, db_error = _database_build_id(db)
    if db_state == "owned":
        if db_owner != current_build:
            retained_identity = (
                _retained_corpus_identity(
                    anchor.retained_corpus_db, db_owner,
                    exact=exact_retained)
                if anchor.state == "current" else None
            )
            if retained_identity is not None:
                return _DerivedWriteOwnership(
                    owner_state, replace_retained_db=True,
                    retained_reader_identity=retained_identity,
                    retained_build_id=db_owner)
            return _DerivedWriteOwnership(
                "refused",
                f"corpus.db owned-by {db_owner}; "
                f"this build is {current_build}")
        return _DerivedWriteOwnership(owner_state)
    if db_state == "absent":
        return _DerivedWriteOwnership(owner_state)
    if db_state == "unowned":
        if _legacy_corpus_proof_matches(anchor.legacy_corpus_db):
            return _DerivedWriteOwnership("adoption", adopt_legacy_db=True)
        if anchor.state == "absent":
            return _DerivedWriteOwnership(
                "refused",
                "corpus.db has no writing-build identity and no durable "
                "ownership anchor binds it as the one legacy publication")
        return _DerivedWriteOwnership(
            "post-adoption-clobber",
            f"derived stores owned-by {anchor.build_id}, but corpus.db has "
            "no writing-build identity and does not match the one legacy "
            "publication authorized by the ownership anchor; automatic "
            "repair is disabled because replacing corpus.db could destroy "
            "the last-good searchable snapshot; run `agrep doctor` for the "
            "safe backup-and-reindex remedy")
    if db_state in {"uncertain", "journal"}:
        sqlite_failure = (
            db_error if isinstance(db_error, sqlite3.Error) else None)
        return _DerivedWriteOwnership(
            "refused",
            str(db_error)
            if db_error
            else "corpus.db ownership changed during writer preflight",
            sqlite_failure=sqlite_failure,
            journal_blocked=db_state == "journal")
    if owner_state == "adoption":
        # A current owner-bearing cache witnesses an anchor-publication crash.
        # Readable foreign owners were refused; unreadable bytes retain the
        # same-build atomic rebuild path.
        return _DerivedWriteOwnership("adoption")
    # The durable current anchor keeps the pre-R8 same-build repair path:
    # connect() rebuilds a damaged DB to a temp file and atomically replaces it.
    return _DerivedWriteOwnership("current")


class _DerivedOwnershipRefusal(RuntimeError):
    pass


def _adopt_legacy_database_owner() -> bool:
    """Consume the exact legacy proof by adding this build ID transactionally."""
    if _protected_derived_target(DB_PATH):
        return False
    ownership = _derived_write_ownership(for_write=True)
    if not ownership.adopt_legacy_db:
        return ownership.state == "current"
    db = None
    try:
        current_build = indexd_runtime.derived_writer_build_id(
            require_binary=True)
    except OSError:
        return False
    try:
        db = sqlite3.connect(DB_PATH)
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("BEGIN IMMEDIATE")
        state, owner, _error = _database_build_id(db)
        if state == "owned":
            if owner != current_build:
                raise _DerivedOwnershipRefusal(
                    f"corpus.db owned-by {owner}; "
                    f"this build is {current_build}")
        elif state != "unowned":
            raise _DerivedOwnershipRefusal(
                "corpus.db ownership changed during legacy adoption")
        else:
            current = _derived_write_ownership(db, for_write=True)
            if not current.adopt_legacy_db:
                raise _DerivedOwnershipRefusal(
                    current.reason
                    or "corpus.db changed during legacy ownership adoption")
            db.execute(
                "INSERT INTO meta(key, value) VALUES('build_id', ?)",
                (current_build,))
        db.commit()
    except (OSError, sqlite3.DatabaseError, _DerivedOwnershipRefusal):
        if db is not None:
            try:
                db.rollback()
            except sqlite3.DatabaseError:
                pass
        return False
    finally:
        if db is not None:
            db.close()
    state, owner, _error = _database_build_id()
    return state == "owned" and owner == current_build


def _content_sha256(
        path: Path,
        identity: tuple[int, int, int, int, int] | None = None) -> bytes:
    expected = identity or _proof_file_identity(path)
    digest = hashlib.sha256()
    with _open_derived_file(path, expected) as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest()


def _read_derived_file(
        path: Path, max_bytes: int,
) -> tuple[tuple[int, int, int, int, int], bytes]:
    identity = _proof_file_identity(path)
    with _open_derived_file(path, identity) as stream:
        raw = stream.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise OSError(f"derived artifact exceeds {max_bytes} bytes: {path}")
    return identity, raw


def _optional_sqlite_identity(path: Path):
    try:
        return _sqlite_file_identity(path)
    except FileNotFoundError:
        return None


def _sqlite_file_identity(path: Path) -> tuple[int, int, int, int, int]:
    info = path.lstat()
    reparse = getattr(statmod, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (not statmod.S_ISREG(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0) & reparse)):
        raise _NonRegularSQLiteSource(
            f"SQLite source is not a plain regular file: {path}")
    # Windows st_ctime is creation time, not NTFS change identity. fileops uses
    # a no-follow handle and FILE_BASIC_INFO.ChangeTime, so same-size or
    # restored-mtime edits cannot evade the family snapshot bracket.
    return _native_file_identity(fileops.file_identity(path))


@contextmanager
def _open_sqlite_file(
        path: Path, expected: tuple[int, int, int, int, int]):
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(path, flags)
    stream = None
    try:
        opened = os.fstat(fd)
        reparse = getattr(statmod, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        opened_identity = _native_file_identity(
            fileops.file_identity_fd(fd))
        if (not statmod.S_ISREG(opened.st_mode)
                or bool(getattr(opened, "st_file_attributes", 0) & reparse)):
            raise _NonRegularSQLiteSource(
                f"SQLite source is not a plain regular file: {path}")
        if opened_identity != expected:
            raise _SQLitePublicationMoved(
                f"SQLite source changed before reading: {path}")
        stream = os.fdopen(fd, "rb")
        fd = -1
        yield stream
        after = _native_file_identity(
            fileops.file_identity_fd(stream.fileno()))
        current = _sqlite_file_identity(path)
        if after != expected or current != expected:
            raise _SQLitePublicationMoved(
                f"SQLite source changed while reading: {path}")
    finally:
        if stream is not None:
            stream.close()
        elif fd >= 0:
            os.close(fd)


def _sqlite_alias_tempdir(_path: Path):
    # Never stage beside a live or foreign derived store. System temp is
    # private scratch and lets SQLite create snapshot-local SHM safely.
    return tempfile.TemporaryDirectory(prefix=".agrep-sqlite-")


def _try_clone_sqlite_file(source_fd: int, target: Path) -> bool:
    if sys.platform == "darwin":
        import ctypes

        library = ctypes.CDLL(None, use_errno=True)
        clone = getattr(library, "fclonefileat", None)
        if clone is None:
            return False
        clone.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_char_p,
                          ctypes.c_uint32)
        clone.restype = ctypes.c_int
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            if clone(source_fd, directory_fd, os.fsencode(target.name), 0) == 0:
                return True
        finally:
            os.close(directory_fd)
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        return False
    if sys.platform.startswith("linux"):
        import fcntl

        flags = (os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                 | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        target_fd = os.open(target, flags, 0o600)
        cloned = False
        try:
            fcntl.ioctl(target_fd, 0x40049409, source_fd)
            cloned = True
            return True
        except OSError:
            return False
        finally:
            os.close(target_fd)
            if not cloned and target.exists():
                target.unlink()
    return False


def _copy_sqlite_file(
        source, target: Path, *, max_copy_bytes: int | None = None) -> int:
    """Clone one SQLite member, or byte-copy it within the remaining budget.

    The routine bound protects actual I/O, not the logical size of a reflink.
    APFS ``fclonefileat`` and Linux ``FICLONE`` are constant-space snapshots for
    this purpose, so rejecting a corpus before trying them turns the fast safe
    path into a JSONL fallback merely because the database is large.

    Return the number of bytes charged to the byte-copy budget (zero for a
    clone).  A refused fallback never creates a partial target.
    """
    if _try_clone_sqlite_file(source.fileno(), target):
        return 0
    size = int(os.fstat(source.fileno()).st_size)
    if max_copy_bytes is not None and size > max(0, int(max_copy_bytes)):
        raise AliasCloneRefused(
            f"the SQLite alias byte-copy fallback needs {size} bytes; this "
            f"caller has {max(0, int(max_copy_bytes))} bytes left")
    source.seek(0)
    flags = (os.O_WRONLY | os.O_CREAT | os.O_TRUNC
             | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(target, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            fd = -1
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                output.write(chunk)
    finally:
        if fd >= 0:
            os.close(fd)
    return size


def _copy_sqlite_snapshot(
        alias: Path, sources, identities,
        max_copy_bytes: int | None = None) -> None:
    with ExitStack() as stack:
        opened = []
        for source, identity in zip(sources[:3], identities[:3]):
            opened.append(
                None if identity is None
                else stack.enter_context(_open_sqlite_file(source, identity)))
        copied = 0
        for suffix, stream in zip(("", "-journal", "-wal"), opened):
            if stream is not None:
                remaining = (
                    None if max_copy_bytes is None
                    else max(0, int(max_copy_bytes) - copied))
                copied += _copy_sqlite_file(
                    stream, Path(f"{alias}{suffix}"),
                    max_copy_bytes=remaining)


def _sqlite_sources_match(expected, observed) -> bool:
    return tuple(expected) == tuple(observed)


def _sqlite_file_evidence(
        path: Path, identity: tuple[int, int, int, int, int]) -> bytes:
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(path, flags)
    stream = None
    try:
        opened = os.fstat(fd)
        reparse = getattr(statmod, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        opened_identity = _native_file_identity(
            fileops.file_identity_fd(fd))
        if (not statmod.S_ISREG(opened.st_mode)
                or bool(getattr(opened, "st_file_attributes", 0) & reparse)
                or opened_identity != identity):
            raise OSError(f"SQLite source changed before inspection: {path}")
        stream = os.fdopen(fd, "rb")
        fd = -1
        edge = 4096
        digest = hashlib.sha256()
        digest.update(identity[0].to_bytes(8, "little"))
        digest.update(stream.read(min(identity[0], edge)))
        if identity[0] > edge:
            stream.seek(-min(identity[0], edge), os.SEEK_END)
            digest.update(stream.read(edge))
        after = _native_file_identity(
            fileops.file_identity_fd(stream.fileno()))
        current = _sqlite_file_identity(path)
        if after != identity or current != identity:
            raise OSError(f"SQLite source changed during inspection: {path}")
        # Hot reads bound SQLite checks to header/tail and data_version.
        # Doctor's quick_check owns arbitrary interior-page corruption.
        return digest.digest()
    finally:
        if stream is not None:
            stream.close()
        elif fd >= 0:
            os.close(fd)


def _sqlite_source_evidence(sources, identities):
    return tuple(
        None if identity is None else _sqlite_file_evidence(source, identity)
        for source, identity in zip(sources[:3], identities[:3])
    )


class AliasCloneRefused(OSError):
    """The alias snapshot's clone fallback would exceed its byte-copy bound."""


# Routine probes bound only the byte-copy fallback; filesystem clones stay
# eligible, while foreign readers keep the unbounded compatibility default.
_ROUTINE_ALIAS_CLONE_MAX_BYTES = 64 * 1024 * 1024
_CONTENDED_READER_WAIT_MS = 250
_SQLITE_ERRORS_HAVE_CODES = sys.version_info >= (3, 11)


def _sqlite_alias_snapshot(path: Path, max_clone_bytes: int | None = None):
    suffixes = ("", "-journal", "-wal", "-shm")
    sources = tuple(Path(f"{path}{suffix}") for suffix in suffixes)
    before = tuple(_optional_sqlite_identity(source) for source in sources)
    if before[0] is None:
        raise FileNotFoundError(path)
    cleanup = _sqlite_alias_tempdir(path)
    alias = Path(cleanup.name) / "corpus.db"
    try:
        # Hard links are not read-only: creating one changes source inode
        # metadata, and a linked WAL/SHM exposes live bytes to SQLite. A clone
        # or byte copy always gives the snapshot a distinct private inode.
        copy_identities = tuple(
            _optional_sqlite_identity(source) for source in sources)
        if not _sqlite_sources_match(before, copy_identities):
            raise OSError("SQLite source changed before copying")
        _copy_sqlite_snapshot(
            alias, sources, copy_identities,
            max_copy_bytes=max_clone_bytes)
        stable = tuple(_optional_sqlite_identity(source) for source in sources)
        if not _sqlite_sources_match(before, stable):
            raise OSError("SQLite source changed before inspection")
        evidence = _sqlite_source_evidence(sources, stable)
        return cleanup, alias, sources, stable, evidence
    except Exception:
        cleanup.cleanup()
        raise


class _PublishedConnection(sqlite3.Connection):
    _source_identity = None
    _source_build_id = None
    _retained_snapshot = False
    _source_stamp_current = None


class _AliasedConnection(_PublishedConnection):
    _alias_cleanup = None
    _alias_sources = ()
    _alias_stable = ()
    _alias_evidence = ()
    _alias_data_version = None

    def source_stable(self) -> bool:
        try:
            observed = tuple(
                _optional_sqlite_identity(source) for source in self._alias_sources)
            if not _sqlite_sources_match(self._alias_stable, observed):
                return False
            evidence = _sqlite_source_evidence(self._alias_sources, observed)
            data_version = self.execute("PRAGMA data_version").fetchone()
            return (evidence == self._alias_evidence
                    and data_version is not None
                    and data_version[0] == self._alias_data_version)
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return False

    def close(self):
        cleanup, self._alias_cleanup = self._alias_cleanup, None
        self._alias_sources = ()
        self._alias_stable = ()
        self._alias_evidence = ()
        self._alias_data_version = None
        try:
            return super().close()
        finally:
            if cleanup is not None:
                cleanup.cleanup()


def _connect_read_alias(
        path: Path, timeout_s: float,
        max_clone_bytes: int | None = None) -> sqlite3.Connection:
    cleanup, alias, sources, stable, evidence = _sqlite_alias_snapshot(
        path, max_clone_bytes)
    db = None
    try:
        if stable[1] is not None and stable[1][0] != 0:
            # Recover only the private clone so the source remains untouched
            # and every returned handle can stay read-only.
            recovery = None
            try:
                recovery = sqlite3.connect(
                    alias.resolve().as_uri() + "?mode=rw", uri=True,
                    timeout=timeout_s)
                # Entering the pager forces hot-journal recovery when needed;
                # a non-hot journal is harmless and remains snapshot-local.
                recovery.execute("PRAGMA schema_version").fetchone()
            finally:
                if recovery is not None:
                    recovery.close()
        db = sqlite3.connect(
            alias.resolve().as_uri() + "?mode=ro", uri=True,
            timeout=timeout_s, factory=_AliasedConnection)
        data_version = db.execute("PRAGMA data_version").fetchone()
        observed = tuple(_optional_sqlite_identity(source) for source in sources)
        if (not _sqlite_sources_match(stable, observed)
                or _sqlite_source_evidence(sources, observed) != evidence
                or data_version is None):
            raise OSError("SQLite source changed while opening")
        db._alias_cleanup = cleanup
        db._alias_sources = sources
        db._alias_stable = stable
        db._alias_evidence = evidence
        db._alias_data_version = data_version[0]
        db._source_identity = stable[0]
        return db
    except Exception:
        if db is not None:
            db.close()
        cleanup.cleanup()
        raise


def _connect_read_direct(
        path: Path, timeout_s: float = 0) -> sqlite3.Connection:
    path = Path(path)
    deadline = time.monotonic() + max(0.0, timeout_s)
    for attempt in range(2):
        remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
        try:
            return _open(path, remaining_ms)
        except _SQLitePublicationMoved:
            if attempt == 0:
                continue
            raise
    raise AssertionError("unreachable")


def _connect_read_snapshot(
        path: Path, timeout_s: float = 0, *,
        max_clone_bytes: int | None = None,
) -> sqlite3.Connection:
    """Open a published SQLite snapshot without a sidecar in its live parent.

    Checkpointed and live non-hot DELETE publications use SQLite's read-only
    path. Dead hot journals, WAL, and damaged states are inspected from a
    coherent system-temporary byte snapshot, never recovered against the source
    pathname. `max_clone_bytes` bounds only the byte-copy fallback after a
    filesystem clone has been attempted; a caller inside a routine budget gets
    AliasCloneRefused instead of a corpus-scale copy when cloning is unavailable,
    while the default stays unbounded for proof readers.
    """
    path = Path(path)
    try:
        return _connect_read_direct(path, timeout_s)
    except _SQLitePublicationMoved:
        raise
    except sqlite3.DatabaseError as error:
        code = getattr(error, "sqlite_errorcode", None)
        if (query_database_error_kind(error) == "transient"
                or (type(code) is not int and _SQLITE_ERRORS_HAVE_CODES)):
            raise
        if sqlite_failure_is_contention(error):
            # A locked live database must surface busy instead of quietly
            # reading through the byte-clone fallback (3.10 has no code).
            raise
        # Python 3.10 cannot expose the numeric cause. Its safe compatibility
        # path recovers only a stable private snapshot, never the source family.
        return _connect_read_alias(
            path, max(0.0, timeout_s), max_clone_bytes)
    except OSError:
        return _connect_read_alias(
            path, max(0.0, timeout_s), max_clone_bytes)


def _unix_change_token(ctime_ns: int) -> int:
    secs, nanos = divmod(int(ctime_ns), 1_000_000_000)
    unsigned = secs & 0xFFFFFFFFFFFFFFFF
    rotated = ((unsigned << 17) | (unsigned >> 47)) & 0xFFFFFFFFFFFFFFFF
    return rotated ^ nanos


def _rust_windows_usn_token(usn: int) -> int:
    value = int(usn) & 0xFFFFFFFFFFFFFFFF
    rotated = ((value << 7) | (value >> 57)) & 0xFFFFFFFFFFFFFFFF
    return rotated ^ 0x55534E5F46494C45


_WINDOWS_IDENTITY_API = None


def _windows_identity_api():
    global _WINDOWS_IDENTITY_API
    if _WINDOWS_IDENTITY_API is not None:
        return _WINDOWS_IDENTITY_API
    import ctypes
    from ctypes import wintypes

    class FileBasicInfo(ctypes.Structure):
        _fields_ = (
            ("creation", ctypes.c_longlong),
            ("access", ctypes.c_longlong),
            ("write", ctypes.c_longlong),
            ("change", ctypes.c_longlong),
            ("attributes", wintypes.DWORD),
        )

    class ReadFileUsnData(ctypes.Structure):
        _fields_ = (("minimum", wintypes.WORD), ("maximum", wintypes.WORD))

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetFileInformationByHandleEx.argtypes = (
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    )
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel32.DeviceIoControl.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, wintypes.LPDWORD, wintypes.LPVOID,
    )
    kernel32.DeviceIoControl.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    _WINDOWS_IDENTITY_API = (
        ctypes, wintypes, kernel32, FileBasicInfo, ReadFileUsnData,
    )
    return _WINDOWS_IDENTITY_API


def _windows_file_state(path: Path, *, include_usn: bool) -> tuple[int, int | None]:
    ctypes, wintypes, kernel32, basic_type, request_type = _windows_identity_api()
    handle = kernel32.CreateFileW(
        str(path), 0x0080, 0x00000007, None, 3, 0x00200000, None,
    )
    if handle in (None, ctypes.c_void_p(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        basic = basic_type()
        if not kernel32.GetFileInformationByHandleEx(
                handle, 0, ctypes.byref(basic), ctypes.sizeof(basic)):
            raise ctypes.WinError(ctypes.get_last_error())
        if basic.attributes & 0x00000400:
            raise OSError("derived artifact became a reparse point")
        if not include_usn:
            return int(basic.change), None
        request = request_type(2, 3)
        output = (ctypes.c_ubyte * 1024)()
        returned = wintypes.DWORD()
        if not kernel32.DeviceIoControl(
                handle, 0x000900EB, ctypes.byref(request), ctypes.sizeof(request),
                output, ctypes.sizeof(output), ctypes.byref(returned), None):
            raise ctypes.WinError(ctypes.get_last_error())
        raw = bytes(output[:returned.value])
        if len(raw) < 8:
            raise OSError("invalid file USN response")
        record_length = int.from_bytes(raw[0:4], "little")
        major = int.from_bytes(raw[4:6], "little")
        usn_offset = 24 if major == 2 else 40 if major == 3 else -1
        if (usn_offset < 0 or record_length > len(raw)
                or record_length < usn_offset + 8):
            raise OSError("invalid file USN response")
        usn = int.from_bytes(raw[usn_offset:usn_offset + 8], "little", signed=True)
        return int(basic.change), _rust_windows_usn_token(usn)
    finally:
        kernel32.CloseHandle(handle)


def _derived_failure(code: str, detail: str) -> dict:
    return {"state": code, "detail": detail, "corpus_age_s": None}


# Routine verdict when a Windows no-USN ContentSha256 proof would cost a
# complete content hash. Deliberately not status-deferred: it never claims
# failing/unchecked, so it can never overwrite a real recorded failure.
GENERATION_VERIFICATION_DEFERRED = "generation-verification-deferred"


def _validate_derived_file(
        root: Path, expected: dict,
        validated: dict[str, tuple[tuple[int, int, int, int, int], object]] | None = None,
        *, routine: bool = False,
) -> str | None:
    name = expected.get("name")
    if not isinstance(name, str) or name not in _DERIVED_PROOF_NAMES:
        return "derived proof names an unsupported artifact"
    path = root / name
    try:
        lst = path.lstat()
        if not statmod.S_ISREG(lst.st_mode) or path.is_symlink():
            return f"{name} is not a regular file"
        before = _proof_file_identity(path)
        if (type(expected.get("len")) is not int
                or type(expected.get("modified_ns")) is not int
                or type(expected.get("edge_hash")) is not int):
            return f"{name} has malformed proof metadata"
        if before[0] != expected["len"] or before[1] != expected["modified_ns"]:
            return f"{name} does not match its committed generation"
        token = expected.get("change_token")
        windows_before = None
        windows_metadata = False
        if _PLATFORM_NAME == "nt":
            if (isinstance(token, dict) and set(token) == {"Metadata"}
                    and type(token["Metadata"]) is int
                    and 0 <= token["Metadata"] <= 0xFFFFFFFFFFFFFFFF):
                windows_metadata = True
            elif isinstance(token, dict) and set(token) == {"ContentSha256"}:
                windows_before = _windows_file_state(path, include_usn=False)
            else:
                return f"{name} has malformed change identity"
        if _edge_hash(path, before[0], before) != expected["edge_hash"]:
            return f"{name} content edges do not match its committed generation"
        if _PLATFORM_NAME == "posix":
            if token != {"Metadata": _unix_change_token(before[2])}:
                return f"{name} change identity does not match its committed generation"
        elif _PLATFORM_NAME == "nt":
            if not windows_metadata:
                digest = token.get("ContentSha256")
                if (not isinstance(digest, list) or len(digest) != 32
                        or any(type(value) is not int or not 0 <= value <= 255
                               for value in digest)):
                    return f"{name} has malformed content identity"
                if routine:
                    # Without a USN this proof needs a complete content hash
                    # of the artifact set - deep-tier work, never routine.
                    return GENERATION_VERIFICATION_DEFERRED
                if list(_content_sha256(path, before)) != digest:
                    return f"{name} content identity does not match its committed generation"
        elif token != {"Metadata": 0}:
            return f"{name} change identity does not match its committed generation"
        after = _proof_file_identity(path)
        windows_after = (
            _windows_file_state(path, include_usn=windows_metadata)
            if _PLATFORM_NAME == "nt" else None
        )
    except OSError as error:
        return f"{name} cannot be verified ({error})"
    if (_PLATFORM_NAME == "nt" and windows_metadata
            and windows_after[1] != token["Metadata"]):
        return f"{name} change identity does not match its committed generation"
    if (before != after or (_PLATFORM_NAME == "nt"
                            and not windows_metadata
                            and windows_before != windows_after)):
        return f"{name} changed during generation verification"
    if validated is not None:
        validated[name] = (after, windows_after)
    return None


def _derived_publication_health(
        now: float | None = None, *, routine: bool = False) -> dict:
    root = common.DATA_DIR
    signal = root / ".ingest.sig"
    proof_path = root / ".derived_generation.json"
    published = False
    for name in (".ingest.sig", ".derived_generation.json", "messages.jsonl"):
        try:
            (root / name).lstat()
            published = True
            break
        except FileNotFoundError:
            continue
        except OSError as error:
            return _derived_failure(
                "generation-unavailable",
                f"the corpus generation cannot be inspected ({error})")
    if not published:
        return _derived_failure("never-built", "no corpus generation is published")
    try:
        signal_identity, signal_before = _read_derived_file(
            signal, _INGEST_SIGNATURE_MAX_BYTES)
        proof_identity, proof_before = _read_derived_file(
            proof_path, _DERIVED_PROOF_MAX_BYTES)
        proof = json.loads(proof_before.decode("utf-8"))
    except FileNotFoundError:
        return _derived_failure(
            "torn-generation", "the corpus generation commit is incomplete")
    except (OSError, RecursionError, UnicodeError, ValueError, TypeError) as error:
        return _derived_failure(
            "torn-generation", f"the corpus generation proof is unreadable ({error})")
    try:
        signature = signal_before.decode("utf-8").strip()
    except UnicodeError as error:
        return _derived_failure(
            "torn-generation", f"the ingest signature is unreadable ({error})")
    files = proof.get("files") if isinstance(proof, dict) else None
    if (not isinstance(proof, dict)
            or proof.get("version") != _DERIVED_PROOF_VERSION
            or not signature or proof.get("signature") != signature
            or not isinstance(files, list)
            or len(files) != len(_DERIVED_PROOF_NAMES)):
        return _derived_failure(
            "torn-generation", "the corpus generation proof does not match the ingest commit")
    names = [row.get("name") for row in files if isinstance(row, dict)]
    if len(names) != len(files) or set(names) != set(_DERIVED_PROOF_NAMES):
        return _derived_failure(
            "torn-generation", "the corpus generation proof has an invalid artifact census")
    validated = {}
    for row in files:
        problem = _validate_derived_file(root, row, validated, routine=routine)
        if problem == GENERATION_VERIFICATION_DEFERRED:
            return {
                "state": GENERATION_VERIFICATION_DEFERRED,
                "detail": (
                    "full content verification of the derived generation is "
                    "deferred on the routine tier; run `agrep doctor --deep` "
                    "for the complete proof"),
                "corpus_age_s": _signal_age(signal_identity, now),
            }
        if problem:
            return _derived_failure("torn-generation", problem)
    try:
        for row in files:
            name = row["name"]
            identity, windows_state = validated[name]
            if _proof_file_identity(root / name) != identity:
                return _derived_failure(
                    "generation-moving",
                    f"{name} changed after generation verification")
            if _PLATFORM_NAME == "nt":
                metadata = (
                    isinstance(row.get("change_token"), dict)
                    and set(row["change_token"]) == {"Metadata"}
                )
                if _windows_file_state(
                        root / name, include_usn=metadata) != windows_state:
                    return _derived_failure(
                        "generation-moving",
                        f"{name} changed after generation verification")
    except (KeyError, OSError, TypeError, ValueError) as error:
        return _derived_failure(
            "generation-moving",
            f"the corpus generation changed after verification ({error})")
    try:
        signal_after_identity, signal_after = _read_derived_file(
            signal, _INGEST_SIGNATURE_MAX_BYTES)
        proof_after_identity, proof_after = _read_derived_file(
            proof_path, _DERIVED_PROOF_MAX_BYTES)
        if (signal_before != signal_after
                or signal_identity != signal_after_identity
                or proof_identity != proof_after_identity
                or proof_before != proof_after):
            return _derived_failure(
                "generation-moving", "the corpus generation changed during verification")
    except OSError as error:
        return _derived_failure(
            "generation-moving", f"the corpus generation changed during verification ({error})")
    return {"state": "ready", "detail": "derived generation is committed",
            "corpus_age_s": _signal_age(signal_identity, now)}


def _signal_age(
        signal_identity: tuple[int, int, int, int, int],
        now: float | None) -> float | None:
    observed = time.time() if now is None else float(now)
    age = observed - signal_identity[1] / 1_000_000_000
    return round(age, 3) if math.isfinite(age) and age >= 0.0 else None


def search_generation_health(
        now: float | None = None, *, routine: bool = False) -> dict:
    """Cheap search-path proof over the same source and DB generations doctor checks.

    The default is the complete verification (`agrep doctor --deep`'s
    semantics). `routine=True` is the interactive tier: it defers the Windows
    full-content proof as GENERATION_VERIFICATION_DEFERRED and bounds the
    alias snapshot clone instead of paying corpus-scale work mid-search.
    """
    publication = _derived_publication_health(now, routine=routine)
    if publication["state"] != "ready":
        return publication
    family_stamp = common.session_family_source_stamp(common.DATA_DIR)
    if family_stamp is None:
        return _derived_failure(
            "torn-generation", "the session-family generation is incomplete or unstable")
    path = common.DATA_DIR / "corpus.db"
    try:
        db_stat = path.lstat()
    except FileNotFoundError:
        return publication
    except OSError as error:
        return {**_derived_failure(
            "generation-unavailable", f"the search database cannot be inspected ({error})"),
            "corpus_age_s": publication["corpus_age_s"]}
    if not statmod.S_ISREG(db_stat.st_mode) or path.is_symlink():
        return {**_derived_failure(
            "generation-unavailable", "the search database is not a regular file"),
            "corpus_age_s": publication["corpus_age_s"]}
    db = None
    try:
        before = _stamp()
        db = (
            _connect_read_snapshot(
                path, 0,
                max_clone_bytes=_ROUTINE_ALIAS_CLONE_MAX_BYTES)
            if routine else _connect_read_alias(path, 0)
        )
        db.execute("PRAGMA query_only=ON")
        meta = dict(db.execute(
            "SELECT key, value FROM meta "
            "WHERE key IN ('schema', 'stamp', 'family_stamp')"))
        if (isinstance(db, _AliasedConnection)
                and not db.source_stable()):
            raise OSError("the search database changed during verification")
        after = _stamp()
    except (OSError, sqlite3.Error, TypeError, ValueError) as error:
        return {**_derived_failure(
            "generation-unavailable", f"the search generation cannot be verified ({error})"),
            "corpus_age_s": publication["corpus_age_s"]}
    finally:
        if db is not None:
            try:
                db.close()
            except sqlite3.Error:
                pass
    if not _stamps_equal(before, after):
        return {**_derived_failure(
            "generation-moving", "the source generation changed during verification"),
            "corpus_age_s": publication["corpus_age_s"]}
    if (meta.get("schema") != _SCHEMA
            or meta.get("family_stamp") != family_stamp
            or not _stamps_equal(meta.get("stamp", ""), after)):
        return {**_derived_failure(
            "search-index-stale", "the search database does not match the committed corpus"),
            "corpus_age_s": publication["corpus_age_s"]}
    if _query_rebuild_marker_applies(path):
        return {**_derived_failure(
            "search-index-stale", "a full search-database rebuild is pending"),
            "corpus_age_s": publication["corpus_age_s"]}
    return publication


class RepairPlan(NamedTuple):
    """What the daemon must rebuild, in the scope that rebuilds it."""
    code: str = ""      # "" when nothing derived needs rebuilding
    scope: str = ""     # full (reparse the sources) | publish (reproject them)
    detail: str = ""


NO_REPAIR = RepairPlan()

# Derived states the daemon rebuilds unattended, and the scope each needs.
# Absent means healthy, a race, the ownership lockout, or never-built - an
# unbuilt corpus is not damage and must never reschedule a reparse.
_REPAIR_SCOPE = MappingProxyType({
    "torn-generation": "full",
    "generation-unavailable": "full",
    "proof-damaged": "full",
    "search-index-stale": "publish",
    "generation-relocated": "publish",
})


def repairs_itself(state: str) -> bool:
    """True when this derived state is agrep's own work to undo.

    The one predicate the repair loop and every surface share: the daemon
    rebuilds from this table, and a surface may not report a fault the daemon
    is about to erase. Emitter and checker, one artifact."""
    return str(state) in _REPAIR_SCOPE


# Search-db states a background publication closes by itself; the reader
# hears about none. Deliberately absent: the ownership lockout, a foreign
# owner, and `busy` (a writer mid-flight, not a fault).
SELF_REPAIRING_DB_STATES = frozenset({
    "missing", "stale", "rebuild-pending", "corrupt", "unreadable",
    "not-verified",
})
# The census surface's names for faults the repair table already owns -
# derived from it, not restated, so a row can never outlive the repair that
# erases it: schedulable names stop rendering on the same commit.
SELF_REPAIRING_CORPUS_STATES = frozenset(
    name for name in
    ("proof-damaged", "torn-generation", "generation-unavailable",
     "generation-relocated")
    if repairs_itself(name))

# Verdicts that settle by themselves: a publication caught mid-flight, and
# a proof whose expensive half routine declined. Alarming on evidence that
# agrep is working is the sharpest way to violate law 3.
_SELF_CLEARING_STATES = frozenset({
    "generation-moving", GENERATION_VERIFICATION_DEFERRED,
})


def state_self_clears(code: object) -> bool:
    """True when a derived verdict names work the daemon already owns, or a
    race that re-observation settles. Neither is the reader's situation."""
    name = str(code or "")
    return name in _REPAIR_SCOPE or name in _SELF_CLEARING_STATES


def _relocated_with_contents_intact() -> bool:
    """True when every artifact still carries its committed length and content
    edges, and only its timestamps moved.

    Restoring a backup, migrating to a new Mac, or copying the data directory
    to look inside it gives every file a fresh mtime and ctime while the bytes
    are untouched. The proof binds both, so it fails - but the corpus is fine,
    and reparsing every source to learn that would be the wrong answer. An
    incremental publish re-commits the proof from the parse cache in a moment.
    Length or edge drift is real damage and never reaches here."""
    root = common.DATA_DIR
    try:
        proof = json.loads(
            (root / ".derived_generation.json").read_text(encoding="utf-8"))
        files = proof["files"]
    except (OSError, ValueError, TypeError, KeyError):
        return False
    if not isinstance(files, list) or len(files) != len(_DERIVED_PROOF_NAMES):
        return False
    moved = False
    for row in files:
        if not isinstance(row, dict) or row.get("name") not in _DERIVED_PROOF_NAMES:
            return False
        path = root / row["name"]
        try:
            identity = _proof_file_identity(path)
            if identity[0] != row.get("len"):
                return False
            if _edge_hash(path, identity[0], identity) != row.get("edge_hash"):
                return False
        except (OSError, TypeError, ValueError):
            return False
        if identity[1] != row.get("modified_ns"):
            moved = True
        elif (_PLATFORM_NAME == "posix" and row.get("change_token")
                != {"Metadata": _unix_change_token(identity[2])}):
            moved = True
    return moved

# The census walks sessions.jsonl; on a huge corpus that is not a per-tick cost.
_CENSUS_PROBE_BUDGET_S = 2.0


def derived_repair_plan(now: float | None = None) -> RepairPlan:
    """The one verdict the daemon repairs from and the surfaces report from.

    Both derived verifiers - the generation proof and the census - read the
    same artifacts, so deriving one plan is what keeps a repair and a report
    from disagreeing about a single damaged file.
    """
    try:
        health = search_generation_health(now)
    except Exception as exc:  # noqa: BLE001 -- an unreadable proof is damage
        return RepairPlan("generation-unavailable", "full",
                          f"the corpus generation cannot be verified ({exc})")
    state = str(health.get("state") or "")
    if state == "ready":
        try:
            census = common.index_summary(
                deadline=time.monotonic() + _CENSUS_PROBE_BUDGET_S)
        except TimeoutError:
            return NO_REPAIR  # unbudgeted, not unhealthy
        except Exception:  # noqa: BLE001
            census = None
        if census is None:
            return RepairPlan(
                "proof-damaged", "full",
                "the published corpus census cannot be verified")
        return NO_REPAIR
    if state == "torn-generation" and _relocated_with_contents_intact():
        state = "generation-relocated"
        return RepairPlan(
            state, _REPAIR_SCOPE[state],
            "the derived generation moved with its contents intact")
    scope = _REPAIR_SCOPE.get(state)
    return (RepairPlan(state, scope, str(health.get("detail") or ""))
            if scope else NO_REPAIR)


def machine_freshness_fields(
        freshness: dict, now: float | None = None, *,
        routine: bool = True,
        publication_converging: bool = False) -> dict:
    # Every --json render calls this, so the routine tier is the default; a
    # deferred verification never claims failure and never overwrites one -
    # a real recorded freshness failure always outranks any deferral.
    health = search_generation_health(now, routine=routine)
    state = str(health.get("state") or "")
    live_publication = False
    if publication_converging and state == "torn-generation":
        live_publication = _live_refresh_lock()
        if not live_publication:
            health = search_generation_health(now, routine=routine)
            state = str(health.get("state") or "")
    out = dict(freshness)
    problem = state not in (
        "ready", "never-built", GENERATION_VERIFICATION_DEFERRED)
    eligible_base = (
        not bool(out.get("failing"))
        and out.get("checked") is not False
        and out.get("state") in ("no-known-failure", "index-behind")
    )
    owned_transition = (
        publication_converging and eligible_base
        and state in ("generation-moving", "search-index-stale")
    )
    if (publication_converging and eligible_base
            and state == "torn-generation" and live_publication):
        owned_transition = True
    if owned_transition:
        if out.get("state") != "index-behind":
            checked = out.get("checked")
            out = surface.publication_freshness_disclosure()
            if checked is not None:
                out["checked"] = checked
    elif problem and (state == "torn-generation"
                    or out.get("state") in ("no-known-failure", "unchecked")):
        out.update(state="degraded", failing=True, may_be_stale=True,
                   code=state,
                   reason=common.terminal_safe(health.get("detail") or ""),
                   consecutive_failures=0)
    return {"freshness": out, "corpus_age_s": health.get("corpus_age_s")}


def _read_boundary_stats() -> list[tuple[str, int, int, int]]:
    """Validate the Rust sidecar before importing it into the derived SQLite index."""
    try:
        obj = json.loads(BOUNDARY_STATS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(obj, dict) or obj.get("schema") not in (1, 2):
        return []
    try:
        published_generation = INGEST_SIG_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return []
    if obj.get("generation") != published_generation:
        return []
    tokens = obj.get("tokens")
    if not isinstance(tokens, dict):
        return []
    rows: list[tuple[str, int, int, int]] = []
    for token, counts in tokens.items():
        if not isinstance(token, str) or not token or len(token) > 16:
            continue
        if (not isinstance(counts, list) or len(counts) not in (2, 3)
                or not all(isinstance(value, int) for value in counts)):
            continue
        n, s = counts[:2]
        quality = counts[2] if len(counts) == 3 else 2
        if n < 0 or s < 0 or s > n or quality not in (0, 1, 2):
            continue
        rows.append((token, n, s, quality))
    return rows


def _replace_boundary_stats(db: sqlite3.Connection) -> None:
    rows = _read_boundary_stats()
    db.execute("DELETE FROM boundary_stats")
    db.executemany(
        "INSERT INTO boundary_stats(token, n, s, q) VALUES(?, ?, ?, ?)", rows)


@dataclass(frozen=True)
class _SessionFamilySnapshot:
    source_stamp: str | None
    sessions: frozenset[str]
    parents: Mapping[str, str]


def _read_session_families() -> _SessionFamilySnapshot:
    """Read one coherent compact session-family publication."""
    path = common.DATA_DIR / "sessions.jsonl"
    meta_path = common.DATA_DIR / common.SESSION_FAMILY_META_FILE
    census = common.read_session_family_census()
    if census is None:
        if not path.exists() and not meta_path.exists():
            return _SessionFamilySnapshot(
                common.SESSION_FAMILY_MISSING_STAMP, frozenset(), {})
        return _SessionFamilySnapshot(None, frozenset(), {})
    return _SessionFamilySnapshot(
        census.proof.stamp,
        census.sessions,
        census.parents,
    )


def _replace_session_families(
        db: sqlite3.Connection,
        snapshot: _SessionFamilySnapshot,
        known_sessions=(),
) -> None:
    if snapshot.source_stamp is None:
        raise _SourceMoved("session-family publication is invalid")
    extras = {
        str(session) for session in known_sessions if session
    } | set(snapshot.parents.values())
    extras.difference_update(snapshot.sessions)
    memo: dict[str, str] = {}
    db.execute("DELETE FROM session_family")
    db.executemany(
        "INSERT INTO session_family(session, root, side) VALUES(?, ?, ?)",
        (
            (session, common.family_root(session, snapshot.parents, memo),
             int(session in snapshot.parents))
            for session in chain(snapshot.sessions, sorted(extras))
        ),
    )
    db.execute(
        "INSERT INTO meta(key, value) VALUES('family_stamp', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (snapshot.source_stamp or "",),
    )


def _reconcile_session_families(
        db: sqlite3.Connection,
        snapshot: _SessionFamilySnapshot,
        changed: set[str] | str,
) -> None:
    """Apply a family publication without rewriting unchanged sidecar rows."""
    if snapshot.source_stamp is None:
        raise _SourceMoved("session-family publication is invalid")
    extras = set(snapshot.parents.values())
    extras.difference_update(snapshot.sessions)
    changed_set = changed if isinstance(changed, set) else set()
    affected = set(changed_set)
    if changed_set:
        roots = _session_family_roots_strict(db, changed_set)
        old_roots = {root for session, root in roots.items()
                     if session in changed_set}
        ordered_roots = tuple(old_roots)
        for start in range(0, len(ordered_roots), 500):
            page = ordered_roots[start:start + 500]
            marks = ",".join("?" for _ in page)
            affected.update(
                str(row[0]) for row in db.execute(
                    "SELECT session FROM session_family "
                    f"WHERE root IN ({marks})", page)
            )
    dead: list[tuple[str]] = []
    for row in db.execute("SELECT session FROM session_family"):
        session = str(row[0])
        if session not in snapshot.sessions and session not in extras:
            dead.append((session,))
    if dead:
        db.executemany("DELETE FROM session_family WHERE session=?", dead)

    memo: dict[str, str] = {}

    def rows(sessions):
        for session in sessions:
            if session in snapshot.sessions or session in extras:
                yield (
                    session,
                    common.family_root(session, snapshot.parents, memo),
                    int(session in snapshot.parents),
                )

    full = changed == "*" or not changed_set
    wanted = chain(snapshot.sessions, extras) if full else chain(affected, extras)
    db.executemany(
        "INSERT INTO session_family(session,root,side) VALUES(?,?,?) "
        "ON CONFLICT(session) DO UPDATE SET "
        "root=excluded.root, side=excluded.side "
        "WHERE root<>excluded.root OR side<>excluded.side",
        rows(wanted),
    )
    expected = len(snapshot.sessions) + len(extras)
    actual = int(db.execute("SELECT count(*) FROM session_family").fetchone()[0])
    if actual != expected:
        db.executemany(
            "INSERT INTO session_family(session,root,side) VALUES(?,?,?) "
            "ON CONFLICT(session) DO UPDATE SET "
            "root=excluded.root, side=excluded.side "
            "WHERE root<>excluded.root OR side<>excluded.side",
            rows(chain(snapshot.sessions, extras)),
        )
        actual = int(db.execute(
            "SELECT count(*) FROM session_family").fetchone()[0])
        if actual != expected:
            raise _SourceMoved(
                "session-family reconciliation did not publish every session")
    db.execute(
        "INSERT INTO meta(key,value) VALUES('family_stamp',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (snapshot.source_stamp,),
    )


def boundary_token_stats(db: sqlite3.Connection,
                         tokens: list[str]) -> dict[str, tuple[int, int]]:
    """Fetch only this query's ambiguity inputs; scoring never parses the sidecar."""
    unique = list(dict.fromkeys(token for token in tokens if token))
    if not unique:
        return {}
    marks = ",".join("?" for _ in unique)
    try:
        rows = db.execute(
            f"SELECT token, n, s FROM boundary_stats WHERE token IN ({marks})", unique)
        return {str(token): (int(n), int(s)) for token, n, s in rows}
    except (AttributeError, TypeError, ValueError):
        return {}


def boundary_token_qualities(db: sqlite3.Connection,
                             tokens: list[str]) -> dict[str, int]:
    """Fetch generation-pinned hard boundary-quality ceilings for short tokens."""
    unique = list(dict.fromkeys(token for token in tokens if token))
    if not unique:
        return {}
    marks = ",".join("?" for _ in unique)
    try:
        rows = db.execute(
            f"SELECT token, q FROM boundary_stats WHERE token IN ({marks})", unique)
        return {str(token): int(quality) for token, quality in rows
                if int(quality) in (0, 1, 2)}
    except (AttributeError, TypeError, ValueError):
        return {}


def _session_family_roots_strict(
        db: sqlite3.Connection, sessions: Iterable[str]) -> dict[str, str]:
    """Resolve roots for a writer; any malformed row or DB failure aborts."""
    wanted = tuple(dict.fromkeys(
        str(session) for session in sessions if session))
    roots = {session: session for session in wanted}
    for start in range(0, len(wanted), 500):
        page = wanted[start:start + 500]
        marks = ",".join("?" for _ in page)
        for session, root in db.execute(
                "SELECT session, root FROM session_family "
                f"WHERE session IN ({marks})",
                page,
        ):
            if (not isinstance(session, str) or not session
                    or not isinstance(root, str) or not root
                    or session not in roots):
                raise sqlite3.DatabaseError("invalid session-family root row")
            roots[session] = root
    return roots


def session_family_roots(
        db: sqlite3.Connection, sessions: Iterable[str]) -> dict[str, str]:
    """Resolve roots, defaulting only malformed compatibility values to self roots."""
    wanted = tuple(dict.fromkeys(
        str(session) for session in sessions if session))
    try:
        return _session_family_roots_strict(db, wanted)
    except (TypeError, ValueError):
        return {session: session for session in wanted}


def _stamps_equal(old_stamp: str, new_stamp: str) -> bool:
    old, new = _stamp_parts(old_stamp), _stamp_parts(new_stamp)
    return old is not None and new is not None and old == new


_TRIGRAM_OK: bool | None = None


def _trigram_ok() -> bool:
    # probed once per process: the answer is a property of the linked sqlite,
    # and the throwaway :memory: FTS table isn't free on the cold-search path
    global _TRIGRAM_OK
    if _TRIGRAM_OK is None:
        db = None
        try:
            db = sqlite3.connect(":memory:")
            db.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='trigram')")
            _TRIGRAM_OK = True
        except sqlite3.OperationalError:
            _TRIGRAM_OK = False
        finally:
            if db is not None:
                db.close()
    return _TRIGRAM_OK


def _open(path, busy_timeout_ms: int = 5000) -> sqlite3.Connection:
    """Connect for reading with pragmas sized for a corpus-scale db: default
    SQLite cache and no mmap make the first cold query pay random-read I/O. The
    busy timeout lets ordinary readers wait briefly for a commit. Published
    corpus.db is installed in DELETE-journal mode. The common foreground and
    read-only lane opens that main file with SQLite's normal read-only locking:
    no alias, clone, full copy, or sidecar write, while a concurrent incremental
    writer still yields a coherent committed snapshot. SQLite locking
    distinguishes that live non-hot journal from a dead hot journal, which a
    snapshot caller recovers only in a private alias. WAL or an unrecognized
    header falls back to a private alias or the materialized JSONL reader."""
    path = Path(path)
    before = _sqlite_file_identity(path)
    with _open_sqlite_file(path, before) as stream:
        header = stream.read(100)
    if (len(header) != 100
            or header[:16] != b"SQLite format 3\0"
            or header[18:20] != b"\x01\x01"):
        raise OSError(
            f"SQLite publication is not a DELETE-journal snapshot: {path}")
    for suffix in ("-wal",):
        sidecar_path = Path(f"{path}{suffix}")
        sidecar = _optional_sqlite_identity(sidecar_path)
        if sidecar is not None and sidecar[0] != 0:
            raise OSError(
                f"SQLite publication is not checkpointed: {sidecar_path}")
    uri = Path(os.path.abspath(os.fspath(path))).as_uri()
    db = sqlite3.connect(
        f"{uri}?mode=ro", uri=True,
        timeout=max(0, busy_timeout_ms) / 1000.0,
        factory=_PublishedConnection)
    try:
        if _sqlite_file_identity(path) != before:
            raise _SQLitePublicationMoved(
                "SQLite publication changed while opening")
        db.execute(f"PRAGMA busy_timeout={max(0, busy_timeout_ms)}")
        db.execute("PRAGMA mmap_size=268435456")
        db.execute("PRAGMA cache_size=-65536")
        db.execute("PRAGMA temp_store=MEMORY")
        db.execute("PRAGMA schema_version").fetchone()
        if _sqlite_file_identity(path) != before:
            raise _SQLitePublicationMoved(
                "SQLite publication changed while acquiring its read lock")
        db._source_identity = before
        return db
    except Exception:
        db.close()
        raise


def _scan(only: set[str] | None = None) -> dict[str, list[tuple]]:
    """Parse the materialized corpus into per-session row lists - the exact rows the msgs
    table holds (one per user turn, one per agent reply), mirroring explore's fallback so
    every engine reports identical hits. Shared by the full build and the incremental update
    so both index byte-identical content. The session concept rides in each row, so a concept
    relabel changes that session's fingerprint and re-indexes it like any other content move.

    `only` restricts the (expensive) JSON parse to a small set of session ids: each line's
    session is pulled out with one cheap `find` (the Rust writer emits compact JSON, so the
    keys are literal `"session":"` / `"id":"`), and non-candidate lines are skipped before
    json.loads. The incremental refresh therefore parses only changed sessions.
    Per-line field extraction also avoids testing every candidate id against every line.
    """
    import conceptpair

    def _digest(row: dict, text: str, source: str) -> str:
        if "content_digest" not in row:
            return compact.content_digest(text)
        try:
            return compact.require_content_digest(row["content_digest"])
        except compact.CompactError as exc:
            raise _SourceMoved(
                f"{source} has an invalid content digest") from exc

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
    names: dict[int, str] = {}
    concept: dict[str, str] = {}
    # Enrichment is optional, but a manifest-selected pair is indivisible: reject a
    # corrupt generation as "no labels" rather than combining independently read files.
    try:
        concept_rows, session_rows, _ = conceptpair.read(common.DATA_DIR)
        for row in concept_rows:
            names[int(row["concept_id"])] = (
                row.get("name") or row.get("label") or "").strip()
        for row in session_rows:
            concept[row["session"]] = (names.get(int(row.get("concept_id", -1)))
                                       or row.get("label", ""))
    except (conceptpair.IncoherentConceptPair, KeyError, TypeError, ValueError):
        names, concept = {}, {}

    reps: dict[str, tuple[str, str]] = {}
    p = common.DATA_DIR / "replies.jsonl"
    if p.exists():
        with p.open(encoding="utf-8") as replies:
            for line in replies:
                line = line.strip()
                if not line:
                    continue
                if only is not None:
                    idv = _field(line, "id")
                    parts = idv.split(":") if idv else []
                    if len(parts) < 3 or parts[1] not in only:
                        continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if o.get("id"):
                    reply = o.get("reply", "") or ""
                    reps[o["id"]] = (
                        reply, _digest(o, reply, "replies.jsonl"))

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
            # Absent-key fallback only: an explicit empty value matches the JSONL fallback.
            model_source = o.get("model_source", "explicit" if model else "unknown")
            base = (s, o.get("turn", 0), o.get("ts", 0), o.get("agent", ""),
                    o.get("project", ""), concept.get(s, ""), model, model_source)
            rows = by.setdefault(s, [])
            t = o.get("text", "") or ""
            if t:
                who = o.get("who", "user")
                rows.append((*base, who, t, _digest(o, t, "messages.jsonl")))
            r, r_digest = reps.get(o.get("id", ""), ("", ""))
            if r:
                rows.append((*base, "agent", r, r_digest))
    if common.setting("tools") != "off":
        keys = ((rows[0][3], session) for session, rows in by.items() if rows)
        full_events = only is None
        try:
            for agent, session, payload in common.event_blobs_bulk(keys, full=full_events):
                rows = by.get(session)
                if not rows or rows[0][3] != agent:
                    continue
                first = rows[0]
                turns = [(row[2], row[1]) for row in rows]
                for tool in common.tool_rows_from_payload(payload, turns):
                    rows.append((session, tool["turn"], tool["ts"], agent, first[4], first[5],
                                 "", "tool", "tool", tool["text"],
                                 compact.content_digest(tool["text"])))
        except RuntimeError as error:
            raise _SourceMoved("event generation unavailable during corpus scan") from error
    return by


def _session_sig(rows: list[tuple]) -> str:
    """Order-independent fingerprint of one session's indexed rows. Sorted before hashing so a
    reordering in messages.jsonl never reads as a change; covers every column the msgs table
    stores, so any content/metadata move (edit, model backfill, concept relabel) flips the sig."""
    import hashlib
    h = hashlib.md5()
    for row in sorted(rows, key=lambda r: (r[1], r[8], r[9])):  # (turn, who, text)
        h.update(repr(row).encode("utf-8", "replace"))
    return h.hexdigest()


class _SourceMoved(RuntimeError):
    pass


def _install_triggers(db: sqlite3.Connection) -> None:
    for name in _TRIGGER_NAMES:
        db.execute(f"DROP TRIGGER IF EXISTS {name}")
    for definition in _TRIGGER_DEFS:
        db.execute(definition)


def _maintain_schema7(db: sqlite3.Connection, current_stamp: str | None = None,
                      best_effort: bool = False) -> bool:
    """Upgrade schema-7 trigger/stamp metadata without rebuilding either FTS table.

    The schema number describes table compatibility, so changing trigger predicates does
    not justify rewriting the derived index. Incremental writers call this strictly
    before mutating rows. Read-only fast paths use ``best_effort`` so a competing reader or
    writer never turns an otherwise valid snapshot into a latency spike.
    """
    try:
        meta = dict(db.execute(
            "SELECT key, value FROM meta WHERE key IN ('stamp', 'fts_triggers')"))
        stamp_update = (current_stamp is not None
                        and meta.get("stamp") != current_stamp
                        and _stamps_equal(meta.get("stamp", ""), current_stamp))
        if meta.get("fts_triggers") == _TRIGGER_SCHEMA and not stamp_update:
            return True
        db.execute("BEGIN IMMEDIATE")
        meta = dict(db.execute(
            "SELECT key, value FROM meta WHERE key IN ('stamp', 'fts_triggers')"))
        if meta.get("fts_triggers") != _TRIGGER_SCHEMA:
            _install_triggers(db)
            db.execute(
                "CREATE INDEX IF NOT EXISTS msgs_who_ts "
                "ON msgs(who, coalesce(ts, 0) DESC)")
            db.execute("INSERT INTO meta(key, value) VALUES('fts_triggers', ?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (_TRIGGER_SCHEMA,))
        if (current_stamp is not None and meta.get("stamp") != current_stamp
                and _stamps_equal(meta.get("stamp", ""), current_stamp)):
            db.execute("UPDATE meta SET value=? WHERE key='stamp'", (current_stamp,))
        db.commit()
        return True
    except (sqlite3.DatabaseError, OSError):
        try:
            db.rollback()
        except sqlite3.DatabaseError:
            pass
        if best_effort:
            return False
        raise


# The cold build writes one temp file that every exit unlinks and only a
# complete build renames into place, so a torn journal is never read back. The
# rollback journal buys nothing here; FTS5's segment merge wants the cache.
_BULK_LOAD_PRAGMAS = ("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; "
                      "PRAGMA cache_size=-32768; PRAGMA temp_store=MEMORY;")


def _build(dst, expected_stamp: str | None = None) -> None:
    """One-shot rebuild: stream every session into msgs + its FTS mirror, then arm the sync
    triggers and record per-session fingerprints so subsequent refreshes go incremental."""
    if _protected_derived_target(dst):
        raise OSError(
            "AGREP_DATA_READONLY protects the search database publication")
    expected_stamp = expected_stamp or _stamp()
    if _stamp() != expected_stamp:
        raise _SourceMoved("corpus sources moved before full scan")
    family_snapshot = _read_session_families()
    if family_snapshot.source_stamp is None:
        raise _SourceMoved("session-family publication is invalid")
    db = sqlite3.connect(dst)
    try:
        db.executescript(_BULK_LOAD_PRAGMAS + _SCHEMA_SQL)
        by = _scan()
        session_sigs = []
        session_rows = None
        for session, session_rows in by.items():
            _insert_index_rows(db, session_rows)
            session_sigs.append((session, _session_sig(session_rows)))
        known_sessions = tuple(by)
        del by
        session_rows = None
        # bulk FTS build in one shot (no triggers armed yet, so rows aren't double-indexed)
        db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
        db.execute("INSERT INTO msgs_prose_fts(rowid, text) "
                   "SELECT id, coalesce(fts_text, text) FROM msgs "
                   "WHERE who <> 'tool'")
        db.executescript(_TRIGGERS_SQL)
        _replace_boundary_stats(db)
        _replace_session_families(db, family_snapshot, known_sessions)
        db.executemany("INSERT INTO session_sig(session, sig) VALUES(?, ?)",
                       session_sigs)
        if _stamp() != expected_stamp:
            raise _SourceMoved("corpus sources moved during full scan")
        db.execute("INSERT INTO meta VALUES('stamp', ?)", (expected_stamp,))
        db.execute("INSERT INTO meta VALUES('schema', ?)", (_SCHEMA,))
        db.execute("INSERT INTO meta VALUES('fts_triggers', ?)", (_TRIGGER_SCHEMA,))
        db.execute(
            "INSERT INTO meta VALUES('build_id', ?)",
            (indexd_runtime.derived_writer_build_id(
                require_binary=True),))
        db.commit()
    finally:
        db.close()


def _valid_db(stamp: str, busy_timeout_ms: int = 5000) -> sqlite3.Connection | None:
    if not DB_PATH.exists():
        return None
    db = None
    try:
        db = _connect_read_snapshot(
            DB_PATH, max(0, busy_timeout_ms) / 1000.0)
        meta = dict(db.execute(
            "SELECT key, value FROM meta "
            "WHERE key IN ('stamp', 'schema', 'fts_triggers', 'build_id')"))
        current_build = indexd_runtime.derived_writer_build_id(
            require_binary=True)
        if (meta.get("schema") == _SCHEMA
                and meta.get("build_id") == current_build
                and _stamps_equal(meta.get("stamp", ""), stamp)):
            needs_maintenance = (
                meta.get("fts_triggers") != _TRIGGER_SCHEMA
                or meta.get("stamp") != stamp)
            if not needs_maintenance:
                return db
            db.close()
            db = None
            if _protected_derived_target(DB_PATH):
                return None
            writer = None
            try:
                writer = sqlite3.connect(
                    DB_PATH, timeout=max(0, busy_timeout_ms) / 1000.0)
                writer.execute(f"PRAGMA busy_timeout={max(0, busy_timeout_ms)}")
                _maintain_schema7(writer, stamp, best_effort=True)
            except (OSError, sqlite3.DatabaseError):
                pass
            finally:
                if writer is not None:
                    writer.close()
            db = _open(DB_PATH, busy_timeout_ms)
            current = dict(db.execute(
                "SELECT key, value FROM meta "
                "WHERE key IN ('stamp', 'schema', 'build_id')"))
            if (current.get("schema") != _SCHEMA
                    or current.get("build_id") != current_build
                    or not _stamps_equal(current.get("stamp", ""), stamp)):
                db.close()
                db = None
                return None
            return db
        db.close()
        db = None
    except (sqlite3.DatabaseError, TypeError, OSError):
        if db is not None:
            try:
                db.close()
            except sqlite3.DatabaseError:
                pass
    return None


def _tmp_db_path() -> Path:
    """One well-known path, claimed by the index lock the rebuild already holds: a
    per-attempt unique name is one no later attempt can reclaim, so failures accumulate."""
    return DB_PATH.with_name(f"{DB_PATH.name}.building")


def _cleanup_tmp(path) -> None:
    if _protected_derived_target(path):
        return
    for p in (
            path, Path(str(path) + "-wal"), Path(str(path) + "-shm"),
            Path(str(path) + "-journal")):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


# Pre-`.building` builds named their temp per attempt, so an upgraded box still carries
# their leaked GBs and nothing else will ever name those files. Migration only - delete
# once no store predating `.building` can be met; nothing writes this shape now.
_LEGACY_TMP_DB_RE = re.compile(
    rf"^{re.escape(DB_PATH.name)}\.\d+\.\d+\.tmp"
    r"(?:-(?:wal|shm|journal))?$")


def orphan_temp_artifacts() -> dict:
    """Inventory legacy per-attempt build files without following filesystem links."""
    paths = []
    size = 0
    try:
        entries = os.scandir(DB_PATH.parent)
    except OSError:
        return {"count": 0, "bytes": 0, "paths": ()}
    with entries:
        for entry in entries:
            if _LEGACY_TMP_DB_RE.fullmatch(entry.name) is None:
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            paths.append(Path(entry.path))
            size += int(stat.st_size)
    return {"count": len(paths), "bytes": size, "paths": tuple(paths)}


def purge_legacy_build_temps() -> dict:
    """Reclaim the pre-`.building` scheme's leaks. Callers hold the index lock, so no
    concurrent agrep build - of either scheme - owns what this removes."""
    inventory = orphan_temp_artifacts()
    if _protected_derived_target(DB_PATH):
        return {
            "found": inventory["count"],
            "found_bytes": inventory["bytes"],
            "removed": 0,
            "removed_bytes": 0,
            "deferred": inventory["count"],
        }
    removed = 0
    removed_bytes = 0
    for path in inventory["paths"]:
        try:
            size = path.stat(follow_symlinks=False).st_size
            path.unlink()
        except OSError:
            continue
        removed += 1
        removed_bytes += int(size)
    return {
        "found": inventory["count"],
        "found_bytes": inventory["bytes"],
        "removed": removed,
        "removed_bytes": removed_bytes,
        "deferred": inventory["count"] - removed,
    }


def _read_changed() -> "set[str] | str | None":
    """The Rust ingest's changed-session delta: "*" (rescan everything), a set of session
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
    if _protected_derived_target(CHANGED_PATH):
        return
    try:
        CHANGED_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def _concepts_moved(old_stamp: str, new_stamp: str) -> bool:
    """Did a concept source change between the indexed stamp and now?"""
    old, new = _stamp_parts(old_stamp), _stamp_parts(new_stamp)
    return (old is None or new is None
            or any(old[i] != new[i] for i in _CONCEPT_IDX))


def _bulk_sources_moved(old_stamp: str, new_stamp: str) -> bool:
    """Did a source move whose row shape cannot use concept-only reconciliation?"""
    old, new = _stamp_parts(old_stamp), _stamp_parts(new_stamp)
    return (old is None or new is None
            or any(old[i] != new[i] for i in _BULK_IDX))


def _boundary_stats_moved(old_stamp: str, new_stamp: str) -> bool:
    old, new = _stamp_parts(old_stamp), _stamp_parts(new_stamp)
    return (old is None or new is None
            or old[_BOUNDARY_IDX] != new[_BOUNDARY_IDX])


def _apply_session_row_diff(db: sqlite3.Connection, session: str,
                            desired: list[tuple]) -> tuple[int, int]:
    """Make one session's stored rows equal ``desired`` as a multiset.

    A session may legitimately contain byte-identical tool rows, so a set diff loses
    data. Counter preserves that multiplicity; the rowid buckets let us delete exactly
    the excess copies while retaining every unchanged row (and therefore avoiding its
    two FTS trigger lanes). Concept-only changes update metadata in place; other changed
    rows are intentionally one removal plus one insert.
    """
    by_row: dict[tuple, list[int]] = defaultdict(list)
    sidecar_updates: list[tuple[str | None, int]] = []
    for raw in db.execute(
            f"SELECT id, {_ROW_COLS}, fts_text FROM msgs "
            "WHERE session = ? ORDER BY id",
            (session,)):
        rowid = int(raw[0])
        row = tuple(raw[1:-1])
        by_row[row].append(rowid)
        expected = _fts_text_sidecar(row[-2])
        if raw[-1] != expected:
            sidecar_updates.append((expected, rowid))

    want = Counter(desired)

    # First retain byte-identical rows. Then pair the remainder by every stored
    # column except concept. A relabel is metadata, not a new transcript row: update
    # that one column in place so rowids and both FTS posting lists remain untouched.
    old_by_shape: dict[tuple, list[tuple[tuple, int]]] = defaultdict(list)
    new_by_shape: dict[tuple, list[tuple]] = defaultdict(list)

    def without_concept(row: tuple) -> tuple:
        return row[:5] + row[6:]

    for row, ids in by_row.items():
        keep = min(len(ids), want.get(row, 0))
        for rowid in ids[keep:]:
            old_by_shape[without_concept(row)].append((row, rowid))
        for _ in range(max(0, want.get(row, 0) - keep)):
            new_by_shape[without_concept(row)].append(row)
        want.pop(row, None)
    # Desired rows with no exact stored counterpart were not visited above.
    for row, count in want.items():
        for _ in range(count):
            new_by_shape[without_concept(row)].append(row)

    updates: list[tuple[str, int]] = []
    remove_ids: list[int] = []
    add_rows: list[tuple] = []
    for shape in set(old_by_shape) | set(new_by_shape):
        olds = sorted(old_by_shape.get(shape, []), key=lambda item: (repr(item[0][5]), item[1]))
        news = sorted(new_by_shape.get(shape, []), key=lambda row: repr(row[5]))
        paired = min(len(olds), len(news))
        updates.extend((news[i][5], olds[i][1]) for i in range(paired))
        remove_ids.extend(rowid for _row, rowid in olds[paired:])
        add_rows.extend(news[paired:])

    if sidecar_updates:
        db.executemany("UPDATE msgs SET fts_text = ? WHERE id = ?", sidecar_updates)
    if updates:
        db.executemany("UPDATE msgs SET concept = ? WHERE id = ?", updates)
    if remove_ids:
        db.executemany("DELETE FROM msgs WHERE id = ?",
                       ((rowid,) for rowid in remove_ids))
    if add_rows:
        _insert_index_rows(db, add_rows)
    return len(add_rows), len(remove_ids)


def _incremental(stamp: str) -> sqlite3.Connection | None:
    """Refresh an existing current-schema db from the Rust changed-session delta.

    Changed sessions are reparsed, but their stored rows are reconciled as multisets:
    unchanged rows keep their rowids and never fire FTS triggers. A ``*`` or oversized
    named delta performs one full corpus parse, then uses the same signature/row diff.
    Concept-source moves full-scan and signature-diff all sessions because the Rust
    delta does not name relabels. Returns None only when a clean bulk rebuild is
    required (cold/schema/tools-setting/invalid).
    """
    if _protected_derived_target(DB_PATH):
        return None
    started = time.perf_counter()
    if not DB_PATH.exists():
        common.dbg("corpusdb incremental unavailable: no published db")
        return None
    db = None
    try:
        db = sqlite3.connect(DB_PATH)
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA cache_size=-65536")
        meta = dict(db.execute(
            "SELECT key, value FROM meta "
            "WHERE key IN ('schema', 'stamp', 'family_stamp', 'build_id')"))
        current_build = indexd_runtime.derived_writer_build_id(
            require_binary=True)
        if (meta.get("schema") != _SCHEMA
                or meta.get("build_id") != current_build):
            common.dbg("corpusdb incremental unavailable: schema "
                       f"{meta.get('schema')!r} != {_SCHEMA!r} or owner "
                       f"{meta.get('build_id')!r} != "
                       f"{current_build!r}")
            return None  # schema bump -> caller rebuilds from scratch
        old_stamp = meta.get("stamp", "")
        if _bulk_sources_moved(old_stamp, stamp):
            common.dbg("corpusdb incremental unavailable: bulk row-shape source moved")
            return None
        # Existing schema-7 databases get metadata-aware UPDATE triggers in place;
        # no FTS rebuild is needed merely to change trigger definitions.
        _maintain_schema7(db)
        old = dict(db.execute("SELECT session, sig FROM session_sig"))
        family_snapshot = _read_session_families()
        if family_snapshot.source_stamp is None:
            common.dbg(
                "corpusdb incremental unavailable: sessions.jsonl invalid")
            return None
        if family_snapshot.source_stamp == common.SESSION_FAMILY_MISSING_STAMP:
            common.dbg(
                "corpusdb incremental unavailable: sessions.jsonl missing")
            return None
        concept_scan = _concepts_moved(old_stamp, stamp)
        changed = _read_changed()
        if concept_scan:
            # A manifest/name/assignment publication can relabel any session and has
            # no changed-session marker. One parse plus signatures is still far cheaper
            # than rebuilding both FTS tables, and unchanged rows never fire triggers.
            by = _scan()
            current = set(by)
            targets = set(by) | set(old)
            full_scan = True
            mode = "concept"
        else:
            current = set(family_snapshot.sessions)
            if changed != "*" and not isinstance(changed, set):
                common.dbg(f"corpusdb incremental unavailable: delta={changed!r}")
                return None
            full_scan = changed == "*" or len(changed) > _FAST_MAX
            by = _scan() if full_scan else _scan(only=changed)
            # '*' means every live session is a candidate. An oversized named delta uses
            # the full parse for efficiency but still limits reconciliation to named sessions.
            targets = (set(by) | current) if changed == "*" else set(changed)
            mode = "all" if changed == "*" else "full-parse" if full_scan else "named"
        common.dbg(f"corpusdb incremental scan: mode={mode} targets={len(targets)}")
        db.execute("BEGIN")
        if _boundary_stats_moved(old_stamp, stamp):
            _replace_boundary_stats(db)
        if meta.get("family_stamp", "") != (family_snapshot.source_stamp or ""):
            _reconcile_session_families(db, family_snapshot, changed)
        n_add = n_remove = n_changed = n_unchanged = n_removed_sessions = 0
        for s in sorted(targets):
            rows = by.get(s, [])
            sig = _session_sig(rows) if rows else None
            if sig == old.get(s):
                n_unchanged += 1
                continue  # candidate didn't really change
            added, removed = _apply_session_row_diff(db, s, rows)
            n_add += added
            n_remove += removed
            n_changed += 1
            if rows:
                db.execute("INSERT OR REPLACE INTO session_sig(session, sig) VALUES(?, ?)", (s, sig))
            else:
                db.execute("DELETE FROM session_sig WHERE session = ?", (s,))
        # removals the delta didn't name (e.g. a deleted session file): anything indexed that
        # sessions.jsonl no longer lists.
        for s in old:
            if s not in targets and s not in current:
                added, removed = _apply_session_row_diff(db, s, [])
                n_add += added
                n_remove += removed
                n_removed_sessions += 1
                db.execute("DELETE FROM session_sig WHERE session = ?", (s,))
        # Never stamp rows from one generation as a newer generation that landed
        # during the scan. Recording `stamp` (not a post-scan value) means even a
        # change immediately after this check is observed by the next connect.
        if _stamp() != stamp:
            raise _SourceMoved("corpus sources moved during incremental scan")
        db.execute("UPDATE meta SET value = ? WHERE key = 'stamp'", (stamp,))
        db.execute(
            "INSERT INTO meta(key, value) VALUES('build_id', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (current_build,))
        db.commit()
        _consume_changed()  # applied -> clear the delta
        elapsed_ms = (time.perf_counter() - started) * 1000
        common.dbg("corpusdb incremental rows: "
                   f"sessions changed={n_changed} unchanged={n_unchanged} "
                   f"removed={n_removed_sessions}; +{n_add}/-{n_remove} rows "
                   f"in {elapsed_ms:.1f}ms")
        db.close()
        db = None
        return _open(DB_PATH)
    except Exception as exc:  # noqa: BLE001 -- ANY failure (db corruption, a half-written source's
        # OSError/JSONDecodeError) drops to a clean full rebuild, never escapes to crash the
        # search. finally closes db so no write-locked handle blocks the rebuild swap.
        common.dbg("corpusdb incremental failed: "
                   f"{type(exc).__name__}: {exc}", "!")
        if db is not None:
            try:
                db.rollback()
            except sqlite3.DatabaseError:
                pass
        return None
    finally:
        if db is not None:
            try:
                db.close()
            except sqlite3.DatabaseError:
                pass


def _stale_db(
        busy_timeout_ms: int = 5000, *,
        expected_build_id: str | None = None,
) -> sqlite3.Connection | None:
    """The current published db regardless of stamp, for readers deferring to a live
    freshener. Schema must still match - stale rows are fine, missing columns are not."""
    if not DB_PATH.exists():
        return None
    db = None
    try:
        db = _connect_read_direct(
            DB_PATH, max(0, busy_timeout_ms) / 1000.0)
        meta = dict(db.execute(
            "SELECT key, value FROM meta "
            "WHERE key IN ('schema', 'build_id')"))
        if (meta.get("schema") == _SCHEMA
                and (expected_build_id is None
                     or meta.get("build_id") == expected_build_id)):
            db._source_build_id = meta.get("build_id")
            return db
        db.close()
        db = None
    except (sqlite3.DatabaseError, TypeError, OSError):
        if db is not None:
            try:
                db.close()
            except sqlite3.DatabaseError:
                pass
    return None


def _foreign_stale_db(
        busy_timeout_ms: int = 0,
        *,
        expected_identity: tuple[int, int, int, int, int] | None = None,
        expected_build_id: str | None = None,
) -> sqlite3.Connection | None:
    """Read a schema-compatible foreign publication without touching it.

    Checkpointed and live non-hot DELETE databases open directly in mode=ro.
    WAL or dead hot rollback-journal families are cloned to private system
    scratch; filesystem clones are free of the routine byte-copy budget, while
    an unavailable reflink refuses before a corpus-scale copy. Build ownership
    gates writers, not readers, so only the schema must match here.
    """
    if _query_failure_matches_current():
        return None
    if not DB_PATH.exists():
        return None
    db = None
    try:
        db = _connect_read_snapshot(
            DB_PATH, max(0, busy_timeout_ms) / 1000.0,
            max_clone_bytes=_ROUTINE_ALIAS_CLONE_MAX_BYTES)
        meta = dict(db.execute(
            "SELECT key, value FROM meta "
            "WHERE key IN ('schema', 'build_id')"))
        if (meta.get("schema") == _SCHEMA
                and (expected_build_id is None
                     or meta.get("build_id") == expected_build_id)
                and (expected_identity is None
                     or db._source_identity == expected_identity)):
            db._source_build_id = meta.get("build_id")
            return db
    except (AliasCloneRefused, OSError, sqlite3.DatabaseError,
            TypeError, ValueError):
        pass
    if db is not None:
        try:
            db.close()
        except sqlite3.DatabaseError:
            pass
    return None


def _interactive_snapshot(
        stamp: str, *, repair_required: bool,
        ownership: _DerivedWriteOwnership | None = None,
) -> sqlite3.Connection | None:
    """Read the last publication without entering any derived-store writer path.

    A query-damage marker makes the SQLite snapshot ineligible, but the
    materialized transcript still supplies the direct-scan engine. Otherwise a
    schema-readable database is safe to serve even when its source stamp is old.
    Build ownership can strengthen `_stale_db` later without changing this
    foreground no-write boundary.
    """
    if _query_failure_matches_current():
        return None
    ownership = ownership or _derived_write_ownership(for_write=True)
    ownership_refused = not ownership.writable
    sqlite_contention = bool(
        ownership.journal_blocked
        or sqlite_failure_is_contention(ownership.sqlite_failure))
    retained_replacement = ownership.replace_retained_db
    if ownership_refused or retained_replacement:
        # Never create aliases or sidecars in the owner's directory. A private
        # clone (or a checkpointed mode=ro open) can still serve the fast index
        # while the successor daemon performs the exact ownership handoff.
        db = None if repair_required else _foreign_stale_db(
            _CONTENDED_READER_WAIT_MS if sqlite_contention else 0,
            expected_identity=(
                ownership.retained_reader_identity
                if retained_replacement else None),
            expected_build_id=(
                ownership.retained_build_id
                if retained_replacement else None),
        )
        expected_build = None
        if sqlite_contention:
            try:
                expected_build = indexd_runtime.derived_writer_build_id(
                    require_binary=True)
            except OSError:
                pass
        observed_build = getattr(db, "_source_build_id", None)
        same_build_contention = bool(
            sqlite_contention and expected_build
            and observed_build == expected_build)
        foreign_contention = bool(
            sqlite_contention and expected_build and observed_build
            and observed_build != expected_build)
        if ownership_refused and (not sqlite_contention or foreign_contention):
            indexd_runtime.disclose_foreground_snapshot(
                direct_scan=db is None,
                code=indexd_runtime.DERIVED_STORE_OWNER_CODE,
                reason=ownership.reason,
            )
        if db is None:
            if sqlite_contention:
                indexd_runtime.disclose_foreground_snapshot(
                    direct_scan=True,
                    reason="the search index is busy updating")
            common.dbg(
                "corpusdb: contended or foreign publication is not safely "
                "readable; materialized snapshot selected")
            return None
        if ownership_refused and sqlite_contention and not (
                same_build_contention or foreign_contention):
            indexd_runtime.disclose_foreground_snapshot(
                direct_scan=False,
                reason="search-index ownership is unavailable while updating")
        if same_build_contention:
            common.dbg(
                "corpusdb: contended publication opened from a coherent "
                "read-only snapshot")
        elif retained_replacement or foreign_contention or not sqlite_contention:
            common.dbg(
                "corpusdb: schema-compatible foreign publication opened "
                "read-only")
        else:
            common.dbg(
                "corpusdb: schema-compatible publication opened read-only")
    else:
        try:
            expected_build_id = indexd_runtime.derived_writer_build_id(
                require_binary=True)
        except OSError as exc:
            indexd_runtime.disclose_foreground_snapshot(
                direct_scan=True,
                code=indexd_runtime.DERIVED_STORE_OWNER_CODE,
                reason=(
                    "derived writer identity is unavailable because the ingest "
                    f"binary cannot be verified ({exc})"),
            )
            return None
        db = (
            None
            if repair_required
            else _stale_db(
                _CONTENDED_READER_WAIT_MS,
                expected_build_id=expected_build_id)
        )
    current = False
    if db is not None:
        try:
            row = db.execute(
                "SELECT value FROM meta WHERE key = 'stamp'").fetchone()
            current = bool(row and _stamps_equal(str(row[0]), stamp))
        except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
            try:
                db.close()
            except sqlite3.DatabaseError:
                pass
            db = None
    if db is not None:
        db._retained_snapshot = retained_replacement
        db._source_stamp_current = current
    if current:
        indexd_runtime._set_fts_delegated(False)
        if (indexd_runtime.foreground_refresh_deferred()
                and not indexd_runtime.sources_verified_current()):
            # A deferred refresh over provably-undrifted sources skipped
            # nothing: the census is the verdict, so the current snapshot is
            # simply current and there is nothing to hedge about.
            indexd_runtime.disclose_foreground_snapshot(direct_scan=False)
        common.dbg(
            "corpusdb: published db matches the source stamp -> read without maintenance")
        return db
    if db is not None and indexd_runtime.search_index_build_pending():
        # `agrep index` returns promising the scan serves until the search
        # database lands. A snapshot whose stamp predates the rows just
        # published answers from exactly the generation that promise skipped.
        try:
            db.close()
        except sqlite3.DatabaseError:
            pass
        db = None
        common.dbg(
            "corpusdb: published db is behind the source stamp and a derived "
            "build is queued -> direct scan serves the promised gap")
    # Staleness is result integrity, not progress noise. It remains visible on
    # quiet plumbing and count/machine-shaped porcelain (on stderr).
    indexd_runtime.disclose_foreground_snapshot(
        direct_scan=db is None,
        code="search-index-stale")
    common.dbg(
        ("corpusdb: published SQLite snapshot unavailable; direct engine "
         "must verify the materialized transcript snapshot")
        if db is None else
        ("corpusdb: foreground reader selected a published SQLite snapshot; "
         "derived refresh stays outside the search"))
    return db


_QUERY_REBUILD_LOCAL: set[str] = set()
_FOREIGN_QUERY_FAILURE_LOCAL: dict[
    str, tuple[int, int, int, int, int] | None,
] = {}
_QUERY_REBUILD_MARKER_VERSION = 1
_QUERY_REBUILD_MARKER_MAX_BYTES = 4096


def _protected_derived_target(path: os.PathLike) -> bool:
    """True when a derived writer target is inside the exact protected root."""
    if not common.data_dir_readonly(common.DATA_DIR):
        return False
    try:
        root = os.path.normcase(os.path.realpath(common.DATA_DIR))
        target = os.path.normcase(os.path.realpath(os.fspath(path)))
        return os.path.commonpath((root, target)) == root
    except (OSError, ValueError):
        # The exact root is protected. An unresolvable descendant cannot be
        # promoted to writable merely because its metadata is hostile.
        return True


def _query_rebuild_key() -> str:
    return os.path.abspath(os.fspath(DB_PATH))


def _query_rebuild_marker() -> Path:
    return DB_PATH.with_name(".corpusdb-rebuild")


_QUERY_DB_TRANSIENT_CODES = frozenset((
    getattr(sqlite3, "SQLITE_BUSY", 5),
    getattr(sqlite3, "SQLITE_LOCKED", 6),
))
_QUERY_DB_STRUCTURAL_CODES = frozenset((
    getattr(sqlite3, "SQLITE_CORRUPT", 11),
    getattr(sqlite3, "SQLITE_NOTADB", 26),
))


def query_database_error_kind(error: sqlite3.DatabaseError) -> str:
    """Classify a query failure without parsing localized SQLite messages."""
    code = getattr(error, "sqlite_errorcode", None)
    if type(code) is int:
        primary = code & 0xFF
        if primary in _QUERY_DB_TRANSIENT_CODES:
            return "transient"
        if primary in _QUERY_DB_STRUCTURAL_CODES:
            return "structural"
        return "unavailable"
    # Before Python 3.11, SQLITE_AUTH and several unrelated primary codes also
    # arrive as an exact DatabaseError. Missing numeric evidence cannot mutate.
    return "unavailable"


def sqlite_failure_is_contention(error: sqlite3.Error | None) -> bool:
    """BUSY/LOCKED by code, or by decades-stable prose where 3.10 has no code.

    For display and read-path routing only: adoption/ownership authority keeps
    the strict coded classifier, because prose must never authorize a mutation.
    """
    if error is None:
        return False
    if query_database_error_kind(error) == "transient":
        return True
    if type(getattr(error, "sqlite_errorcode", None)) is int:
        return False
    message = str(error).lower()
    return ("database is locked" in message
            or "database table is locked" in message)


def bind_query_database_error(
        error: sqlite3.DatabaseError,
        db: sqlite3.Connection | None) -> None:
    """Attach the exact opened publication to an error before its handle closes."""
    if db is None:
        return
    identity = getattr(db, "_source_identity", None)
    build_id = getattr(db, "_source_build_id", None)
    try:
        if (isinstance(identity, tuple) and len(identity) == 5
                and all(type(value) is int for value in identity)):
            error._agrep_source_identity = identity
        if (isinstance(build_id, str)
                and indexd_runtime._BUILD_ID_RE.fullmatch(build_id)):
            error._agrep_source_build_id = build_id
    except (AttributeError, TypeError):
        pass


def _query_failure_matches_current() -> bool:
    key = _query_rebuild_key()
    if key not in _FOREIGN_QUERY_FAILURE_LOCAL:
        return False
    expected = _FOREIGN_QUERY_FAILURE_LOCAL[key]
    if expected is None:
        _FOREIGN_QUERY_FAILURE_LOCAL.pop(key, None)
        _QUERY_REBUILD_LOCAL.discard(key)
        return False
    try:
        current = _optional_sqlite_identity(DB_PATH)
    except OSError:
        return True
    if current == expected:
        return True
    _FOREIGN_QUERY_FAILURE_LOCAL.pop(key, None)
    _QUERY_REBUILD_LOCAL.discard(key)
    return False


def _query_rebuild_marker_applies(path: Path | None = None) -> bool:
    database = DB_PATH if path is None else Path(path)
    marker = database.with_name(".corpusdb-rebuild")
    try:
        with common.open_regular_binary(marker) as stream:
            raw = stream.read(_QUERY_REBUILD_MARKER_MAX_BYTES + 1)
    except FileNotFoundError:
        return False
    except OSError:
        return False
    if len(raw) > _QUERY_REBUILD_MARKER_MAX_BYTES:
        return False
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return False
        identity = payload.get("database_identity")
        build_id = payload.get("build_id")
        if (payload.get("version") != _QUERY_REBUILD_MARKER_VERSION
                or not isinstance(identity, list) or len(identity) != 5
                or any(type(value) is not int for value in identity)
                or not isinstance(build_id, str)
                or indexd_runtime._BUILD_ID_RE.fullmatch(build_id) is None):
            return False
        expected = tuple(identity)
        if _optional_sqlite_identity(database) != expected:
            return False
        state, owner, _detail = _database_build_id(path=database)
        return bool(
            state == "owned" and owner == build_id
            and _optional_sqlite_identity(database) == expected)
    except (OSError, RecursionError, TypeError, ValueError, json.JSONDecodeError):
        return False


def query_rebuild_required() -> bool:
    key = _query_rebuild_key()
    if key in _QUERY_REBUILD_LOCAL and (
            key not in _FOREIGN_QUERY_FAILURE_LOCAL
            or _query_failure_matches_current()):
        return True
    return _query_rebuild_marker_applies()


def record_query_database_error(
        error: sqlite3.DatabaseError,
        db: sqlite3.Connection | None = None) -> None:
    """Make a failed derived-index read fall back now and rebuild on a writer path."""
    kind = query_database_error_kind(error)
    if kind == "transient":
        return
    key = _query_rebuild_key()
    bind_query_database_error(error, db)
    identity = getattr(error, "_agrep_source_identity", None)
    build_id = getattr(error, "_agrep_source_build_id", None)
    if (not isinstance(identity, tuple) or len(identity) != 5
            or any(type(value) is not int for value in identity)):
        return
    _FOREIGN_QUERY_FAILURE_LOCAL[key] = identity
    if kind != "structural":
        return
    if indexd_runtime._data_dir_readonly():
        return
    ownership = _derived_write_ownership(for_write=True)
    if not ownership.writable:
        return
    if (not isinstance(build_id, str)
            or indexd_runtime._BUILD_ID_RE.fullmatch(build_id) is None):
        return
    try:
        if _optional_sqlite_identity(DB_PATH) != identity:
            _FOREIGN_QUERY_FAILURE_LOCAL.pop(key, None)
            _QUERY_REBUILD_LOCAL.discard(key)
            return
    except OSError:
        return
    _QUERY_REBUILD_LOCAL.add(key)
    marker = _query_rebuild_marker()
    temporary = common.embedding_temp_path(marker, "query_rebuild")
    try:
        with temporary.open("wb") as stream:
            stream.write(json.dumps({
                "version": _QUERY_REBUILD_MARKER_VERSION,
                "database_identity": list(identity),
                "build_id": build_id,
            }, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            temporary.chmod(0o600)
        common.replace_with_retry(temporary, marker)
    except OSError:
        return
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    common.dbg(
        f"corpusdb: query failed ({type(error).__name__}); "
        "JSONL serves while a full rebuild is pending", "!")


def _clear_query_rebuild_request() -> None:
    marker = _query_rebuild_marker()
    if _protected_derived_target(marker):
        return
    try:
        marker.unlink(missing_ok=True)
    except OSError:
        return
    key = _query_rebuild_key()
    _QUERY_REBUILD_LOCAL.discard(key)
    _FOREIGN_QUERY_FAILURE_LOCAL.pop(key, None)


def _rebuild_and_publish(stamp: str, repair_required: bool):
    """Build the whole corpus into the single claimed temp path, then publish it by rename.

    Callers hold the index lock. Every exit leaves zero build bytes behind, so a rebuild
    that dies of ENOSPC does not consume the free space its own next attempt needs."""
    purged = purge_legacy_build_temps()
    if purged["removed"]:
        common.dbg(
            f"corpusdb: reclaimed {purged['removed']} legacy build artifact(s), "
            f"{purged['removed_bytes']} byte(s)")
    tmp = _tmp_db_path()
    try:
        _cleanup_tmp(tmp)
        built = False
        for attempt in range(3):
            stamp = _stamp()
            try:
                _build(tmp, stamp)
                built = True
                break
            except _SourceMoved as exc:
                common.dbg(f"corpusdb: full scan generation moved (attempt {attempt + 1}/3): "
                           f"{exc}", "!")
                _cleanup_tmp(tmp)
        if not built:
            common.dbg("corpusdb: sources kept moving; defer full rebuild to freshener", "!")
            return _stale_db()
        ownership = _derived_write_ownership(
            for_write=True, exact_retained=True)
        if not ownership.writable:
            return _interactive_snapshot(
                stamp, repair_required=repair_required, ownership=ownership)

        def retained_replace_fence() -> None:
            if not ownership.replace_retained_db:
                return
            observed = _derived_write_ownership(for_write=True)
            if (not observed.writable or not observed.replace_retained_db
                    or observed.retained_build_id != ownership.retained_build_id
                    or observed.retained_reader_identity
                    != ownership.retained_reader_identity):
                raise OSError(
                    "retained search db changed before atomic replacement")
        try:
            common.replace_with_retry(
                tmp, DB_PATH, before_attempt=retained_replace_fence)
        except OSError:
            # Windows: a live reader can still hold corpus.db - keep the old db and let the next
            # connect retry; _stale_db enforces the schema match, else the JSONL engine serves.
            return _stale_db()
    finally:
        _cleanup_tmp(tmp)
    _consume_changed()  # a full rebuild indexed everything -> the delta is superseded
    _clear_query_rebuild_request()
    indexd_runtime._set_fts_delegated(False)
    return _open(DB_PATH)


def _live_refresh_lock() -> bool:
    """Whether the corpus/ingest lock names its exact live process."""
    try:
        observed = index_lock._snapshot(common.INDEX_LOCK_PATH)
        owner = index_lock.parse_owner(observed.raw) if observed is not None else None
        if owner is None or owner.start in (None, "unknown"):
            return False
        return (common.pid_alive(owner.pid)
                and common.process_start_identity(owner.pid) == owner.start)
    except OSError:
        return False


def query_publication_active() -> bool:
    """Whether the query publication lock names its exact live process."""
    return _live_refresh_lock()


def query_search_index_build_active() -> bool:
    """Whether that live publisher is assembling the next FTS database."""
    if not _live_refresh_lock():
        return False
    try:
        state = _tmp_db_path().lstat()
    except OSError:
        return False
    reparse = getattr(statmod, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(
        statmod.S_ISREG(state.st_mode)
        and not (getattr(state, "st_file_attributes", 0) & reparse)
    )


class _ConnectIndexLock:
    def __init__(self, allow_stale: bool):
        self.allow_stale = allow_stale
        writer = indexd_runtime.derived_writer_build_id(require_binary=True)
        if not re.fullmatch(r"[0-9a-f]{20}", writer):
            raise OSError("derived writer identity is malformed")
        label = f"corpusdb:{writer}"
        self.lock = (
            common.IndexLock(label, timeout=0)
            if allow_stale else common.IndexLock(label)
        )
        self.acquired = False

    def __enter__(self) -> bool:
        try:
            self.lock.__enter__()
        except OSError:
            if not self.allow_stale:
                raise
            common.dbg(
                "corpusdb: index lock is unavailable -> nonblocking JSONL fallback",
                "!")
            return False
        self.acquired = True
        return True

    def __exit__(self, exc_type, exc, traceback):
        if not self.acquired:
            return False
        return self.lock.__exit__(exc_type, exc, traceback)


def connect(quiet: bool = False, allow_stale: bool = False,
            read_only: bool = False) -> sqlite3.Connection | None:
    """A connection to a FRESH corpus db, refreshing first if the sources moved. None when
    there's nothing to index yet or sqlite lacks trigram fts5. The common case - unchanged
    stamp - returns the live db untouched; a moved stamp goes incremental (only the changed
    sessions re-indexed), full rebuild only on a cold start / schema bump / corruption.

    allow_stale (the interactive read path) is categorical: serve a readable
    published db or the materialized-JSONL fallback and never sweep, lock,
    increment, rebuild, or publish a derived artifact. The daemon/indexer and
    explicit index paths keep the writer default.

    read_only (gates and diagnostics; also forced by AGREP_DATA_READONLY): serve
    whatever published db exists, never refresh, never sweep - a dev-tree process
    must not rewrite a data dir owned by the installed daemon (mixed-version
    writers tear each other's caches)."""
    protected_read = indexd_runtime._data_dir_readonly()
    if (read_only or protected_read or allow_stale) \
            and _query_failure_matches_current():
        return None
    if read_only or protected_read:
        # A protected process never writes the publication. A foreign but
        # schema-compatible snapshot remains readable through the same direct
        # mode=ro/private-clone boundary as the interactive lane.
        ownership = _derived_write_ownership(for_write=True)
        if not ownership.writable:
            db = _foreign_stale_db(0)
            indexd_runtime.disclose_foreground_snapshot(
                direct_scan=db is None,
                code=indexd_runtime.DERIVED_STORE_OWNER_CODE,
                reason=ownership.reason,
            )
            return db
        if ownership.replace_retained_db:
            db = _foreign_stale_db(
                0, expected_identity=ownership.retained_reader_identity,
                expected_build_id=ownership.retained_build_id)
            if db is not None:
                try:
                    row = db.execute(
                        "SELECT value FROM meta WHERE key = 'stamp'").fetchone()
                    db._retained_snapshot = True
                    db._source_stamp_current = bool(
                        row and _stamps_equal(str(row[0]), _stamp()))
                except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
                    db._source_stamp_current = False
            if protected_read:
                indexd_runtime.disclose_foreground_snapshot(
                    direct_scan=db is None,
                    code="search-index-stale",
                    reason=indexd_runtime.READONLY_REFRESH_FENCE_REASON,
                )
            return db
        try:
            current_build = indexd_runtime.derived_writer_build_id(
                require_binary=True)
        except OSError as exc:
            if protected_read:
                indexd_runtime.disclose_foreground_snapshot(
                    direct_scan=True,
                    code=indexd_runtime.DERIVED_STORE_OWNER_CODE,
                    reason=(
                        "derived writer identity is unavailable because the "
                        f"ingest binary cannot be verified ({exc})"),
                )
            return None
        db = _stale_db(0, expected_build_id=current_build)
        if protected_read:
            indexd_runtime.disclose_foreground_snapshot(
                direct_scan=db is None,
                code="search-index-stale",
                reason=indexd_runtime.READONLY_REFRESH_FENCE_REASON,
            )
        return db
    messages_available = common.MESSAGES_PATH.exists()
    trigram_available = _trigram_ok()
    if not messages_available or not trigram_available:
        if allow_stale and messages_available:
            ownership = _derived_write_ownership()
            ownership_refused = not ownership.writable
            if (ownership_refused
                    or indexd_runtime.foreground_refresh_deferred()):
                indexd_runtime.disclose_foreground_snapshot(
                    direct_scan=True,
                    code=(indexd_runtime.DERIVED_STORE_OWNER_CODE
                          if ownership_refused else "search-index-stale"),
                    reason=ownership.reason if ownership_refused else None)
        common.dbg(f"corpusdb: no connect (messages={messages_available} "
                   f"trigram_fts={trigram_available})", "!")
        return None
    repair_required = query_rebuild_required()
    stamp = _stamp()
    if allow_stale:
        return _interactive_snapshot(
            stamp, repair_required=repair_required)

    ownership = _derived_write_ownership(for_write=True)
    if not ownership.writable:
        return _interactive_snapshot(
            stamp, repair_required=repair_required, ownership=ownership)
    # Foreground readers returned above; writer paths retain the ordinary
    # SQLite wait while validating the current publication.
    db = (
        None
        if (repair_required or ownership.adopt_legacy_db
            or ownership.replace_retained_db)
        else _valid_db(stamp, 5000)
    )
    if db is not None:
        indexd_runtime._set_fts_delegated(False)
        common.dbg("corpusdb: source stamp unchanged -> reuse current db (no refresh)")
        return db
    if indexd_runtime.fts_delegation_active():
        # The first run handed the build to indexd; the JSONL engine serves this
        # process rather than racing a second multi-minute build inline
        common.dbg("corpusdb: first-run FTS build delegated to indexd -> JSONL engine", "!")
        return None
    common.dbg("corpusdb: source moved -> refresh needed")
    if not quiet:
        common.log("refreshing search index…")
    with _ConnectIndexLock(False) as lock_acquired:
        if not lock_acquired:
            return None
        stamp = _stamp()
        repair_required = query_rebuild_required()
        ownership = _derived_write_ownership(for_write=True)
        if not ownership.writable:
            return _interactive_snapshot(
                stamp, repair_required=repair_required, ownership=ownership)
        if ownership.adopt_legacy_db:
            if not _adopt_legacy_database_owner():
                observed = _derived_write_ownership(for_write=True)
                refused = (
                    observed
                    if not observed.writable
                    else _DerivedWriteOwnership(
                        "refused",
                        "legacy corpus.db ownership could not be committed")
                )
                return _interactive_snapshot(
                    stamp, repair_required=repair_required,
                    ownership=refused)
            ownership = _derived_write_ownership(for_write=True)
            if not ownership.writable:
                return _interactive_snapshot(
                    stamp, repair_required=repair_required,
                    ownership=ownership)
        db = (
            None
            if repair_required or ownership.replace_retained_db
            else _valid_db(stamp)
        )
        if db is not None:
            common.dbg("corpusdb: another process refreshed under lock -> reuse")
            return db
        changed = _read_changed()
        n = ("*" if changed == "*" else len(changed) if changed else 0)
        common.dbg(f"corpusdb: changed-session delta = {n} session(s)")
        db = (
            None
            if repair_required or ownership.replace_retained_db
            else _incremental(stamp)
        )
        if db is not None:
            common.dbg("corpusdb: incremental refresh applied (only changed sessions re-indexed)")
            return db
        ownership = _derived_write_ownership(for_write=True)
        if not ownership.writable:
            return _interactive_snapshot(
                stamp, repair_required=repair_required, ownership=ownership)
        common.dbg("corpusdb: full rebuild (schema bump / cold / bulk row-shape move / invalid db)")
        return _rebuild_and_publish(stamp, repair_required)


def generation(db: sqlite3.Connection) -> tuple[str, str]:
    """Stable identity of the corpus snapshot behind an open connection."""
    meta = dict(db.execute(
        "SELECT key, value FROM meta WHERE key IN ('schema', 'stamp')"))
    return meta.get("schema", ""), meta.get("stamp", "")


# ------------------------------------------------------------------ query engines

def _fts_quote(tok: str) -> str:
    return '"' + tok.replace('"', '""') + '"'


def _like_quote(tok: str) -> str:
    esc = tok.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


_RE_I_FOLD = str.maketrans({"İ": "i", "ı": "i", "ſ": "s", "K": "k"})
_RE_I_RARE_SQL = ("instr(text, 'İ') > 0 OR instr(text, 'ı') > 0 "
                  "OR instr(text, 'ſ') > 0 OR instr(text, 'K') > 0")


def _re_i_canonical(token: str) -> str:
    return token.translate(_RE_I_FOLD)


def _needs_re_i_rare(token: str) -> bool:
    return any(char in _re_i_canonical(token).lower() for char in "isk")


def _like_superset(toks: list[str]) -> tuple[list[str], list[str]]:
    where: list[str] = []
    params: list[str] = []
    for token in toks:
        exceptions = ["instr(CAST(text AS BLOB), X'00') > 0"]
        if _needs_re_i_rare(token):
            exceptions.append(f"({_RE_I_RARE_SQL})")
        if not token.isascii() and (
                token.lower() != token or token.upper() != token):
            # LIKE cannot case-fold unicode, so CASED non-ascii tokens widen
            # to every non-ascii row; a caseless token (emoji, CJK) has no fold
            # to miss, and the blanket clause cost 351k python confirms.
            exceptions.append("length(CAST(text AS BLOB)) != length(text)")
        where.append("(text LIKE ? ESCAPE '\\' OR " + " OR ".join(exceptions) + ")")
        params.append(_like_quote(_re_i_canonical(token)))
    return where, params


def _register_functions(db: sqlite3.Connection) -> None:
    installed = {str(row[0]).lower() for row in db.execute("PRAGMA function_list")}
    functions = (
        ("agrep_contains_ci", lambda actual, needle: int(
            (needle or "").lower() in (actual or "").lower())),
        ("agrep_starts_ci", lambda actual, needle: int(
            (actual or "").lower().startswith((needle or "").lower()))),
        ("agrep_equal_ci", lambda actual, needle: int(
            (actual or "").lower() == (needle or "").lower())),
    )
    for name, function in functions:
        if name not in installed:
            db.create_function(name, 2, function, deterministic=True)


def _filter_sql(flt: dict | None) -> tuple[list[str], list]:
    """SQL predicates with the same Unicode-lower semantics as search._filtered."""
    where: list[str] = []
    params: list = []
    if not flt:
        return where, params
    if flt.get("agent"):
        where.append("agrep_contains_ci(agent, ?)")
        params.append(flt["agent"])
    if flt.get("project"):
        where.append("agrep_contains_ci(project, ?)")
        params.append(flt["project"])
    if flt.get("chat"):  # 8-char id prefix or full session uuid
        where.append("agrep_starts_ci(session, ?)")
        params.append(flt["chat"])
    excluded_sessions = flt.get("_exclude_sessions")
    if excluded_sessions:
        if (not isinstance(excluded_sessions, (list, tuple, set, frozenset))
                or len(excluded_sessions) > _EXACT_SESSION_EXCLUSION_MAX
                or any(not isinstance(session, str) or not session
                       or len(session) > 1024 for session in excluded_sessions)):
            raise ValueError("exact session exclusions are invalid")
        exact = tuple(sorted(set(excluded_sessions)))
        if len(exact) <= _EXACT_SESSION_EXCLUSION_INLINE_MAX:
            where.append(f"session NOT IN ({','.join('?' for _ in exact)})")
            params.extend(exact)
        else:
            where.append(
                "session NOT IN (SELECT value FROM json_each(?))")
            params.append(json.dumps(exact, separators=(",", ":")))
    if flt.get("exclude_session"):
        boundary = flt.get("exclude_session_from_turn")
        exclude_family = flt.get("exclude_family", True)
        if boundary is not None:
            where.append(
                "NOT (session = ? AND typeof(turn) = 'integer' AND turn >= ?)")
            params.extend((flt["exclude_session"], int(boundary)))

        else:
            where.append("session <> ?")
            params.append(flt["exclude_session"])
            if exclude_family:
                where.append(
                    "session NOT IN ("
                    "SELECT member.session FROM session_family AS member "
                    "JOIN session_family AS caller ON caller.root=member.root "
                    "WHERE caller.session=?)"
                )
                params.append(flt["exclude_session"])
    who = flt.get("who")
    if isinstance(who, str) and who:
        where.append("who = ?")
        params.append(who)
    elif who is not None:
        # surface_policy.SpeakerFilter: an include list, or a pure exclusion
        include = getattr(who, "include", None)
        exclude = tuple(getattr(who, "exclude", ()) or ())
        if include:
            where.append(f"who IN ({','.join('?' * len(include))})")
            params.extend(include)
        if exclude:
            where.append(f"who NOT IN ({','.join('?' * len(exclude))})")
            params.extend(exclude)
    elif flt.get("include_tools") is False:
        where.append("who <> 'tool'")
    if flt.get("model"):
        if flt.get("model_soft"):
            where.append("agrep_contains_ci(model, ?)")
            params.append(flt["model"])
        else:
            where.append("agrep_equal_ci(model, ?)")
            params.append(flt["model"])
    if flt.get("since_ms") is not None:
        where.append("coalesce(ts, 0) >= ?")
        params.append(flt["since_ms"])
    if flt.get("until_ms") is not None:
        where.append("coalesce(ts, 0) < ?")
        params.append(flt["until_ms"])
    return where, params


def _distinct_tokens(toks: list[str]) -> list[str]:
    """First-occurrence-ordered distinct tokens.

    Every candidate predicate below is a conjunction of one clause per token,
    and ``AND`` is idempotent: repeating a token cannot change which rows
    qualify, only how many times each is re-tested. A pasted log line
    ("deadlock" x1200) otherwise multiplies the FTS posting walk and the
    per-row span probe by its own repetition count. Dedupe is on the exact
    token so every emitted clause stays byte-identical.
    """
    return list(dict.fromkeys(toks))


def _candidate_where(toks: list[str], flt: dict | None = None) -> tuple[str, list]:
    """SQL predicate/params shared by exhaustive and bounded candidate readers.

    LIKE clauses for short tokens are a superset filter: SQLite LIKE is ASCII-only
    and stops at NUL, while Python re.I has four extra Unicode equivalences. Candidate
    predicates widen for both differences; exact confirmation stays in Python."""
    toks = _distinct_tokens(toks)
    fts = [t for t in toks if len(t) >= 3]
    likes = [t for t in toks if len(t) < 3]
    fts_table = _fts_table(flt)
    if fts:
        indexed = f"id IN (SELECT rowid FROM {fts_table} WHERE {fts_table} MATCH ?)"
        if any(_needs_re_i_rare(token) for token in fts):
            rare = ("id IN (SELECT id FROM msgs INDEXED BY msgs_re_i_exceptions "
                    f"WHERE {_RE_I_RARE_SQL})")
            indexed = f"({indexed} OR {rare})"
        where = [indexed]
        params: list = [" AND ".join(_fts_quote(_re_i_canonical(t)) for t in fts)]
    else:
        where, params = [], []
    like_where, like_params = _like_superset(likes)
    where += like_where
    params += like_params
    fw, fp = _filter_sql(flt)
    where += fw
    params += fp
    return (" WHERE " + " AND ".join(where) if where else ""), params


def _candidates(db: sqlite3.Connection, toks: list[str], flt: dict | None = None):
    """Rows that contain every token, via the cheapest applicable index: trigram FTS
    for >=3-char tokens, indexed LIKE for the stubs. Filters ride the same WHERE, so
    a filtered query never materializes rows the caller would drop. Yields msgs rows."""
    _register_functions(db)
    where, params = _candidate_where(toks, flt)
    sel = ("SELECT session, agent, project, concept, model, model_source, "
           "turn, ts, who, text, content_digest FROM msgs")
    return db.execute(sel + where, params)


def candidate_count_capped(db: sqlite3.Connection, toks: list[str], flt: dict | None,
                           cap: int) -> int:
    """Count candidates only through ``cap`` for an adaptive bounded-search gate.

    This deliberately counts the indexed superset, not confirmed phrase hits. Its only
    contract is answering "is this candidate lane large enough to justify the ordered
    path?" without paying a corpus-wide count first.
    """
    _register_functions(db)
    cap = max(1, int(cap))
    toks = _distinct_tokens(toks)
    active = {name for name, value in (flt or {}).items()
              if name != "include_tools" and value is not None and value is not False}
    if toks and all(len(token) >= 3 for token in toks) and not active:
        table = _fts_table(flt)
        query = " AND ".join(
            _fts_quote(_re_i_canonical(token)) for token in toks)
        sql = (f"SELECT count(*) FROM (SELECT rowid FROM {table} "
               f"WHERE {table} MATCH ? LIMIT ?)")
        indexed = int(db.execute(sql, (query, cap)).fetchone()[0])
        if indexed >= cap or not any(_needs_re_i_rare(token) for token in toks):
            return indexed
        filtered, filter_params = _filter_sql(flt)
        predicate = " AND ".join([f"({_RE_I_RARE_SQL})", *filtered])
        rare_sql = (
            "SELECT count(*) FROM (SELECT m.id FROM msgs AS m "
            "INDEXED BY msgs_re_i_exceptions WHERE " + predicate
            + f" AND NOT EXISTS (SELECT 1 FROM {table} "
            + f"WHERE {table}.rowid = m.id AND {table} MATCH ?) LIMIT ?)")
        rare = int(db.execute(
            rare_sql, [*filter_params, query, cap - indexed]).fetchone()[0])
        return min(cap, indexed + rare)
    where, params = _candidate_where(toks, flt)
    sql = f"SELECT count(*) FROM (SELECT id FROM msgs{where} LIMIT ?)"
    return int(db.execute(sql, [*params, cap]).fetchone()[0])


def _posting_intersection_floor(
        db: sqlite3.Connection, toks: list[str], flt: dict | None,
        population: int) -> int | None:
    active = {name for name, value in (flt or {}).items()
              if name != "include_tools" and value is not None and value is not False}
    canonical = list(dict.fromkeys(_re_i_canonical(token).lower() for token in toks))
    if (len(canonical) < 2 or active
            or any(len(token) < 3 or not token.isascii() or not token.isalnum()
                   for token in canonical)):
        return None
    table = _fts_table(flt)
    try:
        counts = [int(db.execute(
            f"SELECT count(*) FROM {table} WHERE {table} MATCH ?",
            (_fts_quote(token),)).fetchone()[0]) for token in canonical]
    except sqlite3.DatabaseError:
        return None
    return max(0, sum(counts) - (len(counts) - 1) * population)


def dense_candidate_lane(db: sqlite3.Connection, toks: list[str],
                         flt: dict | None) -> bool:
    """Choose a bounded recency walk over a large posting materialization."""
    _register_functions(db)
    active = {name for name, value in (flt or {}).items()
              if name not in {
                  "include_tools", "exclude_session",
                  "exclude_session_from_turn", "exclude_family",
                  "_exclude_sessions"}
              and value is not None and value is not False}
    if active:
        # A capped filtered count repeats the same expensive posting walk; the
        # metadata path preserves ordering while paying that predicate only once.
        return False
    filtered, params = _filter_sql(flt)
    where = " WHERE " + " AND ".join(filtered) if filtered else ""
    population = int(db.execute("SELECT count(*) FROM msgs" + where, params).fetchone()[0])
    if population < 1_000:
        return False
    floor = _posting_intersection_floor(db, toks, flt, population)
    if floor is not None and (floor > 100_000 or floor * 10 >= population * 9):
        return True
    candidates = candidate_count_capped(db, toks, flt, 100_001)
    return candidates > 100_000 or candidates * 10 >= population * 9


def dense_phrase_preflight(db: sqlite3.Connection, toks: list[str], flt: dict | None,
                           limit: int) -> tuple[bool, list[tuple[tuple, int, int]]]:
    """Return every phrase row when the phrase tier is thinner than ``limit``.

    ``complete=False`` means the scan reached ``limit`` and stopped; the ordered
    candidate lane still owns the top-k. A complete thin result lets that lane settle
    the remaining all-terms slots without rescoring the whole dense posting set.
    """
    if len(toks) < 2 or limit <= 0:
        return False, []
    _register_functions(db)
    filtered, params = _filter_sql(flt)
    where = " WHERE " + " AND ".join(filtered) if filtered else ""
    pattern = re.compile(r"[\W_]*".join(re.escape(token) for token in toks), re.I)
    refs: list[tuple[int, int, int]] = []
    started = time.perf_counter()
    examined = 0
    cursor = db.execute("SELECT id, text FROM msgs" + where, params)
    exhausted = True
    try:
        for rowid, text in cursor:
            examined += 1
            match = pattern.search(text)
            if match is None:
                continue
            refs.append((int(rowid), match.start(), match.end()))
            if len(refs) >= limit:
                exhausted = False
                break
    finally:
        cursor.close()
    common.dbg(
        f"dense phrase preflight: examined {examined} row(s), found {len(refs)}, "
        f"complete={exhausted} in {(time.perf_counter() - started) * 1000:.1f}ms")
    if not exhausted or not refs:
        return exhausted, []
    ids = [rowid for rowid, _start, _end in refs]
    marks = ",".join("?" for _ in ids)
    rows = {
        int(row[0]): tuple(row[1:])
        for row in db.execute(
            "SELECT id, session, agent, project, concept, model, model_source, "
            f"turn, ts, who, text, content_digest "
            f"FROM msgs WHERE id IN ({marks})", ids)
    }
    return True, [(rows[rowid], start, end) for rowid, start, end in refs]


@dataclass(frozen=True, slots=True)
class ShortKeywordCandidate:
    """One widened LIKE-lane row, with its exact Python match result.

    ``rounded_score_ceiling`` is rounded *up* to the four decimal places used by
    search ranking.  A caller may settle a top-k only when ``strictly_below`` is
    true for its current kth score.  Equality is deliberately not enough: an unseen
    equal-score row can still win the timestamp/session/turn/speaker tie-breaks.

    ``span is None`` means SQLite admitted a widening-only false positive (for
    example a nonmatching row containing NUL or one of Python re.I's rare folds).
    The row was still examined and is intentionally surfaced so the caller can
    apply the ceiling test before doing any ranking work.
    """

    score_ceiling: float
    rounded_score_ceiling: float
    row: tuple
    span: tuple[int, int] | None
    lowered: str | None = None
    family_root: str = ""

    @property
    def matched(self) -> bool:
        return self.span is not None

    def strictly_below(self, rounded_score: float) -> bool:
        """Whether this and every later row are unable to tie ``rounded_score``.

        Invalid thresholds fail closed: they never authorize early termination.
        """
        try:
            score = float(rounded_score)
        except (TypeError, ValueError):
            return False
        return math.isfinite(score) and self.rounded_score_ceiling < score


_SHORT_DISTINCT_TRACK_MAX = 4_096


class _BoundedDistinctCounter:
    """Retain a fixed-size distinct lower bound and disclose overflow."""

    __slots__ = ("_limit", "_values", "exact")

    def __init__(self, limit: int | None = None):
        self._limit = max(0, int(
            _SHORT_DISTINCT_TRACK_MAX if limit is None else limit))
        self._values: set[str] = set()
        self.exact = True

    def add(self, value: str) -> None:
        if value in self._values:
            return
        if len(self._values) >= self._limit:
            self.exact = False
            return
        self._values.add(value)

    def __len__(self) -> int:
        return len(self._values)


@dataclass(frozen=True, slots=True)
class ShortKeywordProgress:
    """Observed exact-match lower bounds for a short-keyword stream snapshot."""

    candidates_examined: int
    observed_total: int
    observed_chats: int
    observed_tool_hits: int
    exhausted: bool
    stopped: bool
    chat_count_exact: bool

    @property
    def totals_exact(self) -> bool:
        return self.exhausted and self.chat_count_exact


def _upward_score_round(value: float) -> float:
    """Conservative four-place ceiling for scores rounded by ``search._score``.

    The tiny outward nudge covers floating-operation order differences between the
    ceiling and exact scorer.  It can retain one adjacent score band, but can never
    discard a row whose rounded score ties the retained frontier.
    """
    return math.ceil((value + 1e-12) * 10_000.0) / 10_000.0


class ShortKeywordCandidateStream:
    """K-way merge of per-speaker ``msgs_who_ts`` streams for a 1-2ch token.

    Each speaker cursor follows descending recency.  Its score ceiling is therefore
    monotone too, including the old-user plateau introduced by ``user_recency_floor``.
    Only one row per cursor is kept in the merge heap.  The successor is fetched on
    the next iteration rather than before yielding, so an early-stop decision never
    pays for a needless tail scan.

    Consume to ``StopIteration`` for exact totals, or call ``stop()``/``close()``
    after a sound ceiling decision.  In the latter case ``progress`` remains an
    explicit observed lower bound.
    """

    def __init__(
            self, db: sqlite3.Connection, q: str, flt: dict | None, *,
            now_ms: float, who_weights: dict[str, float],
            source_scales: dict[str, float], recency_half_life_days: float,
            user_recency_floor: float, boundary_ceiling: float = 1.0,
            only_family: str | None = None,
            exclude_families: Iterable[str] = ()):
        _register_functions(db)
        self._db = db
        self._query = q
        self._now_ms = self._finite_nonnegative(now_ms, "now_ms")
        half_life_days = self._finite_nonnegative(
            recency_half_life_days, "recency_half_life_days")
        if half_life_days == 0.0:
            raise ValueError("recency_half_life_days must be positive")
        self._half_life_ms = half_life_days * 86_400_000.0
        if not math.isfinite(self._half_life_ms):
            raise ValueError("recency_half_life_days is too large")
        self._user_floor = self._finite_nonnegative(
            user_recency_floor, "user_recency_floor")
        self._who_weights = self._validated_policy(who_weights, "who_weights")
        self._source_scales = self._validated_policy(
            source_scales, "source_scales")
        self._boundary_ceiling = self._finite_nonnegative(
            boundary_ceiling, "boundary_ceiling")
        if self._boundary_ceiling > 1.0:
            raise ValueError("boundary_ceiling must not exceed one")
        if only_family is not None and (
                not isinstance(only_family, str) or not only_family):
            raise ValueError("only_family must be a nonempty string")
        excluded_roots = tuple(dict.fromkeys(
            str(root) for root in exclude_families if root))
        if only_family is not None and excluded_roots:
            raise ValueError("only_family and exclude_families are mutually exclusive")
        self._heap: list[tuple] = []
        self._cursors: list[sqlite3.Cursor] = []
        self._pending_cursor: sqlite3.Cursor | None = None
        self._serial = 0
        self._candidates_examined = 0
        self._observed_total = 0
        self._observed_chats = _BoundedDistinctCounter()
        self._observed_tool_hits = 0
        self._exhausted = False
        self._stopped = False
        self._closed = False

        normalized_filter = self._validate_filter(flt)
        boundary = normalized_filter.get("exclude_session_from_turn")
        caller = str(normalized_filter.get("exclude_session") or "")
        exclude_family = normalized_filter.pop("exclude_family", True)
        if boundary is None:
            normalized_filter.pop("exclude_session", None)
        caller_row = (
            db.execute(
                "SELECT root FROM session_family WHERE session=?", (caller,)
            ).fetchone()
            if caller and boundary is None and exclude_family else None
        )
        caller_root = str(caller_row[0]) if caller_row and caller_row[0] else ""
        if caller_root:
            excluded_roots = tuple(dict.fromkeys((*excluded_roots, caller_root)))
        self._excluded_sessions = (
            frozenset({caller})
            if caller and not caller_root and boundary is None else frozenset())
        where, params = _candidate_where([q], normalized_filter)
        predicate = where.removeprefix(" WHERE ") or "1"
        group_where = " WHERE who <> 'tool'" if (
            normalized_filter.get("include_tools") is False) else ""
        self._own_transaction = not db.in_transaction
        group_cursor = None
        try:
            if self._own_transaction:
                db.execute("BEGIN")
            # DISTINCT can walk the leading index column without touching message text.
            group_cursor = db.execute(
                "SELECT DISTINCT who FROM msgs INDEXED BY msgs_who_ts" + group_where)
            groups = [row[0] for row in group_cursor]
            group_cursor.close()
            group_cursor = None
            if only_family is not None and only_family == caller_root:
                statements = ()
            elif only_family is None:
                family_sql = ""
                family_params: list[str] = []
                if excluded_roots:
                    marks = ",".join("?" for _ in excluded_roots)
                    family_sql = (
                        "session NOT IN (SELECT session FROM session_family "
                        f"WHERE root IN ({marks})) "
                        f"AND session NOT IN ({marks}) AND "
                    )
                    family_params = [*excluded_roots, *excluded_roots]
                select = (
                    "SELECT session, agent, project, concept, model, model_source, "
                    "turn, ts, who, text, content_digest, coalesce(("
                    "SELECT root FROM session_family "
                    "WHERE session=msgs.session), msgs.session) "
                    "FROM msgs INDEXED BY msgs_who_ts "
                    f"WHERE who IS ? AND {family_sql}{predicate} "
                    "ORDER BY coalesce(ts, 0) DESC"
                )
                statements = (
                    (select, [group, *family_params, *params]) for group in groups
                )
            else:
                indexed = db.execute(
                    "SELECT 1 FROM session_family WHERE root=? LIMIT 1",
                    (only_family,),
                ).fetchone()
                if indexed:
                    select = (
                        "SELECT msgs.session, agent, project, concept, model, "
                        "model_source, turn, ts, who, text, content_digest, family.root "
                        "FROM session_family AS family "
                        "INDEXED BY session_family_root "
                        "JOIN msgs INDEXED BY msgs_session "
                        "ON msgs.session=family.session "
                        f"WHERE family.root=? AND who IS ? AND {predicate} "
                        "ORDER BY coalesce(ts, 0) DESC"
                    )
                    statements = (
                        (select, [only_family, group, *params]) for group in groups
                    )
                else:
                    select = (
                        "SELECT session, agent, project, concept, model, model_source, "
                        "turn, ts, who, text, content_digest, session "
                        "FROM msgs INDEXED BY msgs_session "
                        f"WHERE session=? AND who IS ? AND {predicate} "
                        "ORDER BY coalesce(ts, 0) DESC"
                    )
                    statements = (
                        (select, [only_family, group, *params]) for group in groups
                    )
            for select, values in statements:
                cursor = db.execute(select, values)
                self._cursors.append(cursor)
                self._push_next(cursor)
            if not self._heap:
                self._finish(exhausted=True)
        except Exception:
            if group_cursor is not None:
                group_cursor.close()
            self._finish(exhausted=False)
            raise

    @staticmethod
    def _finite_nonnegative(value: float, name: str) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be a finite nonnegative number") from exc
        if not math.isfinite(number) or number < 0.0:
            raise ValueError(f"{name} must be a finite nonnegative number")
        return number

    @classmethod
    def _validated_policy(cls, values: dict[str, float], name: str) -> dict[str, float]:
        if not isinstance(values, dict):
            raise ValueError(f"{name} must be a mapping")
        return {key: cls._finite_nonnegative(value, f"{name}[{key!r}]")
                for key, value in values.items()}

    @staticmethod
    def _validate_filter(flt: dict | None) -> dict:
        if flt is None:
            return {}
        if not isinstance(flt, dict):
            raise ValueError("short-keyword lane filters must be a mapping")
        known = {"agent", "project", "who", "model", "model_soft", "chat",
                 "since_ms", "until_ms", "include_tools", "exclude_session",
                 "exclude_session_from_turn", "exclude_family",
                 "_exclude_sessions"}
        if set(flt) - known:
            raise ValueError("short-keyword lane received an unknown filter")
        if (any(flt.get(name) for name in (
                "agent", "project", "who", "model", "model_soft", "chat"))
                or flt.get("since_ms") is not None
                or flt.get("until_ms") is not None):
            raise ValueError("short-keyword lane requires an unfiltered corpus")
        include_tools = flt.get("include_tools", True)
        if not isinstance(include_tools, bool):
            raise ValueError("short-keyword lane include_tools must be boolean")
        normalized = {} if include_tools else {"include_tools": False}
        if flt.get("_exclude_sessions"):
            excluded_sessions = flt["_exclude_sessions"]
            if (not isinstance(excluded_sessions, (list, tuple, set, frozenset))
                    or len(excluded_sessions) > _EXACT_SESSION_EXCLUSION_MAX
                    or any(not isinstance(session, str) or not session
                           or len(session) > 1024
                           for session in excluded_sessions)):
                raise ValueError(
                    "short-keyword lane exact session exclusions are invalid")
            normalized["_exclude_sessions"] = tuple(
                sorted(set(excluded_sessions)))
        exclude_session = flt.get("exclude_session")
        if exclude_session:
            if not isinstance(exclude_session, str):
                raise ValueError(
                    "short-keyword lane exclude_session must be a string")
            normalized["exclude_session"] = exclude_session
        exclude_family = flt.get("exclude_family", True)
        if not isinstance(exclude_family, bool):
            raise ValueError(
                "short-keyword lane exclude_family must be boolean")
        if not exclude_family:
            if not exclude_session:
                raise ValueError(
                    "short-keyword lane exclude_family requires a session")
            normalized["exclude_family"] = False
        boundary = flt.get("exclude_session_from_turn")
        if boundary is not None:
            if (not exclude_session or not isinstance(boundary, int)
                    or isinstance(boundary, bool) or boundary < 0):
                raise ValueError(
                    "short-keyword lane window exclusion is invalid")
            normalized["exclude_session_from_turn"] = boundary
        return normalized

    def _score_ceiling(self, row: tuple) -> float:
        speaker = row[8] or ""
        try:
            timestamp = float(row[7] or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("short-keyword row timestamp is not numeric") from exc
        if not math.isfinite(timestamp):
            raise ValueError("short-keyword row timestamp is not finite")
        age = max(0.0, self._now_ms - timestamp)
        recency = 0.5 ** (age / self._half_life_ms)
        if speaker == "user":
            recency = max(recency, self._user_floor)
        boundary_ceiling = 1.0 if speaker == "tool" else self._boundary_ceiling
        ceiling = (recency * self._who_weights.get(speaker, 0.5)
                   * self._source_scales.get(speaker, 1.0)
                   * boundary_ceiling)
        if not math.isfinite(ceiling):
            raise ValueError("short-keyword score ceiling is not finite")
        return ceiling

    def _push_next(self, cursor: sqlite3.Cursor) -> None:
        raw = cursor.fetchone()
        if raw is None:
            return
        row = tuple(raw[:_DIGEST + 1])
        family_root = str(raw[_DIGEST + 1] or row[0])
        ceiling = self._score_ceiling(row)
        timestamp = float(row[7] or 0)
        turn = -1 if row[6] is None else int(row[6])
        # Mirror score ranking's tie fields to reach the likely head winner first;
        # strict stopping, rather than this secondary order, provides correctness.
        key = (-ceiling, -timestamp, str(row[0]), turn, str(row[8] or ""),
               self._serial, row, family_root, cursor)
        self._serial += 1
        heapq.heappush(self._heap, key)

    def __iter__(self):
        return self

    def __next__(self) -> ShortKeywordCandidate:
        if self._closed:
            raise StopIteration
        try:
            while True:
                if self._pending_cursor is not None:
                    cursor, self._pending_cursor = self._pending_cursor, None
                    self._push_next(cursor)
                if not self._heap:
                    self._finish(exhausted=True)
                    raise StopIteration
                entry = heapq.heappop(self._heap)
                ceiling, row, family_root, cursor = (
                    -entry[0], entry[-3], entry[-2], entry[-1])
                self._pending_cursor = cursor
                self._candidates_examined += 1
                if row[0] not in self._excluded_sessions:
                    break
            text = row[_TEXT]
            lowered = text.lower() if text.isascii() else None
            if lowered is None:
                span = common.insensitive_span(text, self._query)
            else:
                start = lowered.find(self._query)
                span = None if start < 0 else (start, start + len(self._query))
            if span is not None:
                self._observed_total += 1
                self._observed_chats.add(row[0])
                self._observed_tool_hits += (row[8] or "") == "tool"
            return ShortKeywordCandidate(
                score_ceiling=ceiling,
                rounded_score_ceiling=_upward_score_round(ceiling),
                row=row,
                span=span,
                lowered=lowered,
                family_root=family_root,
            )
        except StopIteration:
            raise
        except Exception:
            self._finish(exhausted=False)
            raise

    @property
    def progress(self) -> ShortKeywordProgress:
        return ShortKeywordProgress(
            candidates_examined=self._candidates_examined,
            observed_total=self._observed_total,
            observed_chats=len(self._observed_chats),
            observed_tool_hits=self._observed_tool_hits,
            exhausted=self._exhausted,
            stopped=self._stopped,
            chat_count_exact=self._observed_chats.exact,
        )

    def _finish(self, *, exhausted: bool) -> None:
        if exhausted:
            self._exhausted = True
        elif not self._exhausted:
            self._stopped = True
        for cursor in self._cursors:
            try:
                cursor.close()
            except sqlite3.Error:
                pass
        self._cursors.clear()
        self._heap.clear()
        self._pending_cursor = None
        self._closed = True
        if self._own_transaction and self._db.in_transaction:
            try:
                self._db.rollback()
            except sqlite3.Error:
                pass

    def stop(self) -> None:
        """Close an intentionally pruned stream, preserving lower-bound progress."""
        if not self._closed:
            self._finish(exhausted=False)

    close = stop

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self.close()


def bounded_short_keyword_candidates(
        db: sqlite3.Connection, q: str, flt: dict | None, *, now_ms: float,
        who_weights: dict[str, float], source_scales: dict[str, float],
        recency_half_life_days: float,
        user_recency_floor: float,
        boundary_ceiling: float = 1.0,
        exclude_families: Iterable[str] = ()) -> ShortKeywordCandidateStream:
    """Open the bounded 1-2 ASCII-alphanumeric LIKE lane.

    This intentionally performs no exhaustive count or temporary candidate-table
    build.  Exact Python confirmation and observed aggregate accounting happen as the
    per-speaker merge advances.  Unsupported query/filter shapes and a missing
    ``msgs_who_ts`` index raise, so callers can fail closed to the exhaustive engine.
    """
    ql = q.strip().lower()
    if len(ql) not in (1, 2) or not ql.isascii() or not ql.isalnum():
        raise ValueError(
            "bounded short-keyword lane requires 1-2 ASCII alphanumeric characters")
    return ShortKeywordCandidateStream(
        db, ql, flt, now_ms=now_ms, who_weights=who_weights,
        source_scales=source_scales,
        recency_half_life_days=recency_half_life_days,
        user_recency_floor=user_recency_floor,
        boundary_ceiling=boundary_ceiling,
        exclude_families=exclude_families,
    )


def bounded_short_keyword_family_candidates(
        db: sqlite3.Connection, q: str, family_root: str,
        flt: dict | None, *, now_ms: float,
        who_weights: dict[str, float], source_scales: dict[str, float],
        recency_half_life_days: float,
        user_recency_floor: float,
        boundary_ceiling: float = 1.0) -> ShortKeywordCandidateStream:
    """Open one family's bounded stream in the same ceiling order as the global lane."""
    ql = q.strip().lower()
    if len(ql) not in (1, 2) or not ql.isascii() or not ql.isalnum():
        raise ValueError(
            "bounded short-keyword lane requires 1-2 ASCII alphanumeric characters")
    return ShortKeywordCandidateStream(
        db, ql, flt, now_ms=now_ms, who_weights=who_weights,
        source_scales=source_scales,
        recency_half_life_days=recency_half_life_days,
        user_recency_floor=user_recency_floor,
        boundary_ceiling=boundary_ceiling,
        only_family=family_root,
    )


def _exact_single_keyword_metadata(
        rows, ql: str) -> tuple[tuple[int, int, int], set[str | None]]:
    """Count candidate metadata using exactly the one-token keyword matcher.

    For an ASCII-alphanumeric needle, a true SQLite ``LIKE`` is also necessarily a
    Python-lowercase substring match. SQL represents that common case with ``NULL``.
    False ``LIKE`` results are only candidates, not rejections: Unicode compatibility
    folds and content beyond a NUL are checked here with the canonical Python operation.
    """
    total = tool_hits = 0
    chats: set[str] = set()
    groups: set[str | None] = set()
    for session, who, unproved_text in rows:
        if unproved_text is not None and common.insensitive_span(unproved_text, ql) is None:
            continue
        total += 1
        chats.add(session)
        speaker = who or ""
        groups.add(who)
        tool_hits += speaker == "tool"
    return (total, len(chats), tool_hits), groups


def bounded_single_keyword_candidates(
        db: sqlite3.Connection, q: str, flt: dict | None, *, now_ms: float,
        who_weights: dict[str, float], source_scales: dict[str, float],
        recency_half_life_days: float, user_recency_floor: float):
    """Return exact aggregates plus candidates in descending safe-ceiling order.

    A temporary rowid set pays the broad FTS posting-list walk once. One cursor per
    speaker then follows ``msgs_who_ts`` in recency order; within a speaker, recency is
    the only changing part of the score ceiling. A tiny heap merges those streams. This
    avoids SQLite's corpus-sized expression sort and lets the caller stop after its top
    rows are mathematically settled.

    The lane intentionally accepts only the unfiltered all/prose corpus. Filtered lanes
    keep using the ordinary SQL predicate, where candidate counts are normally small.
    """
    import heapq
    _register_functions(db)

    flt = flt or {}
    if (any(flt.get(name) for name in (
            "agent", "project", "who", "model", "model_soft", "chat"))
            or flt.get("since_ms") is not None
            or flt.get("until_ms") is not None):
        raise ValueError("bounded single-keyword lane requires an unfiltered corpus")
    ql = q.strip().lower()
    if len(ql) < 3 or not ql.isascii() or not ql.isalnum():
        raise ValueError("bounded single-keyword lane requires an ASCII trigram")

    temp = "agrep_keyword_candidates"
    db.execute(f"CREATE TEMP TABLE IF NOT EXISTS {temp} "
               "(id INTEGER PRIMARY KEY) WITHOUT ROWID")
    db.execute(f"DELETE FROM {temp}")
    # Populate via the canonical candidate predicate: it selects the prose/full index and
    # widens the superset for the Unicode folds FTS5 and Python disagree on (dotted-I).
    where, params = _candidate_where([ql], flt)
    db.execute(f"INSERT INTO {temp}(id) SELECT id FROM msgs{where}", params)

    # Preserve keyword()'s exact Python confirmation while keeping broad ASCII hits
    # narrow on the SQLite/Python boundary. The same pass also discovers speaker groups.
    metadata = db.execute(
        "SELECT m.session, m.who, "
        "CASE WHEN coalesce(m.text, '') LIKE ? THEN NULL "
        "ELSE coalesce(m.text, '') END "
        f"FROM {temp} AS c JOIN msgs AS m ON m.id = c.id", (f"%{ql}%",))
    exact_stats, groups = _exact_single_keyword_metadata(metadata, ql)
    select = ("SELECT m.session, m.agent, m.project, m.concept, m.model, "
              "m.model_source, m.turn, m.ts, m.who, m.text, m.content_digest "
              "FROM msgs AS m "
              "INDEXED BY msgs_who_ts WHERE m.who IS ? "
              f"AND m.id IN (SELECT id FROM {temp}) "
              "ORDER BY coalesce(m.ts, 0) DESC")
    cursors = [db.execute(select, (group,)) for group in groups]
    half_life_ms = max(1.0, float(recency_half_life_days) * 86_400_000.0)

    def ceiling(row) -> float:
        speaker = row[8] or ""
        age = max(0.0, float(now_ms) - float(row[7] or 0))
        recency = 0.5 ** (age / half_life_ms)
        if speaker == "user":
            recency = max(recency, float(user_recency_floor))
        return (recency * float(who_weights.get(speaker, 0.5))
                * float(source_scales.get(speaker, 1.0)))

    def merged():
        heap = []
        serial = 0
        try:
            for cursor in cursors:
                row = cursor.fetchone()
                if row is not None:
                    heapq.heappush(heap, (-ceiling(row), serial, row, cursor))
                    serial += 1
            while heap:
                neg_ceiling, _serial, row, cursor = heapq.heappop(heap)
                yield -neg_ceiling, row
                row = cursor.fetchone()
                if row is not None:
                    heapq.heappush(heap, (-ceiling(row), serial, row, cursor))
                    serial += 1
        finally:
            for cursor in cursors:
                cursor.close()

    return exact_stats, merged()


def score_ceiling_candidates(
        db: sqlite3.Connection, toks: list[str], flt: dict | None, *, now_ms: float,
        who_weights: dict[str, float], source_scales: dict[str, float],
        recency_half_life_days: float, user_recency_floor: float,
        dense: bool | None = None, include_family: bool = False):
    """Yield ``(score_ceiling, msgs_row)`` in descending safe-ceiling order.

    Sparse postings sort lightweight metadata before hydrating bounded text pages. Dense
    lanes merge one monotone recency cursor per speaker. Boundary quality is at most one,
    so its unknown value cannot raise either ceiling.
    """
    _register_functions(db)
    if not toks:
        return
    dense = dense_candidate_lane(db, toks, flt) if dense is None else bool(dense)
    half_life_ms = max(1.0, float(recency_half_life_days) * 86_400_000.0)

    def ceiling_values(timestamp, speaker) -> float:
        speaker = speaker or ""
        age = max(0.0, float(now_ms) - float(timestamp or 0))
        recency = 0.5 ** (age / half_life_ms)
        if speaker == "user":
            recency = max(recency, float(user_recency_floor))
        return (recency * float(who_weights.get(speaker, 0.5))
                * float(source_scales.get(speaker, 1.0)))

    def ceiling(row: tuple) -> float:
        return ceiling_values(row[7], row[8])

    if not dense:
        common.dbg("ordered candidates: indexed posting sort")
        where, params = _candidate_where(toks, flt)
        cursor = db.execute(
            "SELECT id, session, turn, ts, who FROM msgs" + where, params)
        try:
            ordered = [tuple(row) for row in cursor]
        finally:
            cursor.close()
        ordered.sort(key=lambda row: (
            -ceiling_values(row[3], row[4]), -float(row[3] or 0), row[1],
            -1 if row[2] is None else row[2], row[4] or ""))
        for offset in range(0, len(ordered), 256):
            page = ordered[offset:offset + 256]
            marks = ",".join("?" for _ in page)
            family_sql = (
                ", coalesce((SELECT root FROM session_family "
                "WHERE session=msgs.session), msgs.session)"
                if include_family else "")
            hydrated = {
                int(row[0]): tuple(row[1:])
                for row in db.execute(
                    "SELECT id, session, agent, project, concept, model, model_source, "
                    f"turn, ts, who, text, content_digest{family_sql} "
                    f"FROM msgs WHERE id IN ({marks})",
                    [row[0] for row in page])
            }
            if len(hydrated) != len(page):
                raise sqlite3.DatabaseError("candidate row disappeared during hydration")
            for metadata in page:
                row = hydrated[int(metadata[0])]
                yield ceiling(row), row
        return

    filtered, params = _filter_sql(flt)
    like_where, like_params = (
        _like_superset(toks) if all(token.isascii() for token in toks) else ([], []))
    route = "user-LIKE-prefiltered" if like_where else "unfiltered"
    common.dbg(f"ordered candidates: dense recency walk ({route})")

    groups_sql = "SELECT DISTINCT who FROM msgs INDEXED BY msgs_who_ts"
    if flt and flt.get("include_tools") is False and not flt.get("who"):
        groups_sql += " WHERE who <> 'tool'"
    groups = [row[0] for row in db.execute(groups_sql)]
    family_sql = (
        ", coalesce((SELECT root FROM session_family "
        "WHERE session=msgs.session), msgs.session)"
        if include_family else "")
    cursors = []
    for group in groups:
        group_where = [*filtered]
        group_params = [*params]
        if group == "user" and like_where:
            group_where = [*like_where, *group_where]
            group_params = [*like_params, *group_params]
        group_where.append("who IS ?")
        group_params.append(group)
        select = (
            "SELECT session, agent, project, concept, model, model_source, "
            "turn, ts, who, text, content_digest" + family_sql
            + " FROM msgs INDEXED BY msgs_who_ts WHERE "
            + " AND ".join(group_where)
            + " ORDER BY coalesce(ts, 0) DESC"
        )
        cursors.append(db.execute(select, group_params))

    heap = []
    serial = 0
    try:
        for cursor in cursors:
            row = cursor.fetchone()
            if row is None:
                continue
            value = ceiling(row)
            heapq.heappush(heap, (-value, serial, tuple(row), cursor))
            serial += 1
        while heap:
            neg_value, _serial, row, cursor = heapq.heappop(heap)
            yield -neg_value, row
            successor = cursor.fetchone()
            if successor is not None:
                value = ceiling(successor)
                heapq.heappush(heap, (-value, serial, tuple(successor), cursor))
                serial += 1
    finally:
        for cursor in cursors:
            cursor.close()


def _fts_table(flt: dict | None) -> str:
    """Choose the smallest posting list that can satisfy the speaker filter."""
    who = (flt or {}).get("who")
    if who is None:
        prose_only = (flt or {}).get("include_tools") is False
    elif isinstance(who, str):
        prose_only = who != "tool"
    else:
        prose_only = not who.admits("tool")
    return "msgs_prose_fts" if prose_only else "msgs_fts"


def _row_digest(row: tuple) -> str:
    value = row[_DIGEST] if len(row) > _DIGEST else None
    if value is None:
        return compact.content_digest(row[_TEXT])
    try:
        return compact.require_content_digest(value)
    except compact.CompactError as exc:
        raise sqlite3.DatabaseError(
            "indexed row has an invalid content digest") from exc


def _hit(row, start: int, end: int) -> dict:
    h = dict(zip(_FIELDS, row[:_TEXT]))
    h["snippet"] = _snip_at(row[_TEXT], start, end)
    h["content_digest"] = _row_digest(row)
    h["_match_span"] = (start, end)
    if start <= 80 and end + 80 >= len(row[_TEXT]):
        h["_snippet_complete"] = True
    if h.get("who") == "tool":
        identity = common.tool_event_identity(
            h.get("session"), h.get("turn"), h.get("ts"), row[_TEXT])
        if identity is not None:
            h["_event_identity"] = identity
    return h


def _spans_hit(row, spans: list[tuple[int, int]]) -> dict:
    h = dict(zip(_FIELDS, row[:_TEXT]))
    h["snippet"] = _snip_spans(row[_TEXT], spans)
    h["content_digest"] = _row_digest(row)
    if spans:
        h["_match_span"] = min(spans, key=lambda span: (span[0], span[1]))
    if h.get("who") == "tool":
        identity = common.tool_event_identity(
            h.get("session"), h.get("turn"), h.get("ts"), row[_TEXT])
        if identity is not None:
            h["_event_identity"] = identity
    return h


def _lower_hit(row, lowered: str, start: int, end: int) -> dict:
    """Build a hit from indices into ``text.lower()``, preserving the ASCII fast path."""
    text = row[_TEXT]
    start, end = common.original_span_for_lowered(text, lowered, start, end)
    return _hit(row, start, end)


def _pack(hits: list[dict], k: int, position_order: bool = True) -> dict:
    if position_order:
        hits.sort(key=lambda h: (h["session"], h["turn"],
                                 0 if h["who"] != "agent" else 1))
    return {"hits": hits[:k], "total": len(hits), "chats": len({h["session"] for h in hits})}


def count_tokens(q: str) -> list[str]:
    """The tokens ``keyword_count`` counts, in its own normalized form."""
    toks = [token for token in re.split(r"[\s\-_]+", q.strip()) if token]
    if not toks:
        return []
    return [q.strip().lower()] if len(toks) == 1 else [t.lower() for t in toks]


def count_rides_the_index(q: str) -> bool:
    """Whether counting this query stays inside the trigram index. A token
    under three characters has no posting list, so its count is a full-table
    LIKE scan - measured in seconds, not the milliseconds an index costs."""
    lows = count_tokens(q)
    return bool(lows) and all(len(token) >= 3 for token in lows)


def keyword_count(db: sqlite3.Connection, q: str,
                  flt: dict | None = None, *,
                  cap: int | None = None) -> dict[str, int]:
    """Count the phrase/all-terms union without constructing result rows.

    ``cap`` stops the count once that many rows are confirmed: the answer then
    carries ``exact`` False and its numbers are floors of the result set (never
    of the scan). An uncapped count is always exact."""
    lows = count_tokens(q)
    if not lows:
        return {"total": 0, "chats": 0, "tool_hits": 0, "exact": True}
    toks = [token for token in re.split(r"[\s\-_]+", q.strip()) if token]
    _register_functions(db)
    where, params = _candidate_where(lows, flt)
    ceiling = None if cap is None else max(1, int(cap))
    direct = all(
        token.isascii() and token.isalnum() and len(token) >= 3
        and not _needs_re_i_rare(token)
        for token in lows
    )
    if direct:
        rows = "SELECT session, who FROM msgs" + where
        limit: list = []
        if ceiling is not None:
            rows += " LIMIT ?"
            limit = [ceiling + 1]
        total, chats, tools = db.execute(
            "SELECT count(*), count(DISTINCT session), "
            "coalesce(sum(CASE WHEN who = 'tool' THEN 1 ELSE 0 END), 0) "
            f"FROM ({rows})", [*params, *limit]).fetchone()
        return {"total": int(total), "chats": int(chats), "tool_hits": int(tools),
                "exact": ceiling is None or int(total) <= ceiling}

    select = "SELECT session, who, text FROM msgs" + where
    total = tools = 0
    chats: set[str] = set()
    needle = q.strip() if len(toks) == 1 else None
    for session, who, text in db.execute(select, params):
        matched = (common.insensitive_span(text, needle) is not None
                   if needle is not None else
                   all(common.insensitive_span(text, token) is not None for token in toks))
        if not matched:
            continue
        total += 1
        chats.add(session)
        tools += (who or "") == "tool"
        if ceiling is not None and total > ceiling:
            return {"total": total, "chats": len(chats), "tool_hits": tools,
                    "exact": False}
    return {"total": total, "chats": len(chats), "tool_hits": tools, "exact": True}


def keyword(db: sqlite3.Connection, q: str, k: int, flt: dict | None = None, *,
            position_order: bool = True) -> dict:
    """Separator-flexible keyword search, same semantics as explore.keyword_search:
    FTS candidates (superset: tokens anywhere in the row), then the exact matcher
    confirms adjacency and places the snippet."""
    toks = [t for t in re.split(r"[\s\-_]+", q.strip()) if t]
    common.dbg(f"keyword: q={q!r} toks={toks} "
               f"(fts>=3ch={[t for t in toks if len(t) >= 3]} like<3ch={[t for t in toks if len(t) < 3]})")
    if not toks:
        return {"hits": [], "total": 0, "chats": 0}
    hits = []
    cand = 0
    if len(toks) == 1:  # no separators in q, so the token IS the query
        token = q.strip()
        for row in _candidates(db, [token.lower()], flt):
            cand += 1
            span = common.insensitive_span(row[_TEXT], token)
            if span is not None:
                hits.append(_hit(row, *span))
    else:
        # token gap = any non-alphanumeric run: [\W_] keeps underscore,
        # so "cyber filter" finds "cyber_filter".
        pat = re.compile(r"[\W_]*".join(re.escape(t) for t in toks), re.I)
        for row in _candidates(db, toks, flt):
            cand += 1
            m = pat.search(row[_TEXT])
            if m:
                hits.append(_hit(row, m.start(), m.end()))
    # cand >> hits means FTS surfaced rows the adjacency matcher then rejected - the exact
    # signature of a separator/punctuation mismatch (the bug AGREP_DEBUG is here to expose).
    common.dbg(f"keyword: {cand} FTS candidate(s) -> {len(hits)} confirmed hit(s)"
               + (f"  [{cand - len(hits)} rejected by adjacency matcher]" if cand > len(hits) else ""))
    return _pack(hits, k, position_order)


def content(db: sqlite3.Connection, q: str, k: int, flt: dict | None = None) -> dict:
    """Scored-OR over content terms - the last-resort tier for natural-language
    queries where even bag-of-words AND finds nothing (evidence rarely holds every
    word of a question in one message). Candidates come from the rarest few terms
    (the answer almost certainly contains at least one), each candidate is scored
    by idf-weighted term coverage, and the score rides out on h['coverage'] so the
    ranking layer can use it instead of re-deriving match strength from a snippet."""
    _register_functions(db)
    toks = list(dict.fromkeys(t.lower() for t in re.split(r"[\s\-_]+", q.strip()) if t))
    if not toks:
        return {"hits": [], "total": 0, "chats": 0}
    fts_table = _fts_table(flt)
    prose_only = fts_table == "msgs_prose_fts"
    n_docs = db.execute("SELECT count(*) FROM msgs" +
                        (" WHERE who <> 'tool'" if prose_only else "")).fetchone()[0] or 1
    idf: dict[str, float] = {}
    for t in toks:
        if len(t) >= 3:
            df = db.execute(
                f"SELECT count(*) FROM {fts_table} WHERE {fts_table} MATCH ?",
                (_fts_quote(t),)).fetchone()[0]
        else:  # too short for the trigram index; treat as common
            df = n_docs
        idf[t] = math.log(1.0 + n_docs / (df + 1))
    anchors = [t for t in sorted(toks, key=lambda t: -idf[t])
               if len(t) >= 3 and idf[t] > 0][:3]
    if not anchors:
        return {"hits": [], "total": 0, "chats": 0}
    sel = ("SELECT session, agent, project, concept, model, model_source, "
           "turn, ts, who, text, content_digest FROM msgs "
           f"WHERE id IN (SELECT rowid FROM {fts_table} WHERE {fts_table} MATCH ?)")
    fw, fp = _filter_sql(flt)
    total_idf = sum(idf.values()) or 1.0
    hits = []
    for row in db.execute(sel + ("".join(" AND " + w for w in fw)),
                          [" OR ".join(_fts_quote(t) for t in anchors), *fp]):
        low = row[_TEXT].lower()
        pos = {t: low.find(t) for t in toks}
        matched = [t for t, p in pos.items() if p >= 0]
        if not matched:
            continue  # trigram candidates are a superset
        # no coverage floor: every swept floor lost real LongMemEval recall.
        # ranking sorts weak hits last; _nl_query gates garbage queries.
        cov = sum(idf[t] for t in matched) / total_idf
        first = min(pos[t] for t in matched)
        tok = next(t for t in matched if pos[t] == first)
        h = _lower_hit(row, low, first, first + len(tok))
        h["coverage"] = round(cov, 4)
        hits.append(h)
    hits.sort(key=lambda h: (-h["coverage"], h["session"], h["turn"]))
    return {"hits": hits[:k], "total": len(hits), "chats": len({h["session"] for h in hits})}


def term_session_df(db: sqlite3.Connection,
                    terms: list[str]) -> dict[str, float]:
    """Per-term session document frequency (0..1): the corpus's own measure of
    which query terms discriminate. A ubiquitous term is narration to the
    coverage retry; this only measures - callers pick the threshold."""
    total = db.execute(
        "SELECT count(DISTINCT session) FROM msgs").fetchone()[0]
    if not total:
        return {}
    out = {}
    for t in terms:
        if len(t) < 3:
            continue
        n = db.execute(
            "SELECT count(DISTINCT m.session) FROM msgs_fts "
            "JOIN msgs m ON m.id = msgs_fts.rowid WHERE msgs_fts MATCH ?",
            (_fts_quote(t),)).fetchone()[0]
        out[t] = n / total
    return out


COVERAGE_MASS_MIN_DOCS = 1000


def coverage_rank(db: sqlite3.Connection, q: str, k: int,
                  flt: dict | None = None) -> list[dict]:
    """Over-specification recovery lane: OR the indexable query terms and rank
    with FTS5's bm25(), so the corpus's own document frequencies decide which
    terms are informative - ubiquitous narration weighs ~nothing next to rare
    evidence terms, and length normalization keeps giant blobs from outranking
    focused rows. Rows return engine-best-first, each carrying _terms_matched /
    _terms_missing (presence of every deduped query term in the row text) and
    _query_echo (text quotes the whole query in order: a reflection of the
    query, not independent evidence)."""
    _register_functions(db)
    raw = [t for t in re.split(r"[\s\-_]+", q.strip()) if t]
    toks = list(dict.fromkeys(t.lower() for t in raw))
    fts_toks = [t for t in toks if len(t) >= 3]
    if len(raw) < 2 or not fts_toks:
        return []
    echo_pat = re.compile(r"[\W_]*".join(re.escape(t) for t in raw), re.I)
    fts_table = _fts_table(flt)
    n_docs = db.execute("SELECT count(*) FROM msgs").fetchone()[0] or 1
    idf: dict[str, float] = {}
    for t in toks:
        if len(t) >= 3:
            df = db.execute(
                f"SELECT count(*) FROM {fts_table} WHERE {fts_table} MATCH ?",
                (_fts_quote(t),)).fetchone()[0]
        else:
            df = n_docs
        idf[t] = math.log(1.0 + n_docs / (df + 1))
    total_idf = sum(idf.values()) or 1.0
    fw, fp = _filter_sql(flt)
    sel = ("SELECT m.session, m.agent, m.project, m.concept, m.model, "
           "m.model_source, m.turn, m.ts, m.who, m.text, m.content_digest "
           f"FROM {fts_table} JOIN msgs m ON m.id = {fts_table}.rowid "
           f"WHERE {fts_table} MATCH ?"
           + "".join(" AND " + w for w in fw)
           + f" ORDER BY bm25({fts_table}) LIMIT ?")
    hits = []
    for row in db.execute(
            sel, [" OR ".join(_fts_quote(t) for t in fts_toks), *fp, k]):
        low = row[_TEXT].lower()
        pos = {t: low.find(t) for t in toks}
        matched = [t for t in toks if pos[t] >= 0]
        if not matched:
            continue  # trigram candidates are a superset
        first = min(pos[t] for t in matched)
        lead = next(t for t in matched if pos[t] == first)
        h = _lower_hit(row, low, first, first + len(lead))
        h["_terms_matched"] = matched
        h["_terms_missing"] = [t for t in toks if pos[t] < 0]
        h["_query_echo"] = echo_pat.search(row[_TEXT]) is not None
        # idf-mass share of the query this row covers; None below statistical
        # scale, where every unusual word is df=0 and the share means nothing
        h["_coverage_mass"] = (
            round(sum(idf[t] for t in matched) / total_idf, 4)
            if n_docs >= COVERAGE_MASS_MIN_DOCS else None)
        hits.append(h)
    common.dbg(f"coverage_rank: {len(hits)} bm25-ordered row(s) "
               f"over {len(fts_toks)} OR term(s)")
    return hits


def terms(db: sqlite3.Connection, q: str, k: int, flt: dict | None = None, *,
          position_order: bool = True) -> dict:
    """Bag-of-words AND: rows where EVERY token appears somewhere, ANY order - the
    fallback for a multi-word query whose words never sit adjacent (keyword's in-order
    matcher then finds nothing). Uses the exact candidate set keyword does; the snippet
    sits at the earliest matched token."""
    return keyword_terms(db, q, k, flt, position_order=position_order)["terms"]


def keyword_terms(db: sqlite3.Connection, q: str, k: int, flt: dict | None = None, *,
                  position_order: bool = True) -> dict:
    """keyword + terms in ONE candidate walk: `{"phrase": ..., "terms": ...}`, each
    packed like the standalone engines. Every multi-word search now wants both lanes
    (the all-terms superset must always be current - gating it on thin phrase results
    let a searched-for phrase's own transcript echoes shadow scattered hits), and both
    lanes confirm against the identical candidate set, so pay the walk once.
    Single-token queries have no scatter to find; the terms lane comes back empty."""
    toks = [t for t in re.split(r"[\s\-_]+", q.strip()) if t]
    if len(toks) < 2:
        return {"phrase": keyword(db, q, k, flt, position_order=position_order),
                "terms": {"hits": [], "total": 0, "chats": 0}}
    lows = [t.lower() for t in toks]
    pat = re.compile(r"[\W_]*".join(re.escape(t) for t in toks), re.I)
    phrase_hits: list[dict] = []
    term_hits: list[dict] = []
    # Hot at corpus scale: the precomputed lowered tokens feed the span fast
    # path directly, and a phrase twin of an all-terms row copies its dict
    # (same fields, same digest) instead of re-deriving both from the raw row.
    search = pat.search
    span_map = common.original_span_for_lowered
    fallback_span = common.insensitive_span
    token_pairs = list(dict.fromkeys(zip(toks, lows)))
    for row_key, row in enumerate(_candidates(db, toks, flt)):
        text = row[_TEXT]
        low = text.lower()
        spans = []
        complete = True
        for token, token_low in token_pairs:
            start = low.find(token_low)
            span = (span_map(text, low, start, start + len(token_low))
                    if start >= 0 else fallback_span(text, token, low))
            if span is None:
                complete = False
                break
            spans.append(span)
        if not complete:
            m = search(text)
            if m:
                phrase_hit = _hit(row, m.start(), m.end())
                phrase_hit["_agrep_row_key"] = row_key
                phrase_hits.append(phrase_hit)
            continue
        term_hit = _spans_hit(row, spans)
        term_hit["_agrep_row_key"] = row_key
        term_hits.append(term_hit)
        m = search(text)
        if m:
            phrase_hit = dict(term_hit)
            phrase_hit["snippet"] = _snip_at(text, m.start(), m.end())
            phrase_hit["_match_span"] = m.span()
            if m.start() <= 80 and m.end() + 80 >= len(text):
                phrase_hit["_snippet_complete"] = True
            phrase_hits.append(phrase_hit)
    common.dbg(f"keyword_terms: {len(term_hits)} all-terms row(s), "
               f"{len(phrase_hits)} adjacent-phrase row(s)")
    return {"phrase": _pack(phrase_hits, k, position_order),
            "terms": _pack(term_hits, k, position_order)}


def word(db: sqlite3.Connection, q: str, k: int, flt: dict | None = None, *,
         position_order: bool = True) -> dict:
    """Whole-word search: FTS prefilter, boundary check only on candidates."""
    pat = common.literal_word_pattern(q)
    hits = []
    for row in _candidates(db, [q], flt):
        match = pat.search(row[_TEXT])
        if match is not None:
            hits.append(_hit(row, *match.span()))
    return _pack(hits, k, position_order)


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


def regex(db: sqlite3.Connection, pattern: str, k: int, flt: dict | None = None, *,
          position_order: bool = True) -> dict:
    """Regex can't use the index directly, but when the pattern demands a literal
    (most real ones do) the trigram FTS narrows candidates first; otherwise stream
    the table, which still skips the JSONL parse paid by the fallback."""
    _register_functions(db)
    rx = re.compile(pattern, re.I)
    hits = []
    lit = _required_literal(pattern)
    if lit:
        cur = _candidates(db, [lit.lower()], flt)
    else:
        fw, fp = _filter_sql(flt)
        cur = db.execute(
            "SELECT session, agent, project, concept, model, model_source, "
            "turn, ts, who, text, content_digest FROM msgs"
            + (" WHERE " + " AND ".join(fw) if fw else ""), fp)
    for row in cur:
        m = rx.search(row[_TEXT])
        if m:
            hits.append(_hit(row, m.start(), m.end()))
    return _pack(hits, k, position_order)


def session_rows(db: sqlite3.Connection, session: str, *, lo: int | None = None,
                 hi: int | None = None, include_tools: bool = False) -> list[dict]:
    """Transcript rows for one session, optionally bounded by turn number.

    Tool calls are search documents, not conversation messages: their canonical
    representation in a transcript is the per-session event stream. Excluding them
    by default prevents callers from accidentally treating a tool row as the prompt
    for its turn and lets SQLite use the small transcript-only partial index. Search
    and diagnostics can opt into the raw mixed stream with ``include_tools=True``.
    """
    where = ["session = ?"]
    params: list = [session]
    if not include_tools:
        where.append("who <> 'tool'")
    if lo is not None:
        where.append("turn >= ?")
        params.append(lo)
    if hi is not None:
        where.append("turn <= ?")
        params.append(hi)
    cur = db.execute(
        "SELECT session, agent, project, concept, model, model_source, "
        "turn, ts, who, text FROM msgs WHERE " + " AND ".join(where) +
        " ORDER BY turn, CASE WHEN who = 'agent' THEN 1 "
        "WHEN who = 'tool' THEN 2 ELSE 0 END, ts, id", params)
    return [dict(zip((*_FIELDS, "text"), row)) for row in cur]


def session_text_sizes(
        db: sqlite3.Connection, sessions: list[str], *,
        before_turns: Mapping[str, int] | None = None,
) -> dict[str, int]:
    """Indexed prose characters per requested session without hydrating text."""
    unique = list(dict.fromkeys(session for session in sessions if session))
    if not unique:
        return {}
    marks = ",".join("?" for _ in unique)
    where = ["who <> 'tool'", f"session IN ({marks})"]
    params: list = list(unique)
    for session in unique:
        boundary = before_turns.get(session) if before_turns else None
        if boundary is not None:
            where.append("(session <> ? OR turn < ?)")
            params.extend((session, int(boundary)))
    rows = db.execute(
        "SELECT session, coalesce(sum(length(text)), 0) FROM msgs "
        "WHERE " + " AND ".join(where) + " GROUP BY session", params)
    return {str(session): int(chars or 0) for session, chars in rows}


SESSION_TERM_QUERY_LIMIT = 32


def session_term_turns(
        db: sqlite3.Connection, sessions: list[str],
        terms: list[str], max_results: int, *,
        before_turns: Mapping[str, int] | None = None,
) -> list[dict]:
    """Rank bounded turn metadata by distinct matched terms, without returning text."""
    unique_sessions = list(dict.fromkeys(session for session in sessions if session))
    unique_terms = list(dict.fromkeys(
        term for term in terms if term))[:SESSION_TERM_QUERY_LIMIT]
    limit = max(0, int(max_results))
    if not unique_sessions or not unique_terms or not limit:
        return []

    class TermCoverage:
        def __init__(self):
            self.mask = 0

        def step(self, text):
            low = (text or "").lower()
            for index, term in enumerate(unique_terms):
                bit = 1 << index
                if not self.mask & bit and term in low:
                    self.mask |= bit

        def finalize(self):
            return self.mask.bit_count()

    db.create_aggregate("agrep_recall_term_coverage", 1, TermCoverage)
    marks = ",".join("?" for _ in unique_sessions)
    where = ["who <> 'tool'", f"session IN ({marks})"]
    params: list = list(unique_sessions)
    for session in unique_sessions:
        boundary = before_turns.get(session) if before_turns else None
        if boundary is not None:
            where.append("(session <> ? OR turn < ?)")
            params.extend((session, int(boundary)))
    sql = (
        "SELECT session, turn, max(coalesce(ts, 0)), "
        "agrep_recall_term_coverage(text) AS terms FROM msgs "
        "WHERE " + " AND ".join(where) + " GROUP BY session, turn "
        "HAVING terms > 0 "
        "ORDER BY terms DESC, max(coalesce(ts, 0)) DESC LIMIT ?")
    params.append(limit)
    return [{"session": str(session), "turn": int(turn), "ts": int(ts or 0),
             "term_hits": int(term_hits or 0)}
            for session, turn, ts, term_hits in db.execute(sql, params)]


def session_context(db: sqlite3.Connection, session: str) -> dict | None:
    """Small structural metadata needed to build any window in ``session``.

    The timeline contains one ``(turn, ts)`` pair per prompt/control/recap row and
    deliberately carries no message or tool text. Keeping it separate means a
    bounded window can clamp sparse turn numbers and attribute timestamped events
    correctly without loading the full transcript. ``explore`` caches this compact
    result across expansion windows.
    """
    rows = list(db.execute(
        "SELECT turn, ts, agent, project FROM msgs "
        "WHERE session = ? AND who <> 'tool' AND who <> 'agent' "
        "ORDER BY turn, id", (session,)))
    if not rows:
        # Defensive support for an old/partial corpus containing reply-only turns.
        rows = list(db.execute(
            "SELECT turn, ts, agent, project FROM msgs "
            "WHERE session = ? AND who = 'agent' ORDER BY turn, id", (session,)))
    if not rows:
        return None

    timeline = [{"turn": int(r[0]), "ts": int(r[1] or 0)} for r in rows]
    turns = [r["turn"] for r in timeline]
    agent = next((r[2] for r in rows if r[2]), "")
    project = next((r[3] for r in rows if r[3]), "")
    return {
        "agent": agent,
        "project": project,
        "n_turns": len(set(turns)),
        "first_turn": min(turns),
        "last_turn": max(turns),
        "timeline": timeline,
    }
