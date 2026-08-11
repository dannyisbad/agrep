"""Structural scoring contracts for raw provider agent traces."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "bench" / "agentic_trace_score.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "agrep_agentic_trace_score", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class AgenticTraceScoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.scorer = _load_module()

    @staticmethod
    def _event(second: int, payload: dict, *, kind: str = "response_item") -> dict:
        return {
            "timestamp": f"2026-08-05T12:00:{second:02d}.000Z",
            "type": kind,
            "payload": payload,
        }

    def _user(self, second: int, prompt: str) -> dict:
        return self._event(second, {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": prompt}],
        })

    def _final(self, second: int, text: str = "done") -> dict:
        return self._event(second, {
            "type": "message", "role": "assistant", "phase": "final_answer",
            "content": [{"type": "output_text", "text": text}],
        })

    def _exec(self, second: int, call_id: str, command: str, *,
              single_quotes: bool = False) -> dict:
        if single_quotes:
            source = (
                "const r=await tools.exec_command({cmd:'" + command.replace(
                    "'", "\\'") +
                "',workdir:'/workspace',yield_time_ms:10000});text(r.output)\n")
        else:
            source = (
                "const r = await tools.exec_command(" + json.dumps({
                    "cmd": command, "workdir": "/workspace",
                    "yield_time_ms": 10000,
                }, separators=(",", ":")) + "); text(r.output)\n")
        return self._event(second, {
            "type": "custom_tool_call", "call_id": call_id,
            "name": "exec", "input": source,
        })

    def _direct_exec(self, second: int, call_id: str, command: str) -> dict:
        return self._event(second, {
            "type": "function_call", "call_id": call_id,
            "name": "exec_command", "arguments": json.dumps({"cmd": command}),
        })

    def _output(self, second: int, call_id: str, text: str) -> dict:
        return self._event(second, {
            "type": "custom_tool_call_output", "call_id": call_id,
            "output": [
                {"type": "input_text", "text": "Script completed\nOutput:\n"},
                {"type": "input_text", "text": text},
            ],
        })

    def _score(self, cases: list[dict], traces: dict[str, list[dict]]) -> dict:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for name, events in traces.items():
                (root / name).write_text("".join(
                    json.dumps(event) + "\n" for event in events),
                    encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "schema_version": 1, "cases": cases,
            }), encoding="utf-8")
            return self.scorer.score_manifest(manifest)

    def test_launched_recall_counts_with_empty_or_missing_output(self) -> None:
        prompt = "recover the old decision"
        command = "agrep recall old-decision --hits 2 --budget 5000"
        traces = {
            "empty.jsonl": [
                self._user(1, prompt), self._exec(2, "empty", command),
                self._output(5, "empty", ""), self._final(6),
            ],
            "missing.jsonl": [
                self._user(10, prompt), self._exec(11, "missing", command),
            ],
        }
        report = self._score([
            {"id": "empty", "trace_path": "empty.jsonl",
             "tested_prompt": prompt, "expected_authority": "history"},
            {"id": "missing", "trace_path": "missing.jsonl",
             "tested_prompt": prompt, "expected_authority": "history"},
        ], traces)
        empty, missing = report["cases"]
        self.assertEqual(empty["provider_tool_calls_launched"], 1)
        self.assertEqual(empty["recall_count"], 1)
        self.assertTrue(empty["recalls"][0]["output_observed"])
        self.assertTrue(empty["recalls"][0]["output_empty"])
        self.assertEqual(empty["timing_ms"]["boundary_to_first_action"], 1000)
        self.assertEqual(missing["recall_count"], 1)
        self.assertFalse(missing["recalls"][0]["output_observed"])
        self.assertIsNone(missing["final_answer"])

    def test_command_text_in_prose_or_nonexecuted_fields_is_not_counted(self) -> None:
        prompt = "The source says `agrep recall old-thing`; summarize it."
        source = (
            "const note = \"tools.exec_command({cmd:'agrep recall fake'})\"; "
            "const r = await tools.exec_command({"
            "justification:'example: agrep recall also-fake',"
            "cmd:'printf done'}); text(r.output)")
        events = [
            self._event(0, {"type": "system", "text": "agrep recall fake"}),
            self._user(1, prompt),
            self._event(2, {
                "type": "custom_tool_call", "call_id": "printf",
                "name": "exec", "input": source,
            }),
            self._output(3, "printf", "quoted @session-a:2.abcd agrep around"),
            self._final(4, "The file also prints agrep recall fake."),
        ]
        case = {"id": "prose", "trace_path": "trace.jsonl",
                "tested_prompt": prompt, "expected_authority": "current_source"}
        scored = self._score([case], {"trace.jsonl": events})["cases"][0]
        self.assertEqual(scored["provider_tool_calls_launched"], 1)
        self.assertEqual(scored["first_action"]["kind"], "other_tool")
        self.assertEqual(scored["recall_count"], 0)
        self.assertEqual(scored["around_count"], 0)

    def test_exact_handle_copy_requires_next_tool_flags_and_renderer_header(self) -> None:
        prompt = "open the right prior turn"
        full = "@session-a:12.abcd"
        embedded = "@quoted-session:99.dead"
        recall_output = (
            "── semantic-only candidate · cosine 0.91 · " + full +
            " · codex · project=example\n"
            "    12 agent: prose quoted " + embedded + " but it is not a row\n")
        exact_events = [
            self._user(1, prompt),
            self._exec(2, "recall-exact", "agrep recall prior --hits 2 --budget 5000"),
            self._output(3, "recall-exact", recall_output),
            self._exec(4, "around-exact",
                       f"agrep around {full} --full --no-tools",
                       single_quotes=True),
            self._final(5),
        ]
        short_events = [
            self._user(10, prompt),
            self._direct_exec(11, "recall-short",
                              "agrep recall prior --hits 2 --budget 5000"),
            self._output(12, "recall-short", recall_output),
            self._direct_exec(13, "around-short",
                              "agrep around @session-a:12 --full --no-tools"),
            self._final(14),
        ]
        cases = [
            {"id": "exact", "trace_path": "exact.jsonl",
             "tested_prompt": prompt, "expected_authority": "history"},
            {"id": "short", "trace_path": "short.jsonl",
             "tested_prompt": prompt, "expected_authority": "history"},
        ]
        exact, short = self._score(cases, {
            "exact.jsonl": exact_events, "short.jsonl": short_events,
        })["cases"]
        self.assertEqual(exact["recalls"][0]["returned_full_handles"], [full])
        self.assertEqual(exact["handle_copy_statuses"], ["exact"])
        self.assertEqual(exact["exact_immediate_full_handle_copies"], 1)
        self.assertEqual(short["handle_copy_statuses"], ["shortened"])
        self.assertEqual(short["exact_immediate_full_handle_copies"], 0)

    def test_pre_boundary_calls_and_outputs_are_filtered(self) -> None:
        prompt = "everything needed is visible"
        events = [
            self._exec(0, "old", "agrep recall older --hits 2 --budget 5000"),
            self._output(1, "old", "── @old-session:1.abcd · codex\n"),
            self._user(2, prompt), self._final(3, "yes"),
        ]
        case = {"id": "boundary", "trace_path": "trace.jsonl",
                "tested_prompt": prompt, "expected_authority": "visible_context"}
        scored = self._score([case], {"trace.jsonl": events})["cases"][0]
        self.assertEqual(scored["provider_tool_calls_launched"], 0)
        self.assertEqual(scored["first_action"]["kind"], "final_answer")
        self.assertEqual(scored["recall_count"], 0)

    def test_next_provider_user_message_ends_scored_turn(self) -> None:
        prompt = "answer from visible context"
        later_prompt = "now recover history"
        events = [
            self._user(1, prompt), self._final(2, "visible answer"),
            self._user(3, later_prompt),
            self._exec(4, "later", "agrep recall later --hits 2 --budget 5000"),
            self._final(5, "later answer"),
        ]
        case = {"id": "turn-window", "trace_path": "trace.jsonl",
                "tested_prompt": prompt, "expected_authority": "visible_context"}
        scored = self._score([case], {"trace.jsonl": events})["cases"][0]
        self.assertEqual(scored["provider_tool_calls_launched"], 0)
        self.assertEqual(scored["recall_count"], 0)
        self.assertEqual(scored["final_answer"]["text"], "visible answer")

    def test_tool_output_may_not_straddle_a_user_boundary(self) -> None:
        prompt = "run current check"
        events = [
            self._user(1, prompt),
            self._direct_exec(2, "crossing", "git status --short"),
            self._user(3, "new turn"),
            self._output(4, "crossing", "late output"),
            self._final(5),
        ]
        case = {"id": "straddle", "trace_path": "trace.jsonl",
                "tested_prompt": prompt, "expected_authority": "current_source"}
        with self.assertRaisesRegex(
                self.scorer.TraceScoreError, "straddles a user boundary"):
            self._score([case], {"trace.jsonl": events})

    def test_malformed_full_handle_and_legacy_recovery_are_still_counted(self) -> None:
        prompt = "recover history"
        full = "@session-a:12.abcd"
        events = [
            self._user(1, prompt),
            self._exec(2, "recall", "agrep recall prior --hits 2 --budget 5000"),
            self._output(3, "recall", f"── {full} · codex · project=example\n"),
            self._exec(4, "bad", "agrep around session-a:12.abcd --full --no-tools"),
            self._output(5, "bad", "turn must be an integer"),
            self._exec(6, "recovery", "agrep around session-a 12 --full --no-tools"),
            self._final(7),
        ]
        case = {"id": "recovery", "trace_path": "trace.jsonl",
                "tested_prompt": prompt, "expected_authority": "history"}
        scored = self._score([case], {"trace.jsonl": events})["cases"][0]
        self.assertEqual(scored["around_count"], 2)
        self.assertEqual(scored["extra_around_count"], 1)
        self.assertEqual(scored["handle_copy_statuses"], ["full_handle_missing_at"])
        self.assertEqual(
            [item["around_target_status"] for item in scored["arounds"]],
            ["full_handle_missing_at", "legacy_session_turn"])

    def test_session_only_around_attempt_is_counted_not_rejected(self) -> None:
        prompt = "recover history"
        full = "@session-a:12.abcd"
        events = [
            self._user(1, prompt),
            self._exec(2, "recall", "agrep recall prior --hits 2 --budget 5000"),
            self._output(3, "recall", f"── {full} · codex · project=example\n"),
            self._exec(4, "session", "agrep around session-a --full --no-tools"),
            self._output(5, "session", "turn is required"),
            self._exec(6, "legacy", "agrep around session-a 12 --full --no-tools"),
            self._final(7),
        ]
        case = {"id": "session", "trace_path": "trace.jsonl",
                "tested_prompt": prompt, "expected_authority": "history"}
        scored = self._score([case], {"trace.jsonl": events})["cases"][0]
        self.assertEqual(scored["around_count"], 2)
        self.assertEqual(scored["handle_copy_statuses"], ["session_only"])
        self.assertEqual(scored["arounds"][0]["around_target_status"], "session_only")

    def test_unrelated_multiline_shell_is_other_tool_but_agrep_is_ambiguous(self) -> None:
        prompt = "inspect current files"
        unrelated = self._direct_exec(2, "multiline", "pwd\ngit status --short\nfind .")
        case = {"id": "multiline", "trace_path": "trace.jsonl",
                "tested_prompt": prompt, "expected_authority": "current_source"}
        scored = self._score([case], {"trace.jsonl": [
            self._user(1, prompt), unrelated, self._final(3),
        ]})["cases"][0]
        self.assertEqual(scored["provider_tool_calls_launched"], 1)
        self.assertEqual(scored["first_action"]["kind"], "other_tool")
        self.assertEqual(scored["recall_count"], 0)

        ambiguous = self._direct_exec(2, "ambiguous", "pwd\nagrep recall maybe\ngit status")
        with self.assertRaisesRegex(
                self.scorer.TraceScoreError, "may invoke agrep ambiguously"):
            self._score([case], {"trace.jsonl": [
                self._user(1, prompt), ambiguous, self._final(3),
            ]})

    def test_environment_prefixes_still_classify_literal_recall(self) -> None:
        prompt = "recover history"
        cases = [
            {"id": "assignment", "trace_path": "assignment.jsonl",
             "tested_prompt": prompt, "expected_authority": "history"},
            {"id": "env", "trace_path": "env.jsonl",
             "tested_prompt": prompt, "expected_authority": "history"},
            {"id": "command", "trace_path": "command.jsonl",
             "tested_prompt": prompt, "expected_authority": "history"},
        ]
        traces = {
            "assignment.jsonl": [
                self._user(1, prompt), self._direct_exec(
                    2, "assignment", "AGREP_PROFILE=compact agrep recall prior"),
                self._final(3),
            ],
            "env.jsonl": [
                self._user(4, prompt), self._direct_exec(
                    5, "env", "env AGREP_PROFILE=compact agrep recall prior"),
                self._final(6),
            ],
            "command.jsonl": [
                self._user(7, prompt), self._direct_exec(
                    8, "command", "command agrep recall prior"),
                self._final(9),
            ],
        }
        assignment, env, command = self._score(cases, traces)["cases"]
        self.assertEqual(assignment["recall_count"], 1)
        self.assertEqual(env["recall_count"], 1)
        self.assertEqual(command["recall_count"], 1)

    def test_malformed_copy_status_must_correspond_to_returned_handle(self) -> None:
        prompt = "recover history"
        events = [
            self._user(1, prompt),
            self._exec(2, "recall", "agrep recall prior --hits 2 --budget 5000"),
            self._output(3, "recall", "── @session-a:12.abcd · codex\n"),
            self._exec(4, "wrong", "agrep around session-b:99.dead --full --no-tools"),
            self._final(5),
        ]
        case = {"id": "wrong-copy", "trace_path": "trace.jsonl",
                "tested_prompt": prompt, "expected_authority": "history"}
        scored = self._score([case], {"trace.jsonl": events})["cases"][0]
        self.assertEqual(scored["handle_copy_statuses"], ["different_handle"])

    def test_agrep_cannot_share_nested_output_authority(self) -> None:
        prompt = "recover history"
        source = (
            "const a=await tools.exec_command({cmd:'agrep recall prior'}); "
            "const b=await tools.exec_command({cmd:'git status --short'}); "
            "text(a.output); text(b.output)")
        call = self._event(2, {
            "type": "custom_tool_call", "call_id": "mixed",
            "name": "exec", "input": source,
        })
        case = {"id": "mixed", "trace_path": "trace.jsonl",
                "tested_prompt": prompt, "expected_authority": "history"}
        with self.assertRaisesRegex(
                self.scorer.TraceScoreError, "share one output authority"):
            self._score([case], {"trace.jsonl": [
                self._user(1, prompt), call, self._final(3),
            ]})

    def test_report_binds_exact_trace_bytes_and_rejects_direct_symlink(self) -> None:
        prompt = "visible"
        events = [self._user(1, prompt), self._final(2)]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            trace = root / "trace.jsonl"
            body = "".join(json.dumps(event) + "\n" for event in events)
            # bytes, not text: Windows CRLF translation would break the digest
            trace.write_bytes(body.encode("utf-8"))
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "schema_version": 1,
                "cases": [{"id": "hash", "trace_path": "trace.jsonl",
                           "tested_prompt": prompt,
                           "expected_authority": "visible_context"}],
            }), encoding="utf-8")
            scored = self.scorer.score_manifest(manifest)["cases"][0]
            self.assertEqual(
                scored["trace_sha256"], hashlib.sha256(body.encode()).hexdigest())
            self.assertEqual(scored["trace_size_bytes"], len(body.encode()))

            link = root / "trace-link.jsonl"
            link.symlink_to(trace)
            manifest.write_text(json.dumps({
                "schema_version": 1,
                "cases": [{"id": "link", "trace_path": "trace-link.jsonl",
                           "tested_prompt": prompt,
                           "expected_authority": "visible_context"}],
            }), encoding="utf-8")
            with self.assertRaisesRegex(
                    self.scorer.TraceScoreError, "may not be a symlink"):
                self.scorer.score_manifest(manifest)

    def test_template_and_regex_command_prose_are_not_exec_calls(self) -> None:
        prompt = "inspect current data"
        source = (
            "const pattern=/tools.exec_command({cmd:'agrep recall fake'})/; "
            "const note=`tools.exec_command({cmd:'agrep recall fake'}) ${pattern}`; "
            "text(note)")
        call = self._event(2, {
            "type": "custom_tool_call", "call_id": "js-prose",
            "name": "exec", "input": source,
        })
        case = {"id": "js-prose", "trace_path": "trace.jsonl",
                "tested_prompt": prompt, "expected_authority": "current_source"}
        scored = self._score([case], {"trace.jsonl": [
            self._user(1, prompt), call, self._final(3),
        ]})["cases"][0]
        self.assertEqual(scored["provider_tool_calls_launched"], 1)
        self.assertEqual(scored["first_action"]["kind"], "other_tool")
        self.assertEqual(scored["recall_count"], 0)

    def test_prompt_hash_selects_the_exact_boundary(self) -> None:
        prompt = "hash-bound prompt"
        digest = hashlib.sha256(prompt.encode()).hexdigest()
        events = [self._user(1, prompt), self._final(2)]
        case = {"id": "hash", "trace_path": "trace.jsonl",
                "tested_prompt_sha256": digest,
                "expected_authority": "visible_context"}
        scored = self._score([case], {"trace.jsonl": events})["cases"][0]
        self.assertEqual(scored["user_boundary"]["prompt_sha256"], digest)

    def test_duplicate_call_ids_dedupe_but_conflicts_fail_closed(self) -> None:
        prompt = "find history"
        call = self._exec(2, "same", "agrep recall thing --hits 2 --budget 5000")
        duplicate = json.loads(json.dumps(call))
        duplicate["timestamp"] = "2026-08-05T12:00:03.000Z"
        events = [self._user(1, prompt), call, duplicate, self._final(4)]
        case = {"id": "dedupe", "trace_path": "trace.jsonl",
                "tested_prompt": prompt, "expected_authority": "history"}
        scored = self._score([case], {"trace.jsonl": events})["cases"][0]
        self.assertEqual(scored["provider_tool_calls_launched"], 1)
        self.assertEqual(scored["recall_count"], 1)

        conflicting = json.loads(json.dumps(duplicate))
        conflicting["payload"]["input"] = (
            "const r=await tools.exec_command({cmd:'printf different'});")
        with self.assertRaisesRegex(
                self.scorer.TraceScoreError, "conflicting duplicate call_id"):
            self._score([case], {"trace.jsonl": events + [conflicting]})

    def test_ambiguous_boundary_and_missed_command_parser_fail_closed(self) -> None:
        prompt = "same prompt"
        duplicate_boundary = [
            self._user(1, prompt), self._user(2, prompt), self._final(3),
        ]
        case = {"id": "ambiguous", "trace_path": "trace.jsonl",
                "tested_prompt": prompt, "expected_authority": "visible_context"}
        with self.assertRaisesRegex(
                self.scorer.TraceScoreError, "expected one exact user boundary"):
            self._score([case], {"trace.jsonl": duplicate_boundary})

        null_call = self._event(2, {
            "type": "custom_tool_call", "call_id": "null",
            "name": "exec_command", "input": None,
        })
        with self.assertRaisesRegex(
                self.scorer.TraceScoreError, "arguments are not an object"):
            self._score([case], {"trace.jsonl": [
                self._user(1, prompt), null_call, self._final(3),
            ]})

        malformed_nested = self._event(2, {
            "type": "custom_tool_call", "call_id": "malformed",
            "name": "exec",
            "input": "const r=await tools.exec_command({cmd:'agrep recall x'",
        })
        with self.assertRaisesRegex(
                self.scorer.TraceScoreError, "unterminated"):
            self._score([case], {"trace.jsonl": [
                self._user(1, prompt), malformed_nested, self._final(3),
            ]})

    def test_every_agrep_surface_and_path_spelling_is_counted(self) -> None:
        prompt = "do not use history"
        commands = {
            "search": "agrep search old-decision",
            "bare": "agrep old-decision",
            "chats": "agrep chats connector",
            "board": "agrep board --once",
            "path": "/opt/unsealed/bin/agrep recall old --hits 2 --budget 5000",
        }
        expected_kinds = {
            "search": "agrep_search",
            "bare": "agrep_bare",
            "chats": "agrep_chats",
            "board": "agrep_board",
            "path": "recall",
        }
        cases = []
        traces = {}
        for offset, (case_id, command) in enumerate(commands.items()):
            trace_name = f"{case_id}.jsonl"
            cases.append({
                "id": case_id,
                "trace_path": trace_name,
                "tested_prompt": prompt,
                "expected_authority": "history",
            })
            second = offset * 3 + 1
            traces[trace_name] = [
                self._user(second, prompt),
                self._direct_exec(second + 1, case_id, command),
                self._final(second + 2),
            ]
        report = self._score(cases, traces)
        for scored in report["cases"]:
            self.assertEqual(scored["total_agrep_count"], 1)
            self.assertEqual(len(scored["agrep_invocations"]), 1)
            self.assertEqual(
                scored["agrep_invocations"][0]["kind"],
                expected_kinds[scored["id"]],
            )
            self.assertEqual(scored["first_action"]["kind"], expected_kinds[scored["id"]])
            self.assertEqual(scored["observed_authority"], "history")
            self.assertEqual(
                scored["first_action"]["literal_commands"],
                [commands[scored["id"]]],
            )
        self.assertEqual(report["summary"]["total_agrep_calls"], len(commands))

    def test_semantic_outage_is_bound_to_recall_renderer_diagnostic(self) -> None:
        prompt = "recover the old incident"
        handle = "@session-a:12.abcd"
        healthy_events = [
            self._user(1, prompt),
            self._exec(2, "healthy-recall",
                       "agrep recall incident --hits 2 --budget 5000"),
            self._output(3, "healthy-recall", (
                f"── {handle} · codex · project=example\n"
                "    12 agent: the old log said meaning unavailable; keyword-only\n")),
            self._exec(4, "healthy-around",
                       f"agrep around {handle} --full --no-tools"),
            self._output(5, "healthy-around",
                         "meaning unavailable was the historical incident"),
            self._final(6),
        ]
        unavailable_events = [
            self._user(10, prompt),
            self._exec(11, "unavailable-recall",
                       "agrep recall incident --hits 2 --budget 5000"),
            self._output(12, "unavailable-recall",
                         "meaning unavailable; keyword-only\nno semantic rows"),
            self._final(13),
        ]
        cases = [
            {"id": "healthy", "trace_path": "healthy.jsonl",
             "tested_prompt": prompt, "expected_authority": "history"},
            {"id": "unavailable", "trace_path": "unavailable.jsonl",
             "tested_prompt": prompt, "expected_authority": "history"},
        ]
        healthy, unavailable = self._score(cases, {
            "healthy.jsonl": healthy_events,
            "unavailable.jsonl": unavailable_events,
        })["cases"]
        self.assertEqual(
            healthy["recalls"][0]["semantic_renderer_status"],
            "no_unavailability_diagnostic")
        self.assertEqual(
            unavailable["recalls"][0]["semantic_renderer_status"],
            "product_semantic_unavailable")


if __name__ == "__main__":
    unittest.main()
