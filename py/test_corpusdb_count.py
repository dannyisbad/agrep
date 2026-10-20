from __future__ import annotations

import math
import os
from pathlib import Path
import sqlite3
import tempfile
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

    def test_repeated_phrase_skips_rows_too_short_to_match(self) -> None:
        phrase = mock.Mock()
        phrase.search.side_effect = AssertionError("impossible phrase was scanned")
        with mock.patch.object(corpusdb.re, "compile", return_value=phrase):
            result = corpusdb.keyword_terms(
                self.db, "alpha " * 50, 100, position_order=False)
        self.assertEqual(
            {hit["session"] for hit in result["terms"]["hits"]},
            {"a", "b", "c"},
        )
        self.assertEqual(result["phrase"]["hits"], [])
        phrase.search.assert_not_called()

    def test_repeated_count_confirms_each_distinct_term_once(self) -> None:
        with mock.patch.object(
                corpusdb.common, "insensitive_span",
                wraps=corpusdb.common.insensitive_span) as span:
            result = corpusdb.keyword_count(self.db, "sss " * 50)
        self.assertEqual(result["total"], 2)
        self.assertEqual(corpusdb.count_tokens("sss " * 50), ["sss"])
        self.assertEqual(span.call_count, 2)

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


class CorpusCoverageCountTests(unittest.TestCase):
    QUERY = "alpha alpha beta hi absent"
    TERMS = ["beta", "alpha", "alpha", "hi", "absent"]

    @staticmethod
    def _row(session, turn, who, text, project="p"):
        return (session, turn, turn, "codex", project, "", "", "", who, text)

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory(prefix="agrep-coverage-count-")
        self.addCleanup(scratch.cleanup)
        self.path = Path(scratch.name) / "corpus.db"
        self.rows = [
            self._row("a", 0, "user", "alpha beta hi"),
            self._row("a", 1, "agent", "alpha detail"),
            self._row("a", 2, "agent", "alpha detail again"),
            self._row("b", 0, "agent", "alpha payload beta this"),
            self._row("c", 0, "tool", "alpha beta tool payload"),
            self._row("d", 0, "agent", "beta proof"),
            self._row("e", 0, "user", "ordinary evidence"),
            self._row("recap-text", 0, "recap", "recap payload"),
            self._row("a", 3, "recap", ""),
            self._row("recap-only", 0, "recap", ""),
        ] + [
            self._row(f"filler-{index}", 0, "agent", "filler", "other")
            for index in range(992)
        ]
        self._publish(self.path, self.rows)

    @staticmethod
    def _load(db, rows) -> None:
        db.executescript(corpusdb._SCHEMA_SQL)
        db.executemany(corpusdb._INS, rows)
        for table in ("msgs_fts", "msgs_prose_fts"):
            db.execute(f"INSERT INTO {table}({table}) VALUES('rebuild')")
        corpusdb._install_triggers(db)
        db.executemany("INSERT INTO meta VALUES(?, ?)", [
            ("schema", corpusdb._SCHEMA), ("stamp", "same-stamp"),
        ])
        db.commit()

    def _publish(self, path, rows) -> None:
        db = sqlite3.connect(path)
        try:
            self._load(db, rows)
        finally:
            db.close()

    def _reader(self, path=None, *, alias=False):
        db = (corpusdb._connect_read_alias(path or self.path, 0)
              if alias else corpusdb._open(path or self.path))
        self.addCleanup(db.close)
        return db

    @staticmethod
    def _term_counts(trace):
        return [sql for sql in trace
                if sql.startswith("SELECT count(") and " MATCH " in sql]

    @staticmethod
    def _population_counts(trace):
        return [sql for sql in trace
                if sql.startswith("SELECT count(") and " MATCH " not in sql
                and " FROM msgs" in sql]

    def _expected_hits(self, db, documents=1000, alpha=5, beta=4, *, prose=False):
        weights = {
            term: math.log(1.0 + (documents or 1) / (df + 1))
            for term, df in (("alpha", alpha), ("beta", beta),
                             ("hi", documents or 1), ("absent", 0))
        }
        total = sum(weights.values())
        table = "msgs_prose_fts" if prose else "msgs_fts"
        expected = []
        for session, turn, text in db.execute(
                f"SELECT m.session, m.turn, m.text FROM {table} "
                f"JOIN msgs m ON m.id = {table}.rowid WHERE {table} MATCH ? "
                f"ORDER BY bm25({table}) LIMIT 50",
                ('"alpha" OR "beta" OR "absent"',)):
            matched = [term for term in weights if term in text.lower()]
            mass = (round(sum(weights[term] for term in matched) / total, 4)
                    if documents >= 1000 else None)
            expected.append((session, turn, matched, mass))
        return expected

    def _assert_hits(self, hits, expected) -> None:
        self.assertEqual([
            (hit["session"], hit["turn"], hit["_terms_matched"], hit["_coverage_mass"])
            for hit in hits
        ], expected)
        self.assertTrue(all(not hit["_query_echo"] for hit in hits))
        for hit in hits:
            self.assertEqual(
                hit["_terms_missing"],
                [term for term in ("alpha", "beta", "hi", "absent")
                 if term not in hit["_terms_matched"]],
            )

    def test_session_df_shares_aggregate_with_coverage_across_connections(self) -> None:
        db = self._reader()
        expected = self._expected_hits(db)
        trace = []
        db.set_trace_callback(trace.append)
        db.execute("BEGIN")
        frequencies = corpusdb.term_session_df(db, self.TERMS)
        self.assertEqual(list(frequencies), ["beta", "alpha", "absent"])
        self.assertEqual(frequencies, {"beta": 4 / 999, "alpha": 3 / 999, "absent": 0.0})
        for _ in range(2):
            self._assert_hits(corpusdb.coverage_rank(db, self.QUERY, 50), expected)
        self.assertTrue(db.in_transaction)
        db.rollback()
        counts = self._term_counts(trace)
        self.assertEqual(len(counts), 3)
        self.assertTrue(all(
            sql.startswith("SELECT count(*), count(DISTINCT m.session)") for sql in counts))
        self.assertEqual(len(self._population_counts(trace)), 3)

        reopened = self._reader()
        trace.clear()
        reopened.set_trace_callback(trace.append)
        self.assertEqual(corpusdb.term_session_df(reopened, self.TERMS), frequencies)
        self._assert_hits(corpusdb.coverage_rank(reopened, self.QUERY, 50), expected)
        self.assertEqual(self._term_counts(trace), [])
        self.assertEqual(self._population_counts(trace), [])
        self.assertFalse(reopened.in_transaction)

    def test_coverage_first_does_not_precount_or_recount_sessions(self) -> None:
        db = self._reader()
        expected = self._expected_hits(db)
        trace = []
        db.set_trace_callback(trace.append)
        for _ in range(2):
            self._assert_hits(corpusdb.coverage_rank(db, self.QUERY, 50), expected)
        self.assertEqual(len(self._term_counts(trace)), 3)
        self.assertFalse(any(
            "count(DISTINCT" in sql for sql in trace if sql.startswith("SELECT")))
        for _ in range(2):
            self.assertEqual(corpusdb.term_session_df(db, self.TERMS), {
                "beta": 4 / 999, "alpha": 3 / 999, "absent": 0.0,
            })
        counts = self._term_counts(trace)
        self.assertEqual(len(counts), 6)
        self.assertEqual(sum(sql.startswith("SELECT count(*) FROM") for sql in counts), 3)
        self.assertEqual(sum(
            sql.startswith("SELECT count(DISTINCT m.session)") for sql in counts), 3)
        self.assertEqual(len(self._population_counts(trace)), 3)

    def test_content_and_coverage_share_row_counts_but_keep_exact_populations(self) -> None:
        for prose in (False, True):
            with self.subTest(prose=prose):
                path = self.path
                if prose:
                    path = self.path.with_name("prose.db")
                    self._publish(path, self.rows)
                db = self._reader(path)
                flt = {"include_tools": False} if prose else None
                alpha, beta = (4, 3) if prose else (5, 4)
                expected = self._expected_hits(db, alpha=alpha, beta=beta, prose=prose)
                documents = 999 if prose else 1000
                weights = {
                    term: math.log(1.0 + documents / (df + 1))
                    for term, df in (("alpha", alpha), ("beta", beta),
                                     ("hi", documents), ("absent", 0))
                }
                total = sum(weights.values())
                expected_content = sorted([
                    (session, turn, round(sum(weights[term] for term in matched) / total, 4))
                    for session, turn, matched, _mass in expected
                ], key=lambda row: (-row[2], row[0], row[1]))
                trace = []
                db.set_trace_callback(trace.append)
                for _ in range(2):
                    content = corpusdb.content(db, self.QUERY, 50, flt)
                    self.assertEqual([
                        (hit["session"], hit["turn"], hit["coverage"])
                        for hit in content["hits"]
                    ], expected_content)
                    self.assertEqual(content["total"], len(expected_content))
                    self.assertEqual(
                        content["chats"], len({row[0] for row in expected_content}))
                    self._assert_hits(
                        corpusdb.coverage_rank(db, self.QUERY, 50, flt), expected)
                counts = self._term_counts(trace)
                self.assertEqual(len(counts), 3)
                self.assertTrue(all(sql.startswith("SELECT count(*) FROM") for sql in counts))
                self.assertFalse(any(
                    "count(DISTINCT" in sql for sql in trace if sql.startswith("SELECT")))
                self.assertEqual(len(self._population_counts(trace)), 4 if prose else 2)

    def test_prose_row_df_and_all_message_session_df_stay_distinct(self) -> None:
        db = self._reader()
        expected = self._expected_hits(db, alpha=4, beta=3, prose=True)
        trace = []
        db.set_trace_callback(trace.append)
        for _ in range(2):
            self.assertEqual(corpusdb.term_session_df(db, self.TERMS), {
                "beta": 4 / 999, "alpha": 3 / 999, "absent": 0.0,
            })
            self._assert_hits(corpusdb.coverage_rank(
                db, self.QUERY, 50, {"include_tools": False, "project": "p"}), expected)
        counts = self._term_counts(trace)
        self.assertEqual(len(counts), 6)
        self.assertEqual(sum(" FROM msgs_fts " in sql for sql in counts), 3)
        self.assertEqual(sum(" FROM msgs_prose_fts " in sql for sql in counts), 3)
        self.assertFalse(any(
            "DISTINCT" in sql and " FROM msgs_prose_fts " in sql for sql in counts))
        self.assertEqual(len(self._population_counts(trace)), 3)

    def test_in_place_publication_change_invalidates_counts_and_population(self) -> None:
        db = self._reader()
        corpusdb.term_session_df(db, self.TERMS)
        corpusdb.coverage_rank(db, self.QUERY, 50)
        original_identity = db._source_identity
        writer = sqlite3.connect(self.path)
        try:
            writer.execute(corpusdb._INS, self._row("fresh", 0, "agent", "beta added"))
            writer.commit()
        finally:
            writer.close()
        frequencies = {"beta": 5 / 1000, "alpha": 3 / 1000, "absent": 0.0}
        expected = self._expected_hits(db, documents=1001, beta=5)
        self.assertEqual(corpusdb.term_session_df(db, self.TERMS), frequencies)
        self._assert_hits(corpusdb.coverage_rank(db, self.QUERY, 50), expected)

        fresh = self._reader()
        self.assertNotEqual(fresh._source_identity, original_identity)
        trace = []
        fresh.set_trace_callback(trace.append)
        self.assertEqual(corpusdb.term_session_df(fresh, self.TERMS), frequencies)
        for _ in range(2):
            self._assert_hits(corpusdb.coverage_rank(fresh, self.QUERY, 50), expected)
        self.assertEqual(len(self._term_counts(trace)), 3)
        self.assertEqual(len(self._population_counts(trace)), 3)

    def test_different_databases_and_replaced_path_do_not_share_wrong_counts(self) -> None:
        original = self._reader()
        expected = self._expected_hits(original)
        frequencies = corpusdb.term_session_df(original, self.TERMS)
        corpusdb.coverage_rank(original, self.QUERY, 50)
        other_path = self.path.with_name("other.db")
        self._publish(other_path, [self._row("only-beta", 0, "agent", "beta proof")])
        other = self._reader(other_path)
        other_expected = self._expected_hits(other, documents=1, alpha=0, beta=1)
        trace = []
        other.set_trace_callback(trace.append)
        self.assertEqual(corpusdb.term_session_df(other, self.TERMS), {
            "beta": 1.0, "alpha": 0.0, "absent": 0.0,
        })
        self._assert_hits(corpusdb.coverage_rank(other, self.QUERY, 50), other_expected)
        self.assertEqual(len(self._term_counts(trace)), 3)
        trace.clear()
        original.set_trace_callback(trace.append)
        self.assertEqual(corpusdb.term_session_df(original, self.TERMS), frequencies)
        self._assert_hits(corpusdb.coverage_rank(original, self.QUERY, 50), expected)
        self.assertEqual(self._term_counts(trace), [])

        other.close()
        original_identity = original._source_identity
        # Windows refuses to replace a database file another connection holds open
        held_open = os.name != "nt"
        if not held_open:
            original.close()
        other_path.replace(self.path)
        replacement = self._reader()
        self.assertNotEqual(replacement._source_identity, original_identity)
        self.assertEqual(corpusdb.term_session_df(replacement, self.TERMS), {
            "beta": 1.0, "alpha": 0.0, "absent": 0.0,
        })
        self._assert_hits(
            corpusdb.coverage_rank(replacement, self.QUERY, 50), other_expected)
        if held_open:
            self.assertEqual(corpusdb.term_session_df(original, self.TERMS), frequencies)
            self._assert_hits(corpusdb.coverage_rank(original, self.QUERY, 50), expected)

    def test_alias_cache_identity_includes_wal_not_only_main_file(self) -> None:
        writer = sqlite3.connect(self.path)
        self.addCleanup(writer.close)
        self.assertEqual(writer.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(corpusdb._INS, self._row("wal-first", 0, "agent", "beta first"))
        writer.commit()
        first = self._reader(alias=True)
        identity = first._source_identity
        trace = []
        first.set_trace_callback(trace.append)
        self.assertEqual(corpusdb.term_session_df(first, self.TERMS), {
            "beta": 5 / 1000, "alpha": 3 / 1000, "absent": 0.0,
        })
        self._assert_hits(
            corpusdb.coverage_rank(first, self.QUERY, 50),
            self._expected_hits(first, documents=1001, beta=5),
        )
        self.assertEqual(len(self._term_counts(trace)), 3)
        writer.execute(corpusdb._INS, self._row("wal-second", 0, "agent", "beta second"))
        writer.commit()
        second = self._reader(alias=True)
        self.assertEqual(second._source_identity, identity)
        trace.clear()
        second.set_trace_callback(trace.append)
        self.assertEqual(corpusdb.term_session_df(second, self.TERMS), {
            "beta": 6 / 1001, "alpha": 3 / 1001, "absent": 0.0,
        })
        self._assert_hits(
            corpusdb.coverage_rank(second, self.QUERY, 50),
            self._expected_hits(second, documents=1002, beta=6),
        )
        self.assertEqual(len(self._term_counts(trace)), 3)
        self.assertEqual(corpusdb.term_session_df(first, self.TERMS), {
            "beta": 5 / 1000, "alpha": 3 / 1000, "absent": 0.0,
        })

    def test_unpublished_memory_connections_observe_mutations(self) -> None:
        for factory in (sqlite3.Connection, corpusdb._PublishedConnection):
            with self.subTest(factory=factory):
                db = sqlite3.connect(":memory:", factory=factory)
                try:
                    self._load(db, self.rows)
                    self.assertEqual(corpusdb.term_session_df(db, self.TERMS), {
                        "beta": 4 / 999, "alpha": 3 / 999, "absent": 0.0,
                    })
                    self._assert_hits(
                        corpusdb.coverage_rank(db, self.QUERY, 50), self._expected_hits(db))
                    db.execute(corpusdb._INS, self._row("fresh", 0, "agent", "beta added"))
                    self.assertEqual(corpusdb.term_session_df(db, self.TERMS), {
                        "beta": 5 / 1000, "alpha": 3 / 1000, "absent": 0.0,
                    })
                    self._assert_hits(
                        corpusdb.coverage_rank(db, self.QUERY, 50),
                        self._expected_hits(db, documents=1001, beta=5),
                    )
                finally:
                    db.close()


if __name__ == "__main__":
    unittest.main()
