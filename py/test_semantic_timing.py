from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import numpy as np

from _test_support import isolate_data_dir

isolate_data_dir()


# Daemon semantics run real here, daemon processes never do (shared seam).
from _test_support import lift_daemon_semantics
import indexd_runtime

setUpModule, tearDownModule = lift_daemon_semantics(indexd_runtime)
import ask
import common
import console
import embedder
import semantic
import semworker
import search


class _Refs:
    def best_by_session(self, _scores, _filters):
        return {"session-a": 0}

    def resolve(self, _ordinals):
        return [{
            "session": "session-a", "project": "p", "agent": "codex",
            "who": "user", "model": "m", "ts": 1, "turn": 2, "text": "answer",
        }]


class SemanticTimingTests(unittest.TestCase):
    def test_committed_generation_validation_never_reads_large_sidecars(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            matrix_path = root / "embeddings.f32"
            ids_path = root / "embeddings.ids"
            meta_path = root / "embeddings.meta"
            ids = [f"codex:session:{row}" for row in range(32)]
            matrix = np.eye(32, 32, dtype=np.float32)
            common.write_embeddings(
                ids, matrix, embeddings_path=matrix_path, ids_path=ids_path,
                dim=32, model_id="fixture", text_hashes=[f"{row:016x}" for row in range(32)])
            hashes_path = matrix_path.with_suffix(".hashes")
            original_read = Path.read_bytes

            def bounded_read(path):
                if path in (ids_path, hashes_path, matrix_path):
                    raise AssertionError(f"unbounded artifact read: {path.name}")
                return original_read(path)

            with mock.patch.object(Path, "read_bytes", bounded_read):
                state = common.committed_embedding_artifact_state(
                    meta_path, matrix_path, ids_path)
            self.assertEqual(state["commit"]["rows"], len(ids))
            ids_path.write_text("replacement\n", encoding="utf-8")
            with mock.patch.object(Path, "read_bytes", bounded_read), \
                    self.assertRaises(ValueError):
                common.committed_embedding_artifact_state(
                    meta_path, matrix_path, ids_path, attempts=1)

    def _hybrid(self, enabled: bool) -> dict:
        matrix = np.asarray([[1.0, 0.0]], dtype=np.float32)
        with mock.patch.object(ask, "_semantic_timing_enabled", return_value=enabled), \
                mock.patch.object(ask.common, "read_index_meta",
                                  return_value=(2, "fixture")), \
                mock.patch.object(ask, "_guard_embedder"), \
                mock.patch.object(ask, "_message_artifacts",
                                  return_value=(["m0"], matrix, _Refs(),
                                                {"complete": True})), \
                mock.patch.object(ask, "_embed_query",
                                  return_value=np.asarray([1.0, 0.0], dtype=np.float32)), \
                mock.patch.object(ask, "_summary_artifacts",
                                  side_effect=RuntimeError("optional summaries absent")), \
                mock.patch.object(ask, "_family_diversity_enabled",
                                  return_value=False), \
                mock.patch("explore._session_concept", return_value={}):
            return json.loads(ask.tool_search_hybrid("query", 1))

    def test_hybrid_timing_is_opt_in_and_phase_complete(self):
        plain = self._hybrid(False)
        timed = self._hybrid(True)
        self.assertNotIn("_semantic_timing", plain)
        timing = timed["_semantic_timing"]
        self.assertEqual(set(timing["phases_ms"]), {
            "metadata", "artifacts", "embed", "matmul", "best_by_session",
            "summary_artifacts", "family", "resolve", "rank", "enrichment",
        })
        self.assertTrue(all(value >= 0 for value in timing["phases_ms"].values()))
        self.assertGreaterEqual(timing["hybrid_compute_ms"], 0)

    def test_semantic_search_adds_coherence_and_dispatch_totals(self):
        payload = {
            "results": [], "candidate_sessions": 0, "truncated": False,
            "_semantic_timing": {"phases_ms": {"embed": 1.0}},
        }
        coherence = {
            "coherent": True, "searchable": True, "state": "current",
            "coverage": {"complete": True},
        }
        with mock.patch.object(semantic, "_semantic_timing_enabled", return_value=False), \
                mock.patch.object(semantic, "note_semantic_use"), \
                mock.patch.object(semantic, "embedding_coherence", return_value=coherence), \
                mock.patch.object(embedder, "get"), \
                mock.patch.object(ask, "tool_search_hybrid",
                                  return_value=json.dumps(payload)) as hybrid:
            result = semantic.search("query", level="hybrid", k=1,
                                     refresh_if_stale=False, timing=True)
        timing = result["_semantic_timing"]
        self.assertGreaterEqual(timing["phases_ms"]["coherence"], 0)
        self.assertGreaterEqual(timing["semantic_dispatch_ms"], 0)
        self.assertEqual(timing["coherence_state"], "current")
        self.assertIs(hybrid.call_args.kwargs["timing"], True)

    def test_request_false_overrides_a_debug_worker_environment(self):
        payload = {"results": [], "candidate_sessions": 0, "truncated": False}
        coherence = {
            "coherent": True, "searchable": True, "state": "current",
            "coverage": {"complete": True},
        }
        with mock.patch.object(semantic, "_semantic_timing_enabled", return_value=True), \
                mock.patch.object(semantic, "note_semantic_use"), \
                mock.patch.object(semantic, "embedding_coherence", return_value=coherence), \
                mock.patch.object(embedder, "get"), \
                mock.patch.object(ask, "tool_search_hybrid",
                                  return_value=json.dumps(payload)) as hybrid:
            result = semantic.search("query", level="hybrid", k=1,
                                     refresh_if_stale=False, timing=False)
        self.assertNotIn("_semantic_timing", result)
        self.assertIs(hybrid.call_args.kwargs["timing"], False)

    @staticmethod
    def _semantic_payload() -> dict:
        return {
            "results": [], "candidate_sessions": 0, "truncated": False,
            "score_kind": "cosine", "semantic_coverage": {"complete": True},
            "partial": False, "_semantic_timing": {"client_roundtrip_ms": 1.0},
        }

    def test_standalone_flag_emits_and_default_stays_silent(self):
        stderr = io.StringIO()
        with mock.patch.object(console, "DEBUG", False), \
                mock.patch.object(semworker, "resident_status",
                                  return_value={"running": True}), \
                mock.patch.object(semworker, "search_worker",
                                  return_value=self._semantic_payload()), \
                mock.patch.dict(os.environ, {"AGREP_SEM_TIMING": "1"}), \
                contextlib.redirect_stderr(stderr):
            search._semantic_local("meaningful past solution", 3)
        self.assertIn("semantic timing {", stderr.getvalue())

        stderr = io.StringIO()
        with mock.patch.object(console, "DEBUG", False), \
                mock.patch.object(semworker, "resident_status",
                                  return_value={"running": True}), \
                mock.patch.object(semworker, "search_worker",
                                  return_value=self._semantic_payload()), \
                mock.patch.dict(os.environ, {}, clear=False), \
                contextlib.redirect_stderr(stderr):
            os.environ.pop("AGREP_SEM_TIMING", None)
            search._semantic_local("meaningful past solution", 3)
        self.assertNotIn("semantic timing ", stderr.getvalue())

    def test_resident_request_controls_timing_independently_of_worker_env(self):
        def fake_search(_query, *, level, k, filters):
            return {"results": [], "candidate_sessions": 0, "truncated": False}

        try:
            with mock.patch.object(
                    common, "bind_descendants_to_process_lifetime",
                    return_value=True):
                lifetime = semworker.acquire_resident_owner()
                self.assertIsNotNone(lifetime)
                server = semworker.SemanticWorkerServer(
                    search_fn=fake_search, lifetime=lifetime)
        except (PermissionError,
                semworker.ResidentSemanticPreflightUnavailable) as exc:
            lifetime.release(tombstone=True, require_stable_mtime=True)
            self.skipTest(f"loopback bind blocked: {exc}")
        thread = threading.Thread(target=server.serve, daemon=True)
        thread.start()
        rec = json.loads(server.record)
        try:
            timed = semworker._worker_request(rec, {
                "query": "timed", "level": "hybrid", "k": 1,
                "filters": {}, "timing": True,
            })
            plain = semworker._worker_request(rec, {
                "query": "plain", "level": "hybrid", "k": 1,
                "filters": {}, "timing": False,
            })
            indexd_runtime.SEARCH_BEAT_PATH.unlink(missing_ok=True)
            readiness = semworker.diagnostic_query_status(
                ready=True, timeout_s=0.5)
        finally:
            server.request_stop()
            thread.join(timeout=2)
            lifetime.release(tombstone=True, require_stable_mtime=True)
        self.assertIn("worker_search_ms", timed["_semantic_timing"])
        self.assertIn("client_roundtrip_ms", timed["_semantic_timing"])
        self.assertNotIn("_semantic_timing", plain)
        self.assertTrue(readiness["discovered"])
        self.assertTrue(readiness["alive"])
        self.assertTrue(readiness["query_serving"])
        self.assertFalse(indexd_runtime.SEARCH_BEAT_PATH.exists())

    def test_timing_request_validation_and_bench_parser(self):
        valid = semworker._validate_request({
            "query": "q", "level": "hybrid", "k": 1,
            "filters": {"_diagnostic_only": True}, "timing": True,
        })
        self.assertIs(valid[4], True)
        self.assertIs(valid[3]["_diagnostic_only"], True)
        with self.assertRaises(ValueError):
            semworker._validate_request({
                "query": "q", "level": "hybrid", "k": 1,
                "filters": {}, "timing": "yes",
            })
        with self.assertRaises(ValueError):
            semworker._validate_request({
                "query": "q", "level": "hybrid", "k": 1,
                "filters": {"_diagnostic_only": "yes"}, "timing": False,
            })

        sent = []
        with mock.patch.object(semworker, "_ensure_worker", return_value={"pid": 1}), \
                mock.patch.object(semworker, "_worker_request",
                                  side_effect=lambda _rec, body: sent.append(body) or {}):
            semworker.search_worker("q", level="hybrid", k=1, timing=True)
        self.assertIs(sent[0]["timing"], True)

        path = Path(__file__).resolve().parents[1] / "bench" / "perf.py"
        spec = importlib.util.spec_from_file_location("agrep_perf_test", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        timing = {
            "phases_ms": {"embed": 2.0},
            "cache_before": {}, "cache_after": {},
            "hybrid_compute_ms": 3.0, "semantic_dispatch_ms": 4.0,
            "worker_search_ms": 5.0, "client_roundtrip_ms": 6.0,
            "client_overhead_ms": 1.0, "coherence_state": "current",
        }
        line = f"* [agrep +     1.0ms] semantic timing {json.dumps(timing)}"
        self.assertEqual(module._extract_semantic_timing(line), timing)
        self.assertIsNone(module._extract_semantic_timing(
            f"semantic timing {json.dumps(timing)}"))
        poisoned = {**timing, "client_overhead_ms": float("nan")}
        poison_line = f"* [agrep + 1.0ms] semantic timing {json.dumps(poisoned)}"
        self.assertIsNone(module._extract_semantic_timing(poison_line))

        completed = module.subprocess.CompletedProcess(
            ["agrep"], 0, "semantic timing {\"spoof\":0}",
            "* [agrep + 1.0ms] search done: 1 hit via semantic:hybrid")
        with mock.patch.object(module.subprocess, "run", return_value=completed):
            median, _last, detail = module._verified_semantic_median(
                ["agrep"], env={}, cwd=Path("."), n=1)
        self.assertIsNone(median)
        self.assertEqual(detail["status"], "unavailable")
        self.assertIn("timing proof", detail["reason"])


if __name__ == "__main__":
    unittest.main()
