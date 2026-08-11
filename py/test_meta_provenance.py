"""Structural command lineage stays exact and never suppresses sole evidence."""

from __future__ import annotations

import unittest
from unittest import mock

import common
import display_policy
import explore
import recall
import search


def _event(command: str, *, turn: int = 3, ts: int = 30,
           output: str = "") -> dict:
    return {
        "turn": turn, "ts": ts, "kind": "tool", "name": "exec_command",
        "input": command, "input_chars": len(command),
        "input_truncated": False, "output": output,
        "output_chars": len(output), "output_truncated": False, "ok": True,
    }


class HistoryReadParsing(unittest.TestCase):
    def assert_read(self, command: str, query: str = "diagPing6") -> None:
        self.assertTrue(display_policy.history_read_invoked(
            _event(command), query), command)

    def assert_not_read(self, command: str, query: str = "diagPing6") -> None:
        self.assertFalse(display_policy.history_read_invoked(
            _event(command), query), command)

    def test_direct_installed_source_and_windows_commands(self) -> None:
        self.assert_read("agrep recall diagPing6 --no-auto")
        self.assert_read(r".\agrep.cmd search diagPing6 --lexical")
        self.assert_read(".venv/bin/python cli.py recall diagPing6")
        self.assert_read("agrep --lexical --json -n 80 -- diagPing6")

    def test_structured_wrappers_and_argv_forms(self) -> None:
        wrapped = (
            'const r = await tools.exec_command({cmd:"agrep recall '
            'diagPing6 --no-auto",workdir:"/tmp"});')
        self.assert_read(wrapped)
        raw = '["agrep", "recall", "diagPing6"]'
        self.assert_read(raw)
        windows = '["C:\\\\Tools\\\\agrep.cmd", "recall", "diagPing6"]'
        self.assert_read(windows)
        escaped = '{"argv":"[\\"agrep\\",\\"search\\",\\"diagPing6\\"]"}'
        self.assert_read(escaped)
        source = (
            "python -c 'import subprocess,sys; "
            "subprocess.run([sys.executable,\"cli.py\",\"recall\","
            "\"diagPing6\"])'")
        self.assert_read(source)

    def test_prose_paths_and_non_read_verbs_fail_open(self) -> None:
        self.assert_not_read('rg -n "agrep recall diagPing6" docs')
        self.assert_not_read('python -c \'print("agrep recall diagPing6")\'')
        self.assert_not_read("printf 'tools.exec_command({cmd:\"agrep recall "
                             "diagPing6\"})'")
        self.assert_not_read("agrep index diagPing6")
        self.assert_not_read("agrep status diagPing6")
        self.assert_not_read("cli.py recall diagPing6")
        self.assert_not_read("notcli.py recall diagPing6")
        self.assert_not_read("cat > README.md <<EOF\nagrep recall diagPing6\nEOF")
        self.assert_not_read("true || agrep recall diagPing6")
        self.assert_not_read("false && agrep recall diagPing6")
        self.assert_not_read("true # && agrep recall diagPing6")
        self.assert_not_read("command -v agrep")
        self.assert_not_read("command -V agrep recall diagPing6")
        self.assert_not_read("time --version agrep recall diagPing6")
        self.assert_not_read("env --help agrep recall diagPing6")
        self.assert_not_read("...agrep recall diagPing6")
        self.assert_not_read(
            'const r=await tools.exec_command({justification:"example '
            'cmd: \'agrep recall diagPing6\' only",cmd:"printf done"});')

    def test_python_control_flow_is_not_executed_lineage(self) -> None:
        self.assert_not_read(
            "python -c 'if False: subprocess.run([\"agrep\",\"recall\","
            "\"diagPing6\"])'")
        self.assert_not_read(
            "python -c 'False and subprocess.run([\"agrep\",\"recall\","
            "\"diagPing6\"])'")

    def test_conflicting_structured_authorities_fail_open(self) -> None:
        self.assert_not_read(
            '{"cmd":"agrep recall diagPing6","argv":["printf","done"]}')

    def test_malformed_or_partial_events_fail_open(self) -> None:
        event = _event("agrep recall diagPing6")
        event["input_truncated"] = True
        self.assertFalse(display_policy.history_read_invoked(event, "diagPing6"))
        event = _event("agrep recall diagPing6")
        event["input_chars"] += 1
        self.assertFalse(display_policy.history_read_invoked(event, "diagPing6"))
        event = _event("agrep recall diagPing6\x00hidden")
        self.assertFalse(display_policy.history_read_invoked(event, "diagPing6"))
        event = _event("agrep recall diagPing6\ud800")
        self.assertFalse(display_policy.history_read_invoked(event, "diagPing6"))
        event = _event("agrep recall diagPing6")
        event["name"] = "read"
        self.assertFalse(display_policy.history_read_invoked(event, "diagPing6"))

    def test_tool_event_recognizes_queryless_history_reads(self) -> None:
        for command in ("agrep chats -n 3", "agrep around @abc:4", "agrep board"):
            with self.subTest(command=command):
                self.assertTrue(display_policy.history_read_invocation(
                    _event(command)))
        self.assertFalse(display_policy.history_read_invocation(
            _event("agrep index")))


class HistoryMetaMarking(unittest.TestCase):
    SESSION = "session-child"

    @staticmethod
    def _hit(who: str, turn: int, *, query: int = 0, **extra) -> dict:
        return {
            "session": HistoryMetaMarking.SESSION, "turn": turn,
            "ts": turn * 10, "who": who, "agent": "codex",
            "project": "/work/agrep", "snippet": "needle", "score": 1.0,
            "matched": "phrase", "_recall_lane": 0,
            "_recall_query": query, **extra,
        }

    @staticmethod
    def _windows(events: list[dict]):
        def load(requests):
            return [{
                "session": session, "center": turn, "turns": [],
                "events": list(events), "agent": "codex", "project": "agrep",
                "first_turn": 0, "last_turn": 20,
            } for session, turn, _radius in requests]
        return load

    def test_agent_requires_same_query_within_four_prior_turns(self) -> None:
        causal = self._hit("agent", 7)
        old = self._hit("agent", 8)
        future = self._hit("agent", 2)
        wrong = self._hit("agent", 7, query=1)
        direct = self._hit("agent", 7, _direct_handle=True)
        events = [
            _event("agrep recall needle", turn=3),
            _event("agrep recall other", turn=6),
        ]
        with mock.patch.object(
                search, "_family_roots_for_hits",
                return_value={self.SESSION: "root-session"}), \
                mock.patch.object(
                    explore, "get_windows", side_effect=self._windows(events)):
            search._mark_history_meta(
                [causal, old, future, wrong, direct],
                ["needle", "different"])
        self.assertTrue(causal.get("_meta_row"))
        self.assertIsNone(old.get("_meta_row"))
        self.assertIsNone(future.get("_meta_row"))
        self.assertIsNone(wrong.get("_meta_row"))
        self.assertIsNone(direct.get("_meta_row"))
        self.assertEqual(display_policy.row_origin(causal), "sidechain")

    def test_tool_identity_marks_the_invocation_not_its_query(self) -> None:
        invocation = _event(
            "agrep recall diagPing6", turn=5, ts=55,
            output="output-only evidence")
        other = _event("printf unrelated", turn=5, ts=56)
        row = common.tool_row_from_event(invocation, 55, 5)
        identity = common.tool_event_identity(
            self.SESSION, 5, 55, row["text"] if row else None)
        selected = self._hit(
            "tool", 5, query=1, _event_identity=identity)
        different = self._hit(
            "tool", 5, query=1, _event_identity="0" * 24)
        with mock.patch.object(
                search, "_family_roots_for_hits",
                return_value={self.SESSION: self.SESSION}), \
                mock.patch.object(
                    explore, "get_windows",
                    side_effect=self._windows([invocation, other])):
            search._mark_history_meta(
                [selected, different], ["needle", "output-only evidence"])
        self.assertTrue(selected.get("_meta_row"))
        self.assertIsNone(different.get("_meta_row"))

    def test_failed_history_read_marks_tool_but_not_later_answer(self) -> None:
        invocation = _event("agrep recall needle", turn=4, ts=44)
        invocation["ok"] = False
        row = common.tool_row_from_event(invocation, 44, 4)
        identity = common.tool_event_identity(
            self.SESSION, 4, 44, row["text"] if row else None)
        tool = self._hit("tool", 4, _event_identity=identity)
        answer = self._hit("agent", 5)
        with mock.patch.object(
                search, "_family_roots_for_hits",
                return_value={self.SESSION: self.SESSION}), \
                mock.patch.object(
                    explore, "get_windows",
                    side_effect=self._windows([invocation])):
            search._mark_history_meta([tool, answer], ["needle"])
        self.assertTrue(tool.get("_meta_row"))
        self.assertIsNone(answer.get("_meta_row"))
        self.assertTrue(display_policy.history_read_invocation(invocation))
        self.assertFalse(display_policy.history_read_invoked(
            invocation, "needle"))

    def test_filter_retains_one_best_row_only_for_meta_only_queries(self) -> None:
        first = self._hit("agent", 1, _meta_row=True)
        second = self._hit("agent", 2, _meta_row=True)
        lived = self._hit("user", 3, query=1)
        echoed = self._hit("agent", 4, query=1, _meta_row=True)
        kept, dropped, retained = search._filter_meta_rows(
            [first, second, lived, echoed])
        self.assertEqual(kept, [first, lived])
        self.assertEqual((dropped, retained), (2, 1))

    def test_filter_classifies_every_row_in_a_larger_page(self) -> None:
        hits = [self._hit("agent", turn) for turn in range(1, 42)]
        events = [_event("agrep recall needle", turn=turn)
                  for turn in range(1, 42)]
        with mock.patch.object(
                search, "_family_roots_for_hits",
                return_value={self.SESSION: self.SESSION}), \
                mock.patch.object(
                    explore, "get_windows",
                    side_effect=self._windows(events)):
            search._mark_history_meta(hits, ["needle"])
        kept, dropped, retained = search._filter_meta_rows(hits)
        self.assertEqual(kept, [hits[0]])
        self.assertEqual((dropped, retained), (40, 1))

    def test_recall_order_is_stable_when_all_rows_are_meta(self) -> None:
        high = self._hit("agent", 1, _meta_row=True, score=0.9)
        low = self._hit("agent", 2, _meta_row=True, score=0.2)
        self.assertEqual(sorted([low, high], key=recall._merge_key), [high, low])
        lived = self._hit("user", 3, score=0.01)
        self.assertEqual(
            sorted([high, lived], key=recall._merge_key), [lived, high])

    def test_meta_and_origin_are_independent_axes(self) -> None:
        lived = self._hit("agent", 1, _meta_row=True)
        side = self._hit("agent", 2, _meta_row=True, _sidechain=True)
        self.assertEqual(display_policy.row_origin(lived), "lived")
        self.assertEqual(display_policy.row_origin(side), "sidechain")


if __name__ == "__main__":
    unittest.main()
