"""Focused regressions for cold/live seam contracts."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))
sys.path.insert(0, str(ROOT))

from _test_support import isolate_data_dir, without_store_override  # noqa: E402

isolate_data_dir()

from hookless import live as live_mod, locators as locators_mod  # noqa: E402
from hookless.live import (LiveWatcher, _codex_call_output, _cursor_db_paths,  # noqa: E402
                           _project_of)
from hookless.native import opencode_db_paths  # noqa: E402


class LiveParityEdges(unittest.TestCase):
    def test_resumed_old_codex_rollout_is_tailed(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(live_mod, "HOME", td):
            path = Path(td) / ".codex" / "sessions" / "2020" / "01" / "02" / \
                "rollout-old.jsonl"
            path.parent.mkdir(parents=True)
            records = [
                {"type": "session_meta",
                 "payload": {"id": "old", "cwd": "/work/agrep"}},
                {
                    "timestamp": "2026-07-18T12:00:00Z",
                    "type": "response_item",
                    "payload": {"type": "message", "role": "user", "content": [
                        {"type": "input_text", "text": "resumed old session"},
                    ]},
                },
            ]
            path.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8")
            watcher = LiveWatcher()
            now = path.stat().st_mtime + 0.01
            watcher._tick_codex(now)
            self.assertTrue(any(event.get("text") == "resumed old session"
                                for event in watcher.ring))

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_claude_live_discovery_rejects_symlinked_store_entries(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(live_mod, "HOME", td):
            root = Path(td) / ".claude" / "projects"
            project = root / "real"
            project.mkdir(parents=True)

            def row(text):
                return json.dumps({
                    "type": "user", "sessionId": text, "cwd": "/work/agrep",
                    "message": {"content": text},
                }) + "\n"

            safe = project / "safe.jsonl"
            safe.write_text(row("safe prompt"), encoding="utf-8")
            outside = Path(td) / "outside"
            outside.mkdir()
            outside_file = outside / "outside.jsonl"
            outside_file.write_text(row("outside prompt"), encoding="utf-8")
            outside_subagents = outside / "subagents"
            outside_subagents.mkdir()
            (outside_subagents / "child.jsonl").write_text(
                row("outside child"), encoding="utf-8")
            try:
                (root / "linked-project").symlink_to(outside, target_is_directory=True)
                (project / "linked.jsonl").symlink_to(outside_file)
                nested = project / "nested"
                nested.mkdir()
                (nested / "subagents").symlink_to(outside_subagents,
                                                    target_is_directory=True)
                leaf_session = project / "leaf-session" / "subagents"
                leaf_session.mkdir(parents=True)
                (leaf_session / "linked.jsonl").symlink_to(outside_file)
            except OSError as error:
                self.skipTest(f"symlink creation unavailable: {error}")

            watcher = LiveWatcher()
            watcher._tick_claude(time.time())
            texts = [event.get("text") for event in watcher.ring
                     if event.get("type") == "user"]
            self.assertEqual(texts, ["safe prompt"])

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_antigravity_live_discovery_rejects_symlinked_store_entries(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(live_mod, "HOME", td):
            root = Path(td) / ".gemini" / "antigravity-cli" / "brain"
            safe = root / "safe" / ".system_generated"
            (safe / "logs").mkdir(parents=True)
            (safe / "messages").mkdir()

            def transcript(path, text):
                path.write_text(json.dumps({
                    "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": text,
                }) + "\n", encoding="utf-8")

            transcript(safe / "logs" / "transcript.jsonl", "safe prompt")
            outside = Path(td) / "outside-antigravity"
            (outside / ".system_generated" / "logs").mkdir(parents=True)
            transcript(outside / ".system_generated" / "logs" / "transcript.jsonl",
                       "outside session")
            outside_transcript = Path(td) / "outside-transcript.jsonl"
            transcript(outside_transcript, "outside transcript")
            outside_mail = Path(td) / "outside-mail.json"
            outside_mail.write_text(json.dumps({
                "sender": "local/task-bad", "content": "outside mail",
            }), encoding="utf-8")
            try:
                (root / "linked-session").symlink_to(outside, target_is_directory=True)
                linked_generated = root / "linked-generated"
                linked_generated.mkdir()
                (linked_generated / ".system_generated").symlink_to(
                    outside / ".system_generated", target_is_directory=True)
                linked_logs = root / "linked-logs" / ".system_generated"
                linked_logs.mkdir(parents=True)
                (linked_logs / "logs").symlink_to(
                    outside / ".system_generated" / "logs", target_is_directory=True)
                linked_leaf = root / "linked-leaf" / ".system_generated" / "logs"
                linked_leaf.mkdir(parents=True)
                (linked_leaf / "transcript.jsonl").symlink_to(outside_transcript)
                (safe / "messages" / "linked.json").symlink_to(outside_mail)
            except OSError as error:
                self.skipTest(f"symlink creation unavailable: {error}")

            watcher = LiveWatcher()
            watcher._tick_antigravity(time.time())
            texts = [event.get("text") for event in watcher.ring
                     if event.get("type") == "user"]
            outputs = [event.get("output") for event in watcher.ring]
            self.assertEqual(texts, ["safe prompt"])
            self.assertNotIn("outside mail", outputs)

    def test_antigravity_mailbox_closes_message_file(self):
        with tempfile.TemporaryDirectory() as td:
            brain = Path(td)
            messages = brain / ".system_generated" / "messages"
            messages.mkdir(parents=True)
            (messages / "message.json").write_text("fixture", encoding="utf-8")
            opened = mock.mock_open(read_data=json.dumps({
                "sender": "local/task-child",
                "content": "child result",
            }))
            watcher = LiveWatcher()
            with mock.patch("builtins.open", opened):
                watcher._agy_mailbox(str(brain), "session", time.time())
            handle = opened()
            handle.__enter__.assert_called_once_with()
            handle.__exit__.assert_called_once()

    def test_board_feed_keeps_subagent_result_content(self):
        from livetui import _event_line

        rendered = _event_line({
            "type": "subagent_result",
            "name": "audit child",
            "output": "found the retry race",
        }, 100)
        self.assertIn("audit child: found the retry race", rendered)

    def test_board_cell_width_handles_cjk_and_joined_emoji(self):
        from livetui import _cells, _frame, _row

        ascii_row = {
            "agent": "aa", "project": "abcd", "state": "work",
            "session": "same-session", "last_ts": 1,
        }
        wide_row = {
            **ascii_row,
            "agent": "界", "project": "项目", "state": "工作",
        }
        self.assertEqual(_cells("👩\u200d💻"), 2)
        self.assertEqual(
            _cells(_row(ascii_row, 1, 120, False)),
            _cells(_row(wide_row, 1, 120, False)),
        )
        frame = _frame([ascii_row], 1, True, "all", 120, 20, False)
        self.assertIn("● working", frame)
        self.assertIn("↳ child result", frame)

    def test_quiet_parent_lookup_uses_agent_qualified_session_key(self):
        watcher = LiveWatcher()
        now = int(time.time() * 1000)
        watcher._emit({"agent": "codex", "session": "parent", "ts": now - 2_000_000,
                       "type": "user", "text": "old parent"})
        watcher._emit({"agent": "codex", "session": "child", "ts": now,
                       "type": "user", "text": "active child",
                       "sub_session": True, "parent": "parent"})

        sessions = {row["session"] for row in watcher.snapshot()["sessions"]}
        self.assertEqual(sessions, {"parent", "child"})
        with mock.patch.object(live_mod, "_plain_dir", return_value=True), \
                mock.patch.object(live_mod.os, "walk", return_value=[]) as walk:
            watcher._seed_unknown_parents()
        walk.assert_not_called()

    def test_native_cursor_profile_wins_and_symlink_is_not_selected(self):
        fake_os = types.SimpleNamespace(name="posix", path=os.path, environ=os.environ)
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(live_mod, "HOME", td), \
                mock.patch.object(locators_mod, "os", fake_os), \
                mock.patch.object(locators_mod.sys, "platform", "darwin"):
            roots = [
                Path(td) / "Library" / "Application Support" / "Cursor",
                Path(td) / "AppData" / "Roaming" / "Cursor",
                Path(td) / ".config" / "Cursor",
            ]
            for root in roots:
                db = root / "User" / "globalStorage" / "state.vscdb"
                db.parent.mkdir(parents=True)
                db.write_bytes(b"fixture")
            self.assertEqual(_cursor_db_paths(), [str(
                roots[0] / "User" / "globalStorage" / "state.vscdb")])
            native = roots[0] / "User" / "globalStorage" / "state.vscdb"
            native.unlink()
            try:
                native.symlink_to(roots[1] / "User" / "globalStorage" / "state.vscdb")
            except OSError:
                self.skipTest("symlinks unavailable")
            self.assertEqual(_cursor_db_paths(), [str(
                roots[1] / "User" / "globalStorage" / "state.vscdb")])

    def test_opencode_discovers_xdg_channels_and_rejects_backups(self):
        with tempfile.TemporaryDirectory() as td:
            home = str(Path(td) / "home")
            xdg = Path(td) / "xdg"
            store = xdg / "opencode"
            store.mkdir(parents=True)
            channel = store / "opencode-nightly.db"
            channel.write_bytes(b"db")
            (store / "opencode.bak.db").write_bytes(b"backup")
            (store / "opencode.corrupted.db").write_bytes(b"corrupt")
            env = {"XDG_DATA_HOME": str(xdg), "OPENCODE_DB": ""}
            with without_store_override(), \
                    mock.patch.dict("os.environ", env, clear=False):
                self.assertEqual(opencode_db_paths(home), [str(channel)])

    def test_delta_reader_uses_exact_byte_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "live.jsonl"
            path.write_bytes("é\n".encode())
            watcher = LiveWatcher()
            old_size = path.stat().st_size
            original_open = open

            class AppendingReader:
                def __enter__(self):
                    self.stream = original_open(path, "rb")
                    with original_open(path, "ab") as out:
                        out.write(b"X\n")
                    return self.stream

                def __exit__(self, *exc):
                    self.stream.close()

            with mock.patch("builtins.open", return_value=AppendingReader()):
                first = watcher._read_delta(str(path), old_size)
            second = watcher._read_delta(str(path), path.stat().st_size)
            self.assertEqual(first, ["é"])
            self.assertEqual(second, ["X"])

    def test_child_tool_activity_uses_event_schema(self):
        from livetui import _detail

        child = {
            "agent": "codex", "session": "child-1", "working": True,
            "state": "⚙ shell", "state_ts": 9000, "last_ts": 10000,
            "recent": [{"type": "tool", "sub_session": True,
                        "text": "cargo test"}],
        }
        rendered = "\n".join(_detail(child, 10000, 100, False))
        self.assertIn("cargo test", rendered)

    def test_project_names_match_rust_contract(self):
        self.assertEqual(_project_of("C:/Users/tester"), "~")
        self.assertEqual(_project_of("/repo/mobile"), "repo/mobile")
        self.assertEqual(_project_of("/src/web"), "src/web")
        self.assertEqual(_project_of('C:/Users/tester/sample-project"'), "sample-project")

    def test_codex_nested_output_and_replayed_parent(self):
        text, ok = _codex_call_output(
            {"output": "failed cleanly", "metadata": {"exit_code": 7}})
        self.assertEqual(text, "failed cleanly")
        self.assertFalse(ok)

        watcher = LiveWatcher()
        path = "rollout-child"
        watcher._codex_meta[path] = ("child", "agrep")
        watcher._codex_sub[path] = {
            "agent_path": "/root/a", "parent": "", "started": False,
            "start_turn_id": "", "replayed_parent": False, "seen": set(),
        }
        watcher._codex_line(path, json.dumps(
            {"type": "session_meta", "payload": {"id": "parent"}}))
        self.assertTrue(watcher._codex_sub[path]["replayed_parent"])
        self.assertEqual(watcher._codex_sub[path]["parent"], "parent")

    def test_cursor_uses_headers_resolved_args_and_stable_tool_identity(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE cursorDiskKV(key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO cursorDiskKV VALUES (?,?)", (
            "composerData:c", json.dumps({
                "fullConversationHeadersOnly": [
                    {"bubbleId": "good"}, {"bubbleId": "tool"},
                    {"bubbleId": "wrapper"},
                ],
                "workspaceIdentifier": {"uri": {"fsPath": "/work/agrep"}},
            })))
        watcher = LiveWatcher()
        watcher._cursor_bubble(conn, "bubbleId:c:orphan",
                               json.dumps({"type": 1, "text": "stale edit"}), 1.0)
        watcher._cursor_bubble(conn, "bubbleId:c:wrapper", json.dumps(
            {"type": 1, "text": "<system-reminder>generated</system-reminder>"}), 1.0)
        watcher._cursor_bubble(conn, "bubbleId:c:good",
                               json.dumps({"type": 1, "text": "real prompt"}), 1.0)
        watcher._cursor_bubble(conn, "bubbleId:c:tool", json.dumps({
            "type": 2,
            "toolFormerData": {
                "name": "Shell", "status": "failed",
                "params": json.dumps({"command": "resolved path"}),
                "rawArgs": json.dumps({"command": "raw path"}),
                "result": "nope",
            },
        }), 1.0)
        conn.close()

        users = [event for event in watcher.ring if event.get("type") == "user"]
        tools = [event for event in watcher.ring if event.get("type") in ("tool", "tool_done")]
        self.assertEqual([event["text"] for event in users], ["real prompt"])
        self.assertEqual([event["call_id"] for event in tools],
                         ["cursor:tool", "cursor:tool"])
        self.assertIn("resolved path", tools[0]["input"])
        self.assertFalse(tools[-1]["ok"])

    def test_live_database_pollers_close_after_post_connect_error(self):
        watcher = LiveWatcher()
        cases = (
            ("opencode", watcher._poll_opencode_db, ("opencode.db", True)),
            ("cursor", watcher._poll_cursor_db, ("state.vscdb", True, 1.0)),
        )
        for name, poll, args in cases:
            with self.subTest(poller=name):
                connection = mock.Mock()
                connection.execute.side_effect = sqlite3.OperationalError("broken")
                with mock.patch.object(live_mod, "_probe_source"), \
                        mock.patch.object(
                            live_mod.sqlite3, "connect", return_value=connection):
                    poll(*args)
                connection.close.assert_called_once_with()

    def test_opencode_merges_text_parts_and_keeps_orphan_tools(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "opencode.db"
            conn = sqlite3.connect(db)
            conn.executescript("""
                CREATE TABLE session(id TEXT PRIMARY KEY, directory TEXT, title TEXT,
                                     parent_id TEXT);
                CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT,
                                     time_updated INTEGER, data TEXT);
                CREATE TABLE part(id TEXT PRIMARY KEY, message_id TEXT,
                                  time_created INTEGER, time_updated INTEGER, data TEXT);
            """)
            conn.execute("INSERT INTO session VALUES ('s','/work/agrep','title',NULL)")
            conn.execute("INSERT INTO message VALUES (?,?,?,?)",
                         ("m-user", "s", 1002, json.dumps({"role": "user"})))
            for pid, ts, text in (("p1", 1000, "first"), ("p2", 1001, "second")):
                conn.execute("INSERT INTO part VALUES (?,?,?,?,?)", (
                    pid, "m-user", ts, ts, json.dumps({"type": "text", "text": text})))
            conn.execute("INSERT INTO message VALUES (?,?,?,?)",
                         ("m-tool", "s", 1003, json.dumps({"role": "assistant"})))
            conn.execute("INSERT INTO part VALUES (?,?,?,?,?)", (
                "p-tool", "m-tool", 1003, 1003, json.dumps({
                    "type": "tool", "tool": "shell",
                    "state": {"status": "running", "input": {"command": "pwd"}},
                })))
            # No session row: cold deliberately retains tool events even though it drops text.
            conn.execute("INSERT INTO message VALUES (?,?,?,?)",
                         ("m-orphan", "gone", 1004, json.dumps({"role": "assistant"})))
            conn.execute("INSERT INTO part VALUES (?,?,?,?,?)", (
                "p-orphan", "m-orphan", 1004, 1004, json.dumps({
                    "type": "tool", "tool": "read",
                    "state": {"status": "completed", "output": "ok"},
                })))
            conn.commit()
            conn.close()

            watcher = LiveWatcher()
            watcher._poll_opencode_db(str(db), True)
            users = [event for event in watcher.ring if event.get("type") == "user"]
            self.assertEqual([event["text"] for event in users], ["first second"])
            self.assertTrue(any(event.get("call_id") == "p-orphan"
                                for event in watcher.ring))

            conn = sqlite3.connect(db)
            conn.execute("UPDATE part SET time_updated=?, data=? WHERE id='p-tool'", (
                1005, json.dumps({"type": "tool", "tool": "shell",
                                  "state": {"status": "completed", "output": "done"}})))
            conn.commit()
            conn.close()
            watcher._poll_opencode_db(str(db), False)
            shell = [event for event in watcher.ring if event.get("name") == "shell"]
            self.assertEqual([event["call_id"] for event in shell], ["p-tool", "p-tool"])
            self.assertNotIn("p-tool", watcher.sessions["opencode:s"]["pending"])

            conn = sqlite3.connect(db)
            conn.execute("INSERT INTO message VALUES (?,?,?,?)", (
                "m-reply", "s", 1006, json.dumps({"role": "assistant"})))
            conn.execute("INSERT INTO part VALUES (?,?,?,?,?)", (
                "p-reply", "m-reply", 1006, 1006,
                json.dumps({"type": "text", "text": "partial reply"})))
            conn.commit()
            conn.close()
            watcher._poll_opencode_db(str(db), False)

            conn = sqlite3.connect(db)
            conn.execute("UPDATE part SET time_updated=?, data=? WHERE id='p-reply'", (
                1007, json.dumps({"type": "text", "text": "complete reply"})))
            conn.commit()
            conn.close()
            watcher._poll_opencode_db(str(db), False)
            replies = [event["text"] for event in watcher.ring
                       if event.get("type") == "reply"]
            self.assertEqual(replies[-2:], ["partial reply", "complete reply"])

    def test_opencode_tied_watermarks_do_not_drop_rows(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "opencode.db"
            conn = sqlite3.connect(db)
            conn.executescript("""
                CREATE TABLE session(id TEXT PRIMARY KEY, directory TEXT, title TEXT,
                                     parent_id TEXT);
                CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT,
                                     time_updated INTEGER, data TEXT);
                CREATE TABLE part(id TEXT PRIMARY KEY, message_id TEXT,
                                  time_created INTEGER, time_updated INTEGER, data TEXT);
            """)
            conn.execute("INSERT INTO session VALUES ('s','/work/agrep','title',NULL)")
            for i in range(801):
                mid, pid = f"m{i:04d}", f"p{i:04d}"
                conn.execute("INSERT INTO message VALUES (?,?,?,?)", (
                    mid, "s", 1000, json.dumps({"role": "user"})))
                conn.execute("INSERT INTO part VALUES (?,?,?,?,?)", (
                    pid, mid, 1000, 1000,
                    json.dumps({"type": "text", "text": f"row {i}"})))
            conn.commit()
            conn.close()

            watcher = LiveWatcher()
            watcher._poll_opencode_db(str(db), True)
            self.assertEqual(len(watcher._oc_text_messages), 800)
            watcher._poll_opencode_db(str(db), False)
            self.assertEqual(len(watcher._oc_text_messages), 801)
            self.assertEqual(watcher._oc_watermark[str(db)], (1000, "p0800"))

    def test_opencode_tied_completion_watermark_does_not_drop_rows(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "opencode.db"
            conn = sqlite3.connect(db)
            conn.executescript("""
                CREATE TABLE session(id TEXT PRIMARY KEY, directory TEXT, title TEXT,
                                     parent_id TEXT);
                CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT,
                                     time_updated INTEGER, data TEXT);
                CREATE TABLE part(id TEXT PRIMARY KEY, message_id TEXT,
                                  time_created INTEGER, time_updated INTEGER, data TEXT);
            """)
            for i in range(201):
                sid, mid = f"s{i:04d}", f"m{i:04d}"
                conn.execute("INSERT INTO message VALUES (?,?,?,?)", (
                    mid, sid, 1000, json.dumps({
                        "role": "assistant", "time": {"completed": 1000 + i}
                    })))
            conn.commit()
            conn.close()

            watcher = LiveWatcher()
            watcher._poll_opencode_db(str(db), True)
            self.assertEqual(len(watcher._oc_done), 200)
            watcher._poll_opencode_db(str(db), False)
            self.assertEqual(len(watcher._oc_done), 201)
            self.assertEqual(watcher._oc_msg_wm[str(db)], (1000, "m0200"))

    def test_opencode_mutable_recheck_cannot_skip_part_backlog(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "opencode.db"
            conn = sqlite3.connect(db)
            conn.executescript("""
                CREATE TABLE session(id TEXT PRIMARY KEY, directory TEXT, title TEXT,
                                     parent_id TEXT);
                CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT,
                                     time_updated INTEGER, data TEXT);
                CREATE TABLE part(id TEXT PRIMARY KEY, message_id TEXT,
                                  time_created INTEGER, time_updated INTEGER, data TEXT);
            """)
            conn.execute("INSERT INTO session VALUES ('s','/work/agrep','title',NULL)")
            conn.execute("INSERT INTO message VALUES (?,?,?,?)", (
                "m-running", "s", 1000, json.dumps({"role": "assistant"})))
            conn.execute("INSERT INTO part VALUES (?,?,?,?,?)", (
                "p0000", "m-running", 1000, 1000, json.dumps({
                    "type": "tool", "tool": "shell", "callID": "p0000",
                    "state": {"status": "running"},
                })))
            conn.commit()

            watcher = LiveWatcher()
            watcher._poll_opencode_db(str(db), True)

            conn.execute("UPDATE part SET time_updated=?, data=? WHERE id='p0000'", (
                10_000, json.dumps({
                    "type": "tool", "tool": "shell", "callID": "p0000",
                    "state": {"status": "completed"},
                })))
            conn.execute("INSERT INTO message VALUES (?,?,?,?)", (
                "m-bulk", "s", 2900,
                json.dumps({"role": "assistant", "time": {"completed": 2900}})))
            for i in range(1, 901):
                ts = 1999 + i
                conn.execute("INSERT INTO part VALUES (?,?,?,?,?)", (
                    f"p{i:04d}", "m-bulk", ts, ts, json.dumps({
                        "type": "tool", "tool": "bulk", "callID": f"p{i:04d}",
                        "state": {"status": "completed"},
                    })))
            conn.commit()
            conn.close()

            watcher._poll_opencode_db(str(db), False)
            self.assertEqual(watcher._oc_watermark[str(db)], (2799, "p0800"))
            watcher._poll_opencode_db(str(db), False)
            self.assertTrue(all(f"p{i:04d}" in watcher._oc_part_st
                                for i in range(901)))

    def test_opencode_unfinished_recheck_cannot_skip_message_backlog(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "opencode.db"
            conn = sqlite3.connect(db)
            conn.executescript("""
                CREATE TABLE session(id TEXT PRIMARY KEY, directory TEXT, title TEXT,
                                     parent_id TEXT);
                CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT,
                                     time_updated INTEGER, data TEXT);
                CREATE TABLE part(id TEXT PRIMARY KEY, message_id TEXT,
                                  time_created INTEGER, time_updated INTEGER, data TEXT);
            """)
            conn.execute("INSERT INTO message VALUES (?,?,?,?)", (
                "m000", "s000", 1000,
                json.dumps({"role": "assistant", "time": {}})))
            conn.commit()

            watcher = LiveWatcher()
            watcher._poll_opencode_db(str(db), True)

            conn.execute("UPDATE message SET time_updated=?, data=? WHERE id='m000'", (
                10_000,
                json.dumps({"role": "assistant", "time": {"completed": 10_000}})))
            for i in range(1, 251):
                ts = 1999 + i
                conn.execute("INSERT INTO message VALUES (?,?,?,?)", (
                    f"m{i:03d}", f"s{i:03d}", ts,
                    json.dumps({"role": "assistant", "time": {"completed": ts}})))
            conn.commit()
            conn.close()

            watcher._poll_opencode_db(str(db), False)
            self.assertEqual(watcher._oc_msg_wm[str(db)], (2199, "m200"))
            watcher._poll_opencode_db(str(db), False)
            self.assertTrue(all(f"s{i:03d}" in watcher._oc_done
                                for i in range(251)))

    def test_opencode_rechecks_mutable_rows_behind_tied_watermark(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "opencode.db"
            conn = sqlite3.connect(db)
            conn.executescript("""
                CREATE TABLE session(id TEXT PRIMARY KEY, directory TEXT, title TEXT,
                                     parent_id TEXT);
                CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT,
                                     time_updated INTEGER, data TEXT);
                CREATE TABLE part(id TEXT PRIMARY KEY, message_id TEXT,
                                  time_created INTEGER, time_updated INTEGER, data TEXT);
            """)
            conn.execute("INSERT INTO session VALUES ('s','/work/agrep','title',NULL)")
            for mid in ("m-tool-a", "m-tool-z", "m-text-a", "m-text-z"):
                conn.execute("INSERT INTO message VALUES (?,?,?,?)", (
                    mid, "s", 1000, json.dumps({"role": "assistant"})))
            for pid, mid in (("p-a", "m-tool-a"), ("p-z", "m-tool-z")):
                conn.execute("INSERT INTO part VALUES (?,?,?,?,?)", (
                    pid, mid, 1000, 1000, json.dumps({
                        "type": "tool", "tool": "shell",
                        "state": {"status": "running", "input": {"command": pid}},
                    })))
            for pid, mid in (("q-a", "m-text-a"), ("q-z", "m-text-z")):
                conn.execute("INSERT INTO part VALUES (?,?,?,?,?)", (
                    pid, mid, 1000, 1000,
                    json.dumps({"type": "text", "text": f"partial {pid}"})))
            conn.commit()
            conn.close()

            watcher = LiveWatcher()
            watcher._poll_opencode_db(str(db), True)
            self.assertEqual(watcher._oc_watermark[str(db)], (1000, "q-z"))
            self.assertEqual(watcher._oc_msg_wm[str(db)], (1000, "m-tool-z"))

            conn = sqlite3.connect(db)
            conn.execute("UPDATE part SET data=? WHERE id='p-a'", (json.dumps({
                "type": "tool", "tool": "shell",
                "state": {"status": "completed", "output": "done"},
            }),))
            conn.execute("UPDATE part SET data=? WHERE id='q-a'", (json.dumps({
                "type": "text", "text": "complete reply",
            }),))
            conn.execute("UPDATE message SET data=? WHERE id='m-text-a'", (json.dumps({
                "role": "assistant", "time": {"completed": 1001},
            }),))
            conn.commit()
            conn.close()

            watcher._poll_opencode_db(str(db), False)
            shell = [event for event in watcher.ring
                     if event.get("name") == "shell" and event.get("call_id") == "p-a"]
            replies = [event.get("text") for event in watcher.ring
                       if event.get("type") == "reply"]
            self.assertEqual([event["type"] for event in shell], ["tool", "tool_done"])
            self.assertIn("complete reply", replies)
            self.assertEqual(watcher._oc_done.get("s"), 1001)
            self.assertFalse(watcher.sessions["opencode:s"]["working"])
            self.assertNotIn("p-a", watcher._oc_mutable_parts[str(db)])
            self.assertNotIn("q-a", watcher._oc_mutable_parts[str(db)])
            self.assertNotIn("m-text-a", watcher._oc_unfinished_messages[str(db)])

    def test_cursor_in_place_reply_updates_reach_live_feed(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "state.vscdb"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE cursorDiskKV(key TEXT PRIMARY KEY, value TEXT)")
            conn.execute("INSERT INTO cursorDiskKV VALUES (?,?)", (
                "composerData:c", json.dumps({
                    "fullConversationHeadersOnly": [{"bubbleId": "answer"}],
                    "generatingBubbleIds": ["answer"],
                })))
            conn.execute("INSERT INTO cursorDiskKV VALUES (?,?)", (
                "bubbleId:c:answer", json.dumps({
                    "type": 2, "text": "partial reply",
                    "createdAt": "1970-01-01T00:00:01Z",
                })))
            conn.commit()
            conn.close()

            watcher = LiveWatcher()
            watcher._poll_cursor_db(str(db), True, 1.0)
            conn = sqlite3.connect(db)
            conn.execute("UPDATE cursorDiskKV SET value=? WHERE key=?", (
                json.dumps({"type": 2, "text": "complete reply",
                            "createdAt": "1970-01-01T00:00:01Z"}),
                "bubbleId:c:answer"))
            conn.commit()
            conn.close()
            watcher._poll_cursor_db(str(db), False, 2.0)

            replies = [event["text"] for event in watcher.ring
                       if event.get("type") == "reply"]
            self.assertEqual(replies, ["partial reply", "complete reply"])


if __name__ == "__main__":
    unittest.main()
