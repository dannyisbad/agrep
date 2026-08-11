"""A confident zero must be distinguishable from a coverage gap.

`-c deadlock --agent gemini` answered 0/exit 1 - byte-identical to "never
discussed" - while `--agent gemini` selected a dimension holding no indexed
row at all, a fact `doctor` was one command away from printing. `--model
claude-opus5` (one missing hyphen) was the same undetectable zero, and
`--since 1d --until 7d` asked for the interval [now-1d, now-7d), empty by
construction for every corpus, and reported it as a miss.

Each case is driven off the artifact the emitter uses - surface_policy's
COVERAGE_DIMENSIONS / empty_dimension_disclosure / window_bounds_error - so a
dimension added without evidence, or a line that drifts from the JSON field
beside it, fails here. The negative controls matter as much: a genuine zero on
a populated dimension must still render as a plain honest miss.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "py"))

from _test_support import isolate_data_dir, publish_derived_generation  # noqa: E402

isolate_data_dir()

import common  # noqa: E402
import corpusdb  # noqa: E402
import explore  # noqa: E402
import recall  # noqa: E402
import search  # noqa: E402
import surface_policy as surface  # noqa: E402

INDEXED_AGENTS = ("claude", "codex")
INDEXED_PROJECTS = ("/home/u/webapp", "/home/u/apartment")
INDEXED_MODELS = ("claude-opus-5", "claude-opus-4-8", "gpt-5.6-sol")
INDEXED_WHO = ("user", "agent", "tool")
# one value per dimension that the fixture index provably does not hold
ABSENT = {"agent": "gemini", "project": "no-such-project",
          "model": "claude-opus5", "who": "harness"}
PRESENT = {"agent": "claude", "project": "webapp",
           "model": "claude-opus-5", "who": "user"}


def _namespace(**kw) -> argparse.Namespace:
    base = {"agent": None, "project": None, "model": None, "who": None,
            "model_soft": False}
    return argparse.Namespace(**{**base, **kw})


@contextlib.contextmanager
def _indexed(agents=INDEXED_AGENTS, projects=INDEXED_PROJECTS,
             models=INDEXED_MODELS, whos=INDEXED_WHO):
    """The index domains, stubbed at the three sources the evidence reads."""
    by_dimension = {"agent": agents, "project": projects,
                    "model": models, "who": whos}

    def values(dimension: str):
        found = by_dimension.get(dimension)
        return None if found is None else tuple(found)

    with mock.patch.object(search, "_indexed_dimension_values", values), \
            mock.patch.object(
                search.indexd_runtime, "freshness_story",
                return_value=surface.FreshnessStory("current")):
        yield


class DimensionEvidenceTests(unittest.TestCase):
    """Where each domain comes from, and when the tool declines to claim one."""

    def test_agent_domain_rides_the_proof_bound_summary(self) -> None:
        with mock.patch.object(common, "index_summary",
                               return_value={"agents": ["claude", "codex"]}):
            self.assertEqual(search._indexed_dimension_values("agent"),
                             ("claude", "codex"))

    def test_an_unprovable_summary_claims_no_agent_domain(self) -> None:
        # None is "I cannot enumerate this", never "it is empty": the second
        # would turn every unreadable index into a fabricated coverage gap
        with mock.patch.object(common, "index_summary", return_value=None):
            self.assertIsNone(search._indexed_dimension_values("agent"))

    def test_project_domain_rides_the_session_aggregate(self) -> None:
        rows = {"s1": {"project": "/home/u/webapp"},
                "s2": {"project": ""}, "s3": {"project": "/home/u/webapp"}}
        with mock.patch.object(explore, "_session_index", return_value=rows), \
                mock.patch.object(search.indexd_runtime, "freshness_story",
                                  return_value=surface.FreshnessStory("current")), \
                mock.patch.object(common, "index_summary", return_value={"agents": []}):
            self.assertEqual(search._indexed_dimension_values("project"),
                             ("/home/u/webapp",))

    def test_project_domain_withdraws_when_the_served_generation_is_unproven(self) -> None:
        with mock.patch.object(search.indexd_runtime, "freshness_story",
                               return_value=surface.FreshnessStory("unverified")), \
                mock.patch.object(explore, "_session_index") as sessions:
            self.assertIsNone(search._indexed_dimension_values("project"))
        sessions.assert_not_called()

    def test_census_reads_the_column_and_drops_blank_rows(self) -> None:
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE msgs(model TEXT, who TEXT)")
        db.executemany("INSERT INTO msgs VALUES(?, ?)", [
            ("claude-opus-5", "user"), ("claude-opus-5", "agent"),
            ("", "tool"), (None, "tool")])
        with mock.patch.object(search._load_corpusdb(), "connect",
                               return_value=db):
            self.assertEqual(search._census_column("model"),
                             ("claude-opus-5",))
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE msgs(model TEXT, who TEXT)")
        db.executemany("INSERT INTO msgs VALUES(?, ?)", [
            ("claude-opus-5", "user"), ("claude-opus-5", "agent"),
            ("", "tool"), (None, "tool")])
        with mock.patch.object(search._load_corpusdb(), "connect",
                               return_value=db):
            self.assertEqual(set(search._census_column("who")),
                             {"user", "agent", "tool"})

    def test_an_unreadable_index_claims_no_census_domain(self) -> None:
        with mock.patch.object(search._load_corpusdb(), "connect",
                               return_value=None):
            self.assertIsNone(search._census_column("model"))

    def test_an_over_budget_census_withdraws_the_claim(self) -> None:
        # a truncated census is a floor, and a floor cannot prove emptiness
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE msgs(model TEXT)")
        db.executemany("INSERT INTO msgs VALUES(?)",
                       [(f"m{i}",) for i in range(500)])
        with mock.patch.object(search._load_corpusdb(), "connect",
                               return_value=db), \
                mock.patch.object(search, "_DIMENSION_CENSUS_BUDGET_S", -1.0), \
                mock.patch.object(search, "_DIMENSION_CENSUS_OPS", 1):
            self.assertIsNone(search._census_column("model"))
        with self.assertRaises(sqlite3.ProgrammingError):
            db.execute("SELECT DISTINCT model FROM msgs").fetchall()


class EmptyDimensionTests(unittest.TestCase):
    """Every enumerable dimension explains its own zero, and only its own."""

    def test_each_dimension_discloses_a_value_the_index_never_held(self) -> None:
        for spec in surface.COVERAGE_DIMENSIONS:
            with self.subTest(flag=spec.flag), _indexed():
                coverage = search.filter_coverage(
                    _namespace(**{spec.dimension: ABSENT[spec.dimension]}))
                self.assertTrue(coverage["checked"])
                self.assertEqual(
                    [item["flag"] for item in coverage["empty_dimensions"]],
                    [spec.flag])
                record = coverage["empty_dimensions"][0]
                self.assertEqual(record["value"], ABSENT[spec.dimension])
                self.assertEqual(record["dimension"], spec.dimension)

    def test_a_populated_dimension_is_never_disclosed(self) -> None:
        # the negative control: a real miss under a real agent stays a plain
        # miss, or the disclosure becomes noise every zero carries
        for spec in surface.COVERAGE_DIMENSIONS:
            with self.subTest(flag=spec.flag), _indexed():
                coverage = search.filter_coverage(
                    _namespace(**{spec.dimension: PRESENT[spec.dimension]}))
                self.assertEqual(coverage["empty_dimensions"], [])
                self.assertTrue(coverage["checked"])

    def test_an_unfiltered_zero_discloses_nothing(self) -> None:
        with _indexed():
            self.assertEqual(
                search.filter_coverage(_namespace())["empty_dimensions"], [])

    def test_substring_dimensions_use_the_engines_own_predicate(self) -> None:
        # corpusdb._filter_sql matches --agent/--project by ci substring; a
        # disclosure testing equality would call a matching filter empty
        with _indexed():
            self.assertEqual(
                search.filter_coverage(
                    _namespace(project="webapp"))["empty_dimensions"], [])
            self.assertEqual(
                search.filter_coverage(
                    _namespace(agent="CLAUDE"))["empty_dimensions"], [])

    def test_model_is_exact_unless_soft_asks_for_the_substring(self) -> None:
        with _indexed():
            self.assertEqual(
                [item["flag"] for item in search.filter_coverage(
                    _namespace(model="opus-5"))["empty_dimensions"]],
                ["--model"])
            self.assertEqual(
                search.filter_coverage(_namespace(
                    model="opus-5", model_soft=True))["empty_dimensions"], [])

    def test_who_discloses_only_when_every_named_speaker_is_absent(self) -> None:
        with _indexed():
            self.assertEqual(
                [item["flag"] for item in search.filter_coverage(
                    _namespace(who="harness,recap"))["empty_dimensions"]],
                ["--who"])
            # one indexed speaker in the list makes the zero a real miss
            self.assertEqual(
                search.filter_coverage(
                    _namespace(who="harness,user"))["empty_dimensions"], [])

    def test_an_unprovable_domain_claims_nothing_and_says_why(self) -> None:
        with _indexed(models=None):
            coverage = search.filter_coverage(_namespace(model="whatever"))
        self.assertEqual(coverage["empty_dimensions"], [])
        self.assertFalse(coverage["checked"])
        self.assertIn("--model", coverage["reason"])

    def test_a_provable_gap_survives_an_unprovable_neighbour(self) -> None:
        with _indexed(models=None):
            coverage = search.filter_coverage(
                _namespace(agent="gemini", model="whatever"))
        self.assertEqual(
            [item["flag"] for item in coverage["empty_dimensions"]], ["--agent"])
        self.assertFalse(coverage["checked"])

    def test_the_reason_speaks_only_for_the_flags_it_could_not_check(self) -> None:
        # E5: "no coverage gap is claimed" beside a non-empty empty_dimensions
        # is a false sentence an LLM believes over the field next to it
        with _indexed(models=None):
            coverage = search.filter_coverage(
                _namespace(agent="gemini", model="whatever"))
        reason = coverage["reason"]
        self.assertIn("--model", reason)
        self.assertNotIn("--agent", reason)
        self.assertIn("for it", reason)
        self.assertIn("proven independently", reason)
        with _indexed(models=None):
            alone = search.filter_coverage(_namespace(model="whatever"))
        self.assertNotIn("proven independently", alone["reason"])


class DisclosureWordingTests(unittest.TestCase):
    """One artifact behind the prose line and the machine record."""

    def test_the_line_names_the_flag_the_value_and_what_is_indexed(self) -> None:
        spec = surface.COVERAGE_DIMENSIONS_BY_FLAG["--agent"]
        record = surface.empty_dimension_disclosure(
            spec, "gemini", INDEXED_AGENTS)
        line = surface.empty_dimension_line(record)
        self.assertIn("--agent", line)
        self.assertIn("gemini", line)
        for agent in INDEXED_AGENTS:
            self.assertIn(agent, line)
        self.assertTrue(record["known_complete"])

    def test_a_wide_domain_reports_its_size_and_the_nearest_values(self) -> None:
        models = tuple(f"model-{i}" for i in range(40)) + ("claude-opus-5",)
        spec = surface.COVERAGE_DIMENSIONS_BY_FLAG["--model"]
        record = surface.empty_dimension_disclosure(
            spec, "claude-opus5", models)
        self.assertEqual(record["indexed_values"], len(models))
        self.assertFalse(record["known_complete"])
        # the typo's intended value leads the near-miss list
        self.assertEqual(record["known"][0], "claude-opus-5")
        line = surface.empty_dimension_line(record)
        self.assertIn(str(len(models)), line)
        self.assertIn("claude-opus-5", line)

    def test_an_empty_index_says_so_rather_than_listing_nothing(self) -> None:
        spec = surface.COVERAGE_DIMENSIONS_BY_FLAG["--agent"]
        line = surface.empty_dimension_line(
            surface.empty_dimension_disclosure(spec, "gemini", ()))
        self.assertIn("--agent", line)
        self.assertIn("no agents", line)

    def test_human_line_escapes_untrusted_values_without_changing_json(self) -> None:
        requested = "wanted\n\x1b[31mred\u202e"
        indexed = "known\r\x1b]52;c;YQ==\x07\u202d"
        spec = surface.COVERAGE_DIMENSIONS_BY_FLAG["--model"]
        record = surface.empty_dimension_disclosure(
            spec, requested, (indexed,))
        before = json.loads(json.dumps(record))
        line = surface.empty_dimension_line(record)
        for unsafe in ("\n", "\r", "\x1b", "\x07", "\u202d", "\u202e"):
            self.assertNotIn(unsafe, line)
        for escaped in ("\\u000a", "\\u000d", "\\u001b", "\\u0007",
                        "\\u202d", "\\u202e"):
            self.assertIn(escaped, line)
        self.assertEqual(record, before)

    def test_every_dimension_has_a_line_and_a_flag_the_parsers_take(self) -> None:
        for spec in surface.COVERAGE_DIMENSIONS:
            with self.subTest(flag=spec.flag):
                self.assertIn(spec.flag, surface.FILTERS_BY_FLAG)
                self.assertEqual(
                    surface.FILTERS_BY_FLAG[spec.flag].dest, spec.dimension)
                line = surface.empty_dimension_line(
                    surface.empty_dimension_disclosure(spec, "x", ("a", "b")))
                self.assertIn(spec.flag, line)
                self.assertIn(spec.label, line)

    def test_chat_is_absent_because_it_already_refuses(self) -> None:
        # --chat resolves against the indexed sessions before any search runs,
        # so an unindexed chat is an error, which is more than a disclosure
        self.assertNotIn("--chat", surface.COVERAGE_DIMENSIONS_BY_FLAG)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(search._resolve_chat("00000000"))
        self.assertIn("no session matches", err.getvalue())

    def test_the_machine_record_is_emitted_even_when_nothing_is_empty(self) -> None:
        # an absent field would read as "no dimension is empty", the exact
        # inference an unexplained zero already invites
        disclosure = surface.filter_coverage_disclosure([], checked=True)
        self.assertEqual(disclosure, {"empty_dimensions": [], "checked": True})
        self.assertIn("reason", surface.filter_coverage_disclosure(
            [], checked=False, reason="unreadable"))


class WindowTests(unittest.TestCase):
    """[since, until) with the bounds the wrong way round holds no instant."""

    def test_an_inverted_window_names_both_bounds_and_the_swap(self) -> None:
        message = surface.window_bounds_error("1d", 500, "7d", 100)
        self.assertIsNotNone(message)
        self.assertIn("--since 1d", message)
        self.assertIn("--until 7d", message)
        self.assertIn("swap", message)

    def test_equal_bounds_are_the_same_empty_interval(self) -> None:
        self.assertIsNotNone(surface.window_bounds_error("x", 100, "y", 100))

    def test_the_right_way_round_is_not_an_error(self) -> None:
        self.assertIsNone(surface.window_bounds_error("7d", 100, "1d", 500))

    def test_one_bound_alone_is_never_inverted(self) -> None:
        self.assertIsNone(surface.window_bounds_error("7d", 100, None, None))
        self.assertIsNone(surface.window_bounds_error(None, None, "1d", 500))

    def test_search_and_recall_refuse_the_impossible_window(self) -> None:
        for name, entry, base in (("search", search.main, ["deadlock"]),
                                  ("recall", recall.main, ["deadlock"])):
            for window in (["--since", "1d", "--until", "7d"],
                           ["--since", "2026-07-01", "--until", "2026-06-01"]):
                with self.subTest(entry=name, window=window):
                    rc, err = _run(entry, [*base, *window, "--no-auto"])
                    self.assertEqual(rc, 2, err)
                    self.assertIn("empty time window", err)
                    self.assertIn(window[1], err)
                    self.assertIn(window[3], err)

    def test_the_same_bounds_the_right_way_round_reach_the_engine(self) -> None:
        for entry, base in ((search.main, ["deadlock"]),
                            (recall.main, ["deadlock"])):
            _rc, err = _run(entry, [*base, "--since", "7d", "--until", "1d",
                                    "--no-auto"])
            self.assertNotIn("empty time window", err)

    def test_a_malformed_bound_still_reports_the_bad_value(self) -> None:
        # the inversion check must not shadow the parser's own refusal
        rc, err = _run(search.main, ["deadlock", "--since", "garbage",
                                     "--until", "1d", "--no-auto"])
        self.assertEqual(rc, 2, err)
        self.assertIn("garbage", err)


def _run(entry, argv: list[str]) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = entry(argv)
        except SystemExit as exc:
            rc = 2 if exc.code is None else int(exc.code)
    return rc, err.getvalue()


MESSAGES = (
    {"id": "claude:s1:1", "agent": "claude", "project": "/home/u/webapp",
     "session": "0199aaaa-1111-7000-8000-000000000001", "turn": 1, "ts": 1000,
     "who": "user", "model": "claude-opus-5", "text": "deadlock in the writer"},
)


class SurfaceTests(unittest.TestCase):
    """The same verdict on the page, in the count, and in --json."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._prior = {}
        search_db_names = (
            corpusdb.DB_PATH.name,
            f"{corpusdb.DB_PATH.name}-wal",
            f"{corpusdb.DB_PATH.name}-shm",
        )
        generated = (
            "messages.jsonl", "replies.jsonl", "sessions.jsonl",
            common.SESSION_FAMILY_META_FILE, "boundary_stats.json",
            ".boundary_stats.bin", "event_stats.json",
            ".derived_generation.json", ".ingest.sig", "settings.json",
            *search_db_names,
        )
        for name in generated:
            path = common.DATA_DIR / name
            cls._prior[name] = path.read_bytes() if path.exists() else None
        for name in search_db_names:
            (common.DATA_DIR / name).unlink(missing_ok=True)
        publish_derived_generation(
            common.DATA_DIR, list(MESSAGES), common, corpusdb,
            signature="zero-coverage")
        explore._freshen()

    @classmethod
    def tearDownClass(cls) -> None:
        for name, body in cls._prior.items():
            path = common.DATA_DIR / name
            if body is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(body)
        explore._freshen()

    def _search(self, argv):
        with _indexed(), mock.patch.object(
                search.indexd_runtime, "ensure_index", return_value=True):
            return _run(search.main, [*argv, "--no-auto"])

    def test_the_porcelain_zero_carries_the_line_once(self) -> None:
        _rc, err = self._search(["deadlock", "--agent", "gemini"])
        self.assertEqual(err.count("isn't in the index"), 1)
        self.assertIn("--agent gemini", err)

    def test_the_count_surface_keeps_stdout_a_number(self) -> None:
        with _indexed(), mock.patch.object(
                search.indexd_runtime, "ensure_index", return_value=True):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                rc = search.main(["-c", "deadlock", "--agent", "gemini",
                                  "--no-auto"])
        self.assertEqual(rc, 1)
        self.assertEqual(out.getvalue().strip(), "0")
        self.assertIn("isn't in the index", err.getvalue())

    def test_the_json_record_carries_the_same_disclosure(self) -> None:
        with _indexed(), mock.patch.object(
                search.indexd_runtime, "ensure_index", return_value=True):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(io.StringIO()):
                search.main(["deadlock", "--agent", "gemini", "--json",
                             "--no-auto"])
        record = json.loads(out.getvalue().splitlines()[-1])
        coverage = record["filter_coverage"]
        self.assertTrue(coverage["checked"])
        self.assertEqual(coverage["empty_dimensions"][0]["flag"], "--agent")
        # the same artifact renders the human line, so they cannot disagree
        self.assertIn("gemini", surface.empty_dimension_line(
            coverage["empty_dimensions"][0]))

    def test_no_matcher_answers_a_zero_with_silence(self) -> None:
        # E3: piped (no tty), -w/-E wrote nothing anywhere - an agent could
        # not tell "no match" from a broken invocation
        for flags, label in (([], "keyword"), (["-w"], "word"),
                             (["-E"], "regex")):
            with self.subTest(label=label):
                rc, err = self._search([*flags, "zzzznotathing"])
                self.assertEqual(rc, 1)
                owned = [line for line in err.splitlines()
                         if line.startswith(f"{label}: ")]
                self.assertEqual(len(owned), 1, err)
                self.assertRegex(owned[0], r"0 matching rows|no match")
                if label != "keyword":
                    self.assertIn("`-s` runs semantic search", owned[0])

    def test_a_genuine_zero_on_a_populated_dimension_stays_a_plain_miss(self) -> None:
        _rc, err = self._search(["zzzznotathing", "--agent", "claude"])
        self.assertNotIn("isn't in the index", err)
        with _indexed(), mock.patch.object(
                search.indexd_runtime, "ensure_index", return_value=True):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(io.StringIO()):
                search.main(["zzzznotathing", "--agent", "claude", "--json",
                             "--no-auto"])
        coverage = json.loads(out.getvalue().splitlines()[-1])["filter_coverage"]
        self.assertEqual(coverage["empty_dimensions"], [])
        self.assertTrue(coverage["checked"])

    def test_a_hit_never_pays_for_a_census(self) -> None:
        # one hit already proves every filtered dimension populated - and the
        # hit must be this module's own fixture row, not whatever an earlier
        # discovery module left in the shared sandbox
        with _indexed(), \
                mock.patch.object(search, "filter_coverage") as census, \
                mock.patch.object(search.indexd_runtime, "ensure_index",
                                  return_value=True):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(io.StringIO()):
                search.main(["deadlock", "--agent", "claude", "--json",
                             "--no-auto"])
        census.assert_not_called()
        record = json.loads(out.getvalue().splitlines()[0])
        self.assertEqual(record["filter_coverage"],
                         {"empty_dimensions": [], "checked": True})

    def test_a_mistyped_model_is_disclosed_and_never_rejected(self) -> None:
        # --model's domain is data-derived and open: a model this index has
        # not seen is still a legal query, so the zero is explained, not
        # refused. Exit 1 (a miss), never exit 2 (a usage error).
        rc, err = self._search(["deadlock", "--model", "claude-opus5"])
        self.assertEqual(rc, 1, err)
        self.assertIn("--model claude-opus5", err)
        self.assertIn("claude-opus-5", err)

    def test_chats_discloses_the_same_gap(self) -> None:
        with _indexed(), mock.patch.object(
                search.indexd_runtime, "ensure_index", return_value=True):
            rc, err = _run(search.chats_main, ["--agent", "gemini"])
        self.assertEqual(rc, 1)
        self.assertIn("isn't in the index", err)

    def test_recall_discloses_the_same_gap(self) -> None:
        with _indexed(), mock.patch.object(
                recall.indexd_runtime, "ensure_index", return_value=True):
            rc, err = _run(recall.main, ["deadlock", "--agent", "gemini",
                                         "--no-auto"])
        self.assertEqual(rc, 1)
        self.assertIn("--agent gemini", err)
        self.assertIn("isn't in the index", err)

    def test_probe_miss_respects_filters_with_the_same_record(self) -> None:
        # law 4: a probe under a filter the index holds nothing for searched
        # zero sessions, and its miss line must not claim the whole corpus
        with _indexed(), mock.patch.object(
                recall.indexd_runtime, "ensure_index", return_value=True):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = recall.main(["deadlock", "--agent", "gemini", "--probe",
                                  "--lexical", "--no-auto"])
        self.assertEqual(rc, 1)
        line = out.getvalue()
        self.assertIn("searched 0 past session", line)
        self.assertIn("--agent gemini", line)
        self.assertIn("isn't in the index", line)

    def test_an_unfiltered_probe_miss_keeps_the_corpus_scope(self) -> None:
        with _indexed(), mock.patch.object(
                recall.indexd_runtime, "ensure_index", return_value=True):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = recall.main(["zzzznotathing", "--probe",
                                  "--lexical", "--no-auto"])
        self.assertEqual(rc, 1)
        line = out.getvalue()
        self.assertNotIn("searched 0 past session", line)
        self.assertNotIn("isn't in the index", line)


if __name__ == "__main__":
    unittest.main()
