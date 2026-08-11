"""Embed transcript rows into generation-bound immutable semantic segments.

The model is embedder.py's pinned ONNX profile. Incremental passes retain unchanged
vectors, shadow removed or replaced rows, and embed only new or text-changed rows.
Per-row hashes detect an id that survives while its source text changes. A profile
identity change rebuilds the index so queries cannot cross vector spaces.

The indexer's `_refresh_embeddings` delegates freshness to
`semantic.ensure_fresh_async`, which launches this bounded worker; the CLI does not
require a manual embed step.

--background passes yield to the machine: low battery (pmset on macOS,
GetSystemPowerStatus on Windows) or a busy CPU (loadavg, so posix-only) defers the
run (exit 0; the next pass resumes by content hash). AGREP_EMBED_ANYWAY=1 forces it.
Indexd may allow one bounded stale bootstrap on battery; memory and load still gate it.

Usage: python embed.py [--smoke 8] [--full] [--max-new N] [--background]
"""

from __future__ import annotations

import argparse
import heapq
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from collections import ChainMap, OrderedDict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import NamedTuple

import numpy as np

import common
import embedder
import indexd_runtime
import ownerfile
import removal_fence
import semantic
import surface_policy as surface

REPLIES_PATH = common.DATA_DIR / "replies.jsonl"
# Per-id text fingerprint, one line per embeddings.ids row, in the same order.
HASHES_PATH = common.EMBEDDINGS_PATH.with_suffix(".hashes")
_CLAIM_HANDLE: ownerfile.Handle | None = None
_MALFORMED_CLAIM_STALE_S = 30.0
_BACKGROUND_CHUNK = max(1, int(os.environ.get("AGREP_SEM_CHUNK", "500")))
_FOREGROUND_CHUNK = max(_BACKGROUND_CHUNK, 5000)
# Bootstrap pass: small chunks + a wall deadline bound first-search latency;
# the partial-publish path keeps whatever finished.
_BOOTSTRAP_CHUNK = max(1, int(os.environ.get("AGREP_SEM_CHUNK", "64")))
_BOOTSTRAP_DEADLINE_S = float(os.environ.get("AGREP_SEM_BOOTSTRAP_DEADLINE_S", "4.5"))
_Q8_REQUIRED_ROWS = 100_000
GOVERNOR_BATTERY_FLOOR_PCT = surface.SEMANTIC_GOVERNOR.battery_floor_pct
GOVERNOR_MEMORY_FLOOR = surface.SEMANTIC_GOVERNOR.memory_floor
GOVERNOR_LOAD_LIMIT = surface.SEMANTIC_GOVERNOR.load_limit_per_core
_PENDING_PLAN_SCHEMA = "2"
_PENDING_PLAN_BUFFER = 4096


_ACTIVE_LANE: dict[tuple[str, str | None], str] = {}

# --full only: the one sanctioned lane move. A full rebuild replaces the
# whole vector space, so the machine default decides instead of the store;
# every incremental pass still conforms.
_FRESH_LANE = False


def _active_lane() -> str:
    """The embedding lane this pass reads and writes.

    Taken from what the message store already records, so an incremental pass
    conforms to the lane that wrote the existing rows instead of re-deciding
    from the environment: AGREP_MLX=on over a CPU store adds CPU rows, and a
    Metal store keeps getting Metal rows without it. When the store's lane
    cannot open here this resolves to CPU, which fails the identity comparisons
    below and prints the ordinary "embedder changed -> rebuilding" path rather
    than quietly appending rows from the other vector space.

    Memoized per (store, recorded identity) because the answer must not move
    mid-pass. A brand-new store has nothing to conform to and falls back to the
    machine default, which reads live load - re-asking it between batches is how
    a backfill ends up half in each lane. A --full rebuild is treated as a
    brand-new store (_FRESH_LANE): the owner asked for everything re-embedded,
    so the machine default decides, not the rows being thrown away.
    """
    recorded = _recorded_identity()
    key = (os.path.abspath(os.fspath(_meta_path())), recorded)
    if key not in _ACTIVE_LANE:
        _ACTIVE_LANE[key] = embedder.resolve_lane(recorded)
    return _ACTIVE_LANE[key]


def _meta_path():
    return common.EMBEDDINGS_PATH.parent / "embeddings.meta"


def _recorded_identity() -> str | None:
    """The store identity this pass conforms to, or None when nothing binds.

    None on a fresh store, under --full (_FRESH_LANE), and for an identity
    whose lane is unrecognizable - all three resolve to the machine default,
    and keeping them keyed identically lets _adopt_opened_lane rewrite the
    prediction in place.
    """
    if _FRESH_LANE:
        return None
    try:
        recorded = common.read_index_meta(_meta_path())[1]
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
    if recorded is not None and embedder.lane_of(recorded) is None:
        return None
    return recorded


def _lane_predicted() -> bool:
    """True when this pass's lane came from default_lane, not the store."""
    return _recorded_identity() is None


def _adopt_opened_lane(lane: str) -> None:
    """A default-lane prediction met reality: keep what actually opened.

    Only a predicted lane may be adopted. A store-recorded lane that cannot
    open must keep failing loudly rather than quietly rebuilding in the
    other vector space.
    """
    global _LANE_CHANGE
    _ACTIVE_LANE[(os.path.abspath(os.fspath(_meta_path())), None)] = lane
    # A pending plan built before the load is keyed to the predicted
    # identity; keeping it would make every later pass reject it and rescan.
    _drop_pending_plan()
    if _LANE_CHANGE is not None:
        if _LANE_CHANGE.get("from") == lane:
            _LANE_CHANGE = None
        else:
            _LANE_CHANGE = {"from": _LANE_CHANGE["from"], "to": lane}


def _active_model_id() -> str:
    """The ``embeddings.meta`` identity this process reads and writes."""
    return embedder.profile_string(_active_lane())


def _data_dir_readonly() -> bool:
    return _mutation_refusal_reason() is not None


def _mutation_refusal_info():
    if common.data_dir_readonly(common.DATA_DIR):
        return indexd_runtime.DerivedMutationInfo(
            "readonly", None,
            "AGREP_DATA_READONLY protects the data directory")
    return indexd_runtime.derived_writer_mutation_settled()


def _mutation_refusal_reason() -> str | None:
    info = _mutation_refusal_info()
    return None if info.writable else info.reason


def _require_writable_data_dir(action: str) -> None:
    info = _mutation_refusal_info()
    if info.writable:
        return
    if info.journal_blocked:
        raise indexd_runtime.DerivedWriteContended(
            f"{info.reason}; cannot {action}")
    raise PermissionError(f"{info.reason}; cannot {action}")


@dataclass(frozen=True)
class _PendingVectors:
    messages: list[common.Message]
    hashes: list[str]
    parts: list[np.ndarray]
    load_s: float
    inference_s: float


@dataclass
class _RefreshPlan:
    source: dict | None
    replacement_generation: str | None
    manifest: dict | None
    incremental: bool
    cached_plan: dict | None
    cur_hash: Mapping[str, str] | None
    messages: list[common.Message]
    selected_hashes: list[str]
    pending_count: int
    total_rows: int
    manifest_output: str
    pending_plan_active: bool
    metadata_only_ids: object


@dataclass
class _RetentionPlan:
    kept_ids: list[str]
    kept_hashes: list[str]
    kept_count: int
    kept_metadata: Mapping[str, str | None]
    kept_matrix: np.ndarray
    retained_mapping: object | None
    append_segmented: bool
    shadow_refs: list[int]
    old_ids: list[str] | None


class _SegmentCatalogRow(NamedTuple):
    text_hash: str
    metadata_hash: str | None
    row_ref: int
    segment: int
    local_ord: int


@dataclass(frozen=True)
class _SegmentedRebaseDelta:
    total: int
    pending_messages: list[common.Message]
    shadow_refs: list[int]


class _SegmentCatalog(Mapping[str, _SegmentCatalogRow]):
    """Disk-backed live-row lookup; refresh memory must not scale with vector rows."""

    def __init__(self):
        database = sqlite3.connect("")
        try:
            database.executescript("""
                PRAGMA journal_mode=OFF;
                PRAGMA synchronous=OFF;
                PRAGMA temp_store=FILE;
                CREATE TABLE rows(
                    mid TEXT PRIMARY KEY, text_hash TEXT NOT NULL,
                    metadata_hash TEXT, row_ref INTEGER NOT NULL UNIQUE,
                    segment INTEGER NOT NULL, local_ord INTEGER NOT NULL
                ) WITHOUT ROWID;
                CREATE TABLE current(
                    mid TEXT PRIMARY KEY, text_hash TEXT NOT NULL,
                    metadata_hash TEXT,
                    metadata_only INTEGER NOT NULL
                ) WITHOUT ROWID;
                CREATE TABLE ignored_refs(
                    row_ref INTEGER PRIMARY KEY
                ) WITHOUT ROWID;
                CREATE TABLE expected_rows(
                    mid TEXT PRIMARY KEY, text_hash TEXT NOT NULL,
                    metadata_hash TEXT
                ) WITHOUT ROWID;
            """)
        except BaseException:
            database.close()
            raise
        self.db = database
        self.buffer: list[tuple] = []
        self.current_buffer: list[tuple] = []
        self.current_ids: set[str] = set()
        self.count = 0
        self.closed = False
        self._cached: tuple[str, _SegmentCatalogRow] | None = None

    def add(self, mid: str, row: _SegmentCatalogRow) -> None:
        self.buffer.append((mid, *row))
        if len(self.buffer) >= 4096:
            self._flush()

    def _flush(self) -> None:
        if not self.buffer:
            return
        try:
            self.db.executemany("INSERT INTO rows VALUES(?,?,?,?,?,?)", self.buffer)
        except sqlite3.IntegrityError as exc:
            raise RuntimeError("duplicate live embedding id or row reference") from exc
        self.count += len(self.buffer)
        self.buffer.clear()

    def finish(self) -> None:
        self._flush()
        self.db.commit()

    def begin_current(self) -> None:
        self.current_buffer.clear()
        self.current_ids.clear()
        self.db.execute("DELETE FROM current")

    def add_current(
            self, mid: str, text_hash: str, metadata_hash: str | None,
            metadata_only: bool) -> None:
        if mid in self.current_ids:
            raise RuntimeError(f"duplicate semantic source id: {mid}")
        self.current_ids.add(mid)
        self.current_buffer.append(
            (mid, text_hash, metadata_hash, int(metadata_only)))
        if len(self.current_buffer) >= 4096:
            self._flush_current()

    def _flush_current(self) -> None:
        if not self.current_buffer:
            return
        try:
            self.db.executemany(
                "INSERT INTO current VALUES(?,?,?,?)", self.current_buffer)
        except sqlite3.IntegrityError as exc:
            raise RuntimeError("duplicate semantic source id") from exc
        self.current_buffer.clear()
        self.current_ids.clear()

    def finish_current(self) -> None:
        self._flush_current()
        self.db.commit()

    def current_hashes(self):
        return _CatalogCurrentHashes(self)

    def metadata_only_ids(self):
        return _CatalogMetadataOnlyIds(self)

    def covered_mismatch_count(self) -> int:
        return self.binding_mismatch_count(())

    def binding_mismatch_count(self, ignored_refs: Sequence[int]) -> int:
        self.finish()
        self.finish_current()
        self.db.execute("DELETE FROM ignored_refs")
        self.db.executemany(
            "INSERT INTO ignored_refs VALUES(?)",
            ((int(row_ref),) for row_ref in ignored_refs))
        return int(self.db.execute("""
            SELECT count(*) FROM rows AS r
            LEFT JOIN current AS c ON c.mid=r.mid
            LEFT JOIN ignored_refs AS i ON i.row_ref=r.row_ref
            WHERE i.row_ref IS NULL AND (
                c.mid IS NULL OR c.text_hash<>r.text_hash OR c.metadata_only<>0
            )
        """).fetchone()[0])

    def expected_mismatch_count(
            self, ids: Sequence[str], hashes: Sequence[str],
            metadata: Mapping[str, str | None] | None) -> int:
        if len(ids) != len(hashes):
            return max(len(ids), len(hashes), 1)
        self.finish_current()
        self.db.execute("DELETE FROM expected_rows")
        self.db.executemany(
            "INSERT INTO expected_rows VALUES(?,?,?)",
            ((mid, digest, metadata.get(mid) if metadata is not None else None)
             for mid, digest in zip(ids, hashes, strict=True)))
        return int(self.db.execute("""
            SELECT count(*) FROM expected_rows AS e
            LEFT JOIN current AS c ON c.mid=e.mid
            WHERE c.mid IS NULL OR c.text_hash<>e.text_hash
               OR e.metadata_hash IS NOT NULL
                  AND c.metadata_hash IS NOT e.metadata_hash
        """).fetchone()[0])

    def current_count(self) -> int:
        self.finish_current()
        return int(self.db.execute("SELECT count(*) FROM current").fetchone()[0])

    def current_rows(self):
        self.finish()
        self.finish_current()
        rows = self.db.execute("""
            SELECT r.mid,r.text_hash,r.metadata_hash,r.row_ref,
                   r.segment,r.local_ord,c.text_hash,c.metadata_only
            FROM rows AS r LEFT JOIN current AS c ON c.mid=r.mid
            ORDER BY r.row_ref
        """)
        for raw in rows:
            yield (
                str(raw[0]),
                _SegmentCatalogRow(
                    str(raw[1]),
                    str(raw[2]) if raw[2] is not None else None,
                    int(raw[3]), int(raw[4]), int(raw[5])),
                str(raw[6]) if raw[6] is not None else None,
                bool(raw[7]) if raw[7] is not None else False,
            )

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.db.close()
        self.buffer.clear()
        self.current_buffer.clear()
        self.current_ids.clear()
        self._cached = None

    def __del__(self):
        try:
            self.close()
        except (AttributeError, sqlite3.Error):
            pass

    def __len__(self) -> int:
        return self.count + len(self.buffer)

    def __iter__(self):
        self.finish()
        return (str(row[0]) for row in self.db.execute(
            "SELECT mid FROM rows ORDER BY row_ref"))

    def __getitem__(self, mid: str) -> _SegmentCatalogRow:
        if self._cached is not None and self._cached[0] == mid:
            return self._cached[1]
        self.finish()
        raw = self.db.execute(
            "SELECT text_hash,metadata_hash,row_ref,segment,local_ord "
            "FROM rows WHERE mid=?", (mid,)).fetchone()
        if raw is None:
            raise KeyError(mid)
        row = _SegmentCatalogRow(
            str(raw[0]), str(raw[1]) if raw[1] is not None else None,
            int(raw[2]), int(raw[3]), int(raw[4]))
        self._cached = (mid, row)
        return row

    def items(self):
        self.finish()
        for raw in self.db.execute(
                "SELECT mid,text_hash,metadata_hash,row_ref,segment,local_ord "
                "FROM rows ORDER BY row_ref"):
            yield str(raw[0]), _SegmentCatalogRow(
                str(raw[1]), str(raw[2]) if raw[2] is not None else None,
                int(raw[3]), int(raw[4]), int(raw[5]))


class _CatalogCurrentHashes(Mapping[str, str]):
    def __init__(self, catalog: _SegmentCatalog):
        self.catalog = catalog

    def __len__(self) -> int:
        self.catalog.finish_current()
        return int(self.catalog.db.execute(
            "SELECT count(*) FROM current").fetchone()[0])

    def __iter__(self):
        self.catalog.finish_current()
        return (str(row[0]) for row in self.catalog.db.execute(
            "SELECT mid FROM current"))

    def __getitem__(self, mid: str) -> str:
        self.catalog.finish_current()
        row = self.catalog.db.execute(
            "SELECT text_hash FROM current WHERE mid=?", (mid,)).fetchone()
        if row is None:
            raise KeyError(mid)
        return str(row[0])


class _CatalogMetadataOnlyIds:
    def __init__(self, catalog: _SegmentCatalog):
        self.catalog = catalog

    def __contains__(self, mid: str) -> bool:
        self.catalog.finish_current()
        row = self.catalog.db.execute(
            "SELECT metadata_only FROM current WHERE mid=?", (mid,)).fetchone()
        return bool(row and row[0])

    def add(self, mid: str) -> None:
        self.catalog.finish_current()
        changed = self.catalog.db.execute(
            "UPDATE current SET metadata_only=1 WHERE mid=?", (mid,)).rowcount
        if changed != 1:
            raise RuntimeError("metadata-only semantic row was not scanned")


class _ReplyContext(NamedTuple):
    ts: int
    project: str
    model: str
    model_source: str


class _ReplyContextCatalog(Mapping[str, _ReplyContext]):
    def __init__(self):
        database = sqlite3.connect("")
        try:
            database.executescript("""
                PRAGMA journal_mode=OFF;
                PRAGMA synchronous=OFF;
                PRAGMA temp_store=FILE;
                CREATE TABLE contexts(
                    mid TEXT PRIMARY KEY, ts INTEGER NOT NULL,
                    project TEXT NOT NULL, model TEXT NOT NULL,
                    model_source TEXT NOT NULL
                ) WITHOUT ROWID;
            """)
        except BaseException:
            database.close()
            raise
        self.db = database
        self.buffer: list[tuple] = []
        self.count = 0
        self.closed = False

    def add(self, mid: str, value: _ReplyContext) -> None:
        self.buffer.append((mid, *value))
        if len(self.buffer) >= 4096:
            self._flush()

    def _flush(self) -> None:
        if not self.buffer:
            return
        try:
            self.db.executemany(
                "INSERT INTO contexts VALUES(?,?,?,?,?)", self.buffer)
        except sqlite3.IntegrityError as exc:
            raise RuntimeError("duplicate semantic source id") from exc
        self.count += len(self.buffer)
        self.buffer.clear()

    def finish(self) -> None:
        self._flush()
        self.db.commit()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.db.close()
        self.buffer.clear()

    def __len__(self) -> int:
        return self.count + len(self.buffer)

    def __iter__(self):
        self.finish()
        return (str(row[0]) for row in self.db.execute(
            "SELECT mid FROM contexts"))

    def __getitem__(self, mid: str) -> _ReplyContext:
        self.finish()
        row = self.db.execute(
            "SELECT ts,project,model,model_source FROM contexts WHERE mid=?",
            (mid,),
        ).fetchone()
        if row is None:
            raise KeyError(mid)
        return _ReplyContext(
            int(row[0]), str(row[1]), str(row[2]), str(row[3]))


class _FamilyRootResolver:
    def __init__(self):
        self.db = common._open_session_family_index()
        self.parents = None
        self.memo: dict[str, str] = {}
        if (self.db is None
                and ((common.DATA_DIR / "sessions.jsonl").exists()
                     or (common.DATA_DIR
                         / common.SESSION_FAMILY_META_FILE).exists())):
            self.parents = common.await_family_publication(
                common.strict_family_parent_map)
            if self.parents is None:
                raise RuntimeError("session-family publication is unavailable")
        self.cache: OrderedDict[str, tuple[str, bool]] = OrderedDict()

    def metadata(self, session: str) -> tuple[str, bool]:
        session = str(session or "")
        if not session:
            return "", False
        cached = self.cache.get(session)
        if cached is not None:
            self.cache.move_to_end(session)
            return cached
        if self.parents is not None:
            value = (
                common.family_root(session, self.parents, self.memo),
                session in self.parents,
            )
        elif self.db is None:
            value = session, False
        else:
            row = self.db.execute(
                "SELECT root,side FROM session_family WHERE session=?",
                (session,),
            ).fetchone()
            value = (
                str(row[0]) if row and row[0] else session,
                bool(row[1]) if row else False,
            )
        self.cache[session] = value
        if len(self.cache) > 4096:
            self.cache.popitem(last=False)
        return value

    def root(self, session: str) -> str:
        return self.metadata(session)[0]

    def close(self) -> None:
        if self.db is not None:
            self.db.close()
            self.db = None
        self.parents = None
        self.memo.clear()
        self.cache.clear()


def _reply_context(
        message: common.Message,
        string_pool: dict[str, str] | None = None,
) -> _ReplyContext:
    def compact(value: str) -> str:
        text = str(value or "")
        return text if string_pool is None else string_pool.setdefault(text, text)

    return _ReplyContext(
        ts=int(message.ts),
        project=compact(message.project),
        model=compact(message.model),
        model_source=compact(message.model_source or "unknown"),
    )


class _CatalogFieldView(Mapping):
    def __init__(self, catalog: Mapping[str, _SegmentCatalogRow], field: str):
        self.catalog = catalog
        self.field = field

    def __getitem__(self, mid: str):
        return getattr(self.catalog[mid], self.field)

    def __iter__(self):
        return iter(self.catalog)

    def __len__(self) -> int:
        return len(self.catalog)


def _close_manifest_catalog(manifest: dict | None) -> None:
    catalog = (manifest or {}).get("catalog")
    if isinstance(catalog, _SegmentCatalog):
        catalog.close()


class _ManifestCatalogOwner:
    def __init__(self):
        self.catalog: _SegmentCatalog | None = None

    def bind(self, manifest: dict | None) -> None:
        catalog = (manifest or {}).get("catalog")
        next_catalog = catalog if isinstance(catalog, _SegmentCatalog) else None
        if self.catalog is not None and self.catalog is not next_catalog:
            self.catalog.close()
        self.catalog = next_catalog

    def close(self) -> None:
        if self.catalog is not None:
            self.catalog.close()
            self.catalog = None


def _pending_plan_path():
    return common.DATA_DIR / "embeddings.pending.db"


def _drop_pending_plan() -> None:
    if _data_dir_readonly():
        return
    try:
        _pending_plan_path().unlink(missing_ok=True)
    except OSError:
        pass


def _generation_key(value: dict | None) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":")) if value else ""


class _PendingPlanBuilder:
    """Build a prose-free pending-row plan outside the live publication."""

    def __init__(self, source: dict | None, output: str, model: str):
        _require_writable_data_dir("build an embedding pending plan")
        path = _pending_plan_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        common._prune_embedding_temps(path)
        self.path = path
        self.tmp = common.embedding_temp_path(path, "pending_plan")
        self.db = sqlite3.connect(self.tmp)
        self.db.executescript("""
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE pending(
                mid TEXT PRIMARY KEY, text_hash TEXT NOT NULL,
                session TEXT NOT NULL, ts INTEGER NOT NULL, seq INTEGER NOT NULL
            ) WITHOUT ROWID;
            CREATE INDEX pending_order ON pending(ts DESC, seq);
            CREATE INDEX pending_session ON pending(session);
        """)
        self.source = _generation_key(source)
        self.output = str(output or "")
        self.model = str(model)
        self.buffer: list[tuple] = []
        self.pending = 0
        self.closed = False

    def add(self, message: common.Message, digest: str, seq: int) -> None:
        self.buffer.append((
            message.id, digest, str(message.session), int(message.ts), int(seq),
        ))
        self.pending += 1
        if len(self.buffer) >= _PENDING_PLAN_BUFFER:
            self._flush()

    def _flush(self) -> None:
        if not self.buffer:
            return
        self.db.executemany(
            "INSERT INTO pending VALUES(?,?,?,?,?)", self.buffer)
        self.buffer.clear()

    def publish(self, total: int) -> None:
        _require_writable_data_dir("publish an embedding pending plan")
        self._flush()
        rows = {
            "schema": _PENDING_PLAN_SCHEMA, "source": self.source,
            "output": self.output, "model": self.model,
            "total": str(int(total)), "pending": str(self.pending),
            "reusable": "0",
        }
        self.db.executemany("INSERT INTO meta VALUES(?,?)", rows.items())
        self.db.commit()
        self.db.close()
        self.closed = True
        common.replace_with_retry(self.tmp, self.path)

    def abort(self) -> None:
        if not self.closed:
            self.db.close()
            self.closed = True
        if not _data_dir_readonly():
            self.tmp.unlink(missing_ok=True)


def _plan_meta(db: sqlite3.Connection) -> dict[str, str]:
    return dict(db.execute("SELECT key,value FROM meta"))


def _resolve_pending_messages(rows: list[tuple]) -> list[common.Message] | None:
    """Resolve one bounded plan page through corpusdb's indexed session rows."""
    try:
        import corpusdb

        db = corpusdb.connect(quiet=True)
        if db is None:
            return None
        out: list[common.Message] = []
        try:
            for mid, expected_hash, _ts, _seq in rows:
                mid = str(mid)
                reply = mid.endswith("#r")
                base = mid[:-2] if reply else mid
                try:
                    agent, tail = base.split(":", 1)
                    session, turn_raw = tail.rsplit(":", 1)
                    turn = int(turn_raw)
                except (ValueError, TypeError):
                    return None
                if reply:
                    who_sql, who_args = "who = ?", ("agent",)
                else:
                    who_sql, who_args = "who <> ? AND who <> ?", ("agent", "tool")
                found = db.execute(
                    "SELECT ts,project,model,model_source,who,text FROM msgs "
                    "WHERE session=? AND turn=? AND agent=? AND " + who_sql + " LIMIT 2",
                    (session, int(turn), agent, *who_args),
                ).fetchall()
                if len(found) != 1:
                    return None
                ts, project, model, model_source, actual_who, text = found[0]
                text = str(text or "")
                if _text_hash(text) != expected_hash:
                    return None
                out.append(common.Message(
                    id=mid, agent=str(agent), project=str(project or ""),
                    session=str(session), ts=int(ts or 0), turn=int(turn),
                    text=text, who=str(actual_who or "user"), model=str(model or ""),
                    model_source=str(model_source or "unknown"),
                ))
        finally:
            db.close()
        return out
    except (OSError, RuntimeError, sqlite3.DatabaseError, ValueError, TypeError):
        return None


def _load_pending_plan(source: dict | None, output: str, model: str,
                       manifest_rows: int, max_new: int | None) -> dict | None:
    """Load one generation-bound page without walking transcript prose."""
    if max_new is None or max_new <= 0:
        return None
    if not _pending_plan_path().exists():
        return None
    try:
        db = sqlite3.connect(_pending_plan_path())
        try:
            meta = _plan_meta(db)
            pending = int(meta.get("pending", "-1"))
            total = int(meta.get("total", "-1"))
            valid = (
                meta.get("schema") == _PENDING_PLAN_SCHEMA
                and meta.get("source") == _generation_key(source)
                and meta.get("output") == str(output or "")
                and meta.get("model") == str(model)
                and meta.get("reusable") == "1"
                and pending > 0 and total == int(manifest_rows) + pending
            )
            if not valid:
                return None
            limit = min(int(max_new), pending)
            order = "seq" if pending <= int(max_new) else "ts DESC,seq"
            rows = db.execute(
                "SELECT mid,text_hash,ts,seq "
                f"FROM pending ORDER BY {order} LIMIT ?", (limit,)).fetchall()
        finally:
            db.close()
        if len(rows) != limit:
            return None
        messages = _resolve_pending_messages(rows)
        if messages is None or len(messages) != limit:
            return None
        return {
            "messages": messages, "hashes": [str(row[1]) for row in rows],
            "pending": pending, "total": total,
        }
    except (OSError, RuntimeError, sqlite3.DatabaseError, ValueError, TypeError):
        return None


def _advance_pending_plan(source: dict | None, old_output: str, new_output: str,
                          embedded_ids: list[str]) -> bool:
    """Advance the derived cursor only after the vector publication commits."""
    if _data_dir_readonly():
        return False
    if not embedded_ids:
        return False
    if not _pending_plan_path().exists():
        return False
    completed = False
    try:
        db = sqlite3.connect(_pending_plan_path())
        try:
            db.execute("BEGIN IMMEDIATE")
            meta = _plan_meta(db)
            if (meta.get("schema") != _PENDING_PLAN_SCHEMA
                    or meta.get("source") != _generation_key(source)
                    or meta.get("output") != str(old_output or "")):
                db.rollback()
                return False
            pending = int(meta.get("pending", "-1"))
            before = db.total_changes
            db.executemany("DELETE FROM pending WHERE mid=?",
                           ((mid,) for mid in embedded_ids))
            removed = db.total_changes - before
            if removed != len(embedded_ids) or pending < removed:
                db.rollback()
                return False
            remaining = pending - removed
            db.executemany(
                "UPDATE meta SET value=? WHERE key=?",
                ((str(remaining), "pending"),
                 (str(new_output or ""), "output"), ("1", "reusable")),
            )
            db.commit()
            completed = remaining == 0
        finally:
            db.close()
        if completed:
            _drop_pending_plan()
        return True
    except (OSError, RuntimeError, sqlite3.DatabaseError, ValueError, TypeError):
        return False


def _acquire_claim() -> bool:
    """One embedder per data dir, across every process that might spawn one
    (CLI escalation, the semantic worker, indexd). A live holder wins; a dead one is
    reclaimed. New claims bind ownership to a kernel process-birth identity,
    and release always requires the claim's own exact token. Returns False
    when someone else owns the run."""
    global _CLAIM_HANDLE
    if _data_dir_readonly():
        return False
    if _CLAIM_HANDLE is not None:
        return False
    if removal_fence.background_removal_active():
        return False
    path = semantic.embed_claim_path()
    process_start = common.process_start_identity(os.getpid())
    if process_start in (None, "", "None", "unknown"):
        return False
    token = uuid.uuid4().hex
    raw = json.dumps({
        "pid": os.getpid(),
        "process_start": process_start,
        "token": token,
        "started_at": time.time(),
    }).encode("utf-8")
    for _ in range(2):
        try:
            claimed = ownerfile.create_exclusive(path, raw)
            if removal_fence.background_removal_active():
                claimed.release(tombstone=True, require_stable_mtime=True)
                return False
            _CLAIM_HANDLE = claimed
            return True
        except FileExistsError:
            try:
                observed = ownerfile.snapshot(path)
                rec = json.loads(observed.raw.decode("utf-8"))
                if not isinstance(rec, dict):
                    raise TypeError("embed claim must be an object")
                pid = int(rec.get("pid") or 0)
                state = ownerfile.classify_process(
                    pid, rec.get("process_start"),
                    pid_alive=common.pid_alive,
                    process_start=common.process_start_identity)
                if state in (
                        ownerfile.ProcessOwner.EXACT_LIVE,
                        ownerfile.ProcessOwner.UNVERIFIABLE):
                    return False
                if ownerfile.remove_exact(path, observed, tombstone=True):
                    continue
            except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
                age = time.time() - observed.mtime
                if 0.0 <= age < _MALFORMED_CLAIM_STALE_S:
                    return False
                if ownerfile.remove_exact(path, observed, tombstone=True):
                    continue
            except OSError:
                continue
            return False
        except OSError:
            return False
    return False


def _release_claim() -> None:
    global _CLAIM_HANDLE
    handle = _CLAIM_HANDLE
    _CLAIM_HANDLE = None
    if handle is None:
        return
    if _data_dir_readonly():
        handle.close()
        return
    try:
        handle.release(tombstone=True)
    except OSError:
        pass


# failed passes before this one (set in main); rides the "running" state so a
# death without publishing still steps the bootstrap backoff, and stamps +1 on
# this pass's own failed states.
_PRIOR_FAILURES = 0


# The lane change behind a full re-embed; it must ride the state record or
# passes 2..n (ordinary increments) leave minutes of work unattributed.
_LANE_CHANGE: dict | None = None


def _note_lane_change(old_model: str, new_model: str) -> None:
    """Remember an identity change that is ONLY a lane change.

    A different model reads as a different embedder; a lane change keeps the
    same weights and rebuilds anyway, which is the one full re-embed whose
    reason is invisible to a reader watching coverage go partial.
    """
    global _LANE_CHANGE
    old_lane = embedder.lane_of(old_model)
    new_lane = embedder.lane_of(new_model)
    if old_lane and new_lane and old_lane != new_lane:
        _LANE_CHANGE = {"from": old_lane, "to": new_lane}


def _inherited_lane_change() -> dict | None:
    """A lane-change rebuild an earlier pass started and has not finished."""
    prior = semantic.read_embed_state()
    change = prior.get("lane_change")
    if not isinstance(change, dict) or prior.get("state") == "ready":
        return None
    return change


def _publish_state(state: dict) -> None:
    if _data_dir_readonly():
        return
    path = semantic.embed_state_path()
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    payload = {**state, "pid": os.getpid()}
    # Dropped exactly once, on the publish that completes coverage: "ready" is
    # the state that means the rebuild is over and the notice must stop.
    if _LANE_CHANGE is not None and payload.get("state") != "ready":
        payload.setdefault("lane_change", _LANE_CHANGE)
    try:
        tmp.write_text(json.dumps(payload, separators=(",", ":")),
                       encoding="utf-8")
        common.replace_with_retry(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)


_text_hash = common.semantic_text_hash


def _read_hashes(n_expected: int) -> "list[str] | None":
    """The per-id hash sidecar, or None when absent/misaligned.

    An unverified legacy row cannot safely receive a freshly invented text hash;
    callers rebuild that bundle in the active profile.
    """
    if not HASHES_PATH.exists():
        return None
    hs = [ln for ln in HASHES_PATH.read_text(encoding="utf-8").split("\n") if ln]
    return hs if len(hs) == n_expected else None


def _retained_embedding_rows(old_mat, keep: list[int] | None, *, has_pending: bool):
    """Return a zero-copy full retention or a compact copy for pruned rows.

    ``write_embeddings_parts`` consumes its inputs without copying, and the
    Windows reader maps a replace-safe snapshot, so the common all-rows-plus-small-
    delta case keeps that mmap alive through publication instead of allocating a
    private full-matrix copy. Callers must close the source mapping
    only after consuming a returned full-matrix view.
    """
    if keep is None:
        return old_mat
    if not keep:
        return np.zeros((0, old_mat.shape[1]), dtype=np.float32)
    all_rows = (len(keep) == old_mat.shape[0]
                and all(index == value for index, value in enumerate(keep)))
    return old_mat if all_rows else old_mat[keep]


def iter_reply_messages(
        base_rows: Mapping[str, object],
        wanted_ids: set[str] | None = None,
) -> Iterator[common.Message]:
    """Agent replies as embeddable Messages, id = 'agent:session:turn#r'. The '#r'
    suffix keeps a reply's vector distinct from the user turn's at the same base id
    AND keeps every existing user-turn id byte-stable, so adding replies is additive
    - it never invalidates an already-embedded user vector. replies.jsonl stores only
    {id, reply}; agent/session/turn come from the id, ts from the matching user turn."""
    if not REPLIES_PATH.exists():
        return
    # Replies can be larger than messages (full code/log blocks); stream the JSONL
    # rather than loading the whole file at once.
    with REPLIES_PATH.open("r", encoding="utf-8") as src:
        for line in src:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(o, dict):
                continue
            rid, reply = o.get("id"), o.get("reply")
            if (not isinstance(rid, str) or not rid
                    or not isinstance(reply, str) or not reply):
                continue
            try:
                agent, tail = rid.split(":", 1)
                session, raw_turn = tail.rsplit(":", 1)
                turn = int(raw_turn)
            except (ValueError, TypeError):
                continue
            if not agent or not session or turn < 0:
                continue
            reply_id = rid + "#r"
            if wanted_ids is not None and reply_id not in wanted_ids:
                continue
            base = base_rows.get(rid)
            if isinstance(base, (common.Message, _ReplyContext)):
                ts = int(base.ts)
                project = str(base.project or "")
                model = str(base.model or "")
                model_source = str(base.model_source or "unknown")
            else:
                ts = int(base or 0)
                project = ""
                model = ""
                model_source = "unknown"
            yield common.Message(
                id=reply_id, agent=agent, project=project,
                session=session, ts=ts, turn=turn,
                text=reply, who="agent", model=model, model_source=model_source,
            )


def _iter_source_messages() -> Iterator[common.Message]:
    """Stream users and replies while retaining only reply metadata for parent rows."""
    base_rows = _ReplyContextCatalog() if REPLIES_PATH.exists() else None
    try:
        for message in common.iter_messages(path=common.MESSAGES_PATH):
            if base_rows is not None:
                base_rows.add(message.id, _reply_context(message))
            yield message
        if base_rows is not None:
            base_rows.finish()
            yield from iter_reply_messages(base_rows)
    finally:
        if base_rows is not None:
            base_rows.close()


def _load_old_manifest(dim: int, *, metadata_only: bool = False) -> dict | None:
    """Read one coherent ids/hash/model manifest without mapping the matrix."""
    meta_path = common.EMBEDDINGS_PATH.parent / "embeddings.meta"
    try:
        raw = json.loads(meta_path.read_bytes())
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        raw = None
    if isinstance(raw, dict) and raw.get("version") == 2:
        try:
            import embedding_segments
            segmented = embedding_segments.load_manifest(meta_path)
            if int(segmented["model"]["dim"]) != int(dim):
                return None
            refs_versions = embedding_segments.refs_schema_versions(segmented)
            state = common.embedding_artifact_state(
                meta_path, common.EMBEDDINGS_PATH, common.IDS_PATH)
            if metadata_only:
                return {
                    "state": state, "ids": None, "hashes": None,
                    "row_refs": None, "rows": int(segmented["live_rows"]),
                    "model": str(segmented["model"]["id"]),
                    "segmented": True, "segments": segmented,
                    "refs_versions": refs_versions,
                    "metadata_only": True,
                }
            catalog = _SegmentCatalog()
            try:
                for row in embedding_segments.iter_active_rows(segmented):
                    catalog.add(str(row["mid"]), _SegmentCatalogRow(
                        text_hash=str(row["text_hash"]),
                        metadata_hash=(
                            str(row["metadata_hash"])
                            if row.get("model_source_stored")
                            and isinstance(row.get("metadata_hash"), str) else None
                        ),
                        row_ref=int(row["row_ref"]),
                        segment=int(row["segment"]),
                        local_ord=int(row["local_ord"]),
                    ))
                catalog.finish()
                if len(catalog) != int(segmented["live_rows"]):
                    raise RuntimeError(
                        "semantic catalog disagrees with the segmented manifest")
            except Exception:
                catalog.close()
                raise
            return {
                "state": state,
                "catalog": catalog,
                "model": str(segmented["model"]["id"]),
                "segmented": True, "segments": segmented,
                "refs_versions": refs_versions,
                "rows": len(catalog), "metadata_only": False,
            }
        except (OSError, RuntimeError, ValueError, TypeError,
                json.JSONDecodeError, sqlite3.DatabaseError):
            return None
    for _ in range(3):
        try:
            before = common.embedding_artifact_state(
                meta_path, common.EMBEDDINGS_PATH, common.IDS_PATH)
            old_dim, old_model = common.read_index_meta(meta_path)
            old_ids = [line for line in common.IDS_PATH.read_text(
                encoding="utf-8").split("\n") if line]
            old_hashes = _read_hashes(len(old_ids))
            after = common.embedding_artifact_state(
                meta_path, common.EMBEDDINGS_PATH, common.IDS_PATH)
        except Exception:  # corrupt/legacy publication gets a clean rebuild
            return None
        if before != after:
            continue
        if old_dim != dim or old_hashes is None or len(set(old_ids)) != len(old_ids):
            return None
        return {"state": before, "ids": old_ids, "hashes": old_hashes,
                "model": old_model, "segmented": False,
                "rows": len(old_ids), "metadata_only": False}
    raise RuntimeError("embedding publication kept moving while planning refresh")


def _segment_replacement_generation() -> str | None:
    """Snapshot the CAS token needed to replace an existing segmented base."""
    meta_path = common.EMBEDDINGS_PATH.parent / "embeddings.meta"
    try:
        record = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict) or record.get("version") != 2:
        return None
    generation = record.get("generation")
    if not isinstance(generation, str) or not generation:
        raise RuntimeError("segmented embedding manifest has no replacement generation")
    return generation


def _prune_legacy_embedding_layout() -> dict[str, int]:
    """Retire monolithic artifacts only after a coherent v2 generation is live."""
    if _data_dir_readonly():
        return {"removed": 0, "deferred": 0}
    import embedding_segments
    import semantic_q8
    semantic_q8.close_scanner()
    return embedding_segments.prune_legacy_layout(
        embeddings_path=common.EMBEDDINGS_PATH,
        ids_path=common.IDS_PATH,
        q8_manifest_path=semantic_q8.MANIFEST_PATH,
        q8_artifact_dir=semantic_q8.ARTIFACT_DIR,
        generation_marker_path=semantic.generation_marker_path(),
    )


def _scan_source(*, rebuild: bool, old_hash_by_id: Mapping[str, str] | None,
                 max_new: int | None,
                 plan_builder: _PendingPlanBuilder | None = None,
                 old_metadata_by_id: Mapping[str, str | None] | None = None,
                 metadata_only_ids: set[str] | None = None,
                 ) -> tuple[Mapping[str, str], list[common.Message], int]:
    """Stream current users+replies into a bounded pending plan.

    Every id/hash is retained for exact prune/change/coverage accounting. Message
    text survives only for pending rows selected by ``max_new``; a profile reset
    needs O(row metadata + selected text), not O(all transcript text).
    Equal timestamps preserve source order.
    """
    if max_new is not None and max_new <= 0:
        raise ValueError("--max-new must be positive")
    old_hash_by_id = old_hash_by_id or {}
    disk_catalog = (
        old_hash_by_id.catalog
        if isinstance(old_hash_by_id, _CatalogFieldView)
        and isinstance(old_hash_by_id.catalog, _SegmentCatalog)
        else None
    )
    if disk_catalog is not None:
        disk_catalog.begin_current()
        cur_hash: Mapping[str, str] = disk_catalog.current_hashes()
    else:
        cur_hash = {}
    pending_all: list[tuple[int, common.Message]] = []
    selected: list[tuple[int, int, int, common.Message]] = []
    pending_count = 0
    source_seq = 0
    if old_metadata_by_id is not None:
        import semantic_segment_build
        family_roots = _FamilyRootResolver()
    else:
        semantic_segment_build = None
        family_roots = None

    def observe(message: common.Message) -> None:
        nonlocal pending_count, source_seq
        seq = source_seq
        source_seq += 1
        if disk_catalog is None and message.id in cur_hash:
            raise RuntimeError(f"duplicate semantic source id: {message.id}")
        digest = _text_hash(message.text)
        metadata_digest = None
        if semantic_segment_build is not None:
            family_root, side = family_roots.metadata(message.session)
            family_label = semantic_segment_build._family_label_for_root(
                message, family_root)
            metadata_digest = semantic_segment_build.refs_metadata_fingerprint(
                message, family_label, side)
        text_same = old_hash_by_id.get(message.id) == digest
        metadata_same = (
            old_metadata_by_id is None
            or old_metadata_by_id.get(message.id) == metadata_digest
        )
        metadata_only = not rebuild and text_same and not metadata_same
        if disk_catalog is not None:
            disk_catalog.add_current(
                message.id, digest, metadata_digest, metadata_only)
        else:
            cur_hash[message.id] = digest
        if not rebuild and text_same and metadata_same:
            return
        if (metadata_only and metadata_only_ids is not None
                and disk_catalog is None):
            metadata_only_ids.add(message.id)
        pending_count += 1
        if plan_builder is not None:
            plan_builder.add(message, digest, seq)
        if max_new is None:
            pending_all.append((seq, message))
            return
        entry = (int(message.ts), -seq, seq, message)
        if len(selected) < max_new:
            heapq.heappush(selected, entry)
        elif entry[:2] > selected[0][:2]:
            heapq.heapreplace(selected, entry)

    try:
        for message in _iter_source_messages():
            observe(message)
        if disk_catalog is not None:
            disk_catalog.finish_current()
    finally:
        if family_roots is not None:
            family_roots.close()

    if max_new is None:
        pending = [message for _, message in pending_all]
    elif pending_count > max_new:
        pending = [entry[3] for entry in sorted(
            selected, key=lambda entry: (-entry[0], entry[2]))]
    else:
        pending = [entry[3] for entry in sorted(
            selected, key=lambda entry: entry[2])]
    return cur_hash, pending, pending_count


def _load_retained_rows(manifest: dict, keep: list[int] | None, *, has_pending: bool,
                        dim: int):
    if keep is not None and not keep:
        return np.zeros((0, dim), dtype=np.float32)
    old_mat = None
    try:
        loaded_ids, old_mat = common.read_embeddings(
            common.EMBEDDINGS_PATH, common.IDS_PATH, dim=dim,
            meta_path=common.EMBEDDINGS_PATH.parent / "embeddings.meta")
        if (loaded_ids != manifest["ids"]
                or common.embedding_matrix_identity(old_mat)
                != manifest["state"]["identity"]):
            raise RuntimeError("embedding publication moved after source planning")
        retained = _retained_embedding_rows(
            old_mat, keep, has_pending=has_pending)
        if retained is old_mat:
            # Publication consumes this mmap directly; the caller owns closing it.
            return retained, old_mat
        common.close_embedding_matrix(old_mat)
        old_mat = None
        return retained, None
    except Exception:
        if old_mat is not None:
            common.close_embedding_matrix(old_mat)
        raise


def _messages_for_ids(ids: list[str], hashes: list[str]) -> list[common.Message]:
    expected = dict(zip(ids, hashes, strict=True))
    wanted = set(ids)
    found: dict[str, common.Message] = {}
    reply_bases = {
        mid[:-2] for mid in wanted if mid.endswith("#r")
    }
    base_rows: dict[str, _ReplyContext] = {}
    for message in common.iter_messages(path=common.MESSAGES_PATH):
        if message.id in wanted:
            found[message.id] = message
        if message.id in reply_bases:
            base_rows[message.id] = _reply_context(message)
    for message in iter_reply_messages(base_rows, wanted):
        if message.id in wanted:
            found[message.id] = message
    if set(found) != wanted:
        raise RuntimeError("semantic segment source rows are missing")
    for mid, message in found.items():
        if _text_hash(message.text) != expected[mid]:
            raise RuntimeError(f"semantic segment source moved for {mid}")
    return [found[mid] for mid in ids]


def _segment_vectors_for_ids(manifest: dict, ids: list[str], dim: int) -> np.ndarray:
    """Copy selected immutable vectors for a refs-only replacement delta."""
    if not ids:
        return np.zeros((0, dim), dtype=np.float32)
    import embedding_segments

    catalog = manifest.get("catalog")
    if not isinstance(catalog, Mapping) or any(mid not in catalog for mid in ids):
        raise RuntimeError("metadata-only semantic row is no longer live")
    output = np.empty((len(ids), dim), dtype=np.float32)
    by_segment: dict[int, list[tuple[int, int]]] = {}
    for output_row, mid in enumerate(ids):
        row = catalog[mid]
        by_segment.setdefault(int(row.segment), []).append(
            (output_row, int(row.local_ord)))
    for segment_index, wanted in by_segment.items():
        segment = manifest["segments"]["segments"][segment_index]
        matrix = np.memmap(
            embedding_segments.artifact_path(
                manifest["segments"], segment["artifacts"]["f32"]),
            dtype="<f4", mode="r",
            shape=(int(segment["rows"]), int(dim)),
        )
        try:
            for output_row, local_ord in wanted:
                output[output_row] = matrix[local_ord]
        finally:
            common.close_embedding_matrix(matrix)
    return output


def _metadata_hashes_for_messages(messages) -> dict[str, str]:
    import semantic_segment_build

    parents = common.await_family_publication(common.strict_family_parent_map)
    if parents is None:
        raise RuntimeError("session-family publication is unavailable")
    memo: dict[str, str] = {}
    return {
        message.id: semantic_segment_build.message_metadata_fingerprint(
            message, parents, memo)
        for message in messages
    }


def _verified_messages(ids: list[str], hashes: list[str], messages):
    rows = list(messages)
    if ([message.id for message in rows] != ids
            or [_text_hash(message.text) for message in rows] != hashes):
        raise RuntimeError("semantic segment messages moved after planning")
    return rows


def _validated_source_binding(source: dict | None, indexed_ids: list[str],
                              indexed_hashes: list[str], total_rows: int,
                              indexed_metadata: Mapping[str, str | None] | None = None):
    if source is None or len(indexed_ids) != len(indexed_hashes):
        return None
    marker_source = semantic.source_generation()
    if marker_source == source:
        return marker_source, int(total_rows)
    expected = dict(zip(indexed_ids, indexed_hashes, strict=True))
    remaining = set(indexed_ids)
    seen: set[str] = set()
    current_total = 0
    changed = None
    if indexed_metadata is not None:
        import semantic_segment_build
        # a torn concurrent republish must not void this completed ONNX pass
        parents = common.await_family_publication(common.strict_family_parent_map)
        if parents is None:
            return None
        memo: dict[str, str] = {}
    else:
        semantic_segment_build = None
        parents = {}
        memo = {}

    def validate(message: common.Message) -> None:
        nonlocal current_total, changed
        if message.id in seen:
            changed = message.id
            return
        seen.add(message.id)
        current_total += 1
        if (message.id in remaining
                and _text_hash(message.text) != expected.get(message.id)):
            changed = message.id
        if (message.id in remaining and semantic_segment_build is not None
                and semantic_segment_build.message_metadata_fingerprint(
                    message, parents, memo) != indexed_metadata.get(message.id)):
            changed = message.id
        remaining.discard(message.id)

    for message in _iter_source_messages():
        validate(message)
    if semantic.source_generation() != marker_source:
        common.log("transcript kept moving during embedding validation; next pass retries.")
        return None
    if changed is not None or remaining:
        common.log("a covered transcript row moved during embedding; next pass re-embeds it.")
        return None
    common.log("transcript advanced during embedding; rebased unchanged semantic rows.")
    return marker_source, current_total


def _mapping_field(holder: object, name: str) -> Mapping:
    """One level of a manifest, or an empty mapping when the shape is foreign.

    A manifest written by another build carries whatever shape that build
    chose; every read of it must survive a surprise without crashing the
    publication that is holding a finished embedding pass."""
    if not isinstance(holder, Mapping):
        return {}
    value = holder.get(name)
    return value if isinstance(value, Mapping) else {}


def _validated_segmented_source_binding(
        source: dict | None, manifest: dict | None, total_rows: int,
        segment_ids: Sequence[str], segment_hashes: Sequence[str],
        shadow_refs: Sequence[int],
        segment_metadata: Mapping[str, str | None] | None = None,
) -> tuple[dict, int] | None:
    if source is None or not isinstance(manifest, Mapping):
        return None
    # A manifest written by an older build carries a different shape entirely
    # (a real box had an int here), so every read is shape-checked: a foreign
    # manifest means "no binding to prove", never a crash mid-publication.
    if _mapping_field(manifest, "segments").get("source") == source:
        return source, int(total_rows)
    marker_source = semantic.source_generation()
    if marker_source == source:
        return marker_source, int(total_rows)
    catalog = manifest.get("catalog")
    if not isinstance(catalog, _SegmentCatalog):
        return None
    _scan_source(
        rebuild=False,
        old_hash_by_id=_CatalogFieldView(catalog, "text_hash"),
        max_new=1,
        old_metadata_by_id=_CatalogFieldView(catalog, "metadata_hash"),
        metadata_only_ids=catalog.metadata_only_ids(),
    )
    if semantic.source_generation() != marker_source:
        common.log("transcript kept moving during embedding validation; next pass retries.")
        return None
    if (catalog.binding_mismatch_count(shadow_refs)
            or catalog.expected_mismatch_count(
                segment_ids, segment_hashes, segment_metadata)):
        common.log("a covered transcript row moved during embedding; next pass re-embeds it.")
        return None
    common.log("transcript advanced during embedding; rebased unchanged semantic rows.")
    return marker_source, catalog.current_count()


def _replaces_all_active_refs(
        manifest: dict | None, shadow_refs: list[int],
        *, append_segmented: bool,
) -> bool:
    if not append_segmented:
        return True
    catalog = (manifest or {}).get("catalog")
    if not isinstance(catalog, Mapping) or len(catalog) != len(shadow_refs):
        return False
    return all(
        int(row.row_ref) == int(shadow_ref)
        for row, shadow_ref in zip(catalog.values(), shadow_refs, strict=True)
    )


def _segment_source_publication_fence(source: dict):
    """Refuse to publish a fresh binding after its transcript source moved."""
    def before_replace() -> None:
        if semantic.source_generation() != source:
            raise common.TranscriptPublicationRace(
                "transcript generation moved before semantic publication")

    return before_replace


def _publish_segment_generation(
    manifest: dict | None,
    source: dict | None,
    total_rows: int,
    final_ids: list[str],
    final_hashes: list[str],
    segment_ids: list[str],
    segment_hashes: list[str],
    segment_parts: list[np.ndarray],
    shadow_refs: list[int],
    *,
    dim: int,
    append_segmented: bool,
    final_metadata: Mapping[str, str | None] | None = None,
    replacement_generation: str | None = None,
    segment_messages=None,
    final_messages=None,
) -> dict | None:
    _require_writable_data_dir("publish a semantic segment generation")
    if append_segmented:
        binding = _validated_segmented_source_binding(
            source, manifest, total_rows, segment_ids, segment_hashes,
            shadow_refs, final_metadata)
    else:
        binding = _validated_source_binding(
            source, final_ids, final_hashes, total_rows, final_metadata)
    if binding is None:
        return None
    bound_source, bound_total = binding
    import embedding_segments
    import semantic_segment_build

    expected = replacement_generation
    if isinstance(manifest, Mapping):
        # `or {}` does not guard a foreign shape - an int is truthy and then
        # fails .get; read each level as a mapping or treat it as absent.
        expected = str(_mapping_field(manifest, "segments").get("generation")
                       or _mapping_field(
                           _mapping_field(manifest, "state"), "commit",
                       ).get("generation") or "") or None
    meta_path = common.EMBEDDINGS_PATH.parent / "embeddings.meta"
    coverage = {"total": int(bound_total), "order": "newest-first"}
    source_fence = _segment_source_publication_fence(bound_source)
    if append_segmented:
        prepared = None
        try:
            artifacts = None
            refs = ()
            if segment_ids:
                messages = (_messages_for_ids(segment_ids, segment_hashes)
                            if segment_messages is None else _verified_messages(
                                segment_ids, segment_hashes, segment_messages))
                prepared = semantic_segment_build.prepare(
                    segment_parts, segment_ids, segment_hashes, messages,
                    dim=dim, model_id=_active_model_id(),
                    allow_legacy_family_reset=_replaces_all_active_refs(
                        manifest, shadow_refs,
                        append_segmented=append_segmented))
                artifacts, refs = prepared.artifacts, prepared.refs
            published = embedding_segments.publish_delta(
                meta_path, source=bound_source, artifacts=artifacts,
                ids=segment_ids, hashes=segment_hashes, refs=refs,
                shadows=shadow_refs, coverage=coverage,
                expected_generation=expected, _before_replace=source_fence)
        finally:
            if prepared is not None:
                prepared.close()
    else:
        messages = (_messages_for_ids(final_ids, final_hashes)
                    if final_messages is None else _verified_messages(
                        final_ids, final_hashes, final_messages))
        with semantic_segment_build.prepare(
                segment_parts, final_ids, final_hashes, messages,
                dim=dim, model_id=_active_model_id(),
                allow_legacy_family_reset=True) as prepared:
            published = embedding_segments.publish_base(
                meta_path, source=bound_source, model_id=_active_model_id(),
                dim=dim, artifacts=prepared.artifacts, ids=final_ids,
                hashes=final_hashes, refs=prepared.refs, coverage=coverage,
                expected_generation=expected, _before_replace=source_fence)
    return {"indexed": int(published["live_rows"]),
            "total": int(published["coverage"]["total"]),
            "pending": int(published["coverage"]["pending"])}


def _full_rebase_total(
        indexed_ids: list[str],
        expected_hashes: dict[str, str],
        expected_metadata: dict[str, str] | None = None,
) -> int | None:
    """Validate covered text and refs metadata, returning the current row count."""
    remaining = set(indexed_ids)
    seen: set[str] = set()
    total = 0
    valid = True
    if expected_metadata is not None:
        import semantic_segment_build
        parents = common.strict_family_parent_map()
        if parents is None:
            return None
        memo: dict[str, str] = {}
    else:
        semantic_segment_build = None
        parents = {}
        memo = {}

    def observe(message: common.Message) -> None:
        nonlocal total, valid
        if message.id in seen:
            valid = False
            return
        seen.add(message.id)
        total += 1
        if (message.id in remaining
                and _text_hash(message.text) != expected_hashes.get(message.id)):
            valid = False
        if (message.id in remaining and semantic_segment_build is not None
                and semantic_segment_build.message_metadata_fingerprint(
                    message, parents, memo) != expected_metadata.get(message.id)):
            valid = False
        remaining.discard(message.id)

    for message in _iter_source_messages():
        observe(message)
    return total if valid and not remaining else None


def _full_segmented_rebase_total(catalog: _SegmentCatalog) -> int | None:
    metadata_only_ids = catalog.metadata_only_ids()
    current, _messages, _pending = _scan_source(
        rebuild=False,
        old_hash_by_id=_CatalogFieldView(catalog, "text_hash"),
        max_new=1,
        old_metadata_by_id=_CatalogFieldView(catalog, "metadata_hash"),
        metadata_only_ids=metadata_only_ids,
    )
    if catalog.covered_mismatch_count():
        return None
    return len(current)


def _delta_rebase_total(
        indexed_ids: list[str], expected_hashes: dict[str, str],
        changed_sessions: set[str],
        expected_metadata: dict[str, str] | None = None,
) -> int | None:
    """Validate Rust's exact delta against the published transcript artifacts."""
    snapshot = _delta_source_snapshot(
        changed_sessions, include_metadata=expected_metadata is not None)
    if snapshot is None:
        return None
    total, current, current_metadata, _messages = snapshot
    affected = []
    for mid in indexed_ids:
        try:
            _agent, tail = mid.split(":", 1)
            session, _turn = tail.rsplit(":", 1)
        except ValueError:
            continue
        if session in changed_sessions:
            affected.append(mid)
    valid = all(
        current.get(mid) == expected_hashes.get(mid) for mid in affected)
    if valid and expected_metadata is not None:
        valid = all(
            current_metadata.get(mid) == expected_metadata.get(mid)
            for mid in affected
        )
    return total if valid else None


def _delta_source_snapshot(
        changed_sessions: set[str], *, include_metadata: bool,
) -> tuple[
        int, dict[str, str], dict[str, str], dict[str, common.Message]
        ] | None:
    """Count the exact semantic source while retaining only changed sessions."""
    if include_metadata:
        import semantic_segment_build
        parents = common.strict_family_parent_map()
        if parents is None:
            return None
        memo: dict[str, str] = {}
    else:
        semantic_segment_build = None
        parents = {}
        memo = {}
    total = 0
    current: dict[str, str] = {}
    current_metadata: dict[str, str] = {}
    messages: dict[str, common.Message] = {}
    for message in _iter_source_messages():
        total += 1
        if str(message.session) not in changed_sessions:
            continue
        if message.id in current:
            return None
        current[message.id] = _text_hash(message.text)
        messages[message.id] = message
        if semantic_segment_build is not None:
            current_metadata[message.id] = (
                semantic_segment_build.message_metadata_fingerprint(
                    message, parents, memo))
    return total, current, current_metadata, messages


def _segmented_delta_rebase_total(manifest, changed_sessions: set[str]):
    """Validate changed-session text and refs without reconstructing every row."""
    import embedding_segments
    if not changed_sessions:
        snapshot = _delta_source_snapshot(
            changed_sessions, include_metadata=False)
        return (None if snapshot is None else
                _SegmentedRebaseDelta(snapshot[0], [], []))
    dead = embedding_segments._dead_row_mask(manifest)
    expected: dict[str, tuple[str, str, int]] = {}
    sessions = sorted(changed_sessions)
    for segment in manifest["segments"]:
        refs = embedding_segments._open_refs(embedding_segments.artifact_path(
            manifest, segment["artifacts"]["refs"]))
        try:
            if not embedding_segments._refs_have_metadata(refs):
                return None
            for start in range(0, len(sessions), 400):
                page = sessions[start:start + 400]
                marks = ",".join("?" for _ in page)
                rows = refs.execute(
                    f"SELECT row_ref,mid,text_hash,metadata_hash FROM refs "
                    f"WHERE session IN ({marks})", page)
                for row_ref, mid, text_hash, metadata_hash in rows:
                    if dead[int(row_ref)]:
                        continue
                    if str(mid) in expected:
                        return None
                    expected[str(mid)] = (
                        str(text_hash), str(metadata_hash), int(row_ref))
        finally:
            refs.close()
    snapshot = _delta_source_snapshot(
        changed_sessions, include_metadata=True)
    if snapshot is None:
        return None
    total, current, current_metadata, messages = snapshot
    stale = {
        mid for mid, fingerprints in expected.items()
        if (current.get(mid) != fingerprints[0]
            or current_metadata.get(mid) != fingerprints[1])
    }
    pending = [
        message for mid, message in messages.items()
        if mid not in expected or mid in stale]
    return _SegmentedRebaseDelta(
        total, pending, sorted(expected[mid][2] for mid in stale))


def _family_impacted_sessions(manifest) -> set[str] | None:
    """Find live sessions whose stored family label moved in this generation."""
    import embedding_segments

    parents = common.strict_family_parent_map()
    if parents is None:
        return None
    dead = embedding_segments._dead_row_mask(manifest)
    memo: dict[str, str] = {}
    impacted: set[str] = set()
    for segment in manifest["segments"]:
        refs = embedding_segments._open_refs(embedding_segments.artifact_path(
            manifest, segment["artifacts"]["refs"]))
        try:
            if not embedding_segments._refs_have_family_label(refs):
                return None
            for row_ref, session, who, stored_label in refs.execute(
                    "SELECT row_ref,session,who,family_label FROM refs"):
                if dead[int(row_ref)]:
                    continue
                session = str(session or "")
                current_label = None
                if str(who or "").lower() not in common.SEMANTIC_DEFAULT_EXCLUDED_ROLES:
                    root = common.family_root(session, parents, memo) if session else ""
                    if not root:
                        return None
                    current_label = "f:" + root
                if stored_label != current_label:
                    impacted.add(session)
        finally:
            refs.close()
    return impacted


def _publish_rebased_pending_plan(source: dict, output: str, total: int,
                                  messages: list[common.Message]) -> bool:
    if _data_dir_readonly():
        return False
    if not messages:
        return False
    builder = None
    try:
        _drop_pending_plan()
        builder = _PendingPlanBuilder(source, output, _active_model_id())
        for seq, message in enumerate(messages):
            builder.add(message, _text_hash(message.text), seq)
        builder.publish(total)
        db = sqlite3.connect(_pending_plan_path())
        try:
            db.execute("UPDATE meta SET value='1' WHERE key='reusable'")
            db.commit()
        finally:
            db.close()
        return True
    except (OSError, RuntimeError, sqlite3.DatabaseError, ValueError):
        if builder is not None and not builder.closed:
            builder.abort()
        _drop_pending_plan()
        return False


def _rebase_pending_plan(
        previous_source: dict, source: dict,
        previous_output: str, output: str,
        *, previous_live_rows: int, current_live_rows: int,
        previous_pending: int, total: int,
        changed_sessions: set[str], messages: list[common.Message],
) -> bool:
    """Move a reusable backlog by replacing only changed-session rows."""
    if _data_dir_readonly() or not _pending_plan_path().exists():
        return False
    if any(str(message.session) not in changed_sessions for message in messages):
        return False
    try:
        db = sqlite3.connect(_pending_plan_path())
        try:
            db.execute("BEGIN IMMEDIATE")
            meta = _plan_meta(db)
            valid = (
                meta.get("schema") == _PENDING_PLAN_SCHEMA
                and meta.get("source") == _generation_key(previous_source)
                and meta.get("output") == str(previous_output or "")
                and meta.get("model") == _active_model_id()
                and meta.get("reusable") == "1"
                and int(meta.get("pending", "-1")) == int(previous_pending)
                and int(meta.get("total", "-1"))
                == int(previous_live_rows) + int(previous_pending)
            )
            if not valid:
                db.rollback()
                return False
            if changed_sessions:
                page = sorted(changed_sessions)
                for start in range(0, len(page), 400):
                    chunk = page[start:start + 400]
                    marks = ",".join("?" for _ in chunk)
                    db.execute(
                        f"DELETE FROM pending WHERE session IN ({marks})", chunk)
            next_seq = int(db.execute(
                "SELECT coalesce(max(seq),-1)+1 FROM pending").fetchone()[0])
            db.executemany(
                "INSERT INTO pending VALUES(?,?,?,?,?)",
                ((message.id, _text_hash(message.text), str(message.session),
                  int(message.ts), next_seq + offset)
                 for offset, message in enumerate(messages)),
            )
            pending = int(db.execute(
                "SELECT count(*) FROM pending").fetchone()[0])
            if pending != int(total) - int(current_live_rows):
                db.rollback()
                return False
            db.executemany(
                "UPDATE meta SET value=? WHERE key=?",
                ((_generation_key(source), "source"),
                 (str(output or ""), "output"),
                 (str(int(total)), "total"),
                 (str(pending), "pending")),
            )
            db.commit()
            return True
        finally:
            db.close()
    except (OSError, RuntimeError, sqlite3.DatabaseError, ValueError, TypeError):
        return False


def _rebase_segmented_generation(
        changed_sessions: "set[str] | str | None", *,
        expected_previous_source: dict | None,
        expected_current_source: dict | None) -> dict | None:
    if _data_dir_readonly():
        return None
    import embedding_segments

    meta = common.EMBEDDINGS_PATH.parent / "embeddings.meta"
    manifest = embedding_segments.load_manifest(meta)
    if manifest["model"] != {
            "id": _active_model_id(), "dim": int(embedder.PROFILE["dim"])}:
        return None
    source = semantic.source_generation()
    if source is None:
        return None
    if manifest["source"] == source:
        return {
            "indexed": int(manifest["live_rows"]),
            "total": int(manifest["coverage"]["total"]),
            "pending": int(manifest["coverage"]["pending"]),
        }
    exact_delta = (
        isinstance(changed_sessions, set)
        and expected_previous_source is not None
        and expected_current_source is not None
        and manifest["source"] == expected_previous_source
        and source == expected_current_source
    )
    family_moved = manifest["source"].get("family") != source.get("family")
    if family_moved:
        if not exact_delta:
            return None
        impacted = _family_impacted_sessions(manifest)
        if impacted is None:
            return None
        changed_sessions = set(changed_sessions) | impacted
    new_messages: list[common.Message] = []
    shadow_refs: list[int] = []
    delta_rebased = False
    if exact_delta:
        result = _segmented_delta_rebase_total(manifest, changed_sessions)
        if result is not None:
            total = result.total
            new_messages = result.pending_messages
            shadow_refs = result.shadow_refs
            delta_rebased = True
        else:
            total = None
    if not delta_rebased:
        loaded = _load_old_manifest(int(embedder.PROFILE["dim"]))
        if loaded is None or not isinstance(
                loaded.get("catalog"), _SegmentCatalog):
            return None
        try:
            total = _full_segmented_rebase_total(loaded["catalog"])
        finally:
            _close_manifest_catalog(loaded)
    if total is None or semantic.source_generation() != source:
        return None
    if (len(shadow_refs) != len(set(shadow_refs))
            or any(type(row_ref) is not int or row_ref < 0
                   for row_ref in shadow_refs)):
        return None
    remaining_live = int(manifest["live_rows"]) - len(shadow_refs)
    if remaining_live <= 0 or total < remaining_live:
        return None
    try:
        previous_output = semantic.output_generation()
    except (OSError, RuntimeError, ValueError, TypeError):
        return None
    if previous_output is None:
        return None
    source_fence = _segment_source_publication_fence(source)
    if shadow_refs:
        if int(manifest.get("delta_count", 0)) >= 16:
            _schedule_segment_compaction()
            common.log(
                "semantic shadow rebase deferred at the 16-delta cap")
            return None
        current = embedding_segments.publish_delta(
            meta, source=source, artifacts=None,
            shadows=shadow_refs, coverage={"total": int(total)},
            expected_generation=str(manifest["generation"]),
            _before_replace=source_fence)
    else:
        current = embedding_segments.publish_rebind(
            meta, source=source, coverage={"total": int(total)},
            expected_generation=str(manifest["generation"]),
            _before_replace=source_fence)
    coverage = {
        "indexed": int(current["live_rows"]),
        "total": int(current["coverage"]["total"]),
        "pending": int(current["coverage"]["pending"]),
    }
    previous_pending = int(manifest["coverage"]["pending"])
    if delta_rebased and semantic.source_generation() == source:
        try:
            output = semantic.output_generation()
            if output is not None:
                if previous_pending == 0 and len(new_messages) == coverage["pending"]:
                    _publish_rebased_pending_plan(
                        source, str(output["bundle"]),
                        coverage["total"], new_messages)
                elif previous_pending > 0:
                    _rebase_pending_plan(
                        manifest["source"], source,
                        str(previous_output["bundle"]), str(output["bundle"]),
                        previous_live_rows=int(manifest["live_rows"]),
                        current_live_rows=int(current["live_rows"]),
                        previous_pending=previous_pending,
                        total=coverage["total"],
                        changed_sessions=changed_sessions,
                        messages=new_messages,
                    )
        except (OSError, RuntimeError, ValueError, TypeError):
            pass
    if shadow_refs:
        _schedule_segment_compaction()
        common.log(
            f"shadowed {len(shadow_refs)} stale semantic row(s) after ingest")
    common.log(f"rebased segmented semantic coverage after ingest: "
               f"{coverage['indexed']}/{coverage['total']} rows searchable")
    return coverage


def rebase_generation_marker(
        changed_sessions: "set[str] | str | None" = None, *,
        expected_previous_source: dict | None = None,
        expected_current_source: dict | None = None) -> dict | None:
    """Keep an unchanged vector subset searchable across a new ingest generation.

    The Rust changed-session delta makes the common case proportional to the few
    sessions that moved; a missing or wildcard delta falls back to a full streamed
    validation. The delta is trusted only when both ends are proven — the marker
    was bound to exactly the source observed before the Rust run and the live
    source is exactly what that run published; a missed earlier rebase or a
    concurrent later ingest full-hashes instead, since applying only the newest
    delta to an older marker would bless stale rows. No vectors or refs are
    rewritten. Changed or deleted covered rows are shadowed from the rebound
    generation; if no validated vector remains, the normal embed pass repairs it.
    """
    if _data_dir_readonly():
        return None
    try:
        try:
            meta_record = json.loads((common.EMBEDDINGS_PATH.parent /
                                      "embeddings.meta").read_bytes())
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            meta_record = None
        if isinstance(meta_record, dict) and meta_record.get("version") == 2:
            import embedding_segments

            # A low-priority embed or compaction can replace this loaded manifest.
            # Retry the whole operation once to reload that winner; never retry
            # validation or I/O failures or turn sustained contention into a loop.
            for attempt in range(2):
                try:
                    return _rebase_segmented_generation(
                        changed_sessions,
                        expected_previous_source=expected_previous_source,
                        expected_current_source=expected_current_source)
                except embedding_segments.SegmentPublicationRace:
                    if attempt:
                        return None
            return None
        marker = json.loads(semantic.generation_marker_path().read_text(
            encoding="utf-8"))
        output = semantic.output_generation()
        source = semantic.source_generation()
        if (not isinstance(marker, dict) or output is None or source is None
                or marker.get("output") != output):
            return None
        old_coverage = semantic._marker_coverage(marker, output)
        if old_coverage is None:
            return None

        manifest = _load_old_manifest(int(embedder.PROFILE["dim"]))
        if (manifest is None or manifest["model"] != _active_model_id()
                or manifest["state"]["identity"] != output["bundle"]):
            return None
        if marker.get("source") == source:
            return old_coverage

        indexed_ids = manifest["ids"]
        expected = dict(zip(indexed_ids, manifest["hashes"]))
        # Delta only when both ends are proven (marker == pre-run source, live
        # source == the run's published one); any skew full-hashes (see docstring).
        exact_delta = (
            isinstance(changed_sessions, set)
            and expected_previous_source is not None
            and expected_current_source is not None
            and marker.get("source") == expected_previous_source
            and source == expected_current_source
        )
        if exact_delta:
            total = _delta_rebase_total(indexed_ids, expected, changed_sessions)
            if total is None:
                total = _full_rebase_total(indexed_ids, expected)
        else:
            total = _full_rebase_total(indexed_ids, expected)
        if total is None or total < len(indexed_ids):
            return None
        if (semantic.source_generation() != source
                or semantic.output_generation() != output):
            return None
        coverage = {
            "indexed": len(indexed_ids),
            "total": total,
            "pending": total - len(indexed_ids),
        }
        if (semantic.source_generation() != source
                or semantic.output_generation() != output):
            return None
        semantic.write_generation_marker(
            source, indexed_rows=len(indexed_ids), total_rows=total,
            expected_output=output)
        # Publish the proven coverage first; the claim-deduped refs-only worker builds the
        # sidecar off the indexer thread (building it synchronously here made indexd spend
        # seconds duplicating the transcript after every tiny delta).
        if semantic.semantic_recently_used():
            try:
                semantic.ensure_refs_async()
            except Exception:  # noqa: BLE001 -- marker is already valid/searchable
                pass
        common.log(f"rebased semantic coverage after ingest: "
                   f"{coverage['indexed']}/{coverage['total']} rows searchable")
        return coverage
    except Exception:  # noqa: BLE001 -- post-ingest availability optimization only
        return None


def _embed_pending_chunks(
    emb, texts: list[str], source: dict | None, *, background: bool,
    bootstrap: bool = False, progress=None, stats: dict | None = None,
) -> tuple[list[np.ndarray], int, bool]:
    """Embed newest-first chunks and yield early if ingest advances.

    A bootstrap pass additionally stops at _BOOTSTRAP_DEADLINE_S of inference
    and publishes what finished, bounding first-search latency on long rows.
    An idle 5k pass keeps throughput, but once an agent causes a new transcript
    publication it finishes only the current 500-row chunk, publishes that useful
    work, and lets the next incremental pass re-plan. This bounds active-session
    semantic unavailability without throwing away already-computed vectors.
    `progress(done, total)` fires after each chunk - vectors publish to disk only
    at the end, so this is the only live signal status surfaces can render.
    Exact-equal text shares one inference result across the pass; each result is
    scattered back to every original row before publication.
    """
    step = ((_BOOTSTRAP_CHUNK if bootstrap else _BACKGROUND_CHUNK)
            if background
            else min(max(1, len(texts)), _FOREGROUND_CHUNK))
    parts: list[np.ndarray] = []
    known: dict[str, int] = {}
    done = 0
    inferred = 0
    moved = False
    expired = False
    inference_started = time.monotonic()
    for start in range(0, len(texts), step):
        stop = min(len(texts), start + step)
        chunk = texts[start:stop]
        fresh_lookup: dict[str, int] = {}
        fresh_texts: list[str] = []
        fresh_first_rows: list[int] = []
        fresh_rows: list[int] = []
        fresh_sources: list[int] = []
        cached: dict[int, tuple[list[int], list[int]]] = {}
        for row, text in enumerate(chunk):
            prior = known.get(text)
            if prior is not None:
                prior_part, prior_row = divmod(prior, step)
                rows, sources = cached.setdefault(prior_part, ([], []))
                rows.append(row)
                sources.append(prior_row)
                continue
            try:
                source_row = fresh_lookup[text]
            except KeyError:
                source_row = len(fresh_texts)
                fresh_lookup[text] = source_row
                fresh_texts.append(text)
                fresh_first_rows.append(row)
            fresh_rows.append(row)
            fresh_sources.append(source_row)

        if fresh_texts:
            vectors = emb.embed_texts(fresh_texts)
            if vectors.ndim != 2 or vectors.shape[0] != len(fresh_texts):
                raise RuntimeError("embedder returned a misaligned batch")
            if len(fresh_texts) == len(chunk):
                part = vectors
            else:
                part = np.empty((len(chunk), vectors.shape[1]), dtype=vectors.dtype)
                part[np.asarray(fresh_rows)] = vectors[np.asarray(fresh_sources)]
        else:
            part = np.empty((len(chunk), parts[0].shape[1]), dtype=parts[0].dtype)
        for part_index, (rows, sources) in cached.items():
            part[np.asarray(rows)] = parts[part_index][np.asarray(sources)]
        parts.append(part)
        for text, row in zip(fresh_texts, fresh_first_rows):
            known[text] = start + row
        inferred += len(fresh_texts)
        done = stop
        if progress is not None:
            progress(done, len(texts))
        if background and stop < len(texts):
            if (bootstrap and time.monotonic() - inference_started
                    >= _BOOTSTRAP_DEADLINE_S):
                expired = True
                break
            if source is not None:
                try:
                    moved = semantic.source_generation() != source
                except RuntimeError:
                    # An unstable observation during atomic source replacement counts
                    # as movement, so the finished chunk still publishes.
                    moved = True
                if moved:
                    break
    if stats is not None:
        stats.update({"rows": done, "inferred": inferred,
                      "reused": done - inferred})
        if expired:
            stats["deadline"] = True
    return parts, done, moved


def _prepare_refs_for_publication(coverage: dict, *, bootstrap: bool = False) -> None:
    """Prebuild query metadata when it will improve a real user's next search.

    The first bounded publication is always prepared. Later backlog/rebase passes
    do the full-corpus sidecar scan only while semantic search has recent demand;
    an idle backfill therefore avoids repeatedly rewriting a large refs DB.
    """
    if not bootstrap and not semantic.semantic_recently_used():
        return
    try:
        import ask
        prepared = ask.prepare_message_refs(coverage)
        common.log(f"prepared semantic refs: {prepared['rows']} rows, "
                   f"{prepared['bytes'] / 1e6:.1f} MB")
    except Exception as exc:  # noqa: BLE001 -- lazy query build remains correct
        common.log(f"semantic refs prewarm deferred: {type(exc).__name__}: {exc}")


def _prepare_q8_for_publication(coverage: dict, *,
                                previous_state: dict | None = None,
                                strict_append: bool = False) -> bool:
    """Keep every searchable vector generation on the bounded native path."""
    started = time.perf_counter()
    try:
        import semantic_q8
        prepared = semantic_q8.publish_for_generation(
            coverage, previous_state=previous_state,
            strict_append=strict_append)
        if prepared is not None:
            total_bytes = sum(int(prepared[name]) for name in (
                "artifact_size", "group_artifact_size", "exact_artifact_size"))
            common.log(f"prepared semantic accelerator: {prepared['rows']} rows, "
                       f"{total_bytes / 1e6:.1f} MB in "
                       f"{time.perf_counter() - started:.3f}s")
            return True
    except Exception as exc:  # noqa: BLE001 -- derived accelerator retries next pass
        common.log(f"semantic accelerator deferred: {type(exc).__name__}: {exc}")
    return False


_BATTERY_PROBE: list = []


def _probe_battery() -> "tuple[bool, int | None]":
    """(on_battery, percent); unknown or unprobeable power reads as AC."""
    on_battery, percent = common.battery_state()
    return bool(on_battery), percent


def _battery_state() -> "tuple[bool, int | None]":
    # one platform power probe per embed process, however often the governor is consulted
    if not _BATTERY_PROBE:
        _BATTERY_PROBE.append(_probe_battery())
    return _BATTERY_PROBE[0]


def _normalized_load() -> float:
    """Return cross-platform host CPU pressure, treating an unknown probe as idle."""
    value = common.host_cpu_fraction()
    return 0.0 if value is None else value


def _governor_deferral(*, ignore_battery: bool = False) -> "str | None":
    """Why this background pass should yield, or None to proceed.

    Semantic indexing is deferrable-forever work: keyword search never depends
    on it and the next incremental pass resumes exactly where this one stopped,
    so a busy or battery-strapped machine wins. AGREP_EMBED_ANYWAY=1 bypasses.
    A bounded stale bootstrap may ignore battery, but not memory or load.
    """
    if os.environ.get("AGREP_EMBED_ANYWAY", "").lower() not in ("", "0", "false", "no", "off"):
        return None
    battery_probe = (lambda: (False, None)) if ignore_battery else _battery_state
    deferral = surface.observed_semantic_deferral(
        common.available_memory_fraction, battery_probe, _normalized_load)
    return deferral.runtime_reason if deferral else None


def _schedule_segment_compaction(*, refresh_metadata: bool = False) -> bool:
    if _data_dir_readonly():
        return False
    if removal_fence.background_removal_active():
        return False
    try:
        import embedding_segments

        meta = common.EMBEDDINGS_PATH.parent / "embeddings.meta"
        manifest = embedding_segments.load_manifest(meta)
        embedding_segments.prune_orphans(manifest)
        if not refresh_metadata and not embedding_segments.should_compact(manifest):
            return False
        command = [sys.executable, str(common.PY_DIR / "semantic_segment_compact.py")]
        if refresh_metadata:
            command += ["--force", "--refresh-metadata"]
        logf = common.open_bounded_log("semantic-embed.log")
        kwargs = {
            "stdin": subprocess.DEVNULL, "stdout": logf, "stderr": logf,
            "cwd": str(common.REPO_ROOT), "env": dict(os.environ), "close_fds": True,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = common.windows_background_child_flags(
                0x08004208)
        else:
            kwargs["start_new_session"] = True
        try:
            if removal_fence.background_removal_active():
                return False
            subprocess.Popen(command, **kwargs)
        finally:
            logf.close()
        return True
    except Exception as exc:  # noqa: BLE001 -- a later refresh retries maintenance
        common.log(f"semantic compaction launch deferred: {type(exc).__name__}: {exc}")
        return False


def main() -> int:
    common.stamp_stdio_lines()
    ap = argparse.ArgumentParser(
        description="Embed messages.jsonl -> embeddings.f32 + .ids",
        allow_abbrev=False)
    ap.add_argument("--smoke", type=int, metavar="N", default=None,
                    help="Embed only the first N messages (quick end-to-end check).")
    ap.add_argument("--full", action="store_true",
                    help="Re-embed every message on this machine's default lane "
                         "(the one moment a store may change lanes). Default is "
                         "incremental: keep existing vectors, drop ones whose "
                         "message is gone, embed only NEW ones.")
    ap.add_argument("--max-new", type=int, default=None,
                    help="Cap new messages embedded this run. Background refresh passes "
                         "this to bound one pass; rerun continues.")
    ap.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--refs-only", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--no-model-download", action="store_true",
                    help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.smoke is not None:
        try:
            return _run_smoke(args)
        except embedder.EmbedderUnavailable as exc:
            detail = common.terminal_safe(exc)
            if args.no_model_download:
                remedy = "rerun without --no-model-download when online"
            else:
                remedy = "run 'agrep doctor' for semantic setup guidance"
            common.log(f"semantic smoke unavailable: {detail}; {remedy}.")
            return 1

    refusal = _mutation_refusal_reason()
    if refusal is not None:
        if _mutation_refusal_info().journal_blocked:
            common.log(f"embedding deferred: {refusal}")
            return 0
        common.log(f"embedding refused: {refusal}")
        return 1

    # gate before claim/plan/model load: declining after ONNX startup wastes the win.
    # refs-only serves an active semantic search and loads no model; only embedding defers.
    if args.background and not args.refs_only:
        ignore_battery = os.environ.get(
            "AGREP_EMBED_IGNORE_BATTERY", "").lower() in ("1", "true", "yes", "on")
        deferral = _governor_deferral(ignore_battery=ignore_battery)
        if deferral:
            common.log(f"embedding deferred: {deferral}")
            return 0

    # Search freshness is useful, but it must not fight the agent/editor that is
    # generating the history. Windows gets BELOW_NORMAL at Popen; POSIX children
    # lower themselves here so manual/reindex runs retain normal priority.
    if args.background and sys.platform != "win32":
        try:
            nice = max(0, min(19, int(os.environ.get("AGREP_SEM_BG_NICE", "10"))))
            os.nice(nice)
        except (OSError, ValueError):
            pass
    if args.background and "AGREP_SEM_THREADS" not in os.environ:
        # Agent latency wins over backlog throughput; indexd's resource policy
        # overrides this portable default with a polite or catch-up budget.
        os.environ["AGREP_SEM_THREADS"] = os.environ.get(
            "AGREP_SEM_BG_THREADS", "4")

    if not _acquire_claim():
        common.log("another embed pass owns the claim; nothing to do.")
        return 0
    global _PRIOR_FAILURES
    try:
        _PRIOR_FAILURES = semantic.embed_failure_streak(semantic.read_embed_state()) - 1
        if args.refs_only:
            try:
                return _run_refs_only()
            except Exception as exc:  # noqa: BLE001 -- derived refs may retry later
                detail = (f"semantic refs preparation failed: "
                          f"{type(exc).__name__}: {exc}")
                common.log(detail[-500:])
                return 1
        import embedding_segments
        try:
            return _run(args)
        except (
                embedding_segments.SegmentPublicationRace,
                common.TranscriptPublicationRace,
                indexd_runtime.DerivedWriteContended,
        ) as exc:
            # Losing a publication race is concurrency, not a failed build: the
            # bundle is untouched and the next pass republishes.
            detail = f"{type(exc).__name__}: {exc}"[-500:]
            _publish_state({
                "state": "contended",
                "finished_at": time.time(),
                "reason": detail,
            })
            common.log(
                f"embedding pass lost a publication race; the next pass "
                f"republishes: {detail}")
            return 0
        except Exception as exc:  # noqa: BLE001 -- detached job must publish failure
            detail = f"{type(exc).__name__}: {exc}"[-500:]
            _publish_state({
                "state": "failed",
                "finished_at": time.time(),
                "failures": _PRIOR_FAILURES + 1,
                "error": detail,
            })
            common.log(f"embedding failed: {detail}")
            return 1
    finally:
        _release_claim()


def _run_smoke(args) -> int:
    if args.smoke <= 0:
        common.log("error: --smoke must be positive.")
        return 2
    source = semantic.source_generation()
    selected = []
    messages = _iter_source_messages()
    try:
        for message in messages:
            selected.append(message)
            if len(selected) >= args.smoke:
                break
    finally:
        close = getattr(messages, "close", None)
        if callable(close):
            close()
    if not selected:
        common.log("error: no messages to embed (messages.jsonl empty or missing).")
        return 1
    if semantic.source_generation() != source:
        common.log("semantic smoke source moved during the diagnostic; retry it.")
        return 1
    model = embedder.get(
        download=not bool(getattr(args, "no_model_download", False)),
        lane=_active_lane())
    vectors = np.asarray(
        model.embed_texts([message.text for message in selected]),
        dtype=np.float32)
    expected = (len(selected), int(embedder.PROFILE["dim"]))
    if vectors.shape != expected or not np.all(np.isfinite(vectors)):
        common.log("semantic smoke produced invalid vectors.")
        return 1
    common.log(
        f"semantic smoke passed: embedded the first {len(selected)} source rows "
        "as a bounded sample; source total was not counted and production "
        "coverage was not changed.")
    return 0


def _run_refs_only() -> int:
    """Build candidate metadata for an already-searchable vector generation."""
    if _data_dir_readonly():
        return 1
    coherence = semantic.embedding_coherence()
    coverage = coherence.get("coverage")
    if not coherence.get("searchable", coherence.get("coherent", False)) \
            or not isinstance(coverage, dict):
        common.log(f"semantic refs deferred: embeddings {coherence.get('state')}")
        return 1
    import ask
    prepared = ask.prepare_message_refs(coverage)
    # Close the race around the potentially-large source scan. prepare_message_refs
    # binds its DB to a vector/source identity; coherence proves the publish-last
    # marker still grants search permission for that same generation.
    after = semantic.embedding_coherence()
    if (not after.get("searchable", after.get("coherent", False))
            or after.get("coverage") != coverage):
        common.log("semantic refs prepared for a generation that moved; next demand retries")
        return 1
    common.log(f"prepared semantic refs: {prepared['rows']} rows, "
               f"{prepared['bytes'] / 1e6:.1f} MB")
    return 0


def _materialize_pending_vectors(
        messages: list[common.Message], selected_hashes: list[str],
        metadata_only_ids: set[str], *, append_segmented: bool,
        manifest: dict | None, dim: int, source: dict | None,
        args, state: dict,
) -> _PendingVectors:
    selected_hash_by_id = dict(zip(
        (message.id for message in messages), selected_hashes, strict=True))
    if append_segmented:
        metadata_messages = [
            message for message in messages
            if message.id in metadata_only_ids
        ]
        inference_messages = [
            message for message in messages
            if message.id not in metadata_only_ids
        ]
    else:
        metadata_messages = []
        inference_messages = list(messages)

    if inference_messages:
        common.log(
            f"embedding {len(inference_messages)} messages with "
            f"{_active_model_id()}"
            + (f" (smoke, first {args.smoke})" if args.smoke else ""))
    if metadata_messages:
        common.log(
            f"refreshing refs/groups for {len(metadata_messages)} unchanged vector(s)")

    metadata_parts: list[np.ndarray] = []
    if metadata_messages:
        metadata_parts = [_segment_vectors_for_ids(
            manifest, [message.id for message in metadata_messages], dim)]
    load_s = 0.0
    inference_s = 0.0
    dedup_stats: dict[str, int] = {}
    if inference_messages:
        files = embedder.PROFILE.get("files") or {}
        cached = bool(files) and all(
            (embedder.model_dir() / name).is_file() for name in files)
        _publish_state({**state, "phase": (
            "loading the model" if cached
            else "downloading the model (one-time)")})
        load_started = time.perf_counter()
        predicted = _lane_predicted()
        model = embedder.get(
            download=not bool(getattr(args, "no_model_download", False)),
            # Predicted lane = default request: Metal may quietly fall back
            # to CPU and the pass adopts what opened. A store-recorded lane
            # stays named and required - a mismatch there must error.
            lane=None if predicted else _active_lane())
        load_s = time.perf_counter() - load_started
        if predicted:
            opened = embedder.lane_of(model.profile_string)
            if opened is not None and opened != _active_lane():
                _adopt_opened_lane(opened)
        if model.profile_string != _active_model_id():
            raise embedder.EmbedderUnavailable(
                f"planned this pass as '{_active_model_id()}' but the loaded "
                f"embedder produces '{model.profile_string}'")
        inference_started = time.perf_counter()
        parts, embedded_n, source_moved = _embed_pending_chunks(
            model, [message.text for message in inference_messages], source,
            background=bool(getattr(args, "background", False)),
            bootstrap=bool(getattr(args, "bootstrap_pass", False)),
            progress=lambda done, total: _publish_state(
                {**state, "done": done, "total": total}),
            stats=dedup_stats)
        inference_s = time.perf_counter() - inference_started
    else:
        parts, embedded_n, source_moved = [], 0, False
    if dedup_stats.get("reused"):
        common.log(f"embedding dedupe: inferred {dedup_stats['inferred']} unique "
                   f"texts, reused {dedup_stats['reused']} exact duplicates")
    if source_moved:
        common.log(f"transcript advanced during background embedding; publishing "
                   f"the completed {embedded_n}-row chunk and replanning the rest.")
    elif dedup_stats.get("deadline"):
        common.log(f"bootstrap deadline reached; publishing the completed "
                   f"{embedded_n}-row prefix (later passes continue).")

    published_messages = [
        *metadata_messages,
        *inference_messages[:embedded_n],
    ]
    return _PendingVectors(
        messages=published_messages,
        hashes=[selected_hash_by_id[message.id] for message in published_messages],
        parts=[*metadata_parts, *parts],
        load_s=load_s,
        inference_s=inference_s,
    )


def _run(args) -> int:
    if _data_dir_readonly():
        return 1
    owner = _ManifestCatalogOwner()
    try:
        result = _run_owned(args, owner)
        if result == 0 and args.full:
            semantic.clear_integrity_rebuild_request()
        return result
    finally:
        owner.close()


def _defer_segment_maintenance(manifest: dict | None, incremental: bool) -> bool:
    if not incremental or not manifest.get("segmented"):
        return False
    current = manifest["segments"]
    versions = manifest.get("refs_versions")
    if versions is not None and versions != frozenset({5}):
        scheduled = _schedule_segment_compaction(refresh_metadata=True)
        _publish_state({
            "state": "compacting" if scheduled else "stale",
            "finished_at": time.time(),
            "indexed": int(current["live_rows"]),
            "total": int(current["coverage"]["total"]),
            "pending": int(current["coverage"]["pending"]),
        })
        common.log("semantic refs metadata upgrade scheduled without "
                   "dropping live vector coverage.")
        return True
    if int(current["delta_count"]) < 16:
        return False
    _schedule_segment_compaction()
    _publish_state({
        "state": "compacting", "finished_at": time.time(),
        "indexed": int(current["live_rows"]),
        "total": int(current["coverage"]["total"]),
        "pending": int(current["coverage"]["pending"]),
    })
    common.log("semantic refresh deferred at the 16-delta cap; "
               "governed compaction is pending.")
    return True


def _prune_loaded_segment_orphans(manifest: dict | None) -> None:
    if _data_dir_readonly():
        return
    if not manifest or not manifest.get("segmented"):
        return
    import embedding_segments

    segmented = manifest.get("segments")
    if not isinstance(segmented, embedding_segments.LoadedManifest):
        return
    cleanup = embedding_segments.prune_orphans(segmented)
    removed = len(cleanup["removed"])
    deferred = len(cleanup["deferred"])
    if removed or deferred:
        common.log(
            f"semantic segment cleanup: removed {removed} orphan artifact(s), "
            f"deferred {deferred}")


def _scan_refresh_source(
        args, source: dict | None, manifest: dict | None, incremental: bool,
        manifest_output: str, metadata_only_ids,
) -> tuple[Mapping[str, str], list[common.Message], list[str], int, int, bool]:
    catalog = manifest.get("catalog") if incremental else None
    old_hashes = (
        _CatalogFieldView(catalog, "text_hash")
        if isinstance(catalog, Mapping)
        else dict(zip(manifest["ids"], manifest["hashes"]))
        if incremental else None
    )
    old_metadata = (
        _CatalogFieldView(catalog, "metadata_hash")
        if incremental and manifest.get("segmented")
        and isinstance(catalog, Mapping) else None
    )
    builder = None
    if args.max_new is not None and source is not None:
        try:
            builder = _PendingPlanBuilder(
                source, manifest_output, _active_model_id())
        except (OSError, sqlite3.DatabaseError):
            builder = None
    active = False
    try:
        current, messages, pending = _scan_source(
            rebuild=not incremental, old_hash_by_id=old_hashes,
            max_new=args.max_new, plan_builder=builder,
            old_metadata_by_id=old_metadata,
            metadata_only_ids=metadata_only_ids)
        selected = [current[message.id] for message in messages]
        total = len(current)
        if builder is not None and pending > len(messages):
            try:
                builder.publish(total)
                active = True
            except (OSError, sqlite3.DatabaseError):
                builder.abort()
        elif builder is not None:
            builder.abort()
        return current, messages, selected, pending, total, active
    except Exception:
        if builder is not None:
            builder.abort()
        raise


def _plan_refresh(
        args, dim: int, source: dict | None, owner: _ManifestCatalogOwner,
) -> _RefreshPlan | None:
    replacement = _segment_replacement_generation()
    if args.smoke is not None:
        messages = list(common.iter_messages(
            path=common.MESSAGES_PATH, limit=args.smoke))
        current = {message.id: _text_hash(message.text) for message in messages}
        return _RefreshPlan(
            source, replacement, None, False, None, current, messages,
            [current[message.id] for message in messages], len(messages),
            len(current), "", False, set())

    manifest = None if args.full else _load_old_manifest(dim, metadata_only=True)
    owner.bind(manifest)
    _prune_loaded_segment_orphans(manifest)
    incremental = bool(
        manifest and manifest["model"] == _active_model_id())
    if manifest and not incremental:
        common.log(f"embedder changed ({manifest['model']} -> "
                   f"{_active_model_id()}); rebuilding newest-first.")
        _note_lane_change(str(manifest["model"]), _active_model_id())
    elif not args.full and common.EMBEDDINGS_PATH.exists() and manifest is None:
        common.log("embedding cache is legacy/corrupt/misaligned; rebuilding cleanly.")
    if _defer_segment_maintenance(manifest, incremental):
        return None

    output = str(manifest["state"]["identity"]) if incremental else ""
    cached = (_load_pending_plan(
        source, output, _active_model_id(),
        int(manifest["rows"]), args.max_new)
              if incremental and not args.full else None)
    if cached is None and incremental and manifest.get("metadata_only"):
        manifest = _load_old_manifest(dim)
        owner.bind(manifest)
        incremental = bool(
            manifest and manifest["model"] == _active_model_id())
        output = str(manifest["state"]["identity"]) if incremental else ""
    metadata_only_ids = set()
    catalog = manifest.get("catalog") if incremental else None
    if isinstance(catalog, _SegmentCatalog):
        metadata_only_ids = catalog.metadata_only_ids()
    if cached is not None:
        messages = cached["messages"]
        pending = int(cached["pending"])
        common.log("incremental plan: reused generation-bound pending page "
                   f"({len(messages)} selected of {pending})")
        return _RefreshPlan(
            source, replacement, manifest, incremental, cached, None,
            messages, cached["hashes"], pending, int(cached["total"]),
            output, True, metadata_only_ids)
    current, messages, selected, pending, total, active = _scan_refresh_source(
        args, source, manifest, incremental, output, metadata_only_ids)
    return _RefreshPlan(
        source, replacement, manifest, incremental, None, current,
        messages, selected, pending, total, output, active,
        metadata_only_ids)


def _plan_retention(plan: _RefreshPlan, dim: int) -> _RetentionPlan:
    kept_ids: list[str] = []
    kept_hashes: list[str] = []
    kept_count = 0
    kept_metadata: Mapping[str, str | None] = {}
    kept_matrix = np.zeros((0, dim), dtype=np.float32)
    retained_mapping = None
    append = bool(plan.incremental and plan.manifest.get("segmented"))
    shadow_refs: list[int] = []
    old_ids = None
    if not plan.incremental:
        return _RetentionPlan(
            kept_ids, kept_hashes, kept_count, kept_metadata, kept_matrix,
            retained_mapping, append, shadow_refs, old_ids)

    manifest = plan.manifest
    old_count = int(manifest["rows"])
    catalog = manifest.get("catalog")
    if isinstance(catalog, Mapping):
        old_hashes = None
        old_metadata: Mapping[str, str | None] = _CatalogFieldView(
            catalog, "metadata_hash")
    else:
        old_ids, old_hashes = manifest.get("ids"), manifest.get("hashes")
        old_metadata = {}
    keep = None
    if plan.cached_plan is not None:
        kept_count = old_count
        if not manifest.get("metadata_only"):
            kept_ids, kept_hashes = old_ids, old_hashes
    elif append:
        if not isinstance(catalog, Mapping):
            raise RuntimeError("semantic shadow catalog is unavailable")
        catalog_rows = (
            catalog.current_rows()
            if isinstance(catalog, _SegmentCatalog)
            else ((mid, row, plan.cur_hash.get(mid),
                   mid in plan.metadata_only_ids)
                  for mid, row in catalog.items())
        )
        for mid, row, current_hash, metadata_only in catalog_rows:
            if not metadata_only and current_hash == row.text_hash:
                kept_count += 1
            else:
                shadow_refs.append(int(row.row_ref))
        kept_metadata = old_metadata
    else:
        if old_ids is None or old_hashes is None:
            raise RuntimeError("semantic row catalog is unavailable")
        keep = [
            index for index, mid in enumerate(old_ids)
            if mid not in plan.metadata_only_ids
            and plan.cur_hash.get(mid) == old_hashes[index]
        ]
        kept_ids = [old_ids[index] for index in keep]
        kept_hashes = [old_hashes[index] for index in keep]
        kept_count = len(kept_ids)
        kept_metadata = old_metadata
    if plan.cached_plan is None:
        expected_pending = plan.total_rows - kept_count
        if plan.pending_count != expected_pending:
            raise RuntimeError(
                f"semantic plan mismatch: scanned {plan.pending_count} pending, "
                f"derived {expected_pending}")
    common.log(f"incremental: kept {kept_count}, "
               f"pruned {old_count - kept_count}, "
               f"new/changed {plan.pending_count}")
    if (not append and kept_ids
            and (not manifest.get("segmented")
                 or plan.messages or len(kept_ids) != len(old_ids))):
        kept_matrix, retained_mapping = _load_retained_rows(
            manifest, keep, has_pending=bool(plan.messages), dim=dim)
    return _RetentionPlan(
        kept_ids, kept_hashes, kept_count, kept_metadata, kept_matrix,
        retained_mapping, append, shadow_refs, old_ids)


def _publish_state_for_coverage(coverage: dict | None, *, capped=False) -> None:
    _publish_state({
        "state": ("ready" if coverage and coverage["pending"] == 0
                  else "partial" if coverage else "stale"),
        "finished_at": time.time(), **({"capped": bool(capped)} if capped else {}),
        **(coverage or {}),
    })


def _publish_without_pending(
        plan: _RefreshPlan, retention: _RetentionPlan, dim: int) -> int:
    _require_writable_data_dir("publish semantic embeddings")
    try:
        if retention.append_segmented:
            if retention.shadow_refs:
                coverage = _publish_segment_generation(
                    plan.manifest, plan.source, plan.total_rows,
                    retention.kept_ids, retention.kept_hashes,
                    [], [], [], retention.shadow_refs,
                    dim=dim, append_segmented=True,
                    final_metadata=retention.kept_metadata)
                common.log("pruned stale semantic rows; nothing new to embed.")
            else:
                binding = _validated_segmented_source_binding(
                    plan.source, plan.manifest, plan.total_rows,
                    (), (), (), retention.kept_metadata)
                coverage = None
                if binding is not None:
                    import embedding_segments
                    bound_source, bound_total = binding
                    current = plan.manifest["segments"]
                    proof_current = (
                        embedding_segments.publication_artifacts_still_bound(
                            current))
                    if (current.get("source") != bound_source
                            or int(current["coverage"]["total"]) != bound_total
                            or not proof_current):
                        current = embedding_segments.publish_rebind(
                            common.EMBEDDINGS_PATH.parent / "embeddings.meta",
                            source=bound_source, coverage={"total": bound_total},
                            expected_generation=current["generation"])
                    coverage = {
                        "indexed": int(current["live_rows"]),
                        "total": int(current["coverage"]["total"]),
                        "pending": int(current["coverage"]["pending"]),
                    }
                common.log("segmented embeddings already up to date (0 new).")
        else:
            coverage = _publish_segment_generation(
                plan.manifest, plan.source, plan.total_rows,
                retention.kept_ids, retention.kept_hashes,
                retention.kept_ids, retention.kept_hashes,
                [retention.kept_matrix], [], dim=dim,
                append_segmented=False,
                final_metadata=retention.kept_metadata,
                replacement_generation=plan.replacement_generation)
            common.log("migrated the current flat semantic base to segments.")
    finally:
        if retention.retained_mapping is not None:
            common.close_embedding_matrix(retention.retained_mapping)
    if coverage and coverage["pending"] == 0:
        _drop_pending_plan()
    _publish_state_for_coverage(coverage)
    if retention.append_segmented and coverage:
        _schedule_segment_compaction()
    if coverage:
        _prune_legacy_embedding_layout()
    return 0


def _publish_pending(
        args, plan: _RefreshPlan, retention: _RetentionPlan,
        dim: int, base: dict, run_started: float) -> int:
    _require_writable_data_dir("publish semantic embeddings")
    current_metadata = _metadata_hashes_for_messages(plan.messages)
    started = time.perf_counter()
    plan_s = started - run_started
    try:
        pending = _materialize_pending_vectors(
            plan.messages, plan.selected_hashes, plan.metadata_only_ids,
            append_segmented=retention.append_segmented,
            manifest=plan.manifest, dim=dim, source=plan.source,
            args=args, state=base)
        published_messages = pending.messages
        new_ids = [message.id for message in published_messages]
        new_hashes = pending.hashes
        parts = pending.parts
        ids = (
            new_ids if retention.append_segmented
            else retention.kept_ids + new_ids)
        if retention.kept_ids and not retention.append_segmented:
            parts = [retention.kept_matrix, *parts]
        published_hashes = (
            new_hashes if retention.append_segmented
            else retention.kept_hashes + new_hashes)
        final_metadata = ChainMap(
            {mid: current_metadata[mid] for mid in new_ids},
            retention.kept_metadata)
        publish_started = time.perf_counter()
        coverage = _publish_segment_generation(
            plan.manifest, plan.source, plan.total_rows, ids, published_hashes,
            new_ids if retention.append_segmented else ids,
            new_hashes if retention.append_segmented else published_hashes,
            parts, retention.shadow_refs,
            dim=dim, append_segmented=retention.append_segmented,
            final_metadata=final_metadata,
            replacement_generation=plan.replacement_generation,
            segment_messages=(
                published_messages if retention.append_segmented else None),
            final_messages=(
                published_messages
                if not retention.append_segmented
                and not retention.kept_ids else None))
        publish_s = time.perf_counter() - publish_started
        if plan.pending_plan_active and coverage is not None:
            try:
                output = semantic.output_generation()
                if output is not None:
                    _advance_pending_plan(
                        plan.source, plan.manifest_output,
                        str(output["bundle"]), new_ids)
            except (OSError, RuntimeError, ValueError, TypeError):
                pass
    finally:
        if retention.retained_mapping is not None:
            common.close_embedding_matrix(retention.retained_mapping)
    if coverage is not None:
        _prune_legacy_embedding_layout()
    elapsed = time.perf_counter() - started
    published_count = (
        int(coverage["indexed"]) if coverage
        else retention.kept_count + len(new_ids))
    common.log(f"embed done | model={_active_model_id()} | "
               f"count={published_count} | dim={dim} | elapsed={elapsed:.2f}s")
    common.log(f"embed phases | plan={plan_s:.3f}s | "
               f"load={pending.load_s:.3f}s | "
               f"inference={pending.inference_s:.3f}s | "
               f"segment-publish={publish_s:.3f}s")
    common.log("published immutable semantic segment generation")
    if args.smoke is None and coverage and coverage["pending"] == 0:
        _drop_pending_plan()
    _publish_state_for_coverage(
        coverage, capped=bool(coverage and coverage["pending"]))
    if retention.append_segmented and coverage:
        _schedule_segment_compaction()
    return 0


def _run_owned(args, owner: _ManifestCatalogOwner) -> int:
    if _data_dir_readonly():
        return 1
    run_started = time.perf_counter()
    dim = int(embedder.PROFILE["dim"])
    if args.max_new is not None and args.max_new <= 0:
        common.log("error: --max-new must be positive.")
        return 2
    source = semantic.source_generation()
    # Bootstrap-shaped = background pass while the lane cannot answer yet:
    # latency-bound (small chunks + deadline); the drain keeps throughput.
    args.bootstrap_pass = bool(
        getattr(args, "background", False)
        and not semantic.embedding_coherence().get("searchable"))
    global _LANE_CHANGE, _FRESH_LANE
    _LANE_CHANGE = _inherited_lane_change()
    if getattr(args, "full", False):
        # Read the outgoing identity BEFORE the fresh-lane flag hides it, so a
        # lane move gets the same search-side rebuild notice an ordinary
        # identity change earns.
        meta_path = common.EMBEDDINGS_PATH.parent / "embeddings.meta"
        try:
            outgoing = common.read_index_meta(meta_path)[1]
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            outgoing = None
        _FRESH_LANE = True
        if outgoing:
            _note_lane_change(str(outgoing), _active_model_id())
    base = {"state": "running", "started_at": time.time(),
            **({"failures": _PRIOR_FAILURES} if _PRIOR_FAILURES else {})}
    _publish_state(base)
    plan = _plan_refresh(args, dim, source, owner)
    if plan is None:
        return 0
    if plan.total_rows <= 0:
        common.log("error: no messages to embed (messages.jsonl empty or missing).")
        _publish_state({"state": "failed", "finished_at": time.time(),
                        "failures": _PRIOR_FAILURES + 1,
                        "reason": "no messages"})
        return 1
    retention = _plan_retention(plan, dim)
    if args.max_new is not None and plan.pending_count > args.max_new:
        common.log(f"capping at {args.max_new} newest of "
                   f"{plan.pending_count} pending messages")
    if plan.incremental and not plan.messages:
        return _publish_without_pending(plan, retention, dim)
    return _publish_pending(
        args, plan, retention, dim, base, run_started)


def _stamp(source: dict | None, *, indexed_ids: list[str],
           expected_hashes: dict[str, str] | None,
           indexed_hashes: list[str] | None = None,
           total_rows: int, q8_previous_state: dict | None = None,
           q8_strict_append: bool = False) -> dict | None:
    """Bind the published bundle to a validated transcript generation. A capped
    pass is stamped as searchable PARTIAL coverage; later passes retain its
    vectors and converge newest-first.

    If ingest published a newer transcript while ONNX was running, re-read that
    generation and rebase when every indexed id still has the same text hash.
    Additions/uncovered edits simply increase pending coverage; a changed/deleted
    covered row refuses the marker and keeps semantic safely unavailable.
    """
    if _data_dir_readonly():
        return None
    if source is None:
        return None
    try:
        marker_source = semantic.source_generation()
        if marker_source != source:
            if expected_hashes is None:
                if indexed_hashes is None or len(indexed_ids) != len(indexed_hashes):
                    raise ValueError("indexed embedding hashes are misaligned")
                expected_hashes = dict(zip(indexed_ids, indexed_hashes))
            remaining = set(indexed_ids)
            seen: set[str] = set()
            total_rows = 0
            changed = None

            def validate(message: common.Message) -> None:
                nonlocal total_rows, changed
                if message.id in seen:
                    changed = message.id
                    return
                seen.add(message.id)
                total_rows += 1
                if (message.id in remaining
                        and _text_hash(message.text) != expected_hashes.get(message.id)):
                    changed = message.id
                remaining.discard(message.id)

            for message in common.iter_messages(path=common.MESSAGES_PATH):
                validate(message)
            # Timestamp inheritance is irrelevant to generation validation.
            for message in iter_reply_messages({}):
                validate(message)
            if semantic.source_generation() != marker_source:
                common.log("transcript kept moving during embedding validation; "
                           "marker not stamped (the next pass converges).")
                return None
            if changed is not None or remaining:
                common.log("a covered transcript row moved during embedding; marker "
                           "not stamped (the next pass re-embeds it).")
                return None
            common.log("transcript advanced during embedding; validated covered rows "
                       "and rebased partial coverage onto the new generation.")
        indexed_rows = len(indexed_ids)
        coverage = {"indexed": indexed_rows, "total": total_rows,
                    "pending": total_rows - indexed_rows}
        expected_output = semantic.output_generation()
        if expected_output is None:
            return None
        # Build the immutable ordinal/text sidecar before publishing permission to query this generation.
        _prepare_refs_for_publication(
            coverage,
            bootstrap=indexed_rows <= semantic.SEMANTIC_BOOTSTRAP_MAX_NEW)
        if semantic.source_generation() != marker_source:
            common.log("transcript moved during semantic refs preparation; marker "
                       "not stamped (the next pass converges).")
            return None
        native_ready = _prepare_q8_for_publication(
            coverage, previous_state=q8_previous_state,
            strict_append=q8_strict_append)
        if not native_ready and indexed_rows >= _Q8_REQUIRED_ROWS:
            common.log("semantic accelerator was not published; marker not stamped "
                       "so search cannot fall back to an unbounded matrix scan.")
            return None
        if semantic.source_generation() != marker_source:
            common.log("transcript moved during semantic accelerator preparation; marker "
                       "not stamped (the next pass converges).")
            return None
        semantic.write_generation_marker(
            marker_source, indexed_rows=indexed_rows, total_rows=total_rows,
            expected_output=expected_output)
        if coverage["pending"]:
            common.log(
                f"published partial semantic coverage: {indexed_rows}/{total_rows} "
                "rows searchable; background passes continue newest-first.")
        return coverage
    except (OSError, RuntimeError, ValueError) as e:
        common.log(f"marker stamp failed: {e}")
        return None
if __name__ == "__main__":
    raise SystemExit(main())
