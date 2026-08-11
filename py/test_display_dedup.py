"""Pin the display-lane collapse of byte-identical cross-chat rows.

The hazard both ways: a fan-out of twenty sibling sessions repeating one
prompt drowns the page (the spam this exists to kill), while over-eager
folding could hide a genuinely distinct row behind a 16-bit digest
collision (the snippet in the key is the guard).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import re
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "py"))

from _test_support import isolate_data_dir  # noqa: E402

isolate_data_dir()
import compact
import recall
import search


def _hit(session: str, turn: int, text: str, who: str = "user",
         digest: str | None = "auto") -> dict:
    row = {"session": session, "turn": turn, "ts": 1, "who": who,
           "agent": "claude", "project": "labs", "matched": "all-terms",
           "score": 1.0, "snippet": text}
    if digest == "auto":
        row["content_digest"] = compact.content_digest(text)
    elif digest is not None:
        row["content_digest"] = digest
    return row


class CollapseIdentical(unittest.TestCase):
    def test_cross_chat_duplicates_fold_into_the_best_ranked_copy(self) -> None:
        hits = [_hit(f"session-{i:02d}xxxxxxxx", 3, "run the smoke test")
                for i in range(20)]
        kept = search._collapse_identical(hits)
        self.assertEqual(len(kept), 1)
        self.assertIs(kept[0], hits[0])
        self.assertEqual(kept[0]["_dup_chats"], 19)

    def test_repetition_within_one_chat_is_structure_not_spam(self) -> None:
        hits = [_hit("aaaabbbbcccc", 3, "make test"),
                _hit("aaaabbbbcccc", 90, "make test")]
        kept = search._collapse_identical(hits)
        self.assertEqual(len(kept), 2)
        self.assertNotIn("_dup_chats", kept[0])

    def test_a_lived_copy_represents_a_group_a_meta_row_opened(self) -> None:
        # else the fold inherits ~meta and _demote_meta sinks lived evidence
        meta = {**_hit("meta-sessxxxxxxx", 0, "run the smoke test"),
                "_meta_row": True}
        lived = _hit("live-sessxxxxxxx", 3, "run the smoke test")
        trailing = _hit("late-sessxxxxxxx", 5, "run the smoke test")
        kept = search._collapse_identical([meta, lived, trailing])
        self.assertEqual(kept, [lived])
        self.assertFalse(kept[0].get("_meta_row"))
        self.assertEqual(kept[0]["_dup_chats"], 2)

    def test_digest_collision_with_different_snippet_never_folds(self) -> None:
        a = _hit("aaaabbbbcccc", 1, "first thing", digest="dead")
        b = _hit("ddddeeeeffff", 2, "second thing", digest="dead")
        kept = search._collapse_identical([a, b])
        self.assertEqual(len(kept), 2)

    def test_digestless_rows_never_fold(self) -> None:
        hits = [_hit("aaaabbbbcccc", 1, "same words", digest=None),
                _hit("ddddeeeeffff", 2, "same words", digest=None)]
        kept = search._collapse_identical(hits)
        self.assertEqual(len(kept), 2)

    def test_machine_rows_keep_every_duplicate(self) -> None:
        # grep parity: the collapse lives in renderers, not the result set
        hits = [_hit(f"session-{i:02d}xxxxxxxx", 3, "run the smoke test")
                for i in range(5)]
        self.assertEqual(len(search.public_rows(hits)), 5)


class LineageNearFold(unittest.TestCase):
    """The lineage near-fold runs beneath the exact fold on display lanes:
    sibling near-copies fold as xN, failures and lived prose never do."""

    TEXT = ("subagent worker checked the deploy gates and reported the "
            "rollout summary for shard %s with no anomalies found")

    @classmethod
    def _side(cls, session: str, shard: str, **extra) -> dict:
        row = {**_hit(session, 2, cls.TEXT % shard, who="subagent"),
               "name": "Task", "status": "ok"}
        row.update(extra)
        return row

    ROOTS = {"side-axxxxxxxxxxx": "rootxxxxxxxx",
             "side-bxxxxxxxxxxx": "rootxxxxxxxx",
             "far-cxxxxxxxxxxxx": "otherxxxxxxx"}

    def test_sibling_near_copies_fold_and_carry_union_lineage(self) -> None:
        a = self._side("side-axxxxxxxxxxx", "a01")
        b = self._side("side-bxxxxxxxxxxx", "a02")
        kept = search._near_fold([a, b], self.ROOTS)
        self.assertEqual(kept, [a])
        self.assertEqual(kept[0]["_dup_chats"], 1)
        self.assertEqual(kept[0]["_dup_sessions"], ["side-bxxxxxxxxxxx"])

    def test_failures_and_foreign_families_stay_visible(self) -> None:
        a = self._side("side-axxxxxxxxxxx", "a01")
        failed = self._side("side-bxxxxxxxxxxx", "a02",
                            who="tool", kind="tool", ok=False)
        foreign = self._side("far-cxxxxxxxxxxxx", "a03")
        kept = search._near_fold([a, failed, foreign], self.ROOTS)
        self.assertEqual(len(kept), 3)

    def test_lived_prose_never_near_folds(self) -> None:
        a = _hit("side-axxxxxxxxxxx", 2, self.TEXT % "a01")
        b = _hit("side-bxxxxxxxxxxx", 2, self.TEXT % "a02")
        kept = search._near_fold([a, b], self.ROOTS)
        self.assertEqual(len(kept), 2)

    def test_grouped_render_marks_the_near_fold(self) -> None:
        a = self._side("side-axxxxxxxxxxx", "a01")
        b = self._side("side-bxxxxxxxxxxx", "a02")
        out = io.StringIO()
        with mock.patch.object(search, "_family_roots_for_hits",
                               return_value=dict(self.ROOTS)), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            search._emit_grouped([a, b], None, False)
        self.assertIn("×1 chats", out.getvalue())
        self.assertNotIn("side-bxxxxxxxxxxx", out.getvalue())

    def test_machine_rows_stay_uncollapsed(self) -> None:
        rows = [self._side("side-axxxxxxxxxxx", "a01"),
                self._side("side-bxxxxxxxxxxx", "a02")]
        self.assertEqual(len(search.public_rows(rows)), 2)


class EncodedBlobMatch(unittest.TestCase):
    PAT = re.compile("doit", re.I)
    BLOB = "gAAAAABqWJ9rdoItrSfIl1P0GJ8IoUxhMe5lmCd3f1gYbOGTsPweNmjBtp9r26SX"

    def test_interior_match_in_a_base64_run_is_encoded(self) -> None:
        self.assertTrue(search._match_in_encoded_blob(
            f'message: "{self.BLOB}"', self.PAT))

    def test_prose_occurrence_anywhere_rescues_the_row(self) -> None:
        self.assertFalse(search._match_in_encoded_blob(
            f'just doit already: "{self.BLOB}"', self.PAT))

    def test_prefix_grep_of_a_blob_is_deliberate(self) -> None:
        pat = re.compile("gAAAAAB")
        self.assertFalse(search._match_in_encoded_blob(
            f'message: "{self.BLOB}"', pat))

    def test_short_or_single_alphabet_runs_are_not_blobs(self) -> None:
        # a 64-char lowercase hex digest has no case mix - never marked
        pat = re.compile("deadbeef")
        hexrun = "0d3adeadbeef" + "a1" * 26
        self.assertFalse(search._match_in_encoded_blob(
            f"sha: {hexrun}", pat))

    def test_grouped_render_marks_the_encoded_match(self) -> None:
        hit = _hit("aaaabbbbcccc", 7, f'message: "{self.BLOB}"')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            search._emit_grouped([hit], self.PAT, False)
        self.assertIn("~encoded", out.getvalue())


class ToolRunCollapse(unittest.TestCase):
    @staticmethod
    def _ev(name: str, ok: bool = True) -> dict:
        return {"kind": "tool", "name": name, "input": "x", "ok": ok,
                "output_chars": 100, "output": "", "ts": 1, "turn": 0}

    def test_long_ok_run_collapses_but_failures_keep_their_line(self) -> None:
        import around
        events = ([self._ev("exec_command")] * 6 + [self._ev("web_search")] * 3
                  + [self._ev("git status", ok=False)])
        lines = around._tool_block(events, False, 0, "expand-cmd", collapse=True)
        self.assertEqual(len(lines), 2)
        self.assertIn("FAILED git status", lines[0])
        self.assertIn("9 tool calls", lines[1])
        self.assertIn("expand-cmd", lines[1])

    def test_short_runs_and_full_view_stay_verbatim(self) -> None:
        import around
        events = [self._ev("exec_command")] * 3
        self.assertEqual(
            len(around._tool_block(events, False, 0, "e", collapse=True)), 3)
        many = [self._ev("exec_command")] * 9
        self.assertEqual(
            len(around._tool_block(many, False, 0, "e", collapse=False)), 9)


class CollapseRendering(unittest.TestCase):
    def test_grouped_render_marks_the_fold(self) -> None:
        hits = [_hit(f"session-{i:02d}xxxxxxxx", 3, "run the smoke test")
                for i in range(4)]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            search._emit_grouped(hits, None, False)
        text = out.getvalue()
        self.assertIn("×3 chats", text)
        self.assertEqual(text.count("run the smoke test"), 1)

    def test_compact_line_carries_the_same_fold_count(self) -> None:
        hit = _hit("abcdef012345", 3, "run the smoke test")
        hit["_dup_chats"] = 19
        index = compact.session_prefix_index((hit["session"],))
        line = search._compact_line(hit, index)
        self.assertIn("×19-chats", line)


class MetaProvenance(unittest.TestCase):
    """Panel SP1: a row that quotes an incident must not outrank the row that
    lived it, and the demotion must be visible."""

    @staticmethod
    def _row(project: str, who: str = "user") -> dict:
        return {"session": "aaaabbbbcccc", "turn": 1, "ts": 1, "who": who,
                "agent": "claude", "project": project, "snippet": "x"}

    def test_structured_roles_are_meta_but_project_names_are_not(self) -> None:
        for project in ("/work/agrep/bench/gauntlet", "/w/benchmarks",
                        "/w/fixtures"):
            self.assertFalse(search._meta_row(self._row(project)), project)
        self.assertTrue(search._meta_row(self._row("/work/app", who="harness")))

    def test_lived_sessions_are_never_meta(self) -> None:
        for project in ("/work/webapp", "/w/agrep", "/w/version_control_tools"):
            self.assertFalse(search._meta_row(self._row(project)), project)

    def test_marker_substrings_in_real_project_names_stay_lived(self) -> None:
        # structural only: the dir must BE a bench dir, not contain one -
        # the substring detector this replaces flagged all three as meta
        for project in ("/w/workbench", "/w/benchmark-runs", "/w/fixtures-app"):
            self.assertFalse(search._meta_row(self._row(project)), project)

    def test_demotion_is_marked_not_silent(self) -> None:
        hit = self._row("/work/agrep/bench/gauntlet")
        hit.update(matched="all-terms", score=1.0, _meta_row=True,
                   content_digest=compact.content_digest("x"))
        index = compact.session_prefix_index((hit["session"],))
        self.assertIn("~meta", search._compact_line(hit, index))

    def test_spawned_task_turns_are_meta_but_later_turns_are_not(self) -> None:
        spawn = {**self._row("/work/webapp", who="subagent"), "turn": 0}
        answer = {**self._row("/work/webapp", who="subagent"), "turn": 4}
        self.assertTrue(search._meta_row(spawn))
        self.assertFalse(search._meta_row(answer))

    def test_meta_sinks_below_lived_rows_and_is_counted(self) -> None:
        lived = {**self._row("/work/webapp"), "turn": 3}
        meta = {**self._row("/work/webapp", who="subagent"), "turn": 0,
                "_meta_row": True}
        ordered, sunk = search._demote_meta([meta, lived])
        self.assertEqual(ordered, [lived, meta])
        self.assertEqual(sunk, 1)

    def test_an_all_meta_page_sinks_nothing_and_says_nothing(self) -> None:
        # nothing lived to promote above: the page is what it is, no notice
        meta = {**self._row("/w/bench/gauntlet"), "_meta_row": True}
        ordered, sunk = search._demote_meta([meta])
        self.assertEqual((ordered, sunk), ([meta], 0))

    def test_family_promotion_never_lifts_meta_above_lived(self) -> None:
        # diversify runs after _demote_meta: first-per-family promotion must
        # not re-surface the row the notice just said had sunk
        lived_a = {"session": "aaaa", "family": "fam-a", "snippet": "x"}
        lived_b = {"session": "bbbb", "family": "fam-a", "snippet": "x"}
        meta = {"session": "cccc", "family": "fam-c", "snippet": "x",
                "_meta_row": True}
        ordered = compact.diversify_hits([lived_a, lived_b, meta])
        self.assertEqual([h["session"] for h in ordered],
                         ["aaaa", "bbbb", "cccc"])

    def test_demotion_happens_inside_the_frozen_slice(self) -> None:
        # 'never drop it': demote-then-freeze let >40 lived rows push a sunk
        # meta row off the frozen page entirely
        def row(i: int, meta: bool = False) -> dict:
            r = {**self._row("/w/app"), "session": f"sess-{i:03d}xxxxxxxx",
                 "turn": 2, "score": 1.0, "snippet": f"needle {i}"}
            return {**r, "_meta_row": True} if meta else r

        hits = [row(0), row(1, meta=True)] + [row(i) for i in range(2, 41)]
        captured: dict[str, list[str]] = {}

        def grab(prepared, *_args, **_kwargs):
            captured["sessions"] = [r["session"] for r in prepared]
            return mock.Mock()

        err = io.StringIO()
        with mock.patch.object(compact, "start_compact", side_effect=grab), \
                mock.patch("explore._session_index", return_value={}), \
                contextlib.redirect_stderr(err):
            search._start_compact_page(
                hits, "needle", search._match_pat("needle", "keyword"),
                corpus_more=False)
        self.assertIn(row(1)["session"], captured["sessions"])
        self.assertEqual(captured["sessions"][-1], row(1)["session"])
        # demotion speaks through the ~meta row markers alone (law 7)
        self.assertNotIn("sank below lived sessions", err.getvalue())


class RecallMetaStory(unittest.TestCase):
    """SP1 for recall: the shared engine rank-reduces meta rows (x0.45), so
    recall's render must mark them the way search's does."""

    def test_probe_pointer_carries_the_meta_mark(self) -> None:
        hit = {"session": "abcdef0123456789", "turn": 3, "ts": 1,
               "who": "user", "agent": "claude", "project": "/w/fixtures",
               "score": 1.0, "matched": "phrase", "snippet": "needle",
               "content_digest": compact.content_digest("needle"),
               "_meta_row": True}
        index = compact.session_prefix_index((hit["session"],))
        line = recall._probe_line(["needle"], [hit], "corpusdb", 1,
                                  session_index=index)
        self.assertIn("~meta", line or "")

    def test_lived_rows_stay_unmarked(self) -> None:
        self.assertEqual(recall._provenance_marks({"session": "a"}), "")
        self.assertEqual(
            recall._provenance_marks({"_self": True, "_meta_row": True}),
            "~self ~meta")

class CountingModel(unittest.TestCase):
    """C6/SP5: three models could not parse "showing 39 of at least 92 hits
    (75 in tool output)" - the unit and the subset relationship must be said."""

    def _run(self, argv: list[str], hits: list[dict], total: int,
             tool_hits: int = 0, exact: bool = True):
        result = {"hits": hits, "total": total, "chats": 1,
                  "engine": "keyword", "mode": "keyword",
                  "totals_exact": exact, "tool_hits": tool_hits}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"AGREP_PROFILE": ""}), \
                mock.patch.object(search.indexd_runtime, "ensure_index",
                                  return_value=True), \
                mock.patch.object(search, "run_query", return_value=result), \
                mock.patch.object(search.common, "transcript_generation",
                                  return_value={"generation": 1}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            search.main([
                *argv, "--no-auto", "--classic", "--color", "never"])
        return out.getvalue(), err.getvalue()

    def test_piped_header_names_the_unit_and_the_subset(self) -> None:
        rows = [_hit(f"session-{i:02d}xxxx", i, f"row {i}") for i in range(5)]
        _out, err = self._run(["needle", "-n", "5"], rows, 1375, tool_hits=1118)
        self.assertIn("showing 5 of 1375 matching rows", err)
        self.assertIn("(1118 of them in tool output)", err)
        self.assertNotIn("at least", err)

    def test_a_bounded_total_says_plus_not_at_least(self) -> None:
        rows = [_hit("session-01xxxx", 1, "row")]
        _out, err = self._run(["needle", "-n", "1"], rows, 92, exact=False)
        self.assertIn("of 92+ matching rows", err)


class SlowLaneHealsItself(unittest.TestCase):
    """Goal 11 for the unusable-db lane: the state is repaired, not narrated.

    A pending repair or a dead foreign owner's stores are agrep's own damage;
    the query kicks the background repair and serves the scan in silence
    (laws 1 and 3). Only a claim the kick cannot reclaim - a held foreign
    owner - earns the one line naming that one cause (law 5)."""

    def setUp(self) -> None:
        # the once-per-process announce guard must not leak across tests
        search._SLOW_LANE_ANNOUNCED.clear()
        self.addCleanup(search._SLOW_LANE_ANNOUNCED.clear)

    def _run_unusable_db(self, argv: list[str],
                         kick: "indexd_runtime.RepairKick" = None) -> str:
        out, err = io.StringIO(), io.StringIO()
        import corpusdb as corpusdb_mod
        import indexd_runtime
        search._load_corpusdb()
        if kick is None:
            kick = indexd_runtime.RepairKick(True, "")
        with mock.patch.dict(os.environ, {"AGREP_PROFILE": ""}), \
                mock.patch.object(search.indexd_runtime, "ensure_index",
                                  return_value=True), \
                mock.patch.object(search.indexd_runtime,
                                  "kick_background_repair",
                                  return_value=kick) as kicked, \
                mock.patch.object(corpusdb_mod, "connect",
                                  return_value=None), \
                mock.patch.object(corpusdb_mod, "query_rebuild_required",
                                  return_value=True), \
                mock.patch.object(type(corpusdb_mod.DB_PATH), "exists",
                                  lambda self: True), \
                mock.patch.object(search.common, "transcript_generation",
                                  return_value={"generation": 1}), \
                mock.patch.object(search, "_native_event_shape",
                                  return_value=False), \
                mock.patch("explore.direct_snapshot_attempt",
                           side_effect=lambda **_kw: contextlib.nullcontext()), \
                mock.patch("explore.keyword_search",
                           return_value={"hits": [], "total": 0}), \
                mock.patch.object(search, "_terms_scan",
                                  return_value={"hits": [], "total": 0}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            search.main(argv)
        self._kicked = kicked
        return err.getvalue()

    def test_unusable_db_kicks_repair_and_scans_in_silence(self) -> None:
        err = self._run_unusable_db(["zz probe", "--classic", "--color", "never"])
        self._kicked.assert_called()
        self.assertNotIn("scanning", err)
        self.assertNotIn("expect tens of seconds", err)

    def test_user_chosen_declines_stay_silent(self) -> None:
        import indexd_runtime
        for cause in ("readonly", "no-daemon", "no-binary", "spawn-failed"):
            search._SLOW_LANE_ANNOUNCED.clear()
            err = self._run_unusable_db(
                ["zz probe", "--classic", "--color", "never"],
                kick=indexd_runtime.RepairKick(False, cause))
            self.assertNotIn("scanning", err, cause)

    def test_a_held_foreign_owner_earns_its_one_line(self) -> None:
        import indexd_runtime
        err = self._run_unusable_db(
            ["zz probe", "--classic", "--color", "never"],
            kick=indexd_runtime.RepairKick(False, "held-foreign-owner"))
        self.assertIn("held by another agrep - scanning sources this query",
                      err)
        self.assertIn("--who/--project/--since narrows it", err)

    def test_held_owner_lever_tail_omits_filters_already_in_use(self) -> None:
        import indexd_runtime
        err = self._run_unusable_db(
            ["zz probe", "--who", "agent", "--classic", "--color", "never"],
            kick=indexd_runtime.RepairKick(False, "held-foreign-owner"))
        self.assertIn("--project/--since narrows it", err)
        self.assertNotIn("--who/", err)

    def test_held_owner_line_prints_once_per_process(self) -> None:
        import indexd_runtime
        held = indexd_runtime.RepairKick(False, "held-foreign-owner")
        first = self._run_unusable_db(
            ["zz probe", "--classic", "--color", "never"], kick=held)
        second = self._run_unusable_db(
            ["zz probe two", "--classic", "--color", "never"], kick=held)
        self.assertEqual(first.count("held by another agrep"), 1)
        self.assertNotIn("held by another agrep", second)

    def _run_without_index(self, argv):
        out, err = io.StringIO(), io.StringIO()
        import corpusdb as corpusdb_mod
        search._load_corpusdb()
        with mock.patch.dict(os.environ, {"AGREP_PROFILE": ""}), \
                mock.patch.object(search.indexd_runtime, "ensure_index",
                                  return_value=True), \
                mock.patch.object(search.common, "in_agent_context",
                                  return_value=False), \
                mock.patch.object(corpusdb_mod, "connect",
                                  return_value=None), \
                mock.patch.object(corpusdb_mod, "_trigram_ok",
                                  return_value=True), \
                mock.patch.object(type(corpusdb_mod.DB_PATH), "exists",
                                  lambda self: False), \
                mock.patch.object(search.common, "transcript_generation",
                                  return_value={"generation": 1}), \
                mock.patch.object(search.indexd_runtime, "freshness_story",
                                  return_value=search.surface.FreshnessStory(
                                      "current")), \
                mock.patch.object(search, "_native_event_shape",
                                  return_value=False), \
                mock.patch("explore.direct_snapshot_attempt",
                           side_effect=lambda **_kw: contextlib.nullcontext()), \
                mock.patch("explore.keyword_search",
                           return_value={"hits": [], "total": 0,
                                         "chats": 0, "term_hits": []}) as scan, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = search.main(argv)
        return rc, out.getvalue(), err.getvalue(), scan

    def test_missing_index_uses_the_exact_scan_while_it_self_heals(self) -> None:
        rc, _out, err, scan = self._run_without_index(
            ["zz probe", "--who", "tool", "--classic", "--color", "never"])
        self.assertEqual(rc, 1)
        scan.assert_called_once()
        self.assertNotIn("agrep index", err)

    def test_missing_index_json_reports_the_exact_scan_result(self) -> None:
        rc, out, _err, scan = self._run_without_index(
            ["zz probe", "--who", "tool", "--json"])
        self.assertEqual(rc, 1)
        scan.assert_called_once()
        meta = json.loads(out)
        self.assertEqual(meta["engine"], "jsonl")
        self.assertNotIn("error", meta)
        self.assertEqual(meta["hits"], [])


if __name__ == "__main__":
    unittest.main()
