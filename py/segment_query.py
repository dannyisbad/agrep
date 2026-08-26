"""Query adapters for immutable segmented semantic generations."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import struct
import time
from bisect import bisect_right
from pathlib import Path

import numpy as np

import common
import compact
import embedding_segments
import indexd_runtime
import ownerfile
import surface_policy as surface


_INTEGRITY_RECEIPT_MAX_BYTES = 8 * 1024 * 1024
_CORPUS_CONNECT_RETRY_S = 0.2
_CORPUS_CONNECT_POLL_S = 0.01


def _data_dir_readonly() -> bool:
    return common.data_dir_readonly(common.DATA_DIR)


def _mutation_refusal_reason() -> str | None:
    if _data_dir_readonly():
        return "AGREP_DATA_READONLY protects semantic integrity receipts"
    ownership = indexd_runtime.derived_writer_mutation_info()
    return None if ownership.writable else ownership.reason


def corpus_update_active() -> bool:
    """Whether a live process holds the corpus publication lock."""
    try:
        import corpusdb
        return corpusdb._live_refresh_lock()
    except (ImportError, OSError):
        return False


class SegmentQueryError(RuntimeError):
    pass


class SegmentIntegrityError(SegmentQueryError):
    """A deterministic derived-artifact failure that requires a clean rebuild."""


class SegmentArtifactMoved(SegmentQueryError):
    """A concurrent publish or compaction replaced the artifact this reader held.

    Evidence of concurrency, never of damage: callers reopen the newer
    generation and degrade this one answer rather than discarding the bundle."""


# Publication replaces artifacts atomically, so a vanished path or a new inode
# under it is a republication; a mutation inside the SAME inode is damage.
_REPUBLISH_ATTEMPTS = 3


_identity_republished = embedding_segments.identity_republished


def _current_identity_republished(path: Path, before) -> bool:
    """Whether ``path`` now holds a different file than ``before`` recorded."""
    try:
        return _identity_republished(before, embedding_segments._file_identity(path))
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _stamp_republished(before, after) -> bool:
    """Whether an artifact-stamp difference is publication rather than damage."""
    prior, current = dict(before), dict(after)
    if set(prior) != set(current):
        return True
    return any(_identity_republished(prior[key], current[key])
               for key in prior if prior[key] != current[key])


def _meta_republished(before, after) -> bool:
    return before[2:4] != after[2:4]


def metadata_excluded_roles(filters: dict | None) -> tuple[str, ...]:
    raw = (filters or {}).get("_exclude_who") or ()
    if isinstance(raw, str):
        raw = (raw,)
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return ()
    return tuple(sorted({str(value) for value in raw if value}))


def metadata_included_roles(filters: dict | None) -> tuple[str, ...]:
    raw = (filters or {}).get("_include_who") or ()
    if isinstance(raw, str):
        raw = (raw,)
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return ()
    return tuple(sorted({str(value) for value in raw if value}))


def metadata_where(filters: dict | None) -> tuple[str, list]:
    clauses, params = [], []
    filters = filters or {}
    if filters.get("agent"):
        clauses.append("agrep_contains_ci(agent, ?)")
        params.append(filters["agent"])
    if filters.get("project"):
        clauses.append("agrep_contains_ci(project, ?)")
        params.append(filters["project"])
    if filters.get("exclude_project"):
        clauses.append("NOT agrep_contains_ci(project, ?)")
        params.append(filters["exclude_project"])
    if filters.get("chat"):
        clauses.append("agrep_starts_ci(session, ?)")
        params.append(filters["chat"])
    if filters.get("exclude_session"):
        boundary = filters.get("exclude_session_from_turn")
        if boundary is None:
            clauses.append("session <> ?")
            params.append(filters["exclude_session"])
        else:
            clauses.append(
                "NOT (session = ? AND typeof(turn) = 'integer' AND turn >= ?)")
            params.extend((filters["exclude_session"], int(boundary)))
    excluded_sessions = filters.get("_exclude_sessions")
    excluded_sessions_json = filters.get("_exclude_sessions_json")
    if isinstance(excluded_sessions, (list, tuple, set, frozenset)):
        excluded_sessions_json = json.dumps(
            sorted({str(value) for value in excluded_sessions if value}),
            separators=(",", ":"),
        )
    elif "_exclude_sessions" in filters:
        clauses.append("0")
    if excluded_sessions_json:
        clauses.append(
            "session NOT IN (SELECT value FROM json_each(?))")
        params.append(excluded_sessions_json)
    if filters.get("_exclude_family_id") is not None:
        clauses.append("family_id <> ?")
        params.append(int(filters["_exclude_family_id"]))
    if filters.get("who"):
        clauses.append("who = ?")
        params.append(filters["who"])
    if "_include_who" in filters:
        included = metadata_included_roles(filters)
        if included:
            clauses.append(f"who IN ({','.join('?' for _ in included)})")
            params.extend(included)
        else:
            clauses.append("0")
    excluded = metadata_excluded_roles(filters)
    if excluded:
        clauses.append(f"who NOT IN ({','.join('?' for _ in excluded)})")
        params.extend(excluded)
    if filters.get("model"):
        clauses.append(("agrep_contains_ci(model, ?)" if filters.get("model_soft")
                        else "agrep_equal_ci(model, ?)"))
        params.append(filters["model"])
    if filters.get("since_ms") is not None:
        clauses.append("ts >= ?")
        params.append(int(filters["since_ms"]))
    if filters.get("until_ms") is not None:
        clauses.append("ts < ?")
        params.append(int(filters["until_ms"]))
    return " AND ".join(clauses) or "1", params




def register_metadata_functions(db: sqlite3.Connection) -> None:
    db.create_function("agrep_contains_ci", 2, lambda actual, needle: int(
        (needle or "").lower() in (actual or "").lower()), deterministic=True)
    db.create_function("agrep_starts_ci", 2, lambda actual, needle: int(
        (actual or "").lower().startswith((needle or "").lower())), deterministic=True)
    db.create_function("agrep_equal_ci", 2, lambda actual, needle: int(
        (actual or "").lower() == (needle or "").lower()), deterministic=True)


_text_hash = common.semantic_text_hash


def _liveness(manifest: embedding_segments.LoadedManifest) -> np.ndarray:
    live = np.zeros(int(manifest["next_row_ref"]), dtype=np.bool_)
    try:
        for segment in manifest["segments"]:
            start = int(segment["row_base"])
            live[start:start + int(segment["rows"])] = True
        for shadow in manifest["shadows"]:
            path = embedding_segments.artifact_path(
                manifest, shadow["artifact"])
            payload = path.read_bytes()
            if len(payload) != int(shadow["rows"]) * 8:
                raise SegmentIntegrityError(
                    "semantic shadow length is invalid")
            for (row_ref,) in struct.iter_unpack("<Q", payload):
                if row_ref >= len(live) or not live[row_ref]:
                    raise SegmentIntegrityError(
                        "semantic shadow targets a non-live row")
                live[row_ref] = False
    except SegmentIntegrityError:
        raise
    except FileNotFoundError as exc:
        raise SegmentArtifactMoved(
            "semantic liveness artifacts were republished while opening") from exc
    except (IndexError, OSError, TypeError, ValueError, struct.error) as exc:
        raise SegmentIntegrityError(
            "semantic liveness artifacts are unreadable") from exc
    if int(np.count_nonzero(live)) != int(manifest["live_rows"]):
        raise SegmentIntegrityError("semantic liveness disagrees with its manifest")
    return live


class SegmentedMatrix:
    def __init__(self, manifest: embedding_segments.LoadedManifest,
                 live: np.ndarray, assert_current):
        self.manifest = manifest
        self.live = live
        self.assert_current = assert_current
        self.dim = int(manifest["model"]["dim"])
        self.shape = (int(manifest["next_row_ref"]), self.dim)
        self._agrep_commit_generation = str(manifest["generation"])
        self.mappings = []
        try:
            for segment in manifest["segments"]:
                rows = int(segment["rows"])
                path = embedding_segments.artifact_path(
                    manifest, segment["artifacts"]["f32"])
                mapping = np.memmap(
                    path, dtype="<f4", mode="r", shape=(rows, self.dim))
                self.mappings.append((int(segment["row_base"]), mapping))
        except Exception as exc:
            self.close()
            if isinstance(exc, FileNotFoundError):
                raise SegmentArtifactMoved(
                    "segmented semantic matrix was republished while opening") from exc
            if isinstance(exc, (OSError, TypeError, ValueError)):
                raise SegmentIntegrityError(
                    "segmented semantic matrix is unreadable") from exc
            raise

    def __matmul__(self, query) -> np.ndarray:
        self.assert_current()
        vector = np.asarray(query, dtype=np.float32).reshape(-1)
        if len(vector) != self.dim or not np.all(np.isfinite(vector)):
            raise SegmentQueryError("semantic query does not match segmented vectors")
        scores = np.full(self.shape[0], np.finfo(np.float32).min, dtype=np.float32)
        for row_base, mapping in self.mappings:
            block = np.asarray(mapping @ vector, dtype=np.float32)
            scores[row_base:row_base + len(mapping)] = block
        scores[~self.live] = np.finfo(np.float32).min
        if not np.all(np.isfinite(scores)):
            raise SegmentIntegrityError("segmented semantic scores are non-finite")
        self.assert_current()
        return scores

    def close(self) -> None:
        for _, mapping in getattr(self, "mappings", ()):
            common.close_embedding_matrix(mapping)
        self.mappings = []


class SegmentRefStore:
    def __init__(self, manifest: embedding_segments.LoadedManifest,
                 live: np.ndarray, assert_current, corpus_connect=None):
        self.manifest = manifest
        self.live = live
        self.rows = int(manifest["next_row_ref"])
        self.assert_current = assert_current
        self.corpus_connect = corpus_connect or self._connect_corpus
        self.corpus = None
        self._corpus_has_session_index = None
        self._corpus_has_digest = None
        self.segments = []
        self.bases = []
        self._q8_eligibility_cache = {}
        self._integrity_drops: set[int] = set()
        self._absent_drops: set[int] = set()
        self._resolve_considered = 0
        try:
            for segment in manifest["segments"]:
                path = embedding_segments.artifact_path(
                    manifest, segment["artifacts"]["refs"])
                db = embedding_segments._open_refs(path)
                register_metadata_functions(db)
                base, rows = int(segment["row_base"]), int(segment["rows"])
                self.bases.append(base)
                self.segments.append((base, rows, db))
        except Exception as exc:
            self.close()
            if isinstance(exc, FileNotFoundError):
                raise SegmentArtifactMoved(
                    "segmented semantic refs were republished while opening") from exc
            if isinstance(exc, (
                    OSError, TypeError, ValueError, sqlite3.DatabaseError)):
                raise SegmentIntegrityError(
                    "segmented semantic refs are unreadable") from exc
            raise

    @staticmethod
    def _connect_corpus():
        import corpusdb
        # Semantic resolution is an interactive reader too. Text-hash checks
        # below reject stale row mismatches; it must never enter the FTS writer
        # lane merely because a daemon-owned refresh is still converging.
        return corpusdb.connect(quiet=True, allow_stale=True)

    def _row_ref(self, value) -> int:
        try:
            row_ref = int(value)
        except (TypeError, ValueError) as exc:
            raise SegmentIntegrityError("semantic row reference is invalid") from exc
        if row_ref < 0 or row_ref >= self.rows or not self.live[row_ref]:
            raise SegmentIntegrityError("semantic row reference is not live")
        return row_ref

    def _segment(self, row_ref: int):
        index = bisect_right(self.bases, row_ref) - 1
        if index < 0:
            raise SegmentIntegrityError("semantic row reference has no segment")
        base, rows, db = self.segments[index]
        if row_ref >= base + rows:
            raise SegmentIntegrityError("semantic row reference falls in a gap")
        return base, db

    def eligible(self, filters: dict | None) -> np.ndarray:
        self.assert_current()
        output = []
        try:
            for _, _, db in self.segments:
                where, params = metadata_where(filters)
                output.extend(int(row[0]) for row in db.execute(
                    f"SELECT row_ref FROM refs WHERE {where} ORDER BY row_ref", params)
                    if self.live[int(row[0])])
        except (IndexError, sqlite3.DatabaseError) as exc:
            raise SegmentIntegrityError("segmented refs filter failed") from exc
        self.assert_current()
        return np.asarray(output, dtype=np.int64)

    def family_id_for_sessions(self, sessions) -> int | None:
        self.assert_current()
        wanted = sorted({str(session) for session in sessions if session})
        if not wanted:
            return None
        found: set[int] = set()
        try:
            for start in range(0, len(wanted), 500):
                page = wanted[start:start + 500]
                marks = ",".join("?" for _ in page)
                role_marks = ",".join(
                    "?" for _ in common.SEMANTIC_DEFAULT_EXCLUDED_ROLES)
                for _, _, db in self.segments:
                    for row_ref, family_id in db.execute(
                            "SELECT row_ref,family_id FROM refs "
                            f"WHERE session IN ({marks}) "
                            f"AND who NOT IN ({role_marks})",
                            [*page, *common.SEMANTIC_DEFAULT_EXCLUDED_ROLES]):
                        if self.live[int(row_ref)]:
                            found.add(int(family_id))
        except (IndexError, sqlite3.DatabaseError) as exc:
            raise SegmentIntegrityError(
                "segmented refs family lookup failed") from exc
        self.assert_current()
        if len(found) > 1:
            raise SegmentIntegrityError(
                "one conversation family maps to multiple semantic groups")
        return next(iter(found)) if found else None

    def family_id_for_session(self, session: str) -> int | None:
        return self.family_id_for_sessions((session,))

    def q8_eligibility(self, filters: dict | None):
        self.assert_current()
        key = json.dumps(filters or {}, sort_keys=True, separators=(",", ":"),
                         default=str)
        cached = self._q8_eligibility_cache.get(key)
        if cached is not None:
            return cached
        import semantic_q8
        bits = np.zeros((self.rows + 7) // 8, dtype=np.uint8)
        families: set[int] = set()
        count = 0
        try:
            for _, _, db in self.segments:
                where, params = metadata_where(filters)
                cursor = db.execute(
                    f"SELECT row_ref,family_id FROM refs WHERE {where} "
                    "ORDER BY row_ref", params)
                while batch := cursor.fetchmany(65_536):
                    refs = np.fromiter(
                        (int(row[0]) for row in batch), dtype=np.int64,
                        count=len(batch))
                    valid = self.live[refs]
                    refs = refs[valid]
                    if not len(refs):
                        continue
                    masks = np.left_shift(
                        np.uint8(1), np.asarray(refs & 7, dtype=np.uint8))
                    np.bitwise_or.at(bits, refs >> 3, masks)
                    count += len(refs)
                    families.update(int(batch[i][1])
                                    for i in np.flatnonzero(valid))
        except (IndexError, sqlite3.DatabaseError) as exc:
            raise SegmentIntegrityError("segmented refs filter failed") from exc
        self.assert_current()
        result = (semantic_q8.PackedEligibility(bits, self.rows, count),
                  count, len(families))
        if len(self._q8_eligibility_cache) >= 4:
            self._q8_eligibility_cache.pop(next(iter(self._q8_eligibility_cache)))
        self._q8_eligibility_cache[key] = result
        return result

    def best_by_session(self, similarities: np.ndarray,
                        filters: dict | None) -> dict[str, int]:
        self.assert_current()
        scores = np.asarray(similarities, dtype=np.float32).reshape(-1)
        if len(scores) != self.rows or not np.all(np.isfinite(scores)):
            raise SegmentIntegrityError("semantic similarities are misaligned")
        best = {}
        try:
            for _, _, db in self.segments:
                where, params = metadata_where(filters)
                for raw_ref, raw_session in db.execute(
                        f"SELECT row_ref,session FROM refs WHERE {where} ORDER BY row_ref",
                        params):
                    row_ref, session = int(raw_ref), str(raw_session or "")
                    if not session or not self.live[row_ref]:
                        continue
                    previous = best.get(session)
                    if previous is None or scores[row_ref] > scores[previous]:
                        best[session] = row_ref
        except (IndexError, sqlite3.DatabaseError) as exc:
            raise SegmentIntegrityError("segmented refs grouping failed") from exc
        self.assert_current()
        return best

    def _metadata(self, requested: list[int]) -> dict[int, dict]:
        grouped = {}
        for row_ref in requested:
            _, db = self._segment(row_ref)
            grouped.setdefault(db, []).append(row_ref)
        found = {}
        fields = ("row_ref", "mid", "text_hash", "agent", "project", "session",
                  "ts", "turn", "who", "model", "family_id")
        try:
            for db, values in grouped.items():
                marks = ",".join("?" for _ in values)
                has_model_source = embedding_segments._refs_have_model_source(db)
                has_side = embedding_segments._refs_have_side(db)
                columns = (
                    "row_ref,mid,text_hash,agent,project,session,ts,turn,who,"
                    "model,family_id"
                )
                if has_model_source:
                    columns += ",model_source"
                if has_side:
                    columns += ",side"
                rows = db.execute(
                    f"SELECT {columns} FROM refs "
                    f"WHERE row_ref IN ({marks})", values)
                for row in rows:
                    record = dict(zip(fields, row[:len(fields)], strict=True))
                    offset = len(fields)
                    record["model_source"] = (
                        str(row[offset]) if has_model_source else "unknown")
                    offset += int(has_model_source)
                    record["side"] = bool(row[offset]) if has_side else False
                    row_ref = self._row_ref(record["row_ref"])
                    found[row_ref] = record
        except sqlite3.DatabaseError as exc:
            raise SegmentIntegrityError("segmented refs resolution failed") from exc
        if set(found) != set(requested):
            raise SegmentIntegrityError("segmented refs lost a requested row")
        return found

    def _connect_current_corpus(self):
        deadline = time.monotonic() + _CORPUS_CONNECT_RETRY_S
        while self.corpus is None:
            self.corpus = self.corpus_connect()
            if self.corpus is not None:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(_CORPUS_CONNECT_POLL_S, remaining))
        return self.corpus

    def resolve(self, rowrefs) -> list[dict]:
        self.assert_current()
        requested = [self._row_ref(value) for value in rowrefs]
        if not requested:
            return []
        metadata = self._metadata(requested)
        if self.corpus is None and self._connect_current_corpus() is None:
            reason = (surface.SEMANTIC_INDEX_UPDATE_REASON
                      if corpus_update_active() else
                      "current corpus database is unavailable")
            raise SegmentQueryError(reason)
        found = {}
        try:
            if self._corpus_has_session_index is None:
                self._corpus_has_session_index = any(
                    row[1] == "msgs_session"
                    for row in self.corpus.execute("PRAGMA index_list(msgs)"))
            if self._corpus_has_digest is None:
                self._corpus_has_digest = any(
                    row[1] == "content_digest"
                    for row in self.corpus.execute("PRAGMA table_info(msgs)"))
            index_hint = (
                " INDEXED BY msgs_session"
                if self._corpus_has_session_index else "")
            unique = list(dict.fromkeys(requested))
            observed: dict[int, list[tuple[str, object]]] = {}
            for start in range(0, len(unique), 150):
                page = unique[start:start + 150]
                values = ",".join("(?,?,?,?,?)" for _ in page)
                params = []
                for row_ref in page:
                    record = metadata[row_ref]
                    params.extend((
                        row_ref, record["agent"], record["session"],
                        int(record["turn"]), record["who"]))
                rows = self.corpus.execute(
                    "WITH wanted(row_ref,agent,session,turn,who) AS "
                    f"(VALUES {values}) "
                    "SELECT wanted.row_ref,msgs.text"
                    + (",msgs.content_digest" if self._corpus_has_digest else ",NULL")
                    + " FROM wanted "
                    f"JOIN msgs{index_hint} "
                    "ON msgs.agent=wanted.agent AND msgs.session=wanted.session "
                    "AND msgs.turn=wanted.turn AND msgs.who=wanted.who",
                    params)
                for raw_ref, raw_text, raw_digest in rows:
                    observed.setdefault(int(raw_ref), []).append(
                        (str(raw_text), raw_digest))
            self._resolve_considered += len(unique)
            for row_ref in unique:
                record = metadata[row_ref]
                candidates = observed.get(row_ref, ())
                matches = [(text, digest) for text, digest in candidates
                           if _text_hash(text) == record["text_hash"]]
                if not matches or len({text for text, _digest in matches}) != 1:
                    # No corpus row at all is coverage lag in a derived mirror
                    # read stale by design; only a present-but-different text
                    # is evidence about what was embedded.
                    if not candidates:
                        self._absent_drops.add(row_ref)
                    else:
                        self._integrity_drops.add(row_ref)
                    continue
                text, digest = matches[0]
                try:
                    digest = (compact.content_digest(text) if digest is None else
                              compact.require_content_digest(digest))
                except compact.CompactError as exc:
                    raise SegmentQueryError(
                        "current corpus content digest is invalid") from exc
                found[row_ref] = {
                    # A '#cN' chunk hit maps back to its logical row: callers
                    # see the base id, never the chunk-vector store id.
                    "ordinal": row_ref,
                    "mid": common.semantic_chunk_split(record["mid"])[0],
                    "agent": record["agent"], "project": record["project"],
                    "session": record["session"], "ts": int(record["ts"]),
                    "turn": int(record["turn"]), "who": record["who"],
                    "model": record["model"] or "",
                    "model_source": record["model_source"],
                    "family_id": int(record["family_id"]),
                    "side": bool(record["side"]),
                    "text": text, "content_digest": digest,
                }
        except sqlite3.DatabaseError as exc:
            try:
                import corpusdb
                corpusdb.record_query_database_error(exc, self.corpus)
            except (ImportError, OSError):
                pass
            raise SegmentQueryError("current corpus resolution failed") from exc
        self.assert_current()
        return [found[row_ref] for row_ref in requested if row_ref in found]


    def take_integrity_disclosure(self) -> dict | None:
        mismatched = len(self._integrity_drops)
        absent = len(self._absent_drops)
        considered = self._resolve_considered
        self._integrity_drops.clear()
        self._absent_drops.clear()
        self._resolve_considered = 0
        if not (mismatched or absent):
            return None
        if not mismatched:
            return {
                "state": "rows-uncorroborated",
                "dropped": absent,
                "mismatched": 0,
                "absent": absent,
                "considered": considered,
                "reason": "the derived corpus has not mirrored these rows yet",
                "repair": "corpus-refresh-pending",
            }
        return {
            "state": "rows-dropped",
            "dropped": mismatched + absent,
            "mismatched": mismatched,
            "absent": absent,
            "considered": considered,
            "reason": "semantic text proof failed",
            "repair": "full-rebuild-requested",
        }

    def close(self) -> None:
        for _, _, db in getattr(self, "segments", ()):
            try:
                db.close()
            except sqlite3.DatabaseError:
                pass
        self.segments = []
        self._q8_eligibility_cache.clear()
        self._integrity_drops.clear()
        self._absent_drops.clear()
        self._resolve_considered = 0
        if self.corpus is not None:
            try:
                self.corpus.close()
            except sqlite3.DatabaseError:
                pass
            self.corpus = None


class _SegmentIndex:
    def __init__(
            self, manifest: embedding_segments.LoadedManifest,
            artifact_stamp: tuple[
                tuple[str, tuple[int, int, int, int, int]], ...],
    ):
        self.manifest = manifest
        self.live = _liveness(manifest)
        self._matrix = None
        try:
            self._artifact_identities = tuple(
                (Path(path), identity) for path, identity in artifact_stamp)
        except (TypeError, ValueError) as exc:
            raise SegmentIntegrityError(
                "verified semantic artifact identity is unavailable") from exc
        self.refs = SegmentRefStore(
            manifest, self.live, self.assert_artifacts_current)

    def assert_current(self) -> None:
        try:
            current = common.transcript_generation()
        except (OSError, RuntimeError, ValueError) as exc:
            raise SegmentQueryError("semantic source generation is unavailable") from exc
        if current != self.manifest["source"]:
            raise SegmentQueryError("segmented semantic source is stale")

    def assert_artifacts_current(self) -> None:
        self.assert_current()
        for path, expected in self._artifact_identities:
            try:
                current = embedding_segments._file_identity(path)
            except FileNotFoundError as exc:
                raise SegmentArtifactMoved(
                    "active semantic artifact was republished under this reader"
                ) from exc
            except OSError as exc:
                raise SegmentIntegrityError(
                    "active semantic artifact identity is unreadable") from exc
            if current == expected:
                continue
            if _identity_republished(expected, current):
                raise SegmentArtifactMoved(
                    "active semantic artifact was republished under this reader")
            raise SegmentIntegrityError(
                "active semantic artifact moved after integrity verification")

    def assert_matrix_current(self) -> None:
        try:
            self.assert_artifacts_current()
        except SegmentIntegrityError as exc:
            raise SegmentIntegrityError(
                "active semantic matrix moved after integrity verification") from exc

    def matrix(self) -> SegmentedMatrix:
        if self._matrix is None:
            self._matrix = SegmentedMatrix(
                self.manifest, self.live, self.assert_matrix_current)
        return self._matrix

    def coverage(self) -> dict:
        raw = self.manifest["coverage"]
        indexed, total = int(raw["indexed"]), int(raw["total"])
        return {
            "indexed": indexed, "total": total,
            "pending": int(raw["pending"]),
            "fraction": round(indexed / total, 6),
            "complete": bool(raw["complete"]), "order": str(raw["order"]),
        }

    def close(self) -> None:
        self.refs.close()
        if self._matrix is not None:
            self._matrix.close()
            self._matrix = None


_CACHE = {"stamp": None, "artifact_stamp": None, "index": None}


def is_segmented(meta_path: Path) -> bool:
    try:
        record = json.loads(Path(meta_path).read_text(encoding="utf-8"))
        return isinstance(record, dict) and record.get("version") == 2
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _stamp(path: Path) -> tuple[int, int, int, int, bytes]:
    stat = path.stat()
    digest = hashlib.blake2b(path.read_bytes(), digest_size=8).digest()
    return stat.st_mtime_ns, stat.st_size, stat.st_dev, stat.st_ino, digest


def _artifact_stamp(
        manifest: embedding_segments.LoadedManifest,
) -> tuple[tuple[str, tuple[int, int, int, int, int]], ...]:
    paths = embedding_segments.referenced_paths(manifest)
    return tuple(sorted(
        (str(path), embedding_segments._file_identity(path))
        for path in paths))


def _artifact_descriptors(
        manifest: embedding_segments.LoadedManifest,
) -> dict[Path, dict]:
    descriptors = {}
    for segment in manifest["segments"]:
        for descriptor in segment["artifacts"].values():
            descriptors[embedding_segments.artifact_path(
                manifest, descriptor)] = descriptor
    for shadow in manifest["shadows"]:
        descriptor = shadow["artifact"]
        descriptors[embedding_segments.artifact_path(
            manifest, descriptor)] = descriptor
    descriptor = manifest["set_manifest"]
    descriptors[embedding_segments.artifact_path(
        manifest, descriptor)] = descriptor
    descriptor = manifest.get(embedding_segments.PROOF_KEY)
    if descriptor is not None:
        descriptors[embedding_segments.artifact_path(
            manifest, descriptor)] = descriptor
    return descriptors


def _integrity_receipt_path(meta_path: Path) -> Path:
    return meta_path.with_name(".semantic-integrity-cache.json")


def _read_integrity_receipt(meta_path: Path) -> dict:
    try:
        snapshot = ownerfile.snapshot(
            _integrity_receipt_path(meta_path),
            max_bytes=_INTEGRITY_RECEIPT_MAX_BYTES)
        record = json.loads(snapshot.raw)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(record, dict) or record.get("version") != 1:
        return {}
    artifacts = record.get("artifacts")
    return artifacts if isinstance(artifacts, dict) else {}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _write_integrity_receipt(meta_path: Path, artifacts: dict) -> None:
    refusal = _mutation_refusal_reason()
    if refusal is not None:
        common.log(f"semantic integrity receipt skipped: {refusal}")
        return
    path = _integrity_receipt_path(meta_path)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}")
    payload = json.dumps(
        {"version": 1, "artifacts": artifacts},
        sort_keys=True, separators=(",", ":"))
    try:
        temporary.write_text(payload, encoding="utf-8")
        def verify_receipt_publish() -> None:
            reason = _mutation_refusal_reason()
            if reason is not None:
                raise PermissionError(reason)

        common.replace_with_retry(
            temporary, path, before_attempt=verify_receipt_publish)
    except PermissionError as exc:
        common.log(f"semantic integrity receipt skipped: {exc}")
    except OSError:
        pass
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _verify_active_artifacts(
        manifest: embedding_segments.LoadedManifest,
) -> tuple[tuple[str, tuple[int, int, int, int, int]], ...]:
    descriptors = _artifact_descriptors(manifest)
    receipt = _read_integrity_receipt(manifest.path)
    before = _artifact_stamp(manifest)
    identities = dict(before)
    verified = {}
    for path, descriptor in descriptors.items():
        key = str(path)
        identity = list(identities[key])
        expected = str(descriptor["sha256"])
        cached = receipt.get(key)
        if not (isinstance(cached, dict)
                and cached.get("sha256") == expected
                and cached.get("identity") == identity):
            if _sha256_file(path) != expected:
                raise SegmentIntegrityError(
                    f"active semantic artifact digest mismatch: {path}")
        verified[key] = {"sha256": expected, "identity": identity}
    after = _artifact_stamp(manifest)
    if before != after:
        if _stamp_republished(before, after):
            raise SegmentArtifactMoved(
                "semantic artifacts were republished during integrity verification")
        raise SegmentIntegrityError(
            "semantic artifacts moved during integrity verification")
    _write_integrity_receipt(manifest.path, verified)
    return after


def _assert_settled(path: Path, before_meta, before_artifacts, after_artifacts) -> None:
    after_meta = _stamp(path)
    if before_meta != after_meta and _meta_republished(before_meta, after_meta):
        raise SegmentArtifactMoved(
            "a newer semantic generation was published during verification")
    if before_artifacts != after_artifacts and _stamp_republished(
            before_artifacts, after_artifacts):
        raise SegmentArtifactMoved(
            "semantic artifacts were republished during integrity verification")
    if before_meta != after_meta or before_artifacts != after_artifacts:
        raise SegmentIntegrityError(
            "semantic artifacts moved during integrity verification")


def _verified_manifest(path: Path):
    try:
        before_meta = _stamp(path)
        candidate = embedding_segments.load_manifest(path)
        before_artifacts = _artifact_stamp(candidate)
        proof = embedding_segments.publication_artifact_identities(candidate)
        if proof is not None:
            observed = dict(before_artifacts)
            changed = next((artifact for artifact, identity in proof.items()
                            if observed.get(str(artifact)) != identity), None)
            if changed is not None:
                if _current_identity_republished(
                        Path(changed), proof[changed]):
                    raise SegmentArtifactMoved(
                        f"semantic artifact was republished: {changed}")
                raise SegmentIntegrityError(
                    f"active semantic artifact changed after publication: {changed}")
            after_artifacts = _artifact_stamp(candidate)
            _assert_settled(path, before_meta, before_artifacts, after_artifacts)
            return candidate, before_meta, after_artifacts
        manifest = embedding_segments.load_manifest(
            path, validate_liveness=True)
        after_artifacts = _verify_active_artifacts(manifest)
        _assert_settled(path, before_meta, before_artifacts, after_artifacts)
        if candidate["generation"] != manifest["generation"]:
            raise SegmentArtifactMoved(
                "a newer semantic generation was published during verification")
        return manifest, before_meta, after_artifacts
    except (SegmentIntegrityError, SegmentArtifactMoved):
        raise
    except FileNotFoundError as exc:
        raise SegmentArtifactMoved(
            "a semantic artifact was republished during verification") from exc
    except (OSError, RuntimeError, TypeError, ValueError,
            embedding_segments.SegmentError) as exc:
        raise SegmentIntegrityError(
            "active semantic artifact integrity verification failed") from exc


def _open_current_once(path: Path, *, need_matrix: bool):
    stamp = _stamp(path)
    index = _CACHE["index"]
    artifact_stamp = None
    if index is not None:
        try:
            artifact_stamp = _artifact_stamp(index.manifest)
        except (OSError, RuntimeError, TypeError, ValueError,
                embedding_segments.SegmentError):
            # The cached generation was superseded; the meta names the live one.
            close_cache()
            index = None
    if (index is None or _CACHE["stamp"] != stamp
            or _CACHE["artifact_stamp"] != artifact_stamp):
        close_cache()
        manifest, stamp, artifact_stamp = _verified_manifest(path)
        index = _SegmentIndex(manifest, artifact_stamp)
        _CACHE.update(
            stamp=stamp, artifact_stamp=artifact_stamp, index=index)
    index.assert_current()
    return (index.manifest, index.matrix() if need_matrix else None,
            index.refs, index.coverage())


def open_current(meta_path: Path, *, need_matrix: bool = False):
    path = Path(meta_path)
    last: SegmentArtifactMoved | None = None
    for attempt in range(_REPUBLISH_ATTEMPTS):
        try:
            return _open_current_once(path, need_matrix=need_matrix)
        except SegmentArtifactMoved as exc:
            last = exc
            close_cache()
            time.sleep(0.005 * (attempt + 1))
    raise SegmentArtifactMoved(
        f"semantic generations kept being republished while opening: {last}"
    ) from last


def close_cache() -> None:
    index = _CACHE.get("index")
    if index is not None:
        index.close()
    _CACHE.update(stamp=None, artifact_stamp=None, index=None)
