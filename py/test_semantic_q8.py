"""Format, parity, and fail-closed tests for the q8 semantic shadow."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import numpy as np

from _test_support import isolate_data_dir

isolate_data_dir()

import ask  # noqa: E402
import common  # noqa: E402
import semantic_q8  # noqa: E402
import session_context  # noqa: E402
import surface_policy as surface  # noqa: E402


@contextmanager
def _q8_temporary_directory():
    with tempfile.TemporaryDirectory() as raw:
        try:
            yield raw
        finally:
            semantic_q8.close_scanner()


@contextmanager
def _data_dir(root: Path):
    with mock.patch.object(common, "DATA_DIR", root), \
            mock.patch.object(session_context, "DATA_DIR", root):
        yield


class SemanticQ8Tests(unittest.TestCase):
    def test_group_identity_tracks_only_logical_family_changes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            messages = root / "messages.jsonl"
            messages.write_text('{"id":"codex:child:1"}\n', encoding="utf-8")

            def publish(rows: list[dict], signature: str) -> None:
                rows = sorted(rows, key=lambda row: str(row["session"]))
                (root / "sessions.jsonl").write_text(
                    "".join(json.dumps(row) + "\n" for row in rows),
                    encoding="utf-8",
                )
                (root / ".ingest.sig").write_text(
                    signature + "\n", encoding="utf-8")
                pairs = [
                    (str(row["session"]), str(row.get("parent") or ""))
                    for row in rows
                ]
                (root / common.SESSION_FAMILY_META_FILE).write_text(
                    json.dumps({
                        "version": common.SESSION_FAMILY_INDEX_VERSION,
                        "algorithm": common.SESSION_FAMILY_DIGEST_ALGORITHM,
                        "ingest_signature": signature,
                        "count": len(rows),
                        "digest": common.session_family_digest(sorted(pairs)),
                    }),
                    encoding="utf-8",
                )

            expected = {"commit": {"ids": {"sha256": "ids"}}}
            with _data_dir(root), \
                    mock.patch.object(common, "MESSAGES_PATH", messages):
                publish([{"session": "child", "n": 1}], "1:first")
                first = semantic_q8._group_source_identity(expected)
                publish(
                    [{"session": "child", "n": 99, "first_text": "moved"}],
                    "99:second",
                )
                metadata_only = semantic_q8._group_source_identity(expected)
                publish(
                    [{"session": "child", "parent": "root", "n": 99}],
                    "99:third",
                )
                parent_changed = semantic_q8._group_source_identity(expected)
        self.assertEqual(first, metadata_only)
        self.assertNotEqual(first, parent_changed)

    def setUp(self) -> None:
        semantic_q8.close_scanner()

    def tearDown(self) -> None:
        semantic_q8.close_scanner()

    def test_windows_builder_decodes_rust_json_as_utf8(self) -> None:
        record = {
            "version": semantic_q8.VERSION,
            "score_kind": semantic_q8.SCORE_KIND,
            "group_version": semantic_q8.VERSION,
        }
        completed = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps(record), stderr="")
        with mock.patch.object(semantic_q8.common, "WIN", True), \
                mock.patch.object(semantic_q8.subprocess, "CREATE_NO_WINDOW", 1,
                                  create=True), \
                mock.patch.object(semantic_q8.subprocess, "run",
                                  return_value=completed) as run:
            semantic_q8.build_from_f32(
                Path("embeddings.f32"), Path("embeddings.meta"), Path("q8"),
                binary=Path("agrep-rs.exe"), groups_path=Path("groups.ids"))
        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["creationflags"], 1)

    def test_flat_publication_barrier_is_rejected_by_python_and_rust(self) -> None:
        binary = common.ingest_bin()
        if not binary.exists():
            self.skipTest("agrep-rs has not been built")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            embeddings = root / "embeddings.f32"
            meta = root / "embeddings.meta"
            groups = root / "groups.ids"
            embeddings.write_bytes(b"")
            groups.write_text("", encoding="utf-8")
            common._write_embedding_publication_barrier(
                meta, 2, "fixture", lambda: None)
            with self.assertRaisesRegex(
                    ValueError, "^embedding publication is incomplete"):
                common.read_embedding_commit(meta)
            with self.assertRaisesRegex(RuntimeError,
                                        "^q8 derivation failed via "):
                semantic_q8.build_from_f32(
                    embeddings, meta, root / "q8",
                    binary=binary, groups_path=groups)

    def test_windows_hash_reader_does_not_pin_embedding_publication(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            embeddings = root / "embeddings.f32"
            ids_path = root / "embeddings.ids"
            matrix = np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype="<f4")
            common.write_embeddings(
                ["a", "b"], matrix, embeddings_path=embeddings,
                ids_path=ids_path, dim=2, model_id="fixture",
                text_hashes=["a" * 16, "b" * 16])
            with mock.patch.object(common, "WIN", True), \
                    mock.patch.object(common, "EMBEDDINGS_PATH", embeddings):
                hashes = ask._open_embedding_hash_blob(2)
                snapshot = Path(hashes.file.name)
                try:
                    self.assertNotEqual(snapshot, embeddings.with_suffix(".hashes"))
                    common.write_embeddings(
                        ["a", "b"], matrix, embeddings_path=embeddings,
                        ids_path=ids_path, dim=2, model_id="fixture",
                        text_hashes=["c" * 16, "d" * 16])
                    self.assertEqual(hashes.at(0), "a" * 16)
                    self.assertEqual(
                        embeddings.with_suffix(".hashes").read_text(encoding="utf-8"),
                        "c" * 16 + "\n" + "d" * 16 + "\n")
                finally:
                    hashes.close()
                self.assertFalse(snapshot.exists())

    @staticmethod
    def _fixture(root: Path, generation: str) -> tuple[Path, Path, np.ndarray]:
        rng = np.random.default_rng(7)
        matrix = rng.normal(size=(17, 24)).astype("<f4")
        matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
        embeddings = root / "embeddings.f32"
        embeddings.write_bytes(matrix.tobytes())
        meta = root / "embeddings.meta"
        meta.write_text(json.dumps({
            "dim": matrix.shape[1],
            "model": "fixture",
            "commit": {
                "version": 1,
                "generation": generation,
                "rows": matrix.shape[0],
                "matrix": {"size": embeddings.stat().st_size},
            },
        }), encoding="utf-8")
        return embeddings, meta, matrix

    def test_builder_and_scanner_return_full_parity_vector(self) -> None:
        binary = common.ingest_bin()
        if not binary.exists():
            self.skipTest("agrep-rs has not been built")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            generation = "01" * 16
            embeddings, meta, matrix = self._fixture(root, generation)
            groups = root / "groups.ids"
            groups.write_text(
                "".join(f"family-{row // 3}\n" for row in range(len(matrix))),
                encoding="utf-8")
            record = semantic_q8.build_from_f32(
                embeddings, meta, root / "q8", binary=binary,
                groups_path=groups)
            manifest = {
                **record,
                "artifact_path": Path(record["artifact"]),
                "group_artifact_path": Path(record["group_artifact"]),
            }
            scanner = semantic_q8._Q8Scanner(manifest)
            try:
                query = matrix[3]
                scores = scanner.score(query, generation)
                expected = matrix @ query
                self.assertEqual(scores.shape, expected.shape)
                self.assertEqual(int(np.argmax(scores)), int(np.argmax(expected)))
                self.assertLess(float(np.max(np.abs(scores - expected))), 0.02)
                ordinals, candidate_scores = scanner.top(query, generation, 5)
                expected_order = np.argsort(scores)[::-1][:5]
                np.testing.assert_array_equal(ordinals, expected_order)
                np.testing.assert_allclose(candidate_scores, scores[expected_order])
                grouped_ordinals, grouped_scores = scanner.top(
                    query, generation, 5, grouped=True, heads=1)
                expected_groups = []
                for start in range(0, len(scores), 3):
                    rows = np.arange(start, min(start + 3, len(scores)))
                    best = int(rows[np.argmax(scores[rows])])
                    expected_groups.append(best)
                expected_groups.sort(key=lambda row: (-scores[row], row))
                np.testing.assert_array_equal(
                    grouped_ordinals, expected_groups[:5])
                np.testing.assert_allclose(
                    grouped_scores, scores[grouped_ordinals])
                many_ordinals, many_scores = scanner.top(
                    query, generation, 5, grouped=True, heads=8)
                group_rows = [
                    list(range(start, min(start + 3, len(scores))))
                    for start in range(0, len(scores), 3)
                ]
                selected = sorted(
                    range(len(group_rows)),
                    key=lambda group: (-max(scores[group_rows[group]]), group),
                )[:5]
                expected_many = sorted(
                    (row for group in selected for row in group_rows[group]),
                    key=lambda row: (-scores[row], row),
                )
                np.testing.assert_array_equal(many_ordinals, expected_many)
                np.testing.assert_allclose(many_scores, scores[many_ordinals])
                eligible_refs = np.asarray([1, 3, 7, 11, 15], dtype=np.int64)
                filtered_ordinals, filtered_scores = scanner.top(
                    query, generation, 5, eligible=eligible_refs)
                expected_filtered = sorted(
                    map(int, eligible_refs), key=lambda row: (-scores[row], row))
                np.testing.assert_array_equal(filtered_ordinals, expected_filtered)
                np.testing.assert_allclose(filtered_scores, scores[filtered_ordinals])
                eligible_mask = np.zeros(len(scores), dtype=np.bool_)
                eligible_mask[[0, 1, 4, 7]] = True
                grouped_filtered, grouped_filtered_scores = scanner.top(
                    query, generation, 4, grouped=True, heads=1,
                    eligible=eligible_mask)
                expected_grouped_filtered = sorted(
                    [max((0, 1), key=lambda row: (scores[row], -row)), 4, 7],
                    key=lambda row: (-scores[row], row))
                np.testing.assert_array_equal(
                    grouped_filtered, expected_grouped_filtered)
                np.testing.assert_allclose(
                    grouped_filtered_scores, scores[grouped_filtered])
                empty_ordinals, empty_scores = scanner.top(
                    query, generation, 5,
                    eligible=np.zeros(len(scores), dtype=np.bool_))
                self.assertEqual(len(empty_ordinals), 0)
                self.assertEqual(len(empty_scores), 0)
                with self.assertRaises(ValueError):
                    scanner.top(query, generation, 5,
                                eligible=np.zeros(len(scores) - 1, dtype=np.bool_))
                with self.assertRaises(RuntimeError):
                    scanner.score(query, "02" * 16)
                with self.assertRaises(RuntimeError):
                    scanner.top(query, "02" * 16, 5)
            finally:
                scanner.close()

    def test_header_corruption_is_rejected_before_ready(self) -> None:
        binary = common.ingest_bin()
        if not binary.exists():
            self.skipTest("agrep-rs has not been built")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            embeddings, meta, _ = self._fixture(root, "03" * 16)
            record = semantic_q8.build_from_f32(
                embeddings, meta, root / "q8", binary=binary)
            artifact = Path(record["artifact"])
            payload = bytearray(artifact.read_bytes())
            payload[0] ^= 0x20
            artifact.write_bytes(payload)
            result = subprocess.run(
                [str(binary), "semantic-q8-serve", "--artifact", str(artifact)],
                input=b"", capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"bad magic", result.stderr)

    def test_generation_change_builds_a_distinct_immutable_artifact(self) -> None:
        binary = common.ingest_bin()
        if not binary.exists():
            self.skipTest("agrep-rs has not been built")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            embeddings, meta, _ = self._fixture(root, "04" * 16)
            first = semantic_q8.build_from_f32(
                embeddings, meta, root / "q8", binary=binary)
            record = json.loads(meta.read_text(encoding="utf-8"))
            record["commit"]["generation"] = "05" * 16
            meta.write_text(json.dumps(record), encoding="utf-8")
            second = semantic_q8.build_from_f32(
                embeddings, meta, root / "q8", binary=binary)
            self.assertNotEqual(first["artifact"], second["artifact"])
            self.assertTrue(Path(first["artifact"]).exists())
            self.assertTrue(Path(second["artifact"]).exists())

    def test_same_generation_repair_never_replaces_artifact_paths(self) -> None:
        binary = common.ingest_bin()
        if not binary.exists():
            self.skipTest("agrep-rs has not been built")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            generation = "06" * 16
            embeddings, meta, matrix = self._fixture(root, generation)
            groups = root / "groups.ids"
            groups.write_text(
                "".join(f"family-{row // 3}\n" for row in range(len(matrix))),
                encoding="utf-8")
            first = semantic_q8.build_from_f32(
                embeddings, meta, root / "q8", binary=binary,
                groups_path=groups)
            old_paths = [Path(first["artifact"]), Path(first["group_artifact"])]
            old_payloads = []
            for path in old_paths:
                payload = bytearray(path.read_bytes())
                payload[0] ^= 0x20
                path.write_bytes(payload)
                old_payloads.append(bytes(payload))

            repaired = semantic_q8.build_from_f32(
                embeddings, meta, root / "q8", binary=binary,
                groups_path=groups)
            new_paths = [Path(repaired["artifact"]), Path(repaired["group_artifact"])]
            self.assertNotEqual(old_paths, new_paths)
            self.assertTrue(all(path.exists() for path in old_paths + new_paths))
            self.assertEqual([path.read_bytes() for path in old_paths], old_payloads)

    def test_f16_artifact_is_exact_immutable_and_reused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            embeddings, _, matrix = self._fixture(root, "08" * 16)
            first = semantic_q8._build_f16(
                embeddings, root / "q8", generation="08" * 16,
                rows=len(matrix), dim=matrix.shape[1])
            artifact = Path(first["exact_artifact"])
            before = artifact.stat().st_mtime_ns
            stored = np.memmap(
                artifact, dtype="<f2", mode="r", shape=matrix.shape)
            np.testing.assert_array_equal(
                np.asarray(stored), matrix.astype("<f2"))
            del stored
            second = semantic_q8._build_f16(
                embeddings, root / "q8", generation="08" * 16,
                rows=len(matrix), dim=matrix.shape[1])
            self.assertEqual(first["exact_artifact"], second["exact_artifact"])
            self.assertEqual(before, artifact.stat().st_mtime_ns)
            payload = bytearray(artifact.read_bytes())
            payload[0] ^= 1
            artifact.write_bytes(payload)
            repaired_record = semantic_q8._build_f16(
                embeddings, root / "q8", generation="08" * 16,
                rows=len(matrix), dim=matrix.shape[1])
            repaired_path = Path(repaired_record["exact_artifact"])
            self.assertNotEqual(artifact, repaired_path)
            self.assertEqual(artifact.read_bytes(), bytes(payload))
            repaired = np.memmap(
                repaired_path, dtype="<f2", mode="r", shape=matrix.shape)
            np.testing.assert_array_equal(
                np.asarray(repaired), matrix.astype("<f2"))
            del repaired

    def test_excluded_roles_share_one_group_and_consume_at_most_eight_heads(self) -> None:
        binary = common.ingest_bin()
        if not binary.exists():
            self.skipTest("agrep-rs has not been built")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            rows, dim = 20, 24
            matrix = np.zeros((rows, dim), dtype="<f4")
            matrix[:12, 0] = 1.0
            for row in range(12, rows):
                matrix[row, 1 + row - 12] = 1.0
            embeddings = root / "embeddings.f32"
            embeddings.write_bytes(matrix.tobytes())
            generation = "09" * 16
            meta = root / "embeddings.meta"
            meta.write_text(json.dumps({
                "dim": dim, "model": "fixture", "commit": {
                    "version": 1, "generation": generation, "rows": rows,
                    "matrix": {"size": embeddings.stat().st_size},
                },
            }), encoding="utf-8")
            ids = root / "embeddings.ids"
            messages = root / "messages.jsonl"
            with (ids.open("w", encoding="utf-8", newline="\n") as ids_out,
                  messages.open("w", encoding="utf-8", newline="\n") as msg_out):
                for row in range(rows):
                    mid = f"codex:session-{row}:0"
                    ids_out.write(mid + "\n")
                    msg_out.write(json.dumps({
                        "id": mid, "text": f"row {row}",
                        "who": "control" if row < 12 else "user",
                    }) + "\n")
            labels = root / "groups.ids"
            semantic_q8._write_family_groups(
                labels, ids_path=ids, messages_path=messages, parents={})
            values = labels.read_text(encoding="utf-8").splitlines()
            self.assertEqual(set(values[:12]), {semantic_q8.EXCLUDED_GROUP})
            self.assertEqual(len(set(values[12:])), 8)
            record = semantic_q8.build_from_f32(
                embeddings, meta, root / "q8", binary=binary,
                groups_path=labels)
            manifest = {
                **record,
                "artifact_path": Path(record["artifact"]),
                "group_artifact_path": Path(record["group_artifact"]),
            }
            scanner = semantic_q8._Q8Scanner(manifest, binary=binary)
            try:
                ordinals, _ = scanner.top(
                    matrix[0], generation, 20, grouped=True, heads=8)
            finally:
                scanner.close()
            self.assertLessEqual(sum(int(row) < 12 for row in ordinals), 8)

    def test_published_bundle_reranks_without_opening_f32(self) -> None:
        binary = common.ingest_bin()
        if not binary.exists():
            self.skipTest("agrep-rs has not been built")
        with _q8_temporary_directory() as raw:
            root = Path(raw)
            rng = np.random.default_rng(31)
            matrix = rng.normal(size=(17, 24)).astype("<f4")
            matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
            ids = [f"codex:session-{row}:0" for row in range(len(matrix))]
            messages = root / "messages.jsonl"
            with messages.open("w", encoding="utf-8", newline="\n") as stream:
                for row, mid in enumerate(ids):
                    stream.write(json.dumps({
                        "id": mid, "text": f"message {row}", "who": "user",
                    }) + "\n")
            embeddings = root / "embeddings.f32"
            ids_path = root / "embeddings.ids"
            common.write_embeddings(
                ids, matrix, embeddings_path=embeddings, ids_path=ids_path,
                dim=matrix.shape[1], model_id="fixture")
            manifest_path = root / "embeddings.q8.meta"
            artifacts = root / "semantic-q8"
            patches = (
                _data_dir(root),
                mock.patch.object(common, "MESSAGES_PATH", messages),
                mock.patch.object(common, "EMBEDDINGS_PATH", embeddings),
                mock.patch.object(common, "IDS_PATH", ids_path),
                mock.patch.object(semantic_q8, "MANIFEST_PATH", manifest_path),
                mock.patch.object(semantic_q8, "ARTIFACT_DIR", artifacts),
                mock.patch.object(common, "ingest_bin", return_value=binary),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], \
                    patches[5], patches[6]:
                published = semantic_q8.publish_for_generation(
                    {"indexed": len(matrix), "pending": 0})
                self.assertIsNotNone(published)
                generation = published["f32_generation"]
                exact_path = published["exact_artifact_path"]
                before = exact_path.stat().st_mtime_ns
                reused = semantic_q8.publish_for_generation(
                    {"indexed": len(matrix), "pending": 0})
                self.assertEqual(exact_path, reused["exact_artifact_path"])
                self.assertEqual(before, exact_path.stat().st_mtime_ns)
                with mock.patch.object(
                        semantic_q8, "ensure_artifact",
                        side_effect=AssertionError("query attempted a build")):
                    self.assertTrue(semantic_q8.artifact_available(generation))
                    candidates = semantic_q8.grouped_exact_candidates(
                        matrix[3], generation, k=len(matrix), heads=8)
                self.assertIsNotNone(candidates)
                ordinals, scores, group_ids, group_count = candidates
                expected = matrix.astype("<f2").astype(np.float32) @ matrix[3]
                expected_order = np.lexsort((np.arange(len(matrix)), -expected))
                np.testing.assert_array_equal(ordinals, expected_order)
                np.testing.assert_allclose(scores, expected[expected_order])
                np.testing.assert_array_equal(group_ids, expected_order)
                self.assertEqual(group_count, len(matrix))

                previous_state = semantic_q8._f32_state()
                artifact_generation = generation
                artifact_paths = (
                    published["artifact_path"], published["group_artifact_path"],
                    published["exact_artifact_path"],
                )
                extra = rng.normal(size=(1, matrix.shape[1])).astype("<f4")
                extra /= np.linalg.norm(extra, axis=1, keepdims=True)
                matrix = np.vstack((matrix, extra)).astype("<f4", copy=False)
                ids.append(f"codex:session-{len(ids)}:0")
                with messages.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(json.dumps({
                        "id": ids[-1], "text": "partial generation row",
                        "who": "user",
                    }) + "\n")
                common.write_embeddings(
                    ids, matrix, embeddings_path=embeddings, ids_path=ids_path,
                    dim=matrix.shape[1], model_id="fixture")
                partial = semantic_q8.publish_for_generation({
                    "indexed": len(matrix), "total": len(matrix) + 7,
                    "pending": 7,
                }, previous_state=previous_state, strict_append=True)
                self.assertIsNotNone(partial)
                self.assertNotEqual(generation, partial["f32_generation"])
                self.assertEqual(partial["artifact_generation"], artifact_generation)
                self.assertEqual(partial["generation_relation"], "prefix")
                self.assertEqual(partial["rows"], len(matrix) - 1)
                self.assertEqual(partial["f32_rows"], len(matrix))
                self.assertEqual(
                    artifact_paths,
                    (partial["artifact_path"], partial["group_artifact_path"],
                     partial["exact_artifact_path"]),
                )
                generation = partial["f32_generation"]

                class Refs:
                    @staticmethod
                    def resolve(ordinals):
                        return [{
                            "session": f"session-{int(ordinal)}",
                            "project": "p", "agent": "codex", "who": "user",
                            "model": "m", "ts": int(ordinal),
                            "turn": int(ordinal), "text": f"answer {int(ordinal)}",
                        } for ordinal in ordinals]

                coverage = {
                    "indexed": len(matrix), "total": len(matrix) + 7,
                    "pending": 7, "complete": False,
                }
                filters = {"_exclude_who": tuple(sorted(
                    ask._Q8_DEFAULT_EXCLUDED_ROLES))}
                with (mock.patch.object(ask.common, "read_index_meta",
                                        return_value=(matrix.shape[1], "fixture")),
                      mock.patch.object(ask, "_guard_embedder"),
                      mock.patch.object(ask, "_require_current_message_index",
                                        return_value=coverage),
                      mock.patch.object(ask, "_message_refs_from_pointer",
                                        return_value=Refs()),
                      mock.patch.object(ask, "_embed_query", return_value=matrix[3]),
                      mock.patch.object(ask, "_summary_artifacts",
                                        side_effect=RuntimeError("no summaries")),
                      mock.patch.object(ask, "_message_artifacts") as f32,
                      mock.patch.dict(ask._CURRENT_MESSAGE_STATE,
                                      {"generation": generation}, clear=False),
                      mock.patch("explore._session_concept", return_value={})):
                    payload = json.loads(ask.tool_search_hybrid(
                        "query", k=2, filters=filters, timing=True))
                f32.assert_not_called()
                self.assertTrue(payload["partial"])
                self.assertEqual(payload["semantic_accelerator_coverage"], {
                    "indexed": len(matrix) - 1, "total": len(matrix),
                    "pending": 1, "complete": False,
                })
                phases = payload["_semantic_timing"]["phases_ms"]
                self.assertIn("q8_retrieval", phases)
                self.assertNotIn("matmul", phases)

                record = json.loads(
                    (root / "embeddings.meta").read_text(encoding="utf-8"))
                record["commit"]["generation"] = "0a" * 16
                (root / "embeddings.meta").write_text(
                    json.dumps(record), encoding="utf-8")
                self.assertFalse(semantic_q8.artifact_available(generation))
                self.assertIsNone(semantic_q8.grouped_exact_candidates(
                    matrix[3], generation, k=len(matrix), heads=8))

    def test_matrix_generation_mismatch_falls_back_without_starting_scanner(self) -> None:
        class Matrix:
            _agrep_commit_generation = "06" * 16

        matrix = Matrix()
        state = {"commit": {"generation": "07" * 16}}
        with (mock.patch.object(semantic_q8, "enabled", return_value=True),
              mock.patch.object(semantic_q8, "_f32_state", return_value=state),
              mock.patch.object(semantic_q8, "ensure_artifact") as ensure):
            self.assertIsNone(semantic_q8.shadow_scores(np.ones(3), matrix))
            ensure.assert_not_called()

    def test_shadow_mismatch_keeps_f32_kind_and_avoids_its_confidence_floor(self) -> None:
        scores = np.asarray([0.81, 0.9], dtype=np.float32)
        with (mock.patch.dict(os.environ, {"AGREP_SEMANTIC_Q8_SHADOW": "1"}),
              mock.patch.object(semantic_q8, "shadow_scores", return_value=None)):
            diagnostic = ask._q8_shadow_comparison(np.ones(3), object(), scores)
        self.assertEqual(diagnostic["state"], "f32-fallback")
        self.assertEqual(diagnostic["score_kind"], semantic_q8.SCORE_KIND)
        self.assertFalse(diagnostic["used_for_ranking"])
        self.assertNotIn("min_score", diagnostic)

    def test_hybrid_main_path_uses_exact_q8_pool_without_mapping_f32(self) -> None:
        class Refs:
            @staticmethod
            def resolve(ordinals):
                return [{
                    "session": f"session-{int(ordinal)}", "project": "p",
                    "agent": "codex", "who": "user", "model": "m",
                    "ts": int(ordinal), "turn": int(ordinal),
                    "text": f"answer {int(ordinal)}",
                } for ordinal in ordinals]

        coverage = {
            "indexed": 3, "total": 3, "pending": 0,
            "fraction": 1.0, "complete": True, "order": "complete",
        }
        filters = {"_exclude_who": tuple(sorted(
            ask._Q8_DEFAULT_EXCLUDED_ROLES))}
        candidates = (
            np.asarray([2, 0, 1]), np.asarray([0.91, 0.89, 0.80]),
            np.asarray([1, 0, 0], dtype=np.uint32), 3,
        )
        with (mock.patch.object(ask.common, "read_index_meta",
                                return_value=(2, "fixture")),
              mock.patch.object(ask, "_guard_embedder"),
              mock.patch.object(ask, "_require_current_message_index",
                                return_value=coverage),
              mock.patch.object(ask, "_message_refs_from_pointer",
                                return_value=Refs()),
              mock.patch.object(ask, "_embed_query",
                                return_value=np.asarray([1.0, 0.0])),
              mock.patch.object(ask, "_summary_artifacts",
                                side_effect=RuntimeError("no summaries")),
              mock.patch.object(ask, "_message_artifacts") as f32,
              mock.patch.object(semantic_q8, "artifact_available",
                                return_value=True),
              mock.patch.object(semantic_q8, "accelerator_coverage",
                                return_value={
                                    "indexed": 3, "total": 3,
                                    "pending": 0, "complete": True,
                                }),
              mock.patch.object(semantic_q8, "grouped_exact_candidates",
                                return_value=candidates),
              mock.patch.dict(ask._CURRENT_MESSAGE_STATE,
                              {"generation": "0b" * 16}, clear=False),
              mock.patch("explore._session_concept", return_value={})):
            payload = json.loads(ask.tool_search_hybrid(
                "query", k=2, filters=filters, timing=True))
        f32.assert_not_called()
        self.assertEqual(
            [row["session"] for row in payload["results"]],
            ["session-2", "session-0"])
        self.assertIn("q8_retrieval", payload["_semantic_timing"]["phases_ms"])
        self.assertNotIn("matmul", payload["_semantic_timing"]["phases_ms"])

    def test_rebound_pool_uses_generation_family_ids_without_session_scan(self) -> None:
        class Refs:
            @staticmethod
            def resolve(ordinals):
                sessions = ("child", "root", "other")
                return [{
                    "session": sessions[int(ordinal)], "who": "user",
                    "text": f"answer {int(ordinal)}",
                } for ordinal in ordinals]

        candidates = (
            np.asarray([0, 1, 2]), np.asarray([0.95, 0.94, 0.90]),
            np.asarray([0, 0, 2], dtype=np.uint32), 3,
        )
        filters = {"_exclude_who": tuple(sorted(
            ask._Q8_DEFAULT_EXCLUDED_ROLES))}
        with (mock.patch.object(semantic_q8, "grouped_exact_candidates",
                                return_value=candidates),
              mock.patch.dict(ask._CURRENT_MESSAGE_STATE,
                              {"generation": "0c" * 16}, clear=False)):
            rows, scores, _ = ask._q8_grouped_pool(
                np.asarray([1.0, 0.0]), Refs(), filters, 2)
        self.assertEqual([row["session"] for row in rows], ["child", "other"])
        np.testing.assert_allclose(scores, [0.95, 0.90])

    def test_grouped_pool_drops_caller_family_without_refs_scan(self) -> None:
        class Refs:
            @staticmethod
            def family_id_for_session(session):
                return 1 if session == "caller" else None

            @staticmethod
            def q8_eligibility(_filters):
                raise AssertionError("caller family forced a refs scan")

            @staticmethod
            def resolve(ordinals):
                families = (1, 2, 3)
                return [{
                    "session": f"session-{int(ordinal)}",
                    "who": "user",
                    "family_id": families[int(ordinal)],
                    "text": f"answer {int(ordinal)}",
                } for ordinal in ordinals]

        candidates = (
            np.asarray([0, 1, 2]),
            np.asarray([0.95, 0.90, 0.85]),
            np.asarray([1, 2, 3], dtype=np.uint32),
            4,
        )
        filters = {
            "_exclude_who": tuple(sorted(ask._Q8_DEFAULT_EXCLUDED_ROLES)),
            "exclude_session": "caller",
        }
        with mock.patch.object(
                semantic_q8, "grouped_exact_candidates",
                return_value=candidates) as grouped, \
                mock.patch.dict(
                    ask._CURRENT_MESSAGE_STATE,
                    {"generation": "0d" * 16},
                    clear=False):
            rows, _scores, count = ask._q8_grouped_pool(
                np.asarray([1.0, 0.0]), Refs(), filters, 2)
        self.assertEqual(
            [row["session"] for row in rows],
            ["session-1", "session-2"],
        )
        self.assertEqual(count, 2)
        self.assertIsNone(grouped.call_args.kwargs["eligible"])

    def test_grouped_pool_enforces_only_the_recap_window(self) -> None:
        class Refs:
            filters = None

            @classmethod
            def q8_eligibility(cls, filters):
                cls.filters = filters
                return np.asarray([0, 2, 3, 4]), 4, 3

            @staticmethod
            def resolve(ordinals):
                rows = (
                    {"session": "caller", "turn": 2, "family_id": 1,
                     "side": False},
                    {"session": "caller", "turn": 7, "family_id": 1,
                     "side": False},
                    {"session": "child", "turn": 20, "family_id": 1,
                     "side": False},
                    {"session": "agent-echo", "turn": 0, "family_id": 1,
                     "side": True},
                    {"session": "other", "turn": 30, "family_id": 2,
                     "side": False},
                )
                return [
                    {**rows[int(ordinal)], "who": "user",
                     "text": f"answer {int(ordinal)}"}
                    for ordinal in ordinals
                ]

        candidates = (
            np.asarray([0, 2, 3, 4]),
            np.asarray([0.95, 0.96, 0.97, 0.85]),
            np.asarray([1, 1, 1, 2], dtype=np.uint32),
            3,
        )
        filters = {
            "_exclude_who": tuple(sorted(ask._Q8_DEFAULT_EXCLUDED_ROLES)),
            "exclude_session": "caller",
            "exclude_session_from_turn": 7,
        }
        with mock.patch.object(
                semantic_q8, "grouped_exact_candidates",
                return_value=candidates) as grouped, \
                mock.patch.dict(
                    ask._CURRENT_MESSAGE_STATE,
                    {"generation": "0e" * 16},
                    clear=False):
            rows, _scores, count = ask._q8_grouped_pool(
                np.asarray([1.0, 0.0]), Refs(), filters, 3)
        self.assertEqual(
            [row["session"] for row in rows],
            ["agent-echo", "other"],
        )
        self.assertEqual(count, 3)
        self.assertEqual(Refs.filters, filters)
        np.testing.assert_array_equal(
            grouped.call_args.kwargs["eligible"], [0, 2, 3, 4])

    def test_grouped_pool_resolves_family_from_an_embedded_sibling(self) -> None:
        class Refs:
            resolved_members = None

            @staticmethod
            def family_id_for_session(_session):
                return None

            @classmethod
            def family_id_for_sessions(cls, sessions):
                cls.resolved_members = frozenset(sessions)
                return 7

            @staticmethod
            def q8_eligibility(_filters):
                raise AssertionError("caller family forced a refs scan")

            @staticmethod
            def resolve(ordinals):
                return [{
                    "session": f"session-{int(ordinal)}",
                    "who": "user",
                    "family_id": int(group),
                    "text": f"answer {int(ordinal)}",
                } for ordinal, group in zip(ordinals, (8, 9))]

        candidates = (
            np.asarray([0, 1, 2]),
            np.asarray([0.95, 0.90, 0.85]),
            np.asarray([7, 8, 9], dtype=np.uint32),
            10,
        )
        filters = {
            "_exclude_who": tuple(sorted(ask._Q8_DEFAULT_EXCLUDED_ROLES)),
            "exclude_session": "caller",
        }
        family = (
            "root", frozenset({"caller", "embedded-sibling"}), frozenset())
        with mock.patch.object(
                semantic_q8, "grouped_exact_candidates",
                return_value=candidates), \
                mock.patch.object(
                    ask.common, "indexed_calling_family_with_sides",
                    return_value=family), \
                mock.patch.dict(
                    ask._CURRENT_MESSAGE_STATE,
                    {"generation": "0f" * 16},
                    clear=False):
            rows, _scores, count = ask._q8_grouped_pool(
                np.asarray([1.0, 0.0]), Refs(), filters, 2)
        self.assertEqual(Refs.resolved_members, family[1])
        self.assertEqual(
            [row["session"] for row in rows],
            ["session-1", "session-2"],
        )
        self.assertEqual(count, 8)

    def test_split_family_ids_force_preselection_eligibility(self) -> None:
        class Refs:
            scanned = False

            @staticmethod
            def family_id_for_session(_session):
                return None

            @staticmethod
            def family_id_for_sessions(_sessions):
                raise ask.segment_query.SegmentQueryError("split family")

            @classmethod
            def q8_eligibility(cls, filters):
                cls.scanned = True
                self.assertIn("_exclude_sessions", filters)
                self.assertNotIn("_exclude_sessions_json", filters)
                return np.asarray([1, 2]), 2, 2

            @staticmethod
            def resolve(ordinals):
                return [{
                    "session": f"session-{int(ordinal)}",
                    "who": "user", "family_id": int(ordinal),
                    "text": f"answer {int(ordinal)}",
                } for ordinal in ordinals]

        family = ("root", frozenset({"caller", "sibling"}), frozenset())
        candidates = (
            np.asarray([1, 2]), np.asarray([0.9, 0.8]),
            np.asarray([1, 2], dtype=np.uint32), 3,
        )
        filters = {
            "_exclude_who": tuple(sorted(ask._Q8_DEFAULT_EXCLUDED_ROLES)),
            "exclude_session": "caller",
        }
        with mock.patch.object(
                ask.common, "indexed_calling_family_with_sides",
                return_value=family), \
                mock.patch.object(
                    semantic_q8, "grouped_exact_candidates",
                    return_value=candidates), \
                mock.patch.dict(
                    ask._CURRENT_MESSAGE_STATE,
                    {"generation": "10" * 16},
                    clear=False):
            ask._q8_grouped_pool(
                np.asarray([1.0, 0.0]), Refs(), filters, 2)
        self.assertTrue(Refs.scanned)

    def test_authoritative_family_id_avoids_serialized_membership(self) -> None:
        class Refs:
            @staticmethod
            def family_id_for_session(_session):
                return 7

        family = (
            "root",
            frozenset({"caller", *(f"sibling-{index}" for index in range(4_095))}),
            frozenset(),
        )
        with mock.patch.object(
                ask.common, "indexed_calling_family_with_sides",
                return_value=family):
            filters = ask._message_filters(
                Refs(), {"exclude_session": "caller"})
        self.assertEqual(filters["_exclude_family_id"], 7)
        self.assertNotIn("_exclude_sessions_json", filters)
        self.assertEqual(filters["_exclude_sessions"], family[1])
        self.assertFalse(ask._matches(
            {"session": "sibling-1", "family_id": 7}, filters))
        self.assertTrue(ask._matches(
            {"session": "past", "family_id": 8}, filters))
        with mock.patch.object(
                ask.common, "indexed_calling_family_with_sides",
                side_effect=AssertionError("family decoded twice")):
            where, params = ask._MessageRefStore._where(filters)
        self.assertIn("json_each", where)
        self.assertTrue(any("sibling-4094" in str(param) for param in params))

    def test_windowed_filter_never_resolves_or_excludes_family(self) -> None:
        class Refs:
            @staticmethod
            def family_id_for_session(_session):
                raise AssertionError("a caller window needs no family lookup")

        with mock.patch.object(
                ask.common, "indexed_calling_family_with_sides",
                side_effect=AssertionError(
                    "a caller window needs no family expansion")):
            filters = ask._message_filters(
                Refs(), {
                    "exclude_session": "caller",
                    "exclude_session_from_turn": 12,
                })
        self.assertEqual(filters, {
            "exclude_session": "caller",
            "exclude_session_from_turn": 12,
        })
        self.assertTrue(ask._matches(
            {"session": "custom-side", "family_id": 7, "side": True},
            filters))
        self.assertTrue(ask._matches(
            {"session": "ordinary-child", "family_id": 7, "side": False},
            filters))
        where, params = ask._MessageRefStore._where(filters)
        self.assertNotIn("side", where)
        self.assertEqual(params, ["caller", 12])

    def test_exact_exclusion_sequence_normalizes_before_row_matching(
            self) -> None:
        for value in (["root"], ("root",)):
            with self.subTest(container=type(value).__name__):
                filters = {"_exclude_sessions": value}
                self.assertFalse(ask._matches({"session": "root"}, filters))
                self.assertTrue(ask._matches({"session": "other"}, filters))
                self.assertEqual(filters["_exclude_sessions"],
                                 frozenset({"root"}))

    def test_serialized_family_membership_decodes_once_per_query(self) -> None:
        members = [f"session-{index}" for index in range(4_096)]
        filters = {
            "_exclude_sessions_json": json.dumps(
                members, separators=(",", ":")),
        }
        original = json.loads
        with mock.patch.object(ask.json, "loads", wraps=original) as loads:
            for index in range(256):
                self.assertTrue(ask._matches(
                    {"session": f"past-{index}"}, filters))
            self.assertFalse(ask._matches({"session": members[-1]}, filters))
        loads.assert_called_once()
        self.assertEqual(len(filters["_exclude_sessions"]), len(members))

    def test_flat_pool_clears_group_from_cached_base_bitmap(self) -> None:
        class Refs:
            manifest = {"live_rows": 8}
            live = np.ones(8, dtype=np.bool_)

            def __init__(self):
                self.filters = None
                self.base = semantic_q8.PackedEligibility(
                    np.asarray([0xFF], dtype=np.uint8), 8, 8)

            @staticmethod
            def family_id_for_session(session):
                return 7 if session == "caller" else None

            def q8_eligibility(self, filters):
                self.filters = filters
                return self.base, 8, 4

            @staticmethod
            def resolve(ordinals):
                return [{
                    "session": f"past-{int(ordinal)}",
                    "who": "user",
                    "family_id": int(ordinal) + 1,
                    "text": f"answer {int(ordinal)}",
                } for ordinal in ordinals]

        refs = Refs()
        base_without_family = semantic_q8.PackedEligibility(
            np.asarray([0x3F], dtype=np.uint8), 8, 6)
        candidates = (
            np.asarray([0, 1]), np.asarray([0.9, 0.8], dtype=np.float32))
        filters = {
            "_exclude_who": tuple(sorted(ask._Q8_DEFAULT_EXCLUDED_ROLES)),
            "exclude_session": "caller",
        }
        with mock.patch.object(
                semantic_q8,
                "eligibility_without_group",
                return_value=base_without_family) as exclude, \
                mock.patch.object(
                    semantic_q8, "exact_candidates",
                    return_value=candidates), \
                mock.patch.dict(
                    ask._CURRENT_MESSAGE_STATE,
                    {"generation": "0e" * 16},
                    clear=False):
            rows, _scores, count = ask._q8_flat_pool(
                np.asarray([1.0, 0.0]), refs, filters, 2)
        self.assertEqual(
            refs.filters,
            {"_exclude_who": filters["_exclude_who"]},
        )
        self.assertEqual(exclude.call_args.args[1], 7)
        self.assertIs(exclude.call_args.args[3], refs.base)
        self.assertEqual([row["session"] for row in rows], ["past-0", "past-1"])
        self.assertEqual(count, 6)

    def test_group_exclusion_without_live_mask_starts_from_all_rows(self) -> None:
        manifest = {"rows": 4, "group_count": 3, "storage_version": 1}
        groups = np.asarray([0, 1, 1, 2], dtype=np.uint32)
        with mock.patch.object(
                semantic_q8, "_ready_manifest", return_value=manifest), \
                mock.patch.object(
                    semantic_q8, "_groups_for_manifest", return_value=groups):
            eligible = semantic_q8.eligibility_without_group(
                "10" * 16, 1, None)
        self.assertIsNotNone(eligible)
        self.assertEqual(eligible.count, 2)
        np.testing.assert_array_equal(
            np.unpackbits(eligible.bits, bitorder="little")[:4],
            [1, 0, 0, 1],
        )

    def test_group_exclusion_accepts_a_longer_prefix_generation(self) -> None:
        manifest = {"rows": 4, "group_count": 3, "storage_version": 1}
        live = np.ones(6, dtype=np.bool_)
        base = semantic_q8.PackedEligibility(
            np.asarray([0x3F], dtype=np.uint8), 6, 6)
        with mock.patch.object(
                semantic_q8, "_ready_manifest", return_value=manifest):
            eligible = semantic_q8.eligibility_without_group(
                "11" * 16, 7, live, base)
        self.assertIsNotNone(eligible)
        self.assertEqual((eligible.rows, eligible.count), (4, 4))
        np.testing.assert_array_equal(
            np.unpackbits(eligible.bits, bitorder="little")[:4],
            [1, 1, 1, 1],
        )

    def test_rebound_pool_exhausts_bounded_candidates_after_family_merges(self) -> None:
        class Refs:
            @staticmethod
            def resolve(ordinals):
                return [{
                    "session": (f"child-{ordinal}" if int(ordinal) < 120
                                else f"other-{int(ordinal) - 120}"),
                    "who": "user", "text": f"answer {int(ordinal)}",
                } for ordinal in ordinals]

        ordinals = np.arange(128, dtype=np.int64)
        candidates = (
            ordinals, np.linspace(1.0, 0.5, 128, dtype=np.float32),
            np.arange(128, dtype=np.uint32), 128,
        )
        groups = np.concatenate((
            np.zeros(120, dtype=np.uint32),
            np.arange(1, 9, dtype=np.uint32),
        ))
        candidates = (candidates[0], candidates[1], groups, 9)
        filters = {"_exclude_who": tuple(sorted(
            ask._Q8_DEFAULT_EXCLUDED_ROLES))}
        with (mock.patch.object(semantic_q8, "grouped_exact_candidates",
                                return_value=candidates),
              mock.patch.dict(ask._CURRENT_MESSAGE_STATE,
                              {"generation": "0c" * 16}, clear=False)):
            rows, _scores, _ = ask._q8_grouped_pool(
                np.asarray([1.0, 0.0]), Refs(), filters, 5)
        self.assertEqual(rows[0]["session"], "child-0")
        self.assertEqual(len(rows), 9)
        self.assertEqual(rows[-1]["session"], "other-7")

    def test_partial_rebuilds_keep_geometric_milestones(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            manifest = Path(raw) / "embeddings.q8.meta"
            with mock.patch.object(semantic_q8, "MANIFEST_PATH", manifest):
                self.assertTrue(semantic_q8._partial_publish_due(128))
                manifest.write_text(json.dumps({
                    "version": semantic_q8.MANIFEST_VERSION, "rows": 128,
                }), encoding="utf-8")
                self.assertFalse(semantic_q8._partial_publish_due(255))
                self.assertTrue(semantic_q8._partial_publish_due(256))
                manifest.write_text(json.dumps({
                    "version": semantic_q8.MANIFEST_VERSION, "rows": 200_000,
                }), encoding="utf-8")
                self.assertFalse(semantic_q8._partial_publish_due(299_999))
                self.assertTrue(semantic_q8._partial_publish_due(300_000))

    def test_missing_q8_bundle_falls_back_to_existing_f32_path(self) -> None:
        class Refs:
            @staticmethod
            def best_by_session(_scores, _filters):
                return {"session-a": 0}

            @staticmethod
            def resolve(_ordinals):
                return [{
                    "session": "session-a", "project": "p", "agent": "codex",
                    "who": "user", "model": "m", "ts": 1, "turn": 2,
                    "text": "answer",
                }]

        matrix = np.asarray([[1.0, 0.0]], dtype=np.float32)
        coverage = {"indexed": 1, "total": 1, "complete": True}
        filters = {"_exclude_who": tuple(sorted(
            ask._Q8_DEFAULT_EXCLUDED_ROLES))}
        with (mock.patch.object(ask.common, "read_index_meta",
                                return_value=(2, "fixture")),
              mock.patch.object(ask, "_guard_embedder"),
              mock.patch.object(semantic_q8, "artifact_available",
                                return_value=False),
              mock.patch.object(ask, "_message_artifacts",
                                return_value=(("m0",), matrix, Refs(), coverage)) as f32,
              mock.patch.object(ask, "_embed_query",
                                return_value=np.asarray([1.0, 0.0])),
              mock.patch.object(ask, "_summary_artifacts",
                                side_effect=RuntimeError("no summaries")),
              mock.patch.object(ask, "_family_representatives",
                                return_value=np.asarray([0])),
              mock.patch("explore._session_concept", return_value={})):
            payload = json.loads(ask.tool_search_hybrid(
                "query", k=1, filters=filters))
        f32.assert_called_once()
        self.assertEqual(payload["results"][0]["session"], "session-a")

    def test_flat_family_mode_uses_bounded_native_session_heads(self) -> None:
        class Refs:
            manifest = {"live_rows": 300_001}

            @staticmethod
            def resolve(ordinals):
                rows = (
                    {"session": "side-a", "ordinal": 0},
                    {"session": "side-a", "ordinal": 1},
                    {"session": "side-b", "ordinal": 2},
                )
                return [{**rows[int(row)], "who": "user", "text": "answer"}
                        for row in ordinals]

        candidates = (
            np.asarray([0, 1, 2]),
            np.asarray([0.95, 0.90, 0.85], dtype=np.float32),
        )
        allowed = mock.Mock(count=300_000)
        with mock.patch.object(
                semantic_q8, "exact_candidates", return_value=candidates) as exact, \
                mock.patch.object(
                    semantic_q8, "eligibility_without_group",
                    return_value=allowed) as exclude, \
                mock.patch.dict(
                    ask._CURRENT_MESSAGE_STATE,
                    {"generation": "0f" * 16}, clear=False):
            rows, scores, known = ask._q8_session_pool(
                np.asarray([1.0, 0.0]), Refs(),
                {"_family_diverse": False,
                 "_exclude_who": tuple(sorted(
                     ask._Q8_DEFAULT_EXCLUDED_ROLES))}, 200)
        self.assertEqual([row["session"] for row in rows], ["side-a", "side-b"])
        np.testing.assert_allclose(scores, np.asarray([0.95, 0.85]))
        self.assertEqual(known, 2)
        self.assertEqual(exact.call_args.kwargs["k"], 4096)
        exclude.assert_called_once_with("0f" * 16, 0, None, None)

    def test_hidden_multi_role_q8_keeps_distinct_sessions(self) -> None:
        class Refs:
            manifest = {"live_rows": 300_001}

            @staticmethod
            def q8_eligibility(filters):
                self.assertEqual(
                    filters["_include_who"], ("control", "synthetic"))
                return None, 300_001, None

            @staticmethod
            def resolve(ordinals):
                rows = (
                    {"session": "control-side", "ordinal": 0,
                     "who": "control", "text": "control row"},
                    {"session": "synthetic-side", "ordinal": 1,
                     "who": "synthetic", "text": "synthetic row"},
                )
                return [rows[int(row)] for row in ordinals]

        candidates = (
            np.asarray([0, 1]),
            np.asarray([0.95, 0.90], dtype=np.float32),
        )
        filters = {
            "_family_diverse": True,
            "_include_who": ("control", "synthetic"),
        }
        with mock.patch.object(
                semantic_q8, "exact_candidates", return_value=candidates), \
                mock.patch.dict(
                    ask._CURRENT_MESSAGE_STATE,
                    {"generation": "0f" * 16}, clear=False):
            rows, scores, known = ask._q8_session_pool(
                np.asarray([1.0, 0.0]), Refs(), filters, 200)
        self.assertEqual(
            [row["session"] for row in rows],
            ["control-side", "synthetic-side"])
        np.testing.assert_allclose(scores, np.asarray([0.95, 0.90]))
        self.assertEqual(known, 2)
        self.assertTrue(ask._session_grouping_required(filters))

    def test_session_pool_escapes_more_than_one_native_page_of_one_session(
            self) -> None:
        dominant_rows = semantic_q8.MAX_CANDIDATES
        other_rows = 41

        class Refs:
            manifest = {"live_rows": dominant_rows + other_rows}

            @staticmethod
            def q8_eligibility(filters):
                excluded = set(filters.get("_exclude_sessions") or ())
                if "dominant" in excluded:
                    ordinals = np.arange(
                        dominant_rows, dominant_rows + other_rows,
                        dtype=np.int64)
                else:
                    ordinals = np.arange(
                        dominant_rows + other_rows, dtype=np.int64)
                return ordinals, len(ordinals), None

            @staticmethod
            def resolve(ordinals):
                rows = []
                for raw in ordinals:
                    ordinal = int(raw)
                    session = (
                        "dominant" if ordinal < dominant_rows
                        else f"other-{ordinal - dominant_rows:03d}")
                    rows.append({
                        "session": session, "ordinal": ordinal,
                        "who": "user", "text": session,
                    })
                return rows

        first = (
            np.arange(dominant_rows, dtype=np.int64),
            np.linspace(0.99, 0.80, dominant_rows, dtype=np.float32),
        )
        second = (
            np.arange(
                dominant_rows, dominant_rows + other_rows, dtype=np.int64),
            np.linspace(0.79, 0.60, other_rows, dtype=np.float32),
        )
        filters = {"_family_diverse": False}
        with mock.patch.object(
                semantic_q8, "exact_candidates",
                side_effect=(first, second)) as exact, \
                mock.patch.dict(
                    ask._CURRENT_MESSAGE_STATE,
                    {"generation": "0f" * 16}, clear=False):
            rows, scores, known = ask._q8_session_pool(
                np.asarray([1.0, 0.0]), Refs(), filters, 5)
        self.assertEqual(len(rows), 40)
        self.assertEqual(rows[0]["session"], "dominant")
        self.assertEqual(len({row["session"] for row in rows}), 40)
        self.assertEqual(len(scores), 40)
        self.assertEqual(known, 42)
        self.assertEqual(exact.call_count, 2)
        self.assertEqual(
            [call.kwargs["k"] for call in exact.call_args_list],
            [semantic_q8.MAX_CANDIDATES, other_rows])

    def test_session_pool_fails_closed_at_its_native_scan_budget(self) -> None:
        rows_per_page = semantic_q8.MAX_CANDIDATES

        class Refs:
            manifest = {
                "live_rows": rows_per_page
                * (ask.SEMANTIC_MAX_SESSION_PAGES + 1)}

            @staticmethod
            def resolve(ordinals):
                page = int(ordinals[0]) // rows_per_page
                return [{
                    "session": f"dominant-{page}", "ordinal": int(ordinal),
                    "who": "user", "text": "dominant",
                } for ordinal in ordinals]

        def candidates(_query, _generation, *, k, eligible):
            ordinals = np.asarray(eligible, dtype=np.int64)[:k]
            scores = np.linspace(0.99, 0.80, len(ordinals), dtype=np.float32)
            return ordinals, scores

        selection_page = [0]

        def selection(_refs, _filters):
            page = selection_page[0]
            selection_page[0] += 1
            start = page * rows_per_page
            ordinals = np.arange(
                start, start + rows_per_page, dtype=np.int64)
            return ordinals, len(ordinals), None

        filters = {"_family_diverse": False}
        with mock.patch.object(
                semantic_q8, "exact_candidates",
                side_effect=candidates) as exact, \
                mock.patch.object(
                    ask, "_q8_selection", side_effect=selection), \
                mock.patch.dict(
                    ask._CURRENT_MESSAGE_STATE,
                    {"generation": "0f" * 16}, clear=False):
            pooled = ask._q8_session_pool(
                np.asarray([1.0, 0.0]), Refs(), filters, 5)
        self.assertIsNone(pooled)
        self.assertEqual(exact.call_count, ask.SEMANTIC_MAX_SESSION_PAGES)

    def test_hybrid_q8_routes_flat_and_explicit_hidden_roles_by_session(self) -> None:
        self.assertTrue(ask._session_grouping_required(
            {"_family_diverse": False}))
        for who in common.SEMANTIC_DEFAULT_EXCLUDED_ROLES:
            self.assertTrue(ask._session_grouping_required(
                {"_family_diverse": True, "who": who}))
        hidden = surface.SpeakerFilter(
            tuple(sorted(common.SEMANTIC_DEFAULT_EXCLUDED_ROLES)), ())
        self.assertTrue(ask._session_grouping_required(
            {"_family_diverse": True, "who": hidden}))
        without_user = surface.SpeakerFilter(None, ("user",))
        self.assertTrue(ask._session_grouping_required(
            {"_family_diverse": True, "who": without_user}))
        ordinary = surface.SpeakerFilter(("user", "agent"), ())
        self.assertFalse(ask._session_grouping_required(
            {"_family_diverse": True, "who": ordinary}))

        class Refs:
            @staticmethod
            def take_integrity_disclosure():
                return None

        row = {
            "session": "side-a", "project": "p", "agent": "codex",
            "who": "control", "model": "m", "ts": 1, "turn": 2,
            "text": "answer",
        }
        timer = ask._SemanticTimer(False)
        filters = {"_family_diverse": False}
        with mock.patch.object(
                ask, "_require_current_message_index",
                return_value={"indexed": 300_001, "total": 300_001,
                              "complete": True}), \
                mock.patch.object(
                    semantic_q8, "artifact_available", return_value=True), \
                mock.patch.object(
                    semantic_q8, "accelerator_coverage",
                    return_value={"complete": True}), \
                mock.patch.object(
                    ask, "_message_refs_from_pointer", return_value=Refs()), \
                mock.patch.object(
                    ask, "_embed_query", return_value=np.asarray([1.0, 0.0])), \
                mock.patch.object(
                    ask, "_q8_session_pool",
                    return_value=([row], np.asarray([0.9]), 2)) as session_pool, \
                mock.patch.object(
                    ask, "_q8_grouped_pool",
                    side_effect=AssertionError("family grouping used")), \
                mock.patch.object(
                    ask.common, "read_index_meta",
                    side_effect=RuntimeError("no summaries")), \
                mock.patch("explore._session_concept", return_value={}):
            payload = json.loads(ask._tool_search_hybrid_q8(
                "query", 1, filters, timer, 2, "fixture"))
        session_pool.assert_called_once()
        self.assertEqual(payload["results"][0]["session"], "side-a")
        self.assertTrue(payload["truncated"])


if __name__ == "__main__":
    unittest.main()
