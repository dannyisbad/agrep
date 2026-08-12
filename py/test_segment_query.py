from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import struct
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import numpy as np

import ask
import common
import embedding_segments
import segment_query
import semantic_q8
import surface_policy as surface


def _fnv(payload: bytes) -> int:
    value = 0xCBF29CE484222325
    for byte in payload:
        value ^= byte
        value = (value * 0x00000100000001B3) & 0xFFFFFFFFFFFFFFFF
    return value


def _inputs(root: Path, label: str, rows: list[dict]) -> tuple[dict, list, list]:
    dim = 2
    generation = hashlib.md5(label.encode("ascii")).digest()
    vectors = np.asarray([row["vector"] for row in rows], dtype="<f4")
    q8_payload = b"".join(struct.pack("<fbb", 1.0, 1, 0) for _ in rows)
    q8_header = bytearray(64)
    struct.pack_into("<4sIIIQ16sIIQ", q8_header, 0, b"AGQ8", 1, dim, 1,
                     len(rows), generation, dim + 4, 0, _fnv(q8_payload))
    group_payload = b"".join(struct.pack("<I", row["family_id"]) for row in rows)
    group_count = max(row["family_id"] for row in rows) + 1
    group_header = bytearray(64)
    struct.pack_into("<4sIIIQ16sIIQ", group_header, 0, b"AGQG", 1, 0,
                     group_count, len(rows), generation, 4, 0,
                     _fnv(group_payload))
    payloads = {
        "f32": vectors.tobytes(),
        "f16": vectors.astype("<f2").tobytes(),
        "q8": bytes(q8_header) + q8_payload,
        "groups": bytes(group_header) + group_payload,
    }
    artifacts = {}
    for name, payload in payloads.items():
        path = root / f"{label}.{name}"
        path.write_bytes(payload)
        artifacts[name] = path
    hashes = [hashlib.blake2b(
        row["text"].encode("utf-8"), digest_size=8).hexdigest() for row in rows]
    refs = [{
        "mid": row["mid"], "text_hash": text_hash,
        "agent": row["agent"], "project": row["project"],
        "session": row["session"], "ts": row["ts"], "turn": row["turn"],
        "who": row["who"], "model": row["model"],
        "model_source": row["model_source"],
        "family_id": row["family_id"],
        "family_label": (
            None if row["family_id"] == 0
            else f"f:family-{row['family_id']}"
        ),
        "side": bool(row["side"]),
        "metadata_hash": hashlib.blake2b(
            f"metadata:{row['mid']}".encode(), digest_size=16).hexdigest(),
    } for row, text_hash in zip(rows, hashes, strict=True)]
    return artifacts, hashes, refs


def _row(mid: str, text: str, vector, *, agent: str, project: str,
         session: str, ts: int, turn: int, who: str, model: str,
         family_id: int, model_source: str = "explicit",
         side: bool = False) -> dict:
    return {
        "mid": mid, "text": text, "vector": vector, "agent": agent,
        "project": project, "session": session, "ts": ts, "turn": turn,
        "who": who, "model": model, "model_source": model_source,
        "family_id": family_id, "side": side,
    }


def _corpus(path: Path, rows: list[dict]) -> None:
    db = sqlite3.connect(path)
    try:
        db.execute(
            "CREATE TABLE msgs(agent TEXT,project TEXT,session TEXT,ts INTEGER,"
            "turn INTEGER,who TEXT,model TEXT,model_source TEXT,text TEXT)")
        db.execute("CREATE INDEX msgs_session ON msgs(session,turn)")
        db.executemany(
            "INSERT INTO msgs VALUES(?,?,?,?,?,?,?,?,?)",
            [(row["agent"], row["project"], row["session"], row["ts"],
              row["turn"], row["who"], row["model"], row["model_source"],
              row["text"])
             for row in rows])
        db.commit()
    finally:
        db.close()


def _mutate_same_size(path: Path, before: bytes, after: bytes) -> None:
    if len(before) != len(after):
        raise AssertionError("mutation changed width")
    payload = path.read_bytes()
    offset = payload.find(before)
    if offset < 0:
        raise AssertionError(f"mutation target is absent from {path}")
    prior = path.stat()
    prior_identity = embedding_segments._file_identity(path)
    with path.open("r+b") as stream:
        stream.seek(offset)
        stream.write(after)
        stream.flush()
        os.fsync(stream.fileno())
    os.utime(path, ns=(prior.st_atime_ns, prior.st_mtime_ns))
    if embedding_segments._file_identity(path) == prior_identity:
        raise AssertionError("same-size mutation preserved artifact identity")


@contextmanager
def _temporary_root():
    with tempfile.TemporaryDirectory() as temporary:
        try:
            yield Path(temporary)
        finally:
            ask.clear_artifact_cache()
            segment_query.close_cache()


class SegmentQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        segment_query.close_cache()
        ask.clear_artifact_cache()

    def tearDown(self) -> None:
        segment_query.close_cache()
        ask.clear_artifact_cache()

    def test_manifest_cache_stamp_detects_same_metadata_replacement(self) -> None:
        with _temporary_root() as root:
            path = root / "embeddings.meta"
            path.write_bytes(b"aaaaaaaa")
            first = segment_query._stamp(path)
            modified = path.stat()
            path.write_bytes(b"bbbbbbbb")
            os.utime(path, ns=(modified.st_atime_ns, modified.st_mtime_ns))
            second = segment_query._stamp(path)
        self.assertEqual(first[:4], second[:4])
        self.assertNotEqual(first, second)

    def test_cached_query_rejects_same_size_active_sidecar_mutation(self) -> None:
        with _temporary_root() as root:
            source, meta, manifest, _, _ = self._fixture(root)
            with mock.patch.object(
                    common, "transcript_generation", return_value=source):
                first = segment_query.open_current(meta)
                orphan = meta.parent / embedding_segments.SEGMENT_DIR / "orphan.f16"
                orphan.write_bytes(b"unreferenced")
                self.assertIs(segment_query.open_current(meta)[2], first[2])
                segment_query.close_cache()
                with mock.patch.object(
                        segment_query, "_sha256_file",
                        side_effect=AssertionError("unchanged artifacts were rehashed")):
                    segment_query.open_current(meta)

                descriptor = manifest["segments"][-1]["artifacts"]["f16"]
                active = embedding_segments.artifact_path(manifest, descriptor)
                prior = active.stat()
                payload = active.read_bytes()
                midpoint = len(payload) // 2
                active.write_bytes(payload[midpoint:] + payload[:midpoint])
                os.utime(active, ns=(prior.st_atime_ns, prior.st_mtime_ns))

                with self.assertRaisesRegex(
                        segment_query.SegmentIntegrityError,
                        "artifact changed after publication"):
                    segment_query.open_current(meta)

    def test_publisher_proof_keeps_cold_open_bounded(self) -> None:
        with _temporary_root() as root:
            source, meta, _, _, _ = self._fixture(root)
            segment_query.close_cache()
            receipt = meta.with_name(".semantic-integrity-cache.json")
            with (
                mock.patch.object(
                    common, "transcript_generation", return_value=source),
                mock.patch.object(
                    embedding_segments, "_validate_active_rows_streaming",
                    side_effect=AssertionError("cold query scanned refs")),
                mock.patch.object(
                    segment_query, "_sha256_file",
                    side_effect=AssertionError("cold query hashed artifacts")),
            ):
                manifest, _, refs, coverage = segment_query.open_current(meta)
            self.assertEqual(coverage["indexed"], manifest["live_rows"])
            self.assertEqual(refs.rows, manifest["next_row_ref"])
            self.assertFalse(receipt.exists())

    def test_legacy_integrity_receipt_reads_valid_bounded_record(self) -> None:
        with _temporary_root() as root:
            meta = root / "embeddings.meta"
            artifacts = {"artifact": {"sha256": "a" * 64, "identity": [1, 2]}}
            receipt = meta.with_name(".semantic-integrity-cache.json")
            receipt.write_text(json.dumps({
                "version": 1, "artifacts": artifacts,
            }), encoding="utf-8")

            self.assertEqual(
                segment_query._read_integrity_receipt(meta), artifacts)

    def test_legacy_integrity_receipt_rejects_oversized_record(self) -> None:
        with _temporary_root() as root:
            meta = root / "embeddings.meta"
            receipt = meta.with_name(".semantic-integrity-cache.json")
            receipt.write_bytes(b"{}")

            with mock.patch.object(
                    segment_query, "_INTEGRITY_RECEIPT_MAX_BYTES", 1):
                self.assertEqual(
                    segment_query._read_integrity_receipt(meta), {})

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO contract is POSIX-only")
    def test_legacy_integrity_receipt_rejects_nonregular_entries(self) -> None:
        with _temporary_root() as root:
            meta = root / "embeddings.meta"
            receipt = meta.with_name(".semantic-integrity-cache.json")
            os.mkfifo(receipt)
            self.assertEqual(segment_query._read_integrity_receipt(meta), {})

            receipt.unlink()
            target = root / "receipt-target.json"
            target.write_text(
                '{"version":1,"artifacts":{"trusted":{}}}',
                encoding="utf-8")
            receipt.symlink_to(target)
            self.assertEqual(segment_query._read_integrity_receipt(meta), {})

    def test_legacy_generation_keeps_exhaustive_cold_fallback(self) -> None:
        with _temporary_root() as root:
            source, meta, _, _, _ = self._fixture(root)
            record = json.loads(meta.read_bytes())
            record.pop(embedding_segments.PROOF_KEY)
            meta.write_bytes(embedding_segments._canonical(record))
            segment_query.close_cache()
            with (
                mock.patch.object(
                    common, "transcript_generation", return_value=source),
                mock.patch.object(
                    embedding_segments, "_validate_active_rows_streaming",
                    wraps=embedding_segments._validate_active_rows_streaming) as scan,
                mock.patch.object(
                    segment_query, "_sha256_file",
                    wraps=segment_query._sha256_file) as digest,
            ):
                segment_query.open_current(meta)
            self.assertEqual(scan.call_count, 1)
            self.assertGreater(digest.call_count, 0)

    def test_matrix_rejects_row_swap_between_score_boundary_checks(self) -> None:
        with _temporary_root() as root:
            source = {"ingest_signature": "current"}
            rows = [
                _row("a", "alpha", [1.0, 0.0], agent="codex",
                     project="alpha", session="s-a", ts=100, turn=1,
                     who="user", model="gpt-5", family_id=0),
                _row("b", "beta", [0.0, 1.0], agent="codex",
                     project="beta", session="s-b", ts=200, turn=2,
                     who="assistant", model="gpt-5", family_id=1),
            ]
            artifacts, hashes, refs = _inputs(root, "base", rows)
            meta = root / "embeddings.meta"
            manifest = embedding_segments.publish_base(
                meta, source=source, model_id="model", dim=2,
                artifacts=artifacts, ids=[row["mid"] for row in rows],
                hashes=hashes, refs=refs, coverage={"total": 2})
            query = np.asarray([1.0, 0.0], dtype=np.float32)

            with mock.patch.object(
                    common, "transcript_generation", return_value=source):
                _, matrix, _, _ = segment_query.open_current(
                    meta, need_matrix=True)
                np.testing.assert_allclose(matrix @ query, [1.0, 0.0])

                orphan = meta.parent / embedding_segments.SEGMENT_DIR / "orphan.f32"
                orphan.write_bytes(np.asarray([[9.0, 9.0]], dtype="<f4").tobytes())
                np.testing.assert_allclose(matrix @ query, [1.0, 0.0])

                active = embedding_segments.artifact_path(
                    manifest, manifest["segments"][0]["artifacts"]["f32"])
                prior, original_assert = active.stat(), matrix.assert_current
                prior_identity = embedding_segments._file_identity(active)
                mutated = False

                def mutate_after_precheck() -> None:
                    nonlocal mutated
                    original_assert()
                    if mutated:
                        return
                    swapped = np.asarray(
                        [[0.0, 1.0], [1.0, 0.0]], dtype="<f4").tobytes()
                    with active.open("r+b") as stream:
                        stream.write(swapped)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.utime(active, ns=(prior.st_atime_ns, prior.st_mtime_ns))
                    self.assertNotEqual(
                        embedding_segments._file_identity(active), prior_identity)
                    mutated = True

                matrix.assert_current = mutate_after_precheck

                with self.assertRaisesRegex(
                        segment_query.SegmentIntegrityError,
                        "matrix moved after integrity verification"):
                    matrix @ query
                self.assertTrue(mutated)

    def test_resolve_rejects_refs_poison_between_boundary_checks(self) -> None:
        with _temporary_root() as root:
            source, meta, manifest, corpus, _ = self._fixture(root)
            active = embedding_segments.artifact_path(
                manifest, manifest["segments"][-1]["artifacts"]["refs"])
            with mock.patch.object(
                    common, "transcript_generation", return_value=source):
                _, _, refs, _ = segment_query.open_current(meta)
                refs.corpus_connect = lambda: sqlite3.connect(corpus)
                self.assertEqual(refs.resolve([2])[0]["project"], "alpha")
                orphan = active.with_name("orphan.refs")
                orphan.write_bytes(b"alpha")
                _mutate_same_size(orphan, b"alpha", b"omega")
                self.assertEqual(refs.resolve([2])[0]["project"], "alpha")

                segment_query.close_cache()
                _, _, refs, _ = segment_query.open_current(meta)
                refs.corpus_connect = lambda: sqlite3.connect(corpus)
                original_assert, mutated = refs.assert_current, False

                def mutate_after_precheck() -> None:
                    nonlocal mutated
                    original_assert()
                    if not mutated:
                        _mutate_same_size(active, b"alpha", b"omega")
                        mutated = True

                refs.assert_current = mutate_after_precheck
                with self.assertRaisesRegex(
                        segment_query.SegmentIntegrityError,
                        "active semantic artifact moved"):
                    refs.resolve([2])
                self.assertTrue(mutated)
                probe = embedding_segments._open_refs(active)
                try:
                    self.assertEqual(
                        probe.execute(
                            "SELECT project FROM refs WHERE row_ref=2").fetchone()[0],
                        "omega")
                finally:
                    probe.close()

    def test_q8_eligibility_rejects_q8_mutation_before_cache(self) -> None:
        with _temporary_root() as root:
            source, meta, manifest, _, _ = self._fixture(root)
            active = embedding_segments.artifact_path(
                manifest, manifest["segments"][-1]["artifacts"]["q8"])
            with mock.patch.object(
                    common, "transcript_generation", return_value=source):
                _, _, refs, _ = segment_query.open_current(meta)
                _, count, families = refs.q8_eligibility({"who": "user"})
                self.assertEqual((count, families), (1, 1))

                segment_query.close_cache()
                _, _, refs, _ = segment_query.open_current(meta)
                original_assert, mutated = refs.assert_current, False

                def mutate_after_precheck() -> None:
                    nonlocal mutated
                    original_assert()
                    if not mutated:
                        _mutate_same_size(
                            active, struct.pack("<fbb", 1.0, 1, 0),
                            struct.pack("<fbb", -1.0, 1, 0))
                        mutated = True

                refs.assert_current = mutate_after_precheck
                with self.assertRaisesRegex(
                        segment_query.SegmentIntegrityError,
                        "active semantic artifact moved"):
                    refs.q8_eligibility({"who": "user"})
                self.assertTrue(mutated)
                self.assertEqual(refs._q8_eligibility_cache, {})

    def test_frozen_liveness_rejects_shadow_mutation_after_open(self) -> None:
        with _temporary_root() as root:
            source, meta, manifest, _, _ = self._fixture(root)
            active = embedding_segments.artifact_path(
                manifest, manifest["shadows"][-1]["artifact"])
            with mock.patch.object(
                    common, "transcript_generation", return_value=source):
                _, _, refs, _ = segment_query.open_current(meta)
                np.testing.assert_array_equal(refs.eligible(None), [2, 3])

                segment_query.close_cache()
                _, _, refs, _ = segment_query.open_current(meta)
                original_assert, mutated = refs.assert_current, False

                def mutate_after_precheck() -> None:
                    nonlocal mutated
                    original_assert()
                    if not mutated:
                        _mutate_same_size(
                            active, struct.pack("<Q", 0), struct.pack("<Q", 1))
                        mutated = True

                refs.assert_current = mutate_after_precheck
                with self.assertRaisesRegex(
                        segment_query.SegmentIntegrityError,
                        "active semantic artifact moved"):
                    refs.eligible(None)
                self.assertTrue(mutated)

    def _fixture(self, root: Path):
        source = {"ingest_signature": "current"}
        base_rows = [
            _row("a", "old alpha", [1.0, 0.0], agent="codex", project="alpha",
                 session="s-a", ts=100, turn=1, who="user", model="gpt-5",
                 family_id=0),
            _row("b", "deleted beta", [0.0, 1.0], agent="codex", project="beta",
                 session="s-b", ts=200, turn=2, who="assistant", model="gpt-5",
                 family_id=1),
        ]
        artifacts, hashes, refs = _inputs(root, "base", base_rows)
        meta = root / "embeddings.meta"
        base = embedding_segments.publish_base(
            meta, source=source, model_id="model", dim=2, artifacts=artifacts,
            ids=[row["mid"] for row in base_rows], hashes=hashes, refs=refs,
            coverage={"total": 2})
        live_rows = [
            _row("a", "current alpha", [0.8, 0.2], agent="codex", project="alpha",
                 session="s-a", ts=300, turn=3, who="user", model="gpt-5",
                 family_id=0),
            _row("c", "current gamma", [0.6, 0.4], agent="claude", project="beta",
                 session="s-c", ts=400, turn=4, who="assistant", model="sonnet",
                 family_id=2, side=True),
        ]
        artifacts, hashes, refs = _inputs(root, "delta", live_rows)
        manifest = embedding_segments.publish_delta(
            meta, source=source, artifacts=artifacts,
            ids=[row["mid"] for row in live_rows], hashes=hashes, refs=refs,
            shadows=[0, 1], coverage={"total": 2},
            expected_generation=base["generation"])
        corpus = root / "corpus.db"
        _corpus(corpus, live_rows)
        return source, meta, manifest, corpus, live_rows

    def test_corpus_acquisition_retries_one_transient_miss(self) -> None:
        with _temporary_root() as root:
            source, meta, _, corpus, _ = self._fixture(root)
            clock = [0.0]
            calls = 0

            def connect():
                nonlocal calls
                calls += 1
                return None if calls == 1 else sqlite3.connect(corpus)

            def sleep(delay: float) -> None:
                clock[0] += delay

            with mock.patch.object(
                    common, "transcript_generation", return_value=source), \
                    mock.patch.object(
                        segment_query.time, "monotonic",
                        side_effect=lambda: clock[0]), \
                    mock.patch.object(
                        segment_query.time, "sleep", side_effect=sleep):
                _, _, refs, _ = segment_query.open_current(meta)
                refs.corpus_connect = connect
                resolved = refs.resolve([2])
        self.assertEqual(calls, 2)
        self.assertLessEqual(clock[0], segment_query._CORPUS_CONNECT_RETRY_S)
        self.assertEqual(resolved[0]["text"], "current alpha")

    def test_corpus_acquisition_failure_stays_bounded(self) -> None:
        with _temporary_root() as root:
            source, meta, _, _, _ = self._fixture(root)
            clock = [0.0]
            calls = 0

            def connect():
                nonlocal calls
                calls += 1
                return None

            def sleep(delay: float) -> None:
                clock[0] += delay

            with mock.patch.object(
                    common, "transcript_generation", return_value=source), \
                    mock.patch.object(
                        segment_query.time, "monotonic",
                        side_effect=lambda: clock[0]), \
                    mock.patch.object(
                        segment_query.time, "sleep", side_effect=sleep), \
                    mock.patch.object(
                        segment_query, "corpus_update_active",
                        return_value=False):
                _, _, refs, _ = segment_query.open_current(meta)
                refs.corpus_connect = connect
                with self.assertRaisesRegex(
                        segment_query.SegmentQueryError,
                        "current corpus database is unavailable"):
                    refs.resolve([2])
        self.assertGreater(calls, 1)
        self.assertLessEqual(
            clock[0], segment_query._CORPUS_CONNECT_RETRY_S + 1e-9)

    def test_corpus_acquisition_names_a_live_update(self) -> None:
        with _temporary_root() as root:
            source, meta, _, _, _ = self._fixture(root)
            clock = [0.0]

            with mock.patch.object(
                    common, "transcript_generation", return_value=source), \
                    mock.patch.object(
                        segment_query.time, "monotonic",
                        side_effect=lambda: clock[0]), \
                    mock.patch.object(
                        segment_query.time, "sleep",
                        side_effect=lambda delay: clock.__setitem__(
                            0, clock[0] + delay)), \
                    mock.patch.object(
                        segment_query, "corpus_update_active",
                        return_value=True):
                _, _, refs, _ = segment_query.open_current(meta)
                refs.corpus_connect = lambda: None
                with self.assertRaisesRegex(
                        segment_query.SegmentQueryError,
                        surface.SEMANTIC_INDEX_UPDATE_REASON):
                    refs.resolve([2])

    def test_matrix_refs_filters_and_text_proof(self) -> None:
        with _temporary_root() as root:
            source, meta, _, corpus, _ = self._fixture(root)
            with mock.patch.object(common, "transcript_generation", return_value=source):
                _, matrix, refs, coverage = segment_query.open_current(
                    meta, need_matrix=True)
                refs.corpus_connect = lambda: sqlite3.connect(corpus)
                scores = matrix @ np.asarray([1.0, 0.0], dtype=np.float32)
                floor = np.finfo(np.float32).min
                np.testing.assert_array_equal(scores[:2], [floor, floor])
                np.testing.assert_allclose(scores[2:], [0.8, 0.6])
                self.assertEqual(coverage["indexed"], 2)
                np.testing.assert_array_equal(refs.eligible(None), [2, 3])
                self.assertEqual(refs.family_id_for_session("s-a"), 0)
                self.assertEqual(refs.family_id_for_session("s-c"), 2)
                self.assertIsNone(refs.family_id_for_session("missing"))
                np.testing.assert_array_equal(refs.eligible({
                    "agent": "COD", "project": "ALP", "chat": "S-",
                    "who": "user", "model": "GPT-5", "since_ms": 250,
                    "until_ms": 350,
                }), [2])
                np.testing.assert_array_equal(refs.eligible({
                    "model": "son", "model_soft": True,
                    "_exclude_who": ("user",),
                }), [3])
                np.testing.assert_array_equal(refs.eligible({
                    "exclude_session": "s-a",
                }), [3])
                np.testing.assert_array_equal(refs.eligible({
                    "exclude_session": "s-a",
                    "exclude_session_from_turn": 4,
                }), [2, 3])
                np.testing.assert_array_equal(refs.eligible({
                    "exclude_session": "s-a",
                    "exclude_session_from_turn": 3,
                }), [3])
                np.testing.assert_array_equal(refs.eligible({
                    "_exclude_family_id": 0,
                }), [3])
                self.assertTrue(refs.resolve([3])[0]["side"])
                np.testing.assert_array_equal(refs.eligible({
                    "_exclude_sessions": frozenset({"s-a"}),
                }), [3])
                np.testing.assert_array_equal(refs.eligible({
                    "_exclude_sessions": frozenset({"s-a", "s-c"}),
                }), [])
                packed, count, families = refs.q8_eligibility({
                    "agent": "cod", "who": "user",
                    "_exclude_sessions": frozenset({"s-c"}),
                })
                self.assertIsInstance(packed, semantic_q8.PackedEligibility)
                self.assertEqual((count, families), (1, 1))
                np.testing.assert_array_equal(
                    semantic_q8._eligibility_bits(packed, 4), [1 << 2])
                self.assertIs(refs.q8_eligibility({
                    "agent": "cod", "who": "user",
                    "_exclude_sessions": frozenset({"s-c"}),
                })[0], packed)
                self.assertEqual(refs.best_by_session(scores, None),
                                 {"s-a": 2, "s-c": 3})
                self.assertEqual(refs.best_by_session(scores, {
                    "_exclude_sessions": frozenset({"s-a"}),
                }), {"s-c": 3})
                self.assertEqual([row["text"] for row in refs.resolve([2, 3])],
                                 ["current alpha", "current gamma"])
                self.assertEqual(
                    [row["model_source"] for row in refs.resolve([2, 3])],
                    ["explicit", "explicit"],
                )
                with self.assertRaises(segment_query.SegmentQueryError):
                    refs.resolve([0])

            bad_corpus = root / "bad-corpus.db"
            bad = self._fixture_rows_with_bad_text()
            _corpus(bad_corpus, bad)
            segment_query.close_cache()
            with mock.patch.object(common, "transcript_generation", return_value=source):
                _, _, refs, _ = segment_query.open_current(meta)
                refs.corpus_connect = lambda: sqlite3.connect(bad_corpus)
                resolved = refs.resolve([2, 3])
                self.assertEqual(
                    [(row["ordinal"], row["text"]) for row in resolved],
                    [(3, "current gamma")])
                self.assertEqual(refs.take_integrity_disclosure(), {
                    "state": "rows-dropped",
                    "dropped": 1,
                    "mismatched": 1,
                    "absent": 0,
                    "considered": 2,
                    "reason": "semantic text proof failed",
                    "repair": "full-rebuild-requested",
                })
                self.assertIsNone(refs.take_integrity_disclosure())

    def test_family_lookup_ignores_the_global_excluded_role_group(self) -> None:
        db = sqlite3.connect(":memory:")
        db.execute(
            "CREATE TABLE refs("
            "row_ref INTEGER, session TEXT, family_id INTEGER, who TEXT)")
        db.executemany(
            "INSERT INTO refs VALUES(?,?,?,?)",
            (
                (0, "caller", 4, "user"),
                (1, "caller", 9, "control"),
            ),
        )
        refs = object.__new__(segment_query.SegmentRefStore)
        refs.live = np.ones(2, dtype=np.bool_)
        refs.segments = [(0, 2, db)]
        refs.assert_current = mock.Mock()
        try:
            self.assertEqual(refs.family_id_for_session("caller"), 4)
        finally:
            db.close()



    @staticmethod
    def _fixture_rows_with_bad_text() -> list[dict]:
        return [
            _row("a", "changed after publication", [0.8, 0.2], agent="codex",
                 project="alpha", session="s-a", ts=300, turn=3, who="user",
                 model="gpt-5", family_id=0),
            _row("c", "current gamma", [0.6, 0.4], agent="claude",
                 project="beta", session="s-c", ts=400, turn=4,
                 who="assistant", model="sonnet", family_id=2),
        ]

    def test_ask_uses_segmented_refs_for_q8_global_rowrefs(self) -> None:
        with _temporary_root() as root:
            source, meta, manifest, corpus, _ = self._fixture(root)
            embeddings_path = root / "embeddings.f32"
            filters = {"_exclude_who": tuple(sorted(
                ask._Q8_DEFAULT_EXCLUDED_ROLES))}
            q8_result = (
                np.asarray([2, 3], dtype=np.int64),
                np.asarray([0.9, 0.8], dtype=np.float32),
                np.asarray([0, 2], dtype=np.uint32),
                3,
            )
            with (mock.patch.object(common, "EMBEDDINGS_PATH", embeddings_path),
                  mock.patch.object(common, "DATA_DIR", root),
                  mock.patch.object(common, "transcript_generation", return_value=source),
                  mock.patch("semantic_q8.grouped_exact_candidates",
                             return_value=q8_result)):
                coverage = ask._require_current_message_index()
                self.assertEqual(ask._CURRENT_MESSAGE_STATE["generation"],
                                 manifest["generation"])
                ids, matrix, refs, artifact_coverage = ask._message_artifacts(
                    2, meta, allow_refs_build=False)
                self.assertEqual(ids, ())
                self.assertEqual(artifact_coverage, coverage)
                np.testing.assert_allclose(
                    (matrix @ np.asarray([1.0, 0.0], dtype=np.float32))[2:],
                    [0.8, 0.6])
                self.assertIs(ask._message_refs_from_pointer(coverage), refs)
                refs.corpus_connect = lambda: sqlite3.connect(corpus)
                pooled = ask._q8_grouped_pool(
                    np.asarray([1.0, 0.0], dtype=np.float32), refs, filters, 2)
                self.assertIsNotNone(pooled)
                rows, scores, _ = pooled
                self.assertEqual([row["ordinal"] for row in rows], [2, 3])
                self.assertEqual([row["text"] for row in rows],
                                 ["current alpha", "current gamma"])
                np.testing.assert_allclose(scores, [0.9, 0.8])

                bad_corpus = root / "bad-corpus.db"
                _corpus(bad_corpus, self._fixture_rows_with_bad_text())
                refs.corpus.close()
                refs.corpus = None
                refs.corpus_connect = lambda: sqlite3.connect(bad_corpus)
                pooled = ask._q8_grouped_pool(
                    np.asarray([1.0, 0.0], dtype=np.float32), refs, filters, 2)
                self.assertIsNotNone(pooled)
                rows, scores, _ = pooled
                self.assertEqual([row["ordinal"] for row in rows], [3])
                np.testing.assert_allclose(scores, [0.8])
                self.assertEqual(
                    refs.take_integrity_disclosure()["dropped"], 1)

    def _republish(self, root: Path, source: dict, meta: Path,
                   manifest) -> object:
        rows = [
            _row("d", "second gamma", [0.3, 0.7], agent="claude",
                 project="beta", session="s-d", ts=500, turn=5,
                 who="assistant", model="sonnet", family_id=3),
        ]
        artifacts, hashes, refs = _inputs(root, "delta2", rows)
        published = embedding_segments.publish_delta(
            meta, source=source, artifacts=artifacts,
            ids=[row["mid"] for row in rows], hashes=hashes, refs=refs,
            shadows=[], coverage={"total": 3},
            expected_generation=manifest["generation"])
        embedding_segments.prune_orphans(meta, grace_seconds=0.0)
        return published

    def test_publication_race_reopens_instead_of_claiming_damage(self) -> None:
        with _temporary_root() as root:
            source, meta, manifest, _corpus, _ = self._fixture(root)
            with mock.patch.object(
                    common, "transcript_generation", return_value=source):
                held, _, _, _ = segment_query.open_current(meta)
                published = self._republish(root, source, meta, manifest)
                self.assertNotEqual(
                    held["generation"], published["generation"])
                reopened, _, _, _ = segment_query.open_current(meta)
                self.assertEqual(
                    reopened["generation"], published["generation"])

    def test_republished_artifacts_are_transient_not_integrity_failures(
            self) -> None:
        with _temporary_root() as root:
            source, meta, manifest, _corpus, _ = self._fixture(root)
            with mock.patch.object(
                    common, "transcript_generation", return_value=source):
                _, _, refs, _ = segment_query.open_current(meta)
                self._republish(root, source, meta, manifest)
                with self.assertRaises(
                        segment_query.SegmentArtifactMoved) as caught:
                    refs.eligible(None)
            self.assertNotIsInstance(
                caught.exception, segment_query.SegmentIntegrityError)

    def test_absent_corpus_rows_disclose_coverage_not_a_text_mismatch(
            self) -> None:
        with _temporary_root() as root:
            source, meta, _manifest, _current, live_rows = self._fixture(root)
            lagging = root / "lagging-corpus.db"
            _corpus(lagging, live_rows[1:])
            with mock.patch.object(
                    common, "transcript_generation", return_value=source):
                _, _, refs, _ = segment_query.open_current(meta)
                refs.corpus_connect = lambda: sqlite3.connect(lagging)
                self.assertEqual(
                    [row["ordinal"] for row in refs.resolve([2, 3])], [3])
                self.assertEqual(refs.take_integrity_disclosure(), {
                    "state": "rows-uncorroborated",
                    "dropped": 1,
                    "mismatched": 0,
                    "absent": 1,
                    "considered": 2,
                    "reason": "the derived corpus has not mirrored these rows yet",
                    "repair": "corpus-refresh-pending",
                })


if __name__ == "__main__":
    unittest.main()
