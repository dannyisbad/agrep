from __future__ import annotations

import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import ask
import sabel_observer
import sabel_shadow
import search
import semantic_q8


QUERY = "connector cause and whether our probe damaged the socket"
GENERATION = "ab" * 16
PRIVATE_FIELD = sabel_shadow.SEMANTIC_CANDIDATE_LOSS_FIELD
CALL_KWARGS = {"limit": 2, "who": "agent", "family_diverse": True}
EFFECTIVE_FILTERS = {
    "who": "agent", "exclude_family": True, "_family_diverse": True,
}


def _fixture(
        *, source_key: str = "id", request_filters: dict | None = None,
) -> tuple[dict, dict, list[dict]]:
    f16_ordinals = list(range(1, 9))
    f16_scores = [0.95, 0.95, 0.93, 0.91, 0.89, 0.87, 0.85, 0.83]
    groups = [1, 1, 1, 1, 2, 2, 3, 4]
    q8_ordinals = [2, 1, 3, 4, 5, 6, 7, 8]
    q8_scores = [0.99, 0.98, 0.94, 0.92, 0.90, 0.88, 0.86, 0.84]
    diagnostic = {
        "q8_ordinals": q8_ordinals,
        "q8_scores": q8_scores,
        "f16_ordinals": f16_ordinals,
        "f16_scores": f16_scores,
        "f16_groups": groups,
        "group_count": 5,
    }
    texts = {
        1: "provisional connector clue",
        2: "final connector cause and socket probe consequence",
        3: "final connector cause and socket probe consequence",
        4: "later family detail",
        5: "Merlin boundary and authorization phrase",
        6: "final connector cause and socket probe consequence",
        7: "other direct evidence",
        8: "last candidate",
    }
    sessions = {
        1: "child", 2: "parent", 3: "parent", 4: "parent",
        5: "route", 6: "route-copy", 7: "third", 8: "fourth",
    }
    rows = []
    for ordinal in f16_ordinals:
        row = {
            "ordinal": ordinal,
            source_key: f"agent:session:{ordinal}#r",
            "session": sessions[ordinal],
            "who": "agent",
            "turn": ordinal,
            "text": texts[ordinal],
            "content_digest": f"{ordinal:04x}",
        }
        rows.append(row)
    capture = ask._sabel_grouped_candidate_loss(
        QUERY, GENERATION, 2, diagnostic, rows, {"who": "agent"}, None,
        [1, 5], request_level="hybrid",
        request_filters=request_filters or EFFECTIVE_FILTERS)
    return capture, diagnostic, rows


def _bound(capture: dict) -> dict:
    return {
        **copy.deepcopy(capture["record"]),
        "artifact_id": "artifact-1",
        "retrieval_id": "retrieval-1",
    }


class _Refs:
    def __init__(self, rows: list[dict], *, source_key: str = "id") -> None:
        self.by_ordinal = {}
        for raw in rows:
            row = dict(raw)
            ordinal = row.pop("ordinal")
            if source_key == "mid" and "id" in row:
                row["mid"] = row.pop("id")
            self.by_ordinal[ordinal] = row

    def resolve(self, ordinals):
        return [dict(self.by_ordinal[int(ordinal)]) for ordinal in ordinals]

    def resolve_for_diagnostic(self, ordinals):
        return self.resolve(ordinals)


class _IntegrityRefs(_Refs):
    def __init__(self, rows: list[dict], *, absent_ordinal: int) -> None:
        super().__init__(rows)
        self.absent_ordinal = absent_ordinal
        self.considered = 0
        self.absent: set[int] = set()

    def resolve(self, ordinals):
        requested = [int(value) for value in ordinals]
        self.considered += len(set(requested))
        if self.absent_ordinal in requested:
            self.absent.add(self.absent_ordinal)
        return [dict(self.by_ordinal[ordinal]) for ordinal in requested
                if ordinal != self.absent_ordinal]

    def resolve_for_diagnostic(self, ordinals):
        considered, absent = self.considered, set(self.absent)
        try:
            return self.resolve(ordinals)
        finally:
            self.considered, self.absent = considered, absent

    def take_integrity_disclosure(self):
        if not self.absent:
            return None
        return {"absent": len(self.absent), "considered": self.considered}


class SemanticCandidateLossTests(unittest.TestCase):
    def test_eight_heads_exact_copy_and_top_h_are_visible_and_deterministic(self):
        capture, _diagnostic, _rows = _fixture()
        document = _bound(capture)
        sabel_shadow.validate_semantic_candidate_loss(document)
        candidates = document["candidates"]
        self.assertEqual(len(candidates), 8)
        self.assertEqual([item["f16"]["rank"] for item in candidates],
                         list(range(1, 9)))
        self.assertEqual([item["ordinal"] for item in candidates[:2]], [1, 2])
        self.assertEqual([item["q8"]["rank"] for item in candidates[:2]], [2, 1])
        self.assertEqual(document["serving"]["output_candidate_ids"],
                         ["row-1", "row-5"])

        exact = document["ablations"][0]["stages"][0]["decisions"]
        dropped = {item["candidate_id"]: item["retained_as"] for item in exact
                   if item["decision"] == "dropped"}
        self.assertEqual(dropped, {"row-3": "row-2", "row-6": "row-2"})
        self.assertEqual(document["ablations"][0]["candidate_ids"],
                         ["row-1", "row-2", "row-5", "row-7", "row-8"])
        self.assertEqual(document["ablations"][2]["candidate_ids"],
                         ["row-1", "row-2", "row-4", "row-5", "row-7", "row-8"])
        for ablation in document["ablations"]:
            self.assertEqual(ablation["support_selection"]["state"], "not_run")
            self.assertEqual(
                ablation["family_diversity"]["state"],
                "deferred_after_support_selection")

    def test_both_proven_ref_backends_and_conflicts(self):
        for source_key in ("id", "mid"):
            with self.subTest(source_key=source_key):
                capture, _diagnostic, _rows = _fixture(source_key=source_key)
                sabel_shadow.validate_semantic_candidate_loss(_bound(capture))
        _capture, diagnostic, rows = _fixture()
        rows[0]["mid"] = "conflicting-source"
        with self.assertRaisesRegex(ValueError, "identities conflict"):
            ask._sabel_grouped_candidate_loss(
                QUERY, GENERATION, 2, diagnostic, rows, None, None, [1, 5])

    def test_schema_rejects_cross_session_representative_and_duplicate_keep(self):
        capture, _diagnostic, _rows = _fixture()
        cross_session = _bound(capture)
        session_stage = cross_session["serving"]["stages"][1]
        session_stage["state"] = "applied"
        session_stage["reason"] = "fixture session maximum"
        for decision in session_stage["decisions"]:
            decision["reason_code"] = "session_f16_max"
        session_stage["decisions"][1] = {
            "candidate_id": "row-2", "decision": "dropped",
            "reason_code": "lower_f16_within_session",
            "retained_as": "row-1",
        }
        for index in (2, 3):
            session_stage["decisions"][index] = {
                "candidate_id": f"row-{index + 1}", "decision": "dropped",
                "reason_code": "lower_f16_within_session",
                "retained_as": "row-1",
            }
        with self.assertRaisesRegex(
                sabel_shadow.ShadowSchemaError, "different session"):
            sabel_shadow.validate_semantic_candidate_loss(cross_session)

        duplicate_keep = _bound(capture)
        exact = duplicate_keep["ablations"][0]["stages"][0]
        exact["decisions"][2] = {
            "candidate_id": "row-3", "decision": "kept",
            "reason_code": "unique_exact_copy", "retained_as": "row-3",
        }
        with self.assertRaisesRegex(
                sabel_shadow.ShadowSchemaError, "highest-ranked"):
            sabel_shadow.validate_semantic_candidate_loss(duplicate_keep)

    def test_schema_rejects_nonmax_family_nonprefix_output_and_non_top_h(self):
        capture, _diagnostic, _rows = _fixture()

        lower_family_winner = _bound(capture)
        family_stage = lower_family_winner["serving"]["stages"][2]
        family_stage["decisions"][0] = {
            "candidate_id": "row-1", "decision": "dropped",
            "reason_code": "lower_f16_within_family", "retained_as": "row-2",
        }
        family_stage["decisions"][1] = {
            "candidate_id": "row-2", "decision": "kept",
            "reason_code": "family_f16_max", "retained_as": "row-2",
        }
        for index in (2, 3):
            family_stage["decisions"][index]["retained_as"] = "row-2"
        output_stage = lower_family_winner["serving"]["stages"][3]
        output_stage["decisions"][0]["candidate_id"] = "row-2"
        output_stage["decisions"][0]["retained_as"] = "row-2"
        lower_family_winner["serving"]["output_candidate_ids"][0] = "row-2"
        with self.assertRaisesRegex(
                sabel_shadow.ShadowSchemaError, "highest-ranked"):
            sabel_shadow.validate_semantic_candidate_loss(lower_family_winner)

        nonprefix_output = _bound(capture)
        output_stage = nonprefix_output["serving"]["stages"][3]
        output_stage["decisions"][0] = {
            "candidate_id": "row-1", "decision": "dropped",
            "reason_code": "outside_output_limit", "retained_as": None,
        }
        output_stage["decisions"][2] = {
            "candidate_id": "row-7", "decision": "kept",
            "reason_code": "within_output_limit", "retained_as": "row-7",
        }
        nonprefix_output["serving"]["output_candidate_ids"] = ["row-5", "row-7"]
        with self.assertRaisesRegex(
                sabel_shadow.ShadowSchemaError, "highest-ranked"):
            sabel_shadow.validate_semantic_candidate_loss(nonprefix_output)

        non_top_h = _bound(capture)
        family_h = non_top_h["ablations"][0]["stages"][2]
        family_h["decisions"][1] = {
            "candidate_id": "row-2", "decision": "dropped",
            "reason_code": "outside_family_top_h", "retained_as": None,
        }
        family_h["decisions"][2] = {
            "candidate_id": "row-4", "decision": "kept",
            "reason_code": "within_family_top_h", "retained_as": "row-4",
        }
        non_top_h["ablations"][0]["candidate_ids"] = [
            "row-1", "row-4", "row-5", "row-7", "row-8"]
        with self.assertRaisesRegex(
                sabel_shadow.ShadowSchemaError, "highest-ranked"):
            sabel_shadow.validate_semantic_candidate_loss(non_top_h)

    def test_partial_diagnostic_fails_closed(self):
        _capture, diagnostic, rows = _fixture()
        diagnostic["q8_scores"].pop()
        with self.assertRaisesRegex(ValueError, "partial or misaligned"):
            ask._sabel_grouped_candidate_loss(
                QUERY, GENERATION, 2, diagnostic, rows, None, None, [1, 5])

    def test_grouped_pool_shadow_does_not_change_serving_rows_or_scores(self):
        capture, diagnostic, rows = _fixture()
        normal = (
            np.asarray(diagnostic["f16_ordinals"], dtype=np.int64),
            np.asarray(diagnostic["f16_scores"], dtype=np.float32),
            np.asarray(diagnostic["f16_groups"], dtype=np.uint32),
            diagnostic["group_count"],
        )
        refs = _Refs(rows)
        query = np.asarray([1.0, 0.0], dtype=np.float32)
        with mock.patch.dict(ask._CURRENT_MESSAGE_STATE,
                             {"generation": GENERATION}, clear=False), \
             mock.patch.object(semantic_q8, "grouped_exact_candidates",
                               return_value=normal) as serving, \
             mock.patch.object(
                 semantic_q8, "grouped_exact_candidates_with_shadow",
                 return_value=(normal, diagnostic)) as shadow:
            baseline = ask._q8_grouped_pool(query, refs, None, 2)
            sink = {}
            observed = ask._q8_grouped_pool(
                query, refs, None, 2, shadow_sink=sink, shadow_query=QUERY)
        serving.assert_called_once()
        shadow.assert_called_once()
        self.assertEqual(
            [row["session"] for row in baseline[0]],
            [row["session"] for row in observed[0]])
        np.testing.assert_array_equal(baseline[1], observed[1])
        self.assertEqual(baseline[2], observed[2])
        self.assertEqual(sink["capture"]["state"], "captured")
        self.assertEqual(len(sink["capture"]["record"]["candidates"]), 8)
        self.assertEqual(capture["record"]["serving"],
                         sink["capture"]["record"]["serving"])

    def test_diagnostic_failure_preserves_grouped_serving_result(self):
        _capture, diagnostic, rows = _fixture()
        normal = (
            np.asarray(diagnostic["f16_ordinals"], dtype=np.int64),
            np.asarray(diagnostic["f16_scores"], dtype=np.float32),
            np.asarray(diagnostic["f16_groups"], dtype=np.uint32),
            diagnostic["group_count"],
        )
        refs = _Refs(rows)
        query = np.asarray([1.0, 0.0], dtype=np.float32)
        with mock.patch.dict(ask._CURRENT_MESSAGE_STATE,
                             {"generation": GENERATION}, clear=False), \
             mock.patch.object(semantic_q8, "grouped_exact_candidates",
                               return_value=normal), \
             mock.patch.object(
                 semantic_q8, "grouped_exact_candidates_with_shadow",
                 return_value=(normal, None)):
            baseline = ask._q8_grouped_pool(query, refs, None, 2)
            sink = {}
            observed = ask._q8_grouped_pool(
                query, refs, None, 2, shadow_sink=sink, shadow_query=QUERY)
        self.assertIsNotNone(observed)
        self.assertEqual(
            [row["session"] for row in baseline[0]],
            [row["session"] for row in observed[0]])
        np.testing.assert_array_equal(baseline[1], observed[1])
        self.assertEqual(baseline[2], observed[2])
        self.assertEqual(sink["capture"]["state"], "unavailable")
        self.assertIn("failed closed", sink["capture"]["reason"])

    def test_raw_only_absence_cannot_change_serving_integrity_disclosure(self):
        _capture, diagnostic, rows = _fixture()
        normal = (
            np.asarray(diagnostic["f16_ordinals"], dtype=np.int64),
            np.asarray(diagnostic["f16_scores"], dtype=np.float32),
            np.asarray(diagnostic["f16_groups"], dtype=np.uint32),
            diagnostic["group_count"],
        )
        query = np.asarray([1.0, 0.0], dtype=np.float32)
        baseline_refs = _IntegrityRefs(rows, absent_ordinal=2)
        observed_refs = _IntegrityRefs(rows, absent_ordinal=2)
        with mock.patch.dict(ask._CURRENT_MESSAGE_STATE,
                             {"generation": GENERATION}, clear=False), \
             mock.patch.object(semantic_q8, "grouped_exact_candidates",
                               return_value=normal), \
             mock.patch.object(
                 semantic_q8, "grouped_exact_candidates_with_shadow",
                 return_value=(normal, diagnostic)):
            baseline = ask._q8_grouped_pool(query, baseline_refs, None, 2)
            sink = {}
            observed = ask._q8_grouped_pool(
                query, observed_refs, None, 2,
                shadow_sink=sink, shadow_query=QUERY)
        self.assertEqual(
            [row["session"] for row in baseline[0]],
            [row["session"] for row in observed[0]])
        np.testing.assert_array_equal(baseline[1], observed[1])
        self.assertIsNone(baseline_refs.take_integrity_disclosure())
        self.assertIsNone(observed_refs.take_integrity_disclosure())
        self.assertEqual(sink["capture"]["state"], "unavailable")

    @staticmethod
    def _publish(
            root: Path, result: dict, kwargs: dict | None = None,
    ) -> tuple[Path, dict]:
        with mock.patch.dict(os.environ, {sabel_observer.TRACE_ENV: str(root)}):
            scope = sabel_observer.safe_begin("recall", [QUERY])
            assert scope is not None
            sabel_observer.record_search_call(
                QUERY, "semantic", kwargs or CALL_KWARGS, result, 7)
            path = sabel_observer.safe_finish(scope, 0)
        assert path is not None
        return Path(path), sabel_observer.read_bundle(Path(path))

    def test_observer_binds_trace_and_excludes_private_result_meta(self):
        capture, _diagnostic, _rows = _fixture()
        result = {
            "hits": [],
            "semantic_status": {"state": "ready"},
            "semantic_accelerator_coverage": {"complete": True},
            PRIVATE_FIELD: capture,
        }
        with tempfile.TemporaryDirectory() as raw:
            path, manifest = self._publish(Path(raw) / "traces", result)
            descriptors = manifest["artifacts"]
            loss_descriptor = next(item for item in descriptors
                                   if item["kind"] == "candidate-loss")
            loss = json.loads((path / loss_descriptor["path"]).read_bytes())
            self.assertEqual(len(loss["candidates"]), 8)
            call_descriptor = next(item for item in descriptors
                                   if item["kind"] == "search-call")
            call = json.loads((path / call_descriptor["path"]).read_bytes())
            self.assertNotIn(PRIVATE_FIELD, call["result_meta"])
            self.assertEqual(
                call["semantic_pipeline"]["q8_candidates"]["artifact_id"],
                loss_descriptor["artifact_id"])
            self.assertTrue(
                call["semantic_pipeline"]["f16_rerank"]["execution_observed"])
            self.assertEqual(
                call["semantic_pipeline"]["q8_candidates"]["coverage"][
                    "generation"], loss["generation"])
            self.assertEqual(
                call["semantic_pipeline"]["effective_request"],
                loss["effective_request"])
            self.assertEqual(
                loss["effective_request"]["filters"], EFFECTIVE_FILTERS)


    def test_observer_binds_exact_session_exclusions(self):
        filters = {
            **EFFECTIVE_FILTERS, "_exclude_sessions": ["root", "side"],
        }
        capture, _diagnostic, _rows = _fixture(request_filters=filters)
        result = {
            "hits": [],
            "semantic_status": {"state": "ready"},
            "semantic_accelerator_coverage": {"complete": True},
            PRIVATE_FIELD: capture,
        }
        kwargs = {
            **CALL_KWARGS, "_exclude_sessions": ("side", "root", "side"),
        }
        with tempfile.TemporaryDirectory() as raw:
            path, manifest = self._publish(
                Path(raw) / "traces", result, kwargs)
            call_descriptor = next(
                item for item in manifest["artifacts"]
                if item["kind"] == "search-call")
            call = json.loads(
                (path / call_descriptor["path"]).read_bytes())
        self.assertEqual(
            call["semantic_pipeline"]["effective_request"]["filters"],
            filters)

    def test_observer_rejects_candidate_loss_from_a_different_query(self):
        capture, _diagnostic, _rows = _fixture()
        result = {
            "hits": [],
            "semantic_status": {"state": "ready"},
            "semantic_accelerator_coverage": {"complete": True},
            PRIVATE_FIELD: capture,
        }
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(
                os.environ, {sabel_observer.TRACE_ENV: str(Path(raw) / "traces")}):
            scope = sabel_observer.safe_begin("recall", ["different query"])
            self.assertIsNotNone(scope)
            sabel_observer.record_search_call(
                "different query", "semantic", CALL_KWARGS, result, 7)
            self.assertIsNone(sabel_observer.safe_finish(scope, 0))
            run = sabel_observer.newest_run(Path(raw) / "traces")
            self.assertFalse((run / "manifest.json").exists())

    def test_observer_rejects_candidate_loss_from_a_different_config(self):
        capture, _diagnostic, _rows = _fixture()
        result = {
            "hits": [],
            "semantic_status": {"state": "ready"},
            "semantic_accelerator_coverage": {"complete": True},
            PRIVATE_FIELD: capture,
        }
        wrong = {
            "limit": 99, "who": "tool", "family_diverse": False,
        }
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(
                os.environ, {sabel_observer.TRACE_ENV: str(Path(raw) / "traces")}):
            scope = sabel_observer.safe_begin("recall", [QUERY])
            self.assertIsNotNone(scope)
            sabel_observer.record_search_call(
                QUERY, "semantic", wrong, result, 7)
            self.assertIsNone(sabel_observer.safe_finish(scope, 0))
            run = sabel_observer.newest_run(Path(raw) / "traces")
            self.assertFalse((run / "manifest.json").exists())

    def test_effective_request_canonicalizes_all_retrieval_filters(self):
        self.assertEqual(
            tuple(sorted(ask.common.SEMANTIC_DEFAULT_EXCLUDED_ROLES)),
            sabel_shadow.SEMANTIC_EFFECTIVE_REQUEST_DEFAULT_EXCLUDED_ROLES)
        self.assertEqual(
            ask.SEMANTIC_MAX_RESULTS,
            sabel_shadow.SEMANTIC_EFFECTIVE_REQUEST_MAX_RESULTS)
        default_request = sabel_observer._semantic_effective_request_from_call(
            QUERY, {})
        self.assertEqual(default_request, sabel_shadow.semantic_effective_request(
            QUERY, "hybrid", 160, {
                "_exclude_who":
                    sabel_shadow.SEMANTIC_EFFECTIVE_REQUEST_DEFAULT_EXCLUDED_ROLES,
                "exclude_family": True, "_family_diverse": True,
            }))
        kwargs = {
            "limit": 2, "agent": "claude", "project": "/work/current",
            "exclude_project": "/work/old", "who": [None, ["tool", "tool"]],
            "model": "opus", "model_soft": True, "chat": "abc",
            "since_ms": 10, "until_ms": 20,
            "exclude_session": "caller", "exclude_session_from_turn": 7,
            "exclude_family": False, "family_diverse": False,
        }
        request = sabel_observer._semantic_effective_request_from_call(
            f"  {QUERY}  ", kwargs)
        self.assertEqual(request["level"], "hybrid")
        self.assertEqual(request["fetch_k"], 2)
        self.assertEqual(request["filters"], {
            "_exclude_who": ["tool"], "_family_diverse": False,
            "agent": "claude", "chat": "abc", "exclude_family": False,
            "exclude_project": "/work/old", "exclude_session": "caller",
            "exclude_session_from_turn": 7, "model": "opus",
            "model_soft": True, "project": "/work/current",
            "since_ms": 10, "until_ms": 20,
        })
        sabel_shadow.validate_semantic_effective_request(request)
        self.assertEqual(
            request["query_sha256"],
            _fixture()[0]["record"]["query_sha256"])

    def test_malformed_capture_abandons_observer_not_serving_result(self):
        capture, _diagnostic, _rows = _fixture()
        capture["record"]["candidates"][1]["q8"]["rank"] = 1
        result = {
            "hits": [{"session": "still-served"}],
            "semantic_status": {"state": "ready"},
            PRIVATE_FIELD: capture,
        }
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(
                os.environ, {sabel_observer.TRACE_ENV: str(Path(raw) / "traces")}):
            scope = sabel_observer.safe_begin("recall", [QUERY])
            self.assertIsNotNone(scope)
            sabel_observer.record_search_call(
                QUERY, "semantic", CALL_KWARGS, result, 7)
            self.assertIsNone(sabel_observer.safe_finish(scope, 0))
            self.assertEqual(result["hits"], [{"session": "still-served"}])
            run = sabel_observer.newest_run(Path(raw) / "traces")
            self.assertFalse((run / "manifest.json").exists())

    def test_private_field_is_stripped_direct_and_threaded(self):
        capture, _diagnostic, _rows = _fixture()
        raw_result = {
            "hits": [], "semantic_status": {"state": "ready"},
            "semantic_accelerator_coverage": {"complete": True},
            PRIVATE_FIELD: capture,
        }
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(
                os.environ, {sabel_observer.TRACE_ENV: str(Path(raw) / "direct")}):
            scope = sabel_observer.safe_begin("recall", [QUERY])
            self.assertIsNotNone(scope)
            with mock.patch.object(
                    search, "_semantic_candidates",
                    return_value=search.LaneResult(hits=[], engine="semantic")), \
                 mock.patch.object(search, "_finalize_query",
                                   return_value=copy.deepcopy(raw_result)):
                returned = search.run_query(
                    QUERY, mode="semantic", **CALL_KWARGS)
            self.assertNotIn(PRIVATE_FIELD, returned)
            self.assertNotIn(PRIVATE_FIELD, json.dumps(returned))
            self.assertIsNotNone(sabel_observer.safe_finish(scope, 0))

        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(
                os.environ, {sabel_observer.TRACE_ENV: str(Path(raw) / "threaded")}):
            scope = sabel_observer.safe_begin("recall", [QUERY])
            self.assertIsNotNone(scope)
            threaded_result = copy.deepcopy(raw_result)
            observation = {
                "invocation": (
                    QUERY, "semantic", CALL_KWARGS, time.monotonic_ns()),
                "completion": (threaded_result, 7, None),
            }
            thread = mock.Mock()
            thread.is_alive.return_value = False
            returned = search._finish_semantic_query((
                thread,
                {"_sabel_observation": observation,
                 "result": threaded_result},
                time.monotonic() + 1,
            ))
            self.assertNotIn(PRIVATE_FIELD, returned)
            self.assertNotIn(PRIVATE_FIELD, json.dumps(returned))
            self.assertIsNotNone(sabel_observer.safe_finish(scope, 0))

    def test_threaded_observer_never_requests_deadline_bound_candidate_trace(self):
        observation = {}
        with mock.patch.object(
                search, "_semantic_candidates",
                return_value=search.LaneResult(
                    hits=[], engine="semantic")) as candidates, \
             mock.patch.object(search, "_finalize_query",
                               return_value={"hits": []}):
            result = search.run_query(
                QUERY, mode="semantic", _sabel_observation=observation)
        self.assertEqual(result, {"hits": []})
        self.assertNotIn("capture_shadow", candidates.call_args.kwargs)

    def test_observer_off_does_not_request_shadow_capture(self):
        with mock.patch.dict(os.environ, {sabel_observer.TRACE_ENV: ""}), \
             mock.patch.object(search, "_semantic_candidates",
                               return_value=search.LaneResult(
                                   hits=[], engine="semantic")) as candidates, \
             mock.patch.object(search, "_finalize_query",
                               return_value={"hits": []}):
            result = search.run_query(QUERY, mode="semantic")
        self.assertEqual(result, {"hits": []})
        self.assertNotIn("capture_shadow", candidates.call_args.kwargs)
        self.assertNotIn(PRIVATE_FIELD, result)


if __name__ == "__main__":
    unittest.main()
