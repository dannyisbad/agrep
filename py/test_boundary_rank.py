from __future__ import annotations

import json
import math
import re
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

from _test_support import isolate_data_dir

isolate_data_dir()
import boundary_rank
import generate_boundary_fixtures
import search


class BoundaryRankTests(unittest.TestCase):
    def test_fixed_refinement_pool_can_promote_aligned_candidate(self):
        now = 1_900_000_000_000
        hits = [{"session": f"interior-{index:03d}", "turn": index,
                 "ts": now, "who": "user", "snippet": "xxakdyy"}
                for index in range(400)]
        hits.append({"session": "aligned", "turn": 401, "ts": 0,
                     "who": "user", "snippet": "akd"})
        with mock.patch.object(search, "_native_boundary_scores", return_value=False), \
                mock.patch.object(search.time, "time", return_value=now / 1000):
            ranked = search._rank(hits, "akd", "keyword", "score")
        self.assertEqual(ranked[0]["session"], "aligned")
        self.assertEqual(ranked[0]["_boundary_class"], "aligned")

    def test_exhaustive_refinement_classifies_every_hit(self):
        hits = [{"session": f"s{index:04d}", "turn": index, "ts": 0,
                 "who": "user", "snippet": "xxakdyy"}
                for index in range(search._BOUNDARY_REFINE_POOL + 1)]
        ranked = search._rank(
            hits, "akd", "keyword", "score", refine_all=True)
        self.assertEqual(search._count_tiers(ranked), {
            "phrase_aligned": 0,
            "phrase_partial": 0,
            "phrase_interior": len(hits),
            "all_terms": 0,
        })

    def test_adaptive_frontier_refines_every_possible_tie(self):
        # Every equal cheap ceiling remains live at the wave boundary. The final
        # candidate therefore gets exact evidence instead of hiding behind a cap.
        hits = [{"session": f"s{index:04d}", "turn": 0, "ts": 0,
                 "who": "user", "snippet": "peakDetect"}
                for index in range(2041)]
        with mock.patch.object(search, "_native_boundary_scores", return_value=False):
            ranked = search._rank(hits, "akd", "keyword", "score")
        self.assertEqual(ranked[0]["session"], "s0000")
        self.assertEqual(len({hit["score"] for hit in ranked}), 1)
        self.assertEqual({hit.get("_boundary_class") for hit in ranked}, {"interior"})
        self.assertEqual(len({hit.get("_boundary_factor") for hit in ranked}), 1)

    def test_python_fallback_can_promote_beyond_first_wave(self):
        now = 1_900_000_000_000
        hits = [{"session": f"interior-{index}", "turn": index, "ts": now,
                 "who": "user", "snippet": "xxakdyy"}
                for index in range(5)]
        hits.append({"session": "aligned", "turn": 6, "ts": now - 1000,
                     "who": "user", "snippet": "akd"})
        with mock.patch.object(search, "_BOUNDARY_REFINE_POOL", 5), \
                mock.patch.object(search, "_native_boundary_scores", return_value=False), \
                mock.patch.object(search.time, "time", return_value=now / 1000):
            ranked = search._rank(hits, "akd", "keyword", "score", top_k=1)
        self.assertEqual(ranked[0]["session"], "aligned")
        self.assertEqual(ranked[0]["_boundary_class"], "aligned")

    def test_adaptive_top_matches_full_refinement_across_lanes(self):
        now = 1_900_000_000_000
        hits = []
        for index in range(100):
            hit = {
                "session": f"s{index:03}", "turn": index,
                "ts": now - index * 86_400_000, "who": "user",
                "snippet": "akd" if index % 7 == 0 else "peakDetect",
            }
            if index >= 30:
                hit["matched"] = "all-terms"
            hits.append(hit)
        with mock.patch.object(search, "_BOUNDARY_REFINE_POOL", 20), \
                mock.patch.object(search, "_native_boundary_scores", return_value=False), \
                mock.patch.object(search.time, "time", return_value=now / 1000):
            adaptive = search._rank([dict(hit) for hit in hits], "akd", "keyword",
                                    "score", top_k=40)
            complete = search._rank([dict(hit) for hit in hits], "akd", "keyword",
                                    "score", refine_all=True)
        shape = lambda rows: [(hit["session"], hit["score"],  # noqa: E731
                               hit.get("_boundary_class"), hit.get("matched"))
                              for hit in rows[:40]]
        self.assertEqual(shape(adaptive), shape(complete))

    def test_session_heads_do_not_consume_unrefined_score_ceilings(self):
        search._load_corpusdb()
        now = 1_900_000_000_000
        half_life_ms = search._RECENCY_HALF_LIFE_DAYS * 86_400_000
        hits = [{"session": "A", "turn": index, "ts": now,
                 "who": "user", "snippet": "akd"}
                for index in range(search._BOUNDARY_REFINE_POOL)]
        hits.extend([
            {"session": "B", "turn": 0,
             "ts": now + half_life_ms * math.log(0.8, 2),
             "who": "user", "snippet": "xxakdyy"},
            {"session": "C", "turn": 0,
             "ts": now + half_life_ms * math.log(0.7, 2),
             "who": "user", "snippet": "akd"},
        ])

        class FakeDb:
            def close(self):
                pass

        engine = {"hits": hits, "total": len(hits), "chats": 3}
        boundary = search._prepare_boundary("akd", "keyword")
        with mock.patch.object(search.corpusdb, "connect", return_value=FakeDb()), \
                mock.patch.object(search.corpusdb, "keyword", return_value=engine), \
                mock.patch.object(search, "_prepare_boundary", return_value=boundary), \
                mock.patch.object(search, "_native_boundary_scores", return_value=False), \
                mock.patch.object(search.time, "time", return_value=now / 1000):
            result = search.run_query(
                "akd", limit=2, session_limit=2, exact_totals=True,
                allow_fallback=False)
        self.assertEqual([hit["session"] for hit in result["hits"]], ["A", "C"])

    def test_native_pipe_is_utf8_and_keeps_full_scoring_factor(self):
        stats = {"akd": (1, 0)}
        prepared = boundary_rank.prepare_query("akd", stats)
        context = (prepared, re.compile("(akd)", re.I), "akd", stats)
        factor = prepared.evaluate(
            "peakDetect 😀", spans=((2, 5),), validate_spans=False).factor
        response = json.dumps({
            "protocol": search._NATIVE_BOUNDARY_PROTOCOL,
            "results": [{"factor": factor, "match_class": "interior"}],
        })
        completed = mock.Mock(returncode=0, stdout=response, stderr="")
        hit = {"session": "s", "turn": 1, "ts": 0, "who": "agent",
               "snippet": "peakDetect 😀"}
        self.assertEqual(search._NATIVE_BOUNDARY_MIN_ITEMS, 1)
        with mock.patch.object(search, "_NATIVE_BOUNDARY_IDENTITY", None), \
                mock.patch.object(search, "_NATIVE_BOUNDARY_AVAILABLE", None), \
                mock.patch.object(search.common, "ingest_bin", return_value=Path(__file__)), \
                mock.patch.object(search.subprocess, "run", return_value=completed) as run:
            self.assertTrue(search._native_boundary_scores([hit], context))
        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["errors"], "strict")
        request = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(request["protocol"], search._NATIVE_BOUNDARY_PROTOCOL)
        self.assertTrue(request["decut"])
        self.assertEqual(hit["_boundary_score_factor"], factor)
        self.assertEqual(hit["_boundary_factor"], round(factor, 6))

        now = 1_900_000_000_000
        hit["ts"] = now - 2.84 * 86_400_000
        plain = {key: value for key, value in hit.items()
                 if not key.startswith("_boundary_")}
        pattern = search._match_pat("akd", "keyword")
        native_score = round(search._score(
            hit, pattern, 3, now, terms=["akd"], boundary=context), 4)
        python_score = round(search._score(
            plain, pattern, 3, now, terms=["akd"], boundary=context), 4)
        self.assertEqual(native_score, python_score)
        self.assertEqual(native_score, 0.0843)

    def test_aligned_ascii_phrases_bypass_native_without_widening(self):
        context = search._prepare_boundary("akd", "keyword")
        aligned = {"snippet": "say akd now"}
        cases = (
            {"snippet": "peakDetect"},
            {"snippet": "fooAkd"},
            {"snippet": "don't", "query": "t"},
            {"snippet": "akd \U0001f600"},
            {"snippet": "\u2026akd"},
            {"snippet": "say akd now", "matched": "all-terms"},
            {"snippet": "say akd now", "matched": "content-terms"},
        )
        unresolved, certified = search._certify_ascii_aligned_phrases(
            [aligned], context)
        self.assertEqual((unresolved, certified), ([], 1))
        self.assertEqual(
            (aligned["_boundary_class"], aligned["_boundary_score_factor"]),
            ("aligned", 1.0))

        for case in cases:
            query = case.pop("query", "akd")
            with self.subTest(case=case, query=query):
                unresolved, certified = search._certify_ascii_aligned_phrases(
                    [case], search._prepare_boundary(query, "keyword"))
                self.assertEqual((unresolved, certified), ([case], 0))
                self.assertNotIn("_boundary_factor", case)

    def test_native_batch_receives_only_unresolved_rows(self):
        path = Path(__file__)
        identity = search._native_boundary_identity(path)
        context = search._prepare_boundary("akd", "keyword")
        aligned = {"snippet": "say akd now"}
        interior = {"snippet": "peakDetect"}
        fallback = {"snippet": "say akd now", "matched": "all-terms"}
        response = json.dumps({
            "protocol": search._NATIVE_BOUNDARY_PROTOCOL,
            "results": [
                {"factor": 0.25, "match_class": "interior"},
                {"factor": 0.5, "match_class": "partial"},
            ],
        })
        completed = mock.Mock(returncode=0, stdout=response, stderr="")
        with mock.patch.object(search.common, "ingest_bin", return_value=path), \
                mock.patch.object(search, "_NATIVE_BOUNDARY_IDENTITY", identity), \
                mock.patch.object(search, "_NATIVE_BOUNDARY_AVAILABLE", None), \
                mock.patch.object(search.subprocess, "run", return_value=completed) as run:
            self.assertTrue(search._native_boundary_scores(
                [aligned, interior, fallback], context))
        request = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(len(request["items"]), 2)
        self.assertEqual(aligned["_boundary_score_factor"], 1.0)
        self.assertEqual(interior["_boundary_score_factor"], 0.25)
        self.assertEqual(fallback["_boundary_score_factor"], 0.5)

    def test_row_lane_certifies_once_per_batch(self):
        # _boundary_batch partitions the rows, then hands them to the native
        # scorer. Certification is deterministic, so a second pass would
        # rescan every snippet to rebuild the identical split.
        path = Path(__file__)
        identity = search._native_boundary_identity(path)
        context = search._prepare_boundary("akd", "keyword")
        rows = [{"snippet": "say akd now"}, {"snippet": "peakDetect"}]
        response = json.dumps({
            "protocol": search._NATIVE_BOUNDARY_PROTOCOL,
            "results": [{"factor": 0.25, "match_class": "interior"}],
        })
        real_certify = search._certify_ascii_aligned_phrases
        calls = []

        def counting_certify(hits, ctx):
            calls.append(len(hits))
            return real_certify(hits, ctx)

        state = ["native", identity, mock.Mock()]
        with mock.patch.object(search.common, "ingest_bin", return_value=path), \
                mock.patch.object(search, "_NATIVE_BOUNDARY_IDENTITY", identity), \
                mock.patch.object(search, "_NATIVE_BOUNDARY_AVAILABLE", None), \
                mock.patch.object(search, "_certify_ascii_aligned_phrases",
                                  counting_certify), \
                mock.patch.object(search, "_boundary_worker_request",
                                  return_value=json.loads(response)):
            self.assertTrue(search._boundary_batch(rows, context, state))
        self.assertEqual(calls, [2])
        self.assertEqual(rows[0]["_boundary_score_factor"], 1.0)
        self.assertEqual(rows[1]["_boundary_score_factor"], 0.25)

    def test_fully_certified_batch_never_starts_a_worker(self):
        hit = {"snippet": "say akd now"}
        context = search._prepare_boundary("akd", "keyword")
        with mock.patch.object(search.common, "ingest_bin") as ingest, \
                mock.patch.object(search.subprocess, "run") as run:
            self.assertTrue(search._native_boundary_scores([hit], context))
        ingest.assert_not_called()
        run.assert_not_called()

        state = [None, None, None]
        with mock.patch.object(search, "_start_boundary_worker") as start:
            self.assertTrue(search._boundary_batch([hit], context, state))
        start.assert_not_called()
        self.assertEqual(state, [None, None, None])

    def test_disabled_native_path_skips_batch_materialization(self):
        path = Path(__file__)
        identity = search._native_boundary_identity(path)
        context = (*search._prepare_boundary("akd", "keyword")[:2], "akd", {})
        with mock.patch.object(search.common, "ingest_bin", return_value=path), \
                mock.patch.object(search, "_NATIVE_BOUNDARY_IDENTITY", identity), \
                mock.patch.object(search, "_NATIVE_BOUNDARY_AVAILABLE", False), \
                mock.patch.object(search, "_native_boundary_items") as items:
            self.assertFalse(search._native_boundary_scores([], context))
        items.assert_not_called()

    def test_native_chunks_are_bounded_and_fail_without_partial_scores(self):
        path = Path(__file__)
        identity = search._native_boundary_identity(path)
        context = search._prepare_boundary("akd", "keyword")
        hits = [{"session": f"s{index}", "turn": index, "ts": 0,
                 "who": "user", "snippet": "peakDetect"}
                for index in range(5)]
        calls = []

        def run(_cmd, **kwargs):
            request = json.loads(kwargs["input"])
            calls.append(len(request["items"]))
            if len(calls) == 2:
                return mock.Mock(returncode=1, stdout="", stderr="broken")
            response = {"protocol": search._NATIVE_BOUNDARY_PROTOCOL, "results": [
                {"factor": 1.0, "match_class": "aligned"}
                for _item in request["items"]
            ]}
            return mock.Mock(returncode=0, stdout=json.dumps(response), stderr="")

        with mock.patch.object(search.common, "ingest_bin", return_value=path), \
                mock.patch.object(search, "_NATIVE_BOUNDARY_IDENTITY", identity), \
                mock.patch.object(search, "_NATIVE_BOUNDARY_AVAILABLE", None), \
                mock.patch.object(search, "_NATIVE_BOUNDARY_MIN_ITEMS", 1), \
                mock.patch.object(search, "_NATIVE_BOUNDARY_BATCH", 2), \
                mock.patch.object(search.subprocess, "run", side_effect=run):
            self.assertFalse(search._native_boundary_scores(hits, context))
        self.assertEqual(calls, [2, 2])
        self.assertTrue(all("_boundary_factor" not in hit for hit in hits))

    def test_query_tokenization_and_cold_priors(self):
        self.assertEqual(boundary_rank.query_tokens(" cyber-filter_thing "),
                         ("cyber", "filter", "thing"))
        self.assertEqual(boundary_rank.cold_prior("id"), 0.90)
        self.assertEqual(boundary_rank.cold_prior("akd"), 0.75)
        self.assertEqual(boundary_rank.cold_prior("four"), 0.40)
        self.assertEqual(boundary_rank.cold_prior("longer"), 0.05)
        self.assertEqual(boundary_rank.cold_prior("東京"), 0.0)

    def test_code_boundaries(self):
        cases = (
            ("fooBarBaz", "bar", "aligned"),
            ("myHTTPServer", "http", "aligned"),
            ("sha256", "256", "aligned"),
            ("sha256", "2", "partial"),
            ("varΔx", "δ", "aligned"),
            ("peakDetect", "akd", "interior"),
        )
        for text, query, expected in cases:
            with self.subTest(text=text, query=query):
                score = boundary_rank.prepare_query(query).evaluate(text)
                self.assertEqual(score.match_class, expected)
                self.assertTrue(score.matched)

    def test_punctuation_and_apostrophe_joining(self):
        aligned = boundary_rank.prepare_query("filter").evaluate("cyber_filter")
        suffix = boundary_rank.prepare_query("filter").evaluate("cyber_filterilter")
        apostrophe = boundary_rank.prepare_query("t").evaluate("don't")
        self.assertEqual(aligned.qualities, (1.0,))
        self.assertEqual(suffix.qualities, (0.5,))
        self.assertEqual(apostrophe.qualities, (0.5,))

    def test_normalized_offsets_cover_original_graphemes(self):
        sharp_s = boundary_rank.prepare_query("STRASSE").evaluate("Straße")
        composed = boundary_rank.prepare_query("é").evaluate("e\u0301")
        hangul = boundary_rank.prepare_query("가").evaluate("\u1100\u1161")
        self.assertEqual(sharp_s.spans, ((0, 6),))
        self.assertEqual(composed.spans, ((0, 2),))
        self.assertEqual(composed.qualities, (1.0,))
        self.assertEqual(hangul.spans, ((0, 2),))

    def test_casefold_expansion_keeps_original_match_span(self):
        score = boundary_rank.prepare_query("xxi").evaluate(
            "xxİfoo", spans=((0, 3),))
        self.assertTrue(score.matched)
        self.assertEqual(score.spans, ((0, 3),))
        self.assertLess(score.factor, 1.0)

        dotless = boundary_rank.prepare_query("i").evaluate(
            "xıx", spans=((1, 2),), validate_spans=False)
        self.assertEqual(dotless.match_class, "interior")

    def test_combining_variation_and_zwj_sequences_stay_attached(self):
        combining = boundary_rank.prepare_text("a\u0301b")
        emoji = boundary_rank.prepare_text("x👩\u200d💻y")
        variation = boundary_rank.prepare_text("x\u2764\ufe0fy")
        self.assertNotIn(1, combining.boundaries)
        self.assertNotIn(2, combining.boundaries)
        self.assertNotIn(2, emoji.boundaries)
        self.assertNotIn(3, emoji.boundaries)
        self.assertNotIn(2, variation.boundaries)

    def test_observed_contamination_and_geometric_mean(self):
        stats = {"akd": (372, 32), "xyz": (100, 100)}
        single = boundary_rank.prepare_query("akd", stats).evaluate("peakDetect")
        mixed = boundary_rank.prepare_query("akd xyz", stats).evaluate(
            "peakDetect xyz")
        self.assertEqual(single.factor, 0.12)
        self.assertAlmostEqual(mixed.factor, math.sqrt(0.12), places=12)

    def test_stats_resolve_once_per_unique_query_token(self):
        calls: list[str] = []

        def lookup(token: str):
            calls.append(token)
            return 10, 2

        prepared = boundary_rank.prepare_query("akd akd", lookup)
        prepared.evaluate("akd and akd")
        prepared.evaluate("insideakd")
        self.assertEqual(calls, ["akd"])

    def test_exact_spans_prevent_phrase_laundering(self):
        prepared = boundary_rank.prepare_query("cyber filter")
        text = "cyber_filterilter and standalone filter"
        automatic = prepared.evaluate(text)
        exact = prepared.evaluate(text, spans=((0, 5), (6, 12)))
        self.assertEqual(automatic.qualities, (1.0, 1.0))
        self.assertEqual(exact.qualities, (1.0, 0.5))
        self.assertLess(exact.factor, automatic.factor)

    def test_unsegmented_scripts_have_no_cold_prior(self):
        interior = boundary_rank.prepare_query("東京").evaluate("駐東京都")
        aligned = boundary_rank.prepare_query("東京").evaluate("東京 tower")
        self.assertEqual(interior.factor, 1.0)
        self.assertEqual(interior.match_class, "interior")
        self.assertEqual(aligned.factor, 1.0)
        self.assertEqual(aligned.match_class, "aligned")

    def test_unsegmented_observed_stats_still_demote(self):
        stats = {"東京": (100, 0)}
        ambiguity = (100 - 0 + 32.0 * 0.0) / (100 + 32.0)
        interior = boundary_rank.prepare_query("東京", stats).evaluate("駐東京都")
        prefix = boundary_rank.prepare_query("東京", stats).evaluate("東京都")
        aligned = boundary_rank.prepare_query("東京", stats).evaluate("東京 tower")
        self.assertAlmostEqual(interior.factor, 1.0 - ambiguity, places=12)
        self.assertAlmostEqual(prefix.factor, 1.0 - ambiguity * 0.5, places=12)
        self.assertTrue(interior.matched)
        self.assertEqual(aligned.factor, 1.0)
        self.assertEqual(aligned.match_class, "aligned")

    def test_ambiguity_formula_pins_smoothing_away_from_floor(self):
        # A = (n - s + 32*prior) / (n + 32) with prior("akd") = 0.75; both cases sit above the 0.12 floor
        stats = {"akd": (10, 2)}
        ambiguity = (10 - 2 + 32.0 * 0.75) / (10 + 32.0)
        interior = boundary_rank.prepare_query("akd", stats).evaluate("peakDetect")
        half = boundary_rank.prepare_query("akd", stats).evaluate("xakd")
        mapping = boundary_rank.prepare_query(
            "akd", {"akd": {"n": 10, "s": 2}}).evaluate("peakDetect")
        self.assertAlmostEqual(interior.factor, 1.0 - ambiguity, places=12)
        self.assertAlmostEqual(half.factor, 1.0 - ambiguity * 0.5, places=12)
        self.assertEqual(mapping.factor, interior.factor)

    def test_ambiguity_clamps_malformed_stats(self):
        over = boundary_rank.prepare_query("akd", {"akd": (10, 50)}).evaluate("peakDetect")
        negative = boundary_rank.prepare_query("akd", {"akd": (10, -5)}).evaluate("peakDetect")
        unseen = boundary_rank.prepare_query("akd", {"akd": (0, 7)}).evaluate("peakDetect")
        self.assertAlmostEqual(over.factor, 1.0 - 24.0 / 42.0, places=12)
        self.assertAlmostEqual(negative.factor, 1.0 - 34.0 / 42.0, places=12)
        self.assertAlmostEqual(unseen.factor, 1.0 - 0.75, places=12)

    def test_stale_and_negative_spans_rejected_on_every_path(self):
        cases = (("akd", "peakDetect"), ("akd", "peakDetect東"), ("東京", "東京都"))
        for query, text in cases:
            prepared = boundary_rank.prepare_query(query)
            for span in ((0, 999), (-3, 3), (3, 1)):
                with self.subTest(query=query, text=text, span=span):
                    with self.assertRaisesRegex(ValueError, "outside text"):
                        prepared.evaluate(text, spans=(span,))
                    with self.assertRaisesRegex(ValueError, "outside text"):
                        prepared.evaluate(text, spans=(span,), validate_spans=False)

    def test_prepared_text_is_reused_across_occurrence_evaluations(self):
        self.assertIs(boundary_rank.prepare_text("東京都心部"),
                      boundary_rank.prepare_text("東京都心部"))

    def test_invalid_or_missing_spans_are_explicit(self):
        prepared = boundary_rank.prepare_query("one two")
        with self.assertRaises(ValueError):
            prepared.evaluate("one two", spans=((0, 3),))
        with self.assertRaises(ValueError):
            prepared.evaluate("one two", spans=((4, 7), (0, 3)))
        missing = prepared.evaluate("one only")
        self.assertFalse(missing.matched)
        self.assertEqual(missing.spans, ((0, 3), None))

    def test_rust_conformance_fixture_tokens_align(self):
        # The sidecar caps aligned subtokens at 4 graphemes, so this checks one
        # direction only: whatever the rust segmenter aligned, we must align too.
        path = Path(__file__).resolve().parent / "fixtures" / "boundary_conformance.json"
        entries = json.loads(path.read_text(encoding="utf-8"))["segmentation"]
        self.assertGreaterEqual(len(entries), 14)
        for entry in entries:
            for token in entry["aligned"]:
                with self.subTest(text=entry["text"], token=token):
                    score = boundary_rank.prepare_query(token).evaluate(entry["text"])
                    self.assertTrue(score.matched)
                    self.assertEqual(score.match_class, "aligned")

    @unittest.skipUnless(
        unicodedata.unidata_version == "16.0.0", "fixture oracle uses Unicode 16")
    def test_python_oracle_and_unicode_tables_are_frozen(self):
        fixture = Path(__file__).resolve().parent / "fixtures" / "boundary_conformance.json"
        rust = (Path(__file__).resolve().parents[1] / "crates" / "agrep-core"
                / "src" / "unicode_v16.rs")
        self.assertEqual(fixture.read_text(encoding="utf-8"),
                         generate_boundary_fixtures.oracle_text())
        self.assertEqual(rust.read_text(encoding="utf-8"),
                         generate_boundary_fixtures.rust_text())

    def test_conformance_nfc_nfd_pairs_evaluate_identically(self):
        cafes = "caf\u00e9's"
        cafebar = "caf\u00e9Bar"
        for nfc, token in ((cafes, "caf\u00e9"), (cafebar, "caf\u00e9"), (cafebar, "bar")):
            nfd = unicodedata.normalize("NFD", nfc)
            self.assertNotEqual(nfc, nfd)
            with self.subTest(token=token, text=nfc):
                a = boundary_rank.prepare_query(token).evaluate(nfc)
                b = boundary_rank.prepare_query(token).evaluate(nfd)
                self.assertEqual(a.match_class, b.match_class)
                self.assertEqual(a.qualities, b.qualities)


if __name__ == "__main__":
    unittest.main()
