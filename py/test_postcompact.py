"""Deterministic continuation packets stay root-only, recent, and bounded."""

from __future__ import annotations

import contextlib
from datetime import datetime
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from _test_support import isolate_data_dir

isolate_data_dir()

import postcompact  # noqa: E402
import corpusdb  # noqa: E402
import index_lock  # noqa: E402
import session_context  # noqa: E402


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE msgs("
        "id INTEGER PRIMARY KEY, session TEXT, turn INTEGER, ts INTEGER, "
        "agent TEXT, project TEXT, model TEXT, model_source TEXT, who TEXT, "
        "text TEXT, content_digest TEXT)"
    )
    rows = [
        (1, "root", 1, 1, "codex", "agrep", "m", "explicit", "user",
         "older phase", None),
        (2, "root", 2, 2, "codex", "agrep", "", "recap", "recap", "", None),
        (3, "root", 3, 3, "codex", "agrep", "m", "explicit", "user",
         "implement the already-pasted plan", None),
        (4, "root", 3, 3, "codex", "agrep", "m", "explicit", "agent",
         "starting implementation now", None),
        (5, "root", 4, 4, "codex", "agrep", "", "control", "control",
         "continue", None),
        (6, "root", 5, 5, "codex", "agrep", "m", "explicit", "subagent",
         "stale delegated review", None),
        (7, "root", 6, 6, "codex", "agrep", "", "", "tool",
         "secret tool output", None),
        (8, "root", 7, 7, "codex", "agrep", "m", "explicit", "user",
         "Fable owns all security", None),
        (9, "root", 7, 7, "codex", "agrep", "m", "explicit", "agent",
         "continuing the adopted implementation phase", None),
        (10, "root", 8, 8, "codex", "agrep", "", "recap", "recap", "", None),
        (11, "root", 9, 9, "codex", "agrep", "m", "explicit", "user",
         "visible current prompt", None),
        (12, "child", 6, 6, "codex", "agrep", "m", "explicit", "user",
         "delegated-only child prompt", None),
        (13, "child", 6, 6, "codex", "agrep", "m", "explicit", "agent",
         "delegated-only child conclusion", None),
    ]
    db.executemany(
        "INSERT INTO msgs VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
    return db


def _family(boundary: int | None = 8) -> session_context.CallingFamily:
    return session_context.CallingFamily(
        "root", "root", frozenset({"root", "child"}), True, boundary)


class PacketTests(unittest.TestCase):
    def test_packet_selects_only_the_immediate_precompact_root_window(self) -> None:
        db = _db()
        try:
            packet = postcompact.read_packet(db, _family())
        finally:
            db.close()
        self.assertEqual(packet["status"], "recovered")
        self.assertEqual(packet["selection"]["previous_boundary_turn"], 2)
        self.assertEqual(
            [(row["turn"], row["who"]) for row in packet["rows"]],
            [(3, "user"), (3, "agent"), (7, "user"), (7, "agent")],
        )
        rendered = "\n".join(row["text"] for row in packet["rows"])
        for excluded in (
                "older phase", "continue", "stale delegated review",
                "secret tool output", "visible current prompt",
                "delegated-only child prompt",
                "delegated-only child conclusion"):
            self.assertNotIn(excluded, rendered)
        self.assertTrue(all(row["handle"].startswith("@root:")
                            for row in packet["rows"]))
        self.assertTrue(all(row["source_truncated"] is None
                            for row in packet["rows"]))
        self.assertEqual(
            packet["coverage"]["source_truncation_state"],
            "unavailable_in_materialized_index",
        )
        self.assertEqual(packet["selection"]["scope"], "root-only")
        self.assertEqual(
            packet["selection"]["delegated_sessions"], "excluded")
        self.assertNotIn("delegated", packet)
        self.assertEqual(packet["omissions"]["tools"], "policy_excluded")
        self.assertEqual(
            packet["omissions"]["delegated_sessions"], "policy_excluded")

    def test_compacted_resume_serves_the_family_root_tail(self) -> None:
        """pi/omp compaction starts a NEW session whose recap is turn 1: the
        pre-boundary tail lives in the family root. The walk crosses into
        the root, capped at the boundary row's timestamp so nothing written
        after the compaction moment leaks in as pre-compact context."""
        db = _db()
        try:
            db.executemany(
                "INSERT INTO msgs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (60, "resumed", 1, 8, "pi", "agrep", "", "recap",
                     "recap", "Resume prior conversation.", None),
                    (61, "resumed", 2, 9, "pi", "agrep", "m", "explicit",
                     "user", "post-compact prompt", None),
                    # written to the root after the compaction moment: excluded
                    (62, "root", 9, 9, "codex", "agrep", "m", "explicit",
                     "user", "late root row after the boundary", None),
                ])
            family = session_context.CallingFamily(
                "resumed", "root", frozenset({"root", "resumed"}), True, 1)
            packet = postcompact.read_packet(db, family)
        finally:
            db.close()
        self.assertEqual(packet["status"], "recovered")
        self.assertEqual(packet["selection"]["window_source"], "family_root")
        self.assertEqual(
            [(row["session"], row["turn"], row["who"])
             for row in packet["rows"]],
            [("root", 3, "user"), ("root", 3, "agent"),
             ("root", 7, "user"), ("root", 7, "agent")],
        )
        rendered = "\n".join(row["text"] for row in packet["rows"])
        self.assertNotIn("late root row after the boundary", rendered)
        self.assertNotIn("post-compact prompt", rendered)
        self.assertIn("window served from the family root",
                      postcompact._human(packet))

    def test_caller_window_still_wins_when_it_has_content(self) -> None:
        # An in-place compaction (claude/codex shape) never crosses into the
        # root: the caller session's own window serves.
        db = _db()
        try:
            packet = postcompact.read_packet(db, _family())
        finally:
            db.close()
        self.assertEqual(packet["selection"]["window_source"], "caller")
        self.assertNotIn("window served from the family root",
                         postcompact._human(packet))

    def test_adjacent_boundaries_fall_back_to_the_nearest_filled_window(self) -> None:
        # An archive resume immediately re-compacted leaves recap rows on
        # adjacent turns; the packet must serve the nearest earlier window
        # instead of a useless proven-empty one.
        db = _db()
        try:
            db.execute(
                "INSERT INTO msgs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (50, "root", 9, 9, "codex", "agrep", "", "recap", "recap",
                 "", None))
            packet = postcompact.read_packet(db, _family(boundary=9))
        finally:
            db.close()
        self.assertEqual(packet["status"], "recovered")
        self.assertEqual(packet["selection"]["window_fallbacks"], 1)
        self.assertEqual(packet["selection"]["previous_boundary_turn"], 2)
        self.assertEqual(
            [(row["turn"], row["who"]) for row in packet["rows"]],
            [(3, "user"), (3, "agent"), (7, "user"), (7, "agent")],
        )
        rendered = postcompact._human(packet)
        self.assertIn("newest 1 window(s) before this boundary were empty",
                      rendered)

    def test_a_session_with_no_content_before_any_boundary_stays_empty(self) -> None:
        db = sqlite3.connect(":memory:")
        try:
            db.execute(
                "CREATE TABLE msgs("
                "id INTEGER PRIMARY KEY, session TEXT, turn INTEGER, "
                "ts INTEGER, agent TEXT, project TEXT, model TEXT, "
                "model_source TEXT, who TEXT, text TEXT, content_digest TEXT)")
            db.executemany(
                "INSERT INTO msgs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                [(1, "root", 1, 1, "codex", "agrep", "", "recap", "recap",
                  "", None),
                 (2, "root", 2, 2, "codex", "agrep", "", "recap", "recap",
                  "", None)])
            packet = postcompact.read_packet(db, _family(boundary=2))
        finally:
            db.close()
        self.assertEqual(packet["status"], "empty")
        self.assertEqual(packet["selection"]["window_fallbacks"], 1)
        self.assertNotIn(
            "window(s) before this boundary", postcompact._human(packet))

    def test_child_sessions_are_never_injected_into_the_root_packet(self) -> None:
        db = _db()
        try:
            db.executemany(
                "INSERT INTO msgs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (14, "child-2", 1, 4, "codex", "agrep", "m", "explicit",
                     "user", "second commission", None),
                    (15, "child-2", 1, 5, "codex", "agrep", "m", "explicit",
                     "agent", "answer only present in child two", None),
                ],
            )
            family = session_context.CallingFamily(
                "root", "root", frozenset({"root", "child", "child-2"}),
                True, 8)
            packet = postcompact.read_packet(db, family)
        finally:
            db.close()
        wire = json.dumps(packet, ensure_ascii=False)
        self.assertNotIn("delegated-only child conclusion", wire)
        self.assertNotIn("answer only present in child two", wire)
        self.assertEqual(packet["selection"]["scope"], "root-only")
        self.assertEqual(
            packet["omissions"]["delegated_sessions"], "policy_excluded")

    def test_newest_blocks_are_selected_then_rendered_chronologically(self) -> None:
        db = _db()
        try:
            db.executemany(
                "INSERT INTO msgs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                [(100 + turn, "root", turn, turn, "codex", "agrep", "m",
                  "explicit", "user", f"block {turn}", None)
                 for turn in range(10, 20)],
            )
            packet = postcompact.read_packet(db, _family(boundary=20))
        finally:
            db.close()
        shown = [row["turn"] for row in packet["rows"]]
        self.assertEqual(shown, list(range(12, 20)))
        self.assertEqual(packet["coverage"]["shown_root_blocks"], 8)
        self.assertGreater(packet["omissions"]["root_blocks"], 0)

    def test_long_rows_keep_head_and_tail_inside_the_byte_budget(self) -> None:
        text = "HEAD-" + "x" * 5000 + "-TAIL"
        clipped, omitted = postcompact._clip_utf8(text, 500)
        self.assertGreater(omitted, 0)
        self.assertIn("HEAD-", clipped)
        self.assertIn("-TAIL", clipped)
        self.assertIn("UTF-8 bytes omitted", clipped)
        self.assertLessEqual(len(clipped.encode("utf-8")), 500)

    def test_human_contract_says_supplement_and_stays_bounded(self) -> None:
        db = _db()
        try:
            rendered = postcompact._human(
                postcompact.read_packet(db, _family()))
        finally:
            db.close()
        self.assertTrue(rendered.startswith("postcompact: supplement"))
        self.assertIn("newest blocks selected, chronological render", rendered)
        self.assertIn("tools and delegated sessions excluded", rendered)
        self.assertLessEqual(
            len(rendered.encode("utf-8")), postcompact.OUTPUT_BUDGET_BYTES)

    def test_long_full_packet_honors_human_and_json_byte_contracts(self) -> None:
        db = _db()
        try:
            rows = []
            row_id = 100
            for turn in range(10, 18):
                for who in ("user", "agent"):
                    rows.append((
                        row_id, "root", turn, turn, "codex", "p" * 200,
                        "model" * 20, "explicit", who,
                        f"{who}-" + "x" * 5_000, None,
                    ))
                    row_id += 1
            rows.extend([
                (row_id, "root", 20, 20, "codex", "agrep", "", "recap",
                 "recap", "", None),
                (row_id + 1, "child-2", 17, 17, "codex", "agrep", "m",
                 "explicit", "user", "commission", None),
                (row_id + 2, "child-2", 17, 17, "codex", "agrep", "m",
                 "explicit", "agent", "child-" + "y" * 5_000, None),
            ])
            db.executemany(
                "INSERT INTO msgs VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
            family = session_context.CallingFamily(
                "root", "root", frozenset({"root", "child", "child-2"}),
                True, 20)
            packet = postcompact.read_packet(db, family)
        finally:
            db.close()

        human = postcompact._human(packet).encode("utf-8")
        wire = json.dumps(
            packet, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.assertLessEqual(len(human), postcompact.OUTPUT_BUDGET_BYTES)
        self.assertLessEqual(len(wire), postcompact.JSON_OUTPUT_BUDGET_BYTES)
        self.assertEqual(packet["coverage"]["shown_root_blocks"], 8)
        self.assertEqual(
            packet["omissions"]["delegated_sessions"], "policy_excluded")
        self.assertNotIn("project", packet["rows"][0])
        self.assertNotIn("model", packet["rows"][0])

    def test_human_metadata_is_terminal_safe(self) -> None:
        db = _db()
        try:
            db.execute(
                "UPDATE msgs SET project=?, agent=? WHERE session='root'",
                ("unsafe\x1b[31mproject", "codex\x1b[2J"),
            )
            rendered = postcompact._human(
                postcompact.read_packet(db, _family()))
        finally:
            db.close()
        self.assertNotIn("\x1b", rendered)


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        for target, name, value in (
                (postcompact.indexd_runtime, "request_recovery_refresh", "fixture"),
                (postcompact.indexd_runtime, "recovery_refresh_complete", True),
                (postcompact.indexd_runtime, "release_recovery_request", None),
                (postcompact.indexd_runtime, "kick_background_repair", None),
                (postcompact, "_snapshot_current", True)):
            patch = mock.patch.object(target, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)

    @staticmethod
    @contextlib.contextmanager
    def _snapshot(boundary: int | None = 8):
        db = _db()
        try:
            yield (session_context.CallerIdentity("root", "codex"),
                   _family(boundary), db)
        finally:
            db.close()

    def _run(self, argv: list[str], *, boundary: int | None = 8):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(
                postcompact.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(
                    postcompact.indexd_runtime, "agent_freshness_notice",
                    return_value=None), \
                mock.patch.object(
                    postcompact.session_context, "calling_family_snapshot",
                    side_effect=lambda: self._snapshot(boundary)), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = postcompact.main(argv)
        return rc, stdout.getvalue(), stderr.getvalue()

    def test_json_is_one_packet_and_no_auto_is_explicitly_partial(self) -> None:
        rc, stdout, stderr = self._run(["--json", "--no-auto"])
        self.assertEqual((rc, stderr), (2, ""))
        packet = json.loads(stdout)
        self.assertEqual(packet["status"], "partial")
        self.assertEqual(packet["coverage"]["index_freshness"], "unchecked")
        self.assertFalse(packet["authority"]["semantic_search_performed"])
        self.assertFalse(packet["implicit_widening"])

    def test_missing_boundary_fails_without_transcript_prose(self) -> None:
        rc, stdout, stderr = self._run([], boundary=None)
        self.assertEqual((rc, stdout), (2, ""))
        self.assertIn("no structural compaction boundary", stderr)
        self.assertNotIn("Fable owns", stderr)

    def test_query_shaped_argument_is_rejected(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            postcompact.main(["search words"])
        self.assertEqual(raised.exception.code, 2)

    def test_explicit_session_resolves_when_caller_cannot_be_identified(self) -> None:
        db = _db()
        try:
            with mock.patch.object(
                    postcompact.indexd_runtime, "ensure_index",
                    return_value=True), \
                    mock.patch.object(
                        postcompact.session_context,
                        "_open_session_family_index",
                        return_value=db), \
                    mock.patch.object(
                        postcompact.session_context,
                        "_indexed_calling_family_state_in_db",
                        return_value=("root", "root",
                                      frozenset({"root", "child"}), 8)):
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), \
                        contextlib.redirect_stderr(stderr):
                    rc = postcompact.main(["--session", "root", "--json"])
        finally:
            db.close()
        self.assertEqual(rc, 0)
        packet = json.loads(stdout.getvalue())
        self.assertEqual(packet["status"], "recovered")
        self.assertEqual(stderr.getvalue(), "")

    def test_explicit_session_empty_tail_exits_one(self) -> None:
        # The --session branch shares the exit contract: proven-empty is 1,
        # never a silent success (it returned a hardcoded 0 once).
        db = _db()
        try:
            with mock.patch.object(
                    postcompact.indexd_runtime, "ensure_index",
                    return_value=True), \
                    mock.patch.object(
                        postcompact.session_context,
                        "_open_session_family_index",
                        return_value=db), \
                    mock.patch.object(
                        postcompact.session_context,
                        "_indexed_calling_family_state_in_db",
                        return_value=("root", "root",
                                      frozenset({"root", "child"}), 0)):
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), \
                        contextlib.redirect_stderr(stderr):
                    rc = postcompact.main(["--session", "root", "--json"])
        finally:
            db.close()
        self.assertEqual(rc, 1)
        self.assertEqual(
            json.loads(stdout.getvalue())["status"], "empty")

    def test_explicit_session_no_auto_exits_partial(self) -> None:
        db = _db()
        try:
            with mock.patch.object(
                    postcompact.indexd_runtime, "ensure_index",
                    return_value=True), \
                    mock.patch.object(
                        postcompact.session_context,
                        "_open_session_family_index",
                        return_value=db), \
                    mock.patch.object(
                        postcompact.session_context,
                        "_indexed_calling_family_state_in_db",
                        return_value=("root", "root",
                                      frozenset({"root", "child"}), 8)):
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), \
                        contextlib.redirect_stderr(stderr):
                    rc = postcompact.main(
                        ["--session", "root", "--json", "--no-auto"])
        finally:
            db.close()
        self.assertEqual(rc, 2)
        self.assertEqual(
            json.loads(stdout.getvalue())["status"], "partial")

    def test_explicit_session_without_boundary_fails_cleanly(self) -> None:
        db = _db()
        try:
            with mock.patch.object(
                    postcompact.indexd_runtime, "ensure_index",
                    return_value=True), \
                    mock.patch.object(
                        postcompact.session_context,
                        "_open_session_family_index",
                        return_value=db), \
                    mock.patch.object(
                        postcompact.session_context,
                        "_indexed_calling_family_state_in_db",
                        return_value=None):
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), \
                        contextlib.redirect_stderr(stderr):
                    rc = postcompact.main(["--session", "unknown"])
        finally:
            db.close()
        self.assertEqual(rc, 2)
        self.assertIn("no structural compaction boundary", stderr.getvalue())

    def test_explicit_session_with_family_but_no_recap_retries_then_names_it(
            self) -> None:
        """A missing recap remains scoped to the requested session."""
        opened = []

        def _fresh_db(*, allow_behind=False):
            opened.append(_db())
            return opened[-1]

        try:
            with mock.patch.object(
                    postcompact, "_REFRESH_WAIT_S", 0.0), \
                    mock.patch.object(
                        postcompact.indexd_runtime, "ensure_index",
                        return_value=True), \
                    mock.patch.object(
                        postcompact.session_context,
                        "_open_session_family_index",
                        side_effect=_fresh_db), \
                    mock.patch.object(
                        postcompact.session_context,
                        "_indexed_calling_family_state_in_db",
                        return_value=("root", "root", frozenset({"root"}), None)):
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), \
                        contextlib.redirect_stderr(stderr):
                    rc = postcompact.main(["--session", "root"])
        finally:
            for db in opened:
                db.close()
        self.assertEqual(rc, 2)
        self.assertIn("session root", stderr.getvalue())
        self.assertNotIn("this caller", stderr.getvalue())

    def test_boundary_landing_during_the_retry_window_recovers(self) -> None:
        opened = []

        def _fresh_db(*, allow_behind=False):
            opened.append(_db())
            return opened[-1]

        try:
            with mock.patch.object(
                    postcompact, "_REFRESH_WAIT_S", 1.0), \
                    mock.patch.object(
                        postcompact.indexd_runtime, "recovery_refresh_complete",
                        side_effect=[False, False, True]), \
                    mock.patch.object(
                        postcompact.indexd_runtime, "ensure_index",
                        return_value=True), \
                    mock.patch.object(
                        postcompact.indexd_runtime, "agent_freshness_notice",
                        return_value=None), \
                    mock.patch.object(
                        postcompact.session_context,
                        "_open_session_family_index",
                        side_effect=_fresh_db), \
                    mock.patch.object(
                        postcompact.session_context,
                        "_indexed_calling_family_state_in_db",
                        return_value=("root", "root", frozenset({"root", "child"}), 8)):
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), \
                        contextlib.redirect_stderr(stderr):
                    rc = postcompact.main(["--session", "root", "--json"])
        finally:
            for db in opened:
                db.close()
        self.assertEqual(rc, 0)
        self.assertEqual(
            json.loads(stdout.getvalue())["status"], "recovered")
        self.assertEqual(stderr.getvalue(), "")

    def test_unfinished_refresh_serves_a_partial_packet(self) -> None:
        opened = []

        def _fresh_db(*, allow_behind=False):
            opened.append(_db())
            return opened[-1]

        try:
            with mock.patch.object(
                    postcompact, "_REFRESH_WAIT_S", 0.0), \
                    mock.patch.object(
                        postcompact.indexd_runtime, "recovery_refresh_complete",
                        return_value=False), \
                    mock.patch.object(
                        postcompact.indexd_runtime, "ensure_index",
                        return_value=True), \
                    mock.patch.object(
                        postcompact.indexd_runtime, "agent_freshness_notice",
                        return_value="index is tearing down"), \
                    mock.patch.object(
                        postcompact.session_context,
                        "_open_session_family_index",
                        side_effect=_fresh_db), \
                    mock.patch.object(
                        postcompact.session_context,
                        "_indexed_calling_family_state_in_db",
                        return_value=("root", "root",
                                      frozenset({"root", "child"}), 8)):
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), \
                        contextlib.redirect_stderr(stderr):
                    rc = postcompact.main(["--session", "root", "--json"])
        finally:
            for db in opened:
                db.close()
        self.assertEqual(rc, 2)
        packet = json.loads(stdout.getvalue())
        self.assertEqual(packet["status"], "partial")
        self.assertEqual(
            packet["coverage"]["index_freshness"], "index is tearing down")
        self.assertEqual(stderr.getvalue(), "")


    def test_requested_compaction_is_not_replaced_by_a_later_boundary(self) -> None:
        rc, stdout, stderr = self._run(["--json", "--boundary-ms", "2"])
        packet = json.loads(stdout)
        self.assertEqual(rc, 0)
        self.assertEqual(packet["selection"]["boundary_turn"], 2)
        self.assertEqual([row["text"] for row in packet["rows"]], ["older phase"])
        self.assertEqual(stderr, "")

    def test_silent_notice_cannot_mark_a_pending_refresh_fresh(self) -> None:
        with mock.patch.object(
                postcompact.indexd_runtime, "recovery_refresh_complete",
                return_value=False), \
                mock.patch.object(postcompact, "_REFRESH_WAIT_S", 0.0):
            rc, stdout, stderr = self._run(["--json"])
        packet = json.loads(stdout)
        self.assertEqual(rc, 2)
        self.assertEqual(packet["status"], "partial")
        self.assertNotEqual(packet["coverage"]["index_freshness"], "fresh")
        self.assertEqual(packet["selection"]["boundary_turn"], 8)
        self.assertEqual(stderr, "")

    def test_timestamped_recovery_needs_a_published_snapshot_not_global_freshness(
            self) -> None:
        for current, expected_exit in ((True, 0), (False, 2)):
            with self.subTest(snapshot_current=current), \
                    mock.patch.object(
                        postcompact.indexd_runtime, "recovery_refresh_complete",
                        return_value=False), \
                    mock.patch.object(
                        postcompact, "_snapshot_current", return_value=current), \
                    mock.patch.object(postcompact, "_REFRESH_WAIT_S", 0.0):
                rc, stdout, stderr = self._run(
                    ["--json", "--boundary-ms", "2"])
            packet = json.loads(stdout)
            self.assertEqual(rc, expected_exit)
            self.assertEqual(packet["selection"]["boundary_turn"], 2)
            self.assertEqual(
                [row["text"] for row in packet["rows"]], ["older phase"])
            self.assertNotEqual(packet["coverage"]["index_freshness"], "fresh")
            self.assertEqual(
                packet["coverage"]["index_freshness"] == "indexed-snapshot",
                current)
            self.assertEqual(stderr, "")

    def test_unresolvable_generation_never_substitutes_another_boundary(
            self) -> None:
        opened = []

        def _fresh_db(*, allow_behind=False):
            if not allow_behind:
                return None
            opened.append(_db())
            return opened[-1]

        try:
            with mock.patch.object(
                    postcompact, "_REFRESH_WAIT_S", 0.0), \
                    mock.patch.object(
                        postcompact.indexd_runtime, "ensure_index",
                        return_value=True), \
                    mock.patch.object(
                        postcompact.session_context,
                        "_open_session_family_index",
                        side_effect=_fresh_db), \
                    mock.patch.object(
                        postcompact.session_context,
                        "_indexed_calling_family_state_in_db",
                        return_value=("root", "root",
                                      frozenset({"root", "child"}), 8)):
                for boundary, expected in ((None, 8), (2, 2), (3, None)):
                    with self.subTest(boundary=boundary):
                        argv = ["--session", "root", "--json"]
                        if boundary is not None:
                            argv.extend(["--boundary-ms", str(boundary)])
                        stdout, stderr = io.StringIO(), io.StringIO()
                        with contextlib.redirect_stdout(stdout), \
                                contextlib.redirect_stderr(stderr):
                            rc = postcompact.main(argv)
                        self.assertEqual(rc, 2)
                        packet = json.loads(stdout.getvalue())
                        if expected is None:
                            self.assertNotIn("selection", packet)
                            self.assertNotIn("rows", packet)
                        else:
                            self.assertEqual(
                                packet["selection"]["boundary_turn"], expected)
                            self.assertEqual(packet["status"], "partial")
                            self.assertEqual(
                                packet["coverage"]["index_freshness"],
                                postcompact._FAMILY_CHURN_NOTICE)
                        self.assertEqual(stderr.getvalue(), "")
        finally:
            for db in opened:
                db.close()


class SnapshotCurrencyTests(unittest.TestCase):
    def test_startup_failure_cancels_the_pending_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as raw, \
                mock.patch.object(postcompact.common, "DATA_DIR", Path(raw)), \
                mock.patch.dict(os.environ, {"AGREP_NO_DAEMON": ""}), \
                mock.patch.object(postcompact.indexd_runtime, "kick_background_repair"), \
                mock.patch.object(
                    postcompact.indexd_runtime, "ensure_index",
                    side_effect=RuntimeError("startup failed")):
            with self.assertRaisesRegex(RuntimeError, "startup failed"):
                postcompact.main(["--session", "root", "--json"])
            self.assertEqual(
                list((Path(raw) / ".recovery_requests").iterdir()), [])

    def test_completed_refresh_cannot_validate_old_message_rows(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            messages = root / "messages.jsonl"
            messages.write_text("old publication\n", encoding="utf-8")
            db = _db()
            try:
                db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
                with mock.patch.object(postcompact.common, "DATA_DIR", root):
                    db.execute("INSERT INTO meta VALUES('stamp', ?)", (corpusdb._stamp(),))
                    self.assertTrue(postcompact._snapshot_current(db))
                    messages.write_text("new publication with another recap\n", encoding="utf-8")
                    current = postcompact._snapshot_current(db)
                    self.assertFalse(current)
                    packet = postcompact.read_packet(db, _family())
                    stdout = io.StringIO()
                    with mock.patch.object(
                            postcompact.indexd_runtime, "agent_freshness_notice",
                            return_value=""), contextlib.redirect_stdout(stdout):
                        rc = postcompact._finish(
                            mock.Mock(no_auto=False, json=True, boundary_ms=None), packet,
                            retry_pending=False, refresh_complete=True,
                            snapshot_current=current)
                    self.assertEqual(rc, 2)
                    self.assertEqual(json.loads(stdout.getvalue())["status"], "partial")
            finally:
                db.close()


class RecoveryReadPathTests(unittest.TestCase):
    @staticmethod
    def _environment(root: Path, binary: Path, *, automatic: bool) -> dict[str, str]:
        home, data = root / "home", root / "data"
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("AGREP_")}
        env.update({
            "HOME": str(home), "USERPROFILE": str(home),
            "AGREP_HOME": str(home), "AGREP_DATA_DIR": str(data),
            "AGREP_MODEL_DIR": str(root / "models"), "AGREP_RS_BIN": str(binary),
            "AGREP_NO_FETCH": "1", "APPDATA": str(root / "appdata"),
            "LOCALAPPDATA": str(root / "localappdata"),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(root / "share"),
            "CODEX_HOME": str(home / ".codex"), "CLINE_DIR": str(root / "cline"),
            "CRUSH_GLOBAL_DATA": str(root / "crush"), "OPENCODE_DB": "",
        })
        if automatic:
            env["AGREP_INDEXD_IDLE_S"] = "10"
        if not automatic:
            env["AGREP_NO_DAEMON"] = "1"
        return env

    def test_new_compactions_replace_an_existing_boundary_with_a_live_daemon(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        binary = repo / "target" / "release" / (
            "agrep-rs.exe" if os.name == "nt" else "agrep-rs")
        if not binary.is_file():
            self.skipTest("release ingest binary is required")
        with tempfile.TemporaryDirectory(prefix="agrep-new-boundary-") as raw:
            root = Path(raw)
            folder = root / "home/.omp/agent/sessions/project"
            folder.mkdir(parents=True)
            source = folder / "2026-04-05T06-07-08-000Z_file-id.jsonl"
            rows = [
                {"type": "session", "id": "header-id", "version": 3,
                 "cwd": "/work/fixture", "timestamp": "2026-04-05T06:07:08.000Z"},
                {"type": "message", "id": "m0", "parentId": None,
                 "timestamp": "2026-04-05T06:07:09.000Z",
                 "message": {"role": "user", "content": "obsolete window"}},
                {"type": "compaction", "id": "c0", "parentId": "m0",
                 "timestamp": "2026-04-05T06:07:10.000Z", "summary": "old recap"},
            ]
            source.write_text("".join(json.dumps(row) + "\n" for row in rows),
                              encoding="utf-8")
            cli = [sys.executable, str(repo / "cli.py")]
            built = subprocess.run(
                cli + ["index"], cwd=root,
                env=self._environment(root, binary, automatic=False),
                capture_output=True, text=True, timeout=30)
            self.assertEqual(built.returncode, 0, built.stderr)
            env = self._environment(root, binary, automatic=True)
            stop_flush = threading.Event()
            flush_thread = None
            try:
                for number, session in ((1, "file-id"), (2, "header-id")):
                    text = f"current-window-{number}"
                    added = [
                        {"type": "message", "id": f"m{number}",
                         "parentId": f"c{number - 1}",
                         "timestamp": f"2026-04-05T06:07:{10 + 2 * number}.000Z",
                         "message": {"role": "user", "content": text}},
                        {"type": "compaction", "id": f"c{number}",
                         "parentId": f"m{number}",
                         "timestamp": f"2026-04-05T06:07:{11 + 2 * number}.000Z",
                         "summary": f"recap-{number}"},
                    ]
                    body = "".join(json.dumps(row) + "\n" for row in added).encode()
                    expected_source = source.read_bytes() + body
                    if number == 1:
                        def delayed_flush():
                            requests = root / "data/.recovery_requests"
                            first = None
                            while not stop_flush.wait(0.005):
                                if first is None:
                                    first = next(requests.glob("*.json"), None)
                                elif not first.exists():
                                    with source.open("ab") as stream:
                                        stream.write(body)
                                    return

                        flush_thread = threading.Thread(target=delayed_flush)
                        flush_thread.start()
                    else:
                        with source.open("ab") as stream:
                            stream.write(body)
                    boundary_ms = int(datetime.fromisoformat(
                        added[-1]["timestamp"].replace("Z", "+00:00")).timestamp() * 1000)
                    result = subprocess.run(
                        cli + ["postcompact", "--session", session, "--json",
                               "--boundary-ms", str(boundary_ms)],
                        cwd=root, env=env, capture_output=True, text=True, timeout=20)
                    self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                    packet = json.loads(result.stdout)
                    self.assertEqual(packet["selection"]["boundary_turn"], 2 * number + 1)
                    self.assertEqual([row["text"] for row in packet["rows"]], [text])
                    self.assertEqual(source.read_bytes(), expected_source)
            finally:
                stop_flush.set()
                if flush_thread is not None:
                    flush_thread.join(timeout=1)
                stop = (
                    "import sys;sys.path.insert(0," + repr(str(repo / "py")) + ");"
                    "import indexd_runtime,semworker;"
                    "indexd_runtime.stop_indexd_owner();semworker.stop_worker_and_wait()")
                subprocess.run(
                    [sys.executable, "-I", "-c", stop], cwd=root, env=env,
                    capture_output=True, text=True, timeout=20)

    def test_published_packet_survives_an_exclusive_index_holder(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        binary = repo / "target" / "release" / (
            "agrep-rs.exe" if os.name == "nt" else "agrep-rs")
        if not binary.is_file():
            self.skipTest("release ingest binary is required")
        with tempfile.TemporaryDirectory(prefix="agrep-recovery-lock-") as raw:
            root = Path(raw)
            home, data = root / "home", root / "data"
            home.mkdir()
            data.mkdir()
            source = _db()
            try:
                source.executescript("""
                    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
                    INSERT INTO meta VALUES('family_stamp', 'fixture');
                    CREATE TABLE session_family(
                        session TEXT PRIMARY KEY, root TEXT NOT NULL, side INTEGER);
                    INSERT INTO session_family VALUES('root', 'root', 0);
                """)
                source.commit()
                with contextlib.closing(sqlite3.connect(data / "corpus.db")) as published:
                    source.backup(published)
            finally:
                source.close()
            (data / "messages.jsonl").write_text("", encoding="utf-8")
            env = self._environment(root, binary, automatic=False)
            lock_path = data / ".index.lock"
            with mock.patch.object(index_lock, "INDEX_LOCK_PATH", lock_path), \
                    index_lock.IndexLock("agrep-rs"):
                owner = lock_path.read_bytes()
                started = time.monotonic()
                result = subprocess.run(
                    [sys.executable, str(repo / "cli.py"), "postcompact",
                     "--session", "root", "--json"],
                    env=env, cwd=home, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=12, check=False)
                elapsed = time.monotonic() - started
                self.assertEqual(lock_path.read_bytes(), owner)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertLess(elapsed, 8.0)
            packet = json.loads(result.stdout)
            self.assertEqual(packet["status"], "partial")
            self.assertEqual(packet["selection"]["boundary_turn"], 8)
            self.assertEqual(
                {(row["turn"], row["who"]) for row in packet["rows"]},
                {(3, "user"), (3, "agent"), (7, "user"), (7, "agent")})


_UNSET = object()


class AbsenceEvidenceTests(unittest.TestCase):
    """Boundary absence requires current source evidence."""

    _DIGEST = "ab" * 32

    @classmethod
    def _record(cls, digests=_UNSET, signature="sig-1", ts=1_000.0):
        return postcompact.indexd_runtime._VerifiedRecord(
            ts, {"codex": (1, 999)},
            {"codex": cls._DIGEST} if digests is _UNSET else digests,
            signature)

    def _run_missing_boundary(self, argv, *, record, live_digest,
                              members=_UNSET):
        stdout, stderr = io.StringIO(), io.StringIO()
        if members is _UNSET:
            members = {"codex": ["/stores/codex/root.jsonl"]}
        with mock.patch.object(postcompact, "_REFRESH_WAIT_S", 0.0), \
                mock.patch.object(
                    postcompact.indexd_runtime, "ensure_index",
                    return_value=True), \
                mock.patch.object(
                    postcompact.indexd_runtime, "agent_freshness_notice",
                    return_value=None), \
                mock.patch.object(
                    postcompact.indexd_runtime, "_read_verified_record",
                    return_value=record), \
                mock.patch.object(
                    postcompact.indexd_runtime, "_store_paths_census",
                    return_value=members), \
                mock.patch.object(
                    postcompact.indexd_runtime, "_store_change_digest",
                    return_value=live_digest), \
                mock.patch.object(
                    postcompact.session_context, "calling_family_snapshot",
                    side_effect=lambda: CliTests._snapshot(None)), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            rc = postcompact.main(argv)
        return rc, stdout.getvalue(), stderr.getvalue()

    def test_covered_transcript_proves_boundary_absence(self) -> None:
        rc, stdout, stderr = \
            self._run_missing_boundary(
                ["--json"], record=self._record(), live_digest=self._DIGEST)
        self.assertEqual((rc, stderr), (2, ""))
        refusal = json.loads(stdout)
        self.assertEqual(refusal["status"], "boundary_unavailable")
        self.assertEqual(refusal["absence_proof"], "publication-covered")
        self.assertIn("no structural compaction boundary", refusal["reason"])

    def test_covered_evidence_keeps_the_user_facing_refusal(self) -> None:
        rc, stdout, stderr = self._run_missing_boundary(
            [], record=self._record(), live_digest=self._DIGEST)
        self.assertEqual((rc, stdout), (2, ""))
        self.assertIn("no structural compaction boundary", stderr)

    def test_grown_transcript_does_not_claim_verified_absence(self) -> None:
        rc, stdout, stderr = self._run_missing_boundary(
            ["--json"], record=self._record(), live_digest="cd" * 32)
        self.assertEqual(rc, 2)
        self.assertNotIn("absence_proof", json.loads(stdout))

    def test_missing_evidence_does_not_claim_verified_absence(self) -> None:
        rc, stdout, stderr = self._run_missing_boundary(
            ["--json"], record=None, live_digest=self._DIGEST)
        self.assertEqual(rc, 2)
        self.assertNotIn("absence_proof", json.loads(stdout))



class PublishedAbsenceProofTests(unittest.TestCase):
    """_published_absence_proof claims nothing unless the caller's transcript
    is a live member of a store whose verified-current recorded digest
    matches one recomputed from the members on disk now - the same
    monotone-coverage vouching the daemon's restamp branch commits."""

    _DIGEST = "ab" * 32

    def _proof(self, session="root", *, record=_UNSET,
               members=_UNSET, live_digest=_UNSET):
        record = (AbsenceEvidenceTests._record()
                  if record is _UNSET else record)
        if members is _UNSET:
            members = {"codex": ["/stores/codex/root.jsonl"]}
        if live_digest is _UNSET:
            live_digest = self._DIGEST
        with mock.patch.object(
                postcompact.indexd_runtime, "_read_verified_record",
                return_value=record), \
                mock.patch.object(
                    postcompact.indexd_runtime, "_store_paths_census",
                    return_value=members), \
                mock.patch.object(
                    postcompact.indexd_runtime, "_store_change_digest",
                    return_value=live_digest):
            return postcompact._published_absence_proof(session)

    def test_covered_transcript_yields_the_proof(self) -> None:
        self.assertEqual(self._proof(), "publication-covered")

    def test_no_session_claims_nothing(self) -> None:
        self.assertIsNone(self._proof(""))

    def test_missing_record_claims_nothing(self) -> None:
        self.assertIsNone(self._proof(record=None))

    def test_record_without_digests_claims_nothing(self) -> None:
        self.assertIsNone(
            self._proof(record=AbsenceEvidenceTests._record(digests={})))

    def test_record_trailing_the_live_generation_still_vouches(self) -> None:
        # Busy-box shape: publications land every few seconds while the
        # rate-limited restamp trails and pins an older ingest signature;
        # vouching is the per-store digest match, never the global pin.
        for signature in ("sig-0", None):
            record = AbsenceEvidenceTests._record(signature=signature)
            self.assertEqual(
                self._proof(record=record), "publication-covered")

    def test_future_dated_record_claims_nothing(self) -> None:
        record = AbsenceEvidenceTests._record(ts=time.time() + 10_000.0)
        self.assertIsNone(self._proof(record=record))

    def test_unavailable_member_listing_claims_nothing(self) -> None:
        self.assertIsNone(self._proof(members=None))

    def test_unlisted_transcript_claims_nothing(self) -> None:
        self.assertIsNone(
            self._proof(members={"codex": ["/stores/codex/other.jsonl"]}))

    def test_host_store_without_recorded_digest_claims_nothing(self) -> None:
        # The transcript's store is live but the record never digested it:
        # the publication evidence says nothing about those bytes.
        record = AbsenceEvidenceTests._record(digests={"claude": self._DIGEST})
        self.assertIsNone(self._proof(record=record))

    def test_moved_member_identity_claims_nothing(self) -> None:
        self.assertIsNone(self._proof(live_digest="cd" * 32))

    def test_undigestable_store_claims_nothing(self) -> None:
        self.assertIsNone(self._proof(live_digest=None))

    def test_every_matching_store_must_be_covered(self) -> None:
        # A same-named transcript in a second store: whichever one is the
        # caller's real file must be covered, so both must be.
        members = {"codex": ["/stores/codex/root.jsonl"],
                   "claude": ["/stores/claude/root.jsonl"]}
        self.assertIsNone(self._proof(members=members))
        record = AbsenceEvidenceTests._record(
            digests={"codex": self._DIGEST, "claude": self._DIGEST})
        self.assertEqual(
            self._proof(members=members, record=record),
            "publication-covered")


if __name__ == "__main__":
    unittest.main()
