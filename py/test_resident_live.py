"""Resident live-state IPC is bounded and generation exact."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PY_DIR = ROOT / "py"
sys.path.insert(0, str(PY_DIR))

import indexd_runtime  # noqa: E402
import livetui  # noqa: E402
import ownerfile  # noqa: E402
import tail  # noqa: E402
from hookless import live as live_mod, procscan  # noqa: E402


def _owner(token: str = "a" * 32, inode: int = 7) -> ownerfile.Snapshot:
    raw = (
        f"pid=4242 start=birth protocol={indexd_runtime.INDEXD_PROTOCOL} "
        f"package=fixture build={indexd_runtime.INDEXD_BUILD_ID} "
        f"writer=fixture group=4242 token={token} time=1\n"
    ).encode("ascii")
    return ownerfile.Snapshot((1, inode, len(raw), 1), time.time(), raw)


def _session(name: str = "session-a", *, events: list[dict] | None = None) -> dict:
    return {
        "agent": "codex",
        "session": name,
        "project": "fixture",
        "title": f"title {name}",
        "model": "fixture-model",
        "last_ts": 1000,
        "state": "thinking",
        "working": True,
        "parent": None,
        "state_ts": 1000,
        "queued": 0,
        "queued_text": "",
        "sub": False,
        "active": True,
        "recent": events or [],
    }


def _live_snapshot(*sessions: dict, booting: bool = False) -> dict:
    return {
        "now": 1000,
        "window_s": 3600,
        "sessions": list(sessions),
        "booting": booting,
        "n_subs": 0,
        "n_emitted": 4,
        "n_tracked": 2,
        "last_err": "",
        "degraded_sources": [],
        "tick_ms": {},
        "n_loops": 3,
        "watch_mode": "indexd",
        "poll_s": 5.0,
        "work_ms": 1,
        "loop_ms": 1,
        "work_total_ms": 3,
    }


class ResidentSnapshotRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="agrep-live-ipc-")
        self.root = Path(self.tmp.name)
        self.live_path = self.root / ".indexd.live"
        self.path_patch = mock.patch.object(
            indexd_runtime, "INDEXD_LIVE_PATH", self.live_path)
        self.readonly_patch = mock.patch.object(
            indexd_runtime, "_data_dir_readonly", return_value=False)
        self.path_patch.start()
        self.readonly_patch.start()
        self.owner_snapshot = _owner()
        self.owner = mock.Mock(spec=ownerfile.Handle)
        self.owner.verify.return_value = self.owner_snapshot
        self.inspection = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.COMPATIBLE,
            self.owner_snapshot,
            4242,
            "birth",
        )

    def tearDown(self) -> None:
        self.readonly_patch.stop()
        self.path_patch.stop()
        self.tmp.cleanup()

    def _publish(self, payload: dict | None = None) -> ownerfile.Snapshot:
        published = indexd_runtime.publish_indexd_live_snapshot(
            self.owner, payload or _live_snapshot(_session()))
        self.assertIsNotNone(published)
        return published

    def _read(self, *, inspections=None) -> dict | None:
        inspection = inspections or self.inspection
        with (
            mock.patch.object(
                indexd_runtime, "_inspect_indexd_owner",
                side_effect=inspection if isinstance(inspection, list) else None,
                return_value=inspection if not isinstance(inspection, list) else None,
            ),
            mock.patch.object(
                indexd_runtime, "_indexd_ready", return_value=True),
        ):
            return indexd_runtime.resident_indexd_live_snapshot()

    def test_ready_resident_snapshot_round_trips_without_hiding_sessions(
            self) -> None:
        self._publish(_live_snapshot(_session("a"), _session("b")))
        payload = self._read()
        self.assertIsNotNone(payload)
        self.assertEqual(
            [row["session"] for row in payload["sessions"]], ["a", "b"])
        self.assertFalse(payload["booting"])
        self.assertEqual(payload["watch_mode"], "indexd")
        self.assertFalse(payload["_agrep_live_ipc"]["recent_trimmed"])
        mode = indexd_runtime.indexd_live_path(
            self.owner_snapshot).stat().st_mode
        if os.name != "nt":
            self.assertEqual(mode & 0o077, 0)

    def test_reader_observes_exact_owner_without_any_write_or_settlement(
            self) -> None:
        self._publish()
        with (
            mock.patch.object(
                indexd_runtime, "_inspect_indexd_owner",
                return_value=self.inspection) as inspect,
            mock.patch.object(
                indexd_runtime, "_indexd_ready", return_value=True),
            mock.patch.object(indexd_runtime.common, "replace_with_retry") as replace,
            mock.patch.object(indexd_runtime.ownerfile, "remove_exact") as remove,
        ):
            self.assertIsNotNone(
                indexd_runtime.resident_indexd_live_snapshot())
        self.assertEqual(inspect.call_args_list, [
            mock.call(settle_child=False),
            mock.call(settle_child=False),
        ])
        replace.assert_not_called()
        remove.assert_not_called()

    def test_daemon_never_publishes_a_partially_booted_snapshot(self) -> None:
        self.assertIsNone(indexd_runtime.publish_indexd_live_snapshot(
            self.owner, _live_snapshot(booting=True)))
        self.owner.verify.assert_not_called()
        self.assertFalse(
            indexd_runtime.indexd_live_path(self.owner_snapshot).exists())

    def test_publication_never_masks_daemon_ownership_loss(self) -> None:
        self.owner.verify.side_effect = ownerfile.OwnershipLost("replaced")
        with self.assertRaises(ownerfile.OwnershipLost):
            indexd_runtime.publish_indexd_live_snapshot(
                self.owner, _live_snapshot(_session()))

    def test_old_feed_events_trim_but_every_session_and_state_survives(
            self) -> None:
        events = [
            {"type": "reply", "ts": index, "text": "x" * 2000}
            for index in range(80)
        ]
        self._publish(_live_snapshot(
            _session("a", events=events),
            _session("b", events=events),
        ))
        payload = self._read()
        self.assertIsNotNone(payload)
        self.assertEqual(
            [row["session"] for row in payload["sessions"]], ["a", "b"])
        self.assertTrue(all(row["working"] for row in payload["sessions"]))
        for row in payload["sessions"]:
            self.assertTrue(row["recent"])
            self.assertEqual(row["recent"], events[-len(row["recent"]):])
        ipc = payload["_agrep_live_ipc"]
        self.assertTrue(ipc["recent_trimmed"])
        self.assertGreater(ipc["recent_events_omitted"], 0)
        path = indexd_runtime.indexd_live_path(self.owner_snapshot)
        self.assertLessEqual(
            path.stat().st_size, indexd_runtime._INDEXD_LIVE_MAX_BYTES)

    def test_impossible_session_floor_never_serializes_recent_bodies(self) -> None:
        giant_event = {"type": "reply", "ts": 1, "text": "x" * 100_000}
        payload = _live_snapshot(*(
            _session(f"session-{index:04d}", events=[giant_event])
            for index in range(600)
        ))
        real_dumps = json.dumps
        payload_event_counts = []

        def observe(value, *args, **kwargs):
            if isinstance(value, dict) and isinstance(value.get("sessions"), list):
                payload_event_counts.append(sum(
                    len(row.get("recent", ()))
                    for row in value["sessions"] if isinstance(row, dict)))
            return real_dumps(value, *args, **kwargs)

        with mock.patch.object(
                indexd_runtime.json, "dumps", side_effect=observe):
            raw = indexd_runtime._indexd_live_bytes(self.owner_snapshot, payload)
        self.assertIsNone(raw)
        self.assertEqual(payload_event_counts, [0])

    def test_reader_rejects_stale_wrong_generation_and_owner_change(self) -> None:
        self._publish()
        with mock.patch.object(
                indexd_runtime.time, "time",
                return_value=time.time() + indexd_runtime._INDEXD_LIVE_MAX_AGE_S + 1):
            self.assertIsNone(self._read())

        self._publish()
        path = indexd_runtime.indexd_live_path(self.owner_snapshot)
        payload = json.loads(path.read_text(encoding="ascii"))
        payload["_agrep_live_ipc"]["generation"] = "b" * 32
        path.write_text(
            json.dumps(payload, separators=(",", ":")) + "\n",
            encoding="ascii")
        if os.name != "nt":
            path.chmod(0o600)
        self.assertIsNone(self._read())

        self._publish()
        changed = indexd_runtime._IndexdOwnerInspection(
            indexd_runtime._IndexdOwnerState.COMPATIBLE,
            _owner("c" * 32, inode=8),
            4243,
            "other-birth",
        )
        self.assertIsNone(self._read(
            inspections=[self.inspection, changed]))

    def test_reader_rejects_malformed_oversized_and_nonregular_files(self) -> None:
        path = indexd_runtime.indexd_live_path(self.owner_snapshot)
        cases = (
            ("malformed", b"{not-json\n"),
            ("oversized", b"x" * (indexd_runtime._INDEXD_LIVE_MAX_BYTES + 1)),
        )
        for label, raw in cases:
            with self.subTest(label=label):
                path.write_bytes(raw)
                if os.name != "nt":
                    path.chmod(0o600)
                self.assertIsNone(self._read())
                path.unlink()
        path.mkdir()
        self.assertIsNone(self._read())

    @unittest.skipIf(os.name == "nt", "POSIX mode bits are not Windows ACLs")
    def test_reader_rejects_world_readable_snapshot(self) -> None:
        self._publish()
        path = indexd_runtime.indexd_live_path(self.owner_snapshot)
        path.chmod(0o644)
        self.assertIsNone(self._read())

    def test_cleanup_removes_only_the_exact_published_entry(self) -> None:
        published = self._publish()
        path = indexd_runtime.indexd_live_path(self.owner_snapshot)
        replacement = path.with_name("replacement")
        replacement.write_bytes(b"foreign\n")
        os.replace(replacement, path)
        self.assertFalse(indexd_runtime.remove_indexd_live_snapshot(
            self.owner_snapshot, published))
        self.assertEqual(path.read_bytes(), b"foreign\n")

        published = self._publish()
        self.assertTrue(indexd_runtime.remove_indexd_live_snapshot(
            self.owner_snapshot, published))
        self.assertFalse(path.exists())

    def test_daemon_reaper_contains_subprocess_errors(self) -> None:
        process = mock.Mock()
        process.communicate.side_effect = subprocess.SubprocessError(
            "fixture reaper failure")
        indexd_runtime._reap_killed_drift_probe(process)
        process.communicate.assert_called_once_with()


class ResidentSnapshotSurfaceTests(unittest.TestCase):
    class Watcher:
        def __init__(self, payload: dict) -> None:
            self.payload = payload
            self.timeouts: list[float | None] = []

        def wait_boot(self, timeout: float | None = None) -> bool:
            self.timeouts.append(timeout)
            return not self.payload.get("booting")

        def snapshot(self) -> dict:
            return self.payload

    def test_tail_and_board_once_use_resident_state_without_a_watcher(self) -> None:
        payload = _live_snapshot(_session())
        payload["_agrep_live_ipc"] = {
            "published_at_ms": 1_000,
            "recent_trimmed": True,
            "recent_events_omitted": 7,
        }
        tail_out = io.StringIO()
        with (
            mock.patch.object(
                tail.indexd_runtime, "resident_indexd_live_snapshot",
                return_value=payload),
            mock.patch.object(tail.live, "watcher") as watcher,
            contextlib.redirect_stdout(tail_out),
        ):
            self.assertEqual(tail.main(["--snapshot"]), 0)
        watcher.assert_not_called()
        self.assertEqual(
            json.loads(tail_out.getvalue())["sessions"][0]["session"],
            "session-a")

        board_out = io.StringIO()
        with (
            mock.patch.object(
                livetui.indexd_runtime, "resident_indexd_live_snapshot",
                return_value=payload),
            mock.patch.object(livetui.live, "watcher") as watcher,
            mock.patch.object(livetui.common, "setting", return_value=None),
            mock.patch.object(livetui, "_enable_ansi", return_value=False),
            mock.patch.object(livetui.time, "time", return_value=2.001),
            contextlib.redirect_stdout(board_out),
        ):
            self.assertEqual(livetui.main(["--once"]), 0)
        watcher.assert_not_called()
        self.assertIn("session-a", board_out.getvalue())
        self.assertIn(
            "resident snapshot omitted 7 older feed event(s)",
            board_out.getvalue())
        self.assertIn("resident snapshot 2s old", board_out.getvalue())

    def test_invalid_resident_state_falls_back_to_bounded_local_watcher(
            self) -> None:
        payload = _live_snapshot(booting=True)
        watcher = self.Watcher(payload)
        out = io.StringIO()
        err = io.StringIO()
        with (
            mock.patch.object(
                livetui.indexd_runtime, "resident_indexd_live_snapshot",
                return_value=None),
            mock.patch.object(livetui.live, "watcher", return_value=watcher),
            mock.patch.object(livetui.common, "setting", return_value=None),
            mock.patch.object(livetui, "_enable_ansi", return_value=False),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            self.assertEqual(
                livetui.main(["--once", "--session", "not-seen-yet"]), 2)
        self.assertEqual(
            watcher.timeouts, [livetui._ONESHOT_BOOT_TIMEOUT_S])
        self.assertIn("snapshot still scanning", out.getvalue())
        self.assertIn("partial live snapshot; retry:", err.getvalue())
        self.assertIn("--session not-seen-yet", err.getvalue())
        self.assertNotIn("no live session matching", err.getvalue())

    def test_matching_partial_focus_discloses_incomplete_session_state(
            self) -> None:
        payload = _live_snapshot(_session(), booting=True)
        watcher = self.Watcher(payload)
        out = io.StringIO()
        with (
            mock.patch.object(
                livetui.indexd_runtime, "resident_indexd_live_snapshot",
                return_value=None),
            mock.patch.object(livetui.live, "watcher", return_value=watcher),
            mock.patch.object(livetui.common, "setting", return_value=None),
            mock.patch.object(livetui, "_enable_ansi", return_value=False),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                livetui.main(["--once", "--session", "session-a"]), 2)
        rendered = out.getvalue()
        self.assertIn(
            "snapshot still scanning; session events/state may be incomplete",
            rendered)
        self.assertIn("no events visible yet", rendered)
        self.assertNotIn("no events yet this window", rendered)

class PiLiveSnapshotTests(unittest.TestCase):
    def test_omp_journal_maps_to_live_events_and_alias_snapshot(self) -> None:
        """A newly active OMP journal is canonical pi state selected by either name."""
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(live_mod, "HOME", td):
            now = time.time()

            def stamped(offset: int) -> str:
                return time.strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now + offset))

            session_id = "01234567-89ab-7000-8000-0123456789ab"
            store = (Path(td) / ".omp" / "agent" / "sessions"
                     / "-work-app")
            store.mkdir(parents=True)
            path = store / f"2026-08-16T09-00-00-000Z_{session_id}.jsonl"
            rows = [
                {
                    "type": "title", "v": 1, "title": "Fixture task",
                    "updatedAt": stamped(-7), "pad": " ",
                },
                {
                    "type": "session", "version": 3, "id": session_id,
                    "timestamp": stamped(-7), "cwd": "/work/app",
                },
                {
                    "type": "model_change", "id": "model", "parentId": None,
                    "timestamp": stamped(-6), "model": "fixture-model",
                },
                {
                    "type": "message", "id": "user", "parentId": "model",
                    "timestamp": stamped(-5),
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "inspect it"}],
                    },
                },
                {
                    "type": "message", "id": "assistant-tool",
                    "parentId": "user", "timestamp": stamped(-4),
                    "message": {
                        "role": "assistant", "model": "fixture-model",
                        "content": [
                            {"type": "thinking", "thinking": "hidden"},
                            {"type": "text", "text": "checking"},
                            {
                                "type": "toolCall", "id": "call-1",
                                "name": "read",
                                "arguments": {"path": "/work/app/a.txt"},
                            },
                        ],
                        "stopReason": "toolUse",
                    },
                },
                {
                    "type": "custom", "customType": "tool_execution_start",
                    "id": "tool-start", "parentId": "assistant-tool",
                    "timestamp": stamped(-4),
                    "data": {
                        "toolCallId": "call-1", "toolName": "read",
                        "startedAt": stamped(-4),
                    },
                },
                {
                    "type": "message", "id": "tool-result",
                    "parentId": "tool-start", "timestamp": stamped(-3),
                    "message": {
                        "role": "toolResult", "toolCallId": "call-1",
                        "toolName": "read", "isError": False,
                        "content": [{"type": "text", "text": "a.txt"}],
                    },
                },
                {
                    "type": "message", "id": "assistant-final",
                    "parentId": "tool-result", "timestamp": stamped(-2),
                    "message": {
                        "role": "assistant", "model": "fixture-model",
                        "content": [{"type": "text", "text": "finished"}],
                        "stopReason": "stop",
                    },
                },
                {
                    "type": "custom", "customType": "session_exit",
                    "id": "exit", "parentId": "assistant-final",
                    "timestamp": stamped(-1),
                    "data": {"reason": "dispose", "kind": "normal"},
                },
            ]
            path.write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8")

            watcher = live_mod.LiveWatcher()
            watcher._booted = True
            watcher._boot_complete.set()
            events = watcher.subscribe()
            watcher._tick_pi(now)
            emitted = []
            while not events.empty():
                emitted.append(events.get_nowait())

            snapshot = watcher.snapshot()
            self.assertEqual(len(snapshot["sessions"]), 1)
            live_session = snapshot["sessions"][0]
            self.assertEqual(
                {
                    "agent": live_session["agent"],
                    "session": live_session["session"],
                    "project": live_session["project"],
                    "title": live_session["title"],
                    "model": live_session["model"],
                    "state": live_session["state"],
                    "working": live_session["working"],
                },
                {
                    "agent": "pi", "session": session_id,
                    "project": "work/app", "title": "Fixture task",
                    "model": "fixture-model", "state": "done",
                    "working": False,
                },
            )
            self.assertEqual(
                [event["type"] for event in emitted],
                ["user", "reply", "tool", "tool_done", "reply", "done"],
            )
            tool = next(event for event in emitted if event["type"] == "tool")
            self.assertEqual(
                {key: tool[key] for key in ("agent", "session", "name",
                                            "input", "call_id")},
                {
                    "agent": "pi", "session": session_id, "name": "read",
                    "input": "/work/app/a.txt", "call_id": "call-1",
                },
            )
            tool_done = next(
                event for event in emitted if event["type"] == "tool_done")
            self.assertEqual(
                {key: tool_done[key] for key in
                 ("name", "output", "call_id", "ok", "dur")},
                {
                    "name": "read", "output": "a.txt",
                    "call_id": "call-1", "ok": True, "dur": 1000,
                },
            )
            self.assertEqual(
                [event.get("text") for event in emitted
                 if event["type"] in ("user", "reply")],
                ["inspect it", "checking", "finished"],
            )

            out = io.StringIO()
            with (
                mock.patch.object(
                    tail.indexd_runtime, "resident_indexd_live_snapshot",
                    return_value=None),
                mock.patch.object(tail.live, "watcher", return_value=watcher),
                contextlib.redirect_stdout(out),
            ):
                self.assertEqual(
                    tail.main(["--snapshot", "--agent", "omp"]), 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["type"], "snapshot")
            self.assertEqual(
                [(row["agent"], row["session"]) for row in payload["sessions"]],
                [("pi", session_id)],
            )

    def test_pi_reader_discovers_both_home_stores(self) -> None:
        """The canonical pi adapter watches the native and oh-my-pi session roots."""
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(live_mod, "HOME", td):
            now = time.time()
            timestamp = time.strftime(
                "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now))
            expected = set()
            for home_name, session_id in (
                    (".pi", "11111111-1111-7111-8111-111111111111"),
                    (".omp", "22222222-2222-7222-8222-222222222222")):
                expected.add(session_id)
                store = (Path(td) / home_name / "agent" / "sessions"
                         / "-work-project")
                store.mkdir(parents=True)
                path = store / f"2026-08-16T09-00-00-000Z_{session_id}.jsonl"
                rows = [
                    {
                        "type": "session", "version": 3, "id": session_id,
                        "timestamp": timestamp, "cwd": "/work/project",
                    },
                    {
                        "type": "message", "id": f"user-{home_name}",
                        "parentId": None, "timestamp": timestamp,
                        "message": {
                            "role": "user", "content": [
                                {"type": "text", "text": home_name},
                            ],
                        },
                    },
                ]
                path.write_text(
                    "".join(json.dumps(row) + "\n" for row in rows),
                    encoding="utf-8")
            watcher = live_mod.LiveWatcher()
            watcher._booted = True
            watcher._tick_pi(now)
            self.assertEqual(
                {row["session"] for row in watcher.snapshot()["sessions"]},
                expected,
            )

    def test_pi_process_fingerprints_exclude_omp_helpers(self) -> None:
        """Only interactive pi/OMP commands are agent writers; daemon helpers are not."""
        session_id = "12345678-1234-1234-1234-123456789abc"
        self.assertEqual(
            procscan._classify("omp", f"omp --resume {session_id}"), "pi")
        self.assertEqual(
            procscan._session_from_cmd(
                "pi", f"omp --resume {session_id}"),
            session_id,
        )
        self.assertEqual(procscan._classify("pi.exe", "pi"), "pi")
        self.assertEqual(
            procscan._classify(
                "node", "/app/node_modules/pi-coding-agent/dist/cli.js"),
            "pi",
        )
        self.assertIsNone(
            procscan._classify("omp", "omp __omp_worker_daemon_broker"))


class TailSnapshotFilterTests(unittest.TestCase):
    """A validated --agent alias must match the canonical names sessions
    carry, and a supplied --events beside --snapshot is refused - snapshot
    recents never contain done/queued rows, so the filter cannot be honored."""

    def test_agent_alias_filter_matches_canonical_sessions(self) -> None:
        session = _session("session-agy")
        session["agent"] = "antigravity"
        payload = _live_snapshot(session)
        out = io.StringIO()
        with (
            mock.patch.object(
                tail.indexd_runtime, "resident_indexd_live_snapshot",
                return_value=payload),
            mock.patch.object(tail.live, "watcher") as watcher,
            contextlib.redirect_stdout(out),
        ):
            self.assertEqual(tail.main(["--snapshot", "--agent", "agy"]), 0)
        watcher.assert_not_called()
        self.assertEqual(
            [row["session"] for row in json.loads(out.getvalue())["sessions"]],
            ["session-agy"])

    def test_supplied_events_beside_snapshot_is_refused(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit) as ctx:
            tail.main(["--snapshot", "--events", "tool"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("--events", err.getvalue())
        self.assertIn("--snapshot", err.getvalue())

    def test_a_bare_snapshot_still_serves_without_a_refusal(self) -> None:
        payload = _live_snapshot(_session())
        out = io.StringIO()
        with (
            mock.patch.object(
                tail.indexd_runtime, "resident_indexd_live_snapshot",
                return_value=payload),
            mock.patch.object(tail.live, "watcher") as watcher,
            contextlib.redirect_stdout(out),
        ):
            self.assertEqual(tail.main(["--snapshot"]), 0)
        watcher.assert_not_called()
        self.assertEqual(json.loads(out.getvalue())["type"], "snapshot")


if __name__ == "__main__":
    unittest.main()
