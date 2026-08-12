"""Live-only session ids round-trip through `agrep resume`."""

from __future__ import annotations

import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "py"))

from _test_support import isolate_data_dir  # noqa: E402

isolate_data_dir()
import resume  # noqa: E402


def _row(session: str, *, agent: str = "claude") -> dict:
    return {
        "agent": agent,
        "first_text": "task",
        "last_ts": 1,
        "project": "agrep",
        "session": session,
    }


class ResumeLiveTests(unittest.TestCase):
    def _run(self, argv: list[str], rows: list[dict], live):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(resume, "_sessions", return_value=rows), \
                mock.patch.object(resume, "_live_match", return_value=live) as lookup, \
                mock.patch.object(
                    resume.native, "resume_in_place", return_value=0) as launch, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = resume.main(argv)
        return rc, out.getvalue(), err.getvalue(), lookup, launch

    def test_indexed_exact_match_wins_without_live_lookup(self) -> None:
        row = _row("indexed-full-id")
        rc, _out, _err, lookup, launch = self._run(
            ["indexed-full-id"], [row], ([], True))
        self.assertEqual(rc, 0)
        lookup.assert_not_called()
        launch.assert_called_once_with("claude", "indexed-full-id")

    def test_complete_live_exact_match_resumes_before_publication(self) -> None:
        live = _row("branch-full-id")
        rc, out, err, lookup, launch = self._run(
            ["branch-full-id"], [_row("older")], ([live], True))
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, "")
        lookup.assert_called_once_with("branch-full-id")
        launch.assert_called_once_with("claude", "branch-full-id")

    def test_partial_live_lookup_is_unverified(self) -> None:
        rc, out, err, _lookup, launch = self._run(
            ["branch-full-id"], [_row("older")], ([], False))
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertEqual(err.count("live session lookup is incomplete"), 1)
        self.assertNotIn("recent ones", err)
        launch.assert_not_called()

    def test_complete_live_miss_keeps_the_indexed_recent_list(self) -> None:
        rc, out, err, _lookup, launch = self._run(
            ["missing-full-id"], [_row("older")], ([], True))
        self.assertEqual(rc, 1)
        self.assertIn("older", out)
        self.assertIn("no session matches", err)
        launch.assert_not_called()

    def test_empty_index_can_still_resume_an_exact_live_session(self) -> None:
        live = _row("branch-full-id")
        rc, out, err, _lookup, launch = self._run(
            ["@branch-full-id"], [], ([live], True))
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, "")
        launch.assert_called_once_with("claude", "branch-full-id")


if __name__ == "__main__":
    unittest.main()
