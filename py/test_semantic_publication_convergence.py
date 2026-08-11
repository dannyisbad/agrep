from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from _test_support import isolate_data_dir

isolate_data_dir()

import ask
import common
import embedder
import embedding_segments
import segment_query
import semantic
from test_embedding_segments import _inputs


def _coherence(state: str, *, searchable: bool = False) -> dict:
    return {
        "coherent": searchable,
        "searchable": searchable,
        "state": state,
        "coverage": {"complete": searchable},
    }


class SemanticPublicationConvergenceTests(unittest.TestCase):
    def test_unstable_manifest_keeps_the_old_fast_retry_without_publisher(
            self) -> None:
        unstable = _coherence("unstable-embeddings")
        current = _coherence("current", searchable=True)
        with (
            mock.patch.object(semantic.time, "sleep") as sleep,
            mock.patch.object(
                semantic, "embedding_coherence", return_value=current) as read,
            mock.patch.object(
                segment_query, "corpus_update_active") as active,
        ):
            result = semantic._await_publishing_coherence(unstable)

        self.assertIs(result, current)
        sleep.assert_called_once_with(
            semantic.SEMANTIC_UNSTABLE_SETTLE_DELAYS[0])
        read.assert_called_once_with()
        active.assert_not_called()

    def test_verified_publisher_wait_converges_within_bound(self) -> None:
        clock = [0.0]

        def sleep(delay: float) -> None:
            clock[0] += delay

        stale = _coherence("stale")
        current = _coherence("current", searchable=True)
        with (
            mock.patch.object(semantic.time, "monotonic", side_effect=lambda: clock[0]),
            mock.patch.object(semantic.time, "sleep", side_effect=sleep) as waited,
            mock.patch.object(
                segment_query, "corpus_update_active", return_value=True),
            mock.patch.object(
                semantic, "embedding_coherence", side_effect=(stale, current)) as read,
        ):
            result = semantic._await_publishing_coherence(stale)

        self.assertIs(result, current)
        self.assertEqual(read.call_count, 2)
        self.assertGreater(clock[0], 0.0)
        self.assertLessEqual(clock[0], semantic.SEMANTIC_PUBLICATION_WAIT_S)
        self.assertEqual(waited.call_count, 2)

    def test_post_release_grace_is_bounded(self) -> None:
        clock = [0.0]

        def sleep(delay: float) -> None:
            clock[0] += delay

        stale = _coherence("stale")
        with (
            mock.patch.object(semantic, "SEMANTIC_PUBLICATION_WAIT_S", 0.08),
            mock.patch.object(semantic, "SEMANTIC_PUBLICATION_GRACE_S", 0.02),
            mock.patch.object(semantic.time, "monotonic", side_effect=lambda: clock[0]),
            mock.patch.object(semantic.time, "sleep", side_effect=sleep),
            mock.patch.object(
                segment_query, "corpus_update_active",
                side_effect=(True, False, False, False, False)) as active,
            mock.patch.object(
                semantic, "embedding_coherence", return_value=stale) as read,
        ):
            result = semantic._await_publishing_coherence(stale)

        self.assertIs(result, stale)
        self.assertGreaterEqual(active.call_count, 3)
        self.assertLessEqual(active.call_count, 5)
        self.assertGreaterEqual(read.call_count, 2)
        self.assertGreaterEqual(clock[0], 0.02)
        self.assertLessEqual(clock[0], 0.08)

    def test_genuine_unavailability_does_not_wait(self) -> None:
        for state in ("missing-source", "missing-embeddings", "profile-mismatch"):
            unavailable = _coherence(state)
            with self.subTest(state=state):
                with (
                    mock.patch.object(
                        segment_query, "corpus_update_active") as active,
                    mock.patch.object(semantic.time, "sleep") as sleep,
                    mock.patch.object(semantic, "embedding_coherence") as read,
                ):
                    self.assertIs(
                        semantic._await_publishing_coherence(unavailable), unavailable)
                    active.assert_not_called()
                    sleep.assert_not_called()
                    read.assert_not_called()

        stale = _coherence("stale")
        with (
            mock.patch.object(
                segment_query, "corpus_update_active", return_value=False),
            mock.patch.object(semantic.time, "sleep") as sleep,
            mock.patch.object(semantic, "embedding_coherence") as read,
        ):
            self.assertIs(semantic._await_publishing_coherence(stale), stale)
        sleep.assert_not_called()
        read.assert_not_called()

    def test_real_manifest_writer_converges_reader_without_stale_service(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            meta = root / "embeddings.meta"
            base_source = {
                "version": 4,
                "family": "same",
                "files": {"messages.jsonl": {
                    "size": 10, "mtime_ns": 100, "dev": 1, "ino": 1,
                }},
            }

            def source(generation: int) -> dict:
                return {
                    **base_source,
                    "ingest_signature": f"generation-{generation}",
                }

            old_source = source(0)
            artifacts, hashes, refs = _inputs(root, "base", ["a", "b"])
            base = embedding_segments.publish_base(
                meta, source=old_source, model_id="model", dim=2,
                artifacts=artifacts, ids=["a", "b"], hashes=hashes,
                refs=refs, coverage={"total": 2})
            live_source = [old_source]
            publishing = threading.Event()
            done = threading.Event()
            errors: list[BaseException] = []
            observed: list[dict] = []

            patches = ExitStack()
            patches.enter_context(mock.patch.object(common, "DATA_DIR", root))
            patches.enter_context(mock.patch.object(
                common, "EMBEDDINGS_PATH", root / "embeddings.f32"))
            patches.enter_context(mock.patch.object(
                common, "IDS_PATH", root / "embeddings.ids"))
            patches.enter_context(mock.patch.object(
                semantic, "source_generation", side_effect=lambda: live_source[0]))
            patches.enter_context(mock.patch.object(
                semantic, "_active_embedding_profile", return_value=(2, "model")))
            patches.enter_context(mock.patch.object(
                segment_query, "corpus_update_active",
                side_effect=publishing.is_set))
            try:
                self.assertEqual(semantic.embedding_coherence()["state"], "current")

                def writer() -> None:
                    try:
                        current = base
                        for generation in range(1, 13):
                            target = source(generation)
                            publishing.set()
                            live_source[0] = target
                            # Keep a real stale window open long enough for the
                            # reader to enter the verified-publisher path.
                            time.sleep(0.006)
                            current = embedding_segments.publish_rebind(
                                meta, source=target, coverage={"total": 2},
                                expected_generation=current["generation"],
                                _before_replace=lambda target=target: self.assertEqual(
                                    live_source[0], target))
                            publishing.clear()
                            time.sleep(0.002)
                    except BaseException as exc:  # test thread must report failures
                        errors.append(exc)
                    finally:
                        publishing.clear()
                        done.set()

                thread = threading.Thread(target=writer, daemon=True)
                thread.start()
                self.assertTrue(publishing.wait(timeout=1.0))
                while not done.is_set() or publishing.is_set():
                    initial = semantic.embedding_coherence()
                    result = semantic._await_publishing_coherence(initial)
                    observed.append(result)
                thread.join(timeout=2.0)
                # Still inside the patched environment: after the writer
                # finishes, one bounded await must settle searchable-current.
                settled = semantic._await_publishing_coherence(
                    semantic.embedding_coherence())
            finally:
                patches.close()

            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertGreater(len(observed), 0)
            # A descheduled writer can exhaust the reader's bounded patience
            # (SEMANTIC_PUBLICATION_WAIT_S), so the contract is no SILENT
            # staleness: searchable-and-current, or a disclosed transient state.
            transient = {"stale", "unstable-source", "unstable-embeddings"}
            for row in observed:
                if row["searchable"]:
                    self.assertEqual(row["state"], "current")
                else:
                    self.assertIn(row["state"], transient)
            self.assertTrue(
                settled.get("searchable", settled.get("coherent", False)),
                settled)
            self.assertEqual(settled["state"], "current")
            self.assertEqual(
                embedding_segments.load_manifest(meta)["source"], source(12))

    def test_typed_artifact_movement_reopens_whole_query_once(self) -> None:
        current = _coherence("current", searchable=True)
        race = segment_query.SegmentArtifactMoved(
            "active semantic artifact was republished under this reader")
        payload = {"results": [], "candidate_sessions": 0, "truncated": False}
        with (
            mock.patch.object(semantic, "note_semantic_use"),
            mock.patch.object(
                semantic, "embedding_coherence", return_value=current) as coherence,
            mock.patch.object(embedder, "get"),
            mock.patch.object(
                ask, "tool_search_hybrid",
                side_effect=(race, json.dumps(payload))) as query,
            mock.patch.object(
                semantic, "_reset_transient_semantic_readers") as reset,
            mock.patch.object(semantic.time, "sleep") as sleep,
            mock.patch.object(
                semantic, "request_full_rebuild",
                side_effect=AssertionError("a race requested a rebuild")),
        ):
            result = semantic.search(
                "query", refresh_if_stale=False, timing=False)

        self.assertEqual(result["results"], [])
        self.assertEqual(query.call_count, 2)
        self.assertEqual(coherence.call_count, 2)
        reset.assert_called_once_with()
        sleep.assert_called_once_with(semantic.SEMANTIC_REOPEN_DELAY_S)

    def test_untyped_runtime_failure_is_not_retried(self) -> None:
        current = _coherence("current", searchable=True)
        failure = RuntimeError("model profile guard refused the query")
        with (
            mock.patch.object(semantic, "note_semantic_use"),
            mock.patch.object(
                semantic, "embedding_coherence", return_value=current),
            mock.patch.object(embedder, "get"),
            mock.patch.object(
                ask, "tool_search_hybrid", side_effect=failure) as query,
            mock.patch.object(
                semantic, "_reset_transient_semantic_readers") as reset,
            self.assertRaisesRegex(
                semantic.SemanticUnavailable, "model profile guard"),
        ):
            semantic.search("query", refresh_if_stale=False, timing=False)

        query.assert_called_once()
        reset.assert_not_called()


if __name__ == "__main__":
    unittest.main()
