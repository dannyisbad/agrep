from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from _test_support import isolate_data_dir

isolate_data_dir()
import common  # noqa: E402
import corpusdb  # noqa: E402
import embedding_segments  # noqa: E402
import semantic  # noqa: E402
import semantic_q8  # noqa: E402
import semantic_segment_build  # noqa: E402
import semantic_segment_compact as compact  # noqa: E402


def _fnv(payload: bytes) -> int:
    value = 0xCBF29CE484222325
    for byte in payload:
        value ^= byte
        value = (value * 0x00000100000001B3) & 0xFFFFFFFFFFFFFFFF
    return value


def _derived(root: Path, f32: Path, groups: Path,
             rows: int, dim: int) -> dict[str, Path]:
    family_ids = [int(value) for value in groups.read_text(
        encoding="utf-8").splitlines()]
    generation = hashlib.md5(f32.read_bytes()).digest()
    q8_payload = b"".join(
        struct.pack("<f", 1.0) + b"\0" * dim for _ in range(rows))
    q8_header = bytearray(64)
    struct.pack_into("<4sIIIQ16sIIQ", q8_header, 0, b"AGQ8", 1, dim, 1, rows,
                     generation, dim + 4, 0, _fnv(q8_payload))
    q8 = root / "derived.q8"
    q8.write_bytes(bytes(q8_header) + q8_payload)
    group_payload = b"".join(struct.pack("<I", value) for value in family_ids)
    group_header = bytearray(64)
    struct.pack_into(
        "<4sIIIQ16sIIQ", group_header, 0, b"AGQG", 1, 0,
        max(family_ids, default=0) + 1, rows, generation, 4, 0,
        _fnv(group_payload))
    group_artifact = root / "derived.q8g"
    group_artifact.write_bytes(bytes(group_header) + group_payload)
    f16 = root / "derived.f16"
    matrix = np.memmap(f32, dtype="<f4", mode="r", shape=(rows, dim))
    try:
        f16.write_bytes(np.asarray(matrix, dtype="<f2").tobytes(order="C"))
    finally:
        matrix._mmap.close()
    return {"f32": f32, "q8": q8, "groups": group_artifact, "f16": f16}


def _metadata(mids: list[str], families: list[int]) -> tuple[list[str], list[dict]]:
    hashes = [hashlib.blake2b(mid.encode(), digest_size=8).hexdigest() for mid in mids]
    refs = [{
        "mid": mid, "text_hash": text_hash, "agent": "codex",
        "project": f"p-{mid}", "session": f"s-{mid}", "ts": index + 10,
        "turn": index + 20, "who": "user", "model": "m",
        "model_source": f"source-{mid}",
        "family_id": family,
        "family_label": None if family == 0 else f"f:family-{family}",
        "side": False,
        "metadata_hash": hashlib.blake2b(
            f"metadata:{mid}".encode(), digest_size=16).hexdigest(),
    } for index, (mid, text_hash, family) in enumerate(
        zip(mids, hashes, families, strict=True))]
    return hashes, refs


def _inputs(root: Path, label: str, mids: list[str], vectors: np.ndarray,
            families: list[int], *, derive=_derived) -> tuple[dict, list[str], list[dict]]:
    source = root / label
    source.mkdir()
    f32 = source / "source.f32"
    f32.write_bytes(np.asarray(vectors, dtype="<f4").tobytes(order="C"))
    groups = source / "source.groups"
    groups.write_text("".join(f"{value}\n" for value in families), encoding="utf-8")
    artifacts = derive(source, f32, groups, len(mids), vectors.shape[1])
    hashes, refs = _metadata(mids, families)
    return artifacts, hashes, refs


def _with_family(root: Path, artifacts: dict, family: int) -> dict:
    updated = dict(artifacts)
    payload = bytearray(Path(artifacts["groups"]).read_bytes())
    struct.pack_into("<I", payload, 12, family + 1)
    struct.pack_into("<I", payload, 64, family)
    struct.pack_into("<Q", payload, 48, _fnv(payload[64:]))
    path = root / f"family-{family}.q8g"
    path.write_bytes(payload)
    updated["groups"] = path
    return updated


def _legacy_refs(path: Path, refs: list[dict], version: int = 1) -> Path:
    if version not in (1, 2, 3, 4):
        raise ValueError("legacy refs version must be between 1 and 4")
    optional = ""
    if version >= 2:
        optional += ",metadata_hash TEXT NOT NULL"
    if version >= 3:
        optional += ",family_label TEXT"
    if version >= 4:
        optional += ",model_source TEXT"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(f"""
            CREATE TABLE refs(
                local_ord INTEGER PRIMARY KEY, row_ref INTEGER NOT NULL,
                mid TEXT NOT NULL, text_hash TEXT NOT NULL, agent TEXT NOT NULL,
                project TEXT NOT NULL, session TEXT NOT NULL, ts INTEGER NOT NULL,
                turn INTEGER NOT NULL, who TEXT NOT NULL, model TEXT,
                family_id INTEGER NOT NULL{optional});
            CREATE UNIQUE INDEX refs_row_ref ON refs(row_ref);
            CREATE UNIQUE INDEX refs_mid ON refs(mid);
            CREATE INDEX refs_session ON refs(session,turn);
        """)
        values = []
        for index, row in enumerate(refs):
            record = [
                index, index, row["mid"], row["text_hash"], row["agent"],
                row["project"], row["session"], row["ts"], row["turn"],
                row["who"], row["model"], row["family_id"],
            ]
            if version >= 2:
                record.append(row["metadata_hash"])
            if version >= 3:
                record.append(row["family_label"])
            if version >= 4:
                record.append(row["model_source"])
            values.append(tuple(record))
        marks = ",".join("?" for _ in values[0])
        connection.executemany(
            f"INSERT INTO refs VALUES({marks})", values,
        )
        connection.commit()
    finally:
        connection.close()
    return path


class CompactionClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        compact._release_claim()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.saved_data_dir = common.DATA_DIR
        common.DATA_DIR = self.root

    def tearDown(self) -> None:
        compact._release_claim()
        common.DATA_DIR = self.saved_data_dir
        self.temp.cleanup()

    @staticmethod
    def _raw(pid: int, process_start: object,
             token: str = "a" * 32) -> bytes:
        return json.dumps({
            "pid": pid,
            "token": token,
            "process_start": process_start,
        }).encode("utf-8")

    def _assert_no_tombstones(self) -> None:
        self.assertEqual(list(self.root.glob(".*.owner-reap-*")), [])

    def test_claim_bytes_and_token_are_per_acquisition(self) -> None:
        path = compact._claim_path()
        with mock.patch.object(
                common, "process_start_identity", return_value="birth"):
            self.assertTrue(compact._acquire_claim())
            first_raw = path.read_bytes()
            first = json.loads(first_raw)
            self.assertEqual(list(first), ["pid", "token", "process_start"])
            self.assertEqual(len(first["token"]), 32)
            self.assertEqual(
                f"{int(first['token'], 16):032x}", first["token"])
            self.assertEqual(
                first_raw,
                self._raw(os.getpid(), "birth", first["token"]),
            )
            self.assertFalse(first_raw.endswith(b"\n"))
            compact._release_claim()
            self.assertTrue(compact._acquire_claim())
            second = json.loads(path.read_bytes())
            self.assertNotEqual(first["token"], second["token"])
            compact._release_claim()
        self.assertFalse(path.exists())
        self._assert_no_tombstones()

    def test_live_dead_recycled_and_unverifiable_owners(self) -> None:
        path = compact._claim_path()
        owner_pid = os.getpid() + 10_000_000
        cases = (
            ("live", True, "owner-start", False),
            ("dead", False, "owner-start", True),
            ("recycled", True, "new-start", True),
            ("unverifiable", True, None, False),
        )
        for label, alive, actual_start, acquired in cases:
            with self.subTest(label=label):
                original = self._raw(owner_pid, "owner-start")
                path.write_bytes(original)

                def process_start(pid: int) -> str | None:
                    return actual_start if pid == owner_pid else "new-owner"

                with mock.patch.object(
                        common, "pid_alive", return_value=alive), \
                        mock.patch.object(
                            common, "process_start_identity",
                            side_effect=process_start):
                    self.assertEqual(compact._acquire_claim(), acquired)
                if acquired:
                    compact._release_claim()
                    self.assertFalse(path.exists())
                else:
                    self.assertEqual(path.read_bytes(), original)
                    path.unlink()
                self._assert_no_tombstones()
        path.write_bytes(self._raw(owner_pid, "owner-start"))
        os.utime(path, (100, 100))

        def unavailable_start(pid: int) -> str | None:
            return None if pid == owner_pid else "new-owner"

        with mock.patch.object(common, "pid_alive", return_value=True), \
                mock.patch.object(
                    common, "process_start_identity",
                    side_effect=unavailable_start), \
                mock.patch.object(compact.time, "time", return_value=131):
            self.assertFalse(compact._acquire_claim())
        self.assertTrue(path.exists())
        path.unlink()
        self._assert_no_tombstones()

    def test_recent_then_stale_malformed_and_nonobject_claims(self) -> None:
        path = compact._claim_path()
        for label, raw in (("malformed", b"{"), ("array", b"[]")):
            with self.subTest(label=label), mock.patch.object(
                    common, "process_start_identity", return_value="new-owner"):
                path.write_bytes(raw)
                os.utime(path, (100, 100))
                with mock.patch.object(compact.time, "time", return_value=130):
                    self.assertFalse(compact._acquire_claim())
                self.assertEqual(path.read_bytes(), raw)
                with mock.patch.object(compact.time, "time", return_value=131):
                    self.assertTrue(compact._acquire_claim())
                compact._release_claim()
                self.assertFalse(path.exists())
                self._assert_no_tombstones()
                path.write_bytes(raw)
                os.utime(path, (1000, 1000))
                with mock.patch.object(compact.time, "time", return_value=100):
                    self.assertTrue(compact._acquire_claim())
                compact._release_claim()
                self.assertFalse(path.exists())
                self._assert_no_tombstones()

    def test_late_release_preserves_same_and_different_replacements(self) -> None:
        path = compact._claim_path()
        with mock.patch.object(
                common, "process_start_identity", return_value="birth"):
            for label in ("same", "different"):
                with self.subTest(label=label):
                    self.assertTrue(compact._acquire_claim())
                    owned = path.read_bytes()
                    replacement_body = owned if label == "same" else b"replacement"
                    replacement = self.root / f"{label}.replacement"
                    replacement.write_bytes(replacement_body)
                    os.replace(replacement, path)
                    compact._release_claim()
                    self.assertEqual(path.read_bytes(), replacement_body)
                    path.unlink()
                    self._assert_no_tombstones()

    def test_reclaim_snapshot_race_preserves_replacement(self) -> None:
        path = compact._claim_path()
        original = self._raw(99_999_999, "dead-owner")
        replacement_body = b"replacement-owner"
        path.write_bytes(original)
        real_remove = compact.ownerfile.remove_exact

        def raced_remove(target, observed, *, tombstone=False):
            replacement = self.root / "racing.replacement"
            replacement.write_bytes(replacement_body)
            os.replace(replacement, target)
            return real_remove(target, observed, tombstone=tombstone)

        with mock.patch.object(common, "pid_alive", return_value=False), \
                mock.patch.object(
                    common, "process_start_identity", return_value="new-owner"), \
                mock.patch.object(
                    compact.ownerfile, "remove_exact",
                    side_effect=raced_remove) as remove:
            self.assertFalse(compact._acquire_claim())
        self.assertEqual(path.read_bytes(), replacement_body)
        self.assertTrue(remove.call_args.kwargs["tombstone"])
        self._assert_no_tombstones()

    def test_reclaim_and_release_remove_tombstones(self) -> None:
        path = compact._claim_path()
        path.write_bytes(self._raw(99_999_999, "dead-owner"))
        real_remove = compact.ownerfile.remove_exact
        with mock.patch.object(common, "pid_alive", return_value=False), \
                mock.patch.object(
                    common, "process_start_identity", return_value="new-owner"), \
                mock.patch.object(
                    compact.ownerfile, "remove_exact",
                    wraps=real_remove) as remove:
            self.assertTrue(compact._acquire_claim())
            compact._release_claim()
        self.assertGreaterEqual(remove.call_count, 2)
        self.assertTrue(all(
            call.kwargs.get("tombstone") is True
            for call in remove.call_args_list
        ))
        self.assertFalse(path.exists())
        self._assert_no_tombstones()

    def test_real_subprocess_crash_claim_is_reclaimed(self) -> None:
        ready = self.root / "ready"
        script = """
import pathlib
import sys
import time
import common
import semantic_segment_compact as compact
common.DATA_DIR = pathlib.Path(sys.argv[1])
assert compact._acquire_claim()
pathlib.Path(sys.argv[2]).write_text("ready", encoding="ascii")
time.sleep(30)
"""
        env = dict(os.environ)
        py_dir = str(Path(__file__).resolve().parent)
        env["AGREP_DATA_DIR"] = str(self.root)
        env["PYTHONPATH"] = os.pathsep.join(
            value for value in (py_dir, env.get("PYTHONPATH")) if value)
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(self.root), str(ready)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 5
        while not ready.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        if not ready.exists():
            if process.poll() is None:
                process.terminate()
            stdout, stderr = process.communicate(timeout=2)
            self.fail(f"child did not acquire compaction claim: {stdout} {stderr}")
        process.terminate()
        process.communicate(timeout=5)
        self.assertTrue(compact._claim_path().exists())
        self.assertTrue(compact._acquire_claim())
        compact._release_claim()
        self.assertFalse(compact._claim_path().exists())
        self._assert_no_tombstones()


class SegmentCompactionTests(unittest.TestCase):
    def _native_mids(self, manifest, query: np.ndarray, *, grouped: bool) -> list[str]:
        with mock.patch.object(
                common, "EMBEDDINGS_PATH", manifest.path.parent / "embeddings.f32"):
            ready = semantic_q8._validated_manifest({"commit": dict(manifest)})
        self.assertIsNotNone(ready)
        scanner = semantic_q8._Q8Scanner(ready, binary=common.ingest_bin())
        try:
            ordinals, _ = scanner.top(
                query, manifest["generation"], manifest["live_rows"],
                grouped=grouped, heads=1)
        finally:
            scanner.close()
        by_ref = {row["row_ref"]: row["mid"]
                  for row in embedding_segments.active_rows(manifest)}
        return [by_ref[int(row)] for row in ordinals]

    def test_claim_serializes_scheduler_children_and_releases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(compact.common, "DATA_DIR", root):
                self.assertTrue(compact._acquire_claim())
                self.assertFalse(compact._acquire_claim())
                compact._release_claim()
                self.assertFalse(compact._claim_path().exists())

    def test_refs_schema_failure_closes_staging_connection(self) -> None:
        connection = mock.Mock()
        connection.executescript.side_effect = RuntimeError("injected")
        with mock.patch.object(
                compact, "_require_writable_data_dir", return_value=None), \
                mock.patch.object(
                    compact.sqlite3, "connect", return_value=connection):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                compact._create_refs(Path("unused"))
        connection.close.assert_called_once_with()

    def test_compaction_streams_live_rows_and_preserves_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            meta = root / "embeddings.meta"
            base_vectors = np.asarray([[1, 0], [0, 1], [-1, 0]], dtype=np.float32)
            artifacts, hashes, refs = _inputs(
                root, "base", ["a", "b", "c"], base_vectors, [1, 2, 3])
            base = embedding_segments.publish_base(
                meta, source={"ingest_signature": "one"}, model_id="model", dim=2,
                artifacts=artifacts, ids=["a", "b", "c"], hashes=hashes,
                refs=refs, coverage={"total": 3})
            delta_vectors = np.asarray([[0.6, 0.8], [0.8, 0.6]], dtype=np.float32)
            artifacts, hashes, refs = _inputs(
                root, "delta", ["b", "d"], delta_vectors, [2, 4])
            updated = embedding_segments.publish_delta(
                meta, source={"ingest_signature": "two"}, artifacts=artifacts,
                ids=["b", "d"], hashes=hashes, refs=refs, shadows=[1, 2],
                coverage={"total": 3}, expected_generation=base["generation"])
            old_matrix_path = embedding_segments.artifact_path(
                updated, updated["segments"][0]["artifacts"]["f32"])
            old_mapping = np.memmap(old_matrix_path, dtype="<f4", mode="r", shape=(3, 2))
            try:
                with (mock.patch.object(compact, "_derive_artifacts", side_effect=_derived),
                      mock.patch.object(
                          embedding_segments, "_copy_file",
                          side_effect=AssertionError("compaction copied staging"))):
                    result = compact.compact(
                        meta, force=True, governor=lambda: None,
                        check_every_rows=1)
                self.assertEqual(result["state"], "compacted")
                compacted = embedding_segments.load_manifest(
                    meta, verify_hashes=True, validate_liveness=True)
                self.assertEqual((len(compacted["segments"]), compacted["delta_count"]),
                                 (1, 0))
                rows = embedding_segments.active_rows(compacted)
                self.assertEqual([row["mid"] for row in rows], ["a", "b", "d"])
                self.assertEqual([row["family_id"] for row in rows], [1, 2, 4])
                self.assertEqual(rows[1]["project"], "p-b")
                self.assertEqual(
                    [row["model_source"] for row in rows],
                    ["source-a", "source-b", "source-d"],
                )
                matrix_path = embedding_segments.artifact_path(
                    compacted, compacted["segments"][0]["artifacts"]["f32"])
                matrix = np.fromfile(matrix_path, dtype="<f4").reshape(3, 2)
                np.testing.assert_allclose(
                    matrix, np.asarray([[1, 0], [0.6, 0.8], [0.8, 0.6]]))
                groups_path = embedding_segments.artifact_path(
                    compacted, compacted["segments"][0]["artifacts"]["groups"])
                groups = [value[0] for value in struct.iter_unpack(
                    "<I", groups_path.read_bytes()[64:])]
                self.assertEqual(groups, [1, 2, 4])
                np.testing.assert_array_equal(old_mapping, base_vectors)
            finally:
                old_mapping._mmap.close()

    def test_disk_preflight_defers_before_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            meta = root / "embeddings.meta"
            vectors = np.asarray([[1, 0], [0, 1]], dtype=np.float32)
            artifacts, hashes, refs = _inputs(
                root, "base", ["a", "b"], vectors, [1, 2])
            base = embedding_segments.publish_base(
                meta, source={"ingest_signature": "one"}, model_id="model", dim=2,
                artifacts=artifacts, ids=["a", "b"], hashes=hashes,
                refs=refs, coverage={"total": 2})
            before = meta.read_bytes()
            with (mock.patch.object(
                      compact.shutil, "disk_usage",
                      return_value=mock.Mock(free=0)),
                  mock.patch.object(
                      compact, "_stream_live_base",
                      side_effect=AssertionError("staging started"))):
                result = compact.compact(meta, force=True, governor=lambda: None)
            self.assertEqual(result["state"], "deferred")
            self.assertIn("insufficient disk", result["reason"])
            self.assertEqual(meta.read_bytes(), before)
            self.assertEqual(
                embedding_segments.load_manifest(meta)["generation"],
                base["generation"])

    def test_legacy_refs_upgrade_preserves_vectors_and_live_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            meta = root / "embeddings.meta"
            mids = ["a", "b"]
            vectors = np.asarray([[1, 0], [0, 1]], dtype=np.float32)
            artifacts, hashes, refs = _inputs(
                root, "base", mids, vectors, [1, 2])
            legacy_refs = _legacy_refs(root / "legacy.refs.sqlite", refs)
            source = {"ingest_signature": "current"}
            original = embedding_segments.publish_base(
                meta, source=source, model_id="model", dim=2,
                artifacts=artifacts, ids=mids, hashes=hashes, refs=legacy_refs,
                coverage={"total": len(mids)})
            self.assertEqual(
                embedding_segments.refs_schema_versions(original),
                frozenset({1}),
            )

            corpus = root / "corpus.db"
            connection = sqlite3.connect(corpus)
            try:
                connection.execute(
                    "CREATE TABLE msgs(agent TEXT,session TEXT,turn INTEGER,"
                    "who TEXT,text TEXT,ts INTEGER,project TEXT,model TEXT,"
                    "model_source TEXT)")
                connection.executemany(
                    "INSERT INTO msgs VALUES(?,?,?,?,?,?,?,?,?)",
                    [("codex", f"s-{mid}", index + 20, "user", mid,
                      100 + index, f"current-{mid}", "new-model",
                      f"explicit-{mid}")
                     for index, mid in enumerate(mids)],
                )
                connection.commit()
            finally:
                connection.close()

            with (mock.patch.object(
                      corpusdb, "connect",
                      side_effect=lambda **_: sqlite3.connect(corpus)),
                  mock.patch.object(
                      semantic, "source_generation", return_value=source),
                  mock.patch.object(
                      common, "strict_family_parent_map", return_value={}),
                  mock.patch.object(
                      compact, "_derive_artifacts", side_effect=_derived)):
                result = compact.compact(
                    meta, force=True, refresh_metadata=True,
                    governor=lambda: None, check_every_rows=1)

            self.assertEqual(result["state"], "compacted")
            self.assertTrue(result["metadata_refreshed"])
            upgraded = embedding_segments.load_manifest(
                meta, verify_hashes=True, validate_liveness=True)
            self.assertEqual(upgraded["live_rows"], original["live_rows"])
            self.assertEqual(
                embedding_segments.refs_schema_versions(upgraded),
                frozenset({5}),
            )
            rows = embedding_segments.active_rows(upgraded)
            self.assertEqual([row["mid"] for row in rows], mids)
            self.assertEqual(
                [row["model_source"] for row in rows],
                [f"explicit-{mid}" for mid in mids],
            )
            self.assertEqual(
                [row["project"] for row in rows],
                [f"current-{mid}" for mid in mids],
            )
            self.assertEqual(
                [row["model"] for row in rows],
                ["new-model"] * len(mids),
            )
            self.assertEqual(
                [row["ts"] for row in rows],
                list(range(100, 100 + len(mids))),
            )
            for index, row in enumerate(rows):
                message = common.Message(
                    id=row["mid"], agent="codex",
                    project=f"current-{row['mid']}",
                    session=f"s-{row['mid']}", ts=100 + index,
                    turn=index + 20, text="", who="user",
                    model="new-model",
                    model_source=f"explicit-{row['mid']}",
                )
                self.assertEqual(
                    row["metadata_hash"],
                    semantic_segment_build.refs_metadata_fingerprint(
                        message, f"f:s-{row['mid']}"),
                )
            matrix = np.fromfile(
                embedding_segments.artifact_path(
                    upgraded, upgraded["segments"][0]["artifacts"]["f32"]),
                dtype="<f4",
            ).reshape(len(mids), 2)
            np.testing.assert_array_equal(matrix, vectors)

    def test_changed_legacy_row_upgrades_refs_on_old_source_for_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            meta = root / "embeddings.meta"
            artifacts, hashes, refs = _inputs(
                root, "base", ["a"],
                np.asarray([[1, 0]], dtype=np.float32), [1])
            old_source = {"ingest_signature": "old"}
            original = embedding_segments.publish_base(
                meta, source=old_source, model_id="model", dim=2,
                artifacts=artifacts, ids=["a"], hashes=hashes,
                refs=_legacy_refs(root / "legacy.refs.sqlite", refs),
                coverage={"total": 1})

            corpus = root / "corpus.db"
            connection = sqlite3.connect(corpus)
            try:
                connection.execute(
                    "CREATE TABLE msgs(agent TEXT,session TEXT,turn INTEGER,"
                    "who TEXT,text TEXT,ts INTEGER,project TEXT,model TEXT,"
                    "model_source TEXT)")
                connection.execute(
                    "INSERT INTO msgs VALUES(?,?,?,?,?,?,?,?,?)",
                    ("codex", "s-a", 20, "user", "changed", 100,
                     "current-a", "new-model", "explicit-a"),
                )
                connection.commit()
            finally:
                connection.close()

            with (mock.patch.object(
                      corpusdb, "connect",
                      side_effect=lambda **_: sqlite3.connect(corpus)),
                  mock.patch.object(
                      semantic, "source_generation",
                      return_value={"ingest_signature": "current"}),
                  mock.patch.object(
                      common, "strict_family_parent_map", return_value={}),
                  mock.patch.object(
                      compact, "_derive_artifacts", side_effect=_derived)):
                result = compact.compact(
                    meta, force=True, refresh_metadata=True,
                    governor=lambda: None, check_every_rows=1)

            self.assertEqual(result["state"], "compacted")
            self.assertTrue(result["metadata_refresh_requested"])
            self.assertFalse(result["metadata_refreshed"])
            upgraded = embedding_segments.load_manifest(meta)
            self.assertEqual(upgraded["source"], old_source)
            self.assertEqual(upgraded["live_rows"], original["live_rows"])
            self.assertEqual(
                embedding_segments.refs_schema_versions(upgraded),
                frozenset({5}),
            )
            self.assertEqual(
                embedding_segments.active_rows(upgraded)[0]["model_source"],
                "unknown",
            )

    def test_v2_through_v4_refs_upgrade_without_coverage_loss(self) -> None:
        for version in (2, 3, 4):
            with self.subTest(version=version), \
                    tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                meta = root / "embeddings.meta"
                artifacts, hashes, refs = _inputs(
                    root, "base", ["a"],
                    np.asarray([[1, 0]], dtype=np.float32), [1])
                source = {"ingest_signature": f"v{version}"}
                original = embedding_segments.publish_base(
                    meta, source=source, model_id="model", dim=2,
                    artifacts=artifacts, ids=["a"], hashes=hashes,
                    refs=_legacy_refs(
                        root / "legacy.refs.sqlite", refs, version),
                    coverage={"total": 1})
                corpus = root / "corpus.db"
                connection = sqlite3.connect(corpus)
                try:
                    connection.execute(
                        "CREATE TABLE msgs(agent TEXT,session TEXT,"
                        "turn INTEGER,who TEXT,text TEXT,ts INTEGER,"
                        "project TEXT,model TEXT,model_source TEXT)")
                    connection.execute(
                        "INSERT INTO msgs VALUES(?,?,?,?,?,?,?,?,?)",
                        ("codex", "s-a", 20, "user", "a", 100,
                         "current-a", "new-model", "explicit-a"),
                    )
                    connection.commit()
                finally:
                    connection.close()
                with (mock.patch.object(
                          corpusdb, "connect",
                          side_effect=lambda **_: sqlite3.connect(corpus)),
                      mock.patch.object(
                          semantic, "source_generation", return_value=source),
                      mock.patch.object(
                          common, "strict_family_parent_map", return_value={}),
                      mock.patch.object(
                          compact, "_derive_artifacts",
                          side_effect=_derived)):
                    result = compact.compact(
                        meta, force=True, refresh_metadata=True,
                        governor=lambda: None, check_every_rows=1)
                upgraded = embedding_segments.load_manifest(meta)
                self.assertTrue(result["metadata_refreshed"])
                self.assertEqual(
                    upgraded["live_rows"], original["live_rows"])
                self.assertEqual(
                    embedding_segments.refs_schema_versions(upgraded),
                    frozenset({5}),
                )
                self.assertFalse(
                    embedding_segments.active_rows(upgraded)[0]["side"])

    def test_legacy_metadata_upgrade_uses_constant_source_queries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            meta = root / "embeddings.meta"
            row_count = 256
            mids = [f"m-{index}" for index in range(row_count)]
            vectors = np.ones((row_count, 2), dtype=np.float32)
            artifacts, hashes, refs = _inputs(
                root, "base", mids, vectors,
                list(range(1, row_count + 1)))
            source = {"ingest_signature": "current"}
            embedding_segments.publish_base(
                meta, source=source, model_id="model", dim=2,
                artifacts=artifacts, ids=mids, hashes=hashes,
                refs=_legacy_refs(root / "legacy.refs.sqlite", refs),
                coverage={"total": row_count})

            corpus = root / "corpus.db"
            connection = sqlite3.connect(corpus)
            try:
                connection.execute(
                    "CREATE TABLE msgs(agent TEXT,session TEXT,turn INTEGER,"
                    "who TEXT,text TEXT,ts INTEGER,project TEXT,model TEXT,"
                    "model_source TEXT)")
                connection.executemany(
                    "INSERT INTO msgs VALUES(?,?,?,?,?,?,?,?,?)",
                    [("codex", f"s-{mid}", index + 20, "user", mid,
                      100 + index, f"current-{mid}", "new-model",
                      f"explicit-{mid}")
                     for index, mid in enumerate(mids)],
                )
                connection.commit()
            finally:
                connection.close()

            source_queries: list[str] = []

            def connect(**_kwargs):
                current = sqlite3.connect(corpus)
                current.set_trace_callback(source_queries.append)
                return current

            with (mock.patch.object(corpusdb, "connect", side_effect=connect),
                  mock.patch.object(
                      semantic, "source_generation", return_value=source),
                  mock.patch.object(
                      common, "strict_family_parent_map", return_value={}),
                  mock.patch.object(
                      compact, "_derive_artifacts", side_effect=_derived)):
                result = compact.compact(
                    meta, force=True, refresh_metadata=True,
                    governor=lambda: None, check_every_rows=64)

            selects = [
                statement for statement in source_queries
                if statement.lstrip().upper().startswith("SELECT")
            ]
            self.assertEqual(result["rows"], row_count)
            self.assertLessEqual(len(selects), 3)

    def test_deferral_and_precommit_failure_leave_old_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            meta = root / "embeddings.meta"
            vectors = np.asarray([[1, 0], [0, 1]], dtype=np.float32)
            artifacts, hashes, refs = _inputs(
                root, "base", ["a", "b"], vectors, [1, 2])
            base = embedding_segments.publish_base(
                meta, source={"ingest_signature": "one"}, model_id="model", dim=2,
                artifacts=artifacts, ids=["a", "b"], hashes=hashes,
                refs=refs, coverage={"total": 2})
            before = meta.read_bytes()
            decisions = iter((None, "foreground load"))
            deferred = compact.compact(
                meta, force=True, governor=lambda: next(decisions),
                check_every_rows=1)
            self.assertEqual(deferred["state"], "deferred")
            self.assertEqual(meta.read_bytes(), before)

            def fail(stage: str) -> None:
                if stage == "before_manifest_replace":
                    raise RuntimeError("injected")

            with mock.patch.object(compact, "_derive_artifacts", side_effect=_derived):
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    compact.compact(
                        meta, force=True, governor=lambda: None,
                        check_every_rows=1, _on_stage=fail)
            self.assertEqual(meta.read_bytes(), before)
            self.assertEqual(embedding_segments.load_manifest(meta)["generation"],
                             base["generation"])
            current = embedding_segments.load_manifest(meta)
            self.assertEqual(
                set((root / embedding_segments.SEGMENT_DIR).iterdir()),
                embedding_segments.referenced_paths(current))

    def test_current_source_move_defers_at_final_manifest_fence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            meta = root / "embeddings.meta"
            source = {"ingest_signature": "current"}
            moved = {"ingest_signature": "moved"}
            artifacts, hashes, refs = _inputs(
                root, "base", ["a", "b"],
                np.asarray([[1, 0], [0, 1]], dtype=np.float32), [1, 2])
            base = embedding_segments.publish_base(
                meta, source=source, model_id="model", dim=2,
                artifacts=artifacts, ids=["a", "b"], hashes=hashes,
                refs=refs, coverage={"total": 2})
            before = meta.read_bytes()

            with (
                mock.patch.object(
                    semantic, "source_generation", side_effect=(source, moved)),
                mock.patch.object(
                    compact, "_derive_artifacts", side_effect=_derived),
            ):
                result = compact.compact(
                    meta, force=True, governor=lambda: None,
                    check_every_rows=1)

            self.assertEqual(result["state"], "deferred")
            self.assertIn("moved before semantic publication", result["reason"])
            self.assertEqual(meta.read_bytes(), before)
            self.assertEqual(
                embedding_segments.load_manifest(meta)["generation"],
                base["generation"])

    def test_q8_timeout_leaves_old_generation_and_removes_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            meta = root / "embeddings.meta"
            vectors = np.asarray([[1, 0], [0, 1]], dtype=np.float32)
            artifacts, hashes, refs = _inputs(
                root, "base", ["a", "b"], vectors, [1, 2])
            base = embedding_segments.publish_base(
                meta, source={"ingest_signature": "one"}, model_id="model", dim=2,
                artifacts=artifacts, ids=["a", "b"], hashes=hashes,
                refs=refs, coverage={"total": 2})
            before = meta.read_bytes()
            with mock.patch.object(
                    compact, "_derive_artifacts",
                    side_effect=subprocess.TimeoutExpired("semantic-q8-build", 300)):
                with self.assertRaises(subprocess.TimeoutExpired):
                    compact.compact(meta, force=True, governor=lambda: None)
            self.assertEqual(meta.read_bytes(), before)
            self.assertEqual(embedding_segments.load_manifest(meta)["generation"],
                             base["generation"])
            self.assertEqual(list(root.glob(".semantic-compaction-*")), [])

    def test_real_q8_compaction_preserves_flat_and_grouped_stable_ties(self) -> None:
        if not common.ingest_bin().exists():
            self.skipTest("agrep-rs has not been built")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            meta = root / "embeddings.meta"
            tied = np.asarray([[1, 0], [1, 0], [0, 1]], dtype=np.float32)
            artifacts, hashes, refs = _inputs(
                root, "base", ["a", "b", "c"], tied, [1, 1, 2],
                derive=compact._derive_artifacts)
            base = embedding_segments.publish_base(
                meta, source={"ingest_signature": "one"}, model_id="model", dim=2,
                artifacts=artifacts, ids=["a", "b", "c"], hashes=hashes,
                refs=refs, coverage={"total": 3})
            artifacts, hashes, refs = _inputs(
                root, "delta", ["b", "d"],
                np.asarray([[1, 0], [1, 0]], dtype=np.float32), [1, 3],
                derive=compact._derive_artifacts)
            updated = embedding_segments.publish_delta(
                meta, source={"ingest_signature": "two"}, artifacts=artifacts,
                ids=["b", "d"], hashes=hashes, refs=refs, shadows=[1, 2],
                coverage={"total": 3}, expected_generation=base["generation"])
            query = np.asarray([1, 0], dtype=np.float32)
            before_flat = self._native_mids(updated, query, grouped=False)
            before_grouped = self._native_mids(updated, query, grouped=True)

            result = compact.compact(
                meta, force=True, governor=lambda: None, check_every_rows=1)
            self.assertEqual(result["state"], "compacted")
            compacted = embedding_segments.load_manifest(
                meta, verify_hashes=True, validate_liveness=True)
            after_flat = self._native_mids(compacted, query, grouped=False)
            after_grouped = self._native_mids(compacted, query, grouped=True)

            self.assertEqual(before_flat, ["a", "b", "d"])
            self.assertEqual(after_flat, before_flat)
            self.assertEqual(before_grouped, ["a", "d"])
            self.assertEqual(after_grouped, before_grouped)

    def test_native_scanner_parity_at_one_eight_and_sixteen_deltas(self) -> None:
        if not common.ingest_bin().exists():
            self.skipTest("agrep-rs has not been built")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifacts, _, _ = _inputs(
                root, "template", ["template"],
                np.asarray([[1, 0]], dtype=np.float32), [1],
                derive=compact._derive_artifacts)
            query = np.asarray([1, 0], dtype=np.float32)
            for delta_count in (1, 8, 16):
                with self.subTest(delta_count=delta_count):
                    case = root / f"case-{delta_count}"
                    case.mkdir()
                    meta = case / "embeddings.meta"
                    hashes, refs = _metadata(["base"], [1])
                    current = embedding_segments.publish_base(
                        meta, source={"ingest_signature": "base"},
                        model_id="model", dim=2, artifacts=artifacts,
                        ids=["base"], hashes=hashes, refs=refs,
                        coverage={"total": 1})
                    expected = ["base"]
                    for index in range(delta_count):
                        mid = f"delta-{index}"
                        family = index + 2
                        hashes, refs = _metadata([mid], [family])
                        current = embedding_segments.publish_delta(
                            meta, source={"ingest_signature": mid},
                            artifacts=_with_family(root, artifacts, family),
                            ids=[mid], hashes=hashes, refs=refs,
                            coverage={"total": index + 2},
                            expected_generation=current["generation"])
                        expected.append(mid)
                    self.assertEqual(current["delta_count"], delta_count)
                    self.assertEqual(
                        self._native_mids(current, query, grouped=False), expected)
                    self.assertEqual(
                        self._native_mids(current, query, grouped=True), expected)


if __name__ == "__main__":
    unittest.main()
