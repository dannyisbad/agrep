"""Adversarial parity checks spanning keyword and semantic engines."""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from _test_support import isolate_data_dir

isolate_data_dir()
import ask
import common
import corpusdb
import explore
import recall
import search
import semworker
import surface_policy as surface


class _Refs:
    def best_by_session(self, _scores, _filters):
        return {"summary-winner": 0, "message-winner": 1}

    def resolve(self, ordinals):
        rows = ({"session": "summary-winner", "turn": 10, "who": "user",
                 "text": "weak message evidence", "ts": 10},
                {"session": "message-winner", "turn": 20, "who": "user",
                 "text": "strong message evidence", "ts": 20})
        return [rows[int(index)] for index in ordinals]


class SearchCorrectnessTests(unittest.TestCase):
    def test_semantic_worker_never_coerces_malformed_turn_into_a_window(
            self) -> None:
        data = {
            "results": [
                {"session": "root", "turn": "7", "who": "user",
                 "text": "malformed current-window evidence", "score": 0.99},
                {"session": "root", "turn": 6, "who": "user",
                 "text": "older evidence", "score": 0.98},
            ],
            "truncated": False, "score_kind": "cosine",
            "semantic_coverage": {"indexed": 2, "total": 2,
                                  "complete": True},
            "partial": False,
        }
        with mock.patch.object(semworker, "resident_status",
                               return_value={"running": True}), \
                mock.patch.object(semworker, "search_worker",
                                  return_value=data):
            result = search._semantic_local(
                "deployment retry loop", 10,
                exclude_session="root", exclude_session_from_turn=7)
        self.assertEqual([hit["turn"] for hit in result["hits"]], [6])
        self.assertEqual(
            result["semantic_status"]["filtered"]["invalid"], 1)

    def test_window_exclusion_fails_open_on_malformed_turns_in_every_engine(
            self) -> None:
        rows = [
            {"session": "root", "turn": turn, "who": "user",
             "agent": "codex", "project": "agrep", "text": str(turn),
             "snippet": str(turn), "ts": 1}
            for turn in (6, 7, "7", 7.0, True, None)
        ]
        rows.append({
            "session": "other", "turn": 9, "who": "user",
            "agent": "codex", "project": "agrep", "text": "other",
            "snippet": "other", "ts": 1})
        filters = {
            "exclude_session": "root",
            "exclude_session_from_turn": 7,
            "who": "user",
        }

        semantic = search._filtered(
            rows, None, None, None, None, False,
            exclude_session="root", exclude_session_from_turn=7)
        self.assertEqual(
            [row["turn"] for row in semantic], [6, "7", 7.0, True, None, 9])

        filtered = [row for row in rows if ask._matches(dict(row), dict(filters))]
        self.assertEqual(
            [row["turn"] for row in filtered], [6, "7", 7.0, True, None, 9])

        source_rows = [
            {**row, "id": f"row-{index}"}
            for index, row in enumerate(rows)
        ]
        by_session = {
            session: [row for row in source_rows if row["session"] == session]
            for session in ("root", "other")
        }
        with mock.patch.object(
                explore, "_messages_by_session", return_value=by_session), \
                mock.patch.object(explore, "_session_concept", return_value={}):
            fallback = list(explore._iter_kw_corpus(dict(filters)))
        self.assertEqual(
            [row["turn"] for row in fallback], [6, "7", 7.0, True, None, 9])

    def test_recap_role_comes_from_ingest_provenance(self) -> None:
        pasted = {
            "who": "user",
            "text": "This session is being continued from a previous conversation",
        }
        structural = {"who": "recap", "text": "structural continuation"}
        self.assertEqual(search._stream_row_who(pasted), "user")
        self.assertEqual(search._stream_row_who(structural), "recap")

        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            messages = data / "messages.jsonl"
            messages.write_text(
                "\n".join(json.dumps({
                    "id": f"claude:s:{turn}",
                    "agent": "claude",
                    "project": "repo",
                    "session": "s",
                    "turn": turn,
                    "ts": turn,
                    **row,
                }) for turn, row in enumerate((pasted, structural))) + "\n",
                encoding="utf-8",
            )
            with mock.patch.object(common, "DATA_DIR", data), \
                    mock.patch.object(common, "MESSAGES_PATH", messages), \
                    mock.patch.object(common, "setting", return_value="off"):
                rows = corpusdb._scan()["s"]
        self.assertEqual([row[8] for row in rows], ["user", "recap"])

    def test_stream_row_who_reads_the_streamed_field_fail_closed(self) -> None:
        # No prose mirror: the streamed row's own
        # normalize-pass `who` decides, and anything else stays unknown.
        for who in ("user", "subagent", "synthetic", "control", "recap",
                    "harness"):
            self.assertEqual(search._stream_row_who({"who": who}), who)
        for row in ({}, {"who": None}, {"who": 7}, {"who": "operator"},
                    {"who": "x" * 4096, "text": "continue"}):
            self.assertEqual(search._stream_row_who(row), "unknown")

    def test_max_zero_help_discloses_classic_and_compact_limits(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(
                SystemExit) as stopped:
            search.main(["--help"])
        self.assertEqual(stopped.exception.code, 0)
        rendered = " ".join(output.getvalue().split())
        self.assertIn("0 = all keyword hits or up to 200 semantic chats", rendered)
        self.assertIn("compact: adaptive 4-16 rows within 3584 bytes", rendered)
        self.assertIn("explicit positive N requests up to 40 frozen rows", rendered)

    def test_json_fallback_uses_the_bounded_family_index(self) -> None:
        messages = {
            session: [{
                "id": f"codex:{session}:1",
                "agent": "codex",
                "session": session,
                "turn": 1,
                "ts": 1,
                "who": "user",
                "text": session,
            }]
            for session in ("root", "child", "other")
        }
        family = (
            "root", frozenset({"root", "child"}), frozenset({"child"}))
        with mock.patch.object(
                explore.common, "indexed_calling_family_with_sides",
                return_value=family), \
                mock.patch.object(
                    explore, "_messages_by_session", return_value=messages), \
                mock.patch.object(
                    explore, "_session_concept", return_value={}), \
                mock.patch.object(
                    explore.common, "setting", return_value="off"):
            rows = list(explore._iter_kw_corpus({
                "exclude_session": "child",
                "who": "user",
            }))
        self.assertEqual([row["session"] for row in rows], ["other"])

    def test_session_model_lookup_does_not_hydrate_chat_enrichment(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            explore._primary_models.cache_clear()
            try:
                with mock.patch.object(explore.common, "DATA_DIR", data):
                    db = sqlite3.connect(data / "corpus.db")
                    db.execute("CREATE TABLE meta(key TEXT, value TEXT)")
                    db.execute(
                        "CREATE TABLE msgs("
                        "session TEXT, model TEXT, turn INTEGER, who TEXT)")
                    db.executemany("INSERT INTO msgs VALUES(?,?,?,?)", [
                        ("s1", "z-model", 1, "user"),
                        ("s1", "z-model", 2, "user"),
                        ("s1", "a-model", 3, "user"),
                        ("s1", "a-model", 3, "agent"),
                        ("s1", "<recap>", 4, "recap"),
                        ("s2", "z-model", 1, "user"),
                        ("s2", "a-model", 2, "user"),
                    ])
                    db.executemany("INSERT INTO meta VALUES(?,?)", [
                        ("schema", corpusdb._SCHEMA),
                        ("stamp", corpusdb._stamp()),
                    ])
                    db.commit()
                    db.close()
                with mock.patch.object(explore, "_freshen"), \
                        mock.patch.object(explore.common, "DATA_DIR", data), \
                        mock.patch.object(
                            explore, "_messages_by_session",
                            side_effect=AssertionError("message bodies retained")):
                    self.assertEqual(
                        explore.session_models(["s1", "s2", "missing", "s1"]), {
                            "s1": "z-model", "s2": "a-model", "missing": "",
                        })
            finally:
                explore._primary_models.cache_clear()

    def test_artifact_cache_retries_a_moving_publication(self):
        key = "moving-artifact-test"
        with tempfile.TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "artifact"
            artifact.write_text("old", encoding="utf-8")
            calls = []

            def build():
                calls.append(artifact.read_text(encoding="utf-8"))
                if len(calls) == 1:
                    artifact.write_text("new-generation", encoding="utf-8")
                return {"generation": len(calls)}

            ask._CACHE.pop(key, None)
            try:
                self.assertEqual(
                    ask._cached(key, (artifact,), build), {"generation": 2})
                self.assertEqual(calls, ["old", "new-generation"])
                self.assertEqual(
                    ask._CACHE[key][0], ask._artifact_stamp((artifact,)))
            finally:
                ask._CACHE.pop(key, None)

    def test_semantic_chat_search_survives_model_enrichment_failure(self):
        records = {"s1": {"agent": "codex", "summary": "answer"}}
        with mock.patch.object(ask.common, "read_index_meta", return_value=(1, None)), \
                mock.patch.object(ask, "_embed_query",
                                  return_value=np.asarray([1.0], dtype=np.float32)), \
                mock.patch.object(ask, "_summary_artifacts", return_value=(
                    ["s1"], np.asarray([[1.0]], dtype=np.float32), records,
                )), \
                mock.patch.object(ask, "_family_diversity_enabled", return_value=False), \
                mock.patch.object(explore, "_session_concept", return_value={}), \
                mock.patch.object(explore, "session_models", side_effect=OSError("moved")):
            payload = json.loads(ask.tool_search_chats("answer", envelope=True))
        self.assertEqual(payload["results"][0]["session"], "s1")
        self.assertEqual(payload["results"][0]["model"], "")

    def test_semantic_top_k_uses_stable_row_order_for_equal_scores(self):
        scores = np.ones(40, dtype=np.float32)
        np.testing.assert_array_equal(ask._top_indices(scores, 5), np.arange(5))
        mixed = np.asarray([0.5, 1.0, 1.0, 0.7, 1.0], dtype=np.float32)
        np.testing.assert_array_equal(ask._top_indices(mixed, 4), [1, 2, 4, 3])

    def test_recall_expansion_queries_return_only_bounded_metadata(self):
        db = sqlite3.connect(":memory:")
        db.executescript(corpusdb._SCHEMA_SQL)
        rows = [
            ("s1", 1, 1, "codex", "p", "", "", "", "user",
             "bluetooth failed " + "x" * 2000),
            ("s1", 2, 2, "codex", "p", "", "", "", "agent",
             "retry bluetooth"),
            ("s2", 1, 3, "codex", "p", "", "", "", "user", "unrelated"),
        ]
        try:
            db.executemany(corpusdb._INS, rows)
            sizes = corpusdb.session_text_sizes(db, ["s1", "s2"])
            turns = corpusdb.session_term_turns(
                db, ["s1", "s2"], ["bluetooth", "retry"], 8)
        finally:
            db.close()
        self.assertGreater(sizes["s1"], 2000)
        self.assertEqual([(row["session"], row["turn"], row["term_hits"])
                          for row in turns], [("s1", 2, 2), ("s1", 1, 1)])
        self.assertTrue(all(set(row) == {"session", "turn", "ts", "term_hits"}
                            for row in turns))

    def test_recall_expansion_applies_recap_bound_before_limit_and_sum(self):
        db = sqlite3.connect(":memory:")
        db.executescript(corpusdb._SCHEMA_SQL)
        old_text = "old eligible bluetooth evidence"
        rows = [
            ("s1", 5, 5, "codex", "p", "", "", "", "user", old_text),
            *[
                ("s1", turn, 1000 + turn, "codex", "p", "", "", "", "user",
                 "bluetooth retry current echo")
                for turn in range(10, 310)
            ],
        ]
        try:
            db.executemany(corpusdb._INS, rows)
            unbounded = corpusdb.session_term_turns(
                db, ["s1"], ["bluetooth", "retry"], 4)
            bounded = corpusdb.session_term_turns(
                db, ["s1"], ["bluetooth", "retry"], 4,
                before_turns={"s1": 10})
            sizes = corpusdb.session_text_sizes(
                db, ["s1"], before_turns={"s1": 10})
        finally:
            db.close()
        self.assertNotIn(5, [row["turn"] for row in unbounded])
        self.assertEqual([row["turn"] for row in bounded], [5])
        self.assertEqual(sizes, {"s1": len(old_text)})

    def test_whole_word_literals_with_punctuation_match_grep_edges(self):
        rows = [
            ("cpp-alone", 0, 1, "codex", "p", "", "", "", "user", "use C++ here"),
            ("cpp-prefix", 0, 2, "codex", "p", "", "", "", "user", "xC++ here"),
            ("cpp-suffix", 0, 3, "codex", "p", "", "", "", "user", "C++x here"),
            ("scope-alone", 0, 4, "codex", "p", "", "", "", "user", "use ::x here"),
            ("scope-prefix", 0, 5, "codex", "p", "", "", "", "user", "a::x here"),
            ("scope-suffix", 0, 6, "codex", "p", "", "", "", "user", "::xy here"),
        ]
        db = sqlite3.connect(":memory:")
        db.executescript(corpusdb._SCHEMA_SQL)
        db.executemany(corpusdb._INS, rows)
        db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
        db.execute("INSERT INTO msgs_prose_fts(rowid,text) "
                   "SELECT id,text FROM msgs WHERE who <> 'tool'")
        legacy = [{"session": row[0], "turn": row[1], "ts": row[2],
                   "agent": row[3], "project": row[4], "concept": row[5],
                   "model": row[6], "model_source": row[7], "who": row[8],
                   "text": row[9], "low": row[9].lower()} for row in rows]
        try:
            with mock.patch.object(explore, "_iter_kw_corpus",
                                   side_effect=lambda _flt=None: iter(legacy)):
                for query, expected in (("C++", {"cpp-alone"}),
                                        ("::x", {"scope-alone"})):
                    with self.subTest(query=query):
                        indexed = {hit["session"] for hit in
                                   corpusdb.word(db, query, 99)["hits"]}
                        scanned = {hit["session"] for hit in
                                   search._word_scan(query, 99)["hits"]}
                        self.assertEqual(indexed, expected)
                        self.assertEqual(scanned, expected)
                        self.assertIsNotNone(search._match_pat(query, "word").search(
                            next(row[9] for row in rows if row[0] in expected)))
        finally:
            db.close()

    def test_semantic_policy_truncation_reaches_top_level(self):
        rows = [{"session": f"s-{index}", "turn": index, "who": "user",
                 "text": f"deployment retry evidence {index}", "score": 0.9}
                for index in range(20)]
        data = {
            "results": rows, "truncated": False, "score_kind": "cosine",
            "semantic_coverage": {"indexed": 20, "total": 20, "complete": True},
            "partial": False,
        }
        with mock.patch.object(semworker, "search_worker", return_value=data):
            result = search._semantic_local(
                "why did deployment keep retrying", 10, who="user")
        self.assertIsNotNone(result)
        self.assertEqual(len(result["hits"]), 10)
        self.assertTrue(result["truncated"])
        self.assertTrue(result["semantic_status"]["truncated"])

    def test_partial_semantic_coverage_never_claims_exact_totals(self):
        response = {
            "hits": [], "total": 0, "chats": 0, "truncated": False,
            "semantic_coverage": {"indexed": 2, "total": 10, "complete": False},
            "partial": True, "score_kind": "cosine",
            "semantic_status": {"state": "no-confident-match", "complete": False,
                                "partial": True, "truncated": False},
            "fallback_recommended": False,
        }
        with mock.patch.object(search, "_semantic_local", return_value=response):
            result = search.run_query(
                "why did deployment keep retrying", mode="semantic", limit=10)
        self.assertIsNotNone(result)
        self.assertFalse(result["totals_exact"])
        self.assertTrue(result["partial"])

    def test_hidden_semantic_window_never_claims_complete(self):
        self.assertTrue(search._semantic_result_incomplete({
            "self_exclusion_more_unknown": True,
        }))

    def test_semantic_time_sort_fetches_full_bounded_pool_before_cap(self):
        hits = [{"session": f"session-{index}", "turn": index, "who": "user",
                 "ts": index, "snippet": f"meaning {index}",
                 "sem_score": 0.99 - index / 100}
                for index in range(6)]
        response = {
            "hits": hits, "truncated": False, "score_kind": "cosine",
            "semantic_coverage": {"indexed": 6, "total": 6, "complete": True},
            "partial": False,
            "semantic_status": {"state": "ready", "complete": True},
            "fallback_recommended": False,
        }
        with mock.patch.object(search, "_semantic_local",
                               return_value=response) as semantic_local:
            result = search.run_query(
                "deployment retry loop", mode="semantic", limit=2, sort="time")
        self.assertEqual(semantic_local.call_args.args[1], search.SEMANTIC_MAX_RESULTS)
        self.assertEqual([hit["session"] for hit in result["hits"]],
                         ["session-5", "session-4"])

    def test_semantic_position_sort_is_rejected(self):
        with mock.patch.object(search.indexd_runtime, "ensure_index", return_value=True), \
                self.assertRaises(SystemExit) as stopped:
            search.main(["deployment retry loop", "-s", "--sort", "position"])
        self.assertEqual(stopped.exception.code, 2)

    def test_partial_zero_confidence_requests_lexical_fallback(self):
        data = {
            "results": [{"session": "weak", "turn": 1, "who": "user",
                         "text": "unrelated", "score": 0.2}],
            "truncated": False,
            "score_kind": "cosine",
            "semantic_coverage": {"indexed": 10, "total": 100,
                                  "complete": False},
            "partial": True,
        }
        with mock.patch.object(semworker, "search_worker", return_value=data):
            result = search._semantic_local("deployment retry loop", 10)
        self.assertIsNotNone(result)
        self.assertEqual(result["hits"], [])
        self.assertTrue(result["fallback_recommended"])
        self.assertTrue(result["semantic_status"]["fallback_recommended"])
        self.assertFalse(result["semantic_status"]["complete"])

        complete = search.semantic_result_policy(
            "deployment retry loop", [], requested=10,
            coverage={"indexed": 100, "total": 100, "complete": True})
        self.assertFalse(complete["semantic_status"]["fallback_recommended"])

    def test_terse_prose_miss_is_semantic_but_identifiers_and_junk_are_not(self):
        self.assertTrue(recall._auto_semantic_query("deployment retry loop"))
        self.assertTrue(recall._auto_semantic_query("build failure"))
        self.assertFalse(recall._auto_semantic_query("REPLY_CAP MAX_ROWS"))
        self.assertFalse(recall._auto_semantic_query(
            "qzxvplmbrt asdfghjkl abcdefghijkz"))

    def test_default_semantic_noise_filter_precedes_candidate_cutoff(self):
        calls = []

        def worker(_query, *, level, k, filters):
            calls.append((level, k, filters))
            return {
                "results": [], "truncated": False, "score_kind": "cosine",
                "semantic_coverage": {"indexed": 4, "total": 4, "complete": True},
                "partial": False,
            }

        with mock.patch.object(semworker, "search_worker", side_effect=worker):
            search._semantic_local("why did deployment keep retrying", 10)
            search._semantic_local(
                "why did deployment keep retrying", 10, who="control")
            search._semantic_local(
                "why did deployment keep retrying",
                10,
                exclude_session="child",
            )

        excluded = calls[0][2]["_exclude_who"]
        self.assertEqual(
            set(excluded), set(common.SEMANTIC_DEFAULT_EXCLUDED_ROLES))
        self.assertNotIn("_exclude_who", calls[1][2])
        self.assertEqual(calls[1][2]["who"], "control")
        self.assertEqual(calls[2][2]["exclude_session"], "child")
        where, params = ask._MessageRefStore._where({"_exclude_who": excluded})
        self.assertIn("who NOT IN", where)
        self.assertEqual(set(params), set(excluded))
        self.assertFalse(ask._matches(
            {"who": "control"}, {"_exclude_who": excluded}))
        clean = semworker._validate_request({
            "query": "why did deployment keep retrying", "level": "hybrid", "k": 10,
            "filters": {"_exclude_who": excluded},
        })[3]
        self.assertEqual(set(clean["_exclude_who"]), set(excluded))

        family_filters = {"exclude_session": "child"}
        with mock.patch.object(
                ask.common,
                "indexed_calling_family",
                return_value=("root", frozenset({"child", "root"}))):
            where, params = ask._MessageRefStore._where(family_filters)
        self.assertIn(
            "session NOT IN (SELECT value FROM json_each(?))", where)
        self.assertEqual(params, ["child", '["child","root"]'])
        self.assertFalse(ask._matches({"session": "child"}, family_filters))
        self.assertTrue(ask._matches({"session": "past"}, family_filters))
        sql, params = corpusdb._filter_sql(family_filters)
        self.assertEqual(sql, [
            "session <> ?",
            "session NOT IN (SELECT member.session FROM session_family AS member "
            "JOIN session_family AS caller ON caller.root=member.root "
            "WHERE caller.session=?)",
        ])
        self.assertEqual(params, ["child", "child"])

    def test_multi_hidden_speaker_filter_reaches_semantic_selection(self):
        captured = {}

        def worker(_query, *, level, k, filters):
            captured.update(filters)
            return {
                "results": [
                    {"session": "control-chat", "turn": 1,
                     "who": "control", "score": 0.9, "text": "control row"},
                    {"session": "synthetic-chat", "turn": 2,
                     "who": "synthetic", "score": 0.88,
                     "text": "synthetic row"},
                ],
                "truncated": False,
                "score_kind": "cosine",
                "semantic_coverage": {
                    "indexed": 2, "total": 2, "complete": True},
                "partial": False,
            }

        who = surface.speaker_filter(
            "control,synthetic", None, surface.SEARCH_SPEAKER_CHOICES)
        with mock.patch.object(semworker, "search_worker", side_effect=worker):
            result = search._semantic_local(
                "why did deployment keep retrying", 10, who=who)

        self.assertEqual(
            captured["_include_who"], ("control", "synthetic"))
        self.assertNotIn("_exclude_who", captured)
        self.assertEqual(
            {hit["who"] for hit in result["hits"]}, {"control", "synthetic"})
        clean = semworker._validate_request({
            "query": "why did deployment keep retrying",
            "level": "hybrid",
            "k": 10,
            "filters": {"_include_who": captured["_include_who"]},
        })[3]
        self.assertEqual(
            clean["_include_who"], ("control", "synthetic"))

    def test_summary_similarity_cannot_score_an_unrelated_message_turn(self):
        messages = np.asarray([[0.20, 0.0], [0.85, 0.0]], dtype=np.float32)
        summaries = np.asarray([[0.99, 0.0]], dtype=np.float32)
        summary_rows = {"summary-winner": {
            "title": "high scoring summary", "summary": "summary-only evidence"}}
        with mock.patch.object(ask.common, "read_index_meta",
                               return_value=(2, "fixture")), \
                mock.patch.object(ask, "_guard_embedder"), \
                mock.patch.object(ask, "_embed_query",
                                  return_value=np.asarray([1.0, 0.0], dtype=np.float32)), \
                mock.patch.object(ask, "_message_artifacts",
                                  return_value=(["m0", "m1"], messages, _Refs(),
                                                {"complete": True})), \
                mock.patch.object(ask, "_summary_artifacts",
                                  return_value=(["summary-winner"], summaries,
                                                summary_rows)), \
                mock.patch.object(ask, "_family_diversity_enabled", return_value=False), \
                mock.patch("explore._session_concept", return_value={}):
            payload = json.loads(ask.tool_search_hybrid("query", 2))
        rows = payload["results"]
        self.assertEqual([row["session"] for row in rows],
                         ["message-winner", "summary-winner"])
        self.assertEqual([row["score"] for row in rows], [0.85, 0.2])
        self.assertEqual(rows[1]["turn"], 10)
        self.assertEqual(rows[1]["text"], "weak message evidence")
        self.assertEqual(rows[1]["semantic_source"], "message")
        self.assertEqual(rows[1]["summary"], "summary-only evidence")

    def test_corrupt_optional_summary_bundle_does_not_break_message_semantics(self):
        messages = np.asarray([[0.85, 0.0], [0.20, 0.0]], dtype=np.float32)
        with mock.patch.object(ask.common, "read_index_meta",
                               return_value=(2, "fixture")), \
                mock.patch.object(ask, "_guard_embedder"), \
                mock.patch.object(ask, "_embed_query",
                                  return_value=np.asarray([1.0, 0.0], dtype=np.float32)), \
                mock.patch.object(ask, "_message_artifacts",
                                  return_value=(["m0", "m1"], messages, _Refs(),
                                                {"complete": True})), \
                mock.patch.object(ask, "_summary_artifacts",
                                  side_effect=RuntimeError("corrupt optional summary")), \
                mock.patch.object(ask, "_family_diversity_enabled", return_value=False), \
                mock.patch("explore._session_concept", return_value={}):
            payload = json.loads(ask.tool_search_hybrid("query", 2))
        self.assertEqual([row["session"] for row in payload["results"]],
                         ["summary-winner", "message-winner"])
        self.assertTrue(all(row["semantic_source"] == "message"
                            for row in payload["results"]))
        self.assertTrue(all(row["summary"] == "" for row in payload["results"]))

    def test_pack_forced_semantic_mode_is_independent_per_query(self):
        calls: list[tuple[str, str]] = []

        def run_query(query, *, mode, **_kwargs):
            calls.append((query, mode))
            if query == "first" and mode == "semantic":
                return {"hits": [], "total": 0, "chats": 0,
                        "engine": "semantic:hybrid", "fallback_recommended": True,
                        "semantic_status": {"state": "query-rejected",
                                            "fallback_recommended": True,
                                            "complete": False}}
            if query == "second" and mode == "semantic":
                return {"hits": [{"session": "semantic-session", "turn": 2,
                                   "ts": 2, "who": "user", "agent": "codex",
                                   "project": "p", "sem_score": 0.9,
                                   "score": 0.9, "snippet": "meaning evidence"}],
                        "total": 1, "chats": 1, "engine": "semantic:hybrid",
                        "semantic_status": {"state": "ready",
                                            "fallback_recommended": False,
                                            "complete": True},
                        "semantic_coverage": {"indexed": 1, "total": 1,
                                              "complete": True},
                        "score_kind": "cosine"}
            return {"hits": [], "total": 0, "chats": 0,
                    "engine": "semantic:hybrid" if mode == "semantic" else "corpusdb",
                    "semantic_coverage": {"complete": True},
                    "semantic_status": {}, "score_kind": "cosine"}

        window = {"session": "semantic-session", "center": 2, "agent": "codex",
                  "project": "p", "first_turn": 2, "last_turn": 2,
                  "turns": [{"turn": 2, "ts": 2, "who": "user",
                             "text": "meaning evidence", "reply": ""}],
                  "events": []}
        stdout = io.StringIO()
        with mock.patch.object(recall.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(recall.search, "run_query", side_effect=run_query), \
                mock.patch.object(recall.explore, "get_windows", return_value=[window]), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = recall.main(
                ["first", "second", "-s", "--who", "user", "--json", "--budget", "0"],
                prog="pack")
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [("first", "semantic"), ("second", "semantic")])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["engine"], "semantic:policy+semantic:hybrid")
        self.assertEqual(payload["hits"][0]["lane"], "semantic")
        self.assertEqual(payload["semantic_status"]["state"], "mixed")
        self.assertEqual([row["status"]["state"]
                          for row in payload["semantic_queries"]],
                         ["query-rejected", "ready"])

    def _run_recall_tool_fallback(self, prose_matched):
        """Drive recall.main with a prose page of `prose_matched` rows and a
        verbatim tool corpus; return (run_query calls, parsed JSON payload)."""
        calls: list[dict] = []
        prose_hits = [{"session": f"prose-{i}", "turn": i, "ts": 20 - i,
                       "who": "user", "agent": "codex", "project": "p",
                       "score": 9.0, "matched": matched}
                      for i, matched in enumerate(prose_matched, start=1)]
        tool_hits = [{"session": f"tool-{i}", "turn": i, "ts": 10 - i,
                      "who": "tool", "agent": "codex", "project": "p",
                      "score": 5.0,
                      "snippet": "bind: address already in use"}
                     for i in (1, 2)]

        def run_query(query, *, mode, **kwargs):
            calls.append({"query": query, "mode": mode, "who": kwargs.get("who"),
                          "include_tools": kwargs.get("include_tools")})
            hits = tool_hits if kwargs.get("who") == "tool" else prose_hits
            return {"hits": [dict(h) for h in hits], "total": len(hits),
                    "chats": len(hits), "tool_hits": 0, "engine": "corpusdb"}

        def get_windows(requests):
            return [{"session": s, "center": t, "agent": "codex", "project": "p",
                     "first_turn": t, "last_turn": t, "events": [],
                     "turns": [{"turn": t, "ts": 1, "who": "user",
                                "text": "evidence", "reply": ""}]}
                    for s, t, _ in requests]

        stdout = io.StringIO()
        with mock.patch.object(recall.indexd_runtime, "ensure_index",
                               return_value=True), \
                mock.patch.object(recall.search, "_semantic_runtime_installed",
                                  return_value=False), \
                mock.patch.object(recall.search, "run_query",
                                  side_effect=run_query), \
                mock.patch.object(recall.explore, "get_windows",
                                  side_effect=get_windows), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = recall.main(["address already in use", "--json",
                              "--budget", "0"], prog="recall")
        self.assertEqual(rc, 0)
        return calls, json.loads(stdout.getvalue())

    def test_recall_tool_lane_fires_when_prose_fill_is_weak_scatter(self):
        # B1 repro: prose fills the target with bag-of-words scatter while tool
        # sessions hold the phrase verbatim - the tool lane must run and its
        # strong hits must outrank every weak prose row in the pack.
        calls, payload = self._run_recall_tool_fallback(
            ["all-terms", "all-terms", "all-terms"])
        self.assertEqual([c["who"] for c in calls], [None, "tool"])
        self.assertIs(calls[0]["include_tools"], False)
        self.assertEqual([hit["session"] for hit in payload["hits"]],
                         ["tool-1", "tool-2", "prose-1"])

    def test_recall_tool_lane_skipped_on_strong_prose_fill(self):
        # Strong prose filling every requested chat still skips the larger
        # tool corpus - the strength gate only widens the weak-scatter case.
        calls, payload = self._run_recall_tool_fallback([None, None, None])
        self.assertEqual([c["who"] for c in calls], [None])
        self.assertEqual([hit["session"] for hit in payload["hits"]],
                         ["prose-1", "prose-2", "prose-3"])

    def test_recall_merge_order_pins_strength_then_lane_hierarchy(self):
        # The merged page order: strong evidence first, prose > semantic > tool
        # within a strength band, weak scatter after every strong lane.
        rows = [
            {"session": "weak-tool", "turn": 1, "score": 9.9,
             "matched": "all-terms", "_recall_lane": 2},
            {"session": "weak-prose-all", "turn": 1, "score": 9.9,
             "matched": "all-terms", "_recall_lane": 0},
            {"session": "weak-prose-content", "turn": 1, "score": 9.9,
             "matched": "content-terms", "_recall_lane": 0},
            {"session": "strong-tool", "turn": 1, "score": 1.0,
             "_recall_lane": 2},
            {"session": "strong-semantic", "turn": 1, "score": 1.0,
             "sem_score": 0.99, "_recall_lane": 1},
            {"session": "strong-prose", "turn": 1, "score": 1.0,
             "_recall_lane": 0},
        ]
        self.assertEqual(
            [row["session"] for row in sorted(rows, key=recall._merge_key)],
            ["strong-prose", "strong-semantic", "strong-tool",
             "weak-prose-content", "weak-prose-all", "weak-tool"])

    def test_corpusdb_and_jsonl_share_python_re_i_unicode_semantics(self):
        rows = [
            ("dotless", 0, 1, "codex", "p", "", "", "", "user",
             "dotless ıst token"),
            ("long-s", 0, 2, "codex", "p", "", "", "", "user",
             "long ſſſ token"),
            ("decomposed", 0, 3, "codex", "p", "", "", "", "user",
             "decomposed I\u0307 marker"),
            ("noise-s", 0, 4, "codex", "p", "", "", "", "user",
             "one s here"),
            ("noise-i", 0, 5, "codex", "p", "", "", "", "user",
             "unrelated ı noise"),
        ]
        db = sqlite3.connect(":memory:")
        db.executescript(corpusdb._SCHEMA_SQL)
        db.executemany(corpusdb._INS, rows)
        db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
        db.execute("INSERT INTO msgs_prose_fts(rowid,text) "
                   "SELECT id,text FROM msgs WHERE who <> 'tool'")
        db.executescript(corpusdb._TRIGGERS_SQL)
        legacy = [{"session": session, "turn": turn, "ts": ts, "agent": agent,
                   "project": project, "concept": concept, "model": model,
                   "model_source": source, "who": who, "text": text,
                   "low": text.lower()}
                  for session, turn, ts, agent, project, concept, model,
                  source, who, text in rows]
        try:
            with mock.patch.object(explore, "_freshen"), \
                    mock.patch.object(explore, "_iter_kw_corpus",
                                      side_effect=lambda _flt=None: iter(legacy)):
                for query, expected in (("ist", {"dotless"}),
                                        ("sss", {"long-s"}),
                                        ("İ", {"dotless", "decomposed", "noise-i"})):
                    with self.subTest(query=query):
                        indexed = corpusdb.keyword(db, query, 99)["hits"]
                        scanned = explore.keyword_search(query, 99)["hits"]
                        got = {hit["session"] for hit in indexed}
                        self.assertEqual(got, {hit["session"] for hit in scanned})
                        self.assertEqual(got, expected)
        finally:
            db.close()

    def test_unicode_metadata_filters_match_jsonl(self):
        row = ("sess-Ä", 0, 1, "CÖDEX", "/tmp/ÄProject", "", "MÖDEL",
               "explicit", "user", "needle token")
        db = sqlite3.connect(":memory:")
        db.executescript(corpusdb._SCHEMA_SQL)
        db.execute(corpusdb._INS, row)
        db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
        db.execute("INSERT INTO msgs_prose_fts(rowid,text) SELECT id,text FROM msgs")
        legacy = [{"session": row[0], "turn": row[1], "ts": row[2],
                   "agent": row[3], "project": row[4], "concept": row[5],
                   "model": row[6], "model_source": row[7], "who": row[8],
                   "text": row[9], "low": row[9].lower()}]
        try:
            with mock.patch.object(explore, "_freshen"), \
                    mock.patch.object(explore, "_iter_kw_corpus",
                                      side_effect=lambda flt=None: iter(
                                          search._filtered(legacy, flt.get("agent"),
                                                           flt.get("project"), flt.get("who"),
                                                           flt.get("model"),
                                                           flt.get("model_soft", False),
                                                           flt.get("chat")))):
                filters = ({"project": "äproject"}, {"agent": "cödex"},
                           {"model": "mödel"},
                           {"model": "öde", "model_soft": True},
                           {"chat": "SESS-ä"})
                for flt in filters:
                    with self.subTest(filter=flt):
                        indexed = corpusdb.keyword(db, "needle", 10, flt)["hits"]
                        scanned = explore.keyword_search("needle", 10, flt)["hits"]
                        self.assertEqual({hit["session"] for hit in indexed},
                                         {hit["session"] for hit in scanned})
                        self.assertEqual(len(indexed), 1)
        finally:
            db.close()

    def test_re_i_widening_uses_the_sparse_partial_index(self):
        db = sqlite3.connect(":memory:")
        db.executescript(corpusdb._SCHEMA_SQL)
        try:
            where, params = corpusdb._candidate_where(["indexing"], None)
            plan = " ".join(str(cell) for row in db.execute(
                "EXPLAIN QUERY PLAN SELECT id FROM msgs" + where, params)
                            for cell in row)
            self.assertIn("msgs_re_i_exceptions", plan)
        finally:
            db.close()


class CorpusAnchorTests(unittest.TestCase):
    """Multi-word out-of-vocabulary mush clears the strong band on embedding
    hubness alone. Zero anchored words demotes every label and says so once; it
    never refuses the page and never changes an exit code."""

    QUERY = "zqxjklwvutplmb frobnicated quuxstring"

    def _corpus(self) -> sqlite3.Connection:
        db = sqlite3.connect(":memory:")
        db.executescript(corpusdb._SCHEMA_SQL)
        db.executemany(corpusdb._INS, [
            ("lived", 4, 10, "claude", "p", "", "", "", "user",
             "the deployment kept retrying after the publication race"),
        ])
        db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
        db.execute("INSERT INTO msgs_prose_fts(msgs_prose_fts) VALUES('rebuild')")
        return db

    @contextlib.contextmanager
    def _probe(self):
        db = self._corpus()
        try:
            fake = mock.Mock(wraps=corpusdb)
            fake.connect.return_value = db
            with mock.patch.object(search, "corpusdb", fake):
                yield fake
        finally:
            db.close()

    def _semantic_rows(self, score: float = 0.95) -> dict:
        return {
            "results": [{"session": f"s-{index}", "turn": index, "who": "user",
                         "text": f"unrelated neighbor {index}", "score": score}
                        for index in range(3)],
            "truncated": False, "score_kind": "cosine",
            "semantic_coverage": {"indexed": 3, "total": 3, "complete": True},
            "partial": False,
        }

    def test_zero_anchor_query_demotes_every_label_and_warns_once(self):
        with self._probe(), mock.patch.object(
                semworker, "search_worker", return_value=self._semantic_rows()):
            result = search._semantic_local(self.QUERY, 10, who="user")
        status = result["semantic_status"]
        self.assertEqual(status["corpus_anchor"],
                         {"anchored": False, "probed": 3})
        self.assertEqual(status["state"], "ready")
        self.assertTrue(result["hits"])
        for hit in result["hits"]:
            self.assertTrue(search._semantic_row_weak(hit))
        self.assertIn("no query word appears in indexed history",
                      surface.semantic_anchor_notice(status))

    def test_anchored_query_keeps_confident_labels_and_stays_quiet(self):
        with self._probe(), mock.patch.object(
                semworker, "search_worker", return_value=self._semantic_rows()):
            result = search._semantic_local(
                "publication race retrying", 10, who="user")
        status = result["semantic_status"]
        self.assertEqual(status["corpus_anchor"]["anchored"], True)
        self.assertTrue(result["hits"])
        for hit in result["hits"]:
            self.assertFalse(search._semantic_row_weak(hit))
        self.assertIsNone(surface.semantic_anchor_notice(status))

    def test_sub_strong_scores_stay_weak_when_the_query_anchors(self):
        rows = self._semantic_rows(score=search._RECALL_STRONG_SEM - 0.01)
        with self._probe(), mock.patch.object(
                semworker, "search_worker", return_value=rows):
            result = search._semantic_local(
                "publication race retrying", 10, who="user")
        self.assertTrue(result["hits"])
        for hit in result["hits"]:
            self.assertTrue(search._semantic_row_weak(hit))
        self.assertIsNone(
            surface.semantic_anchor_notice(result["semantic_status"]))

    def test_probe_stops_at_eight_words(self):
        query = " ".join(f"zzqx{letter}mush" for letter in "abcdefghijkl")
        with self._probe() as fake:
            anchor = search.semantic_corpus_anchor(query)
        self.assertEqual(anchor, {"anchored": False, "probed": 8})
        self.assertEqual(fake.keyword_count.call_count,
                         search._SEMANTIC_ANCHOR_PROBE_MAX)

    def test_probe_skips_stopwords_short_tokens_and_repeats(self):
        with self._probe() as fake:
            anchor = search.semantic_corpus_anchor(
                "the of a xx zqxjklwvutplmb zqxjklwvutplmb")
        self.assertEqual(anchor, {"anchored": False, "probed": 1})
        self.assertEqual([call.args[1] for call in fake.keyword_count.call_args_list],
                         ["zqxjklwvutplmb"])

    def test_mixed_alphanumeric_terms_do_not_anchor_on_letter_fragments(self):
        with self._probe() as fake:
            anchor = search.semantic_corpus_anchor(
                "z123race456 z789retrying012")
        self.assertEqual(anchor, {"anchored": False, "probed": 2})
        self.assertEqual(
            [call.args[1] for call in fake.keyword_count.call_args_list],
            ["z123race456", "z789retrying012"],
        )

    def test_wordless_query_and_absent_corpus_never_demote(self):
        with self._probe() as fake:
            self.assertEqual(search.semantic_corpus_anchor("$$ 42 ::"),
                             {"anchored": None, "probed": 0})
            self.assertEqual(fake.keyword_count.call_count, 0)
            fake.connect.return_value = None
            self.assertEqual(search.semantic_corpus_anchor(self.QUERY),
                             {"anchored": None, "probed": 0})

    def test_a_lane_that_served_nothing_is_never_probed(self):
        empty = {**self._semantic_rows(), "results": []}
        with self._probe() as fake, mock.patch.object(
                semworker, "search_worker", return_value=empty):
            result = search._semantic_local(self.QUERY, 10, who="user")
        self.assertEqual(result["hits"], [])
        self.assertNotIn("corpus_anchor", result["semantic_status"])
        self.assertEqual(fake.connect.call_count, 0)

    def test_ineligible_query_shapes_are_unchanged(self):
        for query in ("REPLY_CAP", "qzxvplmbrt", "server.yaml"):
            with self.subTest(query=query):
                self.assertFalse(
                    search.semantic_query_policy(query)["eligible"])
        with self._probe() as fake, mock.patch.object(
                semworker, "search_worker", return_value=self._semantic_rows()):
            result = search._semantic_local("qzxvplmbrt", 10, who="user")
        self.assertEqual(result["hits"], [])
        self.assertEqual(result["semantic_status"]["reason"], "gibberish-query")
        self.assertEqual(fake.connect.call_count, 0)


class OverspecRecoveryTests(unittest.TestCase):
    """B4: over-specified natural-language queries retry with bm25 term
    coverage - the corpus's own document frequencies pick the informative
    terms, never a hand-curated word list."""

    QUERY = ("use a smaller embedding model to cut disk but publication "
             "rejects extra vectors today")
    TARGET_TEXT = "embedding publication rejects extra vectors; smaller store now"

    def _corpus(self) -> sqlite3.Connection:
        db = sqlite3.connect(":memory:")
        db.executescript(corpusdb._SCHEMA_SQL)
        rows = [
            ("evidence", 0, 50, "codex", "p", "", "", "", "tool",
             self.TARGET_TEXT),
            ("echo-src", 0, 90, "claude", "p", "", "", "", "user",
             f"asked: {self.QUERY} - no answer yet"),
            ("echo-two", 0, 91, "claude", "p", "", "", "", "user",
             f"same question again: {self.QUERY}"),
            *[(f"filler-{n}", 0, n, "codex", "p", "", "", "", "user",
               "use a smaller model to cut disk but today")
              for n in range(30)],
        ]
        db.executemany(corpusdb._INS, rows)
        db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
        return db

    def test_coverage_rank_prefers_rare_evidence_and_flags_echoes(self):
        db = self._corpus()
        try:
            hits = corpusdb.coverage_rank(db, self.QUERY, 200)
        finally:
            db.close()
        by_session = {hit["session"]: hit for hit in hits}
        self.assertTrue(by_session["echo-src"]["_query_echo"])
        self.assertTrue(by_session["echo-two"]["_query_echo"])
        target = by_session["evidence"]
        self.assertFalse(target["_query_echo"])
        independent = [hit["session"] for hit in hits
                       if not hit["_query_echo"]]
        self.assertEqual(independent[0], "evidence")
        for term in ("publication", "rejects", "vectors"):
            self.assertIn(term, target["_terms_matched"])
        for term in ("use", "cut", "disk", "today"):
            self.assertIn(term, target["_terms_missing"])

    def test_coverage_rank_honors_row_filters(self):
        db = self._corpus()
        try:
            hits = corpusdb.coverage_rank(
                db, self.QUERY, 200, {"exclude_session": "evidence"})
        finally:
            db.close()
        self.assertNotIn("evidence", {hit["session"] for hit in hits})

    def test_overspec_gate_keeps_code_shaped_queries_pure_grep(self):
        self.assertTrue(search._overspec_query(self.QUERY))
        # identifier-shaped: one grep pattern to its author, never retried
        self.assertFalse(search._overspec_query("zzqx_no_such_term_anywhere"))
        # bag of bare keywords: no narration was stripped, strict AND stands
        self.assertFalse(search._overspec_query(
            "embed publication rejects vectors segments"))
        # tight queries have no narration to shed
        self.assertFalse(search._overspec_query("no such table"))

    def test_masked_page_detection_reads_row_text_not_snippets(self):
        db = self._corpus()
        weak = {"session": "w", "turn": 0, "who": "user",
                "matched": "all-terms"}
        echo = {"session": "echo-src", "turn": 0, "who": "user"}
        genuine = {"session": "evidence", "turn": 0, "who": "tool"}
        semantic = {"session": "m", "turn": 0, "who": "user",
                    "sem_score": 0.9}
        try:
            self.assertTrue(search._overspec_masked(
                self.QUERY, [weak, echo, semantic], db))
            self.assertFalse(search._overspec_masked(
                self.QUERY, [weak, genuine], db))
        finally:
            db.close()

    def test_query_echo_demotion_marks_rows_for_tool_rescue(self):
        db = self._corpus()
        hits = [
            {"session": "echo-src", "turn": 0, "who": "user"},
            {"session": "evidence", "turn": 0, "who": "tool"},
        ]
        try:
            with mock.patch.object(search, "corpusdb", corpusdb), \
                    mock.patch.object(corpusdb, "connect", return_value=db):
                search._demote_query_echoes(self.QUERY, hits)
        finally:
            db.close()
        self.assertEqual(hits[0]["session"], "evidence")
        self.assertNotIn("_query_echo", hits[0])
        self.assertTrue(hits[1]["_query_echo"])

    def test_retry_block_skips_echoes_and_shown_sessions(self):
        db = self._corpus()
        page = [{"session": "echo-src", "turn": 0, "who": "user"}]
        try:
            with mock.patch.object(search, "corpusdb") as fake:
                fake.connect.return_value = db
                fake.coverage_rank = corpusdb.coverage_rank
                block, scanned = search._overspec_retry_rows(
                    self.QUERY, {}, page, None)
        finally:
            db.close()
        self.assertTrue(scanned)
        self.assertTrue(block)
        sessions = [hit["session"] for hit in block]
        self.assertEqual(sessions[0], "evidence")
        self.assertNotIn("echo-src", sessions)
        self.assertNotIn("echo-two", sessions)
        self.assertTrue(all(hit["matched"] == "content-terms"
                            for hit in block))
        disclosure = search._overspec_disclosure(block)
        self.assertRegex(disclosure, r"matched \d+/\d+ terms")
        self.assertIn("dropped:", disclosure)
        terse = search._overspec_disclosure(block, self.QUERY)
        self.assertTrue(terse.startswith("~coverage "))
        self.assertIn(" · more: agrep --coverage -- ", terse)
        self.assertNotIn("only echo/weak rows", terse)

    def test_identifier_query_never_reaches_the_corpus(self):
        with mock.patch.object(search, "corpusdb") as fake:
            fake.connect.side_effect = AssertionError("carve-out violated")
            self.assertEqual(search._overspec_retry_rows(
                "zzqx_no_such_term_anywhere", {}, [], None), (None, False))

    def test_forced_lane_skips_gates_and_keeps_shown_sessions(self):
        # law 2: --coverage pins the lane on, past the mask gate and the
        # shown-session filter - it is the auto-retry's deeper view
        db = self._corpus()
        page = [{"session": "evidence", "turn": 0, "who": "tool"}]
        try:
            with mock.patch.object(search, "corpusdb") as fake:
                fake.connect.return_value = db
                fake.coverage_rank = corpusdb.coverage_rank
                block, scanned = search._overspec_retry_rows(
                    self.QUERY, {}, page, None, force=True)
        finally:
            db.close()
        self.assertTrue(scanned)
        self.assertTrue(block)
        self.assertIn("evidence", [hit["session"] for hit in block])
        self.assertIn("--coverage",
                      search._overspec_disclosure(block, self.QUERY))
        self.assertNotIn(
            "--coverage",
            search._overspec_disclosure(block, self.QUERY, force=True))

    def test_empty_retry_is_disclosed_not_silent(self):
        # law 1: "retried, empty" must not be byte-identical to "never retried"
        db = self._corpus()
        sessions = ["evidence", "echo-src", "echo-two",
                    *[f"filler-{n}" for n in range(30)]]
        page = [{"session": s, "turn": 0, "who": "user",
                 "matched": "all-terms"} for s in sessions]
        logged: list[str] = []
        try:
            with mock.patch.object(search, "corpusdb") as fake, \
                    mock.patch.object(search.common, "log", logged.append):
                fake.connect.return_value = db
                fake.coverage_rank = corpusdb.coverage_rank
                fake.term_session_df = corpusdb.term_session_df
                out = search._overspec_retry_rows(self.QUERY, {}, page, None)
        finally:
            db.close()
        self.assertEqual(out, (None, True))
        db = self._corpus()
        try:
            with mock.patch.object(search, "corpusdb") as fake, \
                    mock.patch.object(search.common, "log", logged.append):
                fake.connect.return_value = db
                fake.coverage_rank = corpusdb.coverage_rank
                fake.term_session_df = corpusdb.term_session_df
                search._emit_overspec_block(self.QUERY, {}, page, None)
        finally:
            db.close()
        self.assertTrue(any("coverage retry found no new sessions" in line
                            and "--coverage" in line for line in logged))

    def test_corpus_df_admits_narration_the_stoplist_cannot_see(self):
        # B4 guard: eligibility may not hang on _STOP membership alone - on a
        # masked page the corpus's own document frequencies get the final say
        q = "somehow docker container keeps restarting following reboot"
        self.assertFalse(search._overspec_query(q))
        db = sqlite3.connect(":memory:")
        db.executescript(corpusdb._SCHEMA_SQL)
        rows = [
            ("evidence", 0, 50, "codex", "p", "", "", "", "tool",
             "docker daemon restarting after reboot: fixed via systemd unit"),
            *[(f"chatter-{n}", 0, n, "claude", "p", "", "", "", "user",
               "the build somehow keeps failing")
              for n in range(12)],
        ]
        db.executemany(corpusdb._INS, rows)
        db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
        try:
            with mock.patch.object(search, "corpusdb") as fake:
                fake.connect.return_value = db
                fake.coverage_rank = corpusdb.coverage_rank
                fake.term_session_df = corpusdb.term_session_df
                block, scanned = search._overspec_retry_rows(q, {}, [], None)
        finally:
            db.close()
        self.assertTrue(scanned)
        self.assertTrue(block)
        self.assertIn("evidence", [hit["session"] for hit in block])

    def test_keyword_bag_still_never_retries(self):
        # rare-everywhere terms: the corpus reports no narration to shed, so
        # strict AND semantics stand even though the shape gate passed
        db = self._corpus()
        try:
            with mock.patch.object(search, "corpusdb") as fake:
                fake.connect.return_value = db
                fake.coverage_rank = corpusdb.coverage_rank
                fake.term_session_df = corpusdb.term_session_df
                out = search._overspec_retry_rows(
                    "embed publication rejects vectors segments", {}, [], None)
        finally:
            db.close()
        self.assertEqual(out, (None, False))

    def test_coverage_flag_is_porcelain_only(self):
        for extra in (["--json"], ["--flat"], ["-c"], ["--chats"],
                      ["-s"], ["--lexical"]):
            with self.subTest(extra=extra), \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    search.main(["pattern", "--coverage", *extra])
                self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
