"""Contract tests for native JSONL search orchestration."""

import contextlib
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from _test_support import isolate_data_dir


isolate_data_dir()

import corpusdb
import explore
import indexd_runtime
import search


def _spec(q: str = "needle", **changes) -> search.QuerySpec:
    spec = search.QuerySpec(
        q=q, mode="keyword", limit=4, sort="score", agent=None,
        project=None, who="tool", model=None, model_soft=False, chat=None,
        since_ms=None, until_ms=None, exhaustive=False, session_limit=None,
        include_tools=True, exclude_session=None,
        exclude_session_from_turn=None, allow_fallback=True,
        exact_totals=False, family_diverse=False, semantic_timeout_s=None,
    )
    return replace(spec, **changes)


def _hit(session: str, text: str, *, who: str = "tool", turn: int = 1,
         matched: str | None = None, row_key: int | None = None) -> dict:
    hit = {
        "session": session, "turn": turn, "ts": 1_900_000_000_000,
        "agent": "codex", "project": "fixture", "concept": "",
        "model": "", "model_source": "tool", "who": who,
        "snippet": text,
    }
    if matched is not None:
        hit["matched"] = matched
    if row_key is not None:
        hit["_agrep_row_key"] = row_key
    return hit


class SharedRankEpoch(unittest.TestCase):
    def test_explicit_epoch_avoids_a_second_clock_read(self) -> None:
        hit = _hit("one", "needle")
        boundary = search._prepare_boundary("needle", "keyword", None)
        with mock.patch.object(
                search.time, "time", side_effect=AssertionError("clock reread")), \
                mock.patch.object(
                    search, "_native_boundary_scores", return_value=False):
            ranked = search._rank(
                [hit], "needle", "keyword", "score", boundary=boundary,
                now_ms=2_000_000_000_000)
        self.assertEqual(ranked, [hit])
        self.assertIn("score", hit)


class PublicationRaceRetry(unittest.TestCase):
    def test_native_wrapper_preserves_direct_generation_movement(self) -> None:
        with mock.patch.object(
                search, "_jsonl_native_keyword_once",
                side_effect=explore.DirectSnapshotMoved("generation moved")), \
                self.assertRaises(search.DirectSnapshotQueryMoved):
            search._jsonl_native_keyword(
                _spec(), {}, None, 100, preflight_ok=True)

    def test_completed_publisher_gets_one_immediate_whole_query_retry(
            self) -> None:
        error = search.DirectSnapshotQueryMoved("generation moved")
        expected = search.LaneResult(hits=[], engine="jsonl")
        with mock.patch.object(search, "corpusdb", corpusdb), \
                mock.patch.object(
                    search, "_keyword_candidates_once",
                    side_effect=[error, expected]) as query, \
                mock.patch.object(
                    corpusdb, "query_publication_active",
                    return_value=False) as publishing, \
                mock.patch.object(search.time, "sleep") as sleep:
            actual = search._keyword_candidates(_spec())
        self.assertIs(actual, expected)
        self.assertEqual(query.call_count, 2)
        publishing.assert_called_once_with()
        sleep.assert_not_called()

    def test_repeated_unowned_movement_still_fails_closed(self) -> None:
        error = search.NativeEventScanMoved("generation moved again")
        with mock.patch.object(search, "corpusdb", corpusdb), \
                mock.patch.object(
                    search, "_keyword_candidates_once",
                    side_effect=error) as query, \
                mock.patch.object(
                    corpusdb, "query_publication_active",
                    return_value=False) as publishing, \
                mock.patch.object(search.time, "sleep") as sleep, \
                self.assertRaises(search.NativeEventScanMoved):
            search._keyword_candidates(_spec())
        self.assertEqual(query.call_count, 2)
        self.assertEqual(publishing.call_count, 2)
        sleep.assert_not_called()

    def test_moving_generation_over_last_good_serves_without_waiting(
            self) -> None:
        for error in (
                search.DirectSnapshotQueryMoved("direct generation moved"),
                search.NativeEventScanMoved("native generation moved")):
            with self.subTest(error=type(error).__name__):
                expected = search.LaneResult(
                    hits=[_hit("one", "needle")], engine="corpusdb")
                with mock.patch.object(search, "corpusdb", corpusdb), \
                        mock.patch.object(
                        search, "_keyword_candidates_once",
                        side_effect=[error, expected]) as query, \
                        mock.patch.object(
                            corpusdb, "query_publication_active",
                            return_value=True) as publishing, \
                        mock.patch.object(search.time, "sleep") as sleep:
                    actual = search._keyword_candidates(_spec())
                self.assertIs(actual, expected)
                self.assertEqual(query.call_args_list, [
                    mock.call(_spec()),
                    mock.call(_spec(pin_last_good=True)),
                ])
                publishing.assert_called_once_with()
                sleep.assert_not_called()

    def test_direct_and_native_restart_past_a_third_observation(self) -> None:
        for error in (
                search.DirectSnapshotQueryMoved("direct generation moved"),
                search.NativeEventScanMoved("native generation moved")):
            with self.subTest(error=type(error).__name__):
                expected = search.LaneResult(hits=[], engine="jsonl")
                with mock.patch.object(search, "corpusdb", corpusdb), \
                        mock.patch.object(
                        search, "_keyword_candidates_once",
                        side_effect=[error, error, error, expected]) as query, \
                        mock.patch.object(
                            corpusdb, "query_publication_active",
                            return_value=True) as publishing, \
                        mock.patch.object(search.time, "sleep") as sleep:
                    actual = search._keyword_candidates(_spec())
                self.assertIs(actual, expected)
                self.assertEqual(query.call_args_list, [
                    mock.call(_spec()),
                    mock.call(_spec(pin_last_good=True)),
                    mock.call(_spec(pin_last_good=True)),
                    mock.call(_spec(pin_last_good=True)),
                ])
                self.assertEqual(publishing.call_count, 3)
                self.assertEqual(sleep.call_args_list, [
                    mock.call(0.02), mock.call(0.04),
                ])

    def test_stable_snapshot_damage_fails_without_retry(self) -> None:
        for error in (
                search.DirectSnapshotQueryError("stable direct damage"),
                search.NativeEventScanError("stable native damage")):
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(search, "corpusdb", corpusdb), \
                    mock.patch.object(
                        search, "_keyword_candidates_once",
                        side_effect=error) as query, \
                    mock.patch.object(search.time, "sleep") as sleep, \
                    self.assertRaises(type(error)):
                search._keyword_candidates(_spec())
            query.assert_called_once_with(_spec())
            sleep.assert_not_called()

    def test_first_snapshot_wait_is_bounded_to_one_second(self) -> None:
        error = search.SnapshotPublicationActive(
            "a verified publisher is updating the query generation")
        self.assertEqual(search._QUERY_PUBLICATION_WAIT_S, 1.0)
        with mock.patch.object(
                search, "_keyword_candidates_once",
                side_effect=error) as query, \
                mock.patch.object(
                    search.time, "monotonic",
                    side_effect=[10.0, 10.5, 11.0]), \
                mock.patch.object(search.time, "sleep") as sleep, \
                self.assertRaisesRegex(
                    search.SnapshotPublicationTimeout,
                    "still publishing its first searchable snapshot after 1s"):
            search._keyword_candidates(_spec())
        self.assertEqual(query.call_count, 3)
        self.assertEqual(sleep.call_args_list, [mock.call(0.02), mock.call(0.04)])

    def test_live_transcript_publisher_is_waited_before_any_direct_scan(
            self) -> None:
        with mock.patch.object(search, "corpusdb", corpusdb), \
                mock.patch.object(corpusdb, "connect", return_value=None), \
                mock.patch.object(
                    corpusdb, "query_search_index_build_active",
                    return_value=False), \
                mock.patch.object(
                    corpusdb, "query_publication_active",
                    return_value=True) as active, \
                mock.patch.object(search, "_prepare_boundary") as boundary, \
                self.assertRaises(search.SnapshotPublicationActive):
            search._keyword_candidates_once(_spec())
        active.assert_called_once_with()
        boundary.assert_not_called()

    def test_pinned_exhaustive_count_keeps_the_behind_snapshot(self) -> None:
        db = mock.Mock()
        db._source_stamp_current = False
        db.in_transaction = True
        stop = RuntimeError("stop after the snapshot election")
        with mock.patch.object(search, "corpusdb", corpusdb), \
                mock.patch.object(corpusdb, "connect", return_value=db), \
                mock.patch.object(
                    corpusdb, "query_publication_active",
                    return_value=False) as active, \
                mock.patch.object(
                    search, "_prepare_boundary",
                    side_effect=stop) as boundary, \
                self.assertRaises(RuntimeError):
            search._keyword_candidates_once(
                _spec(exhaustive=True, pin_last_good=True))
        active.assert_not_called()
        boundary.assert_called_once_with("needle", "keyword", db)


class NativeShape(unittest.TestCase):
    def test_non_ascii_owner_filters_stay_on_python_semantics(self) -> None:
        self.assertTrue(search._native_event_shape(_spec()))
        for field in ("agent", "project", "chat"):
            with self.subTest(field=field):
                self.assertFalse(search._native_event_shape(
                    _spec(**{field: "café"})))

    def test_family_resolution_is_frozen_before_both_lanes(self) -> None:
        discovered = (
            "caller", frozenset({"caller", "side"}), frozenset({"side"}))
        with mock.patch.object(
                search.common, "indexed_calling_family_with_sides",
                side_effect=[None, discovered]) as lookup:
            frozen = explore.freeze_native_event_filter({
                "exclude_session": "caller",
                "exclude_session_from_turn": None,
            })
            self.assertEqual(
                explore._native_excluded_sessions(frozen),
                frozenset({"caller"}))
            self.assertEqual(
                explore._native_excluded_sessions(frozen),
                frozenset({"caller"}))
        self.assertEqual(lookup.call_count, 1)

    def test_freshen_detects_same_size_and_mtime_inode_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / "messages.jsonl"
            replacement = root / "replacement"
            fixed_ns = 1_700_000_000_000_000_000
            path.write_text("aa", encoding="utf-8")
            os.utime(path, ns=(fixed_ns, fixed_ns))
            with mock.patch.object(explore.common, "DATA_DIR", root), \
                    mock.patch.object(
                        explore, "_GEN_FILES", ("messages.jsonl",)), \
                    mock.patch.object(explore, "_GEN", None):
                explore._freshen()
                before = explore._GEN
                replacement.write_text("bb", encoding="utf-8")
                os.utime(replacement, ns=(fixed_ns, fixed_ns))
                os.replace(replacement, path)
                explore._freshen()
                after = explore._GEN
            self.assertEqual(before[0][:2], after[0][:2])
            self.assertNotEqual(before, after)

    def test_frozen_tool_lane_does_not_reread_the_setting(self) -> None:
        with mock.patch.object(explore.common, "setting", return_value="off"):
            frozen = explore.freeze_tool_lane_filter({"include_tools": True})
        messages = {"s": [{
            "id": "codex:s:1", "session": "s", "agent": "codex",
            "project": "repo", "turn": 1, "ts": 1, "who": "user",
            "text": "needle",
        }]}
        with mock.patch.object(
                explore.common, "setting",
                side_effect=AssertionError("tools setting reread")), \
                mock.patch.object(
                    explore, "_messages_by_session", return_value=messages), \
                mock.patch.object(
                    explore, "_reply_records_by_id", return_value={}), \
                mock.patch.object(explore, "_session_concept", return_value={}), \
                mock.patch.object(
                    explore.common, "event_blobs_bulk",
                    side_effect=AssertionError("disabled events consumed")):
            rows = list(explore._iter_kw_corpus(frozen))
        self.assertFalse(frozen["_tool_lane_enabled"])
        self.assertEqual([(row["who"], row["text"]) for row in rows], [
            ("user", "needle"),
        ])


class FrontierCertification(unittest.TestCase):
    @staticmethod
    def _ranked(
            session: str, score: float, ts: int, *, matched: str | None = None,
            who: str = "tool",
    ) -> dict:
        hit = _hit(session, "needle", who=who, matched=matched)
        hit.update(score=score, ts=ts)
        return hit

    @staticmethod
    def _tail(
            session: str, upper: float, ts: int, *, matched: str = "phrase",
    ) -> dict:
        return {
            "matched": matched, "upper_score": upper,
            "ts": ts, "session": session,
        }

    def test_omitted_boundary_winner_forces_continuation(self) -> None:
        ranked = [self._ranked("retained", 0.25, 100)]
        response = {
            "envelope_complete": False,
            "best_omitted": self._tail("omitted", 1.0, 90),
        }
        self.assertFalse(search._native_frontier_certified(response, ranked, 1))

    def test_equal_upper_uses_only_known_time_and_session_prefix(self) -> None:
        ranked = [self._ranked("b", 0.5, 100)]
        cases = [
            (self._tail("a", 0.5, 90), True),
            (self._tail("a", 0.5, 110), False),
            (self._tail("c", 0.5, 100), True),
            (self._tail("a", 0.5, 100), False),
        ]
        for omitted, expected in cases:
            with self.subTest(omitted=omitted):
                response = {"envelope_complete": False,
                            "best_omitted": omitted}
                self.assertEqual(
                    search._native_frontier_certified(response, ranked, 1),
                    expected)

    def test_raw_upper_rounds_outward_before_time_can_certify(self) -> None:
        ranked = [self._ranked("retained", 0.5, 100)]
        response = {
            "envelope_complete": False,
            "best_omitted": self._tail("newer", 0.49999, 110),
        }
        self.assertFalse(search._native_frontier_certified(response, ranked, 1))

    def test_phrase_structure_precedes_all_terms_until_remaining_slots(self) -> None:
        ranked = [
            self._ranked("phrase-a", 0.2, 100),
            self._ranked("phrase-b", 0.1, 90),
            self._ranked("terms", 0.9, 110, matched="all-terms"),
        ]
        omitted = self._tail(
            "terms-tail", 0.95, 120, matched="all_terms")
        response = {"envelope_complete": False, "best_omitted": omitted}
        self.assertTrue(search._native_frontier_certified(response, ranked, 2))
        self.assertFalse(search._native_frontier_certified(response, ranked, 3))

    def test_combined_prose_and_tool_kth_is_the_certificate(self) -> None:
        ranked = [
            self._ranked("prose", 0.9, 100, who="user"),
            self._ranked("tool", 0.8, 90),
            self._ranked("older-prose", 0.7, 80, who="user"),
        ]
        response = {
            "envelope_complete": False,
            "best_omitted": self._tail("tail", 0.75, 110),
        }
        self.assertTrue(search._native_frontier_certified(response, ranked, 2))
        self.assertFalse(search._native_frontier_certified(response, ranked, 3))


class RefinedScoreProof(unittest.TestCase):
    @staticmethod
    def _candidate(session: str, ordinal: int, score: float) -> dict:
        return {
            "agent": "codex", "session": session, "ordinal": ordinal,
            "event_ordinal": ordinal, "ts": 100, "matched": "phrase",
            "occurrences": 1, "lower_score": score,
            "upper_score": score, "refined_score": True,
        }

    @staticmethod
    def _ranked(session: str, score: float) -> dict:
        hit = _hit(session, "needle")
        hit.update(ts=100, score=score, _boundary_factor=1.0)
        return hit

    def test_optimistic_order_is_paired_by_ordinal_without_mutating_wire(self) -> None:
        candidates = [
            self._candidate("second", 1, 0.2),
            self._candidate("first", 0, 0.3),
        ]
        before = [dict(candidate) for candidate in candidates]
        changed = search._native_verify_refined_scores(
            candidates,
            [self._ranked("first", 0.3), self._ranked("second", 0.2)],
            _spec(), None, 1_000.0)
        self.assertFalse(changed)
        self.assertEqual(candidates, before)

    def test_missing_exact_factor_is_recomputed_before_interval_check(self) -> None:
        candidate = self._candidate("one", 0, 0.2)
        hit = self._ranked("one", 0.8)
        hit.pop("_boundary_factor")
        with mock.patch.object(search, "_score", return_value=0.2) as scorer:
            changed = search._native_verify_refined_scores(
                [candidate], [hit], _spec(), object(), 1_000.0)
        self.assertTrue(changed)
        self.assertEqual(hit["score"], 0.2)
        scorer.assert_called_once()

    def test_refined_interval_mismatch_uses_the_exact_fallback(self) -> None:
        with self.assertRaisesRegex(
                search.NativeEventFallback, "refined score disagrees"):
            search._native_verify_refined_scores(
                [self._candidate("one", 0, 0.2)],
                [self._ranked("one", 0.3)], _spec(), None, 1_000.0)

    def test_refined_prefix_hydrates_only_possible_winners(self) -> None:
        candidates = [
            self._candidate(f"s{index:03d}", index, 0.9 - index / 1000)
            for index in range(100)
        ]
        response = {
            "candidates": candidates,
            "scanned": {"conservative_matches": 0},
            "best_omitted": None,
            "envelope_complete": True,
        }
        selected = search._native_hydration_candidates(response, 4)
        self.assertEqual(selected, candidates[:4])
        frontier = search._native_hydration_frontier(
            response, len(selected))
        self.assertFalse(frontier["envelope_complete"])
        self.assertIs(frontier["best_omitted"], candidates[4])
        ranked = [
            self._ranked(candidate["session"], candidate["lower_score"])
            for candidate in selected
        ]
        self.assertTrue(search._native_frontier_certified(frontier, ranked, 4))

    def test_overlapping_refined_intervals_are_all_hydrated(self) -> None:
        candidates = [
            self._candidate("a", 0, 0.8) | {"upper_score": 0.9},
            self._candidate("b", 1, 0.7) | {"upper_score": 0.85},
            self._candidate("c", 2, 0.79) | {"upper_score": 0.84},
            self._candidate("d", 3, 0.1) | {"upper_score": 0.2},
        ]
        selected = search._native_hydration_candidates({
            "candidates": candidates,
            "scanned": {"conservative_matches": 0},
        }, 2)
        self.assertEqual(selected, candidates[:3])

    def test_unresolved_intervals_and_exact_ties_stay_conservative(self) -> None:
        candidates = [self._candidate("same", index, 0.5)
                      for index in range(6)]
        refined = {"candidates": candidates,
                   "scanned": {"conservative_matches": 0}}
        self.assertEqual(
            search._native_hydration_candidates(refined, 2), candidates)
        conservative = {"candidates": candidates,
                        "scanned": {"conservative_matches": 1}}
        self.assertIs(
            search._native_hydration_candidates(conservative, 2), candidates)


class NativeBeforeMissingIndex(unittest.TestCase):
    def test_checked_unavailable_preflight_is_not_repeated(self) -> None:
        with mock.patch.object(
                explore, "native_event_scan_preflight",
                side_effect=AssertionError("preflight repeated")):
            result = search._jsonl_native_keyword(
                _spec(), {}, None, 10_000,
                preflight_ok=False, preflight_checked=True)
        self.assertIsNone(result)

    def test_unavailable_preflight_is_bound_to_a_native_snapshot(self) -> None:
        with mock.patch.object(explore, "_freshen"), \
                mock.patch.object(
                    explore, "freeze_native_event_filter", side_effect=dict), \
                mock.patch.object(
                    explore, "native_event_scan_preflight", return_value=False), \
                mock.patch.object(
                    explore, "native_event_scan_snapshot",
                    return_value={"generations": ("g", "e")}) as snapshot, \
                mock.patch.object(
                    explore, "native_event_scan_snapshot_current",
                    return_value=True):
            result = search._jsonl_native_keyword_once(
                _spec(), {}, None, 10_000)
        self.assertIsNone(result)
        snapshot.assert_called_once_with()

    def test_verified_native_tool_lane_precedes_missing_index_gate(self) -> None:
        native = {
            "hits": [], "pre_ranked": True,
            "bounded_rows": {
                "hits": [], "total": 0, "chats": 0,
                "phrase_chats": 0, "tool_hits": 0, "totals_exact": True,
            },
            "terms_fallback": False, "terms_augmented": False,
            "rank_now_ms": 123.0,
        }
        with mock.patch.object(search, "_load_corpusdb"), \
                mock.patch.object(search, "corpusdb", corpusdb), \
                mock.patch.object(corpusdb, "connect", return_value=None), \
                mock.patch.object(corpusdb, "_trigram_ok", return_value=True), \
                mock.patch.object(corpusdb, "DB_PATH", Path("/missing/corpus.db")), \
                mock.patch.object(
                    search, "_jsonl_native_keyword", return_value=native) as scan, \
                mock.patch.object(
                    indexd_runtime, "search_index_build_pending",
                    side_effect=AssertionError("missing-index gate ran")):
            result = search._keyword_candidates_once(_spec())
        self.assertEqual(result.engine, "jsonl+native-events")
        self.assertFalse(result.index_missing)
        self.assertEqual(result.rank_now_ms, 123.0)
        scan.assert_called_once()

    def test_tool_setting_is_frozen_before_snapshot_and_fallback(self) -> None:
        setting_reads = 0

        def setting(name: str):
            nonlocal setting_reads
            if name != "tools":
                return "off"
            setting_reads += 1
            if setting_reads == 1:
                return "off"
            raise AssertionError("tools setting reread")

        prose = {"hits": [], "term_hits": []}
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            messages = root / "messages.jsonl"
            messages.write_text("{}\n", encoding="utf-8")
            with mock.patch.object(search, "_load_corpusdb"), \
                    mock.patch.object(search, "corpusdb", corpusdb), \
                    mock.patch.object(corpusdb, "connect", return_value=None), \
                    mock.patch.object(corpusdb, "_trigram_ok", return_value=True), \
                    mock.patch.object(corpusdb, "DB_PATH", root / "corpus.db"), \
                    mock.patch.object(search.common, "MESSAGES_PATH", messages), \
                    mock.patch.object(search.common, "setting", side_effect=setting), \
                    mock.patch.object(
                        search, "_jsonl_native_keyword",
                        side_effect=AssertionError("disabled native lane ran")), \
                    mock.patch.object(
                        search, "_jsonl_bounded_single_keyword_rows",
                        return_value=None), \
                    mock.patch.object(
                        explore, "keyword_search", return_value=prose) as scan, \
                    mock.patch.object(
                        explore, "direct_snapshot_attempt",
                        side_effect=lambda **_kwargs: contextlib.nullcontext()) \
                        as direct_snapshot:
                result = search._keyword_candidates_once(_spec(who=None))

        self.assertEqual(setting_reads, 1)
        direct_snapshot.assert_called_once_with(include_events=False)
        self.assertFalse(scan.call_args.args[2]["_tool_lane_enabled"])
        self.assertEqual(result.engine, "jsonl")

    def test_family_window_is_frozen_after_freshen(self) -> None:
        state = {"generation": "g0"}

        def freshen() -> None:
            state["generation"] = "g1"

        def messages() -> dict:
            ts = 200 if state["generation"] == "g1" else 100
            return {"caller": [{
                "session": "caller", "agent": "codex", "project": "fixture",
                "ts": ts, "turn": 7,
            }]}

        prose = {
            "hits": [], "total": 0, "chats": 0, "phrase_chats": 0,
            "tool_hits": 0, "totals_exact": True, "_matched_sessions": set(),
        }
        stable = {
            "state": "ok", "ingest_generation": "ingest",
            "event_generation": "events",
            "scanned": {"sessions": 0, "events": 0, "bytes": 0},
            "matches": {
                "tools": 0, "phrase_tools": 0, "all_terms_tools": 0,
                "all_terms_additions": 0, "matched_sessions": 0,
                "eligible_sessions": 0, "matched_owner_bitmap": "",
                "phrase_owner_bitmap": "", "owner_order_sha256": "0" * 64,
            },
            "candidates": [], "best_omitted": None, "next_after": None,
            "envelope_complete": True, "_owner_order": [],
        }
        spec = _spec(
            who=None, exclude_session="caller", exclude_session_from_turn=7)
        with mock.patch.object(explore, "_freshen", side_effect=freshen), \
                mock.patch.object(
                    explore, "native_event_scan_snapshot",
                    return_value={"generations": ("ingest", "events")}), \
                mock.patch.object(
                    explore, "native_event_scan_snapshot_current",
                    return_value=True), \
                mock.patch.object(
                    explore, "native_event_owner_census_matches",
                    return_value=True), \
                mock.patch.object(explore, "_messages_by_session", side_effect=messages), \
                mock.patch.object(
                    explore.common, "indexed_calling_family_with_sides",
                    return_value=None), \
                mock.patch.object(
                    search, "_jsonl_bounded_single_keyword_rows",
                    return_value=prose), \
                mock.patch.object(
                    explore, "native_event_keyword_scan",
                    return_value=stable) as native_scan:
            search._jsonl_native_keyword_once(
                spec, {
                    "exclude_session": "caller",
                    "exclude_session_from_turn": 7,
                }, search._prepare_boundary(spec.q, spec.mode, None),
                10_000, preflight_ok=True)
        frozen = native_scan.call_args.args[2]
        self.assertEqual(
            frozen["_native_caller_event_window"]["marks"],
            [{"ts": 200, "turn": 7}])

    def test_native_timeout_falls_back_to_the_full_python_event_lane(self) -> None:
        spec = _spec("alpha beta", who=None)
        prose = {"hits": [], "term_hits": []}
        tool = _hit("tool-result", "alpha beta", row_key=0)
        complete = {"hits": [tool], "term_hits": [dict(tool)]}
        failed = {
            "state": "unsupported", "_native_started": True,
            "_native_no_payload_work": False, "detail": "timed out",
        }
        with tempfile.TemporaryDirectory() as raw:
            messages_path = Path(raw) / "messages.jsonl"
            messages_path.write_text("{}\n", encoding="utf-8")
            with mock.patch.object(search, "_load_corpusdb"), \
                    mock.patch.object(search, "corpusdb", corpusdb), \
                    mock.patch.object(corpusdb, "connect", return_value=None), \
                    mock.patch.object(
                        corpusdb, "DB_PATH", Path("/missing/corpus.db")), \
                    mock.patch.object(corpusdb, "_trigram_ok", return_value=False), \
                    mock.patch.object(
                        search.common, "MESSAGES_PATH", messages_path), \
                    mock.patch.object(
                        explore, "native_event_scan_preflight", return_value=True), \
                    mock.patch.object(
                        explore, "native_event_scan_snapshot",
                        return_value={"generations": ("ingest", "events")}), \
                    mock.patch.object(
                        explore, "native_event_scan_snapshot_current",
                        return_value=True), \
                    mock.patch.object(
                        explore, "native_event_owner_census_matches",
                        return_value=True), \
                    mock.patch.object(
                        explore, "keyword_search",
                        side_effect=[prose, complete]) as scan, \
                    mock.patch.object(
                        explore, "direct_snapshot_attempt",
                        side_effect=lambda **_kwargs: contextlib.nullcontext()) \
                        as direct_snapshot, \
                    mock.patch.object(
                        explore, "native_event_keyword_scan", return_value=failed), \
                    mock.patch.object(search, "_announce_slow_lane"):
                result = search._keyword_candidates_once(spec)
        self.assertEqual(scan.call_count, 2)
        self.assertTrue(scan.call_args_list[0].args[2]["_skip_event_rows"])
        self.assertFalse(scan.call_args_list[1].args[2].get("_skip_event_rows"))
        direct_snapshot.assert_called_once_with(include_events=True)
        self.assertEqual(result.engine, "jsonl")
        self.assertEqual([hit["session"] for hit in result.hits], ["tool-result"])

    def test_orphan_event_store_falls_back_to_published_owner_scan(self) -> None:
        spec = _spec("alpha beta", who=None)
        prose = {"hits": [], "term_hits": []}
        tool = _hit("tool-result", "alpha beta", row_key=0)
        complete = {"hits": [tool], "term_hits": [dict(tool)]}
        orphan = {
            "state": "integrity_error", "candidates": [],
            "detail": "event row identity has no published owner",
        }
        with tempfile.TemporaryDirectory() as raw:
            messages_path = Path(raw) / "messages.jsonl"
            messages_path.write_text("{}\n", encoding="utf-8")
            with mock.patch.object(search, "_load_corpusdb"), \
                    mock.patch.object(search, "corpusdb", corpusdb), \
                    mock.patch.object(corpusdb, "connect", return_value=None), \
                    mock.patch.object(
                        corpusdb, "DB_PATH", Path("/missing/corpus.db")), \
                    mock.patch.object(corpusdb, "_trigram_ok", return_value=False), \
                    mock.patch.object(
                        search.common, "MESSAGES_PATH", messages_path), \
                    mock.patch.object(
                        explore, "native_event_scan_preflight", return_value=True), \
                    mock.patch.object(
                        explore, "native_event_scan_snapshot",
                        return_value={"generations": ("ingest", "events")}), \
                    mock.patch.object(
                        explore, "native_event_scan_snapshot_current",
                        return_value=True), \
                    mock.patch.object(
                        explore, "native_event_owner_census_matches",
                        return_value=True), \
                    mock.patch.object(
                        explore, "keyword_search",
                        side_effect=[prose, complete]) as scan, \
                    mock.patch.object(
                        explore, "direct_snapshot_attempt",
                        side_effect=lambda **_kwargs: contextlib.nullcontext()) \
                        as direct_snapshot, \
                    mock.patch.object(
                        explore, "native_event_keyword_scan", return_value=orphan), \
                    mock.patch.object(
                        search.indexd_runtime, "kick_background_repair"), \
                    mock.patch.object(search, "_announce_slow_lane"):
                result = search._keyword_candidates_once(spec)
        self.assertEqual(scan.call_count, 2)
        self.assertTrue(scan.call_args_list[0].args[2]["_skip_event_rows"])
        self.assertFalse(scan.call_args_list[1].args[2].get("_skip_event_rows"))
        direct_snapshot.assert_called_once_with(include_events=True)
        self.assertEqual(result.engine, "jsonl")
        self.assertEqual(result.bounded_rows, None)
        self.assertEqual([hit["session"] for hit in result.hits], ["tool-result"])

    def test_published_owner_integrity_mismatch_still_fails_closed(self) -> None:
        prose = {
            "hits": [], "total": 0, "chats": 0, "phrase_chats": 0,
            "tool_hits": 0, "totals_exact": True, "_matched_sessions": set(),
        }
        corrupt = {
            "state": "integrity_error", "candidates": [],
            "detail": "event row filename does not match its owner identity",
        }
        with mock.patch.object(explore, "_freshen"), \
                mock.patch.object(
                    explore, "native_event_scan_snapshot",
                    return_value={"generations": ("ingest", "events")}), \
                mock.patch.object(
                    explore, "native_event_scan_snapshot_current",
                    return_value=True), \
                mock.patch.object(
                    explore, "native_event_owner_census_matches",
                    return_value=True), \
                mock.patch.object(
                    search, "_jsonl_bounded_single_keyword_rows",
                    return_value=prose), \
                mock.patch.object(
                    explore, "native_event_keyword_scan", return_value=corrupt), \
                mock.patch.object(
                    search.indexd_runtime, "kick_background_repair"):
            with self.assertRaisesRegex(
                    search.NativeEventScanError, "filename does not match"):
                search._jsonl_native_keyword_once(
                    _spec(who=None, limit=1), {},
                    search._prepare_boundary("needle", "keyword", None),
                    10_000, preflight_ok=True)

    def test_generation_move_discards_the_attempt_for_shared_restart(self) -> None:
        def prose(session: str) -> dict:
            return {
                "hits": [_hit(session, "needle", who="user")],
                "total": 1, "chats": 1, "phrase_chats": 1,
                "tool_hits": 0, "totals_exact": True,
                "_matched_sessions": {session},
            }

        moved = {
            "state": "generation_moved", "detail": "moved",
            "candidates": [],
        }
        with mock.patch.object(
                explore, "native_event_scan_preflight", return_value=True), \
                mock.patch.object(
                    explore, "native_event_scan_snapshot",
                    return_value={"generations": ("g1", "e1")}), \
                mock.patch.object(
                    explore, "native_event_scan_snapshot_current",
                    return_value=True), \
                mock.patch.object(
                    explore, "native_event_owner_census_matches",
                    return_value=True), \
                mock.patch.object(
                    search, "_jsonl_bounded_single_keyword_rows",
                    return_value=prose("g1")) as prose_scan, \
                mock.patch.object(
                    explore, "native_event_keyword_scan",
                    return_value=moved) as native_scan, \
                mock.patch.object(explore, "_freshen") as freshen, \
                self.assertRaisesRegex(
                    search.NativeEventScanError, "moved"):
            search._jsonl_native_keyword(
                _spec(who=None, limit=1), {},
                search._prepare_boundary("needle", "keyword", None), 10_000,
                preflight_ok=True)
        self.assertEqual(prose_scan.call_count, 1)
        self.assertEqual(freshen.call_count, 1)
        self.assertEqual(
            [call.kwargs["expected_generations"]
             for call in native_scan.call_args_list],
            [("g1", "e1")])

    def test_owner_census_mismatch_fails_before_native_filtering(self) -> None:
        prose = {
            "hits": [], "total": 0, "chats": 0, "phrase_chats": 0,
            "tool_hits": 0, "totals_exact": True, "_matched_sessions": set(),
        }
        with mock.patch.object(explore, "_freshen"), \
                mock.patch.object(
                    explore, "native_event_scan_snapshot",
                    return_value={"generations": ("g", "e")}), \
                mock.patch.object(
                    explore, "native_event_scan_snapshot_current",
                    return_value=True), \
                mock.patch.object(
                    explore, "native_event_owner_census_matches",
                    return_value=False), \
                mock.patch.object(
                    search, "_jsonl_bounded_single_keyword_rows",
                    return_value=prose), \
                mock.patch.object(
                    explore, "native_event_keyword_scan",
                    side_effect=AssertionError("native filter ran")), \
                mock.patch.object(explore, "_kick_derived_repair") as repair:
            with self.assertRaisesRegex(
                    search.NativeEventScanError, "owners disagree"):
                search._jsonl_native_keyword_once(
                    _spec(who=None, limit=1), {},
                    search._prepare_boundary("needle", "keyword", None),
                    10_000, preflight_ok=True)
        repair.assert_called_once_with()

    def test_generation_swap_while_freezing_defers_to_shared_restart(self) -> None:
        prose = {
            "hits": [], "total": 0, "chats": 0, "phrase_chats": 0,
            "tool_hits": 0, "totals_exact": True, "_matched_sessions": set(),
        }
        state = {"generation": 1, "moved": False}
        order = []

        def capture():
            generation = state["generation"]
            order.append(f"capture:{generation}")
            return {"generations": (f"g{generation}", f"e{generation}")}

        def freeze(flt):
            generation = state["generation"]
            order.append(f"freeze:{generation}")
            if not state["moved"]:
                state.update(generation=2, moved=True)
            return {**flt, "_native_family_frozen": True}

        def current(snapshot):
            expected = int(snapshot["generations"][0][1:])
            return expected == state["generation"]

        with mock.patch.object(
                explore, "native_event_scan_snapshot", side_effect=capture), \
                mock.patch.object(explore, "_freshen"), \
                mock.patch.object(
                    explore, "freeze_native_event_filter", side_effect=freeze), \
                mock.patch.object(
                    explore, "native_event_scan_preflight", return_value=True), \
                mock.patch.object(
                    explore, "native_event_scan_snapshot_current",
                    side_effect=current), \
                mock.patch.object(
                    explore, "native_event_owner_census_matches",
                    return_value=True), \
                mock.patch.object(
                    search, "_jsonl_bounded_single_keyword_rows",
                    return_value=prose) as prose_scan, \
                mock.patch.object(
                    explore, "native_event_keyword_scan",
                    side_effect=AssertionError("native page ran")) as native_scan, \
                self.assertRaises(search.NativeEventScanError):
            search._jsonl_native_keyword(
                _spec(who=None, limit=1), {},
                search._prepare_boundary("needle", "keyword", None), 10_000,
                preflight_ok=True)

        self.assertEqual(order[:2], ["capture:1", "freeze:1"])
        self.assertEqual(order[2:], [])
        prose_scan.assert_not_called()
        native_scan.assert_not_called()

    def test_derived_move_after_prose_defers_to_shared_restart(self) -> None:
        prose = {
            "hits": [], "total": 0, "chats": 0, "phrase_chats": 0,
            "tool_hits": 0, "totals_exact": True, "_matched_sessions": set(),
        }
        with mock.patch.object(
                explore, "native_event_scan_preflight", return_value=True), \
                mock.patch.object(explore, "_freshen"), \
                mock.patch.object(
                    explore, "native_event_scan_snapshot",
                    return_value={"generations": ("g1", "e1")}), \
                mock.patch.object(
                    explore, "native_event_scan_snapshot_current",
                    side_effect=[True, False]), \
                mock.patch.object(
                    explore, "native_event_owner_census_matches",
                    return_value=True), \
                mock.patch.object(
                    search, "_jsonl_bounded_single_keyword_rows",
                    return_value=prose) as prose_scan, \
                mock.patch.object(
                    explore, "native_event_keyword_scan",
                    side_effect=AssertionError("native page ran")) as native_scan, \
                self.assertRaises(search.NativeEventScanError):
            search._jsonl_native_keyword(
                _spec(who=None, limit=1), {},
                search._prepare_boundary("needle", "keyword", None), 10_000,
                preflight_ok=True)
        self.assertEqual(prose_scan.call_count, 1)
        native_scan.assert_not_called()


class NativeContinuation(unittest.TestCase):
    @staticmethod
    def _candidate(session: str, ts: int, ordinal: int) -> dict:
        return {
            "agent": "codex", "session": session, "ordinal": ordinal,
            "event_ordinal": ordinal, "ts": ts, "matched": "phrase",
            "occurrences": 1, "upper_score": 0.11,
            "lower_score": 0.0132, "refined_score": False,
        }

    @staticmethod
    def _response(candidates: list[dict], *, omitted: dict | None) -> dict:
        owners = [
            {"agent": "codex", "session": "newer"},
            {"agent": "codex", "session": "older"},
        ]
        return {
            "state": "ok", "ingest_generation": "ingest",
            "event_generation": "events", "scanned": {
                "sessions": 2, "events": 2, "bytes": 200,
                "candidate_sessions": 2, "candidate_events": 2,
                "candidate_bytes": 200,
            },
            "matches": {
                "tools": 2, "phrase_tools": 2, "all_terms_tools": 2,
                "all_terms_additions": 0, "matched_sessions": 2,
                "eligible_sessions": 2, "matched_owner_bitmap": "03",
                "phrase_owner_bitmap": "03", "owner_order_sha256": "owners",
            },
            "candidates": candidates, "best_omitted": omitted,
            "next_after": (search._native_storage_cursor(candidates[-1])
                           if omitted is not None else None),
            "envelope_complete": omitted is None, "_owner_order": owners,
        }

    def test_ambiguous_page_falls_back_without_a_second_native_scan(self) -> None:
        newer = self._candidate("newer", 1_999_999, 0)
        older = self._candidate("older", 1_999_998, 1)
        response = self._response(
            [newer], omitted=search._native_storage_cursor(older))
        prose = {"hits": [], "term_hits": []}

        def hydrate(_query, response):
            return [
                _hit(candidate["session"], "alpha beta", turn=1)
                | {"ts": candidate["ts"]}
                for candidate in response["candidates"]
            ]

        def boundaries(rows, _evidence):
            for hit in rows:
                hit["_boundary_score_factor"] = (
                    0.25 if hit["session"] == "newer" else 1.0)
            return True

        with mock.patch.object(
                explore, "keyword_search", return_value=prose) as prose_scan, \
                mock.patch.object(
                    explore, "native_event_scan_snapshot",
                    return_value={"generations": ("ingest", "events")}), \
                mock.patch.object(
                    explore, "native_event_scan_snapshot_current",
                    return_value=True), \
                mock.patch.object(
                    explore, "native_event_owner_census_matches",
                    return_value=True), \
                mock.patch.object(
                    explore, "native_event_keyword_scan",
                    return_value=response) as native_scan, \
                mock.patch.object(
                    explore, "native_event_candidate_hits",
                    side_effect=hydrate), \
                mock.patch.object(
                    search, "_native_boundary_scores", side_effect=boundaries), \
                mock.patch.object(search, "_NATIVE_EVENT_CANDIDATE_PAGE", 1), \
                mock.patch.object(search.time, "time", return_value=2_000):
            with self.assertRaisesRegex(
                    search.NativeEventFallback, "remained ambiguous"):
                search._jsonl_native_keyword_once(
                    _spec("alpha beta", who=None, limit=1), {},
                    search._prepare_boundary("alpha beta", "keyword", None),
                    10_000, preflight_ok=True)
        self.assertEqual(prose_scan.call_count, 1)
        native_scan.assert_called_once()
        self.assertEqual(native_scan.call_args.kwargs["candidate_limit"], 1)
        self.assertIsNone(native_scan.call_args.kwargs["after"])
        self.assertEqual(
            native_scan.call_args.kwargs["expected_generations"],
            ("ingest", "events"))


class LazyBoundedRows(unittest.TestCase):
    @staticmethod
    def _entries():
        base = {
            "agent": "codex", "project": "fixture", "concept": "",
            "model": "", "model_source": "tool", "who": "tool",
        }
        events = [
            {
                "ts": 1_900_000_000_003, "kind": "tool", "name": "Read",
                "input": '{"note":"needle alpha"}',
                "output": "needle output one", "ok": True,
                "call_id": "call-one", "output_chars": 17,
                "output_bytes": 17,
            },
            {
                "ts": 1_900_000_000_002, "kind": "tool", "name": "Bash",
                "input": '{"cmd":"needle beta"}',
                "output": "failed needle output", "ok": False,
                "call_id": "call-two", "output_chars": 20,
                "output_bytes": 20,
            },
            {
                "ts": 1_900_000_000_001, "kind": "tool", "name": "Write",
                "input": '{"body":"needle gamma"}',
                "output": "needle output three", "ok": True,
                "call_id": "call-three", "output_chars": 19,
                "output_bytes": 19,
            },
        ]
        return [(
            ordinal,
            {**base, "session": f"session-{ordinal}", "turn": ordinal,
             "ts": event["ts"], "_agrep_tool_event": event,
             "_agrep_occurrences": 2},
            -1, -1,
        ) for ordinal, event in enumerate(events)]

    def _run(self, *, eager: bool) -> dict:
        original_hit = explore.scan_hit
        original_materialize = explore.materialize_single_keyword_match

        def eager_hit(entry, start, end, **_kwargs):
            return original_hit(entry, start, end)

        def eager_materialize(entry, query, **_kwargs):
            return original_materialize(entry, query)

        patches = [mock.patch.object(
            explore, "single_keyword_matches", return_value=iter(self._entries()))]
        if eager:
            patches.extend([
                mock.patch.object(explore, "scan_hit", side_effect=eager_hit),
                mock.patch.object(
                    explore, "materialize_single_keyword_match",
                    side_effect=eager_materialize),
            ])
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            return search._jsonl_bounded_single_keyword_rows(
                _spec(limit=2), {},
                search._prepare_boundary("needle", "keyword", None),
                now_ms=1_900_000_000_100)

    def test_lazy_rows_match_eager_full_dicts_and_strong_provenance(self) -> None:
        eager = self._run(eager=True)
        lazy = self._run(eager=False)
        self.assertEqual(lazy, eager)
        self.assertEqual(len(lazy["hits"]), 2)
        for hit in lazy["hits"]:
            self.assertRegex(hit["content_digest"], r"\A[0-9a-f]{4}\Z")
            self.assertRegex(hit["_event_identity"], r"\A[0-9a-f]{24}\Z")
            self.assertIn("call_id", hit)

    def test_control_events_are_rejected_eagerly_and_lazily(self) -> None:
        entry = self._entries()[0][1]
        entry["_agrep_tool_event"] = {
            "kind": "control", "name": "needle", "input": "needle"}
        for provenance in (False, True):
            with self.subTest(provenance=provenance), self.assertRaises(ValueError):
                explore.materialize_single_keyword_match(
                    entry, "needle", provenance=provenance)


class BoundedMultiTermNative(unittest.TestCase):
    def test_exact_aggregates_do_not_require_every_tool_candidate(self) -> None:
        spec = _spec("alpha beta", limit=2)
        phrase = _hit("prose-phrase", "alpha beta", who="user", row_key=0)
        term = _hit(
            "prose-terms", "alpha ... beta", who="user",
            matched="all-terms", row_key=1)
        tool_phrase = _hit("tool-phrase", "alpha beta")
        tool_terms = _hit("tool-terms", "alpha ... beta", matched="all-terms")
        tool_phrase["ts"] = 100
        tool_terms["ts"] = 90
        response = {
            "state": "ok", "ingest_generation": "ingest",
            "event_generation": "events", "scanned": {},
            "matches": {"tools": 10, "phrase_tools": 4},
            "candidates": [
                {"agent": "codex", "session": "tool-phrase", "ordinal": 0,
                 "event_ordinal": 0, "ts": 100, "matched": "phrase",
                 "occurrences": 1, "upper_score": 0.11,
                 "lower_score": 0.0132, "refined_score": False},
                {"agent": "codex", "session": "tool-terms", "ordinal": 1,
                 "event_ordinal": 0, "ts": 90, "matched": "all_terms",
                 "occurrences": 0, "upper_score": 0.11,
                 "lower_score": 0.0132, "refined_score": False},
            ], "best_omitted": None, "next_after": None,
            "envelope_complete": True,
            "_owner_order": [
                {"session": "tool-phrase"}, {"session": "tool-terms"},
            ],
        }
        response["matches"].update({
            "matched_owner_bitmap": "03", "phrase_owner_bitmap": "01",
        })
        prose = {"hits": [phrase], "term_hits": [phrase.copy(), term]}
        with mock.patch.object(
                explore, "keyword_search", return_value=prose), \
                mock.patch.object(
                    explore, "native_event_scan_snapshot",
                    return_value={"generations": ("ingest", "events")}), \
                mock.patch.object(
                    explore, "native_event_scan_snapshot_current",
                    return_value=True), \
                mock.patch.object(
                    explore, "native_event_owner_census_matches",
                    return_value=True), \
                mock.patch.object(
                    explore, "native_event_keyword_scan", return_value=response), \
                mock.patch.object(
                    explore, "native_event_candidate_hits",
                    return_value=[tool_phrase, tool_terms]), \
                mock.patch.object(
                    search, "_native_boundary_scores", return_value=False), \
                mock.patch.object(search.time, "time", return_value=2_000_000_000):
            result = search._jsonl_native_keyword_once(
                spec, {}, search._prepare_boundary(spec.q, spec.mode, None),
                10_000, preflight_ok=True)
        self.assertEqual(result["bounded_rows"]["total"], 12)
        self.assertEqual(result["bounded_rows"]["tool_hits"], 10)
        self.assertEqual(result["bounded_rows"]["chats"], 4)
        self.assertEqual(result["bounded_rows"]["phrase_chats"], 2)
        self.assertEqual(len(result["hits"]), 2)
        self.assertTrue(result["terms_augmented"])
        self.assertTrue(result["pre_ranked"])


if __name__ == "__main__":
    unittest.main()
