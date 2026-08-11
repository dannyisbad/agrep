"""Filterable provenance (goal-10 item 3, 4/4 panel consensus).

--who takes a comma list, --no-who excludes, and both parse through the one
owned vocabulary in surface_policy. --no-meta drops ~meta rows entirely with
a disclosed count. Machine surfaces keep grep parity unless the flag is
explicitly passed. A role the vocabulary does not know survives an exclusion
it is not named in.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "py"))

from _test_support import isolate_data_dir  # noqa: E402

isolate_data_dir()
import corpusdb  # noqa: E402
import recall  # noqa: E402
import search  # noqa: E402
import surface_policy as surface  # noqa: E402


class SpeakerFilterPolicy(unittest.TestCase):
    CHOICES = surface.SEARCH_SPEAKER_CHOICES

    def test_single_include_collapses_to_the_engine_fast_path(self) -> None:
        self.assertEqual(surface.speaker_filter("user", None, self.CHOICES),
                         "user")

    def test_comma_list_parses_and_dedupes(self) -> None:
        parsed = surface.speaker_filter("user, agent,user", None, self.CHOICES)
        self.assertEqual(parsed, surface.SpeakerFilter(("user", "agent"), ()))

    def test_exclusion_is_a_predicate_not_a_complement(self) -> None:
        parsed = surface.speaker_filter(None, "subagent", self.CHOICES)
        self.assertTrue(parsed.admits("user"))
        self.assertFalse(parsed.admits("subagent"))
        # an unrecognized role survives an exclusion it is not named in
        self.assertTrue(parsed.admits("unknown"))

    def test_the_legend_token_filters_on_every_surface(self) -> None:
        # F1: speaker_legend() teaches `you`; around honored it and search and
        # recall exited 2 with a usage wall
        self.assertIn("you", surface.speaker_legend())
        self.assertEqual(surface.speaker_filter("you", None, self.CHOICES),
                         "user")
        self.assertEqual(
            surface.parse_speaker_list("you,agent", self.CHOICES),
            ("user", "agent"))
        # the alias folds, it does not duplicate
        self.assertEqual(
            surface.parse_speaker_list("you,user", self.CHOICES), ("user",))
        excluded = surface.speaker_filter(None, "you,tool", self.CHOICES)
        self.assertFalse(excluded.admits("user"))
        self.assertTrue(excluded.admits("agent"))

    def test_include_and_exclude_are_mutually_exclusive(self) -> None:
        with self.assertRaises(ValueError):
            surface.speaker_filter("user", "tool", self.CHOICES)

    def test_unknown_names_are_refused_with_the_vocabulary(self) -> None:
        with self.assertRaises(ValueError) as raised:
            surface.speaker_filter("operator", None, self.CHOICES)
        self.assertIn("operator", str(raised.exception))
        self.assertIn("user", str(raised.exception))

    def test_admits_covers_every_engine_shape(self) -> None:
        self.assertTrue(surface.speaker_filter_admits(None, "tool"))
        self.assertTrue(surface.speaker_filter_admits("tool", "tool"))
        self.assertFalse(surface.speaker_filter_admits("user", "tool"))
        general = surface.SpeakerFilter(("user", "agent"), ())
        self.assertTrue(surface.speaker_filter_admits(general, "agent"))
        self.assertFalse(surface.speaker_filter_admits(general, "tool"))


class EngineSpeakerFilter(unittest.TestCase):
    """corpusdb SQL and the jsonl scans honor include lists and exclusions."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = sqlite3.connect(":memory:")
        cls.db.executescript(corpusdb._SCHEMA_SQL)
        cls.db.executemany(corpusdb._INS, [
            (f"sess-{who}", 1, 1000, "codex", "proj", "", "", "", who,
             f"needle spoken by {who}")
            for who in ("user", "agent", "subagent", "tool", "harness")])
        cls.db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
        cls.db.execute(
            "INSERT INTO msgs_prose_fts(msgs_prose_fts) VALUES('rebuild')")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.close()

    def _whos(self, flt) -> set[str]:
        result = corpusdb.keyword(self.db, "needle", 40, flt)
        return {hit["who"] for hit in result["hits"]}

    def test_include_list_limits_to_the_named_speakers(self) -> None:
        flt = {"who": surface.SpeakerFilter(("user", "agent"), ())}
        self.assertEqual(self._whos(flt), {"user", "agent"})

    def test_exclusion_drops_only_the_named_speakers(self) -> None:
        flt = {"who": surface.SpeakerFilter(None, ("subagent", "tool"))}
        self.assertEqual(self._whos(flt), {"user", "agent", "harness"})

    def test_single_string_path_is_unchanged(self) -> None:
        self.assertEqual(self._whos({"who": "tool"}), {"tool"})

    def test_prose_fts_table_serves_tool_free_selections(self) -> None:
        prose = surface.SpeakerFilter(("user", "agent"), ())
        self.assertEqual(corpusdb._fts_table({"who": prose}),
                         "msgs_prose_fts")
        with_tools = surface.SpeakerFilter(None, ("subagent",))
        self.assertEqual(corpusdb._fts_table({"who": with_tools}), "msgs_fts")

    def test_scan_lane_honors_the_same_filter(self) -> None:
        import common
        import explore
        with common.MESSAGES_PATH.open("w", encoding="utf-8") as f:
            for who in ("user", "agent", "subagent", "harness"):
                f.write(json.dumps({
                    "id": f"codex:sess-{who}:1", "agent": "codex",
                    "project": "proj", "session": f"sess-{who}", "turn": 1,
                    "ts": 1000, "who": who,
                    "text": f"needle spoken by {who}"}) + "\n")
        explore._freshen()
        # tool excluded too: the scan then never opens the event store
        flt = {"who": surface.SpeakerFilter(None, ("subagent", "tool"))}
        whos = {hit["who"]
                for hit in search._word_scan("needle", 40, flt)["hits"]}
        self.assertEqual(whos, {"user", "agent", "harness"})


def _hits():
    import compact
    rows = [
        ("livedchat01", 3, "user", "needle in lived prose"),
        ("harnesschat", 4, "harness", "needle in harness traffic"),
    ]
    return [
        {"session": session, "turn": turn, "ts": 1000 + turn, "who": who,
         "agent": "codex", "project": "proj", "score": 2.0,
         "matched": "phrase", "snippet": text,
         "content_digest": compact.content_digest(text)}
        for session, turn, who, text in rows
    ]


def _run_search(argv, hits=None):
    stdout, stderr = io.StringIO(), io.StringIO()
    result = {"hits": hits if hits is not None else _hits(),
              "total": len(hits if hits is not None else _hits()),
              "chats": 2, "tool_hits": 0, "engine": "corpusdb",
              "mode": "keyword", "totals_exact": True}
    with mock.patch.object(search.indexd_runtime, "ensure_index",
                           return_value=True), \
            mock.patch.object(search.indexd_runtime,
                              "agent_freshness_notice", return_value=""), \
            mock.patch.object(search.common, "in_agent_context",
                              return_value=False), \
            mock.patch.object(search.common, "ingest_bin",
                              return_value=Path("/nonexistent/agrep-rs")), \
            mock.patch.object(search, "run_query", return_value=result), \
            contextlib.redirect_stdout(stdout), \
            contextlib.redirect_stderr(stderr):
        rc = search.main(argv)
    return rc, stdout.getvalue(), stderr.getvalue()


def _search_json_page(output: str) -> tuple[dict, list[dict]]:
    records = [json.loads(line) for line in output.splitlines() if line]
    if not records or records[0].get("kind") != "agrep-meta":
        raise AssertionError("search JSON did not lead with agrep-meta")
    if any(row.get("kind") == "agrep-meta" for row in records[1:]):
        raise AssertionError("search JSON emitted more than one agrep-meta")
    return records[0], records[1:]


class NoMetaSurface(unittest.TestCase):
    def test_no_meta_drops_and_disclosed(self) -> None:
        rc, out, err = _run_search(
            ["needle", "--no-meta", "--classic", "--color", "never"])
        self.assertEqual(rc, 0)
        self.assertNotIn("harness", out)
        self.assertIn("livedchat01", out)
        self.assertIn(surface.meta_exclusion_notice(1), err)

    def test_machine_surfaces_keep_parity_without_the_flag(self) -> None:
        rc, out, _err = _run_search(["needle", "--json"])
        self.assertEqual(rc, 0)
        meta, rows = _search_json_page(out)
        self.assertEqual(meta["kind"], "agrep-meta")
        whos = [row.get("who") for row in rows]
        self.assertIn("harness", whos)

    def test_explicit_flag_narrows_machine_output_too(self) -> None:
        rc, out, err = _run_search(["needle", "--json", "--no-meta"])
        self.assertEqual(rc, 0)
        _meta, rows = _search_json_page(out)
        whos = [row.get("who") for row in rows]
        self.assertNotIn("harness", whos)
        self.assertIn(surface.meta_exclusion_notice(1), err)

    def test_tool_kind_remains_a_hit_after_the_envelope(self) -> None:
        tool = {**_hits()[0], "session": "toolchat", "who": "tool",
                "kind": "tool", "event_kind": "tool"}
        rc, out, _err = _run_search(["needle", "--json"], hits=[tool])
        self.assertEqual(rc, 0)
        _meta, rows = _search_json_page(out)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["session"], rows[0]["kind"]),
                         ("toolchat", "tool"))

    def test_no_meta_retains_one_marked_row_when_all_evidence_is_meta(self) -> None:
        rows = _hits()[1:]
        rows.append({**rows[0], "session": "harnesscopy", "turn": 5})
        with mock.patch.dict(os.environ, {"AGREP_PROFILE": "compact"}):
            rc, out, err = _run_search(
                ["needle", "--no-meta", "--color", "never"], hits=rows)
        self.assertEqual(rc, 0)
        self.assertEqual(out.count("~meta"), 1)
        self.assertEqual(out.count("harnesschat"), 1)
        self.assertNotIn("harnesscopy", out)
        self.assertIn("retained 1 sole ~meta row", err)

    def test_no_meta_refuses_to_narrow_counts(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), \
                self.assertRaises(SystemExit) as raised:
            search.main(["needle", "-c", "--no-meta"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("grep parity", stderr.getvalue())


class CliWhoArguments(unittest.TestCase):
    def test_bad_who_name_exits_2_with_the_vocabulary(self) -> None:
        for argv in (["needle", "--who", "operator"],
                     ["needle", "--no-who", "operator"],
                     ["needle", "--who", "user", "--no-who", "tool"]):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), \
                    self.assertRaises(SystemExit) as raised:
                search.main(argv)
            self.assertEqual(raised.exception.code, 2, argv)

    def test_recall_shares_the_same_parser(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), \
                self.assertRaises(SystemExit) as raised:
            recall.main(["needle", "--who", "operator"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("operator", stderr.getvalue())

    def test_multi_who_reaches_the_engine_filter(self) -> None:
        captured = {}

        def fake_run_query(q, **kwargs):
            captured["who"] = kwargs.get("who")
            return {"hits": [], "total": 0, "chats": 0, "tool_hits": 0,
                    "engine": "corpusdb", "mode": "keyword"}

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(search.indexd_runtime, "ensure_index",
                               return_value=True), \
                mock.patch.object(search.indexd_runtime,
                                  "agent_freshness_notice",
                                  return_value=""), \
                mock.patch.object(search.common, "in_agent_context",
                                  return_value=False), \
                mock.patch.object(search.common, "ingest_bin",
                                  return_value=Path("/nonexistent/agrep-rs")), \
                mock.patch.object(search, "run_query",
                                  side_effect=fake_run_query), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            search.main(["needle", "--who", "user,agent", "--classic",
                         "--color", "never", "--lexical"])
        self.assertEqual(captured["who"],
                         surface.SpeakerFilter(("user", "agent"), ()))


if __name__ == "__main__":
    unittest.main()
