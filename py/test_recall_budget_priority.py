"""Finite recall pages reserve the selected exchange before tool narration."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "py"))

from _test_support import isolate_data_dir  # noqa: E402

isolate_data_dir()
import recall  # noqa: E402


class RecallBudgetPriorityTests(unittest.TestCase):
    def test_fit_is_linear_on_a_large_window(self) -> None:
        # Old fitter re-encoded the whole block per drop (4.3 GB for a 3 KB
        # block); the 60-row --lexical perf gate can't catch that quadratic.
        import time
        n = 6_000
        records = [{
            "line": f"     ⚙ exec noise-{index}-" + "x" * (index % 40),
            "required": False, "kind": "tool",
            "drop_key": (index, 0, index),
        } for index in range(n)]

        def reference(head: str, records_: list[dict],
                      limit: int, expand: str) -> tuple[str, int]:
            keep = set(range(len(records_)))
            dropped_tools = 0
            required = [i for i, r in enumerate(records_) if r["required"]]
            marker = lambda: (  # noqa: E731
                f"       [+{dropped_tools} tool calls - {expand}]"
                if dropped_tools else "")
            def render() -> str:  # noqa: B023 -- re-reads keep/marker each call
                lines = [head, *(r["line"] for i, r in enumerate(records_)
                                 if i in keep)]
                tail = marker()
                if tail:
                    lines.append(tail)
                return "\n".join(lines)
            for index in sorted(
                    (i for i, r in enumerate(records_) if not r["required"]),
                    key=lambda i: records_[i]["drop_key"]):
                if len(render().encode("utf-8", errors="replace")) <= limit:
                    break
                keep.remove(index)
                dropped_tools += 1
            return render(), dropped_tools

        old = reference("── @w:1", [dict(r) for r in records],
                        3_328, "agrep around w 1")
        start = time.monotonic()
        new = recall._fit_recall_records(
            "── @w:1", [dict(r) for r in records], 3_328, "agrep around w 1")
        elapsed = time.monotonic() - start
        self.assertEqual(new, old)
        self.assertEqual(len(new[0]), len(old[0]))
        self.assertLess(elapsed, 1.0)
    SESSIONS = (
        "0199aaaa-0000-7000-8000-000000000001",
        "0199bbbb-0000-7000-8000-000000000002",
    )


    @classmethod
    def _result(cls) -> dict:
        hits = [{
            "session": session, "turn": 1, "ts": 10 - index,
            "who": "user", "agent": "codex", "project": "agrep",
            "score": 10.0 - index, "matched": "phrase",
            "snippet": "diagPing6",
        } for index, session in enumerate(cls.SESSIONS)]
        return {"hits": hits, "total": 2, "chats": 2, "tool_hits": 0,
                "engine": "corpusdb", "mode": "keyword"}

    @classmethod
    def _windows(cls, requests) -> list[dict]:
        windows = []
        for index, (session, turn, _radius) in enumerate(requests):
            if index == 0:
                events = [{
                    "kind": "tool", "turn": 0, "ts": event,
                    "name": "exec_command", "input": f"diagnostic-step-{event}",
                    "output": "routine diagnostic output " * 12,
                    "ok": True, "output_chars": 312, "output_bytes": 312,
                } for event in range(18)]
                turns = [
                    {"turn": 0, "who": "user", "ts": 1,
                     "text": "earlier diagnostic setup " * 24, "reply": ""},
                    {"turn": turn, "who": "user", "ts": 2,
                     "text": "diagPing6 selected request " * 28,
                     "reply": "PAIRED_AGENT_ANSWER decisive resolution " * 28},
                ]
            else:
                events = []
                turns = [{
                    "turn": turn, "who": "user", "ts": 3,
                    "text": "SECOND_HIT_EVIDENCE diagPing6 independent corroboration",
                    "reply": "SECOND_PAIRED_ANSWER",
                }]
            windows.append({
                "session": session, "center": turn, "first_turn": 0,
                "last_turn": turn, "agent": "codex", "project": "agrep",
                "events": events, "turns": turns,
            })
        return windows

    def _run(self) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(
                recall.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(
                    recall.common, "in_agent_context", return_value=False), \
                mock.patch.object(
                    recall.search, "run_query", return_value=self._result()), \
                mock.patch.object(
                    recall.explore, "get_windows", side_effect=self._windows), \
                mock.patch.object(
                    recall.explore, "_session_index",
                    return_value={session: {} for session in self.SESSIONS}), \
                mock.patch.object(
                    recall, "_expand", side_effect=lambda pairs, *a, **k: pairs), \
                mock.patch.object(
                    recall.indexd_runtime, "agent_freshness_notice", return_value=""), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            rc = recall.main([
                "diagPing6", "--hits", "2", "--budget", "5000",
                "--no-auto", "--lexical", "--color", "never"])
        return rc, stdout.getvalue(), stderr.getvalue()

    def _run_hit(self, result: dict, window: dict,
                 argv: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        session = window["session"]
        with mock.patch.object(
                recall.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(
                    recall.common, "in_agent_context", return_value=False), \
                mock.patch.object(recall.search, "run_query", return_value=result), \
                mock.patch.object(
                    recall.explore, "get_windows", return_value=[window]), \
                mock.patch.object(
                    recall.explore, "_session_index", return_value={session: {}}), \
                mock.patch.object(
                    recall, "_expand", side_effect=lambda pairs, *a, **k: pairs), \
                mock.patch.object(
                    recall.indexd_runtime, "agent_freshness_notice", return_value=""), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            rc = recall.main(argv)
        return rc, stdout.getvalue(), stderr.getvalue()

    def test_incidental_tool_previews_stay_behind_bounded_tool_handle(self) -> None:
        rc, out, err = self._run()
        self.assertEqual(rc, 0, err)
        self.assertLessEqual(len(out.encode("utf-8")), 5000)
        self.assertIn("diagPing6 selected request", out)
        self.assertIn("PAIRED_AGENT_ANSWER decisive resolution", out)
        self.assertIn("SECOND_HIT_EVIDENCE", out)
        self.assertIn("SECOND_PAIRED_ANSWER", out)
        self.assertNotIn("exec_command", out)
        marker = next(line for line in out.splitlines()
                      if "+18 tool calls" in line)
        self.assertIn("agrep around", marker)
        self.assertIn("--tool-output 200]", marker)
        self.assertNotIn("--full", marker)

    def test_fixture_bytes_are_deterministic(self) -> None:
        first = self._run()
        second = self._run()
        self.assertEqual(first, second)

    def test_required_multibyte_rows_survive_a_small_block(self) -> None:
        records = [
            {"line": "     ⚙ exec " + "雑音" * 80, "required": False,
             "kind": "tool", "drop_key": (0, 0, 0)},
            {"line": "     1 user: 診断証拠", "required": True,
             "kind": "context", "drop_key": (2, 0, 1)},
            {"line": "     1 agent: 解決回答", "required": True,
             "kind": "context", "drop_key": (2, 0, 2)},
        ]
        rendered, dropped = recall._fit_recall_records(
            "── @fixture:1", records, 160,
            "agrep around fixture 1")
        self.assertLessEqual(len(rendered.encode("utf-8")), 160)
        self.assertEqual(dropped, 1)
        self.assertIn("診断証拠", rendered)
        self.assertIn("解決回答", rendered)

    def test_pack_fit_counts_multibyte_selected_rows_as_utf8_bytes(self) -> None:
        session = self.SESSIONS[0]
        prompt = "needle " + "質問" * 125
        answer = "解決回答 " + "答" * 90
        hit = {
            "session": session, "turn": 1, "ts": 1, "who": "user",
            "agent": "codex", "project": "agrep", "score": 10.0,
            "matched": "phrase", "snippet": "needle",
            "content_digest": recall.compact.content_digest(prompt),
        }
        result = {
            "hits": [hit], "total": 1, "chats": 1, "tool_hits": 0,
            "engine": "corpusdb", "mode": "keyword",
        }
        window = {
            "session": session, "center": 1, "first_turn": 1, "last_turn": 1,
            "agent": "codex", "project": "agrep", "events": [],
            "turns": [{"turn": 1, "who": "user", "ts": 1,
                       "text": prompt, "reply": answer}],
        }

        rc, rendered, error = self._run_hit(
            result, window,
            ["needle", "--hits", "1", "--budget", "900",
             "--no-auto", "--lexical", "--color", "never"])

        self.assertEqual(rc, 0, error)
        self.assertLessEqual(len(rendered.encode("utf-8")), 900)
        self.assertIn("user: needle", rendered)
        self.assertIn("agent: 解決回答", rendered)

    def test_hidden_ambient_events_do_not_force_message_caps(self) -> None:
        session = self.SESSIONS[0]
        tail = "AMBIENT_EVENTS_MUST_NOT_CLIP_THIS_TAIL"
        prompt = "needle " + "x" * 520 + " " + tail
        answer = "paired answer survives"
        events = [{
            "kind": "tool", "turn": 1, "ts": index,
            "name": "exec_command", "input": f"ambient-{index}",
            "output": "hidden output " * 100, "ok": True,
            "input_chars": len(f"ambient-{index}"),
            "output_chars": 1_400, "output_bytes": 1_400,
            "input_truncated": False, "output_truncated": False,
        } for index in range(20)]
        hit = {
            "session": session, "turn": 1, "ts": 1, "who": "user",
            "agent": "codex", "project": "agrep", "score": 10.0,
            "matched": "phrase", "snippet": "needle",
            "content_digest": recall.compact.content_digest(prompt),
        }
        result = {
            "hits": [hit], "total": 1, "chats": 1, "tool_hits": 0,
            "engine": "corpusdb", "mode": "keyword",
        }
        window = {
            "session": session, "center": 1, "first_turn": 1, "last_turn": 1,
            "agent": "codex", "project": "agrep", "events": events,
            "turns": [{"turn": 1, "who": "user", "ts": 1,
                       "text": prompt, "reply": answer}],
        }

        rc, rendered, error = self._run_hit(
            result, window,
            ["needle", "--hits", "1", "--budget", "900",
             "--no-auto", "--lexical", "--color", "never"])

        self.assertEqual(rc, 0, error)
        self.assertLessEqual(len(rendered.encode("utf-8")), 900)
        self.assertIn(tail, rendered)
        self.assertIn("paired answer survives", rendered)
        self.assertNotIn("exec_command", rendered)

    def test_expanded_window_recovery_pointers_cover_actual_radius(self) -> None:
        session = self.SESSIONS[0]
        center = 10
        hit = {
            "session": session, "turn": center, "ts": center, "who": "user",
            "agent": "codex", "project": "agrep", "score": 10.0,
            "matched": "phrase", "snippet": "needle",
            "content_digest": recall.compact.content_digest("needle center"),
        }
        result = {
            "hits": [hit], "total": 1, "chats": 1, "tool_hits": 0,
            "engine": "corpusdb", "mode": "keyword",
        }
        turns = [{
            "turn": turn, "who": "user", "ts": turn,
            "text": ("needle center" if turn == center else "context " * 40),
            "reply": "selected answer" if turn == center else "",
        } for turn in range(1, center + 1)]
        ambient = {
            "kind": "tool", "turn": center, "ts": 99,
            "name": "exec_command", "input": "ambient",
            "output": "hidden", "ok": True,
            "input_chars": 7, "output_chars": 6, "output_bytes": 6,
            "input_truncated": False, "output_truncated": False,
        }
        window = {
            "session": session, "center": center,
            "first_turn": 1, "last_turn": center,
            "agent": "codex", "project": "agrep",
            "events": [ambient], "turns": turns,
        }

        rc, rendered, error = self._run_hit(
            result, window,
            ["needle", "--hits", "1", "--budget", "0",
             "--no-auto", "--lexical", "--color", "never"])
        self.assertEqual(rc, 0, error)
        marker = next(line for line in rendered.splitlines()
                      if "tool calls" in line)
        self.assertIn("-C 9 --tool-output 200", marker)

        captured = {}

        def capture(obj, *_args, **_kwargs):
            captured.update(obj)
            return "{}"

        with mock.patch.object(
                recall, "_fit_json_payload", side_effect=capture):
            rc, _rendered, error = self._run_hit(
                result, window,
                ["needle", "--hits", "1", "--budget", "2048",
                 "--no-auto", "--lexical", "--json"])
        self.assertEqual(rc, 0, error)
        omitted, = [
            row for row in captured["hits"][0]["window"]
            if row.get("kind") == "events_omitted"
        ]
        self.assertIn("-C 9 --tool-output 200", omitted["expand"])
        self.assertNotIn("--full", omitted["expand"])

    def test_minimal_block_keeps_both_selected_sides(self) -> None:
        records = [
            {"line": "     ⚙ exec " + "noise" * 80, "required": False,
             "kind": "tool", "drop_key": (0, 0, 0)},
            {"line": "  1 user: Q", "required": True,
             "kind": "context", "drop_key": (2, 0, 1)},
            {"line": "  1 agent: A", "required": True,
             "kind": "context", "drop_key": (2, 0, 2)},
        ]
        rendered, _dropped = recall._fit_recall_records(
            "── @a:1", records, 96, "agrep around a 1")
        self.assertLessEqual(len(rendered.encode("utf-8")), 96)
        self.assertIn("user: Q", rendered)
        self.assertIn("agent: A", rendered)

    def test_selected_tool_event_survives_before_optional_events(self) -> None:
        session = self.SESSIONS[0]
        events = []
        for index in range(12):
            output = ("prefix " * 20 + "SELECTED_TOOL_NEEDLE resolved" +
                      " suffix" * 20) if index == 6 else "routine output " * 20
            events.append({
                "kind": "tool", "turn": 1, "ts": index,
                "name": "exec_command", "input": f"step-{index}",
                "output": output, "ok": True, "output_chars": len(output),
                "output_bytes": len(output.encode("utf-8")),
            })
        selected = events[6]
        searchable = recall.common.tool_search_text(selected)
        start = searchable.index("SELECTED_TOOL_NEEDLE")
        identity = recall.common.tool_event_identity(
            session, 1, selected["ts"], searchable)
        hit = {
            "session": session, "turn": 1, "ts": 10, "who": "tool",
            "agent": "codex", "project": "agrep", "score": 10.0,
            "matched": "phrase", "snippet": "SELECTED_TOOL_NEEDLE",
            "content_digest": recall.compact.content_digest(searchable),
            "_event_identity": identity,
            "_match_span": (start, start + len("SELECTED_TOOL_NEEDLE")),
        }
        result = {"hits": [hit], "total": 1, "chats": 1, "tool_hits": 1,
                  "engine": "corpusdb", "mode": "keyword"}
        window = {
            "session": session, "center": 1, "first_turn": 1, "last_turn": 1,
            "agent": "codex", "project": "agrep", "events": events,
            "turns": [{"turn": 1, "who": "user", "ts": 1,
                       "text": "selected tool request",
                       "reply": "PAIRED_TOOL_ANSWER"}],
        }
        args = ["selected tool needle", "--hits", "1", "--budget", "900",
                "--no-auto", "--lexical", "--color", "never"]
        rc, rendered, error = self._run_hit(result, window, args)
        self.assertEqual(rc, 0, error)
        self.assertLessEqual(len(rendered.encode("utf-8")), 900)
        self.assertIn("SELECTED_TOOL_NEEDLE", rendered)
        self.assertIn("PAIRED_TOOL_ANSWER", rendered)
        self.assertLess(rendered.count("exec_command"), len(events))
        self.assertEqual(rendered.count("exec_command"), 1)

        args = ["selected tool needle", "--hits", "1", "--budget", "2048",
                "--no-auto", "--lexical", "--json"]
        rc, rendered, error = self._run_hit(result, window, args)
        self.assertEqual(rc, 0, error)
        selected_row = next(
            row for row in json.loads(rendered)["hits"][0]["window"]
            if row.get("ts") == selected["ts"] and row.get("kind") == "tool")
        self.assertNotIn("output", selected_row)
        self.assertIn("SELECTED_TOOL_NEEDLE", selected_row["match_preview"])

    def test_selected_tool_input_match_survives_classic_and_json(self) -> None:
        session = self.SESSIONS[0]
        needle = "DEEP_INPUT_MATCH"
        selected = {
            "kind": "tool", "turn": 1, "ts": 7, "name": "exec_command",
            "input": "prefix " * 60 + needle + " suffix" * 20,
            "output": "routine output", "ok": True,
            "output_chars": 14, "output_bytes": 14,
        }
        events = [dict(selected, ts=index, input=f"step-{index}")
                  for index in range(6)] + [selected]
        searchable = recall.common.tool_search_text(selected)
        start = searchable.index(needle)
        hit = {
            "session": session, "turn": 1, "ts": 7, "who": "tool",
            "agent": "codex", "project": "agrep", "score": 10.0,
            "matched": "phrase", "snippet": needle,
            "content_digest": recall.compact.content_digest(searchable),
            "_event_identity": recall.common.tool_event_identity(
                session, 1, selected["ts"], searchable),
            "_match_span": (start, start + len(needle)),
        }
        result = {"hits": [hit], "total": 1, "chats": 1, "tool_hits": 1,
                  "engine": "corpusdb", "mode": "keyword"}
        window = {
            "session": session, "center": 1, "first_turn": 1, "last_turn": 1,
            "agent": "codex", "project": "agrep", "events": events,
            "turns": [{"turn": 1, "who": "user", "ts": 1,
                       "text": "selected request", "reply": "paired answer"}],
        }
        classic_args = [needle, "--hits", "1", "--budget", "900",
                        "--no-auto", "--lexical", "--color", "never"]
        rc, rendered, error = self._run_hit(result, window, classic_args)
        self.assertEqual(rc, 0, error)
        self.assertIn("exec_command", rendered)
        self.assertIn(needle, rendered)

        json_args = [needle, "--hits", "1", "--budget", "2048",
                     "--no-auto", "--lexical", "--json"]
        rc, rendered, error = self._run_hit(result, window, json_args)
        self.assertEqual(rc, 0, error)
        selected_row = next(
            row for row in json.loads(rendered)["hits"][0]["window"]
            if row.get("ts") == selected["ts"] and row.get("kind") == "tool")
        self.assertIn(needle, selected_row["match_preview"])


    def test_json_budget_keeps_the_selected_prompt_and_answer(self) -> None:
        session = self.SESSIONS[0]
        window = [{
            "kind": "msg", "session": session, "turn": 0, "ts": 1,
            "who": "user", "text": "lead context " * 20,
        }]
        window.extend({
            "kind": "tool", "session": session, "turn": 0,
            "ts": index + 2, "name": "exec_command",
            "input": "optional tool narration " * 8,
        } for index in range(18))
        window.extend(({
            "kind": "msg", "session": session, "turn": 1, "ts": 30,
            "who": "user", "text": "SELECTED_JSON_PROMPT " * 16,
        }, {
            "kind": "msg", "session": session, "turn": 1, "ts": 30,
            "who": "agent", "text": "SELECTED_JSON_ANSWER " * 16,
        }))
        obj = {"query": "selected json", "engine": "fixture", "hits": [{
            "session": session, "turn": 1, "window": window,
        }]}
        rendered = recall._fit_json_payload(obj, recall.MIN_JSON_BUDGET)
        parsed = json.loads(rendered)
        rows = parsed["hits"][0]["window"]
        self.assertLessEqual(
            len(rendered.encode("utf-8")), recall.MIN_JSON_BUDGET)
        self.assertTrue(any(
            row.get("who") == "user" and row.get("turn") == 1
            for row in rows))
        self.assertTrue(any(
            row.get("who") == "agent" and row.get("turn") == 1
            for row in rows))

    def test_json_budget_keeps_the_selected_tool_identity(self) -> None:
        session = self.SESSIONS[0]
        window = [{
            "kind": "tool", "session": session, "turn": 1, "ts": index,
            "name": "exec_command", "input": "tool narration " * 12,
        } for index in range(20)]
        selected = recall._json_tool_row_key(window[7])
        obj = {"query": "selected tool", "engine": "fixture", "hits": [{
            "session": session, "turn": 1, "window": window,
        }]}
        rendered = recall._fit_json_payload(
            obj, recall.MIN_JSON_BUDGET,
            required_tool_rows=[{selected}])
        parsed = json.loads(rendered)
        keys = {
            recall._json_tool_row_key(row)
            for row in parsed["hits"][0]["window"]
        }
        self.assertLessEqual(
            len(rendered.encode("utf-8")), recall.MIN_JSON_BUDGET)
        self.assertIn(selected, keys)


if __name__ == "__main__":
    unittest.main()
