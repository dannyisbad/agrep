from __future__ import annotations

import copy
import unittest

import sabel_shadow


SHA = "c" * 64


def complete(count: int):
    return {
        "state": "complete", "observed_count": count, "expected_count": count,
        "pending_count": 0, "detail": None,
    }


def diagnostics(count: int):
    return {
        "coverage": complete(count),
        "truncation": {
            "state": "not_truncated", "limit": None,
            "observed_count": count, "detail": None,
        },
        "budget": {
            "state": "not_measured", "limit": None, "used": None,
            "unit": None, "detail": None,
        },
    }


def source_range(span="answer-span", start=0, end=30):
    return {
        "source_span_id": span, "atom_id": "atom-1",
        "decoded_start": start, "decoded_end": end,
    }


def binding(span="candidate-span", start=0, end=30):
    return {
        "source_range": source_range(span, start, end),
        "input_id": "source-input-1", "adapter": "codex",
        "materialization_state": "deterministically_materializable",
    }


def support(slot="slot-1", state="direct", *, span="answer-span",
            start=20, end=30):
    ranges = []
    if state in ("direct", "partial", "contradictory"):
        ranges = [source_range(span, start, end)]
    return {
        "slot_id": slot, "state": state,
        "supporting_source_ranges": ranges,
        "label_id": f"annotation-1:{slot}:candidate-1",
    }


def candidate(*, opened=True):
    selected = [source_range("answer-span", 20, 30)] if opened else []
    return {
        "candidate_id": "candidate-1",
        "evidence_id": "evidence-1",
        "view_id": "view-1",
        "exact_identity_id": "exact-1",
        "exact_content_sha256": SHA,
        "session_id": "session-1",
        "family_id": "family-1",
        "source_bindings": [binding()],
        "unresolved_reason": None,
        "lane_scores": [
            {"lane": "lexical_bm25", "rank": 1, "score": 12.0,
             "score_kind": "bm25"},
            {"lane": "q8", "rank": 1, "score": 0.83,
             "score_kind": "cosine_q8"},
            {"lane": "exhaustive_f16", "rank": 1, "score": 0.84,
             "score_kind": "cosine_f16"},
        ],
        "raw_sequence": 1,
        "preview_warrant": {
            "state": "open_warranted", "label_id": "annotation-1:preview-1",
        },
        "displayed_handle": "@session:1.digest",
        "slot_support": [support()],
        "selection": {
            "opened": opened,
            "selected_alias_atom_id": "atom-1" if opened else None,
            "selected_source_ranges": selected,
            "around_invocation_id": "around-1" if opened else None,
        },
    }


def decision(stage: str, *, visible=None):
    return {
        "candidate_id": "candidate-1", "decision": "kept",
        "reason_code": "pass_through" if stage != "render" else "rendered",
        "reason_detail": None, "retained_as": "candidate-1",
        "visible_source_ranges": visible if visible is not None
        else [source_range("candidate-span", 0, 30)],
    }


def gold_slot(*, render_present=True):
    availability = {
        "frozen_source": "present", "index": "present", "raw": "present",
        "view": "present", "exact_identity": "present", "session": "present",
        "family": "present",
        "render": "present" if render_present else "not_present_in_observed_set",
    }
    return {
        "slot_id": "slot-1", "required": True, "state": "source_bound",
        "selected_authority_ids": ["historical-answer"],
        "source_ranges": [source_range("answer-span", 20, 30)],
        "availability": availability,
        "first_loss_stage": None if render_present else "render",
    }


def trace_body():
    stages = []
    for stage in sabel_shadow.STAGES:
        stages.append({"stage": stage, "decisions": [decision(stage)]})
    return {
        "schema": sabel_shadow.CANDIDATE_TRACE_SCHEMA,
        "version": sabel_shadow.CANDIDATE_TRACE_VERSION,
        "trace_id": "trace-v2-1",
        "trial_id": "trial-1",
        "retrieval_invocation_id": "retrieval-1",
        "query_variant_id": "observed-query",
        "generation_id": "generation-1",
        "annotation_id": "annotation-1",
        "renderer_record_id": "renderer-1",
        "lane_diagnostics": [
            {"lane": "lexical_bm25", "kind": "lexical", "role": "serving",
             "score_kind": "bm25",
             "diagnostics": diagnostics(1),
             "pool_artifact_id": "lexical_bm25-pool"},
            {"lane": "q8", "kind": "semantic", "role": "serving",
             "score_kind": "cosine_q8",
             "diagnostics": diagnostics(1), "pool_artifact_id": "q8-pool"},
            {"lane": "exhaustive_f16", "kind": "semantic", "role": "diagnostic",
             "score_kind": "cosine_f16",
             "diagnostics": diagnostics(1),
             "pool_artifact_id": "exhaustive_f16-pool"},
        ],
        "candidates": [candidate()],
        "stages": stages,
        "gold_slots": [gold_slot()],
        "result": {
            "status": "direct_historical_support",
            "candidate_ids": ["candidate-1"],
            "slot_results": [{
                "slot_id": "slot-1", "status": "direct",
                "candidate_ids": ["candidate-1"],
                "claim_authority": "opened_history_span",
            }],
            "detail": "opened exact historical support",
        },
    }


class CandidateTraceV2Tests(unittest.TestCase):
    def test_valid_trace_separates_preview_warrant_from_source_support(self):
        sealed = sabel_shadow.seal_candidate_trace(trace_body())
        sabel_shadow.validate_candidate_trace(sealed)
        row = sealed["candidates"][0]
        self.assertEqual(row["preview_warrant"]["state"], "open_warranted")
        self.assertEqual(row["slot_support"][0]["state"], "direct")

    def test_render_presence_is_derived_from_visible_late_answer_bytes(self):
        body = trace_body()
        body["stages"][-1]["decisions"][0]["visible_source_ranges"] = [
            source_range("candidate-span", 0, 15)]
        body["gold_slots"][0] = gold_slot(render_present=False)
        sealed = sabel_shadow.seal_candidate_trace(body)
        self.assertEqual(sealed["gold_slots"][0]["first_loss_stage"], "render")

        false_claim = copy.deepcopy(body)
        false_claim["gold_slots"][0]["availability"]["render"] = "present"
        false_claim["gold_slots"][0]["first_loss_stage"] = None
        with self.assertRaisesRegex(
                sabel_shadow.ShadowSchemaError, "exact visible ranges"):
            sabel_shadow.seal_candidate_trace(false_claim)

    def test_unopened_discovery_prose_cannot_back_a_supported_result(self):
        body = trace_body()
        body["candidates"][0]["selection"] = candidate(opened=False)["selection"]
        with self.assertRaisesRegex(
                sabel_shadow.ShadowSchemaError, "opened, not merely previewed"):
            sabel_shadow.seal_candidate_trace(body)

    def test_opened_context_must_include_the_exact_supporting_span(self):
        body = trace_body()
        body["candidates"][0]["selection"]["selected_source_ranges"] = [
            source_range("opened-wrong-span", 0, 10)]
        with self.assertRaisesRegex(
                sabel_shadow.ShadowSchemaError, "ranges opened by around"):
            sabel_shadow.seal_candidate_trace(body)

    def test_zero_open_is_representable_and_multiple_open_is_scoreable(self):
        no_open = trace_body()
        no_open["candidates"][0]["selection"] = candidate(opened=False)["selection"]
        no_open["result"] = {
            "status": "no_supported_evidence_found", "candidate_ids": [],
            "slot_results": [{
                "slot_id": "slot-1", "status": "unresolved",
                "candidate_ids": [], "claim_authority": "unsupported",
            }],
            "detail": "preview did not warrant opening",
        }
        sabel_shadow.validate_candidate_trace(
            sabel_shadow.seal_candidate_trace(no_open))

        two_open = trace_body()
        second = copy.deepcopy(two_open["candidates"][0])
        second.update({
            "candidate_id": "candidate-2", "evidence_id": "evidence-2",
            "view_id": "view-2", "exact_identity_id": "exact-2",
            "session_id": "session-2", "family_id": "family-2",
            "raw_sequence": 2, "displayed_handle": "@session:2.digest",
        })
        for score in second["lane_scores"]:
            score["rank"] = 2
        two_open["candidates"].append(second)
        for lane_info in two_open["lane_diagnostics"]:
            lane_info["diagnostics"] = diagnostics(2)
        for stage in two_open["stages"]:
            second_decision = copy.deepcopy(stage["decisions"][0])
            second_decision["candidate_id"] = "candidate-2"
            second_decision["retained_as"] = "candidate-2"
            stage["decisions"].append(second_decision)
        # The observation schema must preserve policy violations rather than
        # making bad real trials unrepresentable. Bundle scoring flags >1 open.
        sabel_shadow.validate_candidate_trace(
            sabel_shadow.seal_candidate_trace(two_open))

    def test_lane_ranks_are_contiguous_and_score_semantics_are_declared(self):
        body = trace_body()
        body["candidates"][0]["lane_scores"][0]["rank"] = 2
        with self.assertRaisesRegex(sabel_shadow.ShadowSchemaError, "contiguous"):
            sabel_shadow.seal_candidate_trace(body)
        body = trace_body()
        body["candidates"][0]["lane_scores"][0]["score_kind"] = "cosine"
        with self.assertRaisesRegex(sabel_shadow.ShadowSchemaError, "score semantics"):
            sabel_shadow.seal_candidate_trace(body)

    def test_required_slots_are_and_not_flat_span_union(self):
        body = trace_body()
        second_gold = copy.deepcopy(body["gold_slots"][0])
        second_gold.update({
            "slot_id": "slot-2",
            "source_ranges": [source_range("fact-2", 0, 10)],
        })
        body["gold_slots"].append(second_gold)
        body["candidates"][0]["slot_support"].append(
            support("slot-2", "unavailable_or_unjudged"))
        body["result"]["slot_results"].append({
            "slot_id": "slot-2", "status": "unresolved",
            "candidate_ids": [], "claim_authority": "unsupported",
        })
        with self.assertRaisesRegex(sabel_shadow.ShadowSchemaError,
                                    "every required historical slot"):
            sabel_shadow.seal_candidate_trace(body)

    def test_tool_only_evidence_is_not_silently_scored_unrelated(self):
        body = trace_body()
        body["candidates"][0]["slot_support"][0] = support(
            state="omitted_by_view")
        body["candidates"][0]["selection"] = candidate(opened=False)["selection"]
        body["result"] = {
            "status": "no_supported_evidence_found", "candidate_ids": [],
            "slot_results": [{
                "slot_id": "slot-1", "status": "unresolved",
                "candidate_ids": [], "claim_authority": "unsupported",
            }],
            "detail": "--no-tools omitted the only observed evidence",
        }
        sealed = sabel_shadow.seal_candidate_trace(body)
        self.assertEqual(sealed["candidates"][0]["slot_support"][0]["state"],
                         "omitted_by_view")

    def test_mixed_authority_preserves_direct_historical_component_as_partial(self):
        body = trace_body()
        body["gold_slots"][0].update({
            "state": "mixed_authority_required",
            "selected_authority_ids": ["historical-answer", "current-status"],
        })
        body["result"]["status"] = "partial_historical_support"
        body["result"]["detail"] = (
            "history was opened directly; current status remains required")
        sealed = sabel_shadow.seal_candidate_trace(body)
        self.assertEqual(sealed["result"]["status"],
                         "partial_historical_support")

    def test_contradiction_requires_exact_opened_contradictory_ranges(self):
        body = trace_body()
        body["candidates"][0]["slot_support"][0] = support(
            state="contradictory")
        body["result"] = {
            "status": "contradictory_historical_evidence",
            "candidate_ids": ["candidate-1"],
            "slot_results": [{
                "slot_id": "slot-1", "status": "contradictory",
                "candidate_ids": ["candidate-1"],
                "claim_authority": "opened_history_span",
            }],
            "detail": "opened source contradicts the response claim",
        }
        sabel_shadow.validate_candidate_trace(
            sabel_shadow.seal_candidate_trace(body))

        body["candidates"][0]["slot_support"][0][
            "supporting_source_ranges"] = []
        with self.assertRaisesRegex(
                sabel_shadow.ShadowSchemaError, "contradictory judgments"):
            sabel_shadow.seal_candidate_trace(body)


if __name__ == "__main__":
    unittest.main()
