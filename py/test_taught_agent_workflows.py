"""Behavioral proofs for commands taught to coding agents."""

from __future__ import annotations

import contextlib
import io
import os
import re
import shlex
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "py"))

from _test_support import isolate_data_dir  # noqa: E402

isolate_data_dir()
import around  # noqa: E402
import cli  # noqa: E402
import compact  # noqa: E402
import explore  # noqa: E402
import recall  # noqa: E402
import search  # noqa: E402
import surface_policy as surface  # noqa: E402


class TaughtAgentWorkflows(unittest.TestCase):
    SESSION = "0199aaaa-0000-7000-8000-000000000001"
    ARTIFACT_QUERY = "late pipeline goal"
    ARTIFACT_TEXT = (
        "LATE_PIPELINE_GOAL.md defines the exact publication stages and gates."
    )
    ARTIFACT_REPLY = "Use PIPELINE_STAGES.md; the verifier approved stages one to four."

    @staticmethod
    def _result(hits: list[dict]) -> dict:
        sessions = {str(hit["session"]) for hit in hits}
        return {
            "hits": [dict(hit) for hit in hits],
            "total": len(hits),
            "chats": len(sessions),
            "phrase_chats": len(sessions),
            "tool_hits": sum(hit.get("who") == "tool" for hit in hits),
            "engine": "corpusdb",
            "mode": "keyword",
            "totals_exact": True,
        }

    @classmethod
    def _artifact_hit(cls) -> dict:
        return {
            "session": cls.SESSION,
            "turn": 17,
            "ts": 1_700_000_000_000,
            "who": "user",
            "agent": "claude",
            "project": "pipeline",
            "concept": "",
            "title": "",
            "score": 9.0,
            "matched": "phrase",
            "snippet": cls.ARTIFACT_TEXT,
            "content_digest": compact.content_digest(cls.ARTIFACT_TEXT),
        }

    @classmethod
    def _window(cls, turn: int = 17, *, text: str | None = None,
                reply: str | None = None) -> dict:
        return {
            "session": cls.SESSION,
            "center": turn,
            "first_turn": turn,
            "last_turn": turn,
            "agent": "claude",
            "project": "pipeline",
            "concept": "",
            "title": "",
            "events": [],
            "turns": [{
                "turn": turn,
                "who": "user",
                "ts": 1_700_000_000_000 + turn,
                "text": cls.ARTIFACT_TEXT if text is None else text,
                "reply": cls.ARTIFACT_REPLY if reply is None else reply,
            }],
        }

    @staticmethod
    def _run_cli(argv: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["agrep", *argv]), \
                mock.patch.object(
                    cli.legacy_cleanup, "retire_removed_explorer",
                    return_value=None), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            rc = cli._main()
        return rc, stdout.getvalue(), stderr.getvalue()

    def _recall_fixture(self, query_hits) -> ExitStack:
        stack = ExitStack()
        stack.enter_context(mock.patch.object(
            recall.indexd_runtime, "ensure_index", return_value=True))
        stack.enter_context(mock.patch.object(
            recall.indexd_runtime, "agent_freshness_notice", return_value=""))
        stack.enter_context(mock.patch.object(
            recall.indexd_runtime, "freshness_story",
            return_value=surface.FreshnessStory("current")))
        stack.enter_context(mock.patch.object(
            recall.search, "_semantic_runtime_installed", return_value=False))
        stack.enter_context(mock.patch.object(
            recall.search, "run_query", side_effect=query_hits))
        stack.enter_context(mock.patch.object(
            recall.explore, "resolve_session", return_value=[self.SESSION]))
        stack.enter_context(mock.patch.object(
            recall.explore, "get_window", return_value=self._window()))
        stack.enter_context(mock.patch.object(
            recall.explore, "get_windows",
            side_effect=lambda requests: [
                self._window(int(turn)) for _session, turn, _radius in requests
            ]))
        stack.enter_context(mock.patch.object(
            recall.explore, "_session_index",
            return_value={self.SESSION: {}}))
        stack.enter_context(mock.patch.object(
            recall.common, "indexed_session_prefix_candidates",
            return_value=[self.SESSION]))
        stack.enter_context(mock.patch.object(
            recall, "_expand", side_effect=lambda pairs, *args, **kwargs: pairs))
        return stack

    def test_missing_artifact_probe_pull_and_around_handle_execute(self) -> None:
        hit = self._artifact_hit()

        def query(_query: str, **kwargs) -> dict:
            if kwargs.get("who") == "tool":
                return self._result([])
            return self._result([hit])

        with self._recall_fixture(query):
            rc, probe, probe_err = self._run_cli([
                "recall", self.ARTIFACT_QUERY, "--probe", "--lexical",
                "--color", "never",
            ])
            self.assertEqual(rc, 0, probe_err)
            pull = probe.rsplit("pull: ", 1)[1].strip()
            pull_argv = shlex.split(pull)
            self.assertEqual(pull_argv[:2], ["agrep", "recall"])

            rc, recalled, recall_err = self._run_cli(pull_argv[1:])
            self.assertEqual(rc, 0, recall_err)
            self.assertIn(self.ARTIFACT_TEXT, recalled)
            self.assertIn(self.ARTIFACT_REPLY, recalled)

            handle = re.search(r"@[A-Za-z0-9._-]+:\d+\.[0-9a-f]{4}", probe)
            self.assertIsNotNone(handle, probe)
            around_argv = ["around", handle.group(0), "--full"]
            rc, replayed, around_err = self._run_cli(around_argv)
            self.assertEqual(rc, 0, around_err)
            self.assertIn(self.ARTIFACT_TEXT, replayed)
            self.assertIn(self.ARTIFACT_REPLY, replayed)

    def test_compact_answer_flat_tool_rows_and_explicit_exclusion_execute(self) -> None:
        answer_text = "Spectrum S2 is the chosen design system for the application."
        hits = [{
            "session": self.SESSION,
            "turn": 7,
            "ts": 2_000,
            "who": "agent",
            "agent": "claude",
            "project": "spectrum",
            "score": 10.0,
            "matched": "phrase",
            "snippet": answer_text,
            "content_digest": compact.content_digest(answer_text),
        }]
        for index in range(10):
            text = f"tool-{index}: spectrum import inventory row {index}"
            hits.append({
                "session": f"tool-session-{index}",
                "turn": index,
                "ts": 1_000 - index,
                "who": "tool",
                "agent": "claude",
                "project": "spectrum",
                "score": 1.0 - index / 100,
                "matched": "phrase",
                "snippet": text,
                "content_digest": compact.content_digest(text),
            })

        def query(_query: str, **kwargs) -> dict:
            who_filter = kwargs.get("who")
            selected = [
                hit for hit in hits
                if surface.speaker_filter_admits(who_filter, str(hit["who"]))
            ]
            return self._result(selected)

        session_index = {str(hit["session"]): {} for hit in hits}
        with mock.patch.dict(os.environ, {"AGREP_PROFILE": "compact"}), \
                mock.patch.object(
                    search.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(
                    search.indexd_runtime, "agent_freshness_notice", return_value=""), \
                mock.patch.object(search, "run_query", side_effect=query), \
                mock.patch.object(
                    search.common, "transcript_generation",
                    return_value={"generation": 1}), \
                mock.patch.object(
                    search.common, "indexed_session_prefix_candidates",
                    return_value=sorted(session_index)), \
                mock.patch.object(
                    explore, "_session_index", return_value=session_index):
            rc, compact_out, compact_err = self._run_cli([
                "spectrum", "--lexical", "--color", "never",
            ])
            self.assertEqual(rc, 0, compact_err)
            self.assertIn("Spectrum S2 is the chosen design system", compact_out)
            self.assertNotIn("\t", compact_out)
            self.assertLessEqual(
                sum("tool-session" in line for line in compact_out.splitlines()), 2)

            rc, flat_out, flat_err = self._run_cli([
                "spectrum", "--flat", "-n", "0", "--lexical",
                "--color", "never",
            ])
            self.assertEqual(rc, 0, flat_err)
            flat_rows = [line.split("\t") for line in flat_out.splitlines()]
            self.assertEqual(len(flat_rows), 11)
            self.assertEqual(sum(row[4] == "tool" for row in flat_rows), 10)

            rc, prose_out, prose_err = self._run_cli([
                "spectrum", "--flat", "-n", "0", "--no-who", "tool",
                "--lexical", "--color", "never",
            ])
            self.assertEqual(rc, 0, prose_err)
            prose_rows = [line.split("\t") for line in prose_out.splitlines()]
            self.assertEqual(len(prose_rows), 1)
            self.assertEqual(prose_rows[0][4], "agent")
            self.assertIn(answer_text, prose_out)

    def test_compaction_keeps_old_verdict_and_self_rescues_delegated_result(
            self) -> None:
        verdict_text = "VERIFIER_VERDICT: do not push until the snapshot fix lands."
        delegated_text = "DELEGATED_RESULT: the snapshot fix passed its adversarial replay."
        family = recall.common.CallingFamily(
            self.SESSION, self.SESSION, frozenset({self.SESSION}), True, 10)
        policy = recall.common.SelfExclusion(family, 10, "window")

        def hit(turn: int, text: str) -> dict:
            return {
                "session": self.SESSION,
                "turn": turn,
                "ts": 1_700_000_000_000 + turn,
                "who": "agent",
                "agent": "codex",
                "project": "agrep",
                "score": 8.0,
                "matched": "phrase",
                "snippet": text,
                "content_digest": compact.content_digest(text),
            }

        verdict = hit(4, verdict_text)
        delegated = hit(12, delegated_text)

        def query(text: str, **kwargs) -> dict:
            if kwargs.get("who") == "tool":
                return self._result([])
            selected = delegated if "delegated" in text else verdict
            boundary = kwargs.get("exclude_session_from_turn")
            excluded = (
                kwargs.get("exclude_session") == self.SESSION
                and (boundary is None or int(selected["turn"]) >= int(boundary))
            )
            return self._result([] if excluded else [selected])

        def windows(requests) -> list[dict]:
            out = []
            for _session, turn, _radius in requests:
                text = delegated_text if int(turn) == 12 else verdict_text
                out.append(self._window(int(turn), text=text, reply=""))
            return out

        with self._recall_fixture(query), \
                mock.patch.object(
                    recall.common, "in_agent_context", return_value=True), \
                mock.patch.object(
                    recall.common, "calling_self_exclusion", return_value=policy), \
                mock.patch.object(
                    recall.common, "indexed_self_exclusion_has_rows",
                    return_value=True), \
                mock.patch.object(recall.explore, "get_windows", side_effect=windows):
            rc, old_out, old_err = self._run_cli([
                "recall", "verifier verdict", "--lexical", "--hits", "1",
                "-C", "0", "--color", "never",
            ])
            self.assertEqual(rc, 0, old_err)
            self.assertIn(verdict_text, old_out)
            self.assertIn("~self", old_out)

            rc, hidden_out, hidden_err = self._run_cli([
                "recall", "delegated result", "--probe", "--lexical",
                "--color", "never",
            ])
            self.assertEqual(rc, 1, hidden_out + hidden_err)
            self.assertNotIn(delegated_text, hidden_out)
            self.assertIn("excluded 1 hit from the current window", hidden_err)

            rc, rescued_out, rescued_err = self._run_cli([
                "recall", "delegated result", "--probe", "--self",
                "--lexical", "--color", "never",
            ])
            self.assertEqual(rc, 0, rescued_err)
            self.assertIn("@0199aaaa:12", rescued_out)


if __name__ == "__main__":
    unittest.main()
