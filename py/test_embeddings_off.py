"""Network and scheduler contracts for the embeddings privacy switch."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from _test_support import isolate_data_dir  # noqa: E402

isolate_data_dir()

import common  # noqa: E402
import embedder  # noqa: E402
import ownerfile  # noqa: E402
import removal_fence  # noqa: E402
import search  # noqa: E402
import semantic  # noqa: E402
import semworker  # noqa: E402


class EmbeddingsOffTests(unittest.TestCase):
    @staticmethod
    def _profile(payload: bytes) -> dict:
        return {
            "id": "off-contract",
            "repo": "fixture/model",
            "revision": "a" * 40,
            "files": {
                "model.bin": (
                    len(payload), hashlib.sha256(payload).hexdigest()),
            },
            "remote_dir": {},
        }

    @staticmethod
    def _write_setting(path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"embeddings": value}), encoding="utf-8")

    def test_missing_model_off_creates_no_claim_partial_or_network_call(self) -> None:
        payload = b"cached-model"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            model = root / "model"
            settings = root / "settings.json"
            self._write_setting(settings, "off")
            with (
                mock.patch("settings.SETTINGS_PATH", settings),
                mock.patch.object(embedder, "PROFILE", self._profile(payload)),
                mock.patch.object(embedder, "model_dir", return_value=model),
                mock.patch.object(
                    ownerfile, "create_exclusive") as create_claim,
                mock.patch.object(
                    embedder.urllib.request, "urlopen") as network,
            ):
                with self.assertRaisesRegex(
                        embedder.EmbedderUnavailable,
                        "model download disabled"):
                    embedder.ensure_model()
            create_claim.assert_not_called()
            network.assert_not_called()
            self.assertFalse(model.exists())
            self.assertEqual(list(root.rglob("*.part")), [])

    def test_fetch_authority_rechecks_setting_before_opening_url(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            settings = root / "settings.json"
            part = root / "model.part"
            self._write_setting(settings, "off")
            with (
                mock.patch("settings.SETTINGS_PATH", settings),
                mock.patch.object(
                    embedder.urllib.request, "urlopen") as network,
                self.assertRaisesRegex(
                    embedder.EmbedderUnavailable,
                    "model download disabled",
                ),
            ):
                embedder._fetch_pinned(
                    "https://example.invalid/model", part, 1)
            network.assert_not_called()
            self.assertFalse(part.exists())

    def test_verified_cached_model_remains_available_while_off(self) -> None:
        payload = b"cached-model"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            model = root / "model"
            settings = root / "settings.json"
            self._write_setting(settings, "off")
            model.mkdir()
            (model / "model.bin").write_bytes(payload)
            with (
                mock.patch("settings.SETTINGS_PATH", settings),
                mock.patch.object(embedder, "PROFILE", self._profile(payload)),
                mock.patch.object(embedder, "model_dir", return_value=model),
                mock.patch.object(
                    ownerfile, "create_exclusive") as create_claim,
                mock.patch.object(
                    embedder.urllib.request, "urlopen") as network,
            ):
                self.assertEqual(embedder.ensure_model(), model)
            create_claim.assert_not_called()
            network.assert_not_called()

    def test_background_and_sync_refresh_defer_without_spawning(self) -> None:
        with (
            mock.patch.object(
                semantic.common, "setting", return_value="off"),
            mock.patch.object(
                removal_fence, "background_removal_active",
                return_value=False),
            mock.patch.object(
                semantic, "runtime_dependencies_available",
                return_value=True),
            mock.patch.object(semantic, "embedding_coherence") as coherence,
            mock.patch.object(semantic.subprocess, "Popen") as spawn,
        ):
            fresh = semantic.ensure_fresh_async(max_new=128)
            refs = semantic.ensure_refs_async()
            sync = semantic.refresh_embeddings_sync(max_new=128)
        self.assertEqual(fresh["state"], "disabled")
        self.assertEqual(refs["state"], "disabled")
        self.assertEqual(sync["state"], "disabled")
        self.assertFalse(sync["ok"])
        self.assertTrue(all(
            "embeddings=off" in result["reason"]
            for result in (fresh, refs, sync)))
        coherence.assert_not_called()
        spawn.assert_not_called()

    def test_explicit_search_does_not_claim_an_off_model_is_being_fetched(
            self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            with (
                mock.patch.object(search.common, "DATA_DIR", Path(td)),
                mock.patch.object(
                    search.common, "setting", return_value="off"),
                mock.patch.object(
                    embedder, "model_cached", return_value=False),
                mock.patch.object(
                    semworker, "resident_status",
                    return_value={"running": False, "protected": False}),
                mock.patch.object(
                    semworker, "search_worker",
                    side_effect=semworker.ResidentSemanticUnavailable(
                        "model unavailable")),
                contextlib.redirect_stderr(output),
            ):
                result = search._semantic_local(
                    "remember the failed migration", 3)
        self.assertTrue(result["fallback_recommended"])
        self.assertEqual(
            result["semantic_status"]["reason"], "model unavailable")
        rendered = output.getvalue()
        self.assertIn(
            "embeddings=off prevents its download", rendered)
        self.assertNotIn("fetching the semantic model", rendered)


if __name__ == "__main__":
    unittest.main()
