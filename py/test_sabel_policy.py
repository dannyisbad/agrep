from __future__ import annotations

import copy
import hashlib
import unittest

import sabel_shadow
import test_sabel_bundle as bundle_fixture
import test_sabel_candidate_trace as candidate_fixture
import test_sabel_trial as trial_fixture


def _clarify_final_claim(bodies: dict) -> None:
    claim = bodies["claim_assessment"]["claims"][0]
    claim.update({
        "outcome": "clarification",
        "claim_authority": "clarification",
        "evidence": [],
    })
    bodies["claim_assessment"]["slot_coverage"][0].update({
        "state": "clarified",
        "claim_ids": [claim["claim_id"]],
    })


def _set_event_sequence(event: dict, sequence: int) -> None:
    event["sequence"] = sequence
    event["observed_at"] = f"2026-08-05T12:34:{50 + sequence:02d}Z"


def _without_any_retrieval(*, partial: bool = False) -> dict:
    bodies = bundle_fixture.bundle_bodies()
    trial = bodies["trial"]
    trial["retrieval_invocation_ids"] = []
    bodies["retrievals"] = []
    bodies["renderers"] = []
    bodies["candidate_traces"] = []
    bodies["annotation"]["labels"] = []
    _clarify_final_claim(bodies)

    old_events = bodies["action_trace"]["events"]
    final = copy.deepcopy(old_events[-1])
    final.update({
        "event_id": "event-3",
        "observation_event_ids": ["event-1", "event-2"],
        "source": trial_fixture.source("record-3"),
    })
    _set_event_sequence(final, 3)
    bodies["action_trace"]["events"] = old_events[:2] + [final]
    bodies["action_trace"]["completeness"] = (
        {
            "state": "snapshot_partial",
            "observed_count": 3,
            "expected_count": 4,
            "pending_count": 1,
            "detail": "one provider event remains unobserved",
        }
        if partial
        else trial_fixture.complete(3)
    )
    return bodies


def _recall_without_around(*, rendered: bool) -> dict:
    bodies = bundle_fixture.bundle_bodies()
    bodies["trial"]["retrieval_invocation_ids"] = ["retrieval-1"]
    bodies["retrievals"] = bodies["retrievals"][:1]
    bodies["renderers"] = bodies["renderers"][:1]

    events = bodies["action_trace"]["events"]
    final = copy.deepcopy(events[-1])
    final.update({
        "event_id": "event-5",
        "observation_event_ids": ["event-1", "event-2", "event-4"],
        "source": trial_fixture.source("record-5"),
    })
    _set_event_sequence(final, 5)
    bodies["action_trace"]["events"] = events[:4] + [final]
    bodies["action_trace"]["completeness"] = trial_fixture.complete(5)

    trace = bodies["candidate_traces"][0]
    candidate = trace["candidates"][0]
    candidate["selection"] = candidate_fixture.candidate(opened=False)["selection"]
    trace["result"] = {
        "status": "no_supported_evidence_found",
        "candidate_ids": [],
        "slot_results": [{
            "slot_id": "slot-1",
            "status": "unresolved",
            "candidate_ids": [],
            "claim_authority": "unsupported",
        }],
        "detail": "no candidate was opened",
    }
    _clarify_final_claim(bodies)

    if not rendered:
        candidate["displayed_handle"] = None
        render_decision = trace["stages"][-1]["decisions"][0]
        render_decision.update({
            "decision": "dropped",
            "reason_code": "budget_drop",
            "reason_detail": "candidate was not shown to the agent",
            "retained_as": None,
            "visible_source_ranges": [],
        })
        trace["gold_slots"][0] = candidate_fixture.gold_slot(
            render_present=False)
        bodies["retrievals"][0]["returned_handles"] = []
        renderer = bodies["renderers"][0]
        renderer["segments"] = []
        renderer["diagnostics"] = trial_fixture.diagnostics(
            0, rendered_size=50)
    return bodies


def _with_non_retrieval_tool_before_recall() -> dict:
    bodies = bundle_fixture.bundle_bodies()
    events = bodies["action_trace"]["events"]
    before = trial_fixture.event(
        "event-pretool-call",
        3,
        "tool_invocation",
        payload=trial_fixture.absent(
            "not_applicable", "arguments are captured separately"),
        action_id="action-pretool",
        tool_surface="workspace inspect",
        trigger="agent_decision_after_prompt",
        status="proposed",
        arguments=trial_fixture.captured("pretool-args"),
        observation_event_ids=["event-1", "event-2"],
    )
    before_result = trial_fixture.event(
        "event-pretool-result",
        4,
        "tool_result",
        action_id="action-pretool",
        parent_event_id="event-pretool-call",
        tool_surface="workspace inspect",
        arguments=trial_fixture.absent(
            "not_applicable", "result event has no arguments"),
        status="executed_completed",
        payload=trial_fixture.captured("pretool-result"),
        exit_code=0,
        elapsed_ms=3,
        observation_event_ids=["event-1", "event-2", "event-pretool-call"],
    )

    recall_call = copy.deepcopy(events[2])
    recall_call["observation_event_ids"] = [
        "event-1", "event-2", "event-pretool-result"]
    recall_result = copy.deepcopy(events[3])
    around_call = copy.deepcopy(events[4])
    around_result = copy.deepcopy(events[5])
    final = copy.deepcopy(events[6])
    reordered = [events[0], events[1], before, before_result,
                 recall_call, recall_result, around_call, around_result, final]
    for sequence, event in enumerate(reordered, start=1):
        _set_event_sequence(event, sequence)
    bodies["action_trace"]["events"] = reordered
    bodies["action_trace"]["completeness"] = trial_fixture.complete(9)
    return bodies


def _with_second_around() -> dict:
    bodies = bundle_fixture.bundle_bodies()
    first_around = bodies["retrievals"][1]
    second = copy.deepcopy(first_around)
    second.update({
        "retrieval_invocation_id": "around-2",
        "action_id": "action-around-2",
        "query_variant_id": "opened-handle-2",
        "query": trial_fixture.captured("around-query-2", 17),
        "structured_args": trial_fixture.captured("around-args-2"),
        "rendered_output": trial_fixture.captured("around-output-2", 40),
        "renderer_record_id": "renderer-around-2",
    })
    second["query"]["artifact"]["sha256"] = hashlib.sha256(
        second["opened_handle"].encode("utf-8")).hexdigest()
    bodies["retrievals"].append(second)
    bodies["trial"]["retrieval_invocation_ids"].append("around-2")

    second_renderer = copy.deepcopy(bodies["renderers"][1])
    second_renderer.update({
        "renderer_record_id": "renderer-around-2",
        "retrieval_invocation_id": "around-2",
        "output": copy.deepcopy(second["rendered_output"]),
    })
    bodies["renderers"].append(second_renderer)

    events = bodies["action_trace"]["events"]
    second_call = trial_fixture.event(
        "event-7",
        7,
        "tool_invocation",
        payload=trial_fixture.absent(
            "not_applicable", "arguments are captured separately"),
        action_id="action-around-2",
        tool_surface="agrep around",
        trigger="agent_decision_after_prompt",
        status="proposed",
        arguments=copy.deepcopy(second["structured_args"]),
        observation_event_ids=["event-1", "event-2", "event-4", "event-6"],
    )
    second_result = trial_fixture.event(
        "event-8",
        8,
        "tool_result",
        action_id="action-around-2",
        parent_event_id="event-7",
        tool_surface="agrep around",
        arguments=trial_fixture.absent(
            "not_applicable", "result event has no arguments"),
        status="executed_completed",
        payload=copy.deepcopy(second["rendered_output"]),
        exit_code=0,
        elapsed_ms=9,
        observation_event_ids=["event-1", "event-2", "event-4",
                               "event-6", "event-7"],
    )
    final = copy.deepcopy(events[-1])
    final.update({
        "event_id": "event-9",
        "observation_event_ids": [
            "event-1", "event-2", "event-4", "event-6", "event-8"],
        "source": trial_fixture.source("record-9"),
    })
    _set_event_sequence(final, 9)
    bodies["action_trace"]["events"] = events[:-1] + [
        second_call, second_result, final]
    bodies["action_trace"]["completeness"] = trial_fixture.complete(9)
    return bodies


class SabelUsagePolicyMatrixTests(unittest.TestCase):
    def _evaluate(self, bodies: dict) -> dict:
        bundle = bundle_fixture.seal_bundle(bodies)
        sabel_shadow.validate_trial_bundle(bundle)
        evaluation = sabel_shadow.evaluate_trial_policy(bundle)
        sabel_shadow.validate_policy_evaluation(evaluation)
        return evaluation["outcomes"]

    def test_complete_postcompact_zero_recall_is_a_confirmed_miss(self):
        outcomes = self._evaluate(_without_any_retrieval())
        self.assertEqual(outcomes["recall_count"], 0)
        self.assertEqual(outcomes["invocation"], "required_missed")

    def test_partial_postcompact_zero_recall_remains_observation_incomplete(self):
        outcomes = self._evaluate(_without_any_retrieval(partial=True))
        self.assertEqual(outcomes["recall_count"], 0)
        self.assertEqual(outcomes["invocation"], "observation_incomplete")
        self.assertEqual(outcomes["opening"], "observation_incomplete")

    def test_recall_under_explicit_opt_out_is_scored_as_a_violation(self):
        bodies = bundle_fixture.bundle_bodies()
        bodies["annotation"]["history_requirement"] = "explicit_opt_out"
        outcomes = self._evaluate(bodies)
        self.assertEqual(outcomes["invocation"], "explicit_opt_out_violated")

    def test_unnecessary_midflow_recall_is_scored_as_unnecessary(self):
        bodies = bundle_fixture.bundle_bodies()
        bodies["trial"]["context_condition"] = "mid_flow"
        bodies["annotation"]["history_requirement"] = "unnecessary"
        outcomes = self._evaluate(bodies)
        self.assertEqual(outcomes["invocation"], "unnecessary_invocation")

    def test_required_recall_after_another_tool_violates_recall_first(self):
        outcomes = self._evaluate(_with_non_retrieval_tool_before_recall())
        self.assertFalse(outcomes["recall_first"])
        self.assertEqual(outcomes["invocation"], "recall_first_violated")

    def test_two_observed_around_opens_are_scoreable_as_too_many(self):
        outcomes = self._evaluate(_with_second_around())
        self.assertEqual(outcomes["around_count"], 2)
        self.assertEqual(outcomes["opening"], "too_many_opens")

    def test_rendered_open_warranted_preview_without_open_is_missed(self):
        outcomes = self._evaluate(_recall_without_around(rendered=True))
        self.assertEqual(outcomes["around_count"], 0)
        self.assertEqual(outcomes["opening"], "missed_warranted_preview")

    def test_dropped_raw_open_warranted_candidate_is_not_a_missed_preview(self):
        outcomes = self._evaluate(_recall_without_around(rendered=False))
        self.assertEqual(outcomes["around_count"], 0)
        self.assertEqual(outcomes["opening"], "within_policy")


if __name__ == "__main__":
    unittest.main()
