"""Explicit -n compact-page and terse continuation contracts."""

from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "py"))

from _test_support import isolate_data_dir  # noqa: E402

isolate_data_dir()
import compact  # noqa: E402
import search  # noqa: E402


def _records(count: int, *, family: str = "fam", line_bytes: int = 60):
    body = "x" * max(1, line_bytes - 20)
    return [
        {"hit": {"session": family, "turn": index, "score": 2.0,
                 "family_root": family, "matched": "phrase"},
         "line": f"@{family}:{index} {body}"}
        for index in range(count)
    ]


class ComposePageExplicitRows(unittest.TestCase):
    def test_default_page_keeps_the_family_diversity_cap(self) -> None:
        indices, reason = compact._compose_page(_records(10), 100_000)
        self.assertEqual(len(indices), compact.FAMILY_PAGE_CAP)
        self.assertEqual(reason, "diversity")

    def test_explicit_request_waives_the_family_cap(self) -> None:
        indices, reason = compact._compose_page(
            _records(10), 100_000, requested_rows=10)
        self.assertEqual(len(indices), 10)
        self.assertEqual(reason, "exhausted")

    def test_explicit_request_stops_at_the_frozen_limit(self) -> None:
        indices, _reason = compact._compose_page(
            _records(60), 1_000_000, requested_rows=60)
        self.assertLessEqual(len(indices), compact.MAX_FROZEN_HITS)

    def test_byte_budget_still_binds_and_is_named(self) -> None:
        indices, reason = compact._compose_page(
            _records(40, line_bytes=400), 2_000, requested_rows=40)
        self.assertLess(len(indices), 40)
        self.assertGreaterEqual(len(indices), compact.MIN_PAGE_HITS)
        self.assertEqual(reason, "byte-budget")

    def test_explicit_request_never_duplicates_a_turn(self) -> None:
        records = _records(4) + _records(4)  # identical (session, turn) pairs
        indices, _reason = compact._compose_page(
            records, 100_000, requested_rows=8)
        self.assertEqual(len(indices), 4)


class ShortfallDisclosure(unittest.TestCase):
    def _summary(self, page) -> str:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            search._compact_summary(page)
        return stderr.getvalue()

    def _page(self, shown: int, requested: int | None, *,
              more: bool, stopped_by: str = "byte-budget"):
        return compact.CompactPage(
            tuple({"hit": {}, "line": f"row {index}"}
                  for index in range(shown)),
            more, "m.testtest" if more else None, stopped_by,
            shown + (1 if more else 0), query="q",
            requested_rows=requested)

    def test_shortfall_emits_one_copyable_continuation(self) -> None:
        out = self._summary(self._page(27, 40, more=True))
        self.assertEqual(
            out,
            "28+ matches (floor; -c exact) · more: "
            "agrep --more m.testtest\n")

    def test_met_request_renders_no_shortfall_clause(self) -> None:
        out = self._summary(self._page(5, 5, more=True))
        self.assertNotIn("requested", out)

    def test_exhausted_corpus_renders_no_shortfall_clause(self) -> None:
        # fewer hits than requested with nothing withheld: not a shortfall
        out = self._summary(self._page(3, 40, more=False))
        self.assertNotIn("requested", out)

    def test_default_pages_never_mention_requests(self) -> None:
        out = self._summary(self._page(3, None, more=True))
        self.assertNotIn("requested", out)


class EndToEndExplicitPage(unittest.TestCase):
    def _hits(self, count: int):
        return [
            {"session": "0199aaaa-0000-7000-8000-000000000001", "turn": index,
             "ts": 1000 + index, "who": "user", "agent": "codex",
             "project": "proj", "score": 2.0, "matched": "phrase",
             "snippet": f"needle row {index}",
             "content_digest": compact.content_digest(f"needle row {index}")}
            for index in range(count)]

    def test_single_chat_page_honors_the_explicit_request(self) -> None:
        page = search._start_compact_page(
            self._hits(12), "needle", None, corpus_more=False,
            requested_rows=12)
        self.assertEqual(len(page.records), 12)

    def test_single_chat_page_defaults_to_diversity(self) -> None:
        page = search._start_compact_page(
            self._hits(12), "needle", None, corpus_more=False)
        self.assertEqual(len(page.records), compact.FAMILY_PAGE_CAP)
        self.assertTrue(page.more)


if __name__ == "__main__":
    unittest.main()
