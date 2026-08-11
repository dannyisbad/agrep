from __future__ import annotations

import contextlib
import random
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _test_support import isolate_data_dir

isolate_data_dir()
import common
import corpusdb
import search


NOW_S = 2_000_000_000.0
NOW_MS = int(NOW_S * 1000)
DAY_MS = 86_400_000


class SearchShortLaneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        search._load_corpusdb()

    def test_family_dominance_requires_floor_and_clear_separation(self):
        def candidate(root):
            return SimpleNamespace(
                family_root=root, span=None,
                row=("session", 0, 0, "", "", "", "", "", "user", ""),
            )

        floor = search._ShortFamilyPass()
        for _ in range(search._SHORT_DOMINANCE_MIN_CANDIDATES - 1):
            self.assertIsNone(floor.observe(candidate("leader")))
        self.assertEqual(floor.observe(candidate("leader")), "leader")

        ratio = search._ShortFamilyPass()
        for _ in range(5):
            self.assertIsNone(ratio.observe(candidate("runner")))
        for _ in range(39):
            self.assertIsNone(ratio.observe(candidate("leader")))
        self.assertEqual(ratio.observe(candidate("leader")), "leader")

    def test_family_dominance_disables_bounded_state_after_overflow(self):
        def candidate(root, session):
            return SimpleNamespace(
                family_root=root, span=(0, 1),
                row=(session, 0, 0, "", "", "", "", "", "user", ""),
            )

        with mock.patch.object(search, "_SHORT_FAMILY_TRACK_MAX", 2):
            family_pass = search._ShortFamilyPass()
            self.assertIsNone(family_pass.observe(candidate("one", "a")))
            self.assertIsNone(family_pass.observe(candidate("two", "b")))
            self.assertIsNone(family_pass.observe(candidate("three", "c")))
            self.assertTrue(family_pass.disabled)
            self.assertEqual(family_pass.stats, {})
            for _ in range(search._SHORT_DOMINANCE_MIN_CANDIDATES * 2):
                self.assertIsNone(family_pass.observe(candidate("one", "a")))
            self.assertEqual(family_pass.stats, {})

        family_pass = search._ShortFamilyPass()
        for index in range(200_000):
            result = family_pass.observe(candidate(
                f"family-{index}", f"session-{index}"))
        self.assertIsNone(result)
        self.assertTrue(family_pass.disabled)
        self.assertEqual(family_pass.stats, {})

    def _database(self, path: Path) -> None:
        db = sqlite3.connect(path)
        db.executescript(corpusdb._SCHEMA_SQL)
        rows = [
            ("a-tie", 0, NOW_MS, "codex", "p", "", "", "", "agent",
             "hi hi hi tied evidence"),
            ("z-tie", 0, NOW_MS, "codex", "p", "", "", "", "agent",
             "hi hi hi tied evidence"),
        ]
        for index in range(6):
            rows.append((
                "duplicate" if index < 2 else f"fresh-{index}", index,
                NOW_MS - (index + 1) * 60_000, "codex", "p", "", "", "",
                "agent", f"hi hi hi recent evidence {index}",
            ))
        rows.append((
            "old-user", 0, NOW_MS - 200 * DAY_MS, "codex", "p", "", "", "",
            "user", "hi hi hi durable human evidence",
        ))
        for index in range(300):
            rows.append((
                f"stale-{index:03}", 0, NOW_MS - (30 * DAY_MS + index * 60_000),
                "codex", "p", "", "", "", "agent", "hi hi hi stale evidence",
            ))
        db.executemany(corpusdb._INS, rows)
        db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
        db.execute("INSERT INTO msgs_prose_fts(rowid, text) "
                   "SELECT id, text FROM msgs WHERE who <> 'tool'")
        db.commit()
        db.close()

    def _packed_session_database(self, path: Path) -> None:
        db = sqlite3.connect(path)
        db.executescript(corpusdb._SCHEMA_SQL)
        rows = []
        for index in range(96):
            rows.append((
                "packed", index, NOW_MS - index * 1000,
                "codex", "p", "", "", "", "agent", "hi hi hi packed",
            ))
        rows.extend([
            ("parent", 0, NOW_MS - 120_000, "codex", "p", "", "", "",
             "user", "hi aligned parent"),
            ("child", 0, NOW_MS - 121_000, "codex", "p", "", "", "",
             "user", "hi aligned child"),
            ("interior", 0, NOW_MS - 122_000, "codex", "p", "", "", "",
             "user", "which fragment"),
        ])
        for index in range(300):
            rows.append((
                f"stale-{index:03}", 0,
                NOW_MS - (60 * DAY_MS + index * 60_000),
                "codex", "p", "", "", "", "agent", "hi stale",
            ))
        db.executemany(corpusdb._INS, rows)
        db.executemany(
            "INSERT INTO session_family(session, root, side) VALUES(?, ?, ?)",
            (("parent", "parent", 0), ("child", "parent", 0)),
        )
        db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
        db.execute("INSERT INTO msgs_prose_fts(rowid, text) "
                   "SELECT id, text FROM msgs WHERE who <> 'tool'")
        db.commit()
        db.close()

    def _dominant_family_database(
            self, path: Path, rows: int = 20_000, singletons: int = 7) -> None:
        db = sqlite3.connect(path)
        db.executescript(corpusdb._SCHEMA_SQL)
        batch = []
        families = []
        for index in range(rows):
            session = f"dominant-{index % 200:03d}"
            batch.append((
                session, index, NOW_MS - index * DAY_MS,
                "codex", "p", "", "", "", "user", "hi " * 24,
            ))
            if index < 200:
                families.append((session, "dominant", 0))
        for index in range(singletons):
            session = f"singleton-{index}"
            batch.append((
                session, 0, NOW_MS - (rows + index + 1) * DAY_MS,
                "codex", "p", "", "", "", "user", "hi " * 24,
            ))
            families.append((session, session, 0))
        db.executemany(corpusdb._INS, batch)
        db.executemany(
            "INSERT INTO session_family(session, root, side) VALUES(?, ?, ?)",
            families)
        db.commit()
        db.close()

    @staticmethod
    def _shape(result: dict) -> list[tuple]:
        return [(hit["session"], hit["turn"], hit["who"], hit["score"])
                for hit in result["hits"]]

    def test_bounded_heads_match_exhaustive_with_ties_and_user_floor(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "short.db"
            self._database(path)

            def connect(**_kwargs):
                return sqlite3.connect(path)

            with mock.patch.object(search.corpusdb, "connect", side_effect=connect), \
                    mock.patch.object(search.time, "time", return_value=NOW_S), \
                    mock.patch.object(search, "_BOUNDED_ROW_MIN_CANDIDATES", 0), \
                    mock.patch.object(search, "_SHORT_BOUNDARY_SEED", 32), \
                    mock.patch.object(search, "_SHORT_BOUNDARY_REFRESH", 64):
                exact = search.run_query(
                    "hi", limit=9, exact_totals=True, allow_fallback=False)
                bounded = search.run_query(
                    "hi", limit=9, exact_totals=False, allow_fallback=False)

        self.assertEqual(self._shape(bounded), self._shape(exact))
        self.assertEqual(bounded["hits"][0]["session"], "a-tie")
        self.assertIn("old-user", {hit["session"] for hit in bounded["hits"]})
        self.assertFalse(bounded["totals_exact"])
        self.assertLess(bounded["total"], exact["total"])
        self.assertLessEqual(bounded["chats"], exact["chats"])

    def test_agent_family_exclusion_preserves_bounded_short_heads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "short-family.db"
            self._database(path)
            with contextlib.closing(sqlite3.connect(path)) as db:
                db.executemany(
                    "INSERT INTO session_family(session, root, side) "
                    "VALUES(?, ?, ?)",
                    (("a-tie", "caller", 0), ("z-tie", "caller", 0)),
                )
                db.commit()

            def connect(**_kwargs):
                return sqlite3.connect(path)

            with mock.patch.object(search.corpusdb, "connect", side_effect=connect), \
                    mock.patch.object(search.time, "time", return_value=NOW_S), \
                    mock.patch.object(search, "_BOUNDED_ROW_MIN_CANDIDATES", 0), \
                    mock.patch.object(search, "_SHORT_BOUNDARY_SEED", 32), \
                    mock.patch.object(search, "_SHORT_BOUNDARY_REFRESH", 64):
                exact = search.run_query(
                    "hi", limit=6, exact_totals=True, allow_fallback=False,
                    exclude_session="a-tie")
                bounded = search.run_query(
                    "hi", limit=6, exact_totals=False, allow_fallback=False,
                    exclude_session="a-tie")

        self.assertEqual(self._shape(bounded), self._shape(exact))
        self.assertNotIn(
            "a-tie", {hit["session"] for hit in bounded["hits"]})
        self.assertNotIn(
            "z-tie", {hit["session"] for hit in bounded["hits"]})
        self.assertFalse(bounded["totals_exact"])

    def test_recap_window_preserves_short_lane_parity_and_family_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "short-window.db"
            self._database(path)
            with contextlib.closing(sqlite3.connect(path)) as db:
                db.executemany(
                    "INSERT INTO session_family(session, root, side) "
                    "VALUES(?, ?, ?)",
                    (("a-tie", "caller", 0), ("z-tie", "caller", 0)),
                )
                db.execute(
                    corpusdb._INS,
                    ("a-tie", 20, NOW_MS + 1000, "codex", "p", "", "", "",
                     "agent", "hi hi hi current echo"),
                )
                db.commit()

            def connect(**_kwargs):
                return sqlite3.connect(path)

            filters = {
                "exclude_session": "a-tie",
                "exclude_session_from_turn": 10,
            }
            with mock.patch.object(search.corpusdb, "connect", side_effect=connect), \
                    mock.patch.object(search.time, "time", return_value=NOW_S), \
                    mock.patch.object(search, "_BOUNDED_ROW_MIN_CANDIDATES", 0), \
                    mock.patch.object(search, "_SHORT_BOUNDARY_SEED", 32), \
                    mock.patch.object(search, "_SHORT_BOUNDARY_REFRESH", 64):
                exact = search.run_query(
                    "hi", limit=6, exact_totals=True,
                    allow_fallback=False, **filters)
                bounded = search.run_query(
                    "hi", limit=6, exact_totals=False,
                    allow_fallback=False, **filters)
                exact_sessions = search.run_query(
                    "hi", limit=6, session_limit=6, exact_totals=True,
                    allow_fallback=False, **filters)
                bounded_sessions = search.run_query(
                    "hi", limit=6, session_limit=6, exact_totals=False,
                    allow_fallback=False, **filters)

        self.assertEqual(self._shape(bounded), self._shape(exact))
        sessions = {hit["session"] for hit in bounded["hits"]}
        self.assertIn("a-tie", sessions)
        self.assertIn("z-tie", sessions)
        self.assertNotIn(
            ("a-tie", 20),
            {(hit["session"], hit["turn"]) for hit in bounded["hits"]},
        )
        self.assertEqual(
            [(hit["session"], hit["session_hits"])
             for hit in bounded_sessions["hits"]],
            [(hit["session"], hit["session_hits"])
             for hit in exact_sessions["hits"]],
        )

    def test_missing_family_sidecar_falls_back_to_exact_caller_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "short-no-family.db"
            self._database(path)

            def connect(**_kwargs):
                return sqlite3.connect(path)

            with mock.patch.object(search.corpusdb, "connect", side_effect=connect), \
                    mock.patch.object(search.time, "time", return_value=NOW_S):
                exact = search.run_query(
                    "hi", limit=6, exact_totals=True, allow_fallback=False,
                    exclude_session="a-tie")
                bounded = search.run_query(
                    "hi", limit=6, exact_totals=False, allow_fallback=False,
                    exclude_session="a-tie")

        self.assertEqual(self._shape(bounded), self._shape(exact))
        sessions = {hit["session"] for hit in bounded["hits"]}
        self.assertNotIn("a-tie", sessions)
        self.assertIn("z-tie", sessions)

    def test_exact_totals_and_filters_bypass_short_lane(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "short.db"
            self._database(path)

            def connect(**_kwargs):
                return sqlite3.connect(path)

            with mock.patch.object(search.corpusdb, "connect", side_effect=connect), \
                    mock.patch.object(search, "_bounded_short_keyword_rows",
                                      return_value=None) as lane:
                exact = search.run_query(
                    "hi", limit=5, exact_totals=True, allow_fallback=False)
                filtered = search.run_query(
                    "hi", limit=5, exact_totals=False, who="user",
                    allow_fallback=False)

        lane.assert_called_once()
        self.assertNotIn("totals_exact", exact)
        self.assertTrue(filtered["totals_exact"])
        self.assertEqual({hit["who"] for hit in filtered["hits"]}, {"user"})

    def test_filtered_short_query_skips_full_scan_preflight(self):
        db = mock.Mock()
        db.in_transaction = False
        with mock.patch.object(search.corpusdb, "candidate_count_capped") as preflight:
            result = search._bounded_short_keyword_rows(
                db, "hi", 5, {"who": "user", "include_tools": True})
        self.assertIsNone(result)
        preflight.assert_not_called()
        db.execute.assert_not_called()

    def test_ascii_fast_score_matches_canonical_snippet_score(self):
        texts = (
            "this interior fragment",
            "hi hi aligned repeats",
            "xxhi partial hi boundary",
            "x" * 80 + "hi cut-edge hi",
            "don't promote the t apostrophe",
            "camelHi digit2hi punctuation/hi",
            "spaces\tand\nhi collapse",
        )
        for q in ("hi", "is", "t"):
            prepared = search.boundary_rank.prepare_query(q, {})
            boundary = (prepared, re.compile(f"({re.escape(q)})", re.I), q, {})
            for index, text in enumerate(texts):
                span = common.insensitive_span(text, q)
                if span is None:
                    continue
                row = (f"s{index}", "codex", "p", "", "", "", 0,
                       NOW_MS, "user", text)
                fast = search._short_ascii_score(
                    row, span, q, NOW_MS, prepared.terms[0].ambiguity)
                hit = corpusdb._hit(row, *span)
                expected = round(search._score(
                    hit, re.compile(re.escape(q), re.I), len(q), NOW_MS,
                    terms=[q], boundary=boundary), 4)
                self.assertIsNotNone(fast)
                self.assertEqual(fast[0], expected)
                self.assertEqual(fast[1], hit["_boundary_class"])
                self.assertAlmostEqual(fast[2], hit["_boundary_factor"], places=6)

    def test_generation_quality_tightens_short_candidate_ceiling(self):
        db = sqlite3.connect(":memory:")
        db.executescript(corpusdb._SCHEMA_SQL)
        prepared = search.boundary_rank.prepare_query("zz", {"zz": (10, 0)})
        boundary = (prepared, re.compile("(zz)", re.I), "zz", {"zz": (10, 0)})
        try:
            expected = {0: 0.12, 1: 1.0 - prepared.terms[0].ambiguity * 0.5,
                        2: 1.0}
            for quality, ceiling in expected.items():
                db.execute("DELETE FROM boundary_stats")
                db.execute(
                    "INSERT INTO boundary_stats(token, n, s, q) VALUES('zz', 10, 0, ?)",
                    (quality,))
                self.assertAlmostEqual(
                    search._short_boundary_ceiling(db, boundary), ceiling)
        finally:
            db.close()

    def test_pending_aligned_short_hit_is_scored_before_ceiling_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pending.db"
            db = sqlite3.connect(path)
            db.executescript(corpusdb._SCHEMA_SQL)
            db.executemany(corpusdb._INS, [
                ("interior", 0, NOW_MS, "codex", "p", "", "", "", "user",
                 "which fragment"),
                ("winner", 0, NOW_MS - 60_000, "codex", "p", "", "", "", "user",
                 "hi aligned"),
                ("stale", 0, NOW_MS - 200 * DAY_MS, "codex", "p", "", "", "",
                 "user", "hi stale"),
            ])
            db.commit()
            db.close()

            def connect(**_kwargs):
                return sqlite3.connect(path)

            with mock.patch.object(search.corpusdb, "connect", side_effect=connect), \
                    mock.patch.object(search.time, "time", return_value=NOW_S), \
                    mock.patch.object(search, "_SHORT_BOUNDARY_SEED", 1), \
                    mock.patch.object(search, "_SHORT_BOUNDARY_REFRESH", 100):
                bounded = search.run_query(
                    "hi", limit=1, exact_totals=False, allow_fallback=False)
                exact = search.run_query(
                    "hi", limit=1, exact_totals=True, allow_fallback=False)

        self.assertEqual(self._shape(bounded), self._shape(exact))
        self.assertEqual(bounded["hits"][0]["session"], "winner")

    def test_transcript_quality_ceiling_cannot_prune_aligned_tool_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mixed-short.db"
            db = sqlite3.connect(path)
            db.executescript(corpusdb._SCHEMA_SQL)
            db.executemany(corpusdb._INS, [
                ("transcript-interior", 0, NOW_MS, "codex", "p", "", "", "",
                 "user", "xhiz"),
                ("tool-aligned", 0, NOW_MS - 1_000, "codex", "p", "", "", "",
                 "tool", "hi"),
                ("tail", 0, NOW_MS - 400 * DAY_MS, "codex", "p", "", "", "",
                 "agent", "xhiz"),
            ])
            db.execute(
                "INSERT INTO boundary_stats(token, n, s, q) VALUES('hi', 1000, 0, 0)")
            db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
            db.execute("INSERT INTO msgs_prose_fts(rowid, text) "
                       "SELECT id, text FROM msgs WHERE who <> 'tool'")
            db.commit()
            db.close()

            def connect(**_kwargs):
                return sqlite3.connect(path)

            with mock.patch.object(search.corpusdb, "connect", side_effect=connect), \
                    mock.patch.object(search.time, "time", return_value=NOW_S), \
                    mock.patch.object(search, "_SHORT_BOUNDARY_SEED", 1), \
                    mock.patch.object(search, "_SHORT_BOUNDARY_REFRESH", 1), \
                    mock.patch.object(search, "_native_boundary_scores", return_value=False):
                bounded = search.run_query(
                    "hi", limit=1, exact_totals=False, allow_fallback=False,
                    include_tools=True)
                exact = search.run_query(
                    "hi", limit=1, exact_totals=True, allow_fallback=False,
                    include_tools=True)

        self.assertEqual(self._shape(bounded), self._shape(exact))
        self.assertEqual(bounded["hits"][0]["session"], "tool-aligned")

    def test_short_session_and_family_heads_match_exhaustive_after_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "short-sessions.db"
            self._packed_session_database(path)

            def connect(**_kwargs):
                return sqlite3.connect(path)

            with mock.patch.object(search.corpusdb, "connect", side_effect=connect), \
                    mock.patch.object(search.corpusdb, "candidate_count_capped") as count, \
                    mock.patch.object(search, "_native_boundary_scores",
                                      return_value=False), \
                    mock.patch.object(search.time, "time", return_value=NOW_S), \
                    mock.patch.object(search, "_SHORT_BOUNDARY_SEED", 16), \
                    mock.patch.object(search, "_SHORT_BOUNDARY_REFRESH", 32):
                raw_exact = search.run_query(
                    "hi", limit=3, session_limit=3, exact_totals=True,
                    allow_fallback=False, family_diverse=False)
                raw_bounded = search.run_query(
                    "hi", limit=3, session_limit=3, exact_totals=False,
                    allow_fallback=False, family_diverse=False)
                family_exact = search.run_query(
                    "hi", limit=3, session_limit=3, exact_totals=True,
                    allow_fallback=False, family_diverse=True)
                family_bounded = search.run_query(
                    "hi", limit=3, session_limit=3, exact_totals=False,
                    allow_fallback=False, family_diverse=True)

        count.assert_not_called()
        self.assertEqual(self._shape(raw_bounded), self._shape(raw_exact))
        self.assertEqual(self._shape(family_bounded), self._shape(family_exact))
        self.assertEqual(
            [(hit["session"], hit["session_hits"]) for hit in raw_bounded["hits"]],
            [(hit["session"], hit["session_hits"]) for hit in raw_exact["hits"]],
        )
        packed = next(hit for hit in raw_bounded["hits"] if hit["session"] == "packed")
        self.assertEqual(packed["session_hits"], 96)
        self.assertFalse(raw_bounded["totals_exact"])
        self.assertFalse(family_bounded["totals_exact"])
        self.assertLess(raw_bounded["total"], raw_exact["total"])

    def test_family_isolation_skips_python_scan_of_a_dominant_recent_family(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dominant-family.db"
            self._dominant_family_database(path)

            def connect(**_kwargs):
                return sqlite3.connect(path)

            debug = []
            with mock.patch.object(search.corpusdb, "connect", side_effect=connect), \
                    mock.patch.object(search.time, "time", return_value=NOW_S), \
                    mock.patch.object(search.common, "dbg",
                                      side_effect=lambda message, *_: debug.append(
                                          str(message))), \
                    mock.patch.object(search, "_native_boundary_scores",
                                      return_value=False):
                exact = search.run_query(
                    "hi", limit=8, session_limit=8, exact_totals=True,
                    allow_fallback=False, family_diverse=True)
                bounded = search.run_query(
                    "hi", limit=8, session_limit=8, exact_totals=False,
                    allow_fallback=False, family_diverse=True)

        self.assertEqual(self._shape(bounded), self._shape(exact))
        self.assertTrue(any("isolated dominant family" in line for line in debug))
        final = next(
            line for line in reversed(debug)
            if line.startswith("bounded short sessions: examined "))
        examined = int(re.search(r"examined (\d+)", final).group(1))
        self.assertLess(examined, 100)
        self.assertFalse(bounded["totals_exact"])

    def test_family_isolation_matches_exhaustive_across_adversarial_layouts(self):
        for seed in range(12):
            with self.subTest(seed=seed), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "family-property.db"
                rng = random.Random(seed)
                db = sqlite3.connect(path)
                db.executescript(corpusdb._SCHEMA_SQL)
                sessions = [
                    (f"family-{family}-session-{member}",
                     f"family-{family}", 0)
                    for family in range(6) for member in range(3)
                ]
                db.executemany(
                    "INSERT INTO session_family(session, root, side) "
                    "VALUES(?, ?, ?)",
                    sessions,
                )
                rows = []
                texts = ("hi", "which", "HI-hi", "prefix\0hi", "hİ", "miss")
                speakers = ("user", "agent", "tool")
                for index in range(180):
                    family = 0 if index < 80 else 1 if index < 130 else index % 6
                    session = f"family-{family}-session-{rng.randrange(3)}"
                    age = rng.choice((0, 1, 2, 20, 200)) * DAY_MS
                    rows.append((
                        session, index, NOW_MS - age - rng.randrange(10_000),
                        "codex", "p", "", "", "", rng.choice(speakers),
                        rng.choice(texts),
                    ))
                db.executemany(corpusdb._INS, rows)
                db.commit()
                db.close()

                def connect(**_kwargs):
                    return sqlite3.connect(path)

                with mock.patch.object(
                        search.corpusdb, "connect", side_effect=connect), \
                        mock.patch.object(search.time, "time", return_value=NOW_S), \
                        mock.patch.object(
                            search, "_SHORT_DOMINANCE_MIN_CANDIDATES", 8), \
                        mock.patch.object(search, "_SHORT_DOMINANCE_RATIO", 2), \
                        mock.patch.object(search, "_native_boundary_scores",
                                          return_value=False):
                    exact = search.run_query(
                        "hi", limit=4, session_limit=4, exact_totals=True,
                        allow_fallback=False, family_diverse=True)
                    bounded = search.run_query(
                        "hi", limit=4, session_limit=4, exact_totals=False,
                        allow_fallback=False, family_diverse=True)
                self.assertEqual(self._shape(bounded), self._shape(exact))

    def test_uniform_families_do_not_trigger_dominance_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "uniform-families.db"
            db = sqlite3.connect(path)
            db.executescript(corpusdb._SCHEMA_SQL)
            families = [
                (f"uniform-{index:02}", f"uniform-{index:02}", 0)
                for index in range(40)
            ]
            db.executemany(
                "INSERT INTO session_family(session, root, side) "
                "VALUES(?, ?, ?)", families)
            rows = []
            for repeat in range(40):
                for family in range(40):
                    session = f"uniform-{family:02}"
                    rows.append((
                        session, repeat, NOW_MS - repeat,
                        "codex", "p", "", "", "", "user", "hi",
                    ))
            db.executemany(corpusdb._INS, rows)
            db.commit()
            db.close()

            def connect(**_kwargs):
                return sqlite3.connect(path)

            debug = []
            with mock.patch.object(search.corpusdb, "connect", side_effect=connect), \
                    mock.patch.object(search.time, "time", return_value=NOW_S), \
                    mock.patch.object(search.common, "dbg",
                                      side_effect=lambda message, *_: debug.append(
                                          str(message))):
                exact = search.run_query(
                    "hi", limit=8, session_limit=8, exact_totals=True,
                    allow_fallback=False, family_diverse=True)
                bounded = search.run_query(
                    "hi", limit=8, session_limit=8, exact_totals=False,
                    allow_fallback=False, family_diverse=True)
        self.assertEqual(self._shape(bounded), self._shape(exact))
        self.assertFalse(any("isolated " in line for line in debug))

    def test_restart_accounting_is_exact_when_each_partition_exhausts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "family-accounting.db"
            db = sqlite3.connect(path)
            db.executescript(corpusdb._SCHEMA_SQL)
            rows = []
            families = []
            for index in range(40):
                session = f"dominant-{index % 4}"
                rows.append((
                    session, index, NOW_MS - 200 * DAY_MS - index,
                    "codex", "p", "", "", "", "user", "hi",
                ))
                if index < 4:
                    families.append((session, "dominant", 0))
            for index in range(4):
                session = f"other-{index}"
                rows.append((
                    session, 0, NOW_MS - 300 * DAY_MS - index,
                    "codex", "p", "", "", "", "user", "hi",
                ))
                families.append((session, session, 0))
            db.executemany(corpusdb._INS, rows)
            db.executemany(
                "INSERT INTO session_family(session, root, side) "
                "VALUES(?, ?, ?)", families)
            db.commit()
            db.close()

            def connect(**_kwargs):
                return sqlite3.connect(path)

            debug = []
            with mock.patch.object(search.corpusdb, "connect", side_effect=connect), \
                    mock.patch.object(search.time, "time", return_value=NOW_S), \
                    mock.patch.object(search.common, "dbg",
                                      side_effect=lambda message, *_: debug.append(
                                          str(message))):
                exact = search.run_query(
                    "hi", limit=5, session_limit=5, exact_totals=True,
                    allow_fallback=False, family_diverse=True)
                bounded = search.run_query(
                    "hi", limit=5, session_limit=5, exact_totals=False,
                    allow_fallback=False, family_diverse=True)
        self.assertEqual(self._shape(bounded), self._shape(exact))
        self.assertTrue(any("isolated dominant family" in line for line in debug))
        self.assertTrue(bounded["totals_exact"])
        self.assertEqual(
            (bounded["total"], bounded["chats"], bounded["tool_hits"]),
            (exact["total"], exact["chats"], exact["tool_hits"]),
        )


if __name__ == "__main__":
    unittest.main()
