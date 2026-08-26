"""Internal semantic retrieval for agrep's resident-on-demand worker.

The supported entry point is ``agrep ... -s``. It routes through semantic freshness,
single-owner request serialization, and the adaptive idle lease before importing this
module. Running this file directly would bypass those contracts.
"""

from __future__ import annotations

import hashlib
import json
import mmap
import os
import sqlite3
import sys
import time
import uuid

import numpy as np

import common
import compact
import embedder
import indexd_runtime
import segment_query
import surface_policy as surface

_CACHE: dict = {}

# Callers may ask for a large result page, but candidate-pool work stays bounded.
SEMANTIC_MAX_RESULTS = 200
SEMANTIC_MAX_POOL = 256
SEMANTIC_MAX_SESSION_CANDIDATES = 4096
SEMANTIC_MAX_SESSION_PAGES = 4
DENSE_FALLBACK_MAX_ROWS = 250_000
DENSE_SCORE_KIND = "cosine"


def _data_dir_readonly() -> bool:
    return common.data_dir_readonly(common.DATA_DIR)


def _mutation_refusal_reason() -> str | None:
    if _data_dir_readonly():
        return "AGREP_DATA_READONLY protects semantic candidate refs"
    ownership = indexd_runtime.derived_writer_mutation_info()
    return None if ownership.writable else ownership.reason


def _mutation_refused() -> bool:
    return _mutation_refusal_reason() is not None


def _require_mutation_allowed() -> None:
    reason = _mutation_refusal_reason()
    if reason is not None:
        raise MessageRefsUnavailable(reason)


def _artifact_stamp(paths: tuple) -> tuple:
    stamp = []
    for path in paths:
        try:
            state = path.stat()
        except FileNotFoundError:
            stamp.append(None)
        else:
            stamp.append((
                state.st_mtime_ns, state.st_size, state.st_dev, state.st_ino))
    return tuple(stamp)


def _cached(key: str, paths: tuple, build):
    """Cache one artifact generation, retrying when publication moves during load."""
    for attempt in range(3):
        before = _artifact_stamp(paths)
        hit = _CACHE.get(key)
        if hit and hit[0] == before:
            return hit[1]
        try:
            value = build()
        except Exception:
            try:
                moved = before != _artifact_stamp(paths)
            except OSError:
                moved = False
            if attempt < 2 and moved:
                continue
            raise
        try:
            after = _artifact_stamp(paths)
        except OSError:
            _release_cached_value(value)
            raise
        if before != after:
            _release_cached_value(value)
            if attempt < 2:
                continue
            break
        if hit and hit[1] is not value:
            _release_cached_value(hit[1])
        _CACHE[key] = (after, value)
        return value
    raise RuntimeError("artifact publication kept moving while loading")


def _release_cached_value(value, seen: set[int] | None = None) -> None:
    """Close mmap-backed matrices when an artifact cache generation changes."""
    seen = seen if seen is not None else set()
    if id(value) in seen:
        return
    seen.add(id(value))
    if isinstance(value, np.memmap):
        common.close_embedding_matrix(value)
    elif isinstance(value, (tuple, list)):
        for item in value:
            _release_cached_value(item, seen)
    elif isinstance(value, dict):
        for item in value.values():
            _release_cached_value(item, seen)


def clear_artifact_cache() -> None:
    """Release vector mappings and parsed artifacts owned by this process."""
    _close_message_refs()
    old = list(_CACHE.values())
    _CACHE.clear()
    seen: set[int] = set()
    for _, value in old:
        _release_cached_value(value, seen)
    q8 = sys.modules.get("semantic_q8")
    if q8 is not None:
        q8.close_scanner()
    segmented = sys.modules.get("segment_query")
    if segmented is not None:
        segmented.close_cache()


def _lines(text: str) -> list[str]:
    """Split JSONL content on real newlines ONLY, dropping blank lines. `str.
    splitlines()` also breaks on U+2028/U+2029/U+0085 etc., which occur inside
    chat text and split a row mid-JSON-string -> JSONDecodeError. The corpus has
    real U+2028s, so any jsonl parse here must use this, not .splitlines()."""
    return [ln for ln in text.split("\n") if ln.strip()]


def _result_content_digest(row: dict) -> str:
    if "content_digest" not in row:
        return compact.content_digest(row.get("text") or "")
    return compact.require_content_digest(row["content_digest"])


_MESSAGE_REFS_SCHEMA = "6"
_MESSAGE_REFS = {
    "identity": None, "path": None, "db": None, "hashes": None,
    "session_index": None, "ids_obj": None, "identity_record": None,
}
_CURRENT_MESSAGE_STATE = {
    "bundle": None, "artifacts": None, "source": None, "generation": None,
}
_MESSAGE_REFS_GUARDS = {
    "refs_dirty_insert", "refs_dirty_update", "refs_dirty_delete",
    "texts_dirty_insert", "texts_dirty_update", "texts_dirty_delete",
}
_MESSAGE_REFS_POINTER_VERSION = 1


def _message_refs_pointer_path():
    return common.DATA_DIR / "embeddings.refs.meta"


class _MappedHashBlob:
    def __init__(self, path, rows: int):
        self.rows = int(rows)
        self.snapshot = None
        self.snapshot_external = False
        target = path
        if common.WIN:
            if _mutation_refused():
                import embedding_store
                self.snapshot = embedding_store._copy_to_system_temp(
                    path, "embedding-hashes")
                self.snapshot_external = True
            else:
                import shutil
                common._prune_embedding_snapshots(path)
                self.snapshot = common._embedding_snapshot_name(path)
                try:
                    os.link(path, self.snapshot)
                except OSError:
                    shutil.copyfile(path, self.snapshot)
            target = self.snapshot
        self.file = target.open("rb")
        try:
            self.mapping = mmap.mmap(self.file.fileno(), 0, access=mmap.ACCESS_READ)
        except Exception:
            self.file.close()
            if self.snapshot is not None:
                if self.snapshot_external or not _mutation_refused():
                    self.snapshot.unlink(missing_ok=True)
            raise

    def at(self, ordinal: int) -> str:
        start = int(ordinal) * 17
        raw = self.mapping[start:start + 17]
        if len(raw) != 17 or raw[16:] != b"\n":
            raise CorruptMessageRefs(
                f"embedding hash layout is invalid at ordinal {ordinal}")
        try:
            value = raw[:16].decode("ascii")
        except UnicodeDecodeError as exc:
            raise CorruptMessageRefs(
                f"embedding hash is invalid at ordinal {ordinal}") from exc
        if len(value) != 16 or any(ch not in "0123456789abcdef" for ch in value):
            raise CorruptMessageRefs(
                f"embedding hash is invalid at ordinal {ordinal}")
        return value

    def close(self) -> None:
        self.mapping.close()
        self.file.close()
        if self.snapshot is not None:
            if self.snapshot_external or not _mutation_refused():
                try:
                    self.snapshot.unlink(missing_ok=True)
                except OSError:
                    pass
            self.snapshot = None
            self.snapshot_external = False


def _semantic_timing_enabled() -> bool:
    value = os.environ.get("AGREP_SEM_TIMING", "")
    return common.DEBUG or value.lower() not in ("", "0", "false", "no", "off")


def _semantic_cache_state() -> dict[str, bool]:
    return {
        "model": models_loaded(),
        "artifacts": "msg_emb" in _CACHE,
        "refs": _MESSAGE_REFS.get("db") is not None,
        "session_index": _MESSAGE_REFS.get("session_index") is not None,
        "summaries": "summary_semantic" in _CACHE,
    }


class _SemanticTimer:
    def __init__(self, enabled: bool | None = None) -> None:
        self.enabled = _semantic_timing_enabled() if enabled is None else enabled
        self.started = self.last = time.perf_counter()
        self.phases: dict[str, float] = {}
        self.cache_before = _semantic_cache_state() if self.enabled else {}

    def mark(self, name: str) -> None:
        if not self.enabled:
            return
        now = time.perf_counter()
        self.phases[name] = round((now - self.last) * 1000.0, 3)
        self.last = now

    def dumps(self, payload: dict) -> str:
        if not self.enabled:
            return json.dumps(payload)
        timing = {
            "phases_ms": self.phases,
            "cache_before": self.cache_before,
            "cache_after": _semantic_cache_state(),
        }
        shadow = payload.get("_semantic_q8_shadow")
        if isinstance(shadow, dict):
            timing["q8_shadow"] = shadow
        payload["_semantic_timing"] = timing
        timing["hybrid_compute_ms"] = round(
            (time.perf_counter() - self.started) * 1000.0, 3)
        return json.dumps(payload)


def _stat_identity(stat) -> list[int]:
    return [
        int(stat.st_size), int(stat.st_mtime_ns),
        int(stat.st_dev), int(stat.st_ino),
    ]


def _path_identity(path) -> "list[int] | None":
    try:
        return _stat_identity(path.stat())
    except OSError:
        return None


def _message_source_identity() -> tuple:
    return (
        _path_identity(common.MESSAGES_PATH),
        _path_identity(common.DATA_DIR / "replies.jsonl"),
    )


def _ids_sha256(ids: list[str]) -> str:
    h = hashlib.sha256()
    for mid in ids:
        h.update(mid.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def _message_refs_identity(ids: list[str], matrix=None) -> str:
    """Identity of the immutable semantic snapshot a refs DB must describe.

    New embedding indexes carry an atomic commit marker. Legacy indexes fall back
    to all three artifact identities plus an ids digest, so a refs row can never be
    silently reused at the same ordinal after the vector pair moves. Transcript
    and text-hash identities bind the candidate metadata/text to those vectors.
    """
    if len(set(ids)) != len(ids):
        raise RuntimeError("embedding ids are not unique; semantic ordinals are ambiguous")
    meta_path = common.EMBEDDINGS_PATH.parent / "embeddings.meta"
    ids_hash = _ids_sha256(ids)
    try:
        current_bundle = common.embedding_artifact_identity(
            meta_path, common.EMBEDDINGS_PATH, common.IDS_PATH)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        raise _EmbeddingBundleMoved(
            f"embedding publication moved while resolving semantic refs: {exc}") from exc
    loaded_bundle = common.embedding_matrix_identity(matrix) if matrix is not None else None
    if matrix is not None and not loaded_bundle:
        raise _EmbeddingBundleMoved("loaded embedding matrix has no generation identity")
    if loaded_bundle is not None and loaded_bundle != current_bundle:
        raise _EmbeddingBundleMoved(
            "embedding publication changed after the semantic matrix was mapped")
    embedding = {"bundle": loaded_bundle or current_bundle, "ids_sha256": ids_hash}
    hashes_path = common.EMBEDDINGS_PATH.with_suffix(".hashes")
    try:
        source = common.transcript_generation()
    except (OSError, RuntimeError, ValueError) as exc:
        raise _EmbeddingBundleMoved(
            "semantic transcript moved while resolving refs") from exc
    if source is None:
        raise _EmbeddingBundleMoved(
            "semantic transcript disappeared while resolving refs")
    messages_identity, replies_identity = _message_source_identity()
    return json.dumps({
        "schema": _MESSAGE_REFS_SCHEMA,
        "embedding": embedding,
        "source": source,
        "messages": messages_identity,
        "replies": replies_identity,
        "hashes": _path_identity(hashes_path),
    }, sort_keys=True, separators=(",", ":"))


_embedding_text_hash = common.semantic_text_hash


def _read_embedding_hashes(n_expected: int) -> "list[str] | None":
    path = common.EMBEDDINGS_PATH.with_suffix(".hashes")
    if not path.exists():
        return None
    hashes = [line for line in path.read_text(encoding="utf-8").split("\n") if line]
    if len(hashes) != n_expected:
        raise RuntimeError(
            f"embedding hash/id mismatch: {len(hashes)} hashes vs {n_expected} ids")
    return hashes


def _read_embedding_hash_blob(n_expected: int) -> bytes:
    """Read the committed per-ordinal hashes in their compact fixed-width form.

    The embedding writer emits 16-byte BLAKE2 hex digests plus ``\n``. Keeping
    that representation avoids a Python string object per row (material at 10x
    corpora) while still letting candidate resolution prove returned text against
    the vector generation outside the refs SQLite file.
    """
    path = common.EMBEDDINGS_PATH.with_suffix(".hashes")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RuntimeError("embedding text hashes are missing") from exc
    expected_size = int(n_expected) * 17
    if (len(raw) == expected_size
            and raw[16::17] == b"\n" * int(n_expected)):
        return raw

    # Pre-v5 Windows writers published CRLF; normalize only that one legacy
    # fixed-width layout, refusing others so ordinal slicing can never drift.
    legacy_size = int(n_expected) * 18
    if (len(raw) == legacy_size
            and raw[16::18] == b"\r" * int(n_expected)
            and raw[17::18] == b"\n" * int(n_expected)):
        return b"".join(
            raw[offset:offset + 16] + b"\n"
            for offset in range(0, legacy_size, 18))
    raise RuntimeError(
        f"embedding hash layout mismatch: {len(raw)} bytes for "
        f"{n_expected} rows")


def _open_embedding_hash_blob(n_expected: int):
    path = common.EMBEDDINGS_PATH.with_suffix(".hashes")
    try:
        if path.stat().st_size == int(n_expected) * 17:
            return _MappedHashBlob(path, n_expected)
    except OSError as exc:
        raise RuntimeError("embedding text hashes are missing") from exc
    return _read_embedding_hash_blob(n_expected)


def _refs_positions(ids: list[str]) -> dict[str, list[int]]:
    positions: dict[str, list[int]] = {}
    for ordinal, mid in enumerate(ids):
        positions.setdefault(mid, []).append(ordinal)
    return positions


def _ref_row_seal(ordinal: int, mid: str, agent: str, session: str,
                  project: str, model: str, who: str, side: int,
                  turn: int, ts: int, text_hash: str, source_kind: int,
                  byte_offset: int, byte_length: int) -> str:
    """Checksum one surfaced refs row, including metadata and its source locator."""
    payload = json.dumps(
        [int(ordinal), mid, agent, session, project, model, who, int(side),
         int(turn), int(ts), text_hash, int(source_kind), int(byte_offset),
         int(byte_length)],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8", "surrogatepass")
    return hashlib.blake2b(payload, digest_size=16).hexdigest()

def _semantic_side_sessions() -> frozenset[str]:
    parents = common.await_family_publication(common.strict_family_parent_map)
    if parents is not None:
        return frozenset(parents)
    if (not (common.DATA_DIR / "sessions.jsonl").exists()
            and not (common.DATA_DIR
                     / common.SESSION_FAMILY_META_FILE).exists()):
        return frozenset()
    raise RuntimeError("session-family publication is unavailable")


def _create_message_refs_db(path, ids: list[str], identity: str,
                            coverage: dict | None = None) -> None:
    """Stream the transcript into an ordinal-aligned SQLite snapshot.

    Filter metadata stays compact in SQLite; transcript text is not duplicated.
    Instead each row stores a sealed byte range into the immutable messages/replies
    generation, and the worker reads only the bounded dense winners.
    """
    _require_mutation_allowed()
    positions = _refs_positions(ids)
    coverage = coverage or {
        "indexed": len(ids), "total": len(ids), "pending": 0,
        "complete": True,
    }
    hashes = _read_embedding_hashes(len(ids))
    if hashes is None:
        raise RuntimeError(
            "embedding text hashes are missing; refresh semantic embeddings")
    source_seen: set[str] = set()
    side_sessions = _semantic_side_sessions()
    db = sqlite3.connect(path)
    try:
        db.executescript("""
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            PRAGMA temp_store=MEMORY;
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE refs(
                ord INTEGER PRIMARY KEY,
                mid TEXT NOT NULL,
                agent TEXT, session TEXT, project TEXT, model TEXT,
                who TEXT, side INTEGER NOT NULL CHECK(side IN (0, 1)),
                turn, ts INTEGER, valid INTEGER NOT NULL,
                row_seal TEXT NOT NULL);
            CREATE TABLE texts(
                ord INTEGER PRIMARY KEY,
                source_kind INTEGER NOT NULL,
                byte_offset INTEGER NOT NULL,
                byte_length INTEGER NOT NULL,
                text_hash TEXT NOT NULL);
            CREATE INDEX refs_session ON refs(session, ord);
        """)
        db.execute("INSERT INTO meta(key, value) VALUES('identity', ?)", (identity,))
        db.execute("INSERT INTO meta(key, value) VALUES('rows', ?)", (str(len(ids)),))

        def upsert_ref(ordinal: int, mid: str, row: dict, *, who: str,
                       valid: int, text_hash: str | None = None,
                       source_kind: int = -1, byte_offset: int = -1,
                       byte_length: int = -1) -> None:
            agent = str(row.get("agent") or "")
            session = str(row.get("session") or "")
            project = str(row.get("project") or "")
            model = str(row.get("model") or "")
            who = str(who or "user")
            side = int(session in side_sessions)
            turn = int(row.get("turn") or 0)
            ts = int(row.get("ts") or 0)
            seal = (_ref_row_seal(
                ordinal, mid, agent, session, project, model, who, side,
                turn, ts, text_hash, source_kind, byte_offset, byte_length)
                    if valid and text_hash else "")
            db.execute(
                "INSERT OR REPLACE INTO refs"
                "(ord,mid,agent,session,project,model,who,side,turn,ts,"
                "valid,row_seal) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (ordinal, mid, agent, session, project, model, who, side,
                 turn, ts, valid, seal))

        with common.MESSAGES_PATH.open("rb") as src:
            byte_offset = 0
            for raw in src:
                row_offset = byte_offset
                byte_offset += len(raw)
                if not raw.strip():
                    continue
                try:
                    row = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                mid, text = row.get("id"), row.get("text")
                if not mid or text is None:
                    continue
                source_seen.add(mid)
                for ordinal in positions.get(mid, ()):
                    text_hash = _embedding_text_hash(text)
                    upsert_ref(ordinal, mid, row, who=row.get("who", "user"),
                               valid=1, text_hash=text_hash, source_kind=0,
                               byte_offset=row_offset, byte_length=len(raw))
                    db.execute(
                        "INSERT OR REPLACE INTO texts"
                        "(ord,source_kind,byte_offset,byte_length,text_hash) "
                        "VALUES(?,?,?,?,?)",
                        (ordinal, 0, row_offset, len(raw), text_hash))

                reply_id = mid + "#r"
                for ordinal in positions.get(reply_id, ()):
                    parts = mid.split(":")
                    inherited = {
                        "agent": row.get("agent") or (parts[0] if parts else ""),
                        "session": row.get("session") or (
                            parts[1] if len(parts) > 1 else ""),
                        "project": row.get("project", ""),
                        "model": row.get("model", ""),
                        "turn": row.get("turn", parts[2] if len(parts) > 2 else 0),
                        "ts": row.get("ts", 0),
                    }
                    upsert_ref(ordinal, reply_id, inherited, who="agent", valid=0)

        replies_path = common.DATA_DIR / "replies.jsonl"
        if replies_path.exists():
            with replies_path.open("rb") as src:
                byte_offset = 0
                for raw in src:
                    row_offset = byte_offset
                    byte_offset += len(raw)
                    if not raw.strip():
                        continue
                    try:
                        row = json.loads(raw)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    rid, reply = row.get("id"), row.get("reply")
                    if not rid or not reply:
                        continue
                    mid = rid + "#r"
                    source_seen.add(mid)
                    for ordinal in positions.get(mid, ()):
                        text_hash = _embedding_text_hash(reply)
                        found = db.execute(
                            "SELECT mid,agent,session,project,model,who,turn,ts,side "
                            "FROM refs WHERE ord=?", (ordinal,)).fetchone()
                        if found:
                            seal = _ref_row_seal(
                                ordinal, found[0], found[1] or "", found[2] or "",
                                found[3] or "", found[4] or "", found[5] or "agent",
                                int(found[8]), int(found[6] or 0), int(found[7] or 0),
                                text_hash, 1, row_offset, len(raw))
                            db.execute(
                                "UPDATE refs SET valid=1,row_seal=? WHERE ord=?",
                                (seal, ordinal))
                        else:
                            parts = rid.split(":")
                            fallback = {
                                "agent": parts[0] if parts else "",
                                "session": parts[1] if len(parts) > 1 else "",
                                "project": "", "model": "",
                                "turn": parts[2] if len(parts) > 2 else 0, "ts": 0,
                            }
                            upsert_ref(ordinal, mid, fallback, who="agent", valid=1,
                                       text_hash=text_hash, source_kind=1,
                                       byte_offset=row_offset, byte_length=len(raw))
                        db.execute(
                            "INSERT OR REPLACE INTO texts"
                            "(ord,source_kind,byte_offset,byte_length,text_hash) "
                            "VALUES(?,?,?,?,?)",
                            (ordinal, 1, row_offset, len(raw), text_hash))

        refs_count = int(db.execute(
            "SELECT count(*) FROM refs WHERE valid=1 AND row_seal<>''").fetchone()[0])
        texts = list(db.execute("SELECT ord,text_hash FROM texts ORDER BY ord"))
        expected_ids = set(ids)
        missing = expected_ids - source_seen
        unembedded = source_seen - expected_ids
        coverage_valid = (
            int(coverage.get("indexed", -1)) == len(ids)
            and int(coverage.get("total", -1)) == len(source_seen)
            and int(coverage.get("pending", -1)) == len(unembedded)
            and bool(coverage.get("complete")) == (not unembedded)
        )
        if missing or not coverage_valid:
            raise RuntimeError(
                "embedding coverage does not match the current transcript: "
                f"{len(missing)} missing / {len(unembedded)} unembedded source rows; "
                f"marker={coverage.get('indexed')}/{coverage.get('total')}")
        if refs_count != len(ids) or len(texts) != len(ids):
            raise RuntimeError(
                f"embedding transcript snapshot incomplete: {refs_count} metadata / "
                f"{len(texts)} texts / {len(ids)} vectors")
        if any(int(ordinal) != i for i, (ordinal, _) in enumerate(texts)):
            raise RuntimeError("embedding transcript snapshot has non-contiguous ordinals")
        mismatch = next((i for i, (_, got) in enumerate(texts)
                         if got != hashes[i]), None)
        if mismatch is not None:
            raise RuntimeError(
                f"transcript text does not match embedding hash at ordinal {mismatch}; "
                "refresh semantic embeddings")
        # Guard the verified immutable generation in O(1) at reader open. Any
        # ordinary SQLite mutation dirties the seal; selected-row proofs below also
        # defend against page damage that bypasses SQL triggers.
        db.executescript("""
            CREATE TRIGGER refs_dirty_insert AFTER INSERT ON refs BEGIN
                UPDATE meta SET value='dirty' WHERE key='sealed'; END;
            CREATE TRIGGER refs_dirty_update AFTER UPDATE ON refs BEGIN
                UPDATE meta SET value='dirty' WHERE key='sealed'; END;
            CREATE TRIGGER refs_dirty_delete AFTER DELETE ON refs BEGIN
                UPDATE meta SET value='dirty' WHERE key='sealed'; END;
            CREATE TRIGGER texts_dirty_insert AFTER INSERT ON texts BEGIN
                UPDATE meta SET value='dirty' WHERE key='sealed'; END;
            CREATE TRIGGER texts_dirty_update AFTER UPDATE ON texts BEGIN
                UPDATE meta SET value='dirty' WHERE key='sealed'; END;
            CREATE TRIGGER texts_dirty_delete AFTER DELETE ON texts BEGIN
                UPDATE meta SET value='dirty' WHERE key='sealed'; END;
        """)
        db.execute("INSERT INTO meta(key, value) VALUES('sealed', '1')")
        db.commit()
    finally:
        db.close()


def _open_message_refs(path, identity: str, ids=None, *, rows: int | None = None):
    n_rows = len(ids) if ids is not None else int(rows or 0)
    # Published refs generations are immutable. This avoids SQLite writer locks
    # and makes an old reader independent of pruning/next-generation publication.
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    db = sqlite3.connect(uri, uri=True)
    try:
        segment_query.register_metadata_functions(db)
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA mmap_size=67108864")
        meta = dict(db.execute(
            "SELECT key,value FROM meta WHERE key IN ('identity','rows','sealed')"))
        guards = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'")}
        edge_ok = True
        if n_rows:
            first = db.execute(
                "SELECT mid,valid FROM refs WHERE ord=0").fetchone()
            last = db.execute(
                "SELECT mid,valid FROM refs WHERE ord=?", (n_rows - 1,)).fetchone()
            first_text = db.execute(
                "SELECT 1 FROM texts WHERE ord=0").fetchone()
            last_text = db.execute(
                "SELECT 1 FROM texts WHERE ord=?", (n_rows - 1,)).fetchone()
            if ids is None:
                edge_ok = bool(
                    first and first[0] and first[1] == 1
                    and last and last[0] and last[1] == 1
                    and first_text is not None and last_text is not None)
            else:
                edge_ok = (
                    first == (ids[0], 1) and last == (ids[-1], 1)
                    and first_text is not None and last_text is not None)
        try:
            declared_rows = int(meta.get("rows", -1))
        except (TypeError, ValueError):
            declared_rows = -1
        if (meta.get("identity") != identity or meta.get("sealed") != "1"
                or declared_rows != n_rows
                or guards != _MESSAGE_REFS_GUARDS or not edge_ok):
            raise CorruptMessageRefs(
                "stale, modified, or incomplete semantic refs database")
        return db
    except Exception:
        db.close()
        raise


def _close_message_refs() -> None:
    db = _MESSAGE_REFS.get("db")
    if db is not None:
        try:
            db.close()
        except sqlite3.Error:
            pass
    hashes = _MESSAGE_REFS.get("hashes")
    if hasattr(hashes, "close"):
        try:
            hashes.close()
        except OSError:
            pass
    _MESSAGE_REFS.update(
        identity=None, path=None, db=None, hashes=None, session_index=None,
        ids_obj=None, identity_record=None)


class _EmbeddingBundleMoved(RuntimeError):
    pass


class CorruptMessageRefs(RuntimeError):
    """A selected semantic row failed its immutable snapshot proof."""


class MessageRefsUnavailable(RuntimeError):
    """The current vector generation has no prepared candidate sidecar yet."""


class _MessageRefStore:
    def __init__(self, db: sqlite3.Connection, ids, hashes,
                 identity: str, *, rows: int | None = None):
        self.db = db
        self.ids = ids
        self.hashes = hashes
        self.rows = len(ids) if ids is not None else int(rows or 0)
        try:
            record = json.loads(identity)
            self.source_generation = record["source"]
            self.source_identity = (record["messages"], record["replies"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CorruptMessageRefs(
                "semantic refs identity has no sealed source generation") from exc

    def _assert_source_current(self) -> None:
        try:
            generation = common.transcript_generation()
        except (OSError, RuntimeError, ValueError) as exc:
            raise _EmbeddingBundleMoved(
                "semantic transcript moved during candidate resolution") from exc
        if (generation != self.source_generation
                or _message_source_identity() != self.source_identity):
            raise _EmbeddingBundleMoved(
                "semantic transcript moved during candidate resolution")

    def _ordinal(self, raw) -> int:
        try:
            ordinal = int(raw)
        except (TypeError, ValueError) as exc:
            raise CorruptMessageRefs(
                f"semantic refs returned invalid ordinal {raw!r}") from exc
        if ordinal < 0 or ordinal >= self.rows:
            raise CorruptMessageRefs(
                f"semantic refs returned out-of-range ordinal {ordinal}")
        return ordinal

    @staticmethod
    def _where(filters: dict | None) -> tuple[str, list]:
        filters = dict(filters or {})
        filters.pop("_exclude_family_id", None)
        cached_sessions = filters.pop("_exclude_sessions", None)
        if (cached_sessions is not None
                and not filters.get("_exclude_sessions_json")):
            if isinstance(cached_sessions, (list, tuple, set, frozenset)):
                filters["_exclude_sessions_json"] = json.dumps(
                    sorted({str(value) for value in cached_sessions if value}),
                    separators=(",", ":"))
            else:
                filters["_exclude_sessions"] = cached_sessions
        caller = str(filters.get("exclude_session") or "")
        windowed = filters.get("exclude_session_from_turn") is not None
        exclude_family = filters.get("exclude_family", True)
        if (caller and not windowed and exclude_family
                and cached_sessions is None
                and not filters.get("_exclude_sessions_json")):
            indexed = common.indexed_calling_family(caller)
            if indexed is not None:
                filters["_exclude_sessions_json"] = json.dumps(
                    sorted(indexed[1]), separators=(",", ":"))
        where, params = segment_query.metadata_where(filters)
        return f"valid=1 AND {where}", params

    def eligible(self, filters: dict | None) -> np.ndarray:
        self._assert_source_current()
        where, params = self._where(filters)
        def ordinals():
            try:
                for row in self.db.execute(
                        f"SELECT ord FROM refs WHERE {where} ORDER BY ord", params):
                    yield self._ordinal(row[0])
            except sqlite3.DatabaseError as exc:
                raise CorruptMessageRefs(
                    "semantic refs failed while selecting candidates") from exc
        result = np.fromiter(ordinals(), dtype=np.int64)
        self._assert_source_current()
        return result

    def best_by_session(self, similarities: np.ndarray,
                        filters: dict | None) -> dict[str, int]:
        self._assert_source_current()
        if (similarities.ndim != 1 or len(similarities) != self.rows
                or not np.all(np.isfinite(similarities))):
            raise RuntimeError("semantic similarities are non-finite or misaligned")
        fast_filter = (not filters or set(filters) == {"_exclude_who"})
        if fast_filter:
            excluded = segment_query.metadata_excluded_roles(filters)
            indexes = _MESSAGE_REFS.get("session_index")
            if not isinstance(indexes, dict):
                indexes = {}
            cached = indexes.get(excluded)
            if not cached or cached[0] is not self.db:
                try:
                    ordinals, starts, sessions = [], [], []
                    previous = None
                    where, params = self._where(filters)
                    for raw_ordinal, raw_session in self.db.execute(
                            "SELECT ord,session FROM refs "
                            f"WHERE {where} AND session<>'' ORDER BY session,ord",
                            params):
                        ordinal = self._ordinal(raw_ordinal)
                        if not isinstance(raw_session, str):
                            raise TypeError("semantic refs session has the wrong type")
                        if raw_session != previous:
                            starts.append(len(ordinals))
                            sessions.append(raw_session)
                            previous = raw_session
                        ordinals.append(ordinal)
                except sqlite3.DatabaseError as exc:
                    raise CorruptMessageRefs(
                        "semantic refs failed while indexing sessions") from exc
                except (TypeError, ValueError) as exc:
                    raise CorruptMessageRefs(
                        "semantic refs session index is invalid") from exc
                ordinals = np.asarray(ordinals, dtype=np.int64)
                starts = np.asarray(starts, dtype=np.int64)
                if len(starts):
                    counts = np.diff(np.append(starts, len(ordinals)))
                    codes = np.repeat(
                        np.arange(len(starts), dtype=np.int32), counts)
                else:
                    counts = np.empty(0, dtype=np.int64)
                    codes = np.empty(0, dtype=np.int32)
                cached = (self.db, ordinals, starts, counts, codes, sessions)
                indexes[excluded] = cached
                _MESSAGE_REFS["session_index"] = indexes
            _, ordinals, starts, counts, codes, sessions = cached
            if not len(starts):
                self._assert_source_current()
                return {}
            scores = similarities[ordinals]
            maxima = np.maximum.reduceat(scores, starts)
            # Rows are session/ordinal sorted; the first exact maximum keeps the
            # stable "smallest ordinal wins a tie" behavior.
            matches = np.flatnonzero(scores == np.repeat(maxima, counts))
            match_codes = codes[matches]
            first = np.empty(len(matches), dtype=bool)
            first[0] = True
            first[1:] = match_codes[1:] != match_codes[:-1]
            winners = ordinals[matches[first]]
            result = dict(zip(sessions, map(int, winners)))
            self._assert_source_current()
            return result
        where, params = self._where(filters)
        best: dict[str, int] = {}
        try:
            for ordinal, session in self.db.execute(
                    f"SELECT ord,session FROM refs WHERE {where} ORDER BY ord", params):
                ordinal = self._ordinal(ordinal)
                session = session or ""
                previous = best.get(session)
                if session and (previous is None
                                or similarities[ordinal] > similarities[previous]):
                    best[session] = ordinal
        except sqlite3.DatabaseError as exc:
            raise CorruptMessageRefs(
                "semantic refs failed while grouping candidates") from exc
        self._assert_source_current()
        return best

    def resolve(self, ordinals) -> list[dict]:
        self._assert_source_current()
        requested = [int(i) for i in ordinals]
        if not requested:
            self._assert_source_current()
            return []
        placeholders = ",".join("?" for _ in requested)
        try:
            rows = list(self.db.execute(
                "SELECT r.ord,r.mid,r.agent,r.session,r.project,r.model,r.who,"
                "r.side,r.turn,r.ts,t.source_kind,t.byte_offset,t.byte_length,"
                "t.text_hash,r.row_seal "
                "FROM refs r JOIN texts t USING(ord) "
                f"WHERE r.valid=1 AND r.ord IN ({placeholders})", requested))
        except sqlite3.DatabaseError as exc:
            raise CorruptMessageRefs(
                "semantic refs failed while resolving candidates") from exc
        decoded = []
        for row in rows:
            try:
                ordinal = self._ordinal(row[0])
                mid = row[1]
                agent, session = row[2] or "", row[3] or ""
                project, model = row[4] or "", row[5] or ""
                who, side = row[6] or "user", int(row[7])
                turn, ts = int(row[8] or 0), int(row[9] or 0)
                source_kind = int(row[10])
                byte_offset, byte_length = int(row[11]), int(row[12])
                stored_hash, row_seal = row[13] or "", row[14] or ""
                if (side not in (0, 1) or source_kind not in (0, 1)
                        or byte_offset < 0 or byte_length <= 0
                        or not isinstance(mid, str)
                        or any(not isinstance(value, str) for value in (
                            agent, session, project, model, who,
                            stored_hash, row_seal))):
                    raise TypeError("semantic refs scalar has the wrong type")
                expected_source = self.source_identity[source_kind]
                if (not isinstance(expected_source, list)
                        or len(expected_source) != 4):
                    raise TypeError("semantic refs source identity is invalid")
                source_size = int(expected_source[0])
                if (byte_offset > source_size
                        or byte_length > source_size - byte_offset):
                    raise ValueError("semantic refs source locator is out of bounds")
                if hasattr(self.hashes, "at"):
                    expected_hash = self.hashes.at(ordinal)
                else:
                    start = ordinal * 17
                    expected_hash = self.hashes[start:start + 16].decode("ascii")
                expected_seal = _ref_row_seal(
                    ordinal, mid, agent, session, project, model, who, side,
                    turn, ts, stored_hash, source_kind, byte_offset, byte_length)
                id_mismatch = self.ids is not None and mid != self.ids[ordinal]
                if (id_mismatch or stored_hash != expected_hash
                        or row_seal != expected_seal):
                    raise CorruptMessageRefs(
                        f"semantic refs row {ordinal} failed id/metadata proof")
                decoded.append({
                    "ordinal": ordinal, "mid": mid, "agent": agent,
                    "session": session, "project": project, "model": model,
                    "who": who, "side": bool(side), "turn": turn, "ts": ts,
                    "source_kind": source_kind, "byte_offset": byte_offset,
                    "byte_length": byte_length, "expected_hash": expected_hash,
                })
            except CorruptMessageRefs:
                raise
            except (AttributeError, IndexError, OverflowError, TypeError,
                    UnicodeError, ValueError) as exc:
                raise CorruptMessageRefs(
                    "semantic refs selected row has invalid scalar data") from exc
        decoded.sort(key=lambda item: (item["source_kind"], item["byte_offset"]))

        found = {}
        source_paths = (
            common.MESSAGES_PATH,
            common.DATA_DIR / "replies.jsonl",
        )
        handles = {}
        try:
            # Only the bounded dense candidate pool reaches this random-access path. Grouping
            # locators minimizes cold seeks; the result dictionary restores requested order.
            for item in decoded:
                try:
                    ordinal, mid = item["ordinal"], item["mid"]
                    source_kind = item["source_kind"]
                    byte_offset, byte_length = (
                        item["byte_offset"], item["byte_length"])
                    handle = handles.get(source_kind)
                    if handle is None:
                        handle = source_paths[source_kind].open("rb")
                        if (_stat_identity(os.fstat(handle.fileno()))
                                != self.source_identity[source_kind]):
                            handle.close()
                            raise _EmbeddingBundleMoved(
                                "semantic transcript moved before source open")
                        handles[source_kind] = handle
                    handle.seek(byte_offset)
                    raw = handle.read(byte_length)
                    if len(raw) != byte_length:
                        raise ValueError("semantic refs source locator is out of bounds")
                    source_row = json.loads(raw)
                    if source_kind == 0:
                        actual_mid = source_row.get("id")
                        text = source_row.get("text")
                    else:
                        source_mid = source_row.get("id")
                        actual_mid = source_mid + "#r" if isinstance(source_mid, str) else None
                        text = source_row.get("reply")
                    if not isinstance(text, str):
                        raise TypeError("semantic source text has the wrong type")
                    digest = (
                        compact.content_digest(text)
                        if "content_digest" not in source_row else
                        compact.require_content_digest(
                            source_row["content_digest"])
                    )
                    proven = (actual_mid == mid and _embedding_text_hash(text)
                              == item["expected_hash"])
                except CorruptMessageRefs:
                    raise
                except _EmbeddingBundleMoved:
                    raise
                except (AttributeError, IndexError, OverflowError, TypeError,
                        UnicodeError, ValueError) as exc:
                    raise CorruptMessageRefs(
                        "semantic refs selected row has invalid scalar data") from exc
                if not proven:
                    raise CorruptMessageRefs(
                        f"semantic refs row {ordinal} failed id/text/metadata proof")
                found[ordinal] = {
                    "id": mid, "agent": item["agent"],
                    "session": item["session"], "project": item["project"],
                    "model": item["model"], "who": item["who"],
                    "side": item["side"], "turn": item["turn"],
                    "ts": item["ts"], "text": text,
                    "content_digest": digest,
                }
            for source_kind, handle in handles.items():
                if (_stat_identity(os.fstat(handle.fileno()))
                        != self.source_identity[source_kind]):
                    raise _EmbeddingBundleMoved(
                        "semantic transcript moved during source read")
        except OSError as exc:
            raise _EmbeddingBundleMoved(
                "semantic transcript moved during candidate resolution") from exc
        finally:
            for handle in handles.values():
                try:
                    handle.close()
                except OSError:
                    pass
        self._assert_source_current()
        if any(i not in found for i in requested):
            raise CorruptMessageRefs(
                "semantic refs database lost a requested candidate")
        return [found[i] for i in requested]



def _message_refs_prefix(identity: str) -> str:
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"embeddings.refs-{digest}"


def _publish_message_refs_pointer(
        path, identity: str, rows: int, *, before_publish=None) -> None:
    _require_mutation_allowed()
    record = {
        "version": _MESSAGE_REFS_POINTER_VERSION,
        "path": path.name,
        "identity": identity,
        "rows": int(rows),
        "db_identity": _path_identity(path),
    }
    target = _message_refs_pointer_path()
    temp = common.embedding_temp_path(target, "refs_pointer")
    try:
        temp.write_text(json.dumps(record, separators=(",", ":")), encoding="utf-8")

        def verify_pointer_attempt() -> None:
            if before_publish is not None:
                before_publish()
            _require_mutation_allowed()

        common.replace_with_retry(
            temp, target, before_attempt=verify_pointer_attempt)
    finally:
        temp.unlink(missing_ok=True)


def _pointer_identity_current(identity_record: dict, rows: int) -> bool:
    state = _CURRENT_MESSAGE_STATE
    return bool(
        identity_record.get("schema") == _MESSAGE_REFS_SCHEMA
        and (identity_record.get("embedding") or {}).get("bundle")
        == state.get("bundle")
        and identity_record.get("source") == state.get("source")
        and identity_record.get("messages") == _message_source_identity()[0]
        and identity_record.get("replies") == _message_source_identity()[1]
        and identity_record.get("hashes") == _path_identity(
            common.EMBEDDINGS_PATH.with_suffix(".hashes"))
        and int(rows) > 0)


def _message_refs_from_pointer(coverage: dict):
    meta_path = common.EMBEDDINGS_PATH.parent / "embeddings.meta"
    if segment_query.is_segmented(meta_path):
        manifest, _, refs, current = segment_query.open_current(meta_path)
        if (current != coverage
                or manifest["generation"] != _CURRENT_MESSAGE_STATE.get("generation")):
            raise _EmbeddingBundleMoved(
                "segmented publication moved while opening candidate refs")
        return refs

    rows = int(coverage.get("indexed") or 0)
    cached = _MESSAGE_REFS.get("db")
    identity = _MESSAGE_REFS.get("identity")
    identity_record = _MESSAGE_REFS.get("identity_record")
    if (cached is not None and isinstance(identity, str)
            and isinstance(identity_record, dict)
            and _pointer_identity_current(identity_record, rows)):
        return _MessageRefStore(
            cached, None, _MESSAGE_REFS["hashes"], identity, rows=rows)

    _close_message_refs()
    db = None
    hashes = None
    try:
        pointer = json.loads(_message_refs_pointer_path().read_text(encoding="utf-8"))
        if (not isinstance(pointer, dict)
                or int(pointer.get("version", 0)) != _MESSAGE_REFS_POINTER_VERSION
                or int(pointer.get("rows", -1)) != rows):
            return None
        identity = pointer.get("identity")
        identity_record = json.loads(identity) if isinstance(identity, str) else None
        if (not isinstance(identity_record, dict)
                or not _pointer_identity_current(identity_record, rows)):
            return None
        path = (common.DATA_DIR / str(pointer.get("path") or "")).resolve()
        if (path.parent != common.DATA_DIR.resolve()
                or not path.name.startswith(_message_refs_prefix(identity) + "-")
                or path.suffix != ".db"
                or _path_identity(path) != pointer.get("db_identity")
                or _invalid_message_refs_marker(path).exists()):
            return None
        db = _open_message_refs(path, identity, rows=rows)
        hashes = _open_embedding_hash_blob(rows)
        after = _require_current_message_index()
        if (after != coverage
                or not _pointer_identity_current(identity_record, rows)):
            db.close()
            if hasattr(hashes, "close"):
                hashes.close()
            raise _EmbeddingBundleMoved(
                "semantic publication moved while opening candidate refs")
        _MESSAGE_REFS.update(
            identity=identity, path=path, db=db, hashes=hashes,
            ids_obj=None, identity_record=identity_record, session_index=None)
        return _MessageRefStore(db, None, hashes, identity, rows=rows)
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError,
            sqlite3.DatabaseError, CorruptMessageRefs):
        if db is not None:
            db.close()
        if hasattr(hashes, "close"):
            hashes.close()
        return None


def _invalid_message_refs_marker(path):
    return path.with_name(path.name + ".invalid")


def _quarantine_message_refs_path(path) -> None:
    if _mutation_refused():
        return
    marker = _invalid_message_refs_marker(path)
    try:
        marker.touch(exist_ok=True)
    except OSError:
        pass
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return
    try:
        marker.unlink(missing_ok=True)
    except OSError:
        pass


def _existing_message_refs(identity: str, ids: list[str]):
    prefix = _message_refs_prefix(identity)
    try:
        candidates = sorted(common.DATA_DIR.glob(prefix + "-*.db"),
                            key=lambda path: path.stat().st_mtime_ns, reverse=True)
    except OSError:
        candidates = []
    for path in candidates:
        if _invalid_message_refs_marker(path).exists():
            continue
        try:
            return path, _open_message_refs(path, identity, ids)
        except (CorruptMessageRefs, sqlite3.DatabaseError):
            # The prefix already binds this file to the requested generation;
            # failing its immutable shape/seal means it is derived-data debris.
            if not _mutation_refused():
                _quarantine_message_refs_path(path)
            continue
        except (OSError, ValueError):
            continue
    return None, None


def _prune_message_refs(keep) -> None:
    """Best-effort cleanup without ever replacing an open SQLite generation."""
    if _mutation_refused():
        return
    try:
        temps = list(common.DATA_DIR.glob(".embeddings.refs-*.tmp"))
        dbs = sorted(common.DATA_DIR.glob("embeddings.refs-*.db"),
                     key=lambda path: path.stat().st_mtime_ns, reverse=True)
        invalid = list(common.DATA_DIR.glob("embeddings.refs-*.db.invalid"))
    except OSError:
        return
    for path in temps:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    protected = {keep, _MESSAGE_REFS.get("path")}
    for marker in invalid:
        db_path = marker.with_name(marker.name[:-len(".invalid")])
        if db_path in protected:
            continue
        try:
            db_path.unlink(missing_ok=True)
            marker.unlink(missing_ok=True)
        except OSError:
            # Another Windows reader may still own the quarantined generation.
            pass
    for path in dbs:
        if path in protected:
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            # Windows refuses to unlink a generation another worker has open.
            pass


def invalidate_message_refs() -> None:
    """Quarantine the currently-open refs generation after a candidate proof fails.

    Closing before unlink permits immediate repair on Windows. If another process
    still maps the immutable DB, a tiny marker prevents new readers from reopening
    it until best-effort pruning can remove both files.
    """
    path = _MESSAGE_REFS.get("path")
    _close_message_refs()
    if path is None or _mutation_refused():
        return
    _quarantine_message_refs_path(path)


def _prune_dead_message_ref_temps() -> None:
    """Remove killed-builder debris without touching a live process's temp file."""
    if _mutation_refused():
        return
    prefix = ".embeddings.refs-build-"
    try:
        candidates = list(common.DATA_DIR.glob(prefix + "*.tmp"))
    except OSError:
        return
    for path in candidates:
        # .embeddings.refs-build-<pid>-<kernel-start>-<publication>.tmp
        try:
            owner_s, expected_start, _ = path.name[len(prefix):-4].split("-", 2)
            owner = int(owner_s)
        except (ValueError, IndexError):
            continue
        if owner > 0 and common.pid_alive(owner):
            actual_start = common.process_start_identity(owner)
            # If the kernel identity is unavailable on this filesystem/platform,
            # preserving a live PID's file is the conservative choice.
            if (expected_start == "unknown" or actual_start is None
                    or actual_start == expected_start):
                continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _stable_message_hash_blob(ids: list[str], matrix, identity: str):
    """Bind compact candidate hashes to the same matrix/ids publication."""
    raw = _open_embedding_hash_blob(len(ids))
    after = _message_refs_identity(ids, matrix)
    if after != identity:
        if hasattr(raw, "close"):
            raw.close()
        raise _EmbeddingBundleMoved(
            "embedding publication moved while loading candidate hashes")
    return raw


def _fast_cached_message_refs(ids: list[str], matrix):
    """Return the already-open exact generation without rehashing every id.

    `_require_current_message_index` has just validated the committed bundle and
    marker. The same ids object + mmap identity prove the in-process pair, while
    cheap identities close the publication race around canonical artifacts. A new
    mapping or any source/output movement falls back to the full identity proof.
    """
    cached = _MESSAGE_REFS.get("db")
    record = _MESSAGE_REFS.get("identity_record")
    state = _CURRENT_MESSAGE_STATE
    if (cached is None or matrix is None or ids is not _MESSAGE_REFS.get("ids_obj")
            or not isinstance(record, dict)
            or not isinstance(state.get("artifacts"), dict)):
        return None
    try:
        expected_bundle = record["embedding"]["bundle"]
        if (state.get("bundle") != expected_bundle
                or common.embedding_matrix_identity(matrix) != expected_bundle
                or state.get("source") != record.get("source")
                or common.transcript_generation() != record.get("source")):
            return None
        paths = {
            "matrix": common.EMBEDDINGS_PATH,
            "ids": common.IDS_PATH,
            "meta": common.EMBEDDINGS_PATH.parent / "embeddings.meta",
            "hashes": common.EMBEDDINGS_PATH.with_suffix(".hashes"),
        }
        for name, expected in state["artifacts"].items():
            path = paths.get(name)
            if path is None or not common._embedding_identity_matches(expected, path):
                return None
        path = _MESSAGE_REFS.get("path")
        if path is None or _invalid_message_refs_marker(path).exists():
            return None
        return _MessageRefStore(
            cached, ids, _MESSAGE_REFS["hashes"], _MESSAGE_REFS["identity"])
    except (KeyError, OSError, RuntimeError, TypeError, ValueError,
            json.JSONDecodeError):
        return None


def _message_refs(ids: list[str], matrix=None,
                  coverage: dict | None = None, *,
                  allow_build: bool = True) -> _MessageRefStore:
    """Open/build the compact semantic metadata + candidate-text snapshot."""
    # Validate the marker after proving the mapped matrix is still canonical.
    # A publish between map and guard is therefore retried as a moved bundle,
    # then refused if the new commit has not received its publish-last marker.
    if coverage is None:
        coverage = _require_current_message_index()
    fast = _fast_cached_message_refs(ids, matrix)
    if fast is not None:
        return fast
    identity = _message_refs_identity(ids, matrix)
    cached = _MESSAGE_REFS.get("db")
    if (cached is not None and _MESSAGE_REFS.get("identity") == identity
            and _MESSAGE_REFS.get("path") is not None
            and _MESSAGE_REFS.get("hashes") is not None):
        _MESSAGE_REFS["ids_obj"] = ids
        try:
            _MESSAGE_REFS["identity_record"] = json.loads(identity)
        except (TypeError, ValueError, json.JSONDecodeError):
            _MESSAGE_REFS["identity_record"] = None
        return _MessageRefStore(
            cached, ids, _MESSAGE_REFS["hashes"], _MESSAGE_REFS["identity"])
    _close_message_refs()
    _prune_dead_message_ref_temps()

    path, db = _existing_message_refs(identity, ids)
    if db is None:
        refusal = _mutation_refusal_reason()
        if refusal is not None:
            raise MessageRefsUnavailable(refusal)
        if not allow_build:
            raise MessageRefsUnavailable(
                "semantic candidate refs are preparing for this generation")
        _require_mutation_allowed()
        common.DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Serialize only competing refs builders; the global ingest lock would block
        # transcript freshening, and the before/after identity check keeps this safe.
        with common.EmbeddingPublishLock(
                common.DATA_DIR / "embeddings.refs-build",
                timeout=30.0) as publish_lock:
            # Another refs process may have published while we waited.
            path, db = _existing_message_refs(identity, ids)
            if db is None:
                for attempt in range(3):
                    before = _message_refs_identity(ids, matrix)
                    prefix = _message_refs_prefix(before)
                    publication_id = uuid.uuid4().hex
                    path = common.DATA_DIR / f"{prefix}-{publication_id}.db"
                    process_start = common.process_start_identity(os.getpid()) or "unknown"
                    tmp = common.DATA_DIR / (
                        f".embeddings.refs-build-{os.getpid()}-"
                        f"{process_start}-{publication_id}.tmp")
                    try:
                        _create_message_refs_db(tmp, ids, before, coverage)
                        after = _message_refs_identity(ids, matrix)
                        if after != before:
                            if attempt < 2:
                                continue
                            raise RuntimeError(
                                "semantic transcript generation kept moving during snapshot")
                        # final name never pre-exists; open Windows readers keep their generation
                        publish_lock.verify()
                        _require_mutation_allowed()
                        os.replace(tmp, path)
                        identity = before
                        break
                    finally:
                        tmp.unlink(missing_ok=True)
                else:  # pragma: no cover - loop exits by break/raise
                    raise RuntimeError("could not publish semantic transcript snapshot")
                db = _open_message_refs(path, identity, ids)
            publish_lock.verify()
            _prune_message_refs(path)
    hashes = _stable_message_hash_blob(ids, matrix, identity)
    try:
        identity_record = json.loads(identity)
    except (TypeError, ValueError, json.JSONDecodeError):
        identity_record = None
    if not _mutation_refused():
        try:
            meta_path = common.EMBEDDINGS_PATH.parent / "embeddings.meta"
            with common.EmbeddingPublicationGuard(
                    meta_path, common.EMBEDDINGS_PATH,
                    timeout=0.0) as embedding_guard:
                with common.EmbeddingPublishLock(
                        common.DATA_DIR / "embeddings.refs-build",
                        timeout=0.0) as publish_lock:
                    if _message_refs_identity(ids, matrix) != identity:
                        raise _EmbeddingBundleMoved(
                            "embedding publication moved before refs pointer publication")

                    def verify_pointer_publish() -> None:
                        embedding_guard.verify()
                        publish_lock.verify()

                    _publish_message_refs_pointer(
                        path, identity, len(ids),
                        before_publish=verify_pointer_publish)
        except OSError:
            pass
        except _EmbeddingBundleMoved:
            db.close()
            if hasattr(hashes, "close"):
                hashes.close()
            raise
    _MESSAGE_REFS.update(
        identity=identity, path=path, db=db, hashes=hashes,
        ids_obj=ids, identity_record=identity_record, session_index=None)
    return _MessageRefStore(db, ids, hashes, identity)


def prepare_message_refs(coverage: dict) -> dict:
    """Build the next immutable refs sidecar before its generation marker lands.

    The embedding publisher already proved the exact indexed/total coverage. This
    builder independently streams messages/replies, verifies every vector hash and
    the before/after artifact identity, then closes all mappings. Query readers still
    require the publish-last semantic marker before they may open the result.
    """
    meta_path = common.EMBEDDINGS_PATH.parent / "embeddings.meta"
    if segment_query.is_segmented(meta_path):
        manifest, _, _, current = segment_query.open_current(meta_path)
        if current != coverage:
            raise _EmbeddingBundleMoved(
                "segmented publication moved while preparing candidate refs")
        refs_bytes = sum(int(segment["artifacts"]["refs"]["size"])
                         for segment in manifest["segments"])
        return {"rows": int(manifest["live_rows"]), "path": str(meta_path),
                "bytes": refs_bytes}

    dim, model = common.read_index_meta(meta_path)
    _guard_embedder(model, "message index", "embed.py")
    ids, matrix = common.read_embeddings(
        common.EMBEDDINGS_PATH, common.IDS_PATH, dim=dim,
        meta_path=common.EMBEDDINGS_PATH.parent / "embeddings.meta",
        publication_read_only=_mutation_refused())
    normalized = {
        "indexed": int(coverage["indexed"]),
        "total": int(coverage["total"]),
        "pending": int(coverage["pending"]),
        "complete": int(coverage["pending"]) == 0,
    }
    try:
        refs = _message_refs(ids, matrix, coverage=normalized, allow_build=True)
        path = _MESSAGE_REFS.get("path")
        return {"rows": len(ids), "path": str(path) if path else "",
                "bytes": path.stat().st_size if path else 0}
    finally:
        _close_message_refs()
        common.close_embedding_matrix(matrix)


def _message_artifacts(dim: int, meta_path, *, allow_refs_build: bool = True):
    """Load one vector+refs generation, retrying a publish-between-reads race."""
    if segment_query.is_segmented(meta_path):
        coverage = _require_current_message_index()
        manifest, matrix, refs, current = segment_query.open_current(
            meta_path, need_matrix=True)
        if (int(manifest["model"]["dim"]) != int(dim)
                or current != coverage
                or manifest["generation"] != _CURRENT_MESSAGE_STATE.get("generation")):
            raise _EmbeddingBundleMoved(
                "segmented publication moved while loading semantic artifacts")
        return (), matrix, refs, coverage

    last_error = None
    for _ in range(2):
        ids, matrix = _cached(
            "msg_emb", (common.EMBEDDINGS_PATH, common.IDS_PATH, meta_path),
            lambda: _load_message_embeddings(dim, meta_path))
        try:
            coverage = _require_current_message_index()
            return ids, matrix, _message_refs(
                ids, matrix, coverage=coverage,
                allow_build=allow_refs_build), coverage
        except _EmbeddingBundleMoved as exc:
            last_error = exc
            stale = _CACHE.pop("msg_emb", None)
            if stale is not None:
                _release_cached_value(stale[1])
    raise _EmbeddingBundleMoved(
        "embedding publication kept moving while loading semantic refs") from last_error


def _load_message_embeddings(dim: int, meta_path):
    ids, matrix = common.read_embeddings(
        common.EMBEDDINGS_PATH, common.IDS_PATH,
        dim=dim, meta_path=meta_path,
        publication_read_only=_mutation_refused())
    # Freeze ids: object-identity fast paths must not survive an in-process mutation.
    return tuple(ids), matrix


def _require_current_message_index() -> dict:
    """Validate the current full OR explicitly-partial message generation.

    Partial does not mean stale: its marker binds every indexed row to the exact
    current transcript and committed vector bundle while disclosing how many
    source rows remain unembedded. Unmarked/stale bundles remain unusable.
    """
    meta_path = common.EMBEDDINGS_PATH.parent / "embeddings.meta"
    if segment_query.is_segmented(meta_path):
        try:
            manifest, _, _, coverage = segment_query.open_current(meta_path)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise RuntimeError(
                "semantic message index is not a validated current generation; "
                "run meaning search through managed `agrep -s`") from exc
        _CURRENT_MESSAGE_STATE.update(
            bundle=(f"segment:{manifest['generation']}:"
                    f"{manifest['set_manifest']['sha256']}"),
            artifacts={"set_manifest": manifest["set_manifest"]},
            source=manifest["source"], generation=manifest["generation"])
        return coverage

    marker_path = common.DATA_DIR / ".semantic-embeddings-generation.json"
    try:
        source = common.transcript_generation()
        state = common.committed_embedding_artifact_state(
            meta_path, common.EMBEDDINGS_PATH, common.IDS_PATH)
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "semantic message index is not a validated current generation; "
            "run meaning search through managed `agrep -s`") from exc
    if not isinstance(marker, dict):
        marker = {}
    marker_output = marker.get("output")
    if not isinstance(marker_output, dict):
        marker_output = {}
    bound = (source is not None and state["commit"] is not None
             and marker.get("source") == source
             and marker_output.get("bundle") == state["identity"])
    rows = int(state["commit"]["rows"]) if state["commit"] is not None else 0
    coverage = None
    if bound and marker.get("version") == 2:
        coverage = {
            "indexed": rows, "total": rows, "pending": 0,
            "fraction": 1.0, "complete": True, "order": "complete",
        }
    elif bound and marker.get("version") == 3 and isinstance(
            marker.get("coverage"), dict):
        raw = marker["coverage"]
        try:
            indexed, total = int(raw["indexed"]), int(raw["total"])
            pending = int(raw["pending"])
        except (KeyError, TypeError, ValueError):
            indexed = total = pending = -1
        complete = indexed == total
        if (indexed == rows and indexed > 0 and total >= indexed
                and pending == total - indexed):
            coverage = {
                "indexed": indexed, "total": total, "pending": total - indexed,
                "fraction": round(indexed / total, 6), "complete": complete,
                "order": str(raw.get("order") or "newest-first"),
            }
    if coverage is None:
        raise RuntimeError(
            "semantic message index is stale or has invalid coverage; "
            "run meaning search through managed `agrep -s`")
    _CURRENT_MESSAGE_STATE.update(
        bundle=state["identity"], artifacts=state["artifacts"], source=source,
        generation=state["commit"]["generation"])
    return coverage


def _excluded_sessions(filters: dict) -> frozenset[str] | None:
    """Decode a serialized family at most once; ``None`` is fail-closed."""
    if filters.get("_exclude_sessions_invalid"):
        return None
    cached = filters.get("_exclude_sessions")
    if isinstance(cached, (list, tuple, set, frozenset)):
        if any(not isinstance(value, str) or not value for value in cached):
            filters["_exclude_sessions_invalid"] = True
            return None
        decoded = frozenset(cached)
        filters["_exclude_sessions"] = decoded
        return decoded
    if "_exclude_sessions" in filters:
        filters["_exclude_sessions_invalid"] = True
        return None
    raw = filters.get("_exclude_sessions_json")
    if not raw:
        return frozenset()
    try:
        values = json.loads(raw)
    except (TypeError, ValueError):
        filters["_exclude_sessions_invalid"] = True
        return None
    if (not isinstance(values, list)
            or any(not isinstance(value, str) for value in values)):
        filters["_exclude_sessions_invalid"] = True
        return None
    decoded = frozenset(values)
    filters["_exclude_sessions"] = decoded
    return decoded


def _matches(row: dict, filters: dict | None) -> bool:
    """Server-side semantic metadata filtering, mirroring search._filtered.

    Filtering before candidate selection is both faster and correct: client-side
    filtering of an already-truncated semantic page can miss valid rows that were
    just below the unfiltered cutoff.
    """
    if not filters:
        return True
    if filters.get("agent") and filters["agent"].lower() not in (row.get("agent") or "").lower():
        return False
    project = row.get("project") or row.get("cwd_project") or ""
    if filters.get("project") and filters["project"].lower() not in project.lower():
        return False
    if (filters.get("exclude_project")
            and filters["exclude_project"].lower() in project.lower()):
        return False
    if filters.get("chat") and not (row.get("session") or "").lower().startswith(
            filters["chat"].lower()):
        return False
    session = str(row.get("session") or "")
    if session == filters.get("exclude_session"):
        boundary = filters.get("exclude_session_from_turn")
        if boundary is None:
            return False
        turn = row.get("turn")
        if (type(boundary) is int and type(turn) is int  # noqa: E721
                and turn >= boundary):
            return False
    excluded_family_id = filters.get("_exclude_family_id")
    if (excluded_family_id is not None
            and row.get("family_id") == excluded_family_id):
        return False
    if filters.get("_exclude_sessions_json") or "_exclude_sessions" in filters:
        excluded_sessions = _excluded_sessions(filters)
        if excluded_sessions is None or session in excluded_sessions:
            return False
    if filters.get("who") and row.get("who") != filters["who"]:
        return False
    if ("_include_who" in filters
            and row.get("who") not in segment_query.metadata_included_roles(filters)):
        return False
    if row.get("who") in segment_query.metadata_excluded_roles(filters):
        return False
    if filters.get("model"):
        actual = (row.get("model") or "").lower()
        needle = filters["model"].lower()
        if (needle not in actual) if filters.get("model_soft") else (needle != actual):
            return False
    ts = int(row.get("ts") or 0)
    if filters.get("since_ms") is not None and ts < int(filters["since_ms"]):
        return False
    if filters.get("until_ms") is not None and ts >= int(filters["until_ms"]):
        return False
    return True


def _top_indices(values: np.ndarray, n: int) -> np.ndarray:
    """Largest values in score-descending, row-ascending order."""
    values = np.asarray(values).reshape(-1)
    n = max(0, min(int(n), len(values)))
    if not n:
        return np.empty(0, dtype=np.int64)
    if n == len(values):
        selected = np.arange(len(values), dtype=np.int64)
    else:
        threshold = np.partition(values, len(values) - n)[len(values) - n]
        above = np.flatnonzero(values > threshold)
        ties = np.flatnonzero(values == threshold)[:n - len(above)]
        selected = np.concatenate((above, ties)).astype(np.int64, copy=False)
    return selected[np.lexsort((selected, -values[selected]))]




def _family_representatives(sessions: list[str], scores: np.ndarray,
                            parents: dict | None = None) -> np.ndarray:
    """Best raw session for each root conversation family.

    Subagents are independently stored chats because their answers are valuable,
    but treating every child as an unrelated nearest-neighbor candidate lets one
    large swarm consume an entire semantic page.  Collapse only for ranked
    session-level retrieval: the highest-scoring root or child remains the evidence
    row, while literal grep and message-level semantic search stay exhaustive.
    """
    values = np.asarray(scores, dtype=np.float32).reshape(-1)
    if len(sessions) != len(values):
        raise ValueError("semantic family/session score mismatch")
    roots = common.indexed_family_roots(sessions) if parents is None else None
    memo: dict[str, str] = {}
    best: dict[str, int] = {}
    for i, session in enumerate(sessions):
        family = (roots or {}).get(session, session) if parents is None else (
            common.family_root(session, parents, memo))
        previous = best.get(family)
        if previous is None or values[i] > values[previous]:
            best[family] = i
    return np.fromiter(best.values(), dtype=np.int64, count=len(best))


def _family_diversity_enabled(filters: dict | None) -> bool:
    """Internal semantic option; defaults on for direct/older callers."""
    return bool((filters or {}).get("_family_diverse", True))


def _session_grouping_required(filters: dict | None) -> bool:
    if not _family_diversity_enabled(filters):
        return True
    who = (filters or {}).get("who")
    if who is not None:
        return any(
            surface.speaker_filter_admits(who, role)
            for role in common.SEMANTIC_DEFAULT_EXCLUDED_ROLES
        )
    included = set(segment_query.metadata_included_roles(filters))
    if "_include_who" in (filters or {}):
        return bool(included & common.SEMANTIC_DEFAULT_EXCLUDED_ROLES)
    if "_exclude_who" in (filters or {}):
        excluded = set(segment_query.metadata_excluded_roles(filters))
        return bool(common.SEMANTIC_DEFAULT_EXCLUDED_ROLES - excluded)
    return False


def _metadata_filters(filters: dict | None) -> dict | None:
    """User row filters only; private retrieval controls must not disable fast paths."""
    clean = {key: value for key, value in (filters or {}).items()
             if not key.startswith("_")
             or key in {"_exclude_who", "_include_who",
                        "_exclude_sessions"}}
    return clean or None


def _message_filters(refs, filters: dict | None) -> dict | None:
    """Attach generation-local family filters to ordinary semantic filters."""
    clean = _metadata_filters(filters)
    caller = str((clean or {}).get("exclude_session") or "")
    if (clean or {}).get("exclude_session_from_turn") is not None:
        # A recap boundary applies only to the caller's own turn space.
        return clean
    exclude_family = (clean or {}).get("exclude_family", True)
    if not caller or not exclude_family:
        return clean
    family_id = None
    resolver = getattr(refs, "family_id_for_session", None)
    if callable(resolver):
        try:
            family_id = resolver(caller)
        except segment_query.SegmentQueryError:
            family_id = None
    indexed = common.indexed_calling_family_with_sides(caller)
    family_resolver = getattr(refs, "family_id_for_sessions", None)
    if family_id is None and indexed is not None and callable(family_resolver):
        try:
            family_id = family_resolver(indexed[1])
        except segment_query.SegmentQueryError:
            family_id = None
    if family_id is not None:
        clean = dict(clean or {})
        clean["_exclude_family_id"] = family_id
        if indexed is not None:
            clean["_exclude_sessions"] = indexed[1]
        return clean
    if indexed is not None:
        clean = dict(clean or {})
        clean["_exclude_sessions"] = indexed[1]
    return clean


_Q8_DEFAULT_EXCLUDED_ROLES = common.SEMANTIC_DEFAULT_EXCLUDED_ROLES


def _q8_grouped_fast_path(filters: dict | None) -> bool:
    row_filters = filters or {}
    if ((row_filters.get("_exclude_sessions_json")
         or row_filters.get("_exclude_sessions"))
            and row_filters.get("_exclude_family_id") is None):
        return False
    return bool(
        row_filters
        and set(row_filters) <= {
            "_exclude_who", "exclude_session", "_exclude_family_id",
            "_exclude_sessions", "_exclude_sessions_json"}
        and set(segment_query.metadata_excluded_roles(row_filters))
        == _Q8_DEFAULT_EXCLUDED_ROLES)


def _q8_selection(refs, row_filters: dict | None):
    if not row_filters:
        manifest = getattr(refs, "manifest", {})
        count = int(manifest.get("live_rows", getattr(refs, "rows", 0)))
        return None, count, None
    packed = getattr(refs, "q8_eligibility", None)
    if callable(packed):
        return packed(row_filters)
    eligible = refs.eligible(row_filters)
    return eligible, len(eligible), None


def _refuse_large_dense_fallback(refs, coverage: dict | None) -> None:
    rows = max(int((coverage or {}).get("indexed") or 0),
               int(getattr(refs, "rows", 0) or 0))
    if rows > DENSE_FALLBACK_MAX_ROWS:
        raise RuntimeError(
            f"native semantic candidate scan unavailable for {rows} rows; "
            "refusing an exhaustive dense fallback")


def _q8_grouped_pool(query: np.ndarray, refs: _MessageRefStore,
                     filters: dict | None, k: int):
    if not _family_diversity_enabled(filters):
        return None
    generation = str(_CURRENT_MESSAGE_STATE.get("generation") or "")
    if not generation:
        return None
    row_filters = _message_filters(refs, filters)
    excluded_family_id = (row_filters or {}).get("_exclude_family_id")
    eligible = None
    eligible_count = family_count = None
    if row_filters and not _q8_grouped_fast_path(row_filters):
        eligible, eligible_count, family_count = _q8_selection(refs, row_filters)
        if not eligible_count:
            return [], np.empty(0, dtype=np.float32), 0
    try:
        import semantic_q8
        reserved_groups = 1 + int(excluded_family_id is not None)
        group_k = min(256, max(128, int(k) + reserved_groups))
        result = semantic_q8.grouped_exact_candidates(
            query, generation, k=group_k, heads=8, eligible=eligible)
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return None
    if result is None:
        return None
    ordinals, scores, groups, group_count = result
    ordinals = np.asarray(ordinals, dtype=np.int64).reshape(-1)
    scores = np.asarray(scores, dtype=np.float32).reshape(-1)
    groups = np.asarray(groups, dtype=np.uint32).reshape(-1)
    if (not len(ordinals) or len(scores) != len(ordinals)
            or len(groups) != len(ordinals) or not np.all(np.isfinite(scores))):
        return None
    excluded_family_present = (
        excluded_family_id is not None
        and 0 <= int(excluded_family_id) < int(group_count)
    )
    if excluded_family_present:
        keep = groups != int(excluded_family_id)
        ordinals, scores, groups = ordinals[keep], scores[keep], groups[keep]
    best: dict[int, int] = {}
    for position, (ordinal, score, group) in enumerate(
            zip(ordinals, scores, groups)):
        previous = best.get(int(group))
        if (previous is None or score > scores[previous]
                or (score == scores[previous]
                    and int(ordinal) < int(ordinals[previous]))):
            best[int(group)] = position
    selected = sorted(
        best.values(), key=lambda i: (-float(scores[i]), int(ordinals[i])))
    pool_n = min(SEMANTIC_MAX_POOL, max(k * 6, 40), len(selected))
    chosen_ordinals = ordinals[selected]
    rows = refs.resolve(chosen_ordinals)
    selected_by_ordinal = {
        int(ordinals[position]): position for position in selected}
    kept_rows, kept_positions = [], []
    for resolved_index, row in enumerate(rows):
        if not _matches(row, row_filters):
            continue
        ordinal = int(row.get("ordinal", chosen_ordinals[resolved_index]))
        position = selected_by_ordinal.get(ordinal)
        if position is None:
            continue
        kept_rows.append(row)
        kept_positions.append(position)
        if len(kept_rows) >= pool_n:
            break
    if family_count is not None:
        candidate_count = family_count
    elif eligible_count is not None:
        candidate_count = min(int(group_count), int(eligible_count))
    else:
        candidate_count = int(group_count) - int(
            _q8_grouped_fast_path(row_filters))
        if excluded_family_present:
            candidate_count -= 1
    return (
        kept_rows,
        scores[np.asarray(kept_positions, dtype=np.int64)],
        max(0, candidate_count),
    )


def _q8_flat_pool(query: np.ndarray, refs: _MessageRefStore,
                  filters: dict | None, k: int):
    generation = str(_CURRENT_MESSAGE_STATE.get("generation") or "")
    if not generation:
        return None
    row_filters = _message_filters(refs, filters)
    selection_filters = dict(row_filters or {})
    excluded_family_id = selection_filters.pop("_exclude_family_id", None)
    if excluded_family_id is not None:
        selection_filters.pop("exclude_session", None)
    eligible, candidate_count, _ = _q8_selection(
        refs, selection_filters or None)
    if excluded_family_id is not None:
        import semantic_q8
        live = getattr(refs, "live", None)
        eligible = semantic_q8.eligibility_without_group(
            generation, int(excluded_family_id), live, eligible)
        if eligible is None:
            return None
        candidate_count = eligible.count
    if not candidate_count:
        return [], np.empty(0, dtype=np.float32), 0
    try:
        import semantic_q8
        pool_n = min(SEMANTIC_MAX_POOL, max(k * 6, 40), candidate_count)
        result = semantic_q8.exact_candidates(
            query, generation, k=pool_n, eligible=eligible)
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return None
    if result is None:
        return None
    ordinals, scores = result
    ordinals = np.asarray(ordinals, dtype=np.int64).reshape(-1)
    scores = np.asarray(scores, dtype=np.float32).reshape(-1)
    if len(scores) != len(ordinals) or not np.all(np.isfinite(scores)):
        return None
    resolved = refs.resolve(ordinals)
    position_by_ordinal = {
        int(ordinal): position for position, ordinal in enumerate(ordinals)}
    # Candidates arrive score-descending, so keeping a row's first surviving
    # ordinal max-pools its '#cN' chunk vectors into one logical result.
    seen_mid, seen_text, rows, kept = set(), set(), [], []
    for resolved_index, row in enumerate(resolved):
        if not _matches(row, row_filters):
            continue
        mid = str(row.get("mid") or "")
        if mid and mid in seen_mid:
            continue
        key = " ".join((row.get("text") or "").split())[:140].lower()
        if key in seen_text:
            continue
        ordinal = int(row.get("ordinal", ordinals[resolved_index]))
        position = position_by_ordinal.get(ordinal)
        if position is None:
            continue
        if mid:
            seen_mid.add(mid)
        seen_text.add(key)
        rows.append(row)
        kept.append(position)
    return (rows, scores[np.asarray(kept, dtype=np.int64)],
            int(candidate_count))


def _q8_session_pool(query: np.ndarray, refs: _MessageRefStore,
                     filters: dict | None, k: int):
    """Bounded native candidates, retaining the best row from each session."""
    generation = str(_CURRENT_MESSAGE_STATE.get("generation") or "")
    if not generation:
        return None
    row_filters = _message_filters(refs, filters)
    selection_filters = dict(row_filters or {})
    excluded_family_id = selection_filters.pop("_exclude_family_id", None)
    if excluded_family_id is not None:
        selection_filters.pop("exclude_session", None)
    fast_default_roles = _q8_grouped_fast_path(row_filters)
    if fast_default_roles:
        manifest = getattr(refs, "manifest", {})
        candidate_count = int(manifest.get(
            "live_rows", getattr(refs, "rows", 0)))
        eligible = None
    else:
        eligible, candidate_count, _ = _q8_selection(
            refs, selection_filters or None)
    target = min(SEMANTIC_MAX_POOL, max(int(k) * 6, 40))
    probe_target = target + 1
    seen_sessions: set[str] = set()
    best: dict[str, tuple[float, int, dict]] = {}
    pages = 0
    while True:
        try:
            import semantic_q8
            if fast_default_roles and not seen_sessions:
                live = getattr(refs, "live", None)
                eligible = semantic_q8.eligibility_without_group(
                    generation, 0, live, eligible)
                if eligible is None:
                    return None
                if excluded_family_id not in (None, 0):
                    eligible = semantic_q8.eligibility_without_group(
                        generation, int(excluded_family_id), live, eligible)
                    if eligible is None:
                        return None
                candidate_count = eligible.count
            elif seen_sessions:
                next_filters = dict(selection_filters)
                existing = next_filters.get("_exclude_sessions") or ()
                next_filters["_exclude_sessions"] = frozenset(
                    {*existing, *seen_sessions})
                eligible, candidate_count, _ = _q8_selection(
                    refs, next_filters)
                if excluded_family_id is not None:
                    eligible = semantic_q8.eligibility_without_group(
                        generation, int(excluded_family_id),
                        getattr(refs, "live", None), eligible)
                    if eligible is None:
                        return None
                    candidate_count = eligible.count
            elif excluded_family_id is not None:
                eligible = semantic_q8.eligibility_without_group(
                    generation, int(excluded_family_id),
                    getattr(refs, "live", None), eligible)
                if eligible is None:
                    return None
                candidate_count = eligible.count
            if not candidate_count:
                break
            if pages >= SEMANTIC_MAX_SESSION_PAGES:
                return None
            batch_n = min(
                SEMANTIC_MAX_SESSION_CANDIDATES, int(candidate_count))
            result = semantic_q8.exact_candidates(
                query, generation, k=batch_n, eligible=eligible)
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            return None
        if result is None:
            return None
        pages += 1
        ordinals, scores = result
        ordinals = np.asarray(ordinals, dtype=np.int64).reshape(-1)
        scores = np.asarray(scores, dtype=np.float32).reshape(-1)
        if (len(scores) != len(ordinals) or not np.all(np.isfinite(scores))
                or (not len(ordinals) and candidate_count)):
            return None
        positions = {int(ordinal): i for i, ordinal in enumerate(ordinals)}
        batch_sessions = set()
        for resolved_index, row in enumerate(refs.resolve(ordinals)):
            if not _matches(row, row_filters):
                continue
            session = str(row.get("session") or "")
            if not session:
                continue
            ordinal = int(row.get("ordinal", ordinals[resolved_index]))
            position = positions.get(ordinal)
            if position is None:
                continue
            score = float(scores[position])
            previous = best.get(session)
            if (previous is None or score > previous[0]
                    or (score == previous[0] and ordinal < previous[1])):
                best[session] = (score, ordinal, row)
            batch_sessions.add(session)
        if not batch_sessions:
            return None
        seen_sessions.update(batch_sessions)
        exhausted = len(ordinals) < batch_n
        if len(best) >= probe_target or exhausted:
            break
    selected = sorted(
        best.values(), key=lambda item: (-item[0], item[1]))[:target]
    return (
        [item[2] for item in selected],
        np.asarray([item[0] for item in selected], dtype=np.float32),
        len(best),
    )


def _summary_artifacts(dim: int):
    ids_path = common.DATA_DIR / "summary_emb.ids"
    emb_path = common.DATA_DIR / "summary_emb.f32"
    meta_path = common.DATA_DIR / "summary_emb.meta"
    summaries_path = common.DATA_DIR / "summaries.jsonl"

    def build():
        ids, mat = common.read_embeddings(
            emb_path, ids_path, dim=dim, meta_path=meta_path,
            publication_read_only=_mutation_refused())
        recs = {}
        for line in _lines(summaries_path.read_text(encoding="utf-8")):
            o = json.loads(line)
            recs[o["session"]] = o
        return ids, mat, recs

    return _cached(
        "summary_semantic", (ids_path, emb_path, meta_path, summaries_path), build)


# ----------------------------- tool implementations -----------------------------

def _embed_query(text: str, idx_model: str | None = None) -> np.ndarray:
    """Embed a query in the lane that built the rows it will be scored against.

    Not the lane this machine would pick: an fp16-Metal row and an int8-CPU
    query sit at ~0.999 cosine, which survives every shape check and still
    reorders anything near a threshold. The store decides; the machine does not.
    """
    return embedder.get(lane=embedder.resolve_lane(idx_model)).embed_query(text)


def _q8_shadow_comparison(
    query: np.ndarray,
    matrix,
    f32_scores: np.ndarray,
) -> dict | None:
    value = os.environ.get("AGREP_SEMANTIC_Q8_SHADOW", "")
    if value.lower() in ("", "0", "false", "no", "off"):
        return None
    import semantic_q8
    scores = semantic_q8.shadow_scores(query, matrix)
    if scores is None:
        return {
            "state": "f32-fallback",
            "score_kind": semantic_q8.SCORE_KIND,
            "used_for_ranking": False,
        }
    return semantic_q8.comparison(f32_scores, scores)


def _guard_embedder(idx_model: str | None, what: str, rebuild_script: str) -> None:
    """Fail loud when the index wasn't built by the active embedder profile. Without
    this the dot products silently rank in the wrong vector space -> plausible-looking
    but meaningless results. Skipped for legacy indexes whose model id is unknown."""
    active = embedder.store_profile_string(idx_model)
    if idx_model and idx_model != active:
        lever = (
            f"Run where the {embedder.LANE_METAL} lane is available (it is not "
            f"open here), or rebuild on the cpu lane with `{rebuild_script} --full`."
            if embedder.lane_of(idx_model) == embedder.LANE_METAL
            else f"Rebuild with `{rebuild_script} --full`.")
        raise RuntimeError(
            f"{what} was built with '{idx_model}' but the active embedder profile is "
            f"'{active}' - results would come from a different vector "
            f"space. {lever}")


def _rank_candidates(dense_scores: np.ndarray, n_passages: int,
                     k: int) -> tuple[np.ndarray, np.ndarray, dict]:
    """Return (candidate order, aligned scores, response metadata) - dense cosine only."""
    dense = np.asarray(dense_scores, dtype=np.float32).reshape(-1)
    if len(dense) != n_passages:
        raise ValueError(f"candidate/score mismatch: {n_passages} vs {len(dense)}")
    if not np.all(np.isfinite(dense)):
        raise ValueError("dense retrieval returned non-finite scores")
    meta = {"score_kind": DENSE_SCORE_KIND}
    if not n_passages:
        return np.empty(0, dtype=np.int64), dense, meta
    return _top_indices(dense, min(k, len(dense))), dense, meta


def _envelope(results: list[dict], candidate_sessions: int, meta: dict) -> dict:
    return {
        "results": results,
        "candidate_sessions": candidate_sessions,
        "truncated": candidate_sessions > len(results),
        **meta,
    }




def _attach_semantic_integrity(payload: dict, refs) -> dict:
    take = getattr(refs, "take_integrity_disclosure", None)
    disclosure = take() if callable(take) else None
    if disclosure is not None:
        payload["semantic_integrity"] = disclosure
    return payload


def _mark_result(row: dict, meta: dict) -> dict:
    row["score_kind"] = meta["score_kind"]
    return row


def models_loaded() -> bool:
    return embedder.model_loaded()


def tool_search_chats(query: str, k: int = 5, filters: dict | None = None,
                      envelope: bool = False) -> str:
    k = max(1, min(int(k), SEMANTIC_MAX_RESULTS))
    dim, idx_model = common.read_index_meta(common.DATA_DIR / "summary_emb.meta")
    _guard_embedder(idx_model, "summary index",
                    "re-run the enrichment tool that built it")
    qv = _embed_query(query, idx_model)
    ids, mat, recs = _summary_artifacts(dim)
    sims = mat @ qv
    row_filters = _metadata_filters(filters)
    caller = str((row_filters or {}).get("exclude_session") or "")
    excluded = frozenset()
    if (caller and (row_filters or {}).get("exclude_family", True)
            and (row_filters or {}).get("exclude_session_from_turn") is None):
        family = common.indexed_calling_family(caller)
        if family is None:
            raise RuntimeError("session-family index unavailable for chat exclusion")
        excluded = family[1]
    eligible = np.asarray([i for i, sid in enumerate(ids)
                           if sid not in excluded and recs.get(sid)
                           and _matches({**recs[sid], "session": sid}, row_filters)],
                          dtype=np.int64)
    if len(eligible) and _family_diversity_enabled(filters):
        family_keep = _family_representatives(
            [ids[int(i)] for i in eligible], sims[eligible])
        eligible = eligible[family_keep]
    pool_n = min(SEMANTIC_MAX_POOL, max(k * 6, 30), len(eligible))
    local = _top_indices(sims[eligible], pool_n)
    pool = eligible[local]
    cand = [(ids[int(i)], recs[ids[int(i)]]) for i in pool]
    dense = sims[pool] if len(pool) else np.empty(0, dtype=np.float32)
    order, scores, rank_meta = _rank_candidates(dense, len(cand), k)
    import explore
    sc = explore._session_concept()
    try:
        model_by_session = explore.session_models(
            [cand[int(index)][0] for index in order])
    except (OSError, UnicodeError):
        model_by_session = {}
    out = []
    for i in order:
        session = cand[i][0]
        out.append(_mark_result(
            {"session": session, "concept": sc.get(session, ""),
             "project": cand[i][1].get("cwd_project", ""),
             "agent": cand[i][1].get("agent", ""),
             "model": model_by_session.get(session, ""),
             "score": round(float(scores[i]), 6),
             "title": cand[i][1].get("title", ""),
             "summary": cand[i][1].get("summary", ""),
             "tags": cand[i][1].get("tags", []), "n_msgs": cand[i][1].get("n_msgs", 0)},
            rank_meta))
    payload = _envelope(out, len(eligible), rank_meta)
    return json.dumps(payload if envelope else out)


def tool_search_messages(query: str, k: int = 5, filters: dict | None = None,
                         group_session: bool = False, envelope: bool = False) -> str:
    k = max(1, min(int(k), SEMANTIC_MAX_RESULTS))
    meta_path = common.EMBEDDINGS_PATH.parent / "embeddings.meta"
    dim, idx_model = common.read_index_meta(meta_path)
    _guard_embedder(idx_model, "message index", "embed.py")
    fast_coverage = None
    fast_refs = None
    fast_query = None
    fast_pool = None
    accelerator = None
    try:
        import semantic_q8
        fast_coverage = _require_current_message_index()
        generation = str(_CURRENT_MESSAGE_STATE.get("generation") or "")
        fast_refs = _message_refs_from_pointer(fast_coverage)
        if fast_refs is not None and semantic_q8.artifact_available(generation):
            accelerator = semantic_q8.accelerator_coverage(generation)
            fast_query = _embed_query(query, idx_model)
            fast_pool = (_q8_grouped_pool(
                fast_query, fast_refs, filters, k) if group_session else
                _q8_flat_pool(fast_query, fast_refs, filters, k))
    except (OSError, RuntimeError, ValueError, TypeError,
            json.JSONDecodeError, sqlite3.DatabaseError):
        fast_pool = None
    if fast_pool is not None:
        cand, dense, candidate_count = fast_pool
        order, scores, rank_meta = _rank_candidates(dense, len(cand), k)
        out = [_mark_result(
            {"session": cand[i].get("session", ""),
             "project": cand[i].get("project", ""),
             "agent": cand[i].get("agent", ""),
             "who": cand[i].get("who", "user"),
             "model": cand[i].get("model", ""),
             "ts": cand[i].get("ts", 0), "turn": cand[i].get("turn"),
             "score": round(float(scores[i]), 6),
             "content_digest": _result_content_digest(cand[i]),
             "text": " ".join(cand[i].get("text", "").split())[:240]},
            rank_meta)
            for i in order]
        payload = _envelope(out, candidate_count, rank_meta)
        payload["semantic_coverage"] = fast_coverage
        if accelerator is not None:
            payload["semantic_accelerator_coverage"] = accelerator
        payload["partial"] = (not bool(fast_coverage.get("complete"))
                              or bool(accelerator
                                      and not accelerator.get("complete")))
        _attach_semantic_integrity(payload, fast_refs)
        return json.dumps(payload if envelope else out)
    _refuse_large_dense_fallback(fast_refs, fast_coverage)
    # Never initialize ONNX just to learn the refs sidecar isn't ready; managed
    # search catches that lightweight signal and falls back to keyword results.
    ids, mat, refs, coverage = _message_artifacts(
        dim, meta_path, allow_refs_build=False)
    qv = fast_query if fast_query is not None else _embed_query(query, idx_model)
    sims = mat @ qv
    q8_shadow = _q8_shadow_comparison(qv, mat, sims)
    row_filters = _message_filters(refs, filters)

    # Session grouping is a retrieval contract, not a client-side cleanup: it must
    # guarantee N distinct sessions without an arbitrary "scan 4*N and hope" depth.
    if group_session:
        best = refs.best_by_session(sims, row_filters)
        sessions = list(best)
        reps = np.asarray([best[session] for session in sessions], dtype=np.int64)
        if len(reps) and _family_diversity_enabled(filters):
            family_keep = _family_representatives(sessions, sims[reps])
            reps = reps[family_keep]
        pool_n = min(SEMANTIC_MAX_POOL, max(k * 6, 40), len(reps))
        ordered = reps[_top_indices(sims[reps], min(pool_n, len(reps)))]
        cand = refs.resolve(ordered)
        ordered = np.asarray(
            [row.get("ordinal", ordered[index])
             for index, row in enumerate(cand)], dtype=np.int64)
        candidate_count = len(reps)
    else:
        eligible = refs.eligible(row_filters)
        pool_n = min(SEMANTIC_MAX_POOL, max(k * 6, 40), len(eligible))
        local = _top_indices(sims[eligible], pool_n)
        ordered = eligible[local]
        resolved = refs.resolve(ordered)
        # Score-descending resolution: a row's first surviving ordinal
        # max-pools its '#cN' chunk vectors into one logical result.
        seen_mid, seen_text, cand, kept = set(), set(), [], []
        for resolved_index, m in enumerate(resolved):
            mid = str(m.get("mid") or "")
            if mid and mid in seen_mid:
                continue
            key = " ".join(m["text"].split())[:140].lower()
            if key in seen_text:
                continue
            if mid:
                seen_mid.add(mid)
            seen_text.add(key)
            cand.append(m)
            kept.append(int(m.get("ordinal", ordered[resolved_index])))
        ordered = np.asarray(kept, dtype=np.int64)
        candidate_count = len(eligible)
    dense = sims[ordered] if len(ordered) else np.empty(0, dtype=np.float32)
    order, scores, rank_meta = _rank_candidates(dense, len(cand), k)
    out = [_mark_result(
        {"session": cand[i].get("session", ""),
         "project": cand[i].get("project", ""), "agent": cand[i].get("agent", ""),
         "who": cand[i].get("who", "user"), "model": cand[i].get("model", ""),
         "ts": cand[i].get("ts", 0), "turn": cand[i].get("turn"),
         "score": round(float(scores[i]), 6),
         "content_digest": _result_content_digest(cand[i]),
         "text": " ".join(cand[i].get("text", "").split())[:240]}, rank_meta)
        for i in order]
    payload = _envelope(out, candidate_count, rank_meta)
    payload["semantic_coverage"] = coverage
    payload["partial"] = not bool(coverage.get("complete"))
    if q8_shadow is not None:
        payload["_semantic_q8_shadow"] = q8_shadow
    _attach_semantic_integrity(payload, refs)
    return json.dumps(payload if envelope else out)


def _tool_search_hybrid_q8(query: str, k: int, filters: dict | None,
                           timer: _SemanticTimer, dim: int, idx_model: str | None):
    try:
        import semantic_q8
        coverage = _require_current_message_index()
        generation = str(_CURRENT_MESSAGE_STATE.get("generation") or "")
        if not semantic_q8.artifact_available(generation):
            return None
        accelerator = semantic_q8.accelerator_coverage(generation)
        if accelerator is None:
            return None
        refs = _message_refs_from_pointer(coverage)
        if refs is None:
            return None
        timer.mark("artifacts")
        qv = _embed_query(query, idx_model)
        timer.mark("embed")
        if _session_grouping_required(filters):
            pooled = _q8_session_pool(qv, refs, filters, k)
        else:
            pooled = _q8_grouped_pool(qv, refs, filters, k)
        if pooled is None:
            return None
        rows, dense, candidate_sessions = pooled
        timer.mark("q8_retrieval")
    except (OSError, RuntimeError, ValueError, TypeError,
            json.JSONDecodeError, sqlite3.DatabaseError):
        return None

    summary_recs: dict[str, dict] = {}
    try:
        smeta = common.DATA_DIR / "summary_emb.meta"
        sdim, smodel = common.read_index_meta(smeta)
        if sdim == dim and smodel == idx_model:
            _sids, _smat, summary_recs = _summary_artifacts(sdim)
    except (OSError, RuntimeError, TypeError, ValueError, KeyError,
            json.JSONDecodeError):
        summary_recs = {}
    timer.mark("summary_artifacts")

    order, scores, rank_meta = _rank_candidates(dense, len(rows), k)
    timer.mark("rank")
    import explore
    concepts = explore._session_concept()
    out = []
    for i in order:
        row = rows[int(i)]
        session = row.get("session", "")
        summ = summary_recs.get(session) or {}
        out.append(_mark_result({
            "session": session,
            "project": row.get("project", ""), "agent": row.get("agent", ""),
            "who": row.get("who", "user"), "model": row.get("model", ""),
            "ts": row.get("ts", 0), "turn": row.get("turn"),
            "score": round(float(scores[int(i)]), 6),
            "content_digest": _result_content_digest(row),
            "text": " ".join((row.get("text") or "").split())[:240],
            "title": summ.get("title", ""), "summary": summ.get("summary", ""),
            "concept": concepts.get(session, ""),
            "semantic_source": "message",
        }, rank_meta))
    payload = _envelope(out, candidate_sessions, rank_meta)
    payload["semantic_coverage"] = coverage
    payload["semantic_accelerator_coverage"] = accelerator
    payload["partial"] = (not bool(coverage.get("complete"))
                          or not bool(accelerator.get("complete")))
    _attach_semantic_integrity(payload, refs)
    timer.mark("enrichment")
    return timer.dumps(payload)


def tool_search_hybrid(query: str, k: int = 5, filters: dict | None = None,
                       *, timing: bool | None = None) -> str:
    """Session-level semantic search over complete message coverage plus summaries.

    Every score and turn comes from the same message embedding. Optional summaries
    enrich display metadata but cannot lend their confidence to an unrelated turn.
    """
    timer = _SemanticTimer(timing)
    k = max(1, min(int(k), SEMANTIC_MAX_RESULTS))
    msg_meta_path = common.EMBEDDINGS_PATH.parent / "embeddings.meta"
    dim, idx_model = common.read_index_meta(msg_meta_path)
    _guard_embedder(idx_model, "message index", "embed.py")
    timer.mark("metadata")
    fast = _tool_search_hybrid_q8(
        query, k, filters, timer, dim, idx_model)
    if fast is not None:
        return fast
    fallback_coverage = None
    fallback_refs = None
    try:
        fallback_coverage = _require_current_message_index()
        fallback_refs = _message_refs_from_pointer(fallback_coverage)
    except (OSError, RuntimeError, ValueError, TypeError,
            json.JSONDecodeError, sqlite3.DatabaseError):
        pass
    _refuse_large_dense_fallback(fallback_refs, fallback_coverage)
    ids, mat, refs, coverage = _message_artifacts(
        dim, msg_meta_path, allow_refs_build=False)
    timer.mark("artifacts")
    qv = _embed_query(query, idx_model)
    timer.mark("embed")
    sims = mat @ qv
    timer.mark("matmul")
    q8_shadow = _q8_shadow_comparison(qv, mat, sims)
    if q8_shadow is not None:
        timer.mark("q8_shadow")
    row_filters = _message_filters(refs, filters)

    best = refs.best_by_session(sims, row_filters)
    timer.mark("best_by_session")
    if not best:
        _, _, rank_meta = _rank_candidates(np.empty(0, dtype=np.float32), 0, k)
        payload = _envelope([], 0, rank_meta)
        payload["semantic_coverage"] = coverage
        payload["partial"] = not bool(coverage.get("complete"))
        if q8_shadow is not None:
            payload["_semantic_q8_shadow"] = q8_shadow
        _attach_semantic_integrity(payload, refs)
        timer.mark("rank")
        return timer.dumps(payload)

    # Summary records are optional display enrichment. Model identity still has to
    # match, but their session-level vectors never score a specific transcript turn.
    summary_recs: dict[str, dict] = {}
    # A summary spans every speaker and timestamp in a session. Letting it rank a
    # query constrained to --who/--model/a time range would reintroduce evidence the
    # filter explicitly removed. Session-level agent/project/chat filters are safe.
    allow_summary = not row_filters or not any(
        row_filters.get(key) not in (None, "")
        for key in ("who", "model", "since_ms", "until_ms"))
    if allow_summary:
        try:
            smeta = common.DATA_DIR / "summary_emb.meta"
            sdim, smodel = common.read_index_meta(smeta)
            # Width equality is not vector-space compatibility: model ids must
            # agree (even both legacy/unknown), else the metadata may be stale.
            if sdim == dim and smodel == idx_model:
                _sids, _smat, summary_recs = _summary_artifacts(sdim)
        except (OSError, RuntimeError, TypeError, ValueError, KeyError,
                json.JSONDecodeError):
            summary_recs = {}
    timer.mark("summary_artifacts")

    sessions = list(best)
    retrieval = np.asarray(
        [float(sims[best[session]]) for session in sessions], dtype=np.float32)
    if _family_diversity_enabled(filters):
        family_keep = _family_representatives(sessions, retrieval)
        sessions = [sessions[int(i)] for i in family_keep]
        retrieval = retrieval[family_keep]
    timer.mark("family")
    pool_n = min(SEMANTIC_MAX_POOL, max(k * 6, 40), len(sessions))
    pool = _top_indices(retrieval, pool_n)
    requested_refs = [best[sessions[int(p)]] for p in pool]
    pool_rows = refs.resolve(requested_refs)
    pool_by_ref = {
        int(row_ref): int(position)
        for position, row_ref in zip(pool, requested_refs)}
    timer.mark("resolve")
    cand, resolved_pool = [], []
    for resolved_index, row in enumerate(pool_rows):
        row_ref = int(row.get("ordinal", requested_refs[resolved_index]))
        p = pool_by_ref.get(row_ref)
        if p is None:
            continue
        session = sessions[int(p)]
        summ = summary_recs.get(session) or {}
        cand.append((session, row, summ))
        resolved_pool.append(p)

    dense = (retrieval[np.asarray(resolved_pool, dtype=np.int64)]
             if resolved_pool else np.empty(0, dtype=np.float32))
    order, scores, rank_meta = _rank_candidates(dense, len(cand), k)
    timer.mark("rank")
    import explore
    concepts = explore._session_concept()
    out = []
    for i in order:
        session, row, summ = cand[int(i)]
        out.append(_mark_result({
            "session": session,
            "project": row.get("project", ""), "agent": row.get("agent", ""),
            "who": row.get("who", "user"), "model": row.get("model", ""),
            "ts": row.get("ts", 0), "turn": row.get("turn"),
            "score": round(float(scores[int(i)]), 6),
            "content_digest": _result_content_digest(row),
            "text": " ".join((row.get("text") or "").split())[:240],
            "title": summ.get("title", ""), "summary": summ.get("summary", ""),
            "concept": concepts.get(session, ""),
            "semantic_source": "message",
        }, rank_meta))
    payload = _envelope(out, len(sessions), rank_meta)
    payload["semantic_coverage"] = coverage
    payload["partial"] = not bool(coverage.get("complete"))
    if q8_shadow is not None:
        payload["_semantic_q8_shadow"] = q8_shadow
    _attach_semantic_integrity(payload, refs)
    timer.mark("enrichment")
    return timer.dumps(payload)


def main() -> int:
    common.log(
        "ask.py is an internal semantic worker module; use `agrep search -s <query>` "
        "or `agrep recall <query> -s` so freshness and model ownership are enforced")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
