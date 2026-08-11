"""Reply ingest-cap provenance remains visible in context output."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from _test_support import isolate_data_dir  # noqa: E402

isolate_data_dir()
import around  # noqa: E402
import common  # noqa: E402
import explore  # noqa: E402
import recall  # noqa: E402


SESSION = "reply-session-000000000000000000000001"
WINDOW = {
    "session": SESSION, "agent": "codex", "project": "agrep",
    "concept": "", "title": "", "center": 0, "first_turn": 0,
    "last_turn": 0, "turns": [{"turn": 0, "who": "user", "ts": 1,
                                 "text": "question", "reply": "indexed excerpt…",
                                 "reply_chars": 90_000,
                                 "reply_truncated": True}],
    "events": [],
}


class ReplyTruncationTests(unittest.TestCase):
    def test_human_event_lines_label_input_and_subagent_result_loss(self):
        tool = {"kind": "tool", "name": "Bash", "input": "x" * 800,
                "input_chars": 100_000, "input_truncated": True,
                "output": "", "output_chars": 0, "ok": True}
        child = {"kind": "subagent_result", "name": "Task", "input": "",
                 "input_chars": 0, "output": "result" * 100,
                 "output_chars": 90_000, "output_truncated": True, "ok": True}
        self.assertIn("source input truncated at ingest; 100,000 chars",
                      around._tool_line(tool, False, 0))
        self.assertIn("source result truncated at ingest; 90,000 chars",
                      around._tool_line(child, False, 0))

    def test_window_joins_reply_sidecar_provenance(self):
        context = {"agent": "codex", "project": "agrep", "n_turns": 1,
                   "first_turn": 0, "last_turn": 0,
                   "timeline": [{"turn": 0, "ts": 1}]}
        rows = [{"turn": 0, "ts": 1, "agent": "codex", "project": "agrep",
                 "who": "user", "text": "question", "reply": "excerpt…"}]
        record = {f"codex:{SESSION}:0": {"reply": "excerpt…",
                                          "reply_chars": 90_000,
                                          "reply_truncated": True}}
        with mock.patch.object(explore, "_reply_records_by_id", return_value=record), \
                mock.patch.object(explore, "_summary_by_session", return_value={}), \
                mock.patch.object(explore, "_session_concept", return_value={}), \
                mock.patch.object(explore, "has_events", return_value=False):
            window = explore._pack_window(SESSION, context, 0, rows)
        turn = window["turns"][0]
        self.assertEqual(turn["reply_chars"], 90_000)
        self.assertTrue(turn["reply_truncated"])

    def test_around_full_labels_loss_and_json_carries_metadata(self):
        patches = (
            mock.patch.object(common, "MESSAGES_PATH", Path(__file__)),
            mock.patch.object(around.explore, "resolve_session", return_value=[SESSION]),
            mock.patch.object(around.explore, "get_window", return_value=WINDOW),
        )
        with patches[0], patches[1], patches[2]:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(around.main([SESSION, "0", "--full", "--color", "never"]), 0)
            self.assertIn("source truncated at ingest; original reply 90,000 chars",
                          out.getvalue())

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(around.main([SESSION, "0", "--json"]), 0)
        rows = [json.loads(line) for line in out.getvalue().splitlines()]
        agent = next(row for row in rows if row.get("who") == "agent")
        self.assertEqual(agent["reply_chars"], 90_000)
        self.assertTrue(agent["reply_truncated"])

    def test_recall_labels_loss_in_text_and_json(self):
        result = {"hits": [{"session": SESSION, "turn": 0, "ts": 1,
                             "who": "user", "agent": "codex", "project": "agrep",
                             "score": 1.0, "matched": "phrase"}],
                  "total": 1, "chats": 1, "engine": "corpusdb",
                  "totals_exact": True}

        def run(argv: list[str]) -> str:
            out = io.StringIO()
            with mock.patch.object(recall.indexd_runtime, "ensure_index", return_value=True), \
                    mock.patch.object(recall.search, "run_query", return_value=result), \
                    mock.patch.object(recall.explore, "get_windows", return_value=[WINDOW]), \
                    contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(recall.main(argv), 0)
            return out.getvalue()

        text = run(["question", "--budget", "0"])
        self.assertIn("source truncated at ingest; original reply 90,000 chars", text)
        payload = json.loads(run(["question", "--budget", "0", "--json"]))
        agent = next(row for row in payload["hits"][0]["window"]
                     if row.get("who") == "agent")
        self.assertEqual(agent["reply_chars"], 90_000)
        self.assertTrue(agent["reply_truncated"])


if __name__ == "__main__":
    unittest.main()
