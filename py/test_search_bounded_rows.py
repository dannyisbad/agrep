from __future__ import annotations

import contextlib
import sqlite3
import unittest
from unittest import mock

from _test_support import isolate_data_dir

isolate_data_dir()
import corpusdb
import search


NOW_S = 2_000_000_000.0
NOW_MS = int(NOW_S * 1000)
DAY_MS = 86_400_000


class SearchBoundedRowsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        search._load_corpusdb()

    def setUp(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(corpusdb._SCHEMA_SQL)
        rows: list[tuple] = []

        def add(session: str, turn: int, text: str, *, ts: int = NOW_MS,
                who: str = "agent", agent: str = "codex",
                project: str = "/repo/red", model: str = "gpt-5.4") -> None:
            rows.append((session, turn, ts, agent, project, "", model, "fixture",
                         who, text))

        for index in range(48):
            add(f"prune-phrase-{index:03}", index,
                f"phase lock adjacent evidence {index}",
                ts=(NOW_MS - index * 60_000 if index < 8
                    else NOW_MS - (60 + index) * DAY_MS),
                who="user" if index < 8 else "agent")
        for index in range(24):
            add(f"prune-terms-{index:03}", index,
                f"phase has distant evidence {index} before lock",
                ts=NOW_MS - index * DAY_MS)

        add("fill-phrase-a", 0, "cache miss adjacent old evidence",
            ts=NOW_MS - 100 * DAY_MS)
        add("fill-phrase-b", 0, "cache-miss second old evidence",
            ts=NOW_MS - 101 * DAY_MS)
        for index in range(24):
            add(f"fill-terms-{index:03}", index,
                f"cache has current evidence {index} before the miss",
                ts=NOW_MS - index * 60_000)

        for index in range(24):
            add(f"terms-only-{index:03}", index,
                f"vector has payload {index} before the shard boundary",
                ts=NOW_MS - index * 60_000,
                who="user" if index == 19 else "agent")

        add("tie-z-phrase", 7, "equal rank tied evidence", ts=NOW_MS)
        add("tie-a-phrase", 7, "equal rank tied evidence", ts=NOW_MS)
        add("tie-z-terms", 7, "equal has tied evidence before rank", ts=NOW_MS)
        add("tie-a-terms", 7, "equal has tied evidence before rank", ts=NOW_MS)

        add("filter-user", 1, "filtered target user evidence", who="user",
            agent="codex", project="/repo/red", model="gpt-5.4")
        add("filter-tool", 2, "filtered target tool evidence", who="tool",
            agent="codex", project="/repo/red", model="gpt-5.4",
            ts=NOW_MS - 1000)
        add("filter-agent", 3, "filtered target agent evidence", who="agent",
            agent="claude", project="/repo/blue", model="sonnet-4",
            ts=NOW_MS - 2000)
        add("filter-old", 4, "filtered target old evidence", who="agent",
            agent="codex", project="/repo/blue", model="gpt-4.1",
            ts=NOW_MS - 20 * DAY_MS)
        add("filter-new", 5, "filtered target new evidence", who="agent",
            agent="codex", project="/repo/green", model="gpt-5.4-mini",
            ts=NOW_MS - 3000)

        add("nul-phrase", 0, "alpha\x00hi joined evidence", ts=NOW_MS)
        add("nul-terms", 0, "alpha has payload\x00before hi", ts=NOW_MS - 1)
        add("short-interior", 0, "alpha hiking substring evidence", ts=NOW_MS - 2)
        add("long-s-phrase", 0, "alpha ſſſ compatibility evidence",
            ts=NOW_MS - 3)
        add("long-s-terms", 0, "alpha has compatibility evidence before ſſſ",
            ts=NOW_MS - 4)

        for index, session in enumerate(("aa-overlap-a", "ab-overlap-b")):
            add(session, 0, "delta echo phrase evidence", ts=NOW_MS - 100 * DAY_MS)
            add(session, 1, "echodelta compact reverse", ts=NOW_MS - index)
        for index in range(5):
            add(f"zz-overlap-extra-{index}", 0, "echodelta compact reverse",
                ts=NOW_MS - 10 - index)
        for index in range(8):
            add(f"zz-overlap-old-{index}", 0, "echodelta compact reverse",
                ts=NOW_MS - (100 + index) * DAY_MS)

        self.db.executemany(corpusdb._INS, rows)
        self.db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
        self.db.execute(
            "INSERT INTO msgs_prose_fts(rowid, text) "
            "SELECT id, text FROM msgs WHERE who <> 'tool'")
        self.db.executescript(corpusdb._TRIGGERS_SQL)
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()

    @contextlib.contextmanager
    def _fixed_ranking(self):
        with mock.patch.object(search.time, "time", return_value=NOW_S), \
                mock.patch.object(search, "_BOUNDED_ROW_MIN_CANDIDATES", 0), \
                mock.patch.object(search, "_BOUNDED_KEYWORD_MIN_CANDIDATES", 0), \
                mock.patch.object(search, "_native_boundary_scores", return_value=False):
            yield

    def _exhaustive(self, query: str, flt: dict) -> tuple[list[dict], object]:
        boundary = search._prepare_boundary(query, "keyword", self.db)
        both = corpusdb.keyword_terms(
            self.db, query, 10_000_000, flt, position_order=False)
        phrase = both["phrase"]["hits"]
        hits = search._augment_phrase_hits(phrase, both["terms"]["hits"])
        if not phrase:
            for hit in hits:
                hit["matched"] = "all-terms"
        search._rank(hits, query, "keyword", "score", boundary=boundary,
                     refine_all=True, top_k=None)
        return hits, boundary

    @staticmethod
    def _shape(hits: list[dict]) -> list[tuple]:
        return [
            (search._rank_key(hit), hit["session"], hit.get("turn"),
             hit.get("who"), hit.get("matched"), hit["score"], hit["snippet"],
             hit.get("session_hits"))
            for hit in hits
        ]

    def _run(self, query: str, limit: int, flt: dict | None = None):
        filters = {} if flt is None else flt
        with self._fixed_ranking():
            exhaustive, boundary = self._exhaustive(query, filters)
            bounded = search._bounded_keyword_rows(
                self.db, query, limit, filters, False, boundary=boundary)
        self.assertIsNotNone(bounded, f"bounded lane unavailable for {query!r}")
        self.assertEqual(self._shape(bounded["hits"]),
                         self._shape(exhaustive[:limit]))
        return exhaustive, bounded

    def _run_sessions(self, query: str, limit: int, flt: dict | None = None):
        filters = {} if flt is None else flt
        with self._fixed_ranking():
            exhaustive, boundary = self._exhaustive(query, filters)
            bounded = search._bounded_keyword_sessions(
                self.db, query, limit, filters, False, boundary=boundary)
        self.assertIsNotNone(bounded, f"bounded session lane unavailable for {query!r}")
        expected = search._session_heads(exhaustive, limit)
        self.assertEqual(self._shape(bounded["hits"]), self._shape(expected))
        return exhaustive, bounded

    def _assert_lower_bounds(self, exhaustive: list[dict], bounded: dict) -> None:
        exact = {
            "total": len(exhaustive),
            "chats": len({hit["session"] for hit in exhaustive}),
            "tool_hits": sum(hit.get("who") == "tool" for hit in exhaustive),
        }
        for field, count in exact.items():
            self.assertLessEqual(bounded[field], count, field)


    def test_repeated_terms_do_not_repeat_candidate_span_work(self):
        query = ("phase " * 200).strip()
        with self._fixed_ranking(), mock.patch.object(
                search.common, "insensitive_span",
                wraps=search.common.insensitive_span) as span:
            rows = search._bounded_keyword_rows(
                self.db, query, 4, {}, False)
            sessions = search._bounded_keyword_sessions(
                self.db, query, 4, {}, False)
        self.assertIsNotNone(rows)
        self.assertIsNotNone(sessions)
        self.assertTrue(all(hit.get("matched") == "all-terms"
                            for hit in rows["hits"]))
        self.assertTrue(all(hit.get("matched") == "all-terms"
                            for hit in sessions["hits"]))
        self.assertLess(span.call_count, 1_000)

    def test_phrase_page_prunes_with_exact_head(self):
        with mock.patch.object(search, "_BOUNDARY_REFINE_POOL", 1):
            exhaustive, bounded = self._run("phase lock", 4)
        self.assertGreater(len(exhaustive), 4)
        self.assertTrue(all(hit.get("matched") != "all-terms"
                            for hit in bounded["hits"]))
        self.assertFalse(bounded["totals_exact"])
        self._assert_lower_bounds(exhaustive, bounded)

    def test_phrase_shortfall_exhausts_and_fills_from_all_terms(self):
        exhaustive, bounded = self._run("cache miss", 7)
        markers = [hit.get("matched") for hit in bounded["hits"]]
        self.assertEqual(markers[:2], [None, None])
        self.assertEqual(markers[2:], ["all-terms"] * 5)
        self.assertTrue(bounded["totals_exact"])
        self._assert_lower_bounds(exhaustive, bounded)
        self.assertEqual(bounded["total"], len(exhaustive))

    def test_dense_thin_phrase_preflight_fills_from_all_terms(self):
        preflight = corpusdb.dense_phrase_preflight
        with mock.patch.object(corpusdb, "dense_candidate_lane", return_value=True), \
                mock.patch.object(corpusdb, "dense_phrase_preflight",
                                  wraps=preflight) as called:
            exhaustive, bounded = self._run("cache miss", 7)
        self.assertTrue(called.called)
        self.assertEqual([hit.get("matched") for hit in bounded["hits"]],
                         [None, None, *(["all-terms"] * 5)])
        self.assertGreater(len(exhaustive), 7)
        self._assert_lower_bounds(exhaustive, bounded)

    def test_dense_thin_phrase_session_preflight_fills_exact_heads(self):
        preflight = corpusdb.dense_phrase_preflight
        with mock.patch.object(corpusdb, "dense_candidate_lane", return_value=True), \
                mock.patch.object(corpusdb, "dense_phrase_preflight",
                                  wraps=preflight) as called:
            exhaustive, bounded = self._run_sessions("cache miss", 7)
        self.assertTrue(called.called)
        self.assertEqual([hit.get("matched") for hit in bounded["hits"]],
                         [None, None, *(["all-terms"] * 5)])
        self.assertGreater(len(exhaustive), 7)
        self._assert_lower_bounds(exhaustive, bounded)

    def test_dense_session_frontier_excludes_phrase_families(self):
        with mock.patch.object(corpusdb, "dense_candidate_lane", return_value=True):
            exhaustive, bounded = self._run_sessions("delta echo", 7)
        self.assertEqual([hit.get("matched") for hit in bounded["hits"]],
                         [None, None, *(["all-terms"] * 5)])
        self.assertEqual(len({hit["session"] for hit in bounded["hits"]}), 7)
        self.assertGreater(len(exhaustive), 7)
        self._assert_lower_bounds(exhaustive, bounded)

    def test_dense_full_phrase_session_page_still_runs_terms_once(self):
        real_candidates = corpusdb.score_ceiling_candidates
        yielded = 0

        def counted(*args, **kwargs):
            nonlocal yielded
            for item in real_candidates(*args, **kwargs):
                yielded += 1
                yield item

        with mock.patch.object(corpusdb, "dense_candidate_lane", return_value=True), \
                mock.patch.object(corpusdb, "score_ceiling_candidates", counted):
            exhaustive, bounded = self._run_sessions("phase lock", 4)
        self.assertEqual(self._shape(bounded["hits"]),
                         self._shape(search._session_heads(exhaustive, 4)))
        self.assertGreaterEqual(yielded, 1)
        self.assertLess(yielded, len(exhaustive))
        self.assertFalse(bounded["totals_exact"])

    def test_caller_family_exclusion_never_materializes_its_members(self):
        self.db.executemany(
            "INSERT INTO session_family(session, root, side) VALUES(?, ?, ?)",
            (("prune-phrase-000", "caller", 0),
             ("prune-phrase-001", "caller", 0)),
        )
        self.db.commit()
        _exhaustive, bounded = self._run_sessions(
            "phase lock", 4, {"exclude_session": "prune-phrase-000"})
        sessions = {hit["session"] for hit in bounded["hits"]}
        self.assertNotIn("prune-phrase-000", sessions)
        self.assertNotIn("prune-phrase-001", sessions)

    def test_recap_window_preserves_bounded_parity_and_family_rows(self):
        self.db.executemany(
            "INSERT INTO session_family(session, root, side) VALUES(?, ?, ?)",
            (("prune-phrase-000", "caller", 0),
             ("prune-phrase-001", "caller", 0)),
        )
        self.db.execute(
            corpusdb._INS,
            ("prune-phrase-000", 100, NOW_MS + 1000, "codex", "/repo/red",
             "", "gpt-5.4", "fixture", "user",
             "phase lock current window echo"),
        )
        self.db.commit()
        exhaustive, bounded = self._run_sessions(
            "phase lock", 8, {
                "exclude_session": "prune-phrase-000",
                "exclude_session_from_turn": 50,
            })
        sessions = {hit["session"] for hit in bounded["hits"]}
        self.assertIn("prune-phrase-000", sessions)
        self.assertIn("prune-phrase-001", sessions)
        self.assertNotIn(
            ("prune-phrase-000", 100),
            {(hit["session"], hit["turn"]) for hit in exhaustive},
        )

    def test_missing_family_sidecar_excludes_only_the_exact_caller(self):
        _exhaustive, bounded = self._run_sessions(
            "phase lock", 8, {"exclude_session": "prune-phrase-000"})
        sessions = {hit["session"] for hit in bounded["hits"]}
        self.assertNotIn("prune-phrase-000", sessions)
        self.assertIn("prune-phrase-001", sessions)

    def test_zero_phrase_uses_only_all_terms(self):
        exhaustive, bounded = self._run("vector shard", 6)
        self.assertGreater(len(exhaustive), 6)
        self.assertEqual({hit.get("matched") for hit in bounded["hits"]},
                         {"all-terms"})
        self.assertTrue(bounded["totals_exact"])
        self._assert_lower_bounds(exhaustive, bounded)
        self.assertEqual(bounded["total"], len(exhaustive))

    def test_mixed_lane_ties_keep_structural_order(self):
        _exhaustive, bounded = self._run("equal rank", 4)
        self.assertEqual([hit["session"] for hit in bounded["hits"]], [
            "tie-a-phrase", "tie-z-phrase", "tie-a-terms", "tie-z-terms",
        ])
        self.assertEqual([hit.get("matched") for hit in bounded["hits"]],
                         [None, None, "all-terms", "all-terms"])

    def test_filters_match_exhaustive_order_and_payload(self):
        filters = (
            {"who": "user"},
            {"include_tools": False},
            {"agent": "CODEX"},
            {"project": "BLUE"},
            {"chat": "filter-n"},
            {"model": "gpt-5.4"},
            {"model": "GPT-5", "model_soft": True},
            {"since_ms": NOW_MS - 4000},
            {"until_ms": NOW_MS - 2500},
        )
        for flt in filters:
            with self.subTest(flt=flt):
                exhaustive, bounded = self._run("filtered target", 4, flt)
                self.assertTrue(exhaustive)
                self._assert_lower_bounds(exhaustive, bounded)

    def test_unicode_folds_and_nul_widening_match_exhaustive(self):
        for query, sessions in (
                ("alpha hi", {"nul-phrase", "nul-terms", "short-interior"}),
                ("alpha sss", {"long-s-phrase", "long-s-terms"})):
            with self.subTest(query=query):
                exhaustive, bounded = self._run(query, 8)
                self.assertEqual({hit["session"] for hit in bounded["hits"]}, sessions)
                self.assertEqual(len(bounded["hits"]), len(exhaustive))


if __name__ == "__main__":
    unittest.main()
