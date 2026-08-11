from __future__ import annotations

import copy
import math
import unittest

import sabel_shadow


SHA = "a" * 64


def coverage(state="complete"):
    if state == "complete":
        return {
            "state": state, "observed_count": 10, "expected_count": 10,
            "pending_count": 0, "detail": None,
        }
    return {
        "state": state, "observed_count": 8, "expected_count": 10,
        "pending_count": 2, "detail": "two source rows were not indexed",
    }


def manifest_body():
    return {
        "schema": sabel_shadow.GENERATION_SCHEMA,
        "version": sabel_shadow.SCHEMA_VERSION,
        "generation_id": "generation-1",
        "created_at": "2026-08-05T12:34:56Z",
        "publication": {
            "protocol": "manifest_last", "manifest_path": "manifest.json",
            "artifacts_sealed": True,
        },
        "inputs": [{
            "input_id": "claude-fixture", "kind": "source_snapshot",
            "identity": "claude:fixture:1", "size_bytes": 40, "sha256": SHA,
        }],
        "artifacts": [{
            "artifact_id": "traces", "path": "traces.jsonl",
            "size_bytes": 20, "sha256": SHA,
        }],
        "coverage": {"source": coverage(), "index": coverage()},
        "trace_count": 1,
    }


def candidate(candidate_id, rank, *, evidence="evidence-1", exact="copy-1",
              session="session-1", family="family-1", span="span-gold",
              support="direct"):
    spans = [] if span is None else [span]
    return {
        "candidate_id": candidate_id,
        "evidence_id": evidence,
        "view_id": f"view-{candidate_id}",
        "exact_identity_id": exact,
        "session_id": session,
        "family_id": family,
        "source_span_ids": spans,
        "unresolved_reason": None,
        "lane_scores": [
            {"lane": "q8", "rank": rank, "score": 0.9 - rank / 100,
             "score_kind": "cosine"},
            {"lane": "f16", "rank": rank, "score": 0.91 - rank / 100,
             "score_kind": "cosine"},
            {"lane": "fusion", "rank": rank, "score": 1 / (60 + rank),
             "score_kind": "rrf"},
        ],
        "support": {
            "state": support,
            "supporting_span_ids": spans if support in ("direct", "partial") else [],
            "label_provenance": {"kind": "fixture", "label_id": f"label-{candidate_id}"},
        },
        "raw_rank": rank,
    }


def decision(candidate_id, keep=True, *, reason=None, retained_as=None):
    return {
        "candidate_id": candidate_id,
        "decision": "kept" if keep else "dropped",
        "reason": reason or ("survives stage" if keep else "removed by stage"),
        "retained_as": candidate_id if keep else retained_as,
    }


def trace_body():
    first = candidate("c1", 1)
    duplicate = candidate("c2", 2)
    topical = candidate(
        "c3", 3, evidence="evidence-topical", exact="copy-topical",
        span="span-topical", support="topical_only")
    candidates = [first, duplicate, topical]
    active = ["c1", "c2", "c3"]
    stages = []
    for stage in sabel_shadow.STAGES:
        decisions = []
        for candidate_id in active:
            if stage == "exact_identity" and candidate_id == "c2":
                decisions.append(decision(
                    candidate_id, False, reason="exact copy represented by c1",
                    retained_as="c1"))
            elif stage == "family" and candidate_id == "c1":
                decisions.append(decision(
                    candidate_id, False, reason="family representative was topical",
                    retained_as="c3"))
            else:
                decisions.append(decision(candidate_id))
        active = [item["candidate_id"] for item in decisions
                  if item["decision"] == "kept"]
        stages.append({"stage": stage, "decisions": decisions})

    availability = {
        "frozen_source": "present", "index": "present",
        "raw": "present", "view": "present", "exact_identity": "present",
        "session": "present", "family": "not_present_in_observed_set",
        "render": "not_present_in_observed_set",
    }
    return {
        "schema": sabel_shadow.TRACE_SCHEMA,
        "version": sabel_shadow.SCHEMA_VERSION,
        "trace_id": "trace-1",
        "generation_id": "generation-1",
        "query_id": "query-1",
        "diagnostics": {
            "coverage": coverage("index_incomplete"),
            "truncation": {
                "state": "pool_truncated", "limit": 200,
                "observed_count": 3,
                "detail": "only the bounded top-200 pool was traced",
            },
            "budget": {
                "state": "budget_exhausted", "limit": 5000, "used": 5000,
                "unit": "milliseconds", "detail": "query deadline reached",
            },
        },
        "candidates": candidates,
        "stages": stages,
        "gold": {
            "state": "source_bound", "source_span_ids": ["span-gold"],
            "availability": availability, "first_loss_stage": "family",
        },
        "result": {
            "status": "no_supported_evidence_found", "candidate_ids": [],
            "detail": "no support survived this bounded, incomplete trace",
        },
    }


class CanonicalHashTests(unittest.TestCase):
    def test_canonical_bytes_ignore_mapping_insertion_order(self):
        first = {"z": 1, "unicode": "café", "a": {"b": True}}
        second = {"a": {"b": True}, "unicode": "café", "z": 1}
        self.assertEqual(
            sabel_shadow.canonical_bytes(first),
            sabel_shadow.canonical_bytes(second))
        self.assertEqual(
            sabel_shadow.canonical_sha256(first),
            sabel_shadow.canonical_sha256(second))
        self.assertIn("café".encode(), sabel_shadow.canonical_bytes(first))

    def test_canonical_json_rejects_non_finite_numbers_and_non_string_keys(self):
        with self.assertRaises(sabel_shadow.ShadowSchemaError):
            sabel_shadow.canonical_bytes({"bad": math.nan})
        with self.assertRaises(sabel_shadow.ShadowSchemaError):
            sabel_shadow.canonical_bytes({1: "bad key"})


class ManifestTests(unittest.TestCase):
    def test_manifest_is_sealed_and_tampering_is_rejected(self):
        original = manifest_body()
        sealed = sabel_shadow.seal_manifest(original)
        sabel_shadow.validate_manifest(sealed)
        self.assertNotIn("manifest_sha256", original)
        tampered = copy.deepcopy(sealed)
        tampered["trace_count"] = 2
        with self.assertRaisesRegex(
                sabel_shadow.ShadowSchemaError, "canonical hash mismatch"):
            sabel_shadow.validate_manifest(tampered)

    def test_manifest_requires_manifest_last_commit_metadata(self):
        body = manifest_body()
        body["publication"]["protocol"] = "artifacts_last"
        with self.assertRaisesRegex(
                sabel_shadow.ShadowSchemaError, "manifest-last"):
            sabel_shadow.seal_manifest(body)
        body = manifest_body()
        body["artifacts"][0]["path"] = "manifest.json"
        with self.assertRaisesRegex(
                sabel_shadow.ShadowSchemaError, "commit marker"):
            sabel_shadow.seal_manifest(body)

    def test_manifest_artifact_paths_are_unique(self):
        body = manifest_body()
        duplicate = copy.deepcopy(body["artifacts"][0])
        duplicate["artifact_id"] = "same-path"
        body["artifacts"].append(duplicate)
        with self.assertRaisesRegex(
                sabel_shadow.ShadowSchemaError, "paths must be unique"):
            sabel_shadow.seal_manifest(body)


class CandidateTraceTests(unittest.TestCase):
    def test_valid_trace_records_exact_loss_stage_and_bounded_result(self):
        sealed = sabel_shadow.seal_trace(trace_body())
        sabel_shadow.validate_trace(sealed)
        family = sealed["stages"][4]
        self.assertEqual(family["stage"], "family")
        self.assertEqual(family["decisions"][0]["decision"], "dropped")
        self.assertEqual(
            sealed["result"]["status"], "no_supported_evidence_found")
        self.assertEqual(sealed["gold"]["first_loss_stage"], "family")

    def test_every_stage_is_required_in_exact_order(self):
        body = trace_body()
        body["stages"][2], body["stages"][3] = (
            body["stages"][3], body["stages"][2])
        with self.assertRaisesRegex(
                sabel_shadow.ShadowSchemaError, "exactly in order"):
            sabel_shadow.seal_trace(body)

    def test_dropped_candidates_cannot_reappear(self):
        body = trace_body()
        body["stages"][5]["decisions"] = [decision("c1")]
        with self.assertRaisesRegex(
                sabel_shadow.ShadowSchemaError, "every active candidate"):
            sabel_shadow.seal_trace(body)

    def test_exact_identity_representative_must_be_kept_and_identical(self):
        body = trace_body()
        exact = body["stages"][2]["decisions"]
        exact[1]["retained_as"] = "missing"
        with self.assertRaisesRegex(
                sabel_shadow.ShadowSchemaError, "kept at this stage"):
            sabel_shadow.seal_trace(body)

    def test_raw_is_pass_through_and_collapse_drop_needs_representative(self):
        body = trace_body()
        body["stages"][0]["decisions"][0] = decision("c1", False)
        with self.assertRaisesRegex(
                sabel_shadow.ShadowSchemaError, "strict pass-through"):
            sabel_shadow.seal_trace(body)
        body = trace_body()
        body["stages"][4]["decisions"][0]["retained_as"] = None
        with self.assertRaisesRegex(
                sabel_shadow.ShadowSchemaError, "retained representative"):
            sabel_shadow.seal_trace(body)

    def test_gold_checkpoint_cannot_claim_presence_after_loss(self):
        body = trace_body()
        body["gold"]["availability"]["family"] = "present"
        with self.assertRaisesRegex(
                sabel_shadow.ShadowSchemaError, "surviving gold candidate"):
            sabel_shadow.seal_trace(body)

    def test_first_loss_is_derived_and_raw_presence_requires_index_presence(self):
        body = trace_body()
        body["gold"]["first_loss_stage"] = "session"
        with self.assertRaisesRegex(
                sabel_shadow.ShadowSchemaError, "derived first loss"):
            sabel_shadow.seal_trace(body)
        body = trace_body()
        body["gold"]["availability"]["index"] = "outside_bounded_coverage"
        with self.assertRaisesRegex(
                sabel_shadow.ShadowSchemaError, "raw-present"):
            sabel_shadow.seal_trace(body)
        body = trace_body()
        body["gold"]["availability"]["family"] = "outside_bounded_coverage"
        with self.assertRaisesRegex(
                sabel_shadow.ShadowSchemaError, "recorded collapse loss"):
            sabel_shadow.seal_trace(body)

    def test_diagnostics_are_explicit_and_budget_state_is_coherent(self):
        body = trace_body()
        del body["diagnostics"]["truncation"]
        with self.assertRaisesRegex(
                sabel_shadow.ShadowSchemaError, "keys mismatch"):
            sabel_shadow.seal_trace(body)
        body = trace_body()
        body["diagnostics"]["budget"].update({
            "state": "within_budget", "used": 5001,
        })
        with self.assertRaisesRegex(
                sabel_shadow.ShadowSchemaError, "cannot exceed"):
            sabel_shadow.seal_trace(body)

    def test_pool_observed_count_matches_candidate_ledger(self):
        body = trace_body()
        body["diagnostics"]["truncation"]["observed_count"] = 2
        with self.assertRaisesRegex(
                sabel_shadow.ShadowSchemaError, "recorded candidates"):
            sabel_shadow.seal_trace(body)

    def test_topical_candidate_cannot_back_a_direct_result(self):
        body = trace_body()
        body["result"] = {
            "status": "direct_historical_support", "candidate_ids": ["c3"],
            "detail": "topical but not supporting",
        }
        with self.assertRaisesRegex(
                sabel_shadow.ShadowSchemaError, "support labels"):
            sabel_shadow.seal_trace(body)

    def test_current_authority_gold_cannot_return_historical_absence(self):
        body = trace_body()
        body["gold"] = {
            "state": "current_authority_required", "source_span_ids": [],
            "first_loss_stage": None,
            "availability": {
                checkpoint: "not_applicable" for checkpoint in sabel_shadow.CHECKPOINTS
            },
        }
        with self.assertRaisesRegex(
                sabel_shadow.ShadowSchemaError, "current-authority result"):
            sabel_shadow.seal_trace(body)

    def test_unresolved_candidate_must_survive_all_collapse_stages(self):
        body = trace_body()
        hard = body["candidates"][2]
        hard["family_id"] = None
        hard["source_span_ids"] = []
        hard["unresolved_reason"] = "source locator was not mappable"
        body["stages"][4]["decisions"] = [decision("c1"), decision("c3")]
        body["stages"][5]["decisions"] = [decision("c1"), decision("c3")]
        body["gold"]["availability"]["family"] = "present"
        body["gold"]["availability"]["render"] = "present"
        body["gold"]["first_loss_stage"] = None
        body["result"] = {
            "status": "direct_historical_support", "candidate_ids": ["c1"],
            "detail": "source-bound direct support",
        }
        sabel_shadow.validate_trace(sabel_shadow.seal_trace(body))

        body["stages"][4]["decisions"] = [
            decision("c1"),
            decision("c3", False, retained_as="c1",
                     reason="attempted family collapse"),
        ]
        with self.assertRaisesRegex(
                sabel_shadow.ShadowSchemaError, "must survive"):
            sabel_shadow.seal_trace(body)

    def test_lane_scores_are_finite_auditable_and_do_not_exclude_lexical_rows(self):
        sealed = sabel_shadow.seal_trace(trace_body())
        self.assertEqual(
            {score["lane"] for score in sealed["candidates"][0]["lane_scores"]},
            {"q8", "f16", "fusion"})
        body = trace_body()
        body["candidates"][0]["lane_scores"][0]["score"] = math.inf
        with self.assertRaisesRegex(
                sabel_shadow.ShadowSchemaError, "non-finite"):
            sabel_shadow.seal_trace(body)
        body = trace_body()
        body["candidates"][0]["lane_scores"] = [{
            "lane": "lexical_bm25", "rank": 1, "score": 12.5,
            "score_kind": "bm25",
        }]
        sabel_shadow.validate_trace(sabel_shadow.seal_trace(body))
        body["candidates"][0]["lane_scores"] = []
        with self.assertRaisesRegex(
                sabel_shadow.ShadowSchemaError, "at least one retrieval lane"):
            sabel_shadow.seal_trace(body)

    def test_support_claims_must_use_gold_spans_and_trusted_labels(self):
        body = trace_body()
        body["candidates"][2]["support"] = {
            "state": "direct", "supporting_span_ids": ["span-topical"],
            "label_provenance": {"kind": "fixture", "label_id": "bad-label"},
        }
        with self.assertRaisesRegex(
                sabel_shadow.ShadowSchemaError, "source-bound gold spans"):
            sabel_shadow.seal_trace(body)

        body = trace_body()
        body["stages"][4]["decisions"] = [decision("c1"), decision("c3")]
        body["stages"][5]["decisions"] = [decision("c1"), decision("c3")]
        body["gold"]["availability"]["family"] = "present"
        body["gold"]["availability"]["render"] = "present"
        body["gold"]["first_loss_stage"] = None
        body["candidates"][0]["support"]["label_provenance"]["kind"] = "model"
        body["result"] = {
            "status": "direct_historical_support", "candidate_ids": ["c1"],
            "detail": "model prediction is not an adjudicated support label",
        }
        with self.assertRaisesRegex(
                sabel_shadow.ShadowSchemaError, "trusted label provenance"):
            sabel_shadow.seal_trace(body)

        body = trace_body()
        body["candidates"][2]["support"] = {
            "state": "not_judged", "supporting_span_ids": [],
            "label_provenance": {"kind": "unjudged", "label_id": "pending-c3"},
        }
        with self.assertRaisesRegex(
                sabel_shadow.ShadowSchemaError, "unjudged survivors"):
            sabel_shadow.seal_trace(body)

    def test_typed_not_in_history_status_is_rejected_but_prose_is_legal(self):
        body = trace_body()
        body["result"]["detail"] = (
            "This bounded search cannot establish not_in_history.")
        sabel_shadow.validate_trace(sabel_shadow.seal_trace(body))
        body["result"]["status"] = "not_in_history"
        with self.assertRaisesRegex(
                sabel_shadow.ShadowSchemaError, "must be one of"):
            sabel_shadow.seal_trace(body)


if __name__ == "__main__":
    unittest.main()
