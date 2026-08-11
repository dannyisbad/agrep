"""Focused contracts for the bounded 1-2 character SQLite candidate lane."""

from __future__ import annotations

import sqlite3
import unittest
from unittest import mock

from _test_support import isolate_data_dir

isolate_data_dir()
import corpusdb


NOW_MS = 2_000_000_000_000
DAY_MS = 86_400_000
WHO_WEIGHTS = {"user": 1.0, "subagent": 0.85, "agent": 0.8, "tool": 0.4}
SOURCE_SCALES = {"subagent": 0.85, "recap": 0.65, "control": 0.65,
                 "tool": 0.55}


def _row(session: str, turn: int, ts: int, who: str | None, text: str) -> tuple:
    return (session, turn, ts, "codex", "p", "", "", "", who, text)


class ShortKeywordCandidateTests(unittest.TestCase):
    def test_distinct_counter_stays_bounded_across_high_cardinality_input(self):
        limit = 4_096
        counter = corpusdb._BoundedDistinctCounter(limit)
        for index in range(200_000):
            counter.add(f"session-{index}")
        self.assertEqual(len(counter), limit)
        self.assertFalse(counter.exact)
        counter.add("session-0")
        self.assertEqual(len(counter), limit)

    def _db(self, rows: list[tuple]) -> sqlite3.Connection:
        db = sqlite3.connect(":memory:")
        db.executescript(corpusdb._SCHEMA_SQL)
        db.executemany(corpusdb._INS, rows)
        db.commit()
        return db

    def _lane(self, db: sqlite3.Connection, query: str, flt: dict | None = None,
              *, half_life_days: float = 14.0, user_floor: float = 0.35):
        return corpusdb.bounded_short_keyword_candidates(
            db, query, flt, now_ms=NOW_MS, who_weights=WHO_WEIGHTS,
            source_scales=SOURCE_SCALES,
            recency_half_life_days=half_life_days,
            user_recency_floor=user_floor,
        )

    def _assert_exhaustive_parity(self, db: sqlite3.Connection, query: str,
                                  flt: dict | None = None):
        expected = corpusdb.keyword(
            db, query, 10_000, flt, position_order=False)["hits"]
        lane = self._lane(db, query, flt)
        candidates = list(lane)
        matched = [candidate for candidate in candidates if candidate.matched]
        progress = lane.progress
        self.assertEqual({item.row[0] for item in matched},
                         {hit["session"] for hit in expected})
        self.assertEqual(progress.observed_total, len(expected))
        self.assertEqual(progress.observed_chats,
                         len({hit["session"] for hit in expected}))
        self.assertEqual(progress.observed_tool_hits,
                         sum(hit["who"] == "tool" for hit in expected))
        self.assertGreaterEqual(progress.candidates_examined, len(expected))
        self.assertTrue(progress.exhausted)
        self.assertFalse(progress.stopped)
        self.assertTrue(progress.totals_exact)
        return candidates

    def test_hi_and_a_match_exhaustive_with_nul_and_prose_scope(self):
        rows = [
            _row("hi-user", 1, NOW_MS, "user", "say hi"),
            _row("hi-agent", 1, NOW_MS - 1, "agent", "which route"),
            _row("nul-hit", 1, NOW_MS - 2, "user", "prefix\0hi"),
            _row("nul-miss", 1, NOW_MS - 3, "agent", "prefix\0zzz"),
            _row("tool-hit", 1, NOW_MS - 4, "tool", "hi from a tool"),
            _row("a-user", 1, NOW_MS - 5, "user", "a"),
            _row("null-who", 1, NOW_MS - 6, None, "hi and a"),
            _row("miss", 1, NOW_MS - 7, "user", "zzz"),
        ]
        db = self._db(rows)
        try:
            all_hi = self._assert_exhaustive_parity(db, "hi", {})
            nul_false = next(item for item in all_hi if item.row[0] == "nul-miss")
            self.assertIsNone(nul_false.span)
            self.assertEqual(next(item for item in all_hi
                                  if item.row[0] == "nul-hit").span, (7, 9))
            self._assert_exhaustive_parity(db, "a", {})

            prose_hi = self._assert_exhaustive_parity(
                db, "hi", {"include_tools": False})
            self.assertNotIn("tool-hit", {item.row[0] for item in prose_hi})
            # SQL's canonical prose predicate excludes NULL speakers as well as tools.
            self.assertNotIn("null-who", {item.row[0] for item in prose_hi})
            self._assert_exhaustive_parity(db, "a", {"include_tools": False})
        finally:
            db.close()

    def test_python_re_i_folds_are_confirmed_not_assumed(self):
        rows = [
            _row("ascii-i", 1, NOW_MS, "user", "i"),
            _row("dotted-i", 1, NOW_MS - 1, "agent", "İ"),
            _row("dotless-i", 1, NOW_MS - 2, "agent", "ı"),
            _row("dotted-hi", 1, NOW_MS - 3, "user", "hİ"),
            _row("dotless-hi", 1, NOW_MS - 4, "user", "hı"),
            _row("ascii-k", 1, NOW_MS - 5, "agent", "k"),
            _row("kelvin", 1, NOW_MS - 6, "user", "K"),
            _row("ascii-s", 1, NOW_MS - 7, "agent", "s"),
            _row("long-s", 1, NOW_MS - 8, "user", "ſ"),
        ]
        db = self._db(rows)
        try:
            i_candidates = self._assert_exhaustive_parity(db, "i")
            self.assertIsNone(next(item for item in i_candidates
                                   if item.row[0] == "kelvin").span)
            self.assertIsNone(next(item for item in i_candidates
                                   if item.row[0] == "long-s").span)
            self._assert_exhaustive_parity(db, "hi")
            self._assert_exhaustive_parity(db, "k")
            self._assert_exhaustive_parity(db, "s")
        finally:
            db.close()

    def test_user_floor_is_part_of_monotone_effective_ceiling(self):
        rows = [
            _row("fresh-agent", 1, NOW_MS, "agent", "a"),
            _row("old-user", 1, NOW_MS - 100 * DAY_MS, "user", "a"),
            _row("older-user", 1, NOW_MS - 200 * DAY_MS, "user", "a"),
            _row("fresh-tool", 1, NOW_MS, "tool", "a"),
            _row("stale-agent", 1, NOW_MS - 20 * DAY_MS, "agent", "a"),
        ]
        db = self._db(rows)
        try:
            lane = self._lane(db, "a", half_life_days=1.0, user_floor=0.35)
            candidates = list(lane)
            self.assertEqual([item.row[0] for item in candidates], [
                "fresh-agent", "old-user", "older-user", "fresh-tool", "stale-agent"])
            users = [item for item in candidates if item.row[8] == "user"]
            self.assertEqual([item.score_ceiling for item in users], [0.35, 0.35])
            self.assertGreater(users[-1].score_ceiling,
                               candidates[-1].score_ceiling)
            self.assertTrue(lane.progress.totals_exact)
        finally:
            db.close()

    def test_transcript_boundary_ceiling_never_caps_tool_rows(self):
        rows = [
            _row("transcript", 1, NOW_MS, "user", "xhiz"),
            _row("tool", 1, NOW_MS - 1, "tool", "hi"),
        ]
        db = self._db(rows)
        try:
            lane = corpusdb.bounded_short_keyword_candidates(
                db, "hi", None, now_ms=NOW_MS, who_weights=WHO_WEIGHTS,
                source_scales=SOURCE_SCALES, recency_half_life_days=14.0,
                user_recency_floor=0.35, boundary_ceiling=0.12,
            )
            candidates = list(lane)
            self.assertEqual([item.row[0] for item in candidates], ["tool", "transcript"])
            self.assertAlmostEqual(candidates[0].score_ceiling, 0.22)
            self.assertAlmostEqual(candidates[1].score_ceiling, 0.12)
        finally:
            db.close()

    def test_strict_tie_guard_and_early_stop_state(self):
        # Same speaker/timestamp/text gives equal score potential. The first row cannot
        # authorize stopping on equality because the second session can win a tie-break.
        rows = [
            _row("z-first", 1, NOW_MS, "agent", "aaaaaaaaaaaaaaaaaaaa"),
            _row("a-second", 1, NOW_MS, "agent", "aaaaaaaaaaaaaaaaaaaa"),
        ]
        db = self._db(rows)
        try:
            lane = self._lane(db, "a")
            self.assertTrue(db.in_transaction)
            first = next(lane)
            self.assertFalse(first.strictly_below(first.rounded_score_ceiling))
            self.assertFalse(first.strictly_below(float("nan")))
            self.assertTrue(first.strictly_below(
                first.rounded_score_ceiling + 0.0001))
            lane.stop()
            progress = lane.progress
            self.assertEqual(progress.candidates_examined, 1)
            self.assertEqual(progress.observed_total, 1)
            self.assertFalse(progress.exhausted)
            self.assertTrue(progress.stopped)
            self.assertFalse(progress.totals_exact)
            self.assertFalse(db.in_transaction)

            complete = self._lane(db, "a")
            self.assertEqual(len(list(complete)), 2)
            self.assertEqual(complete.progress.observed_total, 2)
            self.assertTrue(complete.progress.exhausted)
            self.assertFalse(complete.progress.stopped)
            self.assertFalse(db.in_transaction)
        finally:
            db.close()

    def test_exhausted_stream_discloses_distinct_chat_overflow(self):
        rows = [
            _row(f"session-{index}", 1, NOW_MS - index, "user", "hi")
            for index in range(3)
        ]
        db = self._db(rows)
        try:
            with mock.patch.object(
                    corpusdb, "_SHORT_DISTINCT_TRACK_MAX", 2):
                lane = self._lane(db, "hi")
                self.assertEqual(len(list(lane)), 3)
            progress = lane.progress
            self.assertEqual(progress.observed_total, 3)
            self.assertEqual(progress.observed_chats, 2)
            self.assertTrue(progress.exhausted)
            self.assertFalse(progress.chat_count_exact)
            self.assertFalse(progress.totals_exact)
        finally:
            db.close()

    def test_family_exclusion_and_target_stream_partition_candidate_snapshot(self):
        rows = [
            _row("a-1", 1, NOW_MS, "user", "hi aligned"),
            _row("a-2", 2, NOW_MS - DAY_MS, "agent", "which interior"),
            _row("b", 1, NOW_MS - 2 * DAY_MS, "tool", "hi tool"),
            _row("nul", 1, NOW_MS - 3 * DAY_MS, "user", "prefix\0hi"),
            _row("miss", 1, NOW_MS - 4 * DAY_MS, "agent", "prefix\0zzz"),
        ]
        db = self._db(rows)
        try:
            db.executemany(
                "INSERT INTO session_family(session, root, side) VALUES(?, ?, ?)",
                (("a-1", "a", 0), ("a-2", "a", 0), ("b", "b", 0),
                 ("nul", "nul", 0), ("miss", "miss", 0)),
            )
            global_lane = self._lane(db, "hi")
            expected: dict[str, list[tuple]] = {}
            for candidate in global_lane:
                expected.setdefault(candidate.family_root, []).append(
                    (candidate.row[0], candidate.span))

            family_lane = corpusdb.bounded_short_keyword_family_candidates(
                db, "hi", "a", None, now_ms=NOW_MS,
                who_weights=WHO_WEIGHTS, source_scales=SOURCE_SCALES,
                recency_half_life_days=14.0, user_recency_floor=0.35,
            )
            remainder = corpusdb.bounded_short_keyword_candidates(
                db, "hi", None, now_ms=NOW_MS, who_weights=WHO_WEIGHTS,
                source_scales=SOURCE_SCALES, recency_half_life_days=14.0,
                user_recency_floor=0.35, exclude_families=("a",),
            )
            actual: dict[str, list[tuple]] = {
                "a": [(candidate.row[0], candidate.span)
                      for candidate in family_lane]
            }
            for candidate in remainder:
                actual.setdefault(candidate.family_root, []).append(
                    (candidate.row[0], candidate.span))
            self.assertEqual(actual, expected)
            with self.assertRaises(ValueError):
                corpusdb.ShortKeywordCandidateStream(
                    db, "hi", None, now_ms=NOW_MS,
                    who_weights=WHO_WEIGHTS, source_scales=SOURCE_SCALES,
                    recency_half_life_days=14.0, user_recency_floor=0.35,
                    only_family="a", exclude_families=("b",),
                )
        finally:
            db.close()

    def test_unsupported_shapes_and_missing_index_fail_closed(self):
        db = self._db([_row("one", 1, NOW_MS, "user", "hi")])
        try:
            for query in ("", "abc", "-", "é"):
                with self.subTest(query=query), self.assertRaises(ValueError):
                    self._lane(db, query)
            for flt in ({"agent": "codex"}, {"since_ms": 1},
                        {"mystery": True}, {"include_tools": None}):
                with self.subTest(flt=flt), self.assertRaises(ValueError):
                    self._lane(db, "hi", flt)
            db.execute("DROP INDEX msgs_who_ts")
            with self.assertRaises(sqlite3.OperationalError):
                self._lane(db, "hi")
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
