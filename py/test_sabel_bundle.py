from __future__ import annotations

import copy
import hashlib
import unittest

import sabel_shadow
import test_sabel_candidate_trace as candidate_fixture
import test_sabel_trial as trial_fixture


def around_retrieval_body():
    body = trial_fixture.retrieval_body()
    body.update({
        "retrieval_invocation_id": "around-1",
        "action_id": "action-around",
        "query_variant_id": "opened-handle",
        "surface": "around",
        "query": trial_fixture.captured("around-query", 17),
        "structured_args": trial_fixture.captured("around-args"),
        "semantic": {"state": "not_requested", "detail": None},
        "query_provenance": [{
            "query_start": 0, "query_end": 17, "state": "observed",
            "origin_surface_ids": ["retrieval-output"],
            "origin_ranges": [{
                "origin_surface_id": "retrieval-output",
                "artifact_id": "retrieval-output",
                "origin_start": 0, "origin_end": 17,
                "transform": "exact_copy",
            }],
        }],
        "lanes": [],
        "returned_handles": [],
        "opened_handle": "@session:1.digest",
        "rendered_output": trial_fixture.captured("around-output", 40),
        "candidate_trace_id": None,
        "renderer_record_id": "renderer-around",
        "exit_code": 0,
        "elapsed_ms": 9,
    })
    body["execution_options"]["semantic_requested"] = False
    body["retriever"]["dense_model_id"] = None
    body["query"]["artifact"]["sha256"] = hashlib.sha256(
        b"@session:1.digest").hexdigest()
    return body


def action_trace_body(recall, around, trial):
    body = trial_fixture.action_trace_body()
    body["events"][3]["payload"] = copy.deepcopy(recall["rendered_output"])
    around_call = trial_fixture.event(
        "event-5", 5, "tool_invocation",
        payload=trial_fixture.absent(
            "not_applicable", "arguments are captured separately"),
        action_id="action-around", tool_surface="agrep around",
        trigger="agent_decision_after_prompt", status="proposed",
        arguments=copy.deepcopy(around["structured_args"]),
        observation_event_ids=["event-1", "event-2", "event-4"],
    )
    around_result = trial_fixture.event(
        "event-6", 6, "tool_result", action_id="action-around",
        parent_event_id="event-5", tool_surface="agrep around",
        arguments=trial_fixture.absent(
            "not_applicable", "result event has no arguments"),
        status="executed_completed", payload=copy.deepcopy(around["rendered_output"]),
        exit_code=0, elapsed_ms=9,
        observation_event_ids=["event-1", "event-2", "event-4", "event-5"],
    )
    final = body["events"].pop()
    final.update({
        "event_id": "event-7", "sequence": 7,
        "observed_at": "2026-08-05T12:34:57Z",
        "source": trial_fixture.source("record-7"),
        "payload": copy.deepcopy(trial["final_response"]),
        "observation_event_ids": ["event-1", "event-2", "event-4", "event-6"],
    })
    body["events"].extend([around_call, around_result, final])
    body["completeness"] = trial_fixture.complete(7)
    return body


def annotation_body():
    body = trial_fixture.annotation_body()
    body["authorities"] = [{
        "authority_id": "historical-answer", "kind": "historical_span",
        "source_ranges": [candidate_fixture.source_range("answer-span", 20, 30)],
        "artifact": trial_fixture.absent(
            "not_applicable", "bound through atom range"),
    }]
    body["slots"] = [{
        "slot_id": "slot-1", "claim": trial_fixture.captured("claim-slot-1"),
        "required": True, "depends_on_all": [], "depends_on_any": [],
        "acceptable_authority_sets": [["historical-answer"]],
        "ambiguity": "clear", "confidence": 0.95,
    }]
    body["labels"] = [
        {
            "label_id": "annotation-1:preview-1", "kind": "preview_warrant",
            "candidate_id": "candidate-1", "slot_id": None,
            "state": "open_warranted",
        },
        {
            "label_id": "annotation-1:slot-1:candidate-1",
            "kind": "source_support", "candidate_id": "candidate-1",
            "slot_id": "slot-1", "state": "direct",
        },
    ]
    return body


def claim_assessment_body(trial):
    return {
        "schema": sabel_shadow.FINAL_CLAIM_SCHEMA,
        "version": sabel_shadow.SCHEMA_VERSION,
        "claim_assessment_id": "claims-1",
        "trial_id": "trial-1",
        "annotation_id": "annotation-1",
        "final_response": copy.deepcopy(trial["final_response"]["artifact"]),
        "claims": [{
            "claim_id": "claim-1", "response_start": 0, "response_end": 8,
            "slot_id": "slot-1", "outcome": "supported",
            "claim_authority": "opened_history_span",
            "evidence": [{
                "kind": "opened_history_span",
                "authority_id": "historical-answer",
                "artifact_id": "around-output",
                "retrieval_invocation_id": "around-1",
                "candidate_id": "candidate-1",
                "source_ranges": [
                    candidate_fixture.source_range("answer-span", 20, 30)],
            }],
        }],
        "slot_coverage": [{
            "slot_id": "slot-1", "state": "claimed",
            "claim_ids": ["claim-1"],
        }],
        "provenance": {
            "annotator_id": "claim-reviewer-1", "kind": "human",
            "adjudication_state": "single_annotator",
            "blind_to": ["candidate-ranking"],
            "sealed_at": "2026-08-05T12:36:00Z",
        },
    }


def recall_renderer_body(recall):
    body = trial_fixture.renderer_body()
    body["output"] = copy.deepcopy(recall["rendered_output"])
    body["segments"][0]["visible_source_ranges"] = [
        candidate_fixture.source_range("candidate-span", 0, 30)]
    body["segments"][0]["source_mappings"] = [{
        "source_range": candidate_fixture.source_range(
            "candidate-span", 0, 30),
        "output_start": 20, "output_end": 50,
    }]
    return body


def around_renderer_body(around):
    return {
        "schema": sabel_shadow.RENDERER_SCHEMA,
        "version": sabel_shadow.SCHEMA_VERSION,
        "renderer_record_id": "renderer-around",
        "trial_id": "trial-1",
        "retrieval_invocation_id": "around-1",
        "renderer": {
            "build_id": "agrep-build-1", "profile": "compact",
            "format_id": "around-full-v38",
        },
        "diagnostics": trial_fixture.diagnostics(1, rendered_size=40),
        "output": copy.deepcopy(around["rendered_output"]),
        "segments": [{
            "candidate_id": "candidate-1", "displayed_handle": None,
            "handle_output_start": None, "handle_output_end": None,
            "handle_sha256": None,
            "display_order": 1,
            "output_start": 0, "output_end": 40,
            "mapping_kind": "exact_decoded_source",
            "visible_source_ranges": [
                candidate_fixture.source_range("answer-span", 20, 30)],
            "source_mappings": [{
                "source_range": candidate_fixture.source_range(
                    "answer-span", 20, 30),
                "output_start": 30, "output_end": 40,
            }],
            "omitted_source_ranges": [],
        }],
    }


def bundle_bodies():
    trial = trial_fixture.trial_body()
    recall = trial_fixture.retrieval_body()
    recall["candidate_trace_id"] = "trace-v2-1"
    for lane in recall["lanes"]:
        lane["diagnostics"] = trial_fixture.diagnostics(1)
    around = around_retrieval_body()
    trace = candidate_fixture.trace_body()
    trace["generation_id"] = "index-1"
    annotation = annotation_body()
    return {
        "trial": trial,
        "action_trace": action_trace_body(recall, around, trial),
        "retrievals": [recall, around],
        "renderers": [recall_renderer_body(recall), around_renderer_body(around)],
        "candidate_traces": [trace],
        "annotation": annotation,
        "claim_assessment": claim_assessment_body(trial),
    }


def current_authority_bundle_bodies():
    bodies = bundle_bodies()
    current_artifact = trial_fixture.captured("current-status")
    bodies["annotation"]["authorities"] = [{
        "authority_id": "current-status", "kind": "current_snapshot",
        "source_ranges": [], "artifact": copy.deepcopy(current_artifact),
    }]
    bodies["annotation"]["slots"][0]["acceptable_authority_sets"] = [
        ["current-status"]]
    bodies["annotation"]["labels"][1]["state"] = "unrelated"

    trace = bodies["candidate_traces"][0]
    trace["candidates"][0]["slot_support"][0].update({
        "state": "unrelated", "supporting_source_ranges": [],
    })
    trace["gold_slots"][0].update({
        "state": "current_authority_required",
        "selected_authority_ids": ["current-status"],
        "source_ranges": [],
        "availability": {
            checkpoint: "not_applicable" for checkpoint in sabel_shadow.CHECKPOINTS
        },
        "first_loss_stage": None,
    })
    trace["result"] = {
        "status": "current_authority_required", "candidate_ids": [],
        "slot_results": [{
            "slot_id": "slot-1", "status": "unresolved",
            "candidate_ids": [], "claim_authority": "current_authority",
        }],
        "detail": "current source is required",
    }

    claim = bodies["claim_assessment"]["claims"][0]
    claim.update({
        "outcome": "supported", "claim_authority": "current_authority",
        "evidence": [{
            "kind": "current_authority", "authority_id": "current-status",
            "artifact_id": "current-status", "retrieval_invocation_id": None,
            "candidate_id": None, "source_ranges": [],
        }],
    })

    action = bodies["action_trace"]
    final = action["events"].pop()
    current_event = trial_fixture.event(
        "event-7", 7, "assistant_text", payload=current_artifact,
        observation_event_ids=["event-6"],
    )
    final.update({
        "event_id": "event-8", "sequence": 8,
        "observed_at": "2026-08-05T12:34:58Z",
        "source": trial_fixture.source("record-8"),
        "observation_event_ids": ["event-1", "event-2", "event-4",
                                  "event-6", "event-7"],
    })
    action["events"].extend([current_event, final])
    action["completeness"] = trial_fixture.complete(8)
    return bodies


def mixed_authority_bundle_bodies():
    bodies = bundle_bodies()
    current_artifact = trial_fixture.captured("current-status")
    visible_capture = next(
        surface["capture"] for surface in bodies["trial"]["input_surfaces"]
        if surface["surface"] == "visible_conversation")
    bodies["annotation"]["authorities"].extend([
        {
            "authority_id": "current-status", "kind": "current_snapshot",
            "source_ranges": [], "artifact": copy.deepcopy(current_artifact),
        },
        {
            "authority_id": "visible-alternative", "kind": "visible_context",
            "source_ranges": [], "artifact": copy.deepcopy(visible_capture),
        },
    ])
    bodies["annotation"]["slots"][0]["acceptable_authority_sets"] = [
        ["historical-answer", "current-status"], ["visible-alternative"],
    ]

    trace = bodies["candidate_traces"][0]
    trace["gold_slots"][0].update({
        "state": "mixed_authority_required",
        "selected_authority_ids": ["historical-answer", "current-status"],
    })
    trace["result"].update({
        "status": "partial_historical_support",
        "detail": "historical component is direct; current authority is external",
    })

    claim = bodies["claim_assessment"]["claims"][0]
    claim["claim_authority"] = "mixed_authority"
    claim["evidence"].append({
        "kind": "current_authority", "authority_id": "current-status",
        "artifact_id": "current-status", "retrieval_invocation_id": None,
        "candidate_id": None, "source_ranges": [],
    })

    action = bodies["action_trace"]
    final = action["events"].pop()
    current_event = trial_fixture.event(
        "event-7", 7, "assistant_text", payload=current_artifact,
        observation_event_ids=["event-6"],
    )
    final.update({
        "event_id": "event-8", "sequence": 8,
        "observed_at": "2026-08-05T12:34:58Z",
        "source": trial_fixture.source("record-8"),
        "observation_event_ids": [
            "event-1", "event-2", "event-4", "event-6", "event-7"],
    })
    action["events"].extend([current_event, final])
    action["completeness"] = trial_fixture.complete(8)
    return bodies


def seal_bundle(bodies=None):
    bodies = copy.deepcopy(bodies or bundle_bodies())
    return {
        "trial": sabel_shadow.seal_trial(bodies["trial"]),
        "action_trace": sabel_shadow.seal_action_trace(bodies["action_trace"]),
        "retrievals": [sabel_shadow.seal_retrieval(item)
                       for item in bodies["retrievals"]],
        "renderers": [sabel_shadow.seal_renderer(item)
                      for item in bodies["renderers"]],
        "candidate_traces": [sabel_shadow.seal_candidate_trace(item)
                             for item in bodies["candidate_traces"]],
        "annotation": sabel_shadow.seal_annotation(bodies["annotation"]),
        "claim_assessment": sabel_shadow.seal_final_claim_assessment(
            bodies["claim_assessment"]),
    }


class TrialBundleTests(unittest.TestCase):
    def test_complete_bundle_joins_exact_shown_and_opened_artifacts(self):
        bundle = seal_bundle()
        sabel_shadow.validate_trial_bundle(bundle)

    def test_prompt_only_hook_cannot_be_relabelled_as_retrieval_side_effect(self):
        bodies = bundle_bodies()
        bodies["action_trace"]["events"][2]["trigger"] = "hook_side_effect"
        with self.assertRaisesRegex(sabel_shadow.ShadowSchemaError,
                                    "prompt-only hooks"):
            sabel_shadow.validate_trial_bundle(seal_bundle(bodies))

    def test_renderer_and_trace_visible_ranges_must_agree(self):
        bodies = bundle_bodies()
        segment = bodies["renderers"][0]["segments"][0]
        segment.update({
            "mapping_kind": "source_derived_preview",
            "visible_source_ranges": [],
            "source_mappings": [],
            "omitted_source_ranges": [{
                "source_range": candidate_fixture.source_range(
                    "candidate-span", 0, 30),
                "reason_code": "head_truncated", "detail": "source not exact",
            }],
        })
        with self.assertRaisesRegex(sabel_shadow.ShadowSchemaError,
                                    "contradict the renderer"):
            sabel_shadow.validate_trial_bundle(seal_bundle(bodies))

    def test_annotation_label_and_around_links_must_resolve(self):
        bodies = bundle_bodies()
        bodies["annotation"]["labels"][0]["state"] = "abstain_warranted"
        with self.assertRaisesRegex(sabel_shadow.ShadowSchemaError,
                                    "label registry"):
            sabel_shadow.validate_trial_bundle(seal_bundle(bodies))
        bodies = bundle_bodies()
        bodies["candidate_traces"][0]["candidates"][0]["selection"][
            "around_invocation_id"] = "around-missing"
        with self.assertRaisesRegex(sabel_shadow.ShadowSchemaError,
                                    "around invocation"):
            sabel_shadow.validate_trial_bundle(seal_bundle(bodies))

    def test_cross_record_artifact_mismatch_is_detected_after_resealing(self):
        bodies = bundle_bodies()
        bodies["renderers"][0]["output"] = trial_fixture.captured(
            "different-render-output", 50)
        with self.assertRaisesRegex(sabel_shadow.ShadowSchemaError,
                                    "exact retrieval stdout"):
            sabel_shadow.validate_trial_bundle(seal_bundle(bodies))

    def test_contradicted_and_unresolved_claims_cannot_invent_authority(self):
        bodies = bundle_bodies()
        claim = bodies["claim_assessment"]["claims"][0]
        claim.update({
            "outcome": "contradicted", "claim_authority": "unsupported",
            "evidence": [],
        })
        with self.assertRaisesRegex(sabel_shadow.ShadowSchemaError,
                                    "acceptable authority"):
            seal_bundle(bodies)

        bodies = bundle_bodies()
        claim = bodies["claim_assessment"]["claims"][0]
        claim.update({
            "outcome": "unresolved",
            "claim_authority": "opened_history_span", "evidence": [],
        })
        with self.assertRaisesRegex(sabel_shadow.ShadowSchemaError,
                                    "unresolved claims require"):
            seal_bundle(bodies)

    def test_final_contradiction_is_independent_of_candidate_slot_support_axis(self):
        bodies = bundle_bodies()
        bodies["claim_assessment"]["claims"][0]["outcome"] = "contradicted"
        bundle = seal_bundle(bodies)
        sabel_shadow.validate_trial_bundle(bundle)
        self.assertEqual(
            bundle["candidate_traces"][0]["candidates"][0]["slot_support"][0]["state"],
            "direct")

    def test_query_provenance_cannot_cite_a_future_final_response(self):
        bodies = bundle_bodies()
        bodies["retrievals"][1]["query_provenance"][0][
            "origin_surface_ids"] = ["final-response"]
        bodies["retrievals"][1]["query_provenance"][0]["origin_ranges"][0].update({
            "origin_surface_id": "final-response",
            "artifact_id": "final-response",
        })
        with self.assertRaisesRegex(sabel_shadow.ShadowSchemaError,
                                    "supplied observations"):
            sabel_shadow.validate_trial_bundle(seal_bundle(bodies))

    def test_invocation_arguments_surface_and_available_origins_are_exact(self):
        bodies = bundle_bodies()
        bodies["retrievals"][0]["structured_args"] = trial_fixture.captured(
            "different-recall-args")
        with self.assertRaisesRegex(sabel_shadow.ShadowSchemaError,
                                    "exact arguments"):
            sabel_shadow.validate_trial_bundle(seal_bundle(bodies))

    def test_artifact_ids_and_returned_handle_order_are_bundle_global(self):
        bodies = bundle_bodies()
        collision = {
            "authority_id": "colliding-current", "kind": "current_snapshot",
            "source_ranges": [],
            "artifact": trial_fixture.captured("different-current-artifact"),
        }
        collision["artifact"]["artifact"]["artifact_id"] = "retrieval-output"
        bodies["annotation"]["authorities"].append(collision)
        with self.assertRaisesRegex(sabel_shadow.ShadowSchemaError,
                                    "multiple descriptors"):
            sabel_shadow.validate_trial_bundle(seal_bundle(bodies))

    def test_current_authority_must_be_exactly_observed_before_final(self):
        bodies = current_authority_bundle_bodies()
        bundle = seal_bundle(bodies)
        sabel_shadow.validate_trial_bundle(bundle)

        bodies = current_authority_bundle_bodies()
        bodies["action_trace"]["events"][-1][
            "observation_event_ids"].remove("event-7")
        with self.assertRaisesRegex(sabel_shadow.ShadowSchemaError,
                                    "current authority"):
            sabel_shadow.validate_trial_bundle(seal_bundle(bodies))

    def test_or_of_and_authority_sets_survive_full_bundle_grounding(self):
        complete = seal_bundle(mixed_authority_bundle_bodies())
        sabel_shadow.validate_trial_bundle(complete)

        missing_current = mixed_authority_bundle_bodies()
        claim = missing_current["claim_assessment"]["claims"][0]
        claim["evidence"] = claim["evidence"][:1]
        claim["claim_authority"] = "opened_history_span"
        with self.assertRaisesRegex(sabel_shadow.ShadowSchemaError,
                                    "complete annotated authority set"):
            sabel_shadow.validate_trial_bundle(seal_bundle(missing_current))

        partial = mixed_authority_bundle_bodies()
        claim = partial["claim_assessment"]["claims"][0]
        claim.update({
            "outcome": "partial", "claim_authority": "opened_history_span",
            "evidence": claim["evidence"][:1],
        })
        sabel_shadow.validate_trial_bundle(seal_bundle(partial))

        crossed_alternatives = mixed_authority_bundle_bodies()
        claim = crossed_alternatives["claim_assessment"]["claims"][0]
        claim["evidence"][1] = {
            "kind": "visible_context", "authority_id": "visible-alternative",
            "artifact_id": "visible_conversation",
            "retrieval_invocation_id": None, "candidate_id": None,
            "source_ranges": [],
        }
        with self.assertRaisesRegex(sabel_shadow.ShadowSchemaError,
                                    "complete annotated authority set"):
            sabel_shadow.validate_trial_bundle(seal_bundle(crossed_alternatives))

        incomplete_historical_authority = mixed_authority_bundle_bodies()
        second_range = candidate_fixture.source_range("second-answer", 10, 15)
        incomplete_historical_authority["annotation"]["authorities"][0][
            "source_ranges"].append(second_range)
        incomplete_historical_authority["candidate_traces"][0]["gold_slots"][0][
            "source_ranges"].append(second_range)
        with self.assertRaisesRegex(sabel_shadow.ShadowSchemaError,
                                    "direct support must cover every"):
            seal_bundle(incomplete_historical_authority)

    def test_exact_handle_surface_and_origin_joins_reject_false_links(self):
        bodies = bundle_bodies()
        bodies["retrievals"][0]["returned_handles"].append("@ghost:2.digest")
        with self.assertRaisesRegex(sabel_shadow.ShadowSchemaError,
                                    "display order"):
            sabel_shadow.validate_trial_bundle(seal_bundle(bodies))

        bodies = bundle_bodies()
        bodies["action_trace"]["events"][2]["tool_surface"] = "agrep search"
        bodies["action_trace"]["events"][3]["tool_surface"] = "agrep search"
        with self.assertRaisesRegex(sabel_shadow.ShadowSchemaError,
                                    "logical tool surface"):
            sabel_shadow.validate_trial_bundle(seal_bundle(bodies))

        bodies = bundle_bodies()
        bodies["retrievals"][0]["query_provenance"][0][
            "origin_surface_ids"] = ["workspace_state"]
        bodies["retrievals"][0]["query_provenance"][0]["origin_ranges"][0].update({
            "origin_surface_id": "workspace_state",
            "artifact_id": "workspace_state",
        })
        with self.assertRaisesRegex(sabel_shadow.ShadowSchemaError,
                                    "supplied observations"):
            sabel_shadow.validate_trial_bundle(seal_bundle(bodies))

    def test_final_claim_cannot_exceed_the_retrieval_slot_result(self):
        bodies = bundle_bodies()
        bodies["candidate_traces"][0]["result"] = {
            "status": "no_supported_evidence_found", "candidate_ids": [],
            "slot_results": [{
                "slot_id": "slot-1", "status": "unresolved",
                "candidate_ids": [], "claim_authority": "unsupported",
            }],
            "detail": "no opened supported evidence",
        }
        with self.assertRaisesRegex(sabel_shadow.ShadowSchemaError,
                                    "retrieval slot result"):
            sabel_shadow.validate_trial_bundle(seal_bundle(bodies))

    def test_policy_evaluation_scores_hook_side_effect_without_hiding_trial(self):
        bundle = seal_bundle()
        evaluation = sabel_shadow.evaluate_trial_policy(bundle)
        self.assertEqual(evaluation["outcomes"], {
            "invocation": "satisfied",
            "recall_count": 1,
            "around_count": 1,
            "recall_first": True,
            "around_immediately_after_recall": True,
            "opening": "within_policy",
            "hook": "prompt_only",
            "semantic_coverage": "available",
            "required_slot_coverage": "complete",
            "final_grounding": "grounded_or_clarified",
        })

        bodies = bundle_bodies()
        bodies["action_trace"]["events"][1][
            "hook_effect"] = "retrieval_side_effect"
        bodies["action_trace"]["events"][2]["trigger"] = "hook_side_effect"
        bad_but_observable = seal_bundle(bodies)
        sabel_shadow.validate_trial_bundle(bad_but_observable)
        evaluation = sabel_shadow.evaluate_trial_policy(bad_but_observable)
        self.assertEqual(evaluation["outcomes"]["hook"],
                         "retrieval_side_effect")

    def test_policy_evaluation_preserves_observation_and_route_unknowns(self):
        bodies = bundle_bodies()
        bodies["action_trace"]["completeness"] = {
            "state": "snapshot_partial", "observed_count": 7,
            "expected_count": 8, "pending_count": 1,
            "detail": "provider event stream ended before full export",
        }
        partial = seal_bundle(bodies)
        sabel_shadow.validate_trial_bundle(partial)
        evaluation = sabel_shadow.evaluate_trial_policy(partial)
        self.assertEqual(evaluation["outcomes"]["invocation"],
                         "observation_incomplete")
        self.assertEqual(evaluation["outcomes"]["opening"],
                         "observation_incomplete")

        bodies = bundle_bodies()
        bodies["annotation"]["valid_outcomes"] = ["history_retrieval"]
        claim = bodies["claim_assessment"]["claims"][0]
        claim.update({
            "outcome": "clarification", "claim_authority": "clarification",
            "evidence": [],
        })
        bodies["claim_assessment"]["slot_coverage"][0][
            "state"] = "clarified"
        disallowed_route = seal_bundle(bodies)
        sabel_shadow.validate_trial_bundle(disallowed_route)
        evaluation = sabel_shadow.evaluate_trial_policy(disallowed_route)
        self.assertEqual(evaluation["outcomes"]["final_grounding"],
                         "invalid_route")


if __name__ == "__main__":
    unittest.main()
