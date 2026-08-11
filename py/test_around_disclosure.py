"""`agrep around --json` stdout stands alone, and handles never recenter.

A caller that captured stdout - the normal pipe idiom - used to receive the
clamped turn believing it was the turn it asked for. Every divergence between
the requested window and the served one now rides in the stream the machine
reads, rendered from the same record as the stderr line a human reads. The
The handle path uses its digest to break a prefix tie and otherwise falls back
to the same candidate list and next step. A handle outside the stored turn
range refuses instead of silently serving the positional form's clamped row.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "py"))

from _test_support import isolate_data_dir  # noqa: E402

isolate_data_dir()
import around  # noqa: E402
import common  # noqa: E402
import compact  # noqa: E402
import explore  # noqa: E402

TWINS = ("agent-ab4360000000000000a", "agent-ab43e0000000000000b")
RENUMBERED = "0199cccc-2222-4000-8000-00000000000c"
SESSIONS = (*TWINS, RENUMBERED)
LAST = 754
TEXT = "the login race condition came back"
DIGEST = compact.content_digest(TEXT)


def _holds(session: str, turn: int) -> bool:
    # the first twin holds the cited content throughout; the renumbered
    # session moved it to turn 7
    return session == TWINS[0] or (session == RENUMBERED and turn == 7)


def _window(session: str, center: int, radius: int) -> dict:
    center = max(0, min(LAST, int(center)))
    radius = max(0, int(radius))
    turns = [{"turn": turn, "ts": 0, "who": "user",
              "text": TEXT if _holds(session, turn) else "unrelated",
              "reply": "r"}
             for turn in range(max(0, center - radius), min(LAST, center + radius) + 1)]
    return {"session": session, "agent": "claude", "project": "p", "concept": "",
            "title": "", "center": center, "first_turn": 0, "last_turn": LAST,
            "turns": turns, "events": []}


class AroundDisclosureTests(unittest.TestCase):
    def setUp(self) -> None:
        around.indexd_runtime._clear_freshen_failure()
        self.addCleanup(around.indexd_runtime._clear_freshen_failure)
        # Restore exact shared-fixture bytes and timestamps for later suites.
        path = common.MESSAGES_PATH
        existed = path.exists()
        prior = path.read_bytes() if existed else None
        prior_times = None
        if existed:
            stat = path.stat()
            prior_times = (stat.st_atime_ns, stat.st_mtime_ns)
        path.touch()
        self.addCleanup(explore._freshen)
        self.addCleanup(
            lambda: (
                path.write_bytes(prior),
                os.utime(path, ns=prior_times),
            ) if existed else path.unlink(missing_ok=True))
        patches = (
            mock.patch.object(around.indexd_runtime, "ensure_index",
                              lambda auto=True, **_kw: True),
            mock.patch.object(explore, "resolve_session",
                              lambda q: [s for s in SESSIONS if s.startswith(q)]),
            mock.patch.object(explore, "get_window", _window),
            mock.patch.object(explore, "get_windows",
                              lambda reqs: [_window(*req) for req in reqs]),
            mock.patch.object(explore, "_session_index",
                              lambda: {s: {} for s in SESSIONS}),
            mock.patch.object(common, "indexed_session_prefix_candidates",
                              lambda sessions: tuple(TWINS)),
            mock.patch.object(
                around.session_context, "indexed_family_roots",
                lambda sessions: {str(session): str(session)
                                  for session in sessions}),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = around.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def _divergences(self, stdout: str) -> list[dict]:
        rows = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        meta = [row for row in rows if row["kind"] == "agrep-meta" and "served" in row]
        self.assertLessEqual(len(meta), 1, "one served record per response")
        if not meta:
            return []
        served = meta[0]["served"]
        self.assertIs(served["served_as_requested"], False)
        self.assertIs(rows[0], meta[0] if rows else None,
                      "the disclosure precedes the rows it qualifies")
        return served["divergences"]

    def test_help_documents_hit_miss_and_error_exit_codes(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit) as raised:
            around.main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn(
            "exit: 0 shown, 1 no selected messages/events, 2 bad target / no index.",
            out.getvalue())
        self.assertEqual(err.getvalue(), "")

    def test_clamped_turn_is_in_the_stream_a_machine_reads(self) -> None:
        rc, out, err = self._run([TWINS[0], "999999"])
        self.assertEqual(rc, 0)
        self.assertIn(f"centered on {LAST}", err)

        rc, out, _ = self._run([TWINS[0], "999999", "--json"])
        self.assertEqual(rc, 0)
        clamp, = [d for d in self._divergences(out) if d["kind"] == "turn_clamped"]
        self.assertEqual((clamp["requested"], clamp["served"]), (999999, LAST))
        self.assertEqual((clamp["first_turn"], clamp["last_turn"]), (0, LAST))
        served = {row["turn"] for row in
                  (json.loads(line) for line in out.splitlines())
                  if row["kind"] == "msg"}
        self.assertEqual(max(served), LAST,
                         "the rows are the clamped window; the meta says so")

    def test_negative_turn_clamps_and_discloses(self) -> None:
        _rc, out, _err = self._run([TWINS[0], "-5", "--json", "-C", "0"])
        clamp, = self._divergences(out)
        self.assertEqual((clamp["kind"], clamp["requested"], clamp["served"]),
                         ("turn_clamped", -5, 0))

    def test_result_handle_never_clamps_and_failure_is_one_line(self) -> None:
        with mock.patch.object(
                around.indexd_runtime, "agent_freshness_notice",
                side_effect=AssertionError("selection failure queried freshness")):
            rc, out, err = self._run([f"@{TWINS[0]}:999999"])
        self.assertEqual((rc, out), (2, ""))
        self.assertEqual(
            err, f"result handle is stale: turn 999999 is out of range "
                 f"(chat has turns 0-{LAST}); "
                 "rerun the search for a current handle\n")

        with mock.patch.object(
                around.indexd_runtime, "machine_freshness",
                side_effect=AssertionError("selection failure queried freshness")):
            rc, out, err = self._run([f"@{TWINS[0]}:999999", "--json"])
        self.assertEqual(rc, 2)
        error = self._error_meta(out)
        self.assertEqual(error["code"], "stale-handle")
        self.assertEqual(error["requested_turn"], 999999)
        self.assertEqual(err, "")

    def test_every_divergence_renders_one_decision(self) -> None:
        """The stderr sentence and the JSON field are the same string, so a
        rewording cannot leave the two surfaces disagreeing."""
        for argv in ([TWINS[0], "999999"], [f"@{TWINS[0]}:5"]):
            _rc, _out, err = self._run(argv)
            _rc, out, _err = self._run([*argv, "--json"])
            lines = [d["note"] for d in self._divergences(out)]
            self.assertTrue(lines, f"{argv} disclosed nothing")
            self.assertEqual(lines, [ln for ln in err.splitlines() if ln in lines])
            for line in lines:
                self.assertIn(line, err)

    def test_ambiguous_handle_with_digest_resolves_by_content(self) -> None:
        rc, out, err = self._run([f"@agent-ab43:5.{DIGEST}", "--json"])
        self.assertEqual(rc, 0, err)
        pick, = [d for d in self._divergences(out)
                 if d["kind"] == "handle_disambiguated"]
        self.assertEqual((pick["candidates"], pick["session"]), (2, TWINS[0]))
        self.assertEqual({json.loads(line).get("session")
                          for line in out.splitlines()} - {None}, {TWINS[0]})

    def test_digest_handle_survives_a_dropped_at_sigil(self) -> None:
        rc, out, err = self._run([f"{TWINS[0]}:5.{DIGEST}", "--json"])
        self.assertEqual(rc, 0, err)
        rows = [json.loads(line) for line in out.splitlines() if line]
        sessions = {row.get("session") for row in rows}
        self.assertEqual(sessions - {None}, {TWINS[0]})

    def test_ambiguous_handle_without_digest_offers_the_positional_next_step(
            self) -> None:
        rc_h, _out, err_h = self._run(["@agent-ab43:5"])
        rc_p, _out, err_p = self._run(["agent-ab43", "5"])
        self.assertEqual((rc_h, rc_p), (2, 2))
        for session in TWINS:
            self.assertIn(session, err_h)
        self.assertIn("add a char: agent-ab436 / agent-ab43e", err_h)
        self.assertEqual(err_h.splitlines(), err_p.splitlines(),
                         "the path with more information must not recover less")

    def test_ambiguity_candidates_exclude_parent_only_family_ids(self) -> None:
        parent = "agent-ab431-parent-only"
        with mock.patch.object(explore, "_freshen"), \
                mock.patch.object(
                    explore.common, "indexed_session_matches",
                    return_value=[parent, *TWINS]), \
                mock.patch.object(
                    explore, "_session_index",
                    return_value={session: {} for session in TWINS}):
            self.assertEqual(explore.resolve_session("agent-ab43"), list(TWINS))

    def test_moved_digest_content_is_disclosed_to_the_pipe(self) -> None:
        rc, out, err = self._run([f"@{RENUMBERED}:5.{DIGEST}", "--json"])
        self.assertEqual(rc, 0, err)
        moved, = [d for d in self._divergences(out) if d["kind"] == "content_moved"]
        self.assertEqual((moved["requested"], moved["served"]), (5, 7))
        self.assertEqual({json.loads(line)["turn"] for line in out.splitlines()
                          if json.loads(line)["kind"] == "msg"}, {7})

    def _error_meta(self, stdout: str) -> dict:
        rows = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        meta = [row for row in rows
                if row.get("kind") == "agrep-meta" and "error" in row]
        self.assertEqual(len(meta), 1, "one error record per failed response")
        return meta[0]["error"]

    def test_json_failures_emit_an_error_record_on_stdout(self) -> None:
        cases = (
            (["zzzz9999", "5", "--json"], "no-session"),
            (["agent-ab43", "5", "--json"], "ambiguous-session"),
            ([f"@{TWINS[0]}:999999", "--json"], "stale-handle"),
        )
        for argv, code in cases:
            with self.subTest(code=code):
                rc, out, err = self._run(argv)
                self.assertEqual(rc, 2)
                error = self._error_meta(out)
                self.assertEqual(error["code"], code)
                self.assertEqual(err, "")

    def test_json_bad_target_answers_in_json_before_exit(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit) as raised:
            around.main([TWINS[0], "not-a-turn", "--json"])
        self.assertEqual(raised.exception.code, 2)
        error = self._error_meta(out.getvalue())
        self.assertEqual(error["code"], "bad-target")
        self.assertEqual(err.getvalue(), "")

    def test_malformed_digest_is_an_invalid_result_handle(self) -> None:
        malformed = f"@{TWINS[0]}:5.{DIGEST[:3]}"
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit) as raised:
            around.main([malformed, "--json"])
        self.assertEqual(raised.exception.code, 2)
        error = self._error_meta(out.getvalue())
        self.assertEqual(error["code"], "bad-target")
        self.assertEqual(error["reason"], "invalid result handle")
        self.assertEqual(err.getvalue(), "")

    def test_overlong_session_is_rejected_before_resolution(self) -> None:
        target = "@" + "x" * (compact.SESSION_ID_MAX_BYTES + 1)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(
                explore, "resolve_session",
                side_effect=AssertionError("invalid target reached session index")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit) as raised:
            around.main([target, "--json"])
        self.assertEqual(raised.exception.code, 2)
        error = self._error_meta(out.getvalue())
        self.assertEqual(error["code"], "bad-target")
        self.assertIn("4096 UTF-8 bytes", error["reason"])
        self.assertEqual(err.getvalue(), "")

    def test_missing_handle_is_one_actionable_selection_failure(self) -> None:
        with mock.patch.object(
                around.indexd_runtime, "agent_freshness_notice",
                side_effect=AssertionError("selection failure queried freshness")):
            rc, out, err = self._run(["@zzzz9999:5"])
        self.assertEqual((rc, out), (2, ""))
        self.assertEqual(len(err.splitlines()), 1)
        self.assertIn("rerun the search", err)
        self.assertNotIn("ids come from", err)

    def test_missing_cited_content_has_no_unrelated_freshness_tail(self) -> None:
        occupied = {compact.content_digest(text)
                    for text in (TEXT, "unrelated", "r")}
        absent = next(f"{value:04x}" for value in range(65536)
                      if f"{value:04x}" not in occupied)
        with mock.patch.object(
                around.indexd_runtime, "agent_freshness_notice",
                side_effect=AssertionError("selection failure queried freshness")):
            rc, out, err = self._run([f"@{RENUMBERED}:5.{absent}"])
        self.assertEqual((rc, out), (2, ""))
        self.assertEqual(
            err, "result handle no longer names the cited content; "
                 "rerun the search for a current handle\n")

    def test_who_miss_is_scoped_and_never_renders_empty_headers(self) -> None:
        argv = [TWINS[0], "5", "-C", "2", "--who", "harness"]
        reason = (
            "no harness messages or events in turns 3-7 of agent-ab436")
        with mock.patch.object(
                around.indexd_runtime, "agent_freshness_notice",
                side_effect=AssertionError("selection failure queried freshness")):
            rc, out, err = self._run(argv)
        self.assertEqual((rc, out, err), (1, "", f"{reason}\n"))

        with mock.patch.object(
                around.indexd_runtime, "machine_freshness",
                side_effect=AssertionError("selection failure queried freshness")):
            rc, out, err = self._run([*argv, "--json"])
        self.assertEqual((rc, err), (1, ""))
        self.assertEqual(out.count("\n"), 1)
        self.assertEqual(out.count(reason), 1)
        self.assertEqual(json.loads(out), {
            "kind": "agrep-meta",
            "miss": {
                "code": "no-speaker-match",
                "reason": reason,
                "scope": {
                    "session": TWINS[0], "who": "harness",
                    "first_turn": 3, "last_turn": 7,
                },
            },
        })

    def test_who_hit_renders_only_turns_with_selected_content(self) -> None:
        def one_harness_turn(session: str, center: int, radius: int) -> dict:
            window = _window(session, center, radius)
            for turn in window["turns"]:
                turn["who"] = "harness" if turn["turn"] == center else "user"
                turn["reply"] = ""
            return window

        with mock.patch.object(explore, "get_window", one_harness_turn):
            rc, out, err = self._run([
                TWINS[0], "5", "-C", "2", "--who", "harness"])
        self.assertEqual(rc, 0, err)
        headers = [line for line in out.splitlines()
                   if line.startswith("── turn ")]
        self.assertEqual(len(headers), 1)
        self.assertIn("── turn 5 ", headers[0])
        self.assertIn("harness:", out)

    def test_no_auto_json_hit_discloses_unchecked_freshness(self) -> None:
        with mock.patch.object(
                around.indexd_runtime, "_drift_report",
                side_effect=AssertionError("--no-auto ran a source census")):
            rc, out, err = self._run([
                TWINS[0], "5", "-C", "0", "--json", "--no-auto"])
        self.assertEqual((rc, err), (0, ""))
        rows = [json.loads(line) for line in out.splitlines()]
        freshness, = [row["freshness"] for row in rows
                      if row.get("kind") == "agrep-meta"
                      and "freshness" in row]
        self.assertEqual(freshness["state"], "unchecked")
        self.assertFalse(freshness["checked"])
        self.assertTrue(freshness["may_be_stale"])

        with mock.patch.object(
                around.indexd_runtime, "agent_freshness_notice",
                side_effect=AssertionError("--no-auto queried freshness")):
            rc, out, err = self._run([
                TWINS[0], "5", "-C", "0", "--no-auto", "--color", "never"])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("user:", out)

    def test_no_auto_scoped_miss_is_unverified_once(self) -> None:
        argv = [TWINS[0], "5", "-C", "0", "--who", "harness", "--no-auto"]
        rc, out, err = self._run([*argv, "--json"])
        self.assertEqual((rc, err), (2, ""))
        row = json.loads(out)
        self.assertEqual(row["miss"]["code"], "no-speaker-match")
        self.assertEqual(row["freshness"]["state"], "unchecked")
        self.assertEqual(out.count(row["miss"]["reason"]), 1)

        rc, out, err = self._run(argv)
        self.assertEqual((rc, out), (2, ""))
        self.assertEqual(len(err.splitlines()), 1)
        self.assertIn("automatic freshness checks are disabled", err)

    def test_bare_printed_session_opens_its_latest_indexed_turn(self) -> None:
        rc, out, err = self._run([f"@{TWINS[0]}", "-C", "0", "--json"])
        self.assertEqual((rc, err), (0, ""))
        rows = [json.loads(line) for line in out.splitlines()]
        self.assertEqual(rows[0]["scope"]["selection_order"], "newest_tail")
        self.assertEqual(rows[0]["scope"]["render_order"], "chronological")
        self.assertEqual({row["turn"] for row in rows if row["kind"] == "msg"},
                         {LAST})
        self.assertFalse(any("served" in row for row in rows))

        rc, human, err = self._run([
            f"@{TWINS[0]}", "-C", "0", "--color", "never"])
        self.assertEqual(rc, 0, err)
        self.assertIn("newest tail selected; chronological render", human)

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit) as raised:
            around.main([TWINS[0]])
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("need a turn", err.getvalue())

    def test_who_filter_treats_events_as_tool_rows(self) -> None:
        def one_tool_turn(session: str, center: int, radius: int) -> dict:
            window = _window(session, center, radius)
            for turn in window["turns"]:
                turn["text"] = ""
                turn["reply"] = ""
            window["events"] = [{
                "kind": "tool", "turn": center, "ts": 0,
                "name": "fixture_tool", "input": "{}", "output": "",
                "ok": True, "output_chars": 0,
            }]
            return window

        with mock.patch.object(explore, "get_window", one_tool_turn):
            rc, out, err = self._run([
                TWINS[0], "5", "-C", "2", "--who", "harness"])
            self.assertEqual((rc, out), (1, ""))
            self.assertIn("no harness messages or events", err)

            rc, out, err = self._run([
                TWINS[0], "5", "-C", "2", "--who", "tool"])
            self.assertEqual(rc, 0, err)
            self.assertEqual(out.count("── turn "), 1)
            self.assertIn("fixture_tool", out)

    def test_selected_tool_hit_never_collapses_and_centers_its_preview(
            self) -> None:
        output = (
            "routine preface " * 15
            + "RUN_SELECTED_NEEDLE exact decisive evidence"
            + " routine trailer" * 15
        )
        events = []
        for index in range(7):
            event_output = output if index == 4 else f"routine success {index}"
            events.append({
                "kind": "tool", "turn": 5, "ts": index,
                "name": "exec_command", "input": f"step-{index}",
                "output": event_output, "ok": True,
                "output_chars": len(event_output),
                "output_bytes": len(event_output.encode("utf-8")),
            })
        selected = events[4]
        searchable = common.tool_search_text(selected)
        start = searchable.index("RUN_SELECTED_NEEDLE")
        event_identity = common.tool_event_identity(
            TWINS[0], 5, selected["ts"], searchable)
        self.assertIsNotNone(event_identity)
        handle = (
            f"@{TWINS[0]}:5.{compact.content_digest(searchable)}"
            f"~{event_identity}:{start}-"
            f"{start + len('RUN_SELECTED_NEEDLE')}"
        )

        def selected_window(session: str, center: int, radius: int) -> dict:
            window = _window(session, center, radius)
            window["events"] = events
            return window

        with mock.patch.object(explore, "get_window", selected_window):
            rc, out, err = self._run([handle, "--color", "never"])
        self.assertEqual(rc, 0, err)
        self.assertIn("RUN_SELECTED_NEEDLE", out)
        self.assertNotIn("6 tool calls", out)
        self.assertNotIn("7 tool calls", out)
        self.assertEqual(out.count("exec_command step-4"), 1)
        self.assertIn("6 unselected tool/workflow events hidden", out)

    def test_digest_root_prose_handle_hides_generic_events_but_keeps_pair(
            self) -> None:
        event = {
            "turn": 5, "ts": 50, "kind": "tool", "name": "exec_command",
            "input": "unselected command", "output": "UNSELECTED_OUTPUT",
            "ok": True, "input_chars": 18, "output_chars": 17,
            "output_bytes": 17, "input_truncated": False,
            "output_truncated": False,
        }

        def selected_window(session: str, center: int, radius: int) -> dict:
            window = _window(session, center, radius)
            window["events"] = [event]
            return window

        handle = f"@{TWINS[0]}:5.{DIGEST}"
        with mock.patch.object(explore, "get_window", selected_window):
            rc, out, err = self._run([handle, "--color", "never"])
        self.assertEqual(rc, 0, err)
        self.assertIn(TEXT, out)
        self.assertIn("agent: r", out)
        self.assertNotIn("UNSELECTED_OUTPUT", out)
        self.assertIn("1 unselected tool/workflow events hidden", out)

        with mock.patch.object(explore, "get_window", selected_window):
            rc, payload, err = self._run([handle, "--json"])
        self.assertEqual(rc, 0, err)
        meta = json.loads(payload.splitlines()[0])
        self.assertEqual(
            meta["scope"]["selected_record_role"], "prose_turn_inclusive")
        self.assertEqual(meta["scope"]["tools"]["hidden"], 1)
        self.assertEqual(
            meta["scope"]["expansion_argv"][-3:], ["-C", "0", "--full"])

    def test_unverified_handle_role_falls_back_to_inclusive_exact_turn(
            self) -> None:
        event = {
            "turn": 5, "ts": 50, "kind": "tool", "name": "exec_command",
            "input": "role proof unavailable", "output": "PRESERVED_EVENT",
            "ok": True, "input_chars": 22, "output_chars": 15,
            "output_bytes": 15, "input_truncated": False,
            "output_truncated": False,
        }

        def selected_window(session: str, center: int, radius: int) -> dict:
            window = _window(session, center, radius)
            window["events"] = [event]
            return window

        handle = f"@{TWINS[0]}:5.{DIGEST}"
        with mock.patch.object(explore, "get_window", selected_window), \
                mock.patch.object(
                    around.session_context, "indexed_family_roots",
                    return_value=None):
            rc, out, err = self._run([handle, "--color", "never"])
        self.assertEqual(rc, 0, err)
        self.assertIn("PRESERVED_EVENT", out)
        self.assertIn("selected session role unverified", out)

    def test_positional_read_hides_generic_events_until_explicit(self) -> None:
        events = [{
            "turn": 5, "ts": index, "kind": "tool", "name": "exec_command",
            "input": f"step {index}", "output": f"result {index}",
            "ok": True, "input_chars": 6, "output_chars": 8,
            "output_bytes": 8, "input_truncated": False,
            "output_truncated": False,
        } for index in range(8)]

        def selected_window(session: str, center: int, radius: int) -> dict:
            window = _window(session, center, radius)
            window["events"] = events
            return window

        with mock.patch.object(explore, "get_window", selected_window):
            rc, out, err = self._run([
                TWINS[0], "5", "-C", "0", "--color", "never"])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("tool calls", out)
        self.assertNotIn("exec_command", out)
        self.assertIn("8 unselected tool/workflow events hidden", out)
        self.assertIn("root/main prose default", out)

        with mock.patch.object(explore, "get_window", selected_window):
            rc, out, err = self._run([
                TWINS[0], "5", "-C", "0", "--who", "tool",
                "--color", "never"])
        self.assertEqual(rc, 0, err)
        self.assertIn("tool calls", out)
        self.assertIn("exec_command", out)

    def test_digest_child_handle_keeps_the_exact_turn_conversation(self) -> None:
        handle = f"@{TWINS[0]}:5.{compact.content_digest('r')}"
        with mock.patch.object(
                around.session_context, "indexed_family_roots",
                return_value={TWINS[0]: "root-session"}):
            rc, out, err = self._run([handle, "--color", "never"])
        self.assertEqual(rc, 0, err)
        self.assertIn(TEXT, out)
        self.assertIn("agent: r", out)
        self.assertIn("selected delegated session", out)
        self.assertIn("root adoption unresolved", out)

    def test_selected_child_keeps_full_prose_instead_of_a_tiny_capsule(
            self) -> None:
        reply = "HEAD_SETUP-" + "x" * 5_000 + "-TAIL_DECISION"
        handle = f"@{TWINS[0]}:5.{compact.content_digest(reply)}"

        def long_child_window(session: str, center: int, radius: int) -> dict:
            window = _window(session, center, radius)
            window["turns"][0]["reply"] = reply
            return window

        with mock.patch.object(explore, "get_window", long_child_window), \
                mock.patch.object(
                    around.session_context, "indexed_family_roots",
                    return_value={TWINS[0]: "root-session"}):
            rc, out, err = self._run([handle, "--color", "never"])
        self.assertEqual(rc, 0, err)
        self.assertIn("HEAD_SETUP", out)
        self.assertIn("TAIL_DECISION", out)
        self.assertNotIn("chars omitted", out)
        self.assertIn("selected turn prose uncapped", out)
        self.assertGreater(len(out), 5_000)

        with mock.patch.object(explore, "get_window", long_child_window), \
                mock.patch.object(
                    around.session_context, "indexed_family_roots",
                    return_value={TWINS[0]: "root-session"}):
            rc, payload, err = self._run([handle, "--json"])
        self.assertEqual(rc, 0, err)
        meta = json.loads(payload.splitlines()[0])
        self.assertEqual(
            meta["scope"]["truncation"]["message_cap_state"],
            "selected_delegated_exact_turn_uncapped",
        )

    def test_selected_child_honors_an_explicit_message_cap(self) -> None:
        reply = "HEAD_SETUP-" + "x" * 5_000 + "-TAIL_DECISION"
        handle = f"@{TWINS[0]}:5.{compact.content_digest(reply)}"

        def long_child_window(session: str, center: int, radius: int) -> dict:
            window = _window(session, center, radius)
            window["turns"][0]["reply"] = reply
            return window

        with mock.patch.object(explore, "get_window", long_child_window), \
                mock.patch.object(
                    around.session_context, "indexed_family_roots",
                    return_value={TWINS[0]: "root-session"}):
            rc, out, err = self._run([
                handle, "--max-chars", "1200", "--color", "never"])
        self.assertEqual(rc, 0, err)
        self.assertIn("HEAD_SETUP", out)
        self.assertNotIn("TAIL_DECISION", out)
        self.assertIn("chars - agrep around", out)
        self.assertNotIn("selected turn prose uncapped", out)

    def test_json_window_error_answers_in_json(self) -> None:
        with mock.patch.object(explore, "get_window",
                               lambda *a: {"error": "window fell over"}):
            rc, out, err = self._run([TWINS[0], "5", "--json"])
        self.assertEqual(rc, 2)
        error = self._error_meta(out)
        self.assertEqual(error["code"], "window-unavailable")
        self.assertEqual(error["reason"], "window fell over")
        self.assertEqual(err, "")

    def test_json_missing_index_answers_in_json(self) -> None:
        with mock.patch.object(around.indexd_runtime, "ensure_index",
                               lambda auto=True, **_kw: False), \
                mock.patch.object(common, "MESSAGES_PATH",
                                  Path("around-no-such-index.jsonl")):
            rc, out, err = self._run([TWINS[0], "5", "--json"])
        self.assertEqual(rc, 2)
        error = self._error_meta(out)
        self.assertEqual(error["code"], "index-missing")
        self.assertEqual(error["remedy"], "agrep index")
        self.assertEqual(err, "")

    def test_human_failures_keep_stdout_clean(self) -> None:
        rc, out, err = self._run(["zzzz9999", "5"])
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("no session matches", err)

    def test_unambiguous_request_discloses_nothing(self) -> None:
        rc, out, err = self._run([TWINS[0], "5", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(self._divergences(out), [])
        self.assertEqual(err, "")


if __name__ == "__main__":
    unittest.main()
