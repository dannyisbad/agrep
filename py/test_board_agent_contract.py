"""Agent-facing live Board selection and completeness contracts."""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "py"))

from _test_support import isolate_data_dir  # noqa: E402

isolate_data_dir()
import explore  # noqa: E402
import livetui  # noqa: E402


TAUGHT_BOARD_ARGS = (
    "board", "--once", "--json", "--sort", "updated", "--agent", "claude",
    "--state", "active", "--roots", "-n", "3",
)


def _row(
        session: str, updated: int, *, agent: str = "claude",
        parent: str = "", working: bool = False, active: bool = True,
        project: str = "agrep",
) -> dict:
    return {
        "active": active,
        "agent": agent,
        "last_ts": updated,
        "parent": parent or None,
        "project": project,
        "recent": [],
        "session": session,
        "state": "thinking" if working else "done",
        "sub": bool(parent),
        "title": f"task {session}",
        "working": working,
    }


def _snapshot(*rows: dict, booting: bool = False) -> dict:
    return {
        "booting": booting,
        "degraded_sources": [],
        "last_err": "",
        "now": 10_000,
        "sessions": list(rows),
        "window_s": 90,
    }


class BoardSelectionTests(unittest.TestCase):
    def test_updated_roots_use_latest_family_activity(self) -> None:
        rows = livetui._visible(
            _snapshot(
                _row("root-old", 100),
                _row("child-new", 900, parent="root-old", working=True),
                _row("root-middle", 800),
                _row("other-agent", 1_000, agent="codex")),
            "claude", state="active", side_mode="roots", sort="updated")
        self.assertEqual([row["session"] for row in rows],
                         ["root-old", "root-middle"])
        self.assertEqual(rows[0]["_family_last_ts"], 900)
        self.assertTrue(rows[0]["_family_working"])

    def test_default_collapses_completed_sides_but_keeps_working_child(self) -> None:
        rows = livetui._visible(
            _snapshot(
                _row("root", 100),
                _row("child-done", 200, parent="root"),
                _row("child-working", 300, parent="root", working=True)),
            "all")
        self.assertEqual([row["session"] for row in rows],
                         ["root", "child-working"])
        self.assertEqual(rows[0]["_collapsed_side_count"], 1)
        expanded = livetui._visible(
            _snapshot(
                _row("root", 100),
                _row("child-done", 200, parent="root"),
                _row("child-working", 300, parent="root", working=True)),
            "all", side_mode="show")
        self.assertEqual(len(expanded), 3)

    def test_family_activity_does_not_falsify_session_working_state(self) -> None:
        rows = livetui._visible(
            _snapshot(
                _row("root", 100, active=False),
                _row("child", 200, parent="root", working=True)),
            "all", state="working", side_mode="roots")
        self.assertEqual([row["session"] for row in rows], ["root"])
        machine = livetui._machine_session(rows[0])
        self.assertFalse(machine["working"])
        self.assertTrue(machine["family_working"])
        self.assertFalse(machine["active"])
        self.assertTrue(machine["family_active"])
        frame_rows = livetui._visible(
            _snapshot(
                _row("root", 100),
                _row("child", 200, parent="root", working=True)),
            "all")
        frame = livetui._frame(
            frame_rows, 300, True, "all", 120, 30, False,
            interactive=False)
        self.assertIn("1 working · 2 live-window", frame)

    def test_direct_session_can_select_a_completed_side(self) -> None:
        rows = livetui._visible(
            _snapshot(
                _row("root", 100),
                _row("child-done", 200, parent="root")),
            "all", session="child-done")
        self.assertEqual([row["session"] for row in rows], ["child-done"])

    def test_structured_metadata_separates_snapshot_page_and_feed(self) -> None:
        snap = _snapshot(_row("root", 100))
        snap["_agrep_live_ipc"] = {
            "published_at_ms": 9_500,
            "recent_trimmed": True,
            "recent_events_omitted": 7,
        }
        args = type("Args", (), {
            "agent": "claude", "json": True, "max": 1,
            "project": None, "roots": True, "session": None,
            "side": False, "side_only": False, "sort": "updated",
            "state": "active",
        })()
        with mock.patch.object(livetui.time, "time", return_value=10.0):
            payload = livetui._machine_snapshot(
                snap, livetui._visible(
                    snap, "claude", state="active", side_mode="roots",
                    sort="updated"),
                2, complete=True, degraded=[], truncated=True,
                source="resident", a=args)
        self.assertTrue(payload["snapshot_complete"])
        self.assertFalse(payload["page_complete"])
        self.assertEqual(payload["completeness"]["matched_basis"], "exact")
        self.assertFalse(payload["feed"]["transport_complete"])
        self.assertEqual(payload["feed"]["recent_events_omitted_by_transport"], 7)
        self.assertEqual(payload["snapshot_age_ms"], 500)

    def test_every_side_signal_uses_the_same_classifier(self) -> None:
        fixtures = (
            {"session": "plain", "parent": "root"},
            {"session": "agent-child"},
            {"session": "plain", "title": "[subagent task] inspect"},
            {"session": "plain", "last_text": "[subagent message] done"},
            {"session": "plain", "first_text": "[subagent task] cached"},
        )
        self.assertTrue(all(livetui.common.is_side_session(row)
                            for row in fixtures))
        self.assertTrue(all(explore._indexed_chat_is_side(row)
                            for row in fixtures))
        self.assertTrue(all(livetui._family_rows([row])[0]["sub"]
                            for row in fixtures))
        self.assertFalse(livetui.common.is_side_session({
            "session": "root", "title": "ordinary chat",
        }))

    def test_unrepresentable_session_never_mints_or_injects_a_handle(self) -> None:
        session = "evil\x1b[2J:owned"
        row = livetui._family_rows([_row(session, 100)])[0]
        machine = livetui._machine_session(row)
        self.assertIsNone(machine["handle"])
        self.assertIn("outside", machine["handle_unavailable_reason"])
        rendered = livetui._row(row, 100, 120, False)
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn(f"@{session}", rendered)
        self.assertIn("\\u001b", rendered)


class BoardPartialTests(unittest.TestCase):
    class Watcher:
        def __init__(self, snapshot: dict) -> None:
            self.value = snapshot
            self.timeouts = []

        def wait_boot(self, timeout=None) -> bool:
            self.timeouts.append(timeout)
            return False

        def snapshot(self) -> dict:
            return self.value

    def test_json_partial_has_no_unrelated_rows_and_exact_retry(self) -> None:
        watcher = self.Watcher(_snapshot(
            _row("unrelated", 100), booting=True))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(
                livetui.indexd_runtime, "resident_indexd_live_snapshot",
                return_value=None), \
                mock.patch.object(livetui.live, "watcher", return_value=watcher), \
                mock.patch.object(livetui, "_enable_ansi", return_value=False), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = livetui.main([
                "--once", "--json", "--session", "deadbeef"])
        self.assertEqual(rc, 2)
        self.assertEqual(err.getvalue(), "")
        payload = json.loads(out.getvalue())
        self.assertFalse(payload["snapshot_complete"])
        self.assertTrue(payload["partial"])
        self.assertEqual(payload["sessions"], [])
        self.assertEqual(payload["selector"]["status"], "unresolved")
        self.assertIn("--session deadbeef", payload["completeness"]["retry"])
        self.assertEqual(payload["completeness"]["retry_argv"][-2:],
                         ["--session", "deadbeef"])

    def test_current_global_error_is_partial_and_machine_visible(self) -> None:
        resident = _snapshot()
        resident["last_err"] = "procscan unavailable: denied"
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(
                livetui.indexd_runtime, "resident_indexd_live_snapshot",
                return_value=resident), \
                mock.patch.object(livetui.live, "watcher") as watcher, \
                mock.patch.object(livetui, "_enable_ansi", return_value=False), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = livetui.main(["--once", "--json", "--wait", "0"])
        self.assertEqual(rc, 2)
        self.assertEqual(err.getvalue(), "")
        watcher.assert_not_called()
        payload = json.loads(out.getvalue())
        self.assertFalse(payload["snapshot_complete"])
        self.assertEqual(
            payload["completeness"]["snapshot_error"],
            "procscan unavailable: denied")
        self.assertIn("--wait", payload["completeness"]["retry_argv"])

    def test_other_agent_source_error_does_not_poison_filtered_completeness(
            self) -> None:
        resident = _snapshot()
        resident["degraded_sources"] = [
            {"agent": "codex", "error": "denied", "path": "/codex"}]
        resident["last_err"] = (
            "codex source unreadable: /codex: denied")
        out = io.StringIO()
        with mock.patch.object(
                livetui.indexd_runtime, "resident_indexd_live_snapshot",
                return_value=resident), \
                mock.patch.object(livetui.live, "watcher") as watcher, \
                mock.patch.object(livetui, "_enable_ansi", return_value=False), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = livetui.main([
                "--once", "--json", "--wait", "5", "--agent", "claude"])
        self.assertEqual(rc, 0)
        watcher.assert_not_called()
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["snapshot_complete"])
        self.assertIsNone(payload["completeness"]["snapshot_error"])
        self.assertEqual(payload["completeness"]["source_errors"], 0)

    def test_exact_live_resolution_reuses_a_complete_resident_snapshot(self) -> None:
        resident = _snapshot(_row("branch-full-id", 100))
        with mock.patch.object(
                livetui.indexd_runtime, "resident_indexd_live_snapshot",
                return_value=resident), \
                mock.patch.object(livetui.live, "watcher") as watcher:
            rows, complete = livetui.resolve_exact_live_session("@branch-full-id")
        self.assertTrue(complete)
        self.assertEqual([row["session"] for row in rows], ["branch-full-id"])
        watcher.assert_not_called()

    def test_exact_live_resolution_waits_when_resident_state_is_partial(self) -> None:
        resident = _snapshot(_row("old", 100), booting=True)
        watcher = self.Watcher(_snapshot(_row("branch-full-id", 200)))
        with mock.patch.object(
                livetui.indexd_runtime, "resident_indexd_live_snapshot",
                return_value=resident), \
                mock.patch.object(livetui.live, "watcher", return_value=watcher):
            rows, complete = livetui.resolve_exact_live_session("branch-full-id")
        self.assertTrue(complete)
        self.assertEqual([row["session"] for row in rows], ["branch-full-id"])
        self.assertEqual(watcher.timeouts, [5.0])

    def test_exact_live_resolution_never_widens_a_prefix(self) -> None:
        resident = _snapshot(_row("branch-full-id", 100))
        with mock.patch.object(
                livetui.indexd_runtime, "resident_indexd_live_snapshot",
                return_value=resident), \
                mock.patch.object(livetui.live, "watcher") as watcher:
            rows, complete = livetui.resolve_exact_live_session("branch")
        self.assertTrue(complete)
        self.assertEqual(rows, [])
        watcher.assert_not_called()

    def test_emitted_retry_waits_past_an_incomplete_resident_snapshot(self) -> None:
        resident = _snapshot(_row("resident", 100))
        resident["degraded_sources"] = [
            {"agent": "claude", "error": "busy", "path": "/store"}]
        first_out = io.StringIO()
        with mock.patch.object(
                livetui.indexd_runtime, "resident_indexd_live_snapshot",
                return_value=resident), \
                mock.patch.object(livetui.live, "watcher") as watcher_factory, \
                mock.patch.object(livetui, "_enable_ansi", return_value=False), \
                contextlib.redirect_stdout(first_out), \
                contextlib.redirect_stderr(io.StringIO()):
            first_rc = livetui.main([
                "--once", "--json", "--wait", "0", "--agent", "claude"])
        self.assertEqual(first_rc, 2)
        watcher_factory.assert_not_called()
        retry = json.loads(first_out.getvalue())["completeness"]["retry_argv"]
        board_index = retry.index("board")
        watcher = self.Watcher(_snapshot(_row("fresh", 200)))
        retry_out = io.StringIO()
        with mock.patch.object(
                livetui.indexd_runtime, "resident_indexd_live_snapshot",
                return_value=resident), \
                mock.patch.object(
                    livetui.live, "watcher", return_value=watcher), \
                mock.patch.object(livetui, "_enable_ansi", return_value=False), \
                contextlib.redirect_stdout(retry_out), \
                contextlib.redirect_stderr(io.StringIO()):
            retry_rc = livetui.main(retry[board_index + 1:])
        self.assertEqual(retry_rc, 0)
        self.assertEqual(watcher.timeouts, [5.0])
        payload = json.loads(retry_out.getvalue())
        self.assertTrue(payload["snapshot_complete"])
        self.assertEqual(payload["source"], "foreground")
        self.assertEqual(payload["sessions"][0]["handle"], "@fresh")

    def test_json_complete_no_match_is_structured(self) -> None:
        watcher = self.Watcher(_snapshot(_row("unrelated", 100)))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(
                livetui.indexd_runtime, "resident_indexd_live_snapshot",
                return_value=None), \
                mock.patch.object(livetui.live, "watcher", return_value=watcher), \
                mock.patch.object(livetui, "_enable_ansi", return_value=False), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = livetui.main([
                "--once", "--json", "--session", "deadbeef"])
        self.assertEqual(rc, 1)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(len(out.getvalue().splitlines()), 1)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["selector"]["status"], "none")
        self.assertEqual(payload["sessions"], [])

    def test_board_dispatches_an_unprefixed_bound_handle(self) -> None:
        watcher = self.Watcher(_snapshot(_row("deadbeef-full", 100)))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(
                livetui.indexd_runtime, "resident_indexd_live_snapshot",
                return_value=None), \
                mock.patch.object(livetui.live, "watcher", return_value=watcher), \
                mock.patch.object(livetui, "_enable_ansi", return_value=False), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = livetui.main([
                "--once", "--json", "--session", "deadbeef:5.abcd"])
        self.assertEqual(rc, 0, err.getvalue())
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["sessions"][0]["session"], "deadbeef-full")

    def test_board_rejects_a_malformed_prefixed_handle(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit) as raised:
            livetui.main(["--once", "--session", "@abc:notint"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("invalid result handle", err.getvalue())

    def test_json_ambiguous_selector_is_structured(self) -> None:
        watcher = self.Watcher(_snapshot(
            _row("shared-one", 100), _row("shared-two", 200)))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(
                livetui.indexd_runtime, "resident_indexd_live_snapshot",
                return_value=None), \
                mock.patch.object(livetui.live, "watcher", return_value=watcher), \
                mock.patch.object(livetui, "_enable_ansi", return_value=False), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = livetui.main([
                "--once", "--json", "--sort", "updated",
                "--session", "shared"])
        self.assertEqual(rc, 2)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(len(out.getvalue().splitlines()), 1)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["selector"]["status"], "ambiguous")
        self.assertEqual(payload["selector"]["matches"], 2)
        self.assertEqual(
            [row["handle"] for row in payload["sessions"]],
            ["@shared-two", "@shared-one"])

    def test_truncated_page_keeps_full_unambiguous_handles(self) -> None:
        rows = [
            livetui._family_rows([_row("abcdefgh-1111", 200)])[0],
            livetui._family_rows([_row("abcdefgh-2222", 100)])[0],
        ]
        args = type("Args", (), {
            "agent": "claude", "json": True, "max": 1,
            "project": None, "roots": True, "session": None,
            "side": False, "side_only": False, "sort": "updated",
            "state": "active",
        })()
        payload = livetui._machine_snapshot(
            _snapshot(*rows), rows[:1], 2, complete=True, degraded=[],
            truncated=True, source="foreground", a=args)
        self.assertFalse(payload["page_complete"])
        self.assertEqual(payload["sessions"][0]["handle"], "@abcdefgh-1111")

    def test_human_partial_never_passes_or_renders_unrelated_rows(self) -> None:
        watcher = self.Watcher(_snapshot(
            _row("unrelated", 100), booting=True))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(
                livetui.indexd_runtime, "resident_indexd_live_snapshot",
                return_value=None), \
                mock.patch.object(livetui.live, "watcher", return_value=watcher), \
                mock.patch.object(livetui, "_enable_ansi", return_value=False), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = livetui.main(["--once", "--session", "deadbeef"])
        self.assertEqual(rc, 2)
        self.assertNotIn("unrelated", out.getvalue())
        self.assertEqual(err.getvalue().count("partial live snapshot"), 1)
        self.assertIn("--session deadbeef", err.getvalue())

    def test_human_complete_selector_has_terse_exit_contracts(self) -> None:
        watcher = self.Watcher(_snapshot(
            _row("shared-one", 100), _row("shared-two", 200)))
        with mock.patch.object(
                livetui.indexd_runtime, "resident_indexd_live_snapshot",
                return_value=None), \
                mock.patch.object(livetui.live, "watcher", return_value=watcher), \
                mock.patch.object(livetui, "_enable_ansi", return_value=False):
            for selector, expected_rc, expected in (
                    ("missing", 1, "no live session matching"),
                    ("shared", 2, "ambiguous live session")):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), \
                        contextlib.redirect_stderr(err):
                    rc = livetui.main([
                        "--once", "--session", selector])
                self.assertEqual(rc, expected_rc)
                self.assertEqual(out.getvalue(), "")
                self.assertIn(expected, err.getvalue())

    def test_windows_unsafe_retry_never_drops_scope(self) -> None:
        args = type("Args", (), {
            "agent": "claude", "json": True, "max": 3,
            "project": "x & calc", "roots": True, "session": None,
            "side": False, "side_only": False, "sort": "updated",
            "state": "active",
        })()
        with mock.patch.object(livetui.console, "WIN", True), \
                mock.patch.object(
                    livetui.dist, "cli_invocation",
                    side_effect=lambda *values: ("agrep.cmd", *values)):
            command = livetui._retry_command(args)
            argv = livetui._retry_argv(args)
            payload = livetui._machine_snapshot(
                _snapshot(booting=True), [], 0, complete=False,
                degraded=[], truncated=False, source="foreground", a=args)
        self.assertIsNone(command)
        self.assertEqual(argv[-2:], ["--project", "x & calc"])
        self.assertIsNone(payload["completeness"]["retry"])
        self.assertEqual(payload["completeness"]["retry_argv"], argv)
        self.assertIn("cannot safely quote", payload["completeness"][
            "retry_command_unavailable"])

    def test_human_root_view_keeps_child_activity_and_visible_handle(self) -> None:
        watcher = self.Watcher(_snapshot(
            _row("root-session", 100, active=False),
            _row("child-session", 300, parent="root-session", working=True)))
        out = io.StringIO()
        with mock.patch.object(
                livetui.indexd_runtime, "resident_indexd_live_snapshot",
                return_value=None), \
                mock.patch.object(livetui.live, "watcher", return_value=watcher), \
                mock.patch.object(livetui, "_enable_ansi", return_value=False), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = livetui.main([
                "--once", "--sort", "updated", "--state", "active", "--roots"])
        self.assertEqual(rc, 0)
        self.assertIn("@root-session", out.getvalue())
        self.assertIn("1 side working", out.getvalue())
        self.assertNotIn("@child-session", out.getvalue())


class TaughtCommandBlackBoxTests(unittest.TestCase):
    def test_literal_taught_command_returns_sorted_reusable_roots(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-board-contract-") as raw:
            root = Path(raw)
            store = root / "home" / ".claude" / "projects" / "fixture"
            store.mkdir(parents=True)
            data = root / "data"
            data.mkdir()
            now = datetime.now(timezone.utc)
            sessions = []
            for index in range(4):
                session = f"11111111-1111-4111-8111-{index:012d}"
                sessions.append(session)
                record = {
                    "cwd": f"/work/project-{index}",
                    "message": {"content": f"active task {index}", "role": "user"},
                    "sessionId": session,
                    "timestamp": (now - timedelta(seconds=index)).isoformat(),
                    "type": "user",
                }
                (store / f"{session}.jsonl").write_text(
                    json.dumps(record) + "\n", encoding="utf-8")
            env = os.environ.copy()
            env.update({
                "AGREP_CLI_NAME": "agrep",
                "AGREP_DATA_DIR": str(data),
                "AGREP_DATA_DIR_SOURCE": "test",
                "AGREP_HOME": str(root / "home"),
                "AGREP_NO_DAEMON": "1",
                "NO_COLOR": "1",
                "TERM": "dumb",
            })
            run = subprocess.run(
                [sys.executable, str(ROOT / "cli.py"), *TAUGHT_BOARD_ARGS],
                cwd=ROOT, env=env, capture_output=True, text=True,
                timeout=15, check=False)
            args = type("Args", (), {
                "agent": "claude", "json": True, "max": 3,
                "project": None, "roots": True, "session": None,
                "side": False, "side_only": False, "sort": "updated",
                "state": "active",
            })()
            partial = livetui._machine_snapshot(
                _snapshot(booting=True), [], 0, complete=False,
                degraded=[], truncated=False, source="foreground", a=args)
            retry = subprocess.run(
                partial["completeness"]["retry"], shell=True,
                cwd=ROOT, env=env, capture_output=True, text=True,
                timeout=15, check=False)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stderr, "")
        self.assertEqual(len(run.stdout.splitlines()), 1)
        payload = json.loads(run.stdout)
        self.assertEqual(payload["kind"], "agrep-board-snapshot")
        self.assertEqual(payload["scope"], "live-window")
        self.assertTrue(payload["snapshot_complete"])
        self.assertFalse(payload["partial"])
        self.assertFalse(payload["page_complete"])
        self.assertEqual(payload["completeness"]["matched"], 4)
        rows = payload["sessions"]
        self.assertEqual(len(rows), 3)
        self.assertEqual([row["session"] for row in rows], sessions[:3])
        self.assertTrue(all(row["agent"] == "claude" for row in rows))
        self.assertTrue(all(row["active"] and not row["side"] for row in rows))
        self.assertEqual(
            [row["family_updated_ms"] for row in rows],
            sorted((row["family_updated_ms"] for row in rows), reverse=True))
        self.assertEqual([row["handle"] for row in rows],
                         [f"@{session}" for session in sessions[:3]])
        self.assertEqual(retry.returncode, 0, retry.stderr)
        retried = json.loads(retry.stdout)
        self.assertTrue(retried["snapshot_complete"])
        self.assertEqual([row["session"] for row in retried["sessions"]],
                         sessions[:3])


if __name__ == "__main__":
    unittest.main()
