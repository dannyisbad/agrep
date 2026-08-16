"""Deterministic continuation packets stay root-only, recent, and bounded."""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import unittest
from unittest import mock

from _test_support import isolate_data_dir

isolate_data_dir()

import postcompact  # noqa: E402
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
                        return_value=("root",
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
                        return_value=("root",
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
                        return_value=("root",
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
        """A session indexed before its compaction landed has a family but
        no recap turn (the omp report): every scheduled retry re-ingests
        without sleeping the suite, then the refusal names the session,
        never "this caller"."""
        opened = []

        def _fresh_db():
            opened.append(_db())
            return opened[-1]

        ingested = []
        try:
            with mock.patch.object(
                    postcompact, "_RETRY_PAUSES_S", (0.0, 0.0)), \
                    mock.patch.object(
                        postcompact.indexd_runtime, "ensure_index",
                        return_value=True), \
                    mock.patch.object(
                        postcompact.indexd_runtime, "build_index",
                        side_effect=lambda quiet: ingested.append(True)), \
                    mock.patch.object(
                        postcompact.common, "ingest_bin",
                        return_value=mock.Mock(exists=lambda: True)), \
                    mock.patch.object(
                        postcompact.session_context,
                        "_open_session_family_index",
                        side_effect=_fresh_db), \
                    mock.patch.object(
                        postcompact.session_context,
                        "_indexed_calling_family_state_in_db",
                        return_value=("root", frozenset({"root"}), None)):
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), \
                        contextlib.redirect_stderr(stderr):
                    rc = postcompact.main(["--session", "root"])
        finally:
            for db in opened:
                db.close()
        self.assertEqual(rc, 2)
        self.assertEqual(ingested, [True, True])
        self.assertIn("session root", stderr.getvalue())
        self.assertNotIn("this caller", stderr.getvalue())

    def test_boundary_landing_during_the_retry_window_recovers(self) -> None:
        # The observed race: the hook fires before the freshly-compacted
        # session flushes its first store write. The boundary appears between
        # attempts; the bounded retry serves it instead of refusing.
        opened = []

        def _fresh_db():
            opened.append(_db())
            return opened[-1]

        states = [None, None, ("root", frozenset({"root", "child"}), 8)]
        try:
            with mock.patch.object(
                    postcompact, "_RETRY_PAUSES_S", (0.0, 0.0)), \
                    mock.patch.object(
                        postcompact.indexd_runtime, "ensure_index",
                        return_value=True), \
                    mock.patch.object(
                        postcompact.indexd_runtime, "agent_freshness_notice",
                        return_value=None), \
                    mock.patch.object(
                        postcompact.indexd_runtime, "build_index",
                        side_effect=lambda quiet: True), \
                    mock.patch.object(
                        postcompact.common, "ingest_bin",
                        return_value=mock.Mock(exists=lambda: True)), \
                    mock.patch.object(
                        postcompact.session_context,
                        "_open_session_family_index",
                        side_effect=_fresh_db), \
                    mock.patch.object(
                        postcompact.session_context,
                        "_indexed_calling_family_state_in_db",
                        side_effect=lambda db, session: states.pop(0)):
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

    def test_explicit_session_freshness_story_degrades_to_a_partial_packet(
            self) -> None:
        """A live freshness story is staleness-shaped first (each retry
        re-ingests), then disclosure: the proven boundary still serves as an
        explicitly partial packet carrying the story, never a confident
        'recovered' and never a refusal (the omp report: the compacting
        session's own churn kept the index "behind" at exactly the moment
        the packet exists for)."""
        opened = []

        def _fresh_db():
            opened.append(_db())
            return opened[-1]

        ingested = []
        try:
            with mock.patch.object(
                    postcompact, "_RETRY_PAUSES_S", (0.0,)), \
                    mock.patch.object(
                        postcompact.indexd_runtime, "ensure_index",
                        return_value=True), \
                    mock.patch.object(
                        postcompact.indexd_runtime, "build_index",
                        side_effect=lambda quiet: ingested.append(True)), \
                    mock.patch.object(
                        postcompact.common, "ingest_bin",
                        return_value=mock.Mock(exists=lambda: True)), \
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
                        return_value=("root",
                                      frozenset({"root", "child"}), 8)):
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), \
                        contextlib.redirect_stderr(stderr):
                    rc = postcompact.main(["--session", "root", "--json"])
        finally:
            for db in opened:
                db.close()
        self.assertEqual(rc, 2)
        self.assertEqual(ingested, [True])
        packet = json.loads(stdout.getvalue())
        self.assertEqual(packet["status"], "partial")
        self.assertEqual(
            packet["coverage"]["index_freshness"], "index is tearing down")
        self.assertEqual(stderr.getvalue(), "")

    def test_unresolvable_generation_serves_the_last_published_snapshot(
            self) -> None:
        """A continuously writing conversation can starve the strict
        generation-stable open forever (the second omp report): the final
        attempt serves the boundary from the last published snapshot as an
        explicitly partial packet naming the churn."""
        opened = []

        def _fresh_db():
            opened.append(_db())
            return opened[-1]

        try:
            with mock.patch.object(
                    postcompact, "_RETRY_PAUSES_S", (0.0,)), \
                    mock.patch.object(
                        postcompact.indexd_runtime, "ensure_index",
                        return_value=True), \
                    mock.patch.object(
                        postcompact.indexd_runtime, "build_index",
                        side_effect=lambda quiet: True), \
                    mock.patch.object(
                        postcompact.common, "ingest_bin",
                        return_value=mock.Mock(exists=lambda: True)), \
                    mock.patch.object(
                        postcompact.session_context,
                        "_open_session_family_index",
                        return_value=None), \
                    mock.patch.object(
                        postcompact, "_lenient_family_snapshot",
                        side_effect=_fresh_db), \
                    mock.patch.object(
                        postcompact.session_context,
                        "_indexed_calling_family_state_in_db",
                        return_value=("root",
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
            packet["coverage"]["index_freshness"],
            postcompact._FAMILY_CHURN_NOTICE)
        self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
