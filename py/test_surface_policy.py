from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))
sys.path.insert(0, str(ROOT))

from _test_support import isolate_data_dir  # noqa: E402

isolate_data_dir()

import around  # noqa: E402
import cli  # noqa: E402
import common  # noqa: E402
import doctor  # noqa: E402
import explore  # noqa: E402
import embed  # noqa: E402
import embedder  # noqa: E402
import indexd_runtime  # noqa: E402
import livetui  # noqa: E402
import recall  # noqa: E402
import session_context  # noqa: E402
import resume  # noqa: E402
import search  # noqa: E402
import surface_policy as surface  # noqa: E402


class _ParserCaptured(Exception):
    pass


def _who_choices(main) -> object:
    found: list[object] = []

    def capture(parser, args=None, namespace=None):
        found.append(next(
            action.choices for action in parser._actions
            if action.dest == "who"))
        raise _ParserCaptured

    with mock.patch.object(argparse.ArgumentParser, "parse_args", capture), \
            contextlib.redirect_stderr(io.StringIO()), \
            contextlib.redirect_stdout(io.StringIO()), \
            unittest.TestCase().assertRaises(_ParserCaptured):
        main([])
    return found[0]


class SharedSurfacePolicyTests(unittest.TestCase):
    def test_remedies_classify_and_auto_remedies_own_a_mechanism(self) -> None:
        kinds = {"auto", "consent", "privilege", "human-prereq"}
        for name, remedy in surface.REMEDIES.items():
            self.assertIn(remedy.kind, kinds, name)
            self.assertTrue(remedy.owner, f"{name} names no owner")
            if remedy.kind == "auto":
                # law 7: an auto remedy reports its mechanism; telling the
                # reader to run a command means it is not auto
                self.assertNotIn("`", remedy.text, name)

    def test_remedy_renderers_consume_the_registry(self) -> None:
        self.assertIn(surface.REMEDIES["stale-handle"].text,
                      surface.stale_handle_recovery("agrep"))
        installed = doctor._installed_build_detail({
            "state": "lagging",
            "detail": "fixture lag",
            "remedy": "replace-installed-tool",
            "remedy_argv": [
                "uv", "tool", "install", "--force", "--from",
                "/fixture source", "agrep",
            ],
        })
        before, after = surface.REMEDIES["replace-installed-tool"].text.split(
            "{command}")
        self.assertIn(before, installed)
        self.assertIn(after, installed)

    def test_semantic_score_bands_have_one_runtime_default(self) -> None:
        bands = surface.DEFAULT_SEMANTIC_SCORE_BANDS
        self.assertIs(embedder.semantic_bands(), bands)
        self.assertIs(search.SEMANTIC_SCORE_BANDS, bands)
        self.assertEqual(search.SEMANTIC_MIN_COSINE, bands.floor)
        self.assertEqual(search._RECALL_STRONG_SEM, bands.strong)
        self.assertLess(bands.floor, bands.strong)
        self.assertGreaterEqual(bands.floor, -1.0)
        self.assertLessEqual(bands.strong, 1.0)

    def test_public_setting_metadata_drives_dispatch_listing_and_defaults(self) -> None:
        self.assertIs(cli.KNOWN_SETTINGS, surface.PUBLIC_SETTING_CHOICES)
        with tempfile.TemporaryDirectory() as td, \
                mock.patch("settings.SETTINGS_PATH", Path(td) / "settings.json"):
            for spec in surface.SETTING_SPECS:
                self.assertEqual(
                    common.setting(spec.name),
                    surface.setting_default(spec.name),
                )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(cli.cmd_set(SimpleNamespace(rest=[])), 0)
        rendered = output.getvalue()
        for spec in surface.PUBLIC_SETTINGS:
            self.assertIn(f"{spec.name} = {spec.default}", rendered)
            self.assertIn(spec.value_help, rendered)

    def test_parser_choices_are_bound_to_shared_per_verb_policy(self) -> None:
        # search/recall route --who/--no-who through the one owned parser
        # (surface_policy.speaker_filter over the per-verb choices); around
        # still binds argparse choices to the shared tuple directly
        for main, choices in ((search.main, surface.SEARCH_SPEAKER_CHOICES),
                              (recall.main, surface.RECALL_SPEAKER_CHOICES)):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), \
                    self.assertRaises(SystemExit):
                main(["needle", "--who", "not-a-speaker"])
            self.assertIn(", ".join(choices), stderr.getvalue())
        self.assertIs(_who_choices(around.main), surface.AROUND_SPEAKER_CHOICES)
        self.assertIn("tool", surface.AROUND_SPEAKER_CHOICES)
        self.assertTrue(surface.around_speaker_matches("you", "user"))
        self.assertFalse(surface.around_speaker_matches("user", "recap"))

    def test_palette_glyph_and_width_consumers_share_objects(self) -> None:
        self.assertIs(common.PALETTE, surface.PALETTE)
        self.assertIs(search._C, surface.PALETTE)
        self.assertIs(around._C, surface.PALETTE)
        self.assertIs(resume._C, surface.PALETTE)
        self.assertIs(doctor._GLYPH, surface.STATUS_GLYPHS)
        self.assertIs(livetui._EVENT_GLYPH, surface.EVENT_GLYPHS)
        self.assertIs(livetui._cells, surface.cell_width)
        self.assertIs(livetui._pad_cells, surface.pad_cells)
        self.assertEqual(surface.cell_width("界"), 2)
        self.assertEqual(surface.cell_width("👩\u200d💻"), 2)
        self.assertEqual(surface.cell_width(surface.pad_cells("界", 4)), 4)
        self.assertEqual(surface.truncate_cells("a界b", 3), "a…")
        event = {"type": "tool", "text": "shell"}
        self.assertTrue(livetui._event_line(event, 20).startswith(
            surface.GLYPHS.tool))

    def test_governor_short_circuits_and_runtime_uses_shared_reason(self) -> None:
        policy = surface.SEMANTIC_GOVERNOR
        memory = mock.Mock(return_value=policy.memory_floor / 2)
        battery = mock.Mock()
        load = mock.Mock()
        deferred = surface.observed_semantic_deferral(memory, battery, load)
        self.assertEqual(deferred.code, "memory-pressure")
        battery.assert_not_called()
        load.assert_not_called()

        memory = mock.Mock(return_value=policy.memory_floor)
        battery = mock.Mock(
            return_value=(True, policy.battery_floor_pct - 1))
        load = mock.Mock()
        deferred = surface.observed_semantic_deferral(memory, battery, load)
        self.assertEqual(deferred.code, "battery")
        load.assert_not_called()

        expected = surface.SemanticDeferral(
            "fixture", "runtime fixture", "surface fixture")
        with mock.patch.object(
                embed.surface, "observed_semantic_deferral",
                return_value=expected) as shared:
            self.assertEqual(embed._governor_deferral(), expected.runtime_reason)
        shared.assert_called_once()

    def test_freshness_threshold_tail_and_handle_recovery_are_shared(self) -> None:
        policy = surface.FreshnessPolicy(
            failure_threshold=7,
            last_good_index_tail="shared last-good fixture",
        )
        with tempfile.TemporaryDirectory() as td:
            signature = Path(td) / ".ingest.sig"
            signature.touch()
            with mock.patch.object(
                    surface, "FRESHNESS_POLICY", policy), \
                    mock.patch.object(common, "DATA_DIR", Path(td)), \
                    mock.patch.object(
                        common, "INGEST_SIG_PATH", signature):
                self.assertFalse(surface.persistent_freshness_failure(6))
                self.assertTrue(surface.persistent_freshness_failure(7))
                self.assertEqual(
                    surface.FRESHNESS_POLICY.last_good_index_tail,
                    "shared last-good fixture",
                )
                with mock.patch.object(
                        indexd_runtime, "_auto_index_health",
                        return_value=indexd_runtime.AutoIndexHealth(
                            "available", 6, "not visible yet", 0.0, False)):
                    self.assertIsNone(indexd_runtime.indexing_failure())
                with mock.patch.object(
                        indexd_runtime, "_auto_index_health",
                        return_value=indexd_runtime.AutoIndexHealth(
                            "available", 7, "shared threshold fixture", 0.0,
                            False)):
                    failure = indexd_runtime.indexing_failure()
                self.assertEqual(failure.consecutive_failures, 7)
                self.assertEqual(failure.reason, "shared threshold fixture")
        self.assertEqual(
            surface.stale_handle_recovery("agrep"),
            "the handle is stale; rerun the search - fresh results mint "
            "current handles.",
        )

    def test_wall_clock_age_is_no_longer_a_read_time_verdict(self) -> None:
        # goal 10 D3: freshness is drift, not duty-cycle. The 120s constant
        # survives only as indexd_runtime's write-side stamp rate limit, and
        # the read side renders exactly one of the three drift states.
        self.assertFalse(hasattr(surface, "FRESHNESS_MAX_AGE_S"))
        self.assertFalse(hasattr(surface, "freshness_signal_failure"))
        self.assertEqual(
            surface.freshness_story_line(surface.FreshnessStory("current")),
            "")
        behind = surface.freshness_story_line(surface.FreshnessStory(
            "behind", behind_s=2520.0, changed_stores=2, converging=True))
        self.assertIn("42m behind", behind)
        self.assertIn("2 stores changed", behind)
        self.assertIn("catching up", behind)
        # law 7 inverse: no daemon, no convergence promise - name the remedy
        stranded = surface.freshness_story_line(surface.FreshnessStory(
            "behind", behind_s=2520.0, changed_stores=2))
        self.assertIn("42m behind", stranded)
        self.assertNotIn("catching up", stranded)
        self.assertIn(
            surface.REMEDIES["index-behind-manual"].text, stranded)
        failing = surface.freshness_story_line(surface.FreshnessStory(
            "failing", consecutive_failures=44, last_good_age_s=3600.0,
            detail="ingest exited 101"))
        self.assertIn("44 consecutive", failing)
        self.assertIn("last-good index from 60m ago", failing)
        self.assertIn("ingest exited 101", failing)
        self.assertIn(
            surface.REMEDIES["auto-rebuild-pending"].text, failing)

    def test_status_uses_the_shared_nonrunning_failure_source(self) -> None:
        failure = surface.FreshnessFailure(
            "missing-ingest-binary", "ingest binary is missing (fixture)")
        summaries = (
            None,
            {
                "sessions": 1, "messages": 2, "agents": 1,
                "per_agent": [], "age_s": 3,
            },
        )
        for summary in summaries:
            with self.subTest(index_built=summary is not None), \
                    mock.patch.object(
                        common, "index_summary", return_value=summary), \
                    mock.patch.object(
                        indexd_runtime, "indexing_failure",
                        return_value=failure):
                status = cli._status_core()
            self.assertEqual(len(status["warnings"]), 1)
            # the shared table owns the sentence: consequence plus command,
            # and the internal reason stays in the machine freshness record
            self.assertEqual(
                status["warnings"][0],
                surface.indexing_advice_line(failure, common.cli_name()))
            self.assertIn("`", status["warnings"][0])
            self.assertNotIn(failure.reason, status["warnings"][0])
            self.assertNotIn("ingest binary", status["warnings"][0])

    def test_search_recall_and_around_render_one_shared_freshness_notice(self) -> None:
        failure = surface.FreshnessFailure(
            "fixture", "shared failing-index fixture")
        result = {
            "hits": [], "total": 0, "chats": 0, "tool_hits": 0,
            "engine": "fixture", "mode": "keyword", "totals_exact": True,
        }
        window = {
            "session": "freshness-session", "project": "project",
            "agent": "codex", "concept": "", "title": "", "center": 1,
            "first_turn": 1, "last_turn": 1, "events": [],
            "turns": [{"turn": 1, "who": "user", "ts": 1,
                       "text": "needle", "reply": ""}],
        }

        def run(call) -> tuple[int, str, str]:
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(stderr):
                rc = call()
            return rc, stdout.getvalue(), stderr.getvalue()

        with mock.patch.object(
                indexd_runtime, "indexing_failure", return_value=failure), \
                mock.patch.object(
                    common, "in_agent_context", return_value=True), \
                mock.patch.object(common, "MESSAGES_PATH", Path(__file__)), \
                mock.patch.object(
                    indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(search, "run_query", return_value=result), \
                mock.patch.object(
                    search, "_semantic_runtime_installed", return_value=False), \
                mock.patch.object(session_context, "calling_family",
                                  return_value=None), \
                mock.patch.object(
                    around.explore, "resolve_session",
                    return_value=["freshness-session"]), \
                mock.patch.object(
                    around.explore, "get_window", return_value=window), \
                mock.patch.object(
                    around.explore, "_session_index",
                    return_value=["freshness-session"]):
            runs = (
                ("search", run(lambda: search.main([
                    "needle", "--lexical", "--classic", "--color", "never"]))),
                ("recall", run(lambda: recall.main([
                    "needle", "--lexical", "--budget", "64"]))),
                ("around", run(lambda: around.main([
                    "freshness-session", "1", "--color", "never"]))),
            )

        expected_rc = {"search": (2,), "recall": (0, 2), "around": (0, 1)}
        for verb, (rc, stdout, stderr) in runs:
            with self.subTest(verb=verb):
                self.assertIn(rc, expected_rc[verb])
                self.assertNotIn(failure.reason, stdout)
                self.assertEqual(stderr.count(failure.reason), 1)

    def test_grep_absence_requires_exact_current_proof(self) -> None:
        current = surface.FreshnessStory("current")
        self.assertEqual(
            surface.grep_absence_exit(exact=True, freshness=current), 1)
        # In-flight states are silent on the page ("system working") but
        # miss_verdict still hedges them as "index catching up; retry
        # shortly" - the zero is not proven, so the exit must not claim 1.
        for in_flight in (
                surface.FreshnessStory("current", absorbed_drift=True),
                surface.FreshnessStory(
                    "behind", changed_stores=1, young=True, converging=True)):
            with self.subTest(state=in_flight.state,
                              absorbed=in_flight.absorbed_drift):
                self.assertEqual(surface.freshness_story_line(in_flight), "")
                self.assertEqual(
                    surface.grep_absence_exit(
                        exact=True, freshness=in_flight), 2)
        for story in (
                surface.FreshnessStory("unverified"),
                surface.FreshnessStory("behind"),
                surface.FreshnessStory("failing")):
            with self.subTest(state=story.state):
                self.assertTrue(surface.freshness_story_line(story))
                self.assertEqual(
                    surface.grep_absence_exit(exact=True, freshness=story), 2)
        self.assertEqual(
            surface.grep_absence_exit(exact=False, freshness=current), 2)

    def test_around_existing_corpus_observes_blocked_owner_without_freshen(
            self) -> None:
        window = {
            "session": "freshness-session", "project": "project",
            "agent": "codex", "concept": "", "title": "", "center": 1,
            "first_turn": 1, "last_turn": 1, "events": [],
            "turns": [{"turn": 1, "who": "user", "ts": 1,
                       "text": "needle", "reply": ""}],
        }
        blocked = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.HOSTILE, None, 42, "birth")
        stdout, stderr = io.StringIO(), io.StringIO()
        indexd_runtime._clear_freshen_failure()
        corpus = tempfile.TemporaryDirectory()
        self.addCleanup(corpus.cleanup)
        # the corpus this test calls existing is its own, not the run's
        (Path(corpus.name) / "messages.jsonl").write_text("{}\n", encoding="utf-8")
        (Path(corpus.name) / ".ingest.sig").write_text("sig", encoding="utf-8")
        with mock.patch.object(common, "DATA_DIR", Path(corpus.name)), \
                mock.patch.object(
                    common, "INGEST_SIG_PATH", Path(corpus.name) / ".ingest.sig"), \
                mock.patch.object(
                    indexd_runtime, "_store_census", return_value=[]), \
                mock.patch.object(common, "MESSAGES_PATH", Path(__file__)), \
                mock.patch.object(common, "ingest_bin", return_value=Path(__file__)), \
                mock.patch.object(common, "in_agent_context", return_value=True), \
                mock.patch.object(
                    indexd_runtime, "indexd_failing", return_value=(0, "")), \
                mock.patch.object(
                    indexd_runtime, "_inspect_indexd_owner",
                    return_value=blocked), \
                mock.patch.object(
                    indexd_runtime, "indexd_resource_status",
                    return_value={
                        "state": indexd_runtime._IndexdOwnerState
                        .HOSTILE.value}), \
                mock.patch.object(indexd_runtime, "ensure_index") as ensure, \
                mock.patch.object(
                    around.explore, "resolve_session",
                    return_value=["freshness-session"]), \
                mock.patch.object(
                    around.explore, "get_window", return_value=window), \
                mock.patch.object(
                    around.explore, "_session_index",
                    return_value=["freshness-session"]), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            rc = around.main([
                "freshness-session", "1", "--color", "never"])
        self.assertEqual(rc, 0)
        ensure.assert_not_called()
        self.assertEqual(stderr.getvalue().count(
            "the freshness owner is blocked (hostile)"), 1)

    def test_around_stale_handle_says_rerun_not_doctor(self) -> None:
        window = {
            "session": "freshness-session", "project": "project",
            "agent": "codex", "concept": "", "title": "", "center": 1,
            "first_turn": 1, "last_turn": 1, "events": [],
            "turns": [{"turn": 1, "who": "user", "ts": 1,
                       "text": "needle", "reply": ""}],
        }
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(
                indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(
                    indexd_runtime, "agent_freshness_notice", return_value=""), \
                mock.patch.object(
                    around.explore, "resolve_session",
                    return_value=["freshness-session"]), \
                mock.patch.object(
                    around.explore, "get_window", return_value=window), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            rc = around.main(["@freshness:99", "--color", "never"])
        self.assertEqual(rc, 2)
        self.assertEqual(stdout.getvalue(), "")
        # law 7: the recovery is the action the reader wants anyway, not a
        # diagnostic detour
        self.assertIn("rerun the search", stderr.getvalue())
        self.assertNotIn("doctor", stderr.getvalue())

    def test_status_and_doctor_render_the_shared_governor_reason(self) -> None:
        deferred = surface.SemanticDeferral(
            "fixture", "runtime fixture", "shared pause reason")
        core = {
            "data_dir": "/tmp/agrep-test",
            "data_dir_source": "test",
            "warnings": [],
            "index_built": True,
            "sessions": 1,
            "messages": 3,
            "per_agent": [],
            "search_index_ready": True,
            "agents_taught": True,
            "detected_not_indexed": [],
            "embeddings_setting": {
                "state": "verified", "value": "auto", "source": "default"},
        }
        semantic = {
            "semantic_deps": True,
            "semantic_verified": True,
            "semantic_ready": False,
            "semantic_embedding_now": False,
            "semantic_state": "partial",
            "semantic_coverage": {
                "indexed": 1, "total": 3, "complete": False},
        }
        with mock.patch.object(cli, "_status_core", return_value=core), \
                mock.patch.object(cli, "_status_semantic", return_value=semantic), \
                mock.patch.object(
                    surface, "observed_semantic_deferral",
                    return_value=deferred):
            status = "\n".join(cli._status_lines("agrep"))
        self.assertIn(deferred.surface_reason, status)
        self.assertNotIn("background passes close the gap", status)

        smart = {
            "live": True,
            "available": True,
            "deps": {"numpy": True, "onnxruntime": True, "tokenizers": True},
            "model_cached": True,
            "embeddings": "partial",
            "embedding_coverage": {
                "indexed": 1, "total": 3, "complete": False},
            "embed_job": "idle",
            "embed_running": False,
            "resident_worker": {"running": False},
        }
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(doctor.common, "DATA_DIR", Path(td)), \
                mock.patch.object(doctor.common, "index_summary", return_value=None), \
                mock.patch.object(
                    doctor, "_corpus_db_readiness",
                    return_value={"state": "missing", "detail": "missing"}), \
                mock.patch.object(
                    doctor.indexd_runtime, "indexd_resource_status",
                    return_value={"running": False}), \
                mock.patch.object(
                    doctor.indexd_runtime, "indexd_failing", return_value=(0, "")), \
                mock.patch.object(doctor, "_semantic_probe", return_value=smart), \
                mock.patch.object(doctor, "_store_counts", return_value=[]), \
                mock.patch.object(doctor.common, "detected_stores", return_value=[]), \
                mock.patch.object(
                    doctor.common, "data_dir_usage",
                    return_value={"bytes": 0, "files": 0}), \
                mock.patch.object(doctor, "_footprint_breakdown", return_value=""), \
                mock.patch.object(
                    surface, "observed_semantic_deferral",
                    return_value=deferred), \
                contextlib.redirect_stdout(output):
            doctor.report()
        self.assertIn(deferred.surface_reason, output.getvalue())
        self.assertNotIn("background passes close the gap", output.getvalue())


class SemanticLaneStoryParityTests(unittest.TestCase):
    """Every degraded-lane string renders from surface.SEMANTIC_LANE_POLICY."""

    POLICY = surface.SemanticLanePolicy(
        keyword_only="fixture keyword-only story",
        warming="fixture warming story",
        retries="fixture retry story",
        disabled_tail="fixture disabled story",
        down_detail="fixture lane-down story",
    )
    QUERY = "why did the deployment keep retrying"

    class _Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    @staticmethod
    def _keyword_result(hits: list[dict]) -> dict:
        return {"hits": [dict(h) for h in hits], "total": len(hits),
                "chats": len({h["session"] for h in hits}), "tool_hits": 0,
                "engine": "corpusdb", "mode": "keyword", "totals_exact": True,
                "phrase_chats": len({h["session"] for h in hits})}

    @staticmethod
    def _windows(requests):
        return [{"session": session, "center": turn, "first_turn": turn,
                 "last_turn": turn, "agent": "codex", "project": "agrep",
                 "turns": [{"turn": turn, "ts": 1, "who": "user",
                            "text": "evidence", "reply": ""}],
                 "events": []} for session, turn, _context in requests]

    def test_query_surfaces_render_the_shared_lane_stories(self) -> None:
        hit = {"session": "past", "turn": 3, "ts": 5, "who": "user",
               "agent": "codex", "project": "agrep", "score": 9.0,
               "matched": "phrase", "snippet": "deployment kept retrying"}

        def run(call, stdout=None) -> tuple[int, str, str]:
            stdout = stdout if stdout is not None else io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(stderr):
                rc = call()
            return rc, stdout.getvalue(), stderr.getvalue()

        with mock.patch.object(surface, "SEMANTIC_LANE_POLICY", self.POLICY), \
                mock.patch.object(
                    indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(
                    indexd_runtime, "freshness_story",
                    return_value=surface.FreshnessStory("current")), \
                mock.patch.object(
                    indexd_runtime, "agent_freshness_notice", return_value=""), \
                mock.patch.object(
                    common, "in_agent_context", return_value=False), \
                mock.patch.object(
                    session_context, "calling_family", return_value=None), \
                mock.patch.object(
                    search, "_semantic_runtime_installed", return_value=True), \
                mock.patch.object(
                    search, "_stream_first_run", return_value=None):
            self.assertEqual(
                surface.semantic_unavailable_notice({"state": "unavailable"}),
                "semantic search unavailable: fixture lane-down story")
            self.assertEqual(
                surface.semantic_unavailable_notice(
                    {"state": "query-rejected", "reason": "identifier-query"},
                    query="q"),
                "semantic search unavailable for 'q': identifier-query")

            semantic_calls = []

            def unavailable_query(_query, *, mode="keyword", **_kwargs):
                semantic_calls.append(mode)
                return None

            with mock.patch.object(
                    search, "run_query", side_effect=unavailable_query):
                rc, _stdout, stderr = run(lambda: search.main(
                    [self.QUERY, "-s", "--color", "never"]))
            self.assertEqual(rc, 2)
            self.assertEqual(semantic_calls, ["semantic"])
            self.assertIn("semantic search unavailable", stderr)
            self.assertNotIn("warming", stderr)

            empty = self._keyword_result([])
            with mock.patch.object(
                    search, "run_query",
                    side_effect=lambda *a, **k: self._keyword_result([])), \
                    mock.patch.object(
                        search, "_start_semantic_query",
                        return_value=object()), \
                    mock.patch.object(
                        search, "_finish_semantic_query", return_value=None):
                rc, _stdout, stderr = run(lambda: search.main(
                    [self.QUERY, "--hybrid", "--classic", "--color", "never"]))
                self.assertIn(rc, (0, 1))
                self.assertIn("fixture keyword-only story", stderr)

            def tty_query(_query, *, mode="keyword", **_kwargs):
                return None if mode == "semantic" else dict(empty)

            with mock.patch.object(
                    search, "run_query", side_effect=tty_query), \
                    mock.patch("importlib.util.find_spec",
                               return_value=object()):
                rc, _stdout, stderr = run(
                    lambda: search.main(
                        [self.QUERY, "--classic", "--color", "never"]),
                    stdout=self._Tty())
            self.assertIn(rc, (0, 1))
            self.assertIn("would run here but is fixture warming story", stderr)

            with mock.patch.object(
                    search, "run_query",
                    side_effect=lambda *a, **k: self._keyword_result([hit])), \
                    mock.patch.object(
                        search, "_start_semantic_query",
                        return_value=object()), \
                    mock.patch.object(
                        search, "_finish_semantic_query", return_value=None), \
                    mock.patch.object(
                        explore, "get_windows", side_effect=self._windows):
                rc, stdout, stderr = run(lambda: recall.main(
                    [self.QUERY, "--json", "--hits", "2", "--budget", "4000"]))
            self.assertEqual(rc, 0)
            self.assertIn("fixture keyword-only story", stderr)
            self.assertEqual(
                json.loads(stdout)["semantic_status"],
                {"state": "unavailable", "complete": False,
                 "fallback_recommended": True})

            def probe_query(_query, *, mode="keyword", **_kwargs):
                return None if mode == "semantic" else self._keyword_result([])

            with mock.patch.object(
                    search, "run_query", side_effect=probe_query):
                rc, stdout, stderr = run(
                    lambda: recall.main([self.QUERY, "--probe"]))
            self.assertEqual(rc, 2)
            # D6: a probe miss is never silent - the owned miss line carries
            # the lane state itself, so no second stderr story may print
            self.assertIn("no confident past-context pointer", stdout)
            self.assertIn("fixture keyword-only story", stdout)
            self.assertEqual(stderr, "")

    def test_doctor_renders_the_shared_semantic_lane_stories(self) -> None:
        smart = {
            "live": True,
            "available": True,
            "deps": {"numpy": True, "onnxruntime": True, "tokenizers": True},
            "model_cached": True,
            "embeddings": "stale",
            "embedding_coverage": {"indexed": 2, "total": 3, "complete": False},
            "embed_job": "failed",
            "embed_fail_reason": "fixture failure",
            "embed_running": False,
            "resident_worker": {"running": False},
        }
        cases = (
            ("auto", dict(smart)),
            ("off", {**smart, "embeddings": "partial", "embed_job": "idle"}),
        )
        for embeddings_setting, probe in cases:
            output = io.StringIO()
            with self.subTest(embeddings=embeddings_setting), \
                    tempfile.TemporaryDirectory() as td, \
                    mock.patch.object(
                        surface, "SEMANTIC_LANE_POLICY",
                        SemanticLaneStoryParityTests.POLICY), \
                    mock.patch.object(doctor.common, "DATA_DIR", Path(td)), \
                    mock.patch.object(
                        doctor.settings, "setting_observation",
                        side_effect=lambda name, *a, **kw: (
                            {"state": "verified", "value": (
                                embeddings_setting if name == "embeddings"
                                else surface.setting_default(name))})), \
                    mock.patch.object(
                        doctor.common, "index_summary", return_value=None), \
                    mock.patch.object(
                        doctor, "_corpus_db_readiness",
                        return_value={"state": "missing", "detail": "missing"}), \
                    mock.patch.object(
                        doctor.indexd_runtime, "indexd_resource_status",
                        return_value={"running": False}), \
                    mock.patch.object(
                        doctor.indexd_runtime, "indexd_failing",
                        return_value=(0, "")), \
                    mock.patch.object(
                        doctor, "_semantic_probe", return_value=probe), \
                    mock.patch.object(doctor, "_store_counts", return_value=[]), \
                    mock.patch.object(
                        doctor.common, "detected_stores", return_value=[]), \
                    mock.patch.object(
                        doctor.common, "data_dir_usage",
                        return_value={"bytes": 0, "files": 0}), \
                    mock.patch.object(
                        doctor, "_footprint_breakdown", return_value=""), \
                    mock.patch.object(
                        surface, "observed_semantic_deferral",
                        return_value=None), \
                    contextlib.redirect_stdout(output):
                doctor.report()
            rendered = output.getvalue()
            if embeddings_setting == "off":
                self.assertIn("refresh fixture disabled story", rendered)
                self.assertNotIn("fixture retry story", rendered)
            else:
                self.assertIn(
                    "fixture retry story (log: semantic-embed.log)", rendered)


class AroundSpeakerPolicyTests(unittest.TestCase):
    SESSION = "surface-policy-session"
    WINDOW = {
        "session": SESSION,
        "project": "project",
        "agent": "codex",
        "concept": "topic",
        "title": "",
        "center": 2,
        "first_turn": 1,
        "last_turn": 3,
        "turns": [
            {"turn": 1, "who": "user", "ts": 1,
             "text": "USER_ONLY", "reply": "AGENT_ONE"},
            {"turn": 2, "who": "control", "ts": 2,
             "text": "CONTROL_ONLY", "reply": "AGENT_TWO"},
            {"turn": 3, "who": "recap", "ts": 3,
             "text": "RECAP_ONLY", "reply": "AGENT_THREE"},
        ],
        "events": [
            {"turn": 2, "kind": "tool", "name": "TOOL_ONLY",
             "input": "input", "output": "output", "ok": True,
             "input_chars": 5, "output_chars": 6, "ts": 2},
        ],
    }

    def _run(self, who: str, *, json_output: bool) -> tuple[int, str]:
        argv = [self.SESSION, "2", "--who", who]
        argv.append("--json" if json_output else "--color")
        if not json_output:
            argv.append("never")
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            signature = Path(td) / ".ingest.sig"
            signature.touch()
            with mock.patch.object(common, "MESSAGES_PATH", Path(__file__)), \
                    mock.patch.object(common, "DATA_DIR", Path(td)), \
                    mock.patch.object(common, "INGEST_SIG_PATH", signature), \
                    mock.patch.object(
                        around.explore, "resolve_session",
                        return_value=[self.SESSION]), \
                    mock.patch.object(
                        around.explore, "get_window", return_value=self.WINDOW), \
                    mock.patch.object(
                        around.explore, "_session_index",
                        return_value=[self.SESSION]), \
                    contextlib.redirect_stdout(output):
                return around.main(argv), output.getvalue()

    def test_text_filter_routes_messages_replies_and_tools_by_role(self) -> None:
        rc, control = self._run("control", json_output=False)
        self.assertEqual(rc, 0)
        self.assertIn("CONTROL_ONLY", control)
        self.assertNotIn("USER_ONLY", control)
        self.assertNotIn("AGENT_", control)
        self.assertNotIn("TOOL_ONLY", control)

        rc, agent = self._run("agent", json_output=False)
        self.assertEqual(rc, 0)
        self.assertIn("AGENT_ONE", agent)
        self.assertNotIn("USER_ONLY", agent)
        self.assertNotIn("TOOL_ONLY", agent)

        rc, tool = self._run("tool", json_output=False)
        self.assertEqual(rc, 0)
        self.assertIn("TOOL_ONLY", tool)
        self.assertNotIn("CONTROL_ONLY", tool)
        self.assertNotIn("AGENT_", tool)

    def test_json_filter_and_legacy_you_alias_follow_the_same_policy(self) -> None:
        rc, recap = self._run("recap", json_output=True)
        self.assertEqual(rc, 0)
        rows = [row for row in map(json.loads, recap.splitlines())
                if row.get("kind") == "msg"]
        self.assertEqual([(row["kind"], row.get("who"))
                          for row in rows], [("msg", "recap")])
        self.assertEqual(rows[0]["text"], "RECAP_ONLY")

        rc, user = self._run("you", json_output=True)
        self.assertEqual(rc, 0)
        rows = [row for row in map(json.loads, user.splitlines())
                if row.get("kind") == "msg"]
        self.assertEqual([(row["kind"], row.get("who"))
                          for row in rows], [("msg", "user")])
        self.assertEqual(rows[0]["text"], "USER_ONLY")

        rc, tool = self._run("tool", json_output=True)
        self.assertEqual(rc, 0)
        rows = [row for row in map(json.loads, tool.splitlines())
                if row.get("kind") != "agrep-meta"]
        self.assertEqual([row["kind"] for row in rows], ["tool"])


class CoverageNoticeTrivialGapTests(unittest.TestCase):
    """A live box's own churn tail is not a coverage story (RC2 m1).

    Hit pages pass suppress_trivial=True; the miss-proof path never does,
    so absence claims always state their scope."""

    def test_churn_scale_gap_is_silent_on_hit_pages(self) -> None:
        churn = {"indexed": 46358, "total": 46421, "complete": False}
        self.assertIsNone(surface.semantic_coverage_notice(
            churn, suppress_trivial=True))
        # the miss-proof caller still gets the full disclosure
        self.assertIn("semantic coverage is partial",
                      surface.semantic_coverage_notice(churn))

    def test_a_real_gap_still_notices_everywhere(self) -> None:
        rebuilding = {"indexed": 4, "total": 7200, "complete": False}
        self.assertIn("converging in the background",
                      surface.semantic_coverage_notice(
                          rebuilding, suppress_trivial=True))
        # small corpus, small absolute gap, but 30% missing is not churn
        small = {"indexed": 20, "total": 28, "complete": False}
        self.assertIsNotNone(surface.semantic_coverage_notice(
            small, suppress_trivial=True))

    def test_accelerator_gaps_and_unknown_counts_never_suppress(self) -> None:
        churn = {"indexed": 46358, "total": 46421, "complete": False}
        accel = {"indexed": 12, "total": 20, "complete": False}
        self.assertIsNotNone(surface.semantic_coverage_notice(
            churn, accel, suppress_trivial=True))
        unknown = {"indexed": "?", "total": "?", "complete": False}
        self.assertIsNotNone(surface.semantic_coverage_notice(
            unknown, suppress_trivial=True))


class MissVerdictTests(unittest.TestCase):
    """The zero-trust contract: one verdict, one vocabulary, never a stack."""

    CURRENT = surface.FreshnessStory("current")
    COMPLETE = {"indexed": 20, "total": 20, "complete": True}

    def test_confident_zero_states_exactly_what_it_proved(self) -> None:
        verdict = surface.miss_verdict(
            self.CURRENT, meaning_served=True, meaning_coverage=self.COMPLETE,
            sessions=4912)
        self.assertTrue(verdict.confident)
        line, consumed = surface.miss_zero_render(4912, verdict)
        self.assertEqual(
            line, "no match across 4,912 sessions - keyword + meaning, "
            "index current")
        self.assertFalse(consumed)
        one, _ = surface.miss_zero_render(1, verdict)
        self.assertTrue(one.startswith("no match across 1 session -"))

    def test_confidence_requires_a_served_lane_with_proven_coverage(self) -> None:
        down = surface.miss_verdict(self.CURRENT, meaning_served=False)
        self.assertFalse(down.confident)
        self.assertEqual(down.tail, surface.SEMANTIC_LANE_POLICY.keyword_only)
        partial = surface.miss_verdict(
            self.CURRENT, meaning_served=True,
            meaning_coverage={"indexed": 4, "total": 20, "complete": False})
        self.assertFalse(partial.confident)
        self.assertEqual(partial.tail, surface.semantic_coverage_notice(
            {"indexed": 4, "total": 20, "complete": False}))
        unproven = surface.miss_verdict(
            self.CURRENT, meaning_served=True, meaning_coverage=None)
        self.assertFalse(unproven.confident)
        self.assertIn("coverage unavailable", unproven.tail)
        short_prefix = surface.miss_verdict(
            self.CURRENT, meaning_served=True, meaning_coverage=self.COMPLETE,
            meaning_accelerator={"indexed": 12, "total": 20,
                                 "complete": False})
        self.assertFalse(short_prefix.confident)
        self.assertIn("semantic coverage is partial", short_prefix.tail)

    def test_freshness_hedge_outranks_the_lane_and_owns_the_story(self) -> None:
        behind = surface.FreshnessStory(
            "behind", behind_s=300.0, changed_stores=1)
        verdict = surface.miss_verdict(
            behind, meaning_served=True, meaning_coverage=self.COMPLETE)
        self.assertFalse(verdict.confident)
        self.assertTrue(verdict.owns_freshness)
        self.assertEqual(verdict.tail, surface.freshness_story_line(behind))
        line, consumed = surface.miss_zero_render(3, verdict)
        self.assertTrue(consumed)
        self.assertIn("no match across 3 sessions - ", line)
        self.assertIn(surface.freshness_story_line(behind), line)

    def test_absorbed_drift_hedges_the_zero_despite_display_silence(self) -> None:
        # young converging drift renders no freshness line beside served rows
        # (law 3), but a zero needs positive currency: silence licenses
        # nothing, so the verdict hedges instead of claiming "index current"
        absorbed = surface.FreshnessStory(
            "behind", changed_stores=1, converging=True, young=True)
        self.assertEqual(surface.freshness_story_line(absorbed), "")
        verdict = surface.miss_verdict(
            absorbed, meaning_served=True, meaning_coverage=self.COMPLETE,
            sessions=3)
        self.assertFalse(verdict.confident)
        self.assertNotIn("index current", verdict.tail)
        self.assertIn("catching up", verdict.tail)

    def test_current_state_with_absorbed_drift_cannot_prove_currency(
            self) -> None:
        # the census saw changes a daemon should absorb: the display verdict
        # stays green, but the zero's proof claim forfeits on the observation
        observed = surface.FreshnessStory("current", absorbed_drift=True)
        self.assertEqual(surface.freshness_story_line(observed), "")
        verdict = surface.miss_verdict(
            observed, meaning_served=True, meaning_coverage=self.COMPLETE,
            sessions=3)
        self.assertFalse(verdict.confident)
        self.assertNotIn("index current", verdict.tail)

    def test_unknown_corpus_scope_refuses_the_confident_form(self) -> None:
        # "across sessions" with no number is not a provable scope: the
        # verdict names the gap instead of stapling a proof to it
        verdict = surface.miss_verdict(
            self.CURRENT, meaning_served=True, meaning_coverage=self.COMPLETE,
            sessions=None)
        self.assertFalse(verdict.confident)
        self.assertNotIn("index current", verdict.tail)
        self.assertIn("corpus scope unavailable", verdict.tail)

    def test_an_empty_index_is_named_not_counted_to_zero(self) -> None:
        verdict = surface.miss_verdict(
            self.CURRENT, meaning_served=True, meaning_coverage=self.COMPLETE,
            sessions=0)
        line, consumed = surface.miss_zero_render(0, verdict)
        self.assertEqual(line, surface.MISS_EMPTY_INDEX_LINE)
        self.assertNotIn("0 session", line)
        self.assertFalse(consumed)

    def test_an_overlong_lever_never_yields_a_fabricated_confident_line(
            self) -> None:
        hedged = surface.MissVerdict(False, "x" * 200, owns_freshness=False)
        self.assertEqual(surface.miss_zero_render(3, hedged), (None, False))
        freshness_owned = surface.MissVerdict(False, "x" * 200,
                                              owns_freshness=True)
        line, consumed = surface.miss_zero_render(3, freshness_owned)
        self.assertEqual(line, "no match across 3 sessions")
        # the story line beside it owns the hedge, so it must still render
        self.assertFalse(consumed)


if __name__ == "__main__":
    unittest.main()
