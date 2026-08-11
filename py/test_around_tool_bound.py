"""Classic around bounds pathological tool tails without weakening handles."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "py"))

from _test_support import isolate_data_dir  # noqa: E402

isolate_data_dir()
import around  # noqa: E402
import common  # noqa: E402
import compact  # noqa: E402
import explore  # noqa: E402


SESSION = "agent-acde1234567890"
TURN = 7


def _event(ts: int, *, input_text: str = "same request",
           output: str = "same failure", ok: bool = False) -> dict:
    return {
        "kind": "tool", "turn": TURN, "ts": ts,
        "name": "exec_command", "input": input_text, "output": output,
        "ok": ok, "input_chars": len(input_text),
        "output_chars": len(output), "output_bytes": len(output.encode()),
        "input_truncated": False, "output_truncated": False,
    }


def _window(events: list[dict]) -> dict:
    return {
        "session": SESSION, "agent": "codex", "project": "agrep",
        "concept": "", "title": "", "center": TURN,
        "first_turn": 0, "last_turn": TURN,
        "turns": [{"turn": TURN, "ts": 1, "who": "user",
                   "text": "inspect the failed run", "reply": "root cause"}],
        "events": events,
    }


def _handle(event: dict, needle: str) -> str:
    searchable = common.tool_search_text(event)
    start = searchable.index(needle)
    identity = common.tool_event_identity(
        SESSION, TURN, event["ts"], searchable)
    return compact.encode_bound_result_handle({
        "session": SESSION, "turn": TURN, "ts": event["ts"],
        "who": "tool", "_event_identity": identity,
        "_match_span": (start, start + len(needle)),
    }, text=searchable)


def _legacy_digest_handle(event: dict) -> str:
    """Pre-event-suffix tool handle: address plus verified content digest."""
    return compact.encode_result_handle(
        {"session": SESSION, "turn": TURN},
        text=common.tool_search_text(event))


class AroundToolBoundTests(unittest.TestCase):
    def setUp(self) -> None:
        data = tempfile.TemporaryDirectory()
        self.addCleanup(data.cleanup)
        messages = Path(data.name) / "messages.jsonl"
        path_patch = mock.patch.object(common, "MESSAGES_PATH", messages)
        path_patch.start()
        self.addCleanup(path_patch.stop)
        common.MESSAGES_PATH.touch()
        self.events: list[dict] = []
        patches = (
            mock.patch.object(around.indexd_runtime, "ensure_index",
                              lambda auto=True, **_kw: True),
            mock.patch.object(explore, "resolve_session",
                              lambda value: [SESSION]
                              if SESSION.startswith(value) else []),
            mock.patch.object(explore, "get_window",
                              lambda *_args: _window(self.events)),
            mock.patch.object(explore, "_session_index",
                              lambda: {SESSION: {}}),
            mock.patch.object(
                around.session_context, "indexed_family_roots",
                lambda sessions: {str(session): str(session)
                                  for session in sessions}),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = around.main(argv)
        return rc, stdout.getvalue(), stderr.getvalue()

    def test_selected_tool_default_keeps_only_the_cited_event_and_root_pair(
            self) -> None:
        self.events = [_event(index) for index in range(2050)]
        self.events[1000] = _event(
            1000, input_text="one-off command", output="SOLE_ROOT_CAUSE")
        self.events[1024] = _event(
            1024, input_text="selected command",
            output="prefix SELECTED_NEEDLE decisive suffix")
        handle = _handle(self.events[1024], "SELECTED_NEEDLE")

        rc, out, err = self._run([handle, "--no-auto", "--color", "never"])

        self.assertEqual((rc, err), (0, ""))
        self.assertLess(len(out.encode()), 4096)
        self.assertIn("SELECTED_NEEDLE", out)
        self.assertNotIn("SOLE_ROOT_CAUSE", out)
        self.assertEqual(out.count("FAILED exec_command"), 1)
        self.assertIn("2,049 unselected tool/workflow events hidden", out)
        self.assertIn("-C 0 --full", out)


    def test_digest_only_tool_handle_recovers_only_its_verified_event(
            self) -> None:
        selected = _event(
            11, input_text="legacy selected command",
            output="LEGACY_SELECTED_EVIDENCE")
        self.events = [
            _event(10, input_text="ambient before", output="AMBIENT_BEFORE"),
            selected,
            _event(12, input_text="ambient after", output="AMBIENT_AFTER"),
        ]
        handle = _legacy_digest_handle(selected)
        self.assertNotIn("~", handle)

        rc, classic, err = self._run(
            [handle, "--no-auto", "--color", "never"])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("LEGACY_SELECTED_EVIDENCE", classic)
        self.assertNotIn("AMBIENT_BEFORE", classic)
        self.assertNotIn("AMBIENT_AFTER", classic)
        self.assertEqual(classic.count("FAILED exec_command"), 1)

        rc, payload, err = self._run([handle, "--json", "--no-auto"])
        self.assertEqual((rc, err), (0, ""))
        tool_rows = [
            row for row in map(json.loads, payload.splitlines())
            if row.get("kind") == "tool"
        ]
        self.assertEqual([row["ts"] for row in tool_rows], [selected["ts"]])

    def test_digest_handle_centers_input_and_output_in_classic_and_json(
            self) -> None:
        cases = (
            _event(1, input_text="prefix INPUT_NEEDLE suffix", output="done"),
            _event(2, input_text="run", output=("prefix " * 30)
                   + "OUTPUT_NEEDLE" + (" suffix" * 30)),
        )
        for event, needle in zip(cases, ("INPUT_NEEDLE", "OUTPUT_NEEDLE")):
            with self.subTest(needle=needle):
                self.events = [_event(0), event, _event(3)]
                handle = _handle(event, needle)

                rc, classic, err = self._run(
                    [handle, "--no-auto", "--color", "never"])
                self.assertEqual((rc, err), (0, ""))
                self.assertIn("exec_command", classic)
                self.assertIn(needle, classic)

                with mock.patch.object(
                        around, "_tool_block",
                        side_effect=AssertionError("JSON entered classic renderer")):
                    rc, payload, err = self._run(
                        [handle, "--json", "--no-auto"])
                self.assertEqual((rc, err), (0, ""))
                rows = [json.loads(line) for line in payload.splitlines()]
                selected, = [row for row in rows
                             if row.get("kind") == "tool"
                             and row.get("ts") == event["ts"]]
                self.assertIn(needle, selected["match_preview"])

    def test_selected_tool_default_does_not_sample_unselected_failure_tail(
            self) -> None:
        self.events = [
            _event(index, input_text=f"command {index}",
                   output=f"distinct error {index}")
            for index in range(2050)
        ]
        self.events[1024] = _event(
            1024, input_text="selected command",
            output="prefix SELECTED_DISTINCT_NEEDLE suffix")
        handle = _handle(self.events[1024], "SELECTED_DISTINCT_NEEDLE")

        rc, out, err = self._run([handle, "--no-auto", "--color", "never"])

        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out.count("FAILED exec_command"), 1)
        self.assertIn("SELECTED_DISTINCT_NEEDLE", out)
        self.assertNotIn("distinct error 0", out)
        self.assertNotIn("distinct error 2049", out)
        self.assertIn("2,049 unselected tool/workflow events hidden", out)

    def test_no_tools_refuses_to_erase_a_selected_tool_handle(self) -> None:
        event = _event(1, output="SELECTED_ONLY_EVIDENCE")
        self.events = [event]
        handle = _handle(event, "SELECTED_ONLY_EVIDENCE")

        rc, out, err = self._run([handle, "--no-tools", "--no-auto"])

        self.assertEqual((rc, out), (2, ""))
        self.assertIn("--no-tools conflicts with this tool result handle", err)

    def test_full_and_tool_output_keep_every_event_detail(self) -> None:
        self.events = [_event(index, input_text=f"request {index}")
                       for index in range(25)]
        for options in (("--full",), ("--tool-output", "1")):
            with self.subTest(options=options):
                rc, out, err = self._run([
                    SESSION, str(TURN), *options,
                    "--no-auto", "--color", "never"])
                self.assertEqual((rc, err), (0, ""))
                self.assertEqual(out.count("FAILED exec_command"), 25)
                self.assertNotIn("collapsed", out)


if __name__ == "__main__":
    unittest.main()
