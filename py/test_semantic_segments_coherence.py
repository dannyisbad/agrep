from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import numpy as np

from _test_support import isolate_data_dir

isolate_data_dir()

import common
import embedder
import embedding_segments
import semantic
from test_embedding_segments import _inputs


def _source(name: str) -> dict:
    return {"version": 1, "ingest_signature": name,
            "files": {"messages": {"size": 10, "mtime_ns": 100}}}


class SemanticSegmentCoherenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.meta = self.root / "embeddings.meta"
        self.f32 = self.root / "embeddings.f32"
        self.ids = self.root / "embeddings.ids"
        self.patches = ExitStack()
        self.patches.enter_context(mock.patch.object(common, "DATA_DIR", self.root))
        self.patches.enter_context(mock.patch.object(common, "EMBEDDINGS_PATH", self.f32))
        self.patches.enter_context(mock.patch.object(common, "IDS_PATH", self.ids))
        self.patches.enter_context(
            mock.patch.object(semantic, "_active_embedding_profile", return_value=(2, "model")))

    def tearDown(self) -> None:
        self.patches.close()
        self.temporary.cleanup()

    def _publish(self, source: dict, total: int = 2):
        artifacts, hashes, refs = _inputs(self.root, "base", ["a", "b"])
        return embedding_segments.publish_base(
            self.meta, source=source, model_id="model", dim=2, artifacts=artifacts,
            ids=["a", "b"], hashes=hashes, refs=refs, coverage={"total": total})

    def test_v2_current_and_partial_use_manifest_without_marker(self) -> None:
        source = _source("current")
        published = self._publish(source)
        with mock.patch.object(semantic, "source_generation", return_value=source):
            current = semantic.embedding_coherence()
        self.assertTrue(current["coherent"])
        self.assertEqual(current["state"], "current")
        self.assertEqual(current["layout"], "segments-v2")
        self.assertFalse(current["migration_pending"])
        self.assertFalse(semantic.generation_marker_path().exists())
        output = semantic.output_generation()
        self.assertEqual((output["generation"], output["live_rows"]),
                         (published["generation"], 2))
        self.assertGreater(len(output["artifacts"]), 3)

        self.meta.unlink()
        for path in (self.root / embedding_segments.SEGMENT_DIR).iterdir():
            path.unlink()
        self._publish(source, total=3)
        with mock.patch.object(semantic, "source_generation", return_value=source):
            partial = semantic.embedding_coherence()
        self.assertFalse(partial["coherent"])
        self.assertTrue(partial["searchable"])
        self.assertEqual((partial["state"], partial["coverage"]["pending"]), ("partial", 1))

    def test_v2_stale_corrupt_and_movement_fail_closed(self) -> None:
        source = _source("published")
        self._publish(source)
        with mock.patch.object(semantic, "source_generation", return_value=_source("newer")):
            self.assertEqual(semantic.embedding_coherence()["state"], "stale")

        first = semantic.output_generation()
        moved = {**first, "generation": "f" * 32}
        with mock.patch.object(semantic, "source_generation", return_value=source), \
                mock.patch.object(semantic, "output_generation", side_effect=[first, moved]):
            self.assertEqual(semantic.embedding_coherence()["state"], "unstable-embeddings")

        manifest = embedding_segments.load_manifest(self.meta)
        embedding_segments.artifact_path(
            manifest, manifest["segments"][0]["artifacts"]["q8"]).unlink()
        with mock.patch.object(semantic, "source_generation", return_value=source):
            self.assertEqual(semantic.embedding_coherence()["state"], "corrupt-embeddings")

    def test_v2_marker_is_validation_only_and_v1_is_unchanged(self) -> None:
        source = _source("v2")
        self._publish(source, total=3)
        output = semantic.output_generation()
        semantic.write_generation_marker(
            source, indexed_rows=2, total_rows=3, expected_output=output)
        self.assertFalse(semantic.generation_marker_path().exists())
        with self.assertRaises(ValueError):
            semantic.write_generation_marker(source, indexed_rows=2, total_rows=4)

        self.meta.unlink()
        for path in (self.root / embedding_segments.SEGMENT_DIR).iterdir():
            path.unlink()
        common.write_embeddings(
            ["a", "b"], np.eye(2, dtype=np.float32), self.f32, self.ids,
            dim=2, model_id="model", text_hashes=["a" * 16, "b" * 16])
        self.assertEqual(common.read_index_meta(self.meta), (2, "model"))
        legacy_output = semantic.output_generation()
        self.assertFalse(legacy_output.get("segmented", False))
        semantic.write_generation_marker(source)
        marker = json.loads(semantic.generation_marker_path().read_text(encoding="utf-8"))
        self.assertEqual(marker["source"], source)
        with mock.patch.object(semantic, "source_generation", return_value=source):
            flat = semantic.embedding_coherence()
        self.assertTrue(flat["coherent"])
        self.assertTrue(flat["searchable"])
        self.assertEqual(flat["layout"], "flat-v1")
        self.assertTrue(flat["migration_pending"])

    def test_current_flat_layout_schedules_background_migration(self) -> None:
        coherence = {
            "coherent": True, "searchable": True, "state": "current",
            "layout": "flat-v1", "migration_pending": True,
        }
        log = mock.Mock()
        with (mock.patch.object(
                  semantic, "_background_refresh_disabled", return_value=None),
              mock.patch.object(
                  semantic, "runtime_dependencies_available", return_value=True),
              mock.patch.object(
                  semantic, "embedding_coherence", return_value=coherence),
              mock.patch.object(semantic, "embed_running", return_value=False),
              mock.patch.object(semantic, "read_embed_state", return_value={}),
              mock.patch.object(embedder, "ensure_model"),
              mock.patch.object(
                  semantic, "_needs_unverified_bundle_rebuild", return_value=False),
              mock.patch.object(common, "open_bounded_log", return_value=log),
              mock.patch.object(semantic.subprocess, "Popen") as popen):
            result = semantic.ensure_fresh_async(max_new=128)
        self.assertEqual(result["state"], "running")
        self.assertEqual(result["coherence"], coherence)
        self.assertIn("--background", popen.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
