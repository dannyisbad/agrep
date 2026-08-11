from __future__ import annotations

import copy
import hashlib
import unittest

import sabel_shadow


SHA = "b" * 64


def artifact(name: str, size: int = 8, *, encoding: str | None = "utf-8"):
    return {
        "artifact_id": name,
        "path": f"artifacts/{name}.bin",
        "size_bytes": size,
        "sha256": SHA,
        "media_type": "application/octet-stream",
        "encoding": encoding,
    }


def captured(name: str, size: int = 8):
    return {"state": "captured", "artifact": artifact(name, size), "detail": None}


def absent(state: str, detail: str):
    return {"state": state, "artifact": None, "detail": detail}


def complete(count: int):
    return {
        "state": "complete", "observed_count": count, "expected_count": count,
        "pending_count": 0, "detail": None,
    }


def diagnostics(count: int, *, rendered_size: int | None = None):
    budget = {
        "state": "not_measured", "limit": None, "used": None,
        "unit": None, "detail": None,
    }
    if rendered_size is not None:
        budget = {
            "state": "within_budget", "limit": 5000, "used": rendered_size,
            "unit": "bytes", "detail": None,
        }
    return {
        "coverage": complete(count),
        "truncation": {
            "state": "not_truncated", "limit": None,
            "observed_count": count, "detail": None,
        },
        "budget": budget,
    }


def source(record: str):
    return {
        "adapter": "codex", "session_id": "session-1",
        "record_locator": record, "record_sha256": SHA,
    }


def source_range(span="span-1", start=0, end=20):
    return {
        "source_span_id": span, "atom_id": "atom-1",
        "decoded_start": start, "decoded_end": end,
    }


def trial_body():
    surfaces = []
    for name in (
        "user_request", "visible_conversation", "system_instructions",
        "developer_instructions", "persistent_policy", "post_compaction_hook",
        "ordinary_prompt_hook", "tool_registry", "tool_help_contract",
        "workspace_state", "provider_native_events",
    ):
        capture = captured(name)
        if name == "workspace_state":
            capture = absent("unavailable", "workspace snapshot was not supplied")
        surfaces.append({"surface": name, "capture": capture})
    return {
        "schema": sabel_shadow.TRIAL_SCHEMA,
        "version": sabel_shadow.SCHEMA_VERSION,
        "trial_id": "trial-1",
        "case_id": "case-1",
        "created_at": "2026-08-05T12:34:56Z",
        "build_id": "agrep-build-1",
        "context_condition": "immediate_post_compaction",
        "agent": {
            "provider": "openai", "surface": "codex", "model": "gpt-5",
            "harness": "codex-desktop", "session_id": "session-1",
            "context_boundary_id": "compact-1",
        },
        "generation": {
            "source_generation_id": "source-1", "index_generation_id": "index-1",
            "source_coverage": complete(10), "index_coverage": complete(10),
        },
        "input_surfaces": surfaces,
        "action_trace_id": "actions-1",
        "claim_assessment_id": "claims-1",
        "retrieval_invocation_ids": ["retrieval-1", "around-1"],
        "final_response": captured("final-response"),
    }


def event(event_id, sequence, kind, *, payload=None, **overrides):
    value = {
        "event_id": event_id,
        "sequence": sequence,
        "observed_at": f"2026-08-05T12:34:{50 + sequence:02d}Z",
        "kind": kind,
        "source": source(f"record-{sequence}"),
        "action_id": None,
        "parent_event_id": None,
        "observation_event_ids": [],
        "tool_surface": None,
        "trigger": None,
        "arguments": absent("not_applicable", "not a tool invocation"),
        "status": "observed",
        "payload": payload or captured(f"payload-{sequence}"),
        "hook_id": None,
        "hook_version": None,
        "hook_effect": None,
        "exit_code": None,
        "elapsed_ms": None,
        "error_code": None,
    }
    value.update(overrides)
    return value


def action_trace_body():
    boundary = event("event-1", 1, "context_boundary")
    hook = event(
        "event-2", 2, "hook_receipt", hook_id="session-start-compact",
        hook_version="v6", hook_effect="prompt_only", exit_code=0,
        elapsed_ms=2)
    invocation = event(
        "event-3", 3, "tool_invocation",
        payload=absent("not_applicable", "arguments are captured separately"),
        action_id="action-1", tool_surface="agrep recall",
        trigger="agent_decision_after_prompt", status="proposed",
        arguments=captured("recall-args"),
        observation_event_ids=["event-1", "event-2"],
    )
    result = event(
        "event-4", 4, "tool_result", action_id="action-1",
        parent_event_id="event-3", tool_surface="agrep recall",
        arguments=absent("not_applicable", "result event has no arguments"),
        status="executed_completed", payload=captured("recall-result"),
        exit_code=0, elapsed_ms=18,
        observation_event_ids=["event-1", "event-2", "event-3"],
    )
    final = event(
        "event-5", 5, "final_response",
        observation_event_ids=["event-1", "event-2", "event-4"],
    )
    return {
        "schema": sabel_shadow.ACTION_TRACE_SCHEMA,
        "version": sabel_shadow.SCHEMA_VERSION,
        "action_trace_id": "actions-1",
        "trial_id": "trial-1",
        "completeness": complete(5),
        "events": [boundary, hook, invocation, result, final],
    }


def lane(name: str, score_kind: str):
    kind = "lexical" if name == "lexical_bm25" else "semantic"
    role = "diagnostic" if name == "exhaustive_f16" else "serving"
    return {
        "lane": name, "kind": kind, "role": role,
        "state": "captured", "requested": True,
        "available": True, "executed": True, "score_kind": score_kind,
        "diagnostics": diagnostics(2), "pool": captured(f"{name}-pool"),
    }


def retrieval_body():
    return {
        "schema": sabel_shadow.RETRIEVAL_SCHEMA,
        "version": sabel_shadow.SCHEMA_VERSION,
        "retrieval_invocation_id": "retrieval-1",
        "trial_id": "trial-1",
        "action_trace_id": "actions-1",
        "action_id": "action-1",
        "query_variant_id": "observed-query",
        "surface": "recall",
        "status": "executed_completed",
        "query": captured("query", 6),
        "structured_args": captured("recall-args"),
        "execution_options": {
            "semantic_requested": True,
            "self_exclusion": "profile_default",
            "output_profile": "compact",
            "byte_budget": 5000,
        },
        "retriever": {
            "build_id": "agrep-build-1", "profile_id": "compact-v38",
            "source_generation_id": "source-1", "index_generation_id": "index-1",
            "dense_model_id": "granite-30m", "lane_config_id": "lanes-v1",
            "query_constructor_id": "free-form-v1",
        },
        "semantic": {"state": "available", "detail": None},
        "query_provenance": [{
            "query_start": 0, "query_end": 6, "state": "observed",
            "origin_surface_ids": ["user_request"],
            "origin_ranges": [{
                "origin_surface_id": "user_request",
                "artifact_id": "user_request",
                "origin_start": 0, "origin_end": 6,
                "transform": "exact_copy",
            }],
        }],
        "lanes": [
            lane("lexical_bm25", "bm25"),
            lane("q8", "cosine_q8"),
            lane("exhaustive_f16", "cosine_f16"),
        ],
        "returned_handles": ["@session:1.digest"],
        "opened_handle": None,
        "rendered_output": captured("retrieval-output", 50),
        "candidate_trace_id": "trace-1",
        "renderer_record_id": "renderer-1",
        "exit_code": 0,
        "elapsed_ms": 18,
    }


def renderer_body():
    return {
        "schema": sabel_shadow.RENDERER_SCHEMA,
        "version": sabel_shadow.SCHEMA_VERSION,
        "renderer_record_id": "renderer-1",
        "trial_id": "trial-1",
        "retrieval_invocation_id": "retrieval-1",
        "renderer": {
            "build_id": "agrep-build-1", "profile": "compact",
            "format_id": "agent-compact-v38",
        },
        "diagnostics": diagnostics(1, rendered_size=50),
        "output": captured("retrieval-output", 50),
        "segments": [{
            "candidate_id": "candidate-1",
            "displayed_handle": "@session:1.digest",
            "handle_output_start": 0, "handle_output_end": 17,
            "handle_sha256": hashlib.sha256(
                b"@session:1.digest").hexdigest(),
            "display_order": 1,
            "output_start": 0, "output_end": 50,
            "mapping_kind": "exact_decoded_source",
            "visible_source_ranges": [source_range()],
            "source_mappings": [{
                "source_range": source_range(),
                "output_start": 30, "output_end": 50,
            }],
            "omitted_source_ranges": [],
        }],
    }


def annotation_body():
    return {
        "schema": sabel_shadow.ANNOTATION_SCHEMA,
        "version": sabel_shadow.SCHEMA_VERSION,
        "annotation_id": "annotation-1",
        "trial_id": "trial-1",
        "created_at": "2026-08-05T12:33:00Z",
        "history_requirement": "required",
        "valid_outcomes": ["history_retrieval", "clarification"],
        "constraints": [{
            "constraint_id": "constraint-1", "kind": "exclusion",
            "text": captured("constraint-text"),
        }],
        "authorities": [
            {
                "authority_id": "historical-answer", "kind": "historical_span",
                "source_ranges": [source_range()],
                "artifact": absent("not_applicable", "bound through atom range"),
            },
            {
                "authority_id": "current-status", "kind": "current_snapshot",
                "source_ranges": [], "artifact": captured("current-status"),
            },
        ],
        "slots": [
            {
                "slot_id": "slot-history", "claim": captured("claim-history"),
                "required": True, "depends_on_all": [], "depends_on_any": [],
                "acceptable_authority_sets": [["historical-answer"]],
                "ambiguity": "clear", "confidence": 0.95,
            },
            {
                "slot_id": "slot-current", "claim": captured("claim-current"),
                "required": True, "depends_on_all": ["slot-history"],
                "depends_on_any": [],
                "acceptable_authority_sets": [["current-status"]],
                "ambiguity": "clear", "confidence": 0.9,
            },
        ],
        "labels": [
            {
                "label_id": "annotation-1:preview-1",
                "kind": "preview_warrant", "candidate_id": "candidate-1",
                "slot_id": None, "state": "open_warranted",
            },
            {
                "label_id": "annotation-1:slot-1:candidate-1",
                "kind": "source_support", "candidate_id": "candidate-1",
                "slot_id": "slot-history", "state": "direct",
            },
        ],
        "provenance": {
            "annotator_id": "reviewer-1", "kind": "human",
            "adjudication_state": "single_annotator", "split": "smoke",
            "blind_to": ["retrieval-results", "final-response"],
            "revision_of": None, "sealed_at": "2026-08-05T12:33:30Z",
        },
    }


class TrialSchemaTests(unittest.TestCase):
    def test_trial_requires_exact_request_and_postcompact_boundary(self):
        sealed = sabel_shadow.seal_trial(trial_body())
        sabel_shadow.validate_trial(sealed)
        body = trial_body()
        body["input_surfaces"][0]["capture"] = absent(
            "unavailable", "request missing")
        with self.assertRaisesRegex(sabel_shadow.ShadowSchemaError, "user request"):
            sabel_shadow.seal_trial(body)
        body = trial_body()
        body["agent"]["context_boundary_id"] = None
        with self.assertRaisesRegex(sabel_shadow.ShadowSchemaError, "boundary id"):
            sabel_shadow.seal_trial(body)

    def test_action_trace_proves_prompt_only_hook_and_exact_result_join(self):
        sealed = sabel_shadow.seal_action_trace(action_trace_body())
        sabel_shadow.validate_action_trace(sealed)
        self.assertEqual(sealed["events"][1]["hook_effect"], "prompt_only")
        body = action_trace_body()
        body["events"][3]["parent_event_id"] = "event-2"
        with self.assertRaisesRegex(sabel_shadow.ShadowSchemaError, "exact invocation"):
            sabel_shadow.seal_action_trace(body)
        body = action_trace_body()
        body["events"][2]["arguments"] = absent("unavailable", "args lost")
        with self.assertRaisesRegex(sabel_shadow.ShadowSchemaError, "exact arguments"):
            sabel_shadow.seal_action_trace(body)

    def test_retrieval_preserves_meaning_unavailable_as_typed_lane_state(self):
        body = retrieval_body()
        body["semantic"] = {
            "state": "meaning_unavailable", "detail": "f16 sidecar absent",
        }
        body["lanes"][1] = {
            "lane": "q8", "kind": "semantic", "role": "serving",
            "state": "unavailable", "requested": True,
            "available": False, "executed": False, "score_kind": "cosine_q8",
            "diagnostics": {
                "coverage": {
                    "state": "unknown", "observed_count": 0,
                    "expected_count": None, "pending_count": None,
                    "detail": "semantic model did not load",
                },
                "truncation": {
                    "state": "unknown", "limit": None, "observed_count": 0,
                    "detail": "pool was not produced",
                },
                "budget": {
                    "state": "not_measured", "limit": None, "used": None,
                    "unit": None, "detail": None,
                },
            },
            "pool": absent("unavailable", "pool was not produced"),
        }
        sealed = sabel_shadow.seal_retrieval(body)
        self.assertEqual(sealed["lanes"][1]["state"], "unavailable")
        self.assertNotIn("score", sealed["lanes"][1])
        body["semantic"] = {"state": "available", "detail": None}
        with self.assertRaisesRegex(sabel_shadow.ShadowSchemaError, "unavailable lanes"):
            body["lanes"][1]["available"] = True
            sabel_shadow.seal_retrieval(body)

    def test_semantic_state_is_derived_only_from_serving_lane_truth(self):
        def unavailable_lane(name="semantic-secondary", *, role="serving"):
            return {
                "lane": name, "kind": "semantic", "role": role,
                "state": "unavailable", "requested": True,
                "available": False, "executed": False, "score_kind": None,
                "diagnostics": diagnostics(0),
                "pool": absent("unavailable", "lane was unavailable"),
            }

        body = retrieval_body()
        body["lanes"].append(unavailable_lane())
        body["semantic"] = {"state": "partial", "detail": "one serving lane unavailable"}
        sabel_shadow.validate_retrieval(sabel_shadow.seal_retrieval(body))
        body["semantic"] = {"state": "available", "detail": None}
        with self.assertRaisesRegex(sabel_shadow.ShadowSchemaError,
                                    "serving-lane-derived partial"):
            sabel_shadow.seal_retrieval(body)

        body = retrieval_body()
        body["lanes"][2] = unavailable_lane(
            "exhaustive_f16", role="diagnostic")
        sabel_shadow.validate_retrieval(sabel_shadow.seal_retrieval(body))

        body = retrieval_body()
        body["lanes"] = [body["lanes"][0], body["lanes"][2]]
        with self.assertRaisesRegex(sabel_shadow.ShadowSchemaError,
                                    "serving semantic lane"):
            sabel_shadow.seal_retrieval(body)

        body = retrieval_body()
        body["lanes"] = [body["lanes"][0], unavailable_lane("q8")]
        body["semantic"] = {
            "state": "meaning_unavailable", "detail": "semantic lane not searchable",
        }
        sabel_shadow.validate_retrieval(sabel_shadow.seal_retrieval(body))

        body = retrieval_body()
        not_run = unavailable_lane("q8")
        not_run.update({
            "state": "not_run", "available": True,
            "pool": absent("not_shown", "lane did not execute"),
        })
        body["lanes"] = [body["lanes"][0], not_run]
        body["semantic"] = {"state": "not_run", "detail": "execution did not begin"}
        sabel_shadow.validate_retrieval(sabel_shadow.seal_retrieval(body))

        body = retrieval_body()
        failed = unavailable_lane("q8")
        failed.update({"state": "failed", "available": True, "executed": True})
        body["lanes"] = [body["lanes"][0], failed]
        body["semantic"] = {"state": "failed", "detail": "execution failed"}
        sabel_shadow.validate_retrieval(sabel_shadow.seal_retrieval(body))

    def test_query_provenance_is_a_full_partition_with_typed_transforms(self):
        body = retrieval_body()
        body["query_provenance"] = [
            {
                "query_start": 0, "query_end": 3, "state": "observed",
                "origin_surface_ids": ["user_request"],
                "origin_ranges": [{
                    "origin_surface_id": "user_request",
                    "artifact_id": "user_request", "origin_start": 0,
                    "origin_end": 3, "transform": "exact_copy",
                }],
            },
            {
                "query_start": 3, "query_end": 6,
                "state": "novel_to_observation", "origin_surface_ids": [],
                "origin_ranges": [],
            },
        ]
        sabel_shadow.validate_retrieval(sabel_shadow.seal_retrieval(body))

        gap = copy.deepcopy(body)
        gap["query_provenance"][1]["query_start"] = 4
        with self.assertRaisesRegex(sabel_shadow.ShadowSchemaError,
                                    "contiguous"):
            sabel_shadow.seal_retrieval(gap)

        wrong_copy = retrieval_body()
        wrong_copy["query_provenance"][0]["origin_ranges"][0][
            "origin_end"] = 5
        with self.assertRaisesRegex(sabel_shadow.ShadowSchemaError,
                                    "exact-copy origin length"):
            sabel_shadow.seal_retrieval(wrong_copy)

        derived = retrieval_body()
        derived["query_provenance"][0]["origin_ranges"][0].update({
            "origin_end": 8, "transform": "derived",
        })
        sabel_shadow.validate_retrieval(sabel_shadow.seal_retrieval(derived))

    def test_renderer_distinguishes_visible_ranges_from_omitted_late_answer(self):
        sealed = sabel_shadow.seal_renderer(renderer_body())
        sabel_shadow.validate_renderer(sealed)
        body = renderer_body()
        body["segments"][0].update({
            "mapping_kind": "source_derived_preview",
            "visible_source_ranges": [],
            "source_mappings": [],
            "omitted_source_ranges": [{
                "source_range": source_range("late-answer", 40, 60),
                "reason_code": "head_truncated", "detail": "late conclusion omitted",
            }],
        })
        sabel_shadow.validate_renderer(sabel_shadow.seal_renderer(body))
        body["segments"][0]["visible_source_ranges"] = [source_range()]
        with self.assertRaisesRegex(sabel_shadow.ShadowSchemaError, "not exact visible evidence"):
            sabel_shadow.seal_renderer(body)

    def test_exact_renderer_mapping_cannot_inflate_or_overlap_evidence_bytes(self):
        body = renderer_body()
        inflated = source_range("inflated", 0, 1_000_000)
        body["segments"][0]["visible_source_ranges"] = [inflated]
        body["segments"][0]["source_mappings"][0]["source_range"] = inflated
        with self.assertRaisesRegex(sabel_shadow.ShadowSchemaError,
                                    "exactly equal length"):
            sabel_shadow.seal_renderer(body)

        body = renderer_body()
        body["segments"][0]["source_mappings"][0].update({
            "output_start": 0, "output_end": 20,
        })
        with self.assertRaisesRegex(sabel_shadow.ShadowSchemaError,
                                    "overlap displayed handle"):
            sabel_shadow.seal_renderer(body)

    def test_annotation_encodes_and_or_slots_and_typed_authority(self):
        sealed = sabel_shadow.seal_annotation(annotation_body())
        sabel_shadow.validate_annotation(sealed)
        body = annotation_body()
        body["slots"][1]["acceptable_authority_sets"] = [["historical-answer"]]
        # The schema preserves the authority kind. Cross-record scoring can then
        # reject historical evidence for this current-status slot without guessing.
        sealed = sabel_shadow.seal_annotation(body)
        authority = next(item for item in sealed["authorities"]
                         if item["authority_id"] == "historical-answer")
        self.assertEqual(authority["kind"], "historical_span")
        body = annotation_body()
        body["slots"][1]["depends_on_all"] = ["missing-slot"]
        with self.assertRaisesRegex(sabel_shadow.ShadowSchemaError, "declared slots"):
            sabel_shadow.seal_annotation(body)

        body = annotation_body()
        body["slots"][0]["acceptable_authority_sets"] = [
            ["historical-answer", "current-status"],
            ["current-status", "historical-answer"],
        ]
        with self.assertRaisesRegex(sabel_shadow.ShadowSchemaError,
                                    "authority sets must be unique"):
            sabel_shadow.seal_annotation(body)

    def test_all_new_records_are_sealed_against_tampering(self):
        records = [
            (sabel_shadow.seal_trial(trial_body()), sabel_shadow.validate_trial),
            (sabel_shadow.seal_action_trace(action_trace_body()),
             sabel_shadow.validate_action_trace),
            (sabel_shadow.seal_retrieval(retrieval_body()),
             sabel_shadow.validate_retrieval),
            (sabel_shadow.seal_renderer(renderer_body()),
             sabel_shadow.validate_renderer),
            (sabel_shadow.seal_annotation(annotation_body()),
             sabel_shadow.validate_annotation),
        ]
        for record, validator in records:
            with self.subTest(schema=record["schema"]):
                tampered = copy.deepcopy(record)
                tampered["version"] = 2
                with self.assertRaises(sabel_shadow.ShadowSchemaError):
                    validator(tampered)


if __name__ == "__main__":
    unittest.main()
