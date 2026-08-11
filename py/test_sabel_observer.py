from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import around
import recall
import sabel_observer
import sabel_pool
import search


class SabelObserverTests(unittest.TestCase):
    def _publish(self, root: Path, action, *, command: str = "recall"):
        with mock.patch.dict(
                os.environ, {sabel_observer.TRACE_ENV: str(root)}):
            scope = sabel_observer.safe_begin(command, ["needle", "--hits", "2"])
            self.assertIsNotNone(scope)
            action()
            published = sabel_observer.safe_finish(scope, 0)
        self.assertIsNotNone(published)
        return Path(published), sabel_observer.read_bundle(Path(published))

    @staticmethod
    def _json_artifacts(path: Path, manifest: dict, *, schema: str):
        documents = []
        for descriptor in manifest["artifacts"]:
            if descriptor.get("schema") == schema:
                documents.append(json.loads((path / descriptor["path"]).read_bytes()))
        return documents

    @staticmethod
    def _write_manifest(path: Path, manifest: dict) -> None:
        (path / "manifest.json").write_bytes(
            sabel_observer._canonical_json(manifest))

    @classmethod
    def _rewrite_json_artifact(cls, path: Path, manifest: dict,
                               descriptor: dict, mutate) -> None:
        artifact_path = path / descriptor["path"]
        document = json.loads(artifact_path.read_bytes())
        mutate(document)
        payload = sabel_observer._canonical_json(document)
        artifact_path.write_bytes(payload)
        descriptor["size"] = len(payload)
        descriptor["sha256"] = sabel_observer._sha256(payload)
        cls._write_manifest(path, manifest)

    def test_env_off_is_a_filesystem_noop(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            absent = Path(raw) / "must-not-exist"
            with mock.patch.dict(os.environ, {sabel_observer.TRACE_ENV: ""}), \
                 mock.patch.object(sabel_observer, "_atomic_write") as write:
                self.assertIsNone(sabel_observer.safe_begin("recall", ["q"]))
                sabel_observer.record_query(["q"], ["q"])
                sabel_observer.record_stdout(b"invisible")
                sabel_observer.record_pool("merged_pre_self", [])
            write.assert_not_called()
            self.assertFalse(absent.exists())

    def test_run_query_pool_is_ordered_self_bound_and_keeps_unknown_family(self) -> None:
        duplicate = {
            "session": "session-a", "turn": 7, "who": "agent", "ts": 11,
            "content_digest": "beef", "snippet": "the answer",
            "score": 91, "matched": "phrase", "_match_span": (1, 4),
        }
        result = {
            "hits": [dict(duplicate), dict(duplicate)],
            "total": 2, "chats": 1, "engine": "fixture", "mode": "keyword",
        }
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "traces"

            def action() -> None:
                sabel_observer.record_query(["needle"], ["needle"])
                with mock.patch.object(search, "_keyword_candidates", return_value=object()), \
                     mock.patch.object(search, "_finalize_query", return_value=result):
                    self.assertIs(search.run_query("needle", mode="keyword"), result)
                sabel_observer.record_pool(
                    "merged_pre_self", result["hits"], lane="merged")
                sabel_observer.record_pool(
                    "post_self", result["hits"], lane="merged")
                sabel_observer.record_pool(
                    "post_meta_sort", result["hits"], lane="merged")
                sabel_observer.record_pool(
                    "final_selected", result["hits"][:1], lane="selected")

            path, manifest = self._publish(root, action)
            pools = self._json_artifacts(
                path, manifest, schema=sabel_observer.POOL_SCHEMA)
            run_query_pool = next(
                pool for pool in pools if pool["stage"] == "run_query_return")
            self.assertEqual(run_query_pool["candidate_count"], 2)
            self.assertTrue(run_query_pool["ordered"])
            self.assertEqual(run_query_pool["retrieval_id"], manifest["retrieval_id"])
            self.assertIsNotNone(run_query_pool["call_id"])
            first, second = run_query_pool["candidates"]
            self.assertEqual(first["raw_rank"], 1)
            self.assertEqual(second["raw_rank"], 2)
            self.assertEqual(first["candidate_id"], second["candidate_id"])
            self.assertIsNone(first["duplicate_of_raw_rank"])
            self.assertEqual(second["duplicate_of_raw_rank"], 1)
            self.assertEqual(first["family"], {
                "root": None, "source": "not_exposed_on_hit",
                "state": "unavailable",
            })
            self.assertEqual(first["lane_score"]["value"], 91)
            self.assertEqual(first["evidence"]["match_span"], [1, 4])
            self.assertIsNone(
                first["exact_hashes"]["source_bytes_sha256"])
            descriptor = next(
                item for item in manifest["artifacts"]
                if item["artifact_id"] == run_query_pool["artifact_id"])
            payload = (path / descriptor["path"]).read_bytes()
            self.assertEqual(descriptor["sha256"],
                             sabel_observer._sha256(payload))
            parsed = sabel_pool.parse_pool_document(
                payload, expected_artifact_id=run_query_pool["artifact_id"],
                expected_retrieval_id=manifest["retrieval_id"],
                expected_lane="keyword", expected_stage="run_query_return")
            self.assertEqual(parsed["candidate_count"], 2)
            swapped = json.loads(payload)
            swapped["candidates"][0]["raw_rank"] = 2
            swapped["candidates"][1]["raw_rank"] = 1
            with self.assertRaisesRegex(
                    sabel_pool.PoolDocumentError, "one-based array rank"):
                sabel_pool.validate_pool_document(swapped)
            stages = [event["stage"] for event in manifest["events"]]
            for before, after in zip(
                    ("query", "run_query_return", "merged_pre_self", "post_self",
                     "post_meta_sort"),
                    ("run_query_return", "merged_pre_self", "post_self",
                     "post_meta_sort", "final_selected")):
                self.assertLess(stages.index(before), stages.index(after))

    def test_recall_integration_records_each_live_v1_stage_in_order(self) -> None:
        hit = {
            "session": "session-a", "turn": 7, "who": "user", "ts": 11,
            "agent": "codex", "project": "agrep", "score": 9,
            "matched": "phrase", "snippet": "needle solved",
            "content_digest": "beef",
        }
        result = {
            "hits": [hit], "total": 1, "chats": 1, "phrase_chats": 1,
            "tool_hits": 0, "engine": "fixture", "mode": "keyword",
            "terms_fallback": False, "content_fallback": False,
            "tools_excluded": False,
        }
        empty = {**result, "hits": [], "total": 0, "chats": 0,
                 "phrase_chats": 0}
        window = {
            "session": "session-a", "agent": "codex", "project": "agrep",
            "concept": "", "title": "", "center": 7,
            "first_turn": 0, "last_turn": 9,
            "turns": [{
                "turn": 7, "ts": 11, "who": "user",
                "text": "needle solved", "reply": "done",
                "reply_chars": 4, "reply_truncated": False,
            }],
            "events": [],
        }

        def finalize(spec, _candidates):
            return empty if spec.who == "tool" else result

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "traces"
            stdout = io.StringIO()
            with mock.patch.dict(
                    os.environ, {sabel_observer.TRACE_ENV: str(root)}), \
                 mock.patch.object(recall.indexd_runtime, "ensure_index",
                                   return_value=True), \
                 mock.patch.object(recall.indexd_runtime,
                                   "agent_freshness_notice", return_value=""), \
                 mock.patch.object(recall.common, "transcript_generation",
                                   return_value={"sig": "g"}), \
                 mock.patch.object(recall.common, "calling_self_exclusion",
                                   return_value=None), \
                 mock.patch.object(recall.common, "in_agent_context",
                                   return_value=False), \
                 mock.patch.object(recall.search, "_keyword_candidates",
                                   return_value=object()), \
                 mock.patch.object(recall.search, "_finalize_query",
                                   side_effect=finalize), \
                 mock.patch.object(recall.search, "_mark_history_meta",
                                   return_value={}), \
                 mock.patch.object(recall.explore, "get_windows",
                                   return_value=[window]), \
                 mock.patch.object(recall, "_expand",
                                   side_effect=lambda pairs, *_a, **_k: pairs), \
                 contextlib.redirect_stdout(stdout):
                rc = recall.main([
                    "needle", "--lexical", "--budget", "0",
                    "--color", "never"])
            self.assertEqual(rc, 0)
            self.assertIn("needle solved", stdout.getvalue())
            path = sabel_observer.newest_run(root)
            manifest = sabel_observer.read_bundle(path)
            stages = [event["stage"] for event in manifest["events"]]
            expected = [
                "query", "run_query_return", "merged_pre_self", "post_self",
                "post_meta_sort", "final_selected", "hydrated_windows",
                "expanded_windows",
            ]
            positions = [stages.index(stage) for stage in expected]
            self.assertEqual(positions, sorted(positions))
            self.assertEqual(manifest["lane_states"]["keyword"]["state"],
                             "captured")
            self.assertEqual(manifest["lane_states"]["semantic"]["state"],
                             "not_run")

    def test_semantic_unavailable_is_distinct_from_not_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "traces"
            _path, not_run = self._publish(
                root,
                lambda: sabel_observer.record_lane_not_run(
                    "semantic", "explicit --lexical"))
            self.assertEqual(not_run["lane_states"]["semantic"]["state"],
                             "not_run")
            self.assertEqual(not_run["lane_states"]["semantic"]["reason"],
                             "explicit --lexical")

            unavailable_result = {
                "hits": [], "total": 0, "chats": 0,
                "semantic_status": {
                    "state": "unavailable", "fallback_recommended": True},
                "semantic_accelerator_coverage": {"complete": False},
            }

            def unavailable_action() -> None:
                with mock.patch.object(
                        search, "_semantic_candidates", return_value=None):
                    self.assertIsNone(search.run_query("meaning", mode="semantic"))
                # Also capture the typed envelope path, whose status is stronger
                # than a bare None.
                sabel_observer.record_search_call(
                    "meaning", "semantic", {}, unavailable_result, 3)

            path, unavailable = self._publish(root, unavailable_action)
            self.assertEqual(
                unavailable["lane_states"]["semantic"]["state"], "unavailable")
            calls = self._json_artifacts(
                path, unavailable, schema="agrep.sabel.search-call.v1")
            typed = calls[-1]
            self.assertEqual(typed["state"], "unavailable")
            self.assertFalse(
                typed["semantic_pipeline"]["q8_candidates"]["execution_observed"])
            self.assertEqual(
                typed["semantic_pipeline"]["exhaustive_f16_diagnostic"]["state"],
                "not_run")

    def test_around_wrapper_captures_exact_stdout_handle_and_opened_source(self) -> None:
        handle = "@session-a:7.beef"
        rendered = "session-a  turns 7-7 of 0-9\n"
        window = {
            "session": "session-a", "center": 7, "first_turn": 0,
            "last_turn": 9, "turns": [], "events": [],
        }
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "traces"

            def fake_main(_argv) -> int:
                sabel_observer.record_around_open(
                    handle, session="session-a", requested_turn=7,
                    served_turn=7, window=window)
                around._stdout_print(rendered[:-1])
                return 0

            stdout = io.StringIO()
            with mock.patch.dict(
                    os.environ, {sabel_observer.TRACE_ENV: str(root)}), \
                 mock.patch.object(around, "_main", side_effect=fake_main), \
                 mock.patch.object(around.common, "transcript_generation",
                                   return_value={"sig": "g"}), \
                 contextlib.redirect_stdout(stdout):
                self.assertEqual(around.main([handle, "--full"]), 0)
            self.assertEqual(stdout.getvalue(), rendered)
            path = sabel_observer.newest_run(root)
            manifest = sabel_observer.read_bundle(path)
            self.assertEqual(manifest["opened_handle"], handle)
            stdout_descriptor = next(
                item for item in manifest["artifacts"]
                if item["artifact_id"] == manifest["stdout_artifact_id"])
            self.assertEqual((path / stdout_descriptor["path"]).read_bytes(),
                             rendered.encode("utf-8"))
            sources = self._json_artifacts(
                path, manifest, schema="agrep.sabel.around-source.v1")
            self.assertEqual(sources[0]["requested_handle"], handle)
            self.assertEqual(sources[0]["window"], window)

    def test_recall_payload_binds_exact_final_bytes_and_returned_handle(self) -> None:
        handle = "@session-a:7.beef"
        fake = "@fake:9.cafe"
        payload = (
            f"── {handle} · codex · agrep\n"
            f"     7 user: quoted historical token {fake}")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "traces"
            stdout = io.StringIO()
            with mock.patch.dict(
                    os.environ, {sabel_observer.TRACE_ENV: str(root)}):
                scope = sabel_observer.safe_begin("recall", ["needle"])
                self.assertIsNotNone(scope)
                with contextlib.redirect_stdout(stdout):
                    recall._write_payload(
                        payload, 0, returned_handles=[handle])
                path = sabel_observer.safe_finish(scope, 0)
            self.assertEqual(stdout.getvalue(), payload + "\n")
            manifest = sabel_observer.read_bundle(Path(path))
            self.assertEqual(manifest["returned_handles"], [handle])
            stdout_descriptor = next(
                item for item in manifest["artifacts"]
                if item["artifact_id"] == manifest["stdout_artifact_id"])
            self.assertEqual(
                (Path(path) / stdout_descriptor["path"]).read_bytes(),
                (payload + "\n").encode("utf-8"))

    def test_manifest_last_reader_rejects_missing_and_corrupt_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "traces"
            path, manifest = self._publish(
                root, lambda: sabel_observer.record_stdout(b"exact\n"))
            descriptor = manifest["artifacts"][0]
            artifact = path / descriptor["path"]
            artifact.write_bytes(artifact.read_bytes() + b"corrupt")
            with self.assertRaisesRegex(ValueError, "do not match descriptor"):
                sabel_observer.read_bundle(path)

            partial = root / "partial-without-manifest"
            partial.mkdir()
            (partial / "artifact.bin").write_bytes(b"partial")
            with self.assertRaisesRegex(ValueError, "no manifest"):
                sabel_observer.read_bundle(partial)

            duplicate_path, _ = self._publish(
                root, lambda: sabel_observer.record_stdout(b"exact\n"))
            manifest_path = duplicate_path / "manifest.json"
            duplicate = manifest_path.read_text(encoding="utf-8").replace(
                '"version":1', '"version":1,"version":1', 1)
            manifest_path.write_text(duplicate, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                sabel_observer.read_bundle(duplicate_path)

    def test_reader_rejects_stdout_reference_swap(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "traces"
            path, manifest = self._publish(
                root, lambda: sabel_observer.record_stdout(b"exact\n"))
            manifest["stdout_artifact_id"] = manifest["artifacts"][0][
                "artifact_id"]
            self._write_manifest(path, manifest)
            with self.assertRaisesRegex(ValueError, "sole trailing stdout"):
                sabel_observer.read_bundle(path)

    def test_reader_rejects_pool_self_binding_and_cross_reference_mutation(self):
        result = {
            "hits": [{
                "session": "session-a", "turn": 1, "who": "agent",
                "score": 2, "snippet": "answer", "content_digest": "beef",
            }],
            "total": 1, "chats": 1, "mode": "keyword", "engine": "fixture",
        }

        def action() -> None:
            sabel_observer.record_search_call(
                "needle", "keyword", {}, result, 3)

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "traces"
            path, manifest = self._publish(root, action)
            pool_descriptor = next(
                item for item in manifest["artifacts"]
                if item["kind"] == "pool")
            self._rewrite_json_artifact(
                path, manifest, pool_descriptor,
                lambda document: document.__setitem__(
                    "retrieval_id", "wrong-retrieval"))
            with self.assertRaisesRegex(ValueError, "sealed binding"):
                sabel_observer.read_bundle(path)

            path, manifest = self._publish(root, action)
            call = manifest["lane_states"]["keyword"]["calls"][0]
            call["query_artifact_id"] = call["pool_artifact_id"]
            self._write_manifest(path, manifest)
            with self.assertRaisesRegex(ValueError, "lane call contradicts"):
                sabel_observer.read_bundle(path)

    def test_reader_rejects_resealed_query_shape_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "traces"
            path, manifest = self._publish(
                root,
                lambda: sabel_observer.record_query(["needle"], ["needle"]))
            descriptor = next(
                item for item in manifest["artifacts"]
                if item["kind"] == "query")
            self._rewrite_json_artifact(
                path, manifest, descriptor,
                lambda document: document.pop("queries"))
            with self.assertRaisesRegex(ValueError, "keys mismatch"):
                sabel_observer.read_bundle(path)

    def test_unknown_or_malformed_semantic_status_is_unavailable(self) -> None:
        for name, status in (
                ("unknown", {"state": "future-magic"}),
                ("malformed-envelope", "ready"),
                ("malformed-state", {"state": 7})):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as raw:
                root = Path(raw) / "traces"

                def action(status=status) -> None:
                    sabel_observer.record_search_call(
                        "meaning", "semantic", {}, {
                            "hits": [], "total": 0, "chats": 0,
                            "semantic_status": status,
                        }, 3)

                path, manifest = self._publish(root, action)
                lane = manifest["lane_states"]["semantic"]
                self.assertEqual(lane["state"], "unavailable")
                call_document = self._json_artifacts(
                    path, manifest,
                    schema="agrep.sabel.search-call.v1")[0]
                self.assertEqual(call_document["state"], "unavailable")
                self.assertRegex(
                    call_document["reason"], "unknown|missing or malformed")

    def test_writer_abandons_oversize_trace_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "traces"
            with mock.patch.dict(
                    os.environ, {sabel_observer.TRACE_ENV: str(root)}):
                scope = sabel_observer.safe_begin("recall", ["needle"])
                self.assertIsNotNone(scope)
                with mock.patch.object(
                        sabel_observer, "MAX_ARTIFACT_BYTES", 4):
                    sabel_observer.record_stdout(b"12345")
                self.assertIsInstance(
                    scope.trace.fatal_record_error,
                    sabel_observer.ObserverBoundsError)
                self.assertIsNone(sabel_observer.safe_finish(scope, 0))
            run = sabel_observer.newest_run(root)
            self.assertFalse((run / "manifest.json").exists())
            self.assertEqual(list(run.iterdir()), [])

            path, manifest = self._publish(
                root, lambda: sabel_observer.record_stdout(b"exact"))
            stdout = next(
                item for item in manifest["artifacts"]
                if item["kind"] == "stdout")
            stdout["size"] -= 1
            self._write_manifest(path, manifest)
            with self.assertRaisesRegex(ValueError, "do not match descriptor"):
                sabel_observer.read_bundle(path)

    def test_nonfinite_pool_value_fails_closed_without_a_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "traces"
            with mock.patch.dict(
                    os.environ, {sabel_observer.TRACE_ENV: str(root)}):
                scope = sabel_observer.safe_begin("recall", ["needle"])
                self.assertIsNotNone(scope)
                sabel_observer.record_pool(
                    "merged_pre_self", [{"score": float("nan")}])
                self.assertIsNone(sabel_observer.safe_finish(scope, 0))
            run = sabel_observer.newest_run(root)
            self.assertFalse((run / "manifest.json").exists())

    def test_observer_write_failure_cannot_change_around_output_or_exit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "traces"
            stdout = io.StringIO()

            def fake_main(_argv) -> int:
                around._stdout_print("normal result")
                return 3

            with mock.patch.dict(
                    os.environ, {sabel_observer.TRACE_ENV: str(root)}), \
                 mock.patch.object(around, "_main", side_effect=fake_main), \
                 mock.patch.object(around.common, "transcript_generation",
                                   return_value={"sig": "g"}), \
                 mock.patch.object(sabel_observer, "_atomic_write",
                                   side_effect=OSError("disk fault")), \
                 contextlib.redirect_stdout(stdout):
                self.assertEqual(around.main(["session-a", "7"]), 3)
            self.assertEqual(stdout.getvalue(), "normal result\n")
            run = sabel_observer.newest_run(root)
            self.assertFalse((run / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
