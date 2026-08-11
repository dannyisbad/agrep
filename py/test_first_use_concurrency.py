from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "py"))

from _test_support import isolate_data_dir  # noqa: E402

isolate_data_dir()

import common  # noqa: E402
import indexd_runtime  # noqa: E402
import search  # noqa: E402
import surface_policy as surface  # noqa: E402


class _InterruptedStdout:
    def __init__(self) -> None:
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        raise KeyboardInterrupt

    def close(self) -> None:
        self.closed = True


class _CompletedStream:
    def __init__(self, rows: list[dict]) -> None:
        self.stdout = io.BytesIO(b"".join(
            (json.dumps({"row": row}) + "\n").encode("utf-8") for row in rows))

    @staticmethod
    def wait() -> int:
        return 0


class _RawDrainStream:
    def __init__(self, first: bytes, tail: bytes) -> None:
        self.first = first
        self.tail = io.BytesIO(tail)
        self.iterations = 0
        self.readinto_calls = 0

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        self.iterations += 1
        if self.iterations > 1:
            raise AssertionError("page-full stream resumed line decoding")
        return self.first

    def readinto(self, target: bytearray) -> int:
        self.readinto_calls += 1
        return self.tail.readinto(target)

    def close(self) -> None:
        self.tail.close()


class FirstUsePublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.messages = self.root / "messages.jsonl"
        self.current = self.root / ".indexd.v2.lock"
        self.legacy = self.root / ".indexd.lock"
        self.build_id = "a" * 20
        body = (
            "state=derived-adoption pid=123 start=unknown "
            f"writer={self.build_id} token={'b' * 32}\n")
        self.current.write_text(body, encoding="utf-8")
        self.legacy.write_text(body, encoding="utf-8")
        self.patches = (
            mock.patch.object(common, "DATA_DIR", self.root),
            mock.patch.object(common, "MESSAGES_PATH", self.messages),
            mock.patch.object(indexd_runtime, "INDEXD_LOCK_PATH", self.current),
            mock.patch.object(indexd_runtime, "LEGACY_INDEXD_LOCK_PATH", self.legacy),
        )
        for patch in self.patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in reversed(self.patches):
            patch.stop()
        self.temp.cleanup()

    def test_waiter_observes_the_winning_publication(self) -> None:
        committed = threading.Event()

        def publish() -> None:
            time.sleep(0.05)
            self.messages.write_text(json.dumps({"id": "winner"}) + "\n")
            time.sleep(0.08)
            committed.set()
            self.current.unlink()
            self.legacy.unlink()

        worker = threading.Thread(target=publish)
        worker.start()
        try:
            with mock.patch.object(
                    indexd_runtime, "_first_publication_committed",
                    side_effect=lambda _build: committed.is_set()):
                started = time.monotonic()
                self.assertTrue(indexd_runtime._await_first_publication(
                    self.build_id, timeout_s=1.0))
                self.assertGreaterEqual(time.monotonic() - started, 0.1)
        finally:
            worker.join()

    def test_disappearing_claim_without_publication_fails(self) -> None:
        self.current.unlink()
        self.legacy.unlink()
        self.assertFalse(indexd_runtime._await_first_publication(
            self.build_id, timeout_s=1.0))

    def test_foreign_or_malformed_claim_is_never_waited_on(self) -> None:
        self.current.write_text("hostile\n", encoding="utf-8")
        self.assertFalse(indexd_runtime._same_build_adoption_claim(self.build_id))

    def test_commit_requires_source_owner_and_derived_proof(self) -> None:
        self.current.unlink()
        self.legacy.unlink()
        source = self.root / ".source_snapshot.bin"
        source.write_bytes(b"committed-source")
        current = indexd_runtime.DerivedMutationInfo(
            "current", self.build_id, "")
        with mock.patch.object(
                indexd_runtime, "derived_mutation_info", return_value=current), \
                mock.patch("corpusdb._derived_publication_health",
                           return_value={"state": "ready"}):
            self.assertTrue(indexd_runtime._first_publication_committed(
                self.build_id))
        source.unlink()
        with mock.patch.object(
                indexd_runtime, "derived_mutation_info", return_value=current), \
                mock.patch("corpusdb._derived_publication_health",
                           return_value={"state": "ready"}):
            self.assertFalse(indexd_runtime._first_publication_committed(
                self.build_id))

    def test_torn_derived_generation_is_not_a_publication(self) -> None:
        self.current.unlink()
        self.legacy.unlink()
        (self.root / ".source_snapshot.bin").write_bytes(b"committed-source")
        current = indexd_runtime.DerivedMutationInfo(
            "current", self.build_id, "")
        with mock.patch.object(
                indexd_runtime, "derived_mutation_info", return_value=current), \
                mock.patch("corpusdb._derived_publication_health",
                           return_value={"state": "torn-generation"}):
            self.assertFalse(indexd_runtime._first_publication_committed(
                self.build_id))

    def test_pending_source_fence_rejects_an_old_snapshot(self) -> None:
        self.current.unlink()
        self.legacy.unlink()
        (self.root / ".source_snapshot.bin").write_bytes(b"old-source")
        (self.root / ".ingest_pending.bin").write_bytes(b"new-source")
        current = indexd_runtime.DerivedMutationInfo(
            "current", self.build_id, "")
        with mock.patch.object(
                indexd_runtime, "derived_mutation_info", return_value=current), \
                mock.patch("corpusdb._derived_publication_health",
                           return_value={"state": "ready"}):
            self.assertFalse(indexd_runtime._first_publication_committed(
                self.build_id))

    def test_interrupted_stream_reaps_its_ingest_and_preserves_interrupt(self) -> None:
        stream = _InterruptedStdout()
        process = mock.Mock(stdout=stream)
        args = mock.Mock(max=0)
        with mock.patch.object(search.subprocess, "Popen", return_value=process), \
                mock.patch.object(search.sys.stderr, "isatty", return_value=False):
            with self.assertRaises(KeyboardInterrupt):
                search._stream_first_run(
                    "needle", "keyword", args, False, None, None)
        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=0.5)
        process.kill.assert_not_called()
        self.assertTrue(stream.closed)

    def test_interrupted_stream_kills_an_ingest_that_ignores_termination(self) -> None:
        process = mock.Mock(stdout=mock.Mock())
        process.wait.side_effect = (
            subprocess.TimeoutExpired(["agrep-rs"], 0.5), 0)
        search._abort_streamed_ingest(process)
        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 2)

    def test_interrupt_after_stream_eof_still_reaps_the_ingest(self) -> None:
        process = mock.Mock(stdout=iter(()))
        process.wait.side_effect = (KeyboardInterrupt, 0)
        with mock.patch.object(search.subprocess, "Popen", return_value=process), \
                mock.patch.object(search.sys.stderr, "isatty", return_value=False):
            with self.assertRaises(KeyboardInterrupt):
                search._stream_first_run(
                    "needle", "keyword", mock.Mock(max=0), False, None, None)
        process.terminate.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 2)

    @staticmethod
    def _stream_args(*, limit: int = 0) -> SimpleNamespace:
        return SimpleNamespace(
            max=limit, agent=None, project=None, exclude_project=None,
            chat=None, who_filter=None, sort="score")

    def test_cold_classic_and_flat_zero_require_verified_completion(self) -> None:
        self.messages.write_text("{}\n", encoding="utf-8")
        unverifiable = (
            None,
            {"hits": [], "total": 0, "totals_exact": False},
            {"hits": [], "total": 0, "totals_exact": True,
             "index_missing": True},
            {"hits": [], "total": 0, "totals_exact": True,
             "tools_excluded": True},
            RuntimeError("damaged publication"),
        )
        for renderer in ("classic", "flat"):
            for result in unverifiable:
                stdout, stderr = io.StringIO(), io.StringIO()
                query_effect = result if isinstance(result, BaseException) else None
                with self.subTest(renderer=renderer, result=type(result).__name__), \
                        mock.patch.object(
                            search.subprocess, "Popen",
                            return_value=_CompletedStream([])), \
                        mock.patch.object(indexd_runtime, "finish_streamed_index"), \
                        mock.patch.object(
                            search, "run_query",
                            side_effect=query_effect,
                            return_value=None if query_effect else result), \
                        mock.patch.object(
                            search.common, "enable_vt", return_value=False), \
                        contextlib.redirect_stdout(stdout), \
                        contextlib.redirect_stderr(stderr):
                    rc = search._stream_first_run(
                        "needle", "keyword", self._stream_args(),
                        False, None, None)
                self.assertEqual(rc, 2)
                self.assertIn("could not verify", stderr.getvalue())

        exact = {"hits": [], "total": 0, "totals_exact": True}
        for story, expected in (
                (surface.FreshnessStory("current"), 1),
                (surface.FreshnessStory("unverified"), 2),
                (surface.FreshnessStory("current", absorbed_drift=True), 2)):
            with self.subTest(freshness=story), \
                    mock.patch.object(
                        search.subprocess, "Popen",
                        return_value=_CompletedStream([])), \
                    mock.patch.object(indexd_runtime, "finish_streamed_index"), \
                    mock.patch.object(search, "run_query", return_value=exact), \
                    mock.patch.object(
                        indexd_runtime, "freshness_story", return_value=story), \
                    mock.patch.object(
                        search.common, "enable_vt", return_value=False), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = search._stream_first_run(
                    "needle", "keyword", self._stream_args(),
                    False, None, None)
            self.assertEqual(rc, expected)

    @staticmethod
    def _matching_row() -> dict:
        return {
            "agent": "codex", "session": "s", "turn": 1,
            "project": "repo", "ts": 1, "who": "user",
            "text": "needle", "reply": "",
        }

    def test_completion_verification_errors_cannot_return_success(self) -> None:
        self.messages.write_text("{}\n", encoding="utf-8")
        errors = (
            search.DirectSnapshotQueryError("damaged publication"),
            search.NativeEventScanError("moving event generation"),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                stdout, stderr = io.StringIO(), io.StringIO()
                with mock.patch.object(
                        search.subprocess, "Popen",
                        return_value=_CompletedStream([self._matching_row()])), \
                        mock.patch.object(
                            indexd_runtime, "finish_streamed_index") as finish, \
                        mock.patch.object(search, "run_query", side_effect=error), \
                        mock.patch.object(search.common, "enable_vt", return_value=False), \
                        contextlib.redirect_stdout(stdout), \
                        contextlib.redirect_stderr(stderr):
                    rc = search._stream_first_run(
                        "needle", "keyword", self._stream_args(),
                        False, None, None)
                self.assertEqual(rc, 2)
                finish.assert_called_once_with(
                    allow_inline_fallback=True)
                self.assertIn("needle", stdout.getvalue())
                self.assertIn("could not verify", stderr.getvalue())
                self.assertNotIn("no hits", stderr.getvalue())

    def test_full_stream_page_needs_a_committed_publication(self) -> None:
        self.messages.write_text("{}\n", encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(
                search.subprocess, "Popen",
                return_value=_CompletedStream([self._matching_row()])), \
                mock.patch.object(
                    indexd_runtime, "finish_streamed_index") as finish, \
                mock.patch.object(
                    search, "_stream_publication_committed",
                    return_value=False) as committed, \
                mock.patch.object(
                    search, "run_query",
                    side_effect=AssertionError("completion scan ran")), \
                mock.patch.object(search.common, "enable_vt", return_value=False), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            rc = search._stream_first_run(
                "needle", "keyword", self._stream_args(limit=1),
                False, None, None)
        self.assertEqual(rc, 2)
        finish.assert_called_once_with(
            allow_inline_fallback=True)
        committed.assert_called_once_with()
        self.assertIn("needle", stdout.getvalue())
        self.assertIn("could not verify", stderr.getvalue())

    def test_full_stream_page_closes_row_emission_without_line_decoding(self) -> None:
        self.messages.write_text("{}\n", encoding="utf-8")
        first = (json.dumps({"row": self._matching_row()}) + "\n").encode("utf-8")
        stream = _RawDrainStream(first, b'{"row":{"text":"tail"}}\n' * 20_000)
        process = mock.Mock(stdout=stream)
        process.wait.return_value = 0
        output = io.StringIO()
        with mock.patch.object(
                search.subprocess, "Popen", return_value=process) as popen, \
                mock.patch.object(
                    indexd_runtime, "finish_streamed_index") as finish, \
                mock.patch.object(
                    search, "_stream_publication_committed", return_value=True), \
                mock.patch.object(search.common, "enable_vt", return_value=False), \
                mock.patch.object(search.sys.stderr, "isatty", return_value=False), \
                contextlib.redirect_stdout(output):
            rc = search._stream_first_run(
                "needle", "keyword", self._stream_args(limit=1),
                False, None, None)
        self.assertEqual(rc, 0)
        finish.assert_called_once_with(
            allow_inline_fallback=False)
        self.assertIn("needle", output.getvalue())
        self.assertEqual(stream.iterations, 1)
        self.assertEqual(stream.readinto_calls, 0)
        self.assertTrue(stream.tail.closed)
        self.assertNotIn("text", popen.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
