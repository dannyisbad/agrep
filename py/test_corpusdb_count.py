from __future__ import annotations

import sqlite3
import unittest
from unittest import mock

from _test_support import isolate_data_dir


isolate_data_dir()
import corpusdb


class _NoFunctionReplacement(sqlite3.Connection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._agrep_functions = set()

    def create_function(self, name, narg, func, *, deterministic=False):
        if name in self._agrep_functions:
            raise sqlite3.OperationalError("Error creating function")
        self._agrep_functions.add(name)
        return super().create_function(
            name, narg, func, deterministic=deterministic)


class CorpusCountTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(corpusdb._SCHEMA_SQL)
        rows = [
            ("a", 0, 10, "codex", "p", "", "", "", "user",
             "the alpha beta hi"),
            ("b", 0, 9, "codex", "p", "", "", "", "agent",
             "the alpha payload beta this"),
            ("c", 0, 8, "codex", "p", "", "", "", "tool",
             "the alpha beta\x00without short token"),
            ("d", 0, 7, "codex", "p", "", "", "", "user",
             "ſſſ FTS compatibility candidate"),
            ("e", 0, 6, "codex", "p", "", "", "", "agent",
             "sss literal evidence"),
            ("f", 0, 5, "codex", "p", "", "", "", "user",
             "plain row without the planted terms"),
        ]
        self.db.executemany(corpusdb._INS, rows)
        self.db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
        self.db.execute("INSERT INTO msgs_prose_fts(rowid, text) "
                        "SELECT id, text FROM msgs WHERE who <> 'tool'")

    def tearDown(self) -> None:
        self.db.close()

    def _expected(self, query: str, flt: dict | None = None) -> tuple[int, int, int]:
        tokens = [token for token in query.replace("-", " ").replace("_", " ").split()
                  if token]
        if len(tokens) >= 2:
            result = corpusdb.keyword_terms(
                self.db, query, 10_000_000, flt, position_order=False)["terms"]
        else:
            result = corpusdb.keyword(
                self.db, query, 10_000_000, flt, position_order=False)
        hits = result["hits"]
        return (len(hits), len({hit["session"] for hit in hits}),
                sum(hit["who"] == "tool" for hit in hits))

    def test_candidate_where_does_not_grow_with_token_repetition(self) -> None:
        single = corpusdb._candidate_where(["a"], None)
        repeated = corpusdb._candidate_where(["a"] * 50, None)
        self.assertEqual(single, repeated)

    def test_count_matches_materialized_union(self) -> None:
        for query in ("the", "alpha beta", "hi", "sss"):
            with self.subTest(query=query):
                got = corpusdb.keyword_count(self.db, query)
                self.assertEqual(
                    (got["total"], got["chats"], got["tool_hits"]),
                    self._expected(query))

    def test_count_preserves_filters(self) -> None:
        for flt in ({"include_tools": False}, {"who": "tool"},
                    {"agent": "code"}, {"project": "p"}):
            with self.subTest(flt=flt):
                got = corpusdb.keyword_count(self.db, "alpha beta", flt)
                self.assertEqual(
                    (got["total"], got["chats"], got["tool_hits"]),
                    self._expected("alpha beta", flt))

    def test_capped_gate_keeps_python_re_i_widening(self) -> None:
        rows = [
            (f"rare-{index}", 0, index, "codex", "p", "", "", "", "user", "İİİ")
            for index in range(1_200)
        ]
        self.db.executemany(corpusdb._INS, rows)
        self.db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
        self.assertEqual(corpusdb.candidate_count_capped(
            self.db, ["iii"], None, 1_001), 1_001)

    def test_capped_rare_union_counts_posting_overlap_once(self) -> None:
        self.assertEqual(corpusdb.candidate_count_capped(
            self.db, ["sss"], None, 100), 2)

    def test_dense_lane_uses_generation_wide_candidate_count(self) -> None:
        rows = [
            (f"filler-{index}", 0, index, "codex", "p", "", "", "", "agent", "filler")
            for index in range(994)
        ]
        self.db.executemany(corpusdb._INS, rows)
        with mock.patch.object(
                corpusdb, "candidate_count_capped",
                side_effect=(899, 900, 100_001)) as count:
            self.assertFalse(corpusdb.dense_candidate_lane(self.db, ["sqlite"], None))
            self.assertTrue(corpusdb.dense_candidate_lane(self.db, ["cache"], None))
            self.assertTrue(corpusdb.dense_candidate_lane(self.db, ["the"], None))
        self.assertEqual(count.call_args_list, [
            mock.call(self.db, ["sqlite"], None, 100_001),
            mock.call(self.db, ["cache"], None, 100_001),
            mock.call(self.db, ["the"], None, 100_001),
        ])

    def test_dense_lane_keeps_snapshot_family_exclusion(self) -> None:
        rows = [
            (f"broad-{index}", 0, index, "codex", "p", "", "", "", "agent",
             "the broad evidence")
            for index in range(1_200)
        ]
        self.db.executemany(corpusdb._INS, rows)
        self.db.executemany(
            "INSERT INTO session_family(session, root, side) VALUES(?, ?, ?)",
            (("broad-1199", "caller", 0), ("broad-1198", "caller", 0)),
        )
        self.db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
        flt = {"exclude_session": "broad-1199"}
        self.assertTrue(corpusdb.dense_candidate_lane(
            self.db, ["the"], flt))
        stats, candidates = corpusdb.bounded_single_keyword_candidates(
            self.db,
            "the",
            flt,
            now_ms=2_000,
            who_weights={"agent": 0.8},
            source_scales={"agent": 1.0},
            recency_half_life_days=14,
            user_recency_floor=0.35,
        )
        self.assertEqual(stats[0], 1_202)
        sessions = {row[0] for _ceiling, row in candidates}
        self.assertNotIn("broad-1199", sessions)
        self.assertNotIn("broad-1198", sessions)

    def test_dense_recency_cursor_filters_ascii_non_candidates(self) -> None:
        expected = ((["alpha"], {"a", "b", "c"}),
                    (["sss"], {"d", "e"}),
                    (["token"], {"c"}),
                    (["alpha", "beta"], {"a", "b", "c"}))
        for tokens, sessions in expected:
            with self.subTest(tokens=tokens):
                candidates = list(corpusdb.score_ceiling_candidates(
                    self.db,
                    tokens,
                    None,
                    now_ms=20,
                    who_weights={"user": 1.0, "agent": 0.8, "tool": 0.4},
                    source_scales={},
                    recency_half_life_days=14,
                    user_recency_floor=0.35,
                    dense=True,
                ))
                self.assertNotIn("f", {row[0] for _ceiling, row in candidates})
                confirmed = {
                    row[0] for _ceiling, row in candidates
                    if all(corpusdb.common.insensitive_span(
                        row[corpusdb._TEXT], token) is not None
                           for token in tokens)
                }
                self.assertEqual(
                    confirmed, sessions)

    def test_dense_multi_term_lane_uses_posting_floor(self) -> None:
        rows = [
            (f"dense-{index}", 0, index, "codex", "p", "", "", "", "agent",
             "race_condition")
            for index in range(1_000)
        ]
        self.db.executemany(corpusdb._INS, rows)
        self.db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
        with mock.patch.object(
                corpusdb, "candidate_count_capped",
                side_effect=AssertionError("posting floor should settle density")):
            self.assertTrue(corpusdb.dense_candidate_lane(
                self.db, ["race", "condition"], None))

    def test_filtered_dense_gate_skips_population_count(self) -> None:
        marker = object()
        with mock.patch.object(corpusdb, "_register_functions"), \
                mock.patch.object(
                    corpusdb, "candidate_count_capped",
                    side_effect=AssertionError("filtered gate should not pre-count")):
            self.assertFalse(corpusdb.dense_candidate_lane(
                marker, ["sqlite"], {"project": "agrep"}))

    def test_sql_functions_are_not_replaced_on_a_live_connection(self) -> None:
        db = sqlite3.connect(":memory:", factory=_NoFunctionReplacement)
        try:
            corpusdb._register_functions(db)
            cursor = db.execute("SELECT 1 UNION ALL SELECT 2")
            self.assertEqual(cursor.fetchone(), (1,))
            corpusdb._register_functions(db)
            self.assertEqual(cursor.fetchone(), (2,))
            self.assertEqual(db.execute(
                "SELECT agrep_contains_ci('Alpha', 'ph')").fetchone(), (1,))
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
