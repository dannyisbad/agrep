from __future__ import annotations

import contextlib
import errno
import json
import os
import pathlib
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
            # staleness: searchable-and-current, a searchable older generation
            # that discloses its lag, or a disclosed transient state.
            transient = {"stale", "unstable-source", "unstable-embeddings"}
            for row in observed:
                if row["searchable"] and row["state"] == "published":
                    self.assertFalse(row["source_current"], row)
                    self.assertIs(row["coverage"]["source_current"], False)
                elif row["searchable"]:
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

    def _denied_open_publication(self, rounds: int) -> tuple[list[dict], dict]:
        """Observe coherence while a real writer republishes and every open of
        embeddings.meta overlapping its replace is denied, as NTFS does."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            meta = root / "embeddings.meta"
            meta_resolved = meta.resolve()
            base_source = {
                "version": 4, "family": "same",
                "files": {"messages.jsonl": {
                    "size": 10, "mtime_ns": 100, "dev": 1, "ino": 1,
                }},
            }

            def source(generation: int) -> dict:
                return {**base_source, "ingest_signature": f"generation-{generation}"}

            artifacts, hashes, refs = _inputs(root, "base", ["a", "b"])
            base = embedding_segments.publish_base(
                meta, source=source(0), model_id="model", dim=2,
                artifacts=artifacts, ids=["a", "b"], hashes=hashes,
                refs=refs, coverage={"total": 2})
            live_source = [source(0)]
            publishing = threading.Event()
            replacing = threading.Event()
            done = threading.Event()
            errors: list[BaseException] = []
            observed: list[dict] = []
            real_open = pathlib.Path.open
            real_replace = os.replace

            def denied_open(path, *args, **kwargs):
                if replacing.is_set() and Path(path).resolve() == meta_resolved:
                    raise PermissionError(13, "Access is denied (delete pending)")
                return real_open(path, *args, **kwargs)

            def slow_replace(src, dst, *args, **kwargs):
                if Path(dst).resolve() != meta_resolved:
                    return real_replace(src, dst, *args, **kwargs)
                replacing.set()
                try:
                    time.sleep(0.004)
                    real_replace(src, dst, *args, **kwargs)
                    time.sleep(0.004)
                finally:
                    replacing.clear()
                return None

            def writer() -> None:
                try:
                    current = base
                    for generation in range(1, rounds + 1):
                        target = source(generation)
                        publishing.set()
                        live_source[0] = target
                        time.sleep(0.004)
                        current = embedding_segments.publish_rebind(
                            meta, source=target, coverage={"total": 2},
                            expected_generation=current["generation"])
                        publishing.clear()
                        time.sleep(0.002)
                except BaseException as exc:  # test thread must report failures
                    errors.append(exc)
                finally:
                    publishing.clear()
                    done.set()

            with ExitStack() as patches:
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
                    segment_query, "corpus_update_active", side_effect=publishing.is_set))
                patches.enter_context(mock.patch.object(semantic, "_WINDOWS", True))
                patches.enter_context(mock.patch.dict(semantic._DENIED_OPEN, {"since": None, "last": None}))
                patches.enter_context(mock.patch.object(pathlib.Path, "open", denied_open))
                patches.enter_context(mock.patch.object(os, "replace", slow_replace))
                self.assertEqual(semantic.embedding_coherence()["state"], "current")
                thread = threading.Thread(target=writer, daemon=True)
                thread.start()
                self.assertTrue(publishing.wait(timeout=1.0))
                while not done.is_set() or publishing.is_set():
                    observed.append(semantic._await_publishing_coherence(
                        semantic.embedding_coherence()))
                thread.join(timeout=5.0)
                settled = semantic._await_publishing_coherence(
                    semantic.embedding_coherence())
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            return observed, settled

    def test_denied_metadata_open_during_publication_is_transient(self) -> None:
        observed, settled = self._denied_open_publication(rounds=12)
        self.assertGreater(len(observed), 0)
        transient = {"stale", "unstable-source", "unstable-embeddings"}
        for row in observed:
            if row["searchable"] and row["state"] == "published":
                self.assertFalse(row["source_current"], row)
            elif row["searchable"]:
                self.assertEqual(row["state"], "current")
            else:
                self.assertIn(row["state"], transient, row)
        self.assertTrue(settled["searchable"], settled)
        self.assertEqual(settled["state"], "current")

    def test_denied_metadata_open_is_not_damage(self) -> None:
        """A reader whose open overlaps a replace must not request a rebuild."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            meta = root / "embeddings.meta"
            source = {"version": 4, "family": "same", "ingest_signature": "g",
                      "files": {"messages.jsonl": {
                          "size": 10, "mtime_ns": 100, "dev": 1, "ino": 1}}}
            artifacts, hashes, refs = _inputs(root, "base", ["a", "b"])
            embedding_segments.publish_base(
                meta, source=source, model_id="model", dim=2,
                artifacts=artifacts, ids=["a", "b"], hashes=hashes,
                refs=refs, coverage={"total": 2})
            meta_resolved = meta.resolve()
            real_open = pathlib.Path.open
            # Opens of the manifest succeed `granted` times, then fail `denied` times.
            window = {"granted": 0, "denied": 0}

            def denied_open(path, *args, **kwargs):
                if Path(path).resolve() == meta_resolved:
                    if window["granted"]:
                        window["granted"] -= 1
                    elif window["denied"]:
                        window["denied"] -= 1
                        raise PermissionError(13, "Access is denied (delete pending)")
                return real_open(path, *args, **kwargs)

            with (
                mock.patch.object(common, "DATA_DIR", root),
                mock.patch.object(common, "EMBEDDINGS_PATH", root / "embeddings.f32"),
                mock.patch.object(common, "IDS_PATH", root / "embeddings.ids"),
                mock.patch.object(semantic, "source_generation", return_value=source),
                mock.patch.object(
                    semantic, "_active_embedding_profile", return_value=(2, "model")),
                mock.patch.object(pathlib.Path, "open", denied_open),
                mock.patch.object(semantic, "_WINDOWS", True),
                mock.patch.dict(semantic._DENIED_OPEN, {"since": None, "last": None}),
            ):
                self.assertEqual(semantic.embedding_coherence()["state"], "current")
                # A one-read denial is absorbed by the identity read's own retry;
                # longer ones outlast it and must still read as movement.
                for granted, denied, expected in (
                        (0, 1, "unstable-embeddings"), (0, 3, "unstable-embeddings"),
                        (1, 1, "current"), (1, 4, "unstable-embeddings"),
                        (1, 12, "unstable-embeddings"), (2, 12, "unstable-embeddings")):
                    window.update(granted=granted, denied=denied)
                    coherence = semantic.embedding_coherence()
                    self.assertEqual(
                        coherence["state"], expected, (granted, denied, coherence))
                    self.assertEqual(
                        coherence["searchable"], expected == "current", coherence)
                # A denial the bounded wait absorbs serves the query; one that
                # outlasts it is a disclosed transient, never a rebuild request.
                for granted, denied, served in ((0, 1, True), (1, 12, False)):
                    window.update(granted=granted, denied=denied)
                    with (
                        mock.patch.object(semantic, "note_semantic_use"),
                        mock.patch.object(
                            segment_query, "corpus_update_active", return_value=False),
                        mock.patch.object(semantic.time, "sleep"),
                        mock.patch.object(
                            semantic, "request_full_rebuild",
                            side_effect=AssertionError(
                                "a denied open requested a rebuild")),
                        mock.patch.object(embedder, "get"),
                        mock.patch.object(
                            ask, "tool_search_hybrid",
                            return_value=json.dumps({
                                "results": [], "candidate_sessions": 0,
                                "truncated": False})),
                    ):
                        if served:
                            result = semantic.search(
                                "query", refresh_if_stale=False, timing=False)
                            self.assertEqual(result["results"], [])
                            self.assertFalse(
                                result.get("semantic_unavailable", False), result)
                        else:
                            with self.assertRaisesRegex(
                                    semantic.SemanticUnavailable, "unstable-embeddings"):
                                semantic.search(
                                    "query", refresh_if_stale=False, timing=False)

    def _published(self, root: Path) -> tuple[Path, dict]:
        meta = root / "embeddings.meta"
        source = {"version": 4, "family": "same", "ingest_signature": "g",
                  "files": {"messages.jsonl": {
                      "size": 10, "mtime_ns": 100, "dev": 1, "ino": 1}}}
        artifacts, hashes, refs = _inputs(root, "base", ["a", "b"])
        embedding_segments.publish_base(
            meta, source=source, model_id="model", dim=2,
            artifacts=artifacts, ids=["a", "b"], hashes=hashes,
            refs=refs, coverage={"total": 2})
        return meta, source

    def _coherence_patches(self, root: Path, source: dict) -> ExitStack:
        patches = ExitStack()
        patches.enter_context(mock.patch.object(common, "DATA_DIR", root))
        patches.enter_context(mock.patch.object(
            common, "EMBEDDINGS_PATH", root / "embeddings.f32"))
        patches.enter_context(mock.patch.object(common, "IDS_PATH", root / "embeddings.ids"))
        patches.enter_context(mock.patch.object(
            semantic, "source_generation", return_value=source))
        patches.enter_context(mock.patch.object(
            semantic, "_active_embedding_profile", return_value=(2, "model")))
        patches.enter_context(mock.patch.dict(semantic._DENIED_OPEN, {"since": None, "last": None}))
        return patches

    def _assert_rebuild_requested(self) -> None:
        with (
            mock.patch.object(semantic, "note_semantic_use"),
            mock.patch.object(segment_query, "corpus_update_active", return_value=False),
            mock.patch.object(semantic.time, "sleep"),
            mock.patch.object(
                semantic, "request_full_rebuild",
                return_value={"state": "running", "persistent": False}) as rebuild,
        ):
            result = semantic.search("query", refresh_if_stale=False, timing=False)
        rebuild.assert_called_once()
        self.assertTrue(result["semantic_unavailable"], result)
        self.assertEqual(result["semantic_integrity"]["state"], "generation-rejected")

    @unittest.skipIf(os.name == "nt", "POSIX permission and directory semantics")
    def test_permanent_posix_read_failures_are_damage_not_movement(self) -> None:
        """A rename never denies an open on POSIX: an unreadable, misplaced or
        failing metadata file is damage that requests a rebuild, not a transient."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            meta, source = self._published(root)
            real_open = pathlib.Path.open
            meta_resolved = meta.resolve()

            def failing_open(path, *args, **kwargs):
                if Path(path).resolve() == meta_resolved:
                    raise OSError(errno.EIO, "Input/output error")
                return real_open(path, *args, **kwargs)

            with self._coherence_patches(root, source):
                self.assertEqual(semantic.embedding_coherence()["state"], "current")
                shapes = [("eio", mock.patch.object(pathlib.Path, "open", failing_open))]
                if os.geteuid() != 0:
                    shapes.append(("chmod-000", _mode(meta, 0)))
                for label, shape in shapes:
                    with self.subTest(shape=label), shape:
                        coherence = semantic.embedding_coherence()
                        self.assertEqual(coherence["state"], "corrupt-embeddings", coherence)
                        self._assert_rebuild_requested()
                meta.unlink()
                meta.mkdir()
                with self.subTest(shape="eisdir"):
                    coherence = semantic.embedding_coherence()
                    self.assertEqual(coherence["state"], "corrupt-embeddings", coherence)
                    self._assert_rebuild_requested()

    def test_windows_denial_is_transient_only_within_patience(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            meta, source = self._published(root)
            real_open = pathlib.Path.open
            meta_resolved = meta.resolve()

            def denied_open(path, *args, **kwargs):
                if Path(path).resolve() == meta_resolved:
                    raise PermissionError(13, "Access is denied (delete pending)")
                return real_open(path, *args, **kwargs)

            clock = [100.0]
            with self._coherence_patches(root, source), \
                    mock.patch.object(semantic, "_WINDOWS", True), \
                    mock.patch.object(semantic.time, "monotonic", side_effect=lambda: clock[0]), \
                    mock.patch.object(pathlib.Path, "open", denied_open):
                # A streak re-observed within the window stays transient until the
                # window has elapsed since the streak began, then it is damage.
                step = semantic.SEMANTIC_DENIED_OPEN_PATIENCE_S / 4
                for _observation in range(4):
                    self.assertEqual(
                        semantic.embedding_coherence()["state"], "unstable-embeddings")
                    clock[0] += step
                coherence = semantic.embedding_coherence()
                self.assertEqual(coherence["state"], "corrupt-embeddings", coherence)
            with self._coherence_patches(root, source), \
                    mock.patch.object(semantic, "_WINDOWS", False), \
                    mock.patch.object(pathlib.Path, "open", denied_open):
                # Off Windows the same denial is damage at once.
                self.assertEqual(
                    semantic.embedding_coherence()["state"], "corrupt-embeddings")

    def test_windows_denial_streak_ends_with_the_quiet_gap(self) -> None:
        """A long denial that was reported as damage must not poison the next,
        unrelated, short denial seconds later in a long-lived process."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            meta, source = self._published(root)
            real_open = pathlib.Path.open
            meta_resolved = meta.resolve()
            window = {"denied": 0}

            def denied_open(path, *args, **kwargs):
                if window["denied"] and Path(path).resolve() == meta_resolved:
                    window["denied"] -= 1
                    raise PermissionError(13, "Access is denied (delete pending)")
                return real_open(path, *args, **kwargs)

            clock = [100.0]
            patience = semantic.SEMANTIC_DENIED_OPEN_PATIENCE_S
            with self._coherence_patches(root, source), \
                    mock.patch.object(semantic, "_WINDOWS", True), \
                    mock.patch.object(semantic.time, "monotonic", side_effect=lambda: clock[0]), \
                    mock.patch.object(semantic.time, "sleep", side_effect=lambda s: clock.__setitem__(0, clock[0] + s)), \
                    mock.patch.object(pathlib.Path, "open", denied_open), \
                    mock.patch.object(semantic, "note_semantic_use"), \
                    mock.patch.object(segment_query, "corpus_update_active", return_value=False), \
                    mock.patch.object(
                        semantic, "request_full_rebuild",
                        side_effect=AssertionError("a short denial requested a rebuild")), \
                    mock.patch.object(embedder, "get"), \
                    mock.patch.object(
                        ask, "tool_search_hybrid",
                        return_value=json.dumps({
                            "results": [], "candidate_sessions": 0, "truncated": False})):
                # A denial outlasting requests' settle waits: unstable throughout,
                # since the streak stays younger than the patience window.
                window["denied"] = 12
                while window["denied"]:
                    with self.assertRaisesRegex(
                            semantic.SemanticUnavailable, "unstable-embeddings"):
                        semantic.search("query", refresh_if_stale=False, timing=False)
                # Quiet gap longer than the patience window, then an unrelated
                # one-read denial: a new streak, absorbed and served.
                clock[0] += patience + 0.2
                window["denied"] = 1
                result = semantic.search("query", refresh_if_stale=False, timing=False)
                self.assertEqual(result["results"], [])
                self.assertFalse(result.get("semantic_unavailable", False), result)


@contextlib.contextmanager
def _mode(path: Path, mode: int):
    saved = path.stat().st_mode & 0o777
    path.chmod(mode)
    try:
        yield
    finally:
        path.chmod(saved)


if __name__ == "__main__":
    unittest.main()
