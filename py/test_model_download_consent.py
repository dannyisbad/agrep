"""Consent contracts for semantic model network access."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from _test_support import isolate_data_dir


isolate_data_dir()
import ask  # noqa: E402
import common  # noqa: E402
import embedder  # noqa: E402
import search  # noqa: E402
import semantic  # noqa: E402
import semworker  # noqa: E402

_MLX_PATCH = None


def setUpModule() -> None:
    global _MLX_PATCH
    _MLX_PATCH = mock.patch.dict(os.environ, {"AGREP_MLX": "off"})
    _MLX_PATCH.start()


def tearDownModule() -> None:
    _MLX_PATCH.stop()


class ModelDownloadConsentTests(unittest.TestCase):
    @staticmethod
    def _stale_coherence() -> dict:
        return {
            "coherent": False,
            "searchable": False,
            "state": "missing-embeddings",
        }

    def test_automatic_refresh_never_spawns_for_an_uncached_model(self) -> None:
        with (
            mock.patch.object(
                semantic, "_background_refresh_disabled", return_value=None),
            mock.patch.object(
                semantic, "runtime_dependencies_available", return_value=True),
            mock.patch.object(
                semantic, "embedding_coherence",
                return_value=self._stale_coherence()),
            mock.patch.object(semantic, "embed_running", return_value=False),
            mock.patch.object(
                embedder, "ensure_model",
                side_effect=embedder.EmbedderUnavailable("missing model")),
            mock.patch.object(semantic.subprocess, "Popen") as spawn,
        ):
            result = semantic.ensure_fresh_async(max_new=128)

        self.assertEqual(result["state"], "model-not-cached")
        spawn.assert_not_called()

    def test_background_refresh_child_is_explicitly_offline(self) -> None:
        log = mock.Mock()
        with (
            mock.patch.object(
                semantic, "_background_refresh_disabled", return_value=None),
            mock.patch.object(
                semantic, "runtime_dependencies_available", return_value=True),
            mock.patch.object(
                semantic, "embedding_coherence",
                return_value=self._stale_coherence()),
            mock.patch.object(semantic, "embed_running", return_value=False),
            mock.patch.object(semantic, "read_embed_state", return_value={}),
            mock.patch.object(embedder, "ensure_model", return_value=Path("model")),
            mock.patch.object(common, "open_bounded_log", return_value=log),
            mock.patch.object(semantic.subprocess, "Popen") as spawn,
        ):
            result = semantic.ensure_fresh_async(max_new=128)

        self.assertEqual(result["state"], "running")
        command = spawn.call_args.args[0]
        self.assertIn("--no-model-download", command)

    def test_explicit_refresh_may_fetch_the_model(self) -> None:
        log = mock.Mock()
        with (
            mock.patch.object(
                semantic, "_background_refresh_disabled", return_value=None),
            mock.patch.object(
                semantic, "runtime_dependencies_available", return_value=True),
            mock.patch.object(
                semantic, "embedding_coherence",
                return_value=self._stale_coherence()),
            mock.patch.object(semantic, "embed_running", return_value=False),
            mock.patch.object(semantic, "read_embed_state", return_value={}),
            mock.patch.object(embedder, "ensure_model") as cached,
            mock.patch.object(common, "open_bounded_log", return_value=log),
            mock.patch.object(semantic.subprocess, "Popen") as spawn,
        ):
            result = semantic.ensure_fresh_async(
                max_new=128, allow_model_download=True)

        self.assertEqual(result["state"], "running")
        self.assertNotIn("--no-model-download", spawn.call_args.args[0])
        cached.assert_not_called()

    def test_query_permission_reaches_the_resident_worker(self) -> None:
        sent = []

        def worker(_query, *, level, k, filters):
            sent.append(dict(filters))
            return {
                "results": [],
                "semantic_coverage": {
                    "indexed": 1, "total": 1, "complete": True,
                },
                "partial": False,
                "score_kind": "cosine",
            }

        with (
            mock.patch.object(
                semworker, "resident_status", return_value={"running": True}),
            mock.patch.object(semworker, "search_worker", side_effect=worker),
        ):
            search._semantic_local("deployment retry loop", 3)
            search._semantic_local(
                "deployment retry loop", 3, allow_model_download=True)

        self.assertIs(sent[0].pop("_allow_model_download"), False)
        self.assertIs(sent[1].pop("_allow_model_download"), True)
        self.assertEqual(sent[0], sent[1])

    def test_semantic_engine_applies_the_request_permission(self) -> None:
        coherence = {
            "coherent": True,
            "searchable": True,
            "migration_pending": False,
            "state": "ready",
            "coverage": {
                "indexed": 1, "total": 1, "complete": True,
            },
        }
        payload = json.dumps({
            "results": [],
            "semantic_coverage": coherence["coverage"],
            "partial": False,
            "score_kind": "cosine",
        })
        with (
            mock.patch.object(
                semantic, "embedding_coherence", return_value=coherence),
            mock.patch.object(semantic, "note_semantic_use"),
            mock.patch.object(embedder, "get") as get_model,
            mock.patch.object(ask, "tool_search_hybrid", return_value=payload),
        ):
            semantic.search(
                "deployment retry loop", allow_model_download=False)
            semantic.search(
                "deployment retry loop", allow_model_download=True)

        self.assertEqual(
            get_model.call_args_list,
            [mock.call(download=False, lane=embedder.LANE_CPU),
             mock.call(download=True, lane=embedder.LANE_CPU)])

    def test_diagnostic_query_has_no_demand_refresh_or_download(self) -> None:
        coherence = {
            "coherent": False, "searchable": True,
            "migration_pending": False, "state": "partial",
            "coverage": {
                "indexed": 1, "total": 2, "complete": False,
            },
        }
        payload = json.dumps({
            "results": [], "semantic_coverage": coherence["coverage"],
            "partial": True, "score_kind": "cosine",
        })
        with (
            mock.patch.object(
                semantic, "embedding_coherence", return_value=coherence),
            mock.patch.object(semantic, "note_semantic_use") as demand,
            mock.patch.object(semantic, "ensure_fresh_async") as refresh,
            mock.patch.object(embedder, "get") as get_model,
            mock.patch.object(
                ask, "tool_search_messages", return_value=payload),
        ):
            semantic.search(
                "deployment retry loop", level="message",
                allow_model_download=True, diagnostic_only=True)
        demand.assert_not_called()
        refresh.assert_not_called()
        get_model.assert_called_once_with(download=False, lane=embedder.LANE_CPU)

    def test_diagnostic_query_does_not_repair_damaged_refs(self) -> None:
        coherence = {
            "coherent": True, "searchable": True,
            "migration_pending": False, "state": "current",
            "coverage": {
                "indexed": 2, "total": 2, "complete": True,
            },
        }
        with (
            mock.patch.object(
                semantic, "embedding_coherence", return_value=coherence),
            mock.patch.object(semantic, "note_semantic_use") as demand,
            mock.patch.object(embedder, "get"),
            mock.patch.object(
                ask, "tool_search_messages",
                side_effect=ask.CorruptMessageRefs("refs page damaged")),
            mock.patch.object(ask, "invalidate_message_refs") as invalidate,
            mock.patch.object(
                semantic, "request_full_rebuild") as rebuild,
        ):
            result = semantic.search(
                "deployment retry loop", level="message",
                diagnostic_only=True)
        self.assertTrue(result["semantic_unavailable"])
        self.assertEqual(
            result["semantic_integrity"]["repair_state"], "not-requested")
        demand.assert_not_called()
        invalidate.assert_not_called()
        rebuild.assert_not_called()

    def test_stale_query_names_generation_progress_and_next_action(self) -> None:
        coherence = {
            "coherent": False, "searchable": False, "state": "stale",
        }
        progress = {
            "state": "running", "phase": "embedding",
            "done": 411, "total": 33_830,
        }
        started = {"state": "running", "coherence": coherence}
        with (
            mock.patch.object(semantic, "note_semantic_use"),
            mock.patch.object(
                semantic, "embedding_coherence", return_value=coherence),
            mock.patch.object(
                semantic, "ensure_fresh_async", return_value=started),
            mock.patch.object(
                semantic, "read_embed_state", return_value=progress),
            mock.patch.object(semantic, "embed_running", return_value=True),
            self.assertRaises(semantic.SemanticUnavailable) as raised,
        ):
            semantic.search("deployment retry loop")
        detail = str(raised.exception)
        self.assertIn("embeddings stale for the current generation", detail)
        self.assertIn("refresh running (embedding · 411/33,830 rows)", detail)
        self.assertIn("retry shortly", detail)
        self.assertIn("doctor --deep", detail)

    def test_cold_notice_honors_the_model_directory_override(self) -> None:
        profile = {
            **embedder.PROFILE,
            "id": "fixture-model",
            "files": {"model.bin": (3, "0" * 64)},
        }
        with tempfile.TemporaryDirectory() as td:
            model_base = Path(td) / "custom-models"
            root = model_base / profile["id"]
            root.mkdir(parents=True)
            (root / "model.bin").write_bytes(b"abc")
            messages = []
            debug = []
            with (
                mock.patch.dict(
                    os.environ, {"AGREP_MODEL_DIR": str(model_base)}),
                mock.patch.object(embedder, "PROFILE", profile),
                mock.patch.object(
                    semworker, "resident_status",
                    return_value={"running": False, "protected": False}),
                mock.patch.object(
                    semworker, "search_worker",
                    return_value={
                        "results": [],
                        "semantic_coverage": {
                            "indexed": 1, "total": 1, "complete": True,
                        },
                        "partial": False,
                        "score_kind": "cosine",
                    }),
                mock.patch.object(
                    common, "log",
                    side_effect=lambda message, *_: messages.append(message)),
                mock.patch.object(
                    common, "dbg",
                    side_effect=lambda message, *_: debug.append(message)),
            ):
                self.assertEqual(embedder.model_dir(), root)
                self.assertTrue(embedder.model_cached())
                search._semantic_local(
                    "deployment retry loop", 3, allow_model_download=True)

        self.assertFalse(messages)
        self.assertTrue(any(
            "starting the semantic worker" in item for item in debug))
        self.assertFalse(any("fetching the semantic model" in item for item in messages))

    def test_no_daemon_never_claims_an_explicit_query_is_fetching(self) -> None:
        messages = []
        debug = []
        with (
            mock.patch.dict(os.environ, {"AGREP_NO_DAEMON": "1"}),
            mock.patch.object(embedder, "model_cached", return_value=False),
            mock.patch.object(common, "setting", return_value="auto"),
            mock.patch.object(
                semantic, "embedding_coherence",
                return_value={"searchable": False, "state": "missing-embeddings"}),
            mock.patch.object(
                semworker, "resident_status",
                return_value={"running": False, "protected": False}),
            mock.patch.object(
                semworker, "search_worker",
                side_effect=semworker.ResidentSemanticUnavailable(
                    "embeddings missing-embeddings; refresh disabled")),
            mock.patch.object(
                common, "log",
                side_effect=lambda message, *_: messages.append(message)),
            mock.patch.object(
                common, "dbg",
                side_effect=lambda message, *_: debug.append(message)),
        ):
            result = search._semantic_local(
                "deployment retry loop", 3, allow_model_download=True)

        self.assertTrue(result["fallback_recommended"])
        self.assertFalse(messages)
        self.assertTrue(any(
            "background refresh is disabled" in item for item in debug))
        self.assertFalse(any(
            "fetching the semantic model" in item for item in [*messages, *debug]))

    def test_no_daemon_keeps_fetch_notice_for_a_published_index(self) -> None:
        messages = []
        with (
            mock.patch.dict(os.environ, {"AGREP_NO_DAEMON": "1"}),
            mock.patch.object(embedder, "model_cached", return_value=False),
            mock.patch.object(common, "setting", return_value="auto"),
            mock.patch.object(
                semantic, "embedding_coherence",
                return_value={"searchable": True, "state": "partial"}),
            mock.patch.object(
                semworker, "resident_status",
                return_value={"running": False, "protected": False}),
            mock.patch.object(
                semworker, "search_worker",
                return_value={
                    "results": [],
                    "semantic_coverage": {
                        "indexed": 1, "total": 2, "complete": False,
                    },
                    "partial": True,
                    "score_kind": "cosine",
                }),
            mock.patch.object(
                common, "log",
                side_effect=lambda message, *_: messages.append(message)),
        ):
            search._semantic_local(
                "deployment retry loop", 3, allow_model_download=True)

        self.assertTrue(any(
            "fetching the semantic model" in item for item in messages))


if __name__ == "__main__":
    unittest.main()
