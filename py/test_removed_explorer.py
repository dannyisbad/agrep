from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _test_support import isolate_data_dir

isolate_data_dir()
import legacy_cleanup


ROOT = Path(__file__).resolve().parents[1]


class RemovedExplorerTests(unittest.TestCase):
    def setUp(self) -> None:
        legacy_cleanup._REMOVED_EXPLORER_CHECKED = False

    def test_exact_old_owner_is_retired(self) -> None:
        path = legacy_cleanup.DATA_DIR / ".server"
        path.write_text(json.dumps({"pid": 123, "port": 8732,
                                    "process_start": "birth", "mode": "explorer"}),
                        encoding="utf-8")
        with mock.patch.object(legacy_cleanup, "pid_alive", return_value=True), \
                mock.patch.object(legacy_cleanup, "process_start_identity",
                                  return_value="birth"), \
                mock.patch.object(legacy_cleanup, "terminate_exact_process",
                                  return_value=True) as stop, \
                contextlib.redirect_stderr(io.StringIO()):
            legacy_cleanup.retire_removed_explorer()
        stop.assert_called_once_with(123, "birth")
        self.assertFalse(path.exists())

    def test_recycled_pid_is_not_signalled(self) -> None:
        path = legacy_cleanup.DATA_DIR / ".server"
        path.write_text(json.dumps({"pid": 123, "port": 8732,
                                    "process_start": "old", "mode": "explorer"}),
                        encoding="utf-8")
        with mock.patch.object(legacy_cleanup, "pid_alive", return_value=True), \
                mock.patch.object(legacy_cleanup, "process_start_identity",
                                  return_value="new"), \
                mock.patch.object(legacy_cleanup, "terminate_exact_process") as stop:
            legacy_cleanup.retire_removed_explorer()
        stop.assert_not_called()
        self.assertFalse(path.exists())

    def test_dead_owner_descriptor_is_cleaned(self) -> None:
        path = legacy_cleanup.DATA_DIR / ".server"
        path.write_text(json.dumps({"pid": 123, "port": 8732,
                                    "process_start": "birth", "mode": "explorer"}),
                        encoding="utf-8")
        with mock.patch.object(legacy_cleanup, "pid_alive", return_value=False), \
                mock.patch.object(legacy_cleanup, "terminate_exact_process") as stop:
            legacy_cleanup.retire_removed_explorer()
        stop.assert_not_called()
        self.assertFalse(path.exists())

    def test_legacy_owner_needs_command_and_http_fingerprints(self) -> None:
        path = legacy_cleanup.DATA_DIR / ".server"
        body = json.dumps({"pid": 123, "port": 8732})
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"unrelated":true}'
        path.write_text(body, encoding="utf-8")
        with mock.patch.object(legacy_cleanup, "WIN", False), \
                mock.patch.object(legacy_cleanup, "pid_alive", return_value=True), \
                mock.patch.object(legacy_cleanup, "process_start_identity",
                                  return_value="birth"), \
                mock.patch.object(legacy_cleanup.subprocess, "run",
                                  return_value=mock.Mock(
                                      stdout="python /opt/agrep/py/server.py")), \
                mock.patch("urllib.request.urlopen", return_value=response), \
                mock.patch.object(legacy_cleanup.os, "kill") as kill, \
                contextlib.redirect_stderr(io.StringIO()):
            legacy_cleanup.retire_removed_explorer()
        kill.assert_not_called()
        self.assertEqual(path.read_text(encoding="utf-8"), body)

    def test_verified_legacy_owner_is_retired(self) -> None:
        path = legacy_cleanup.DATA_DIR / ".server"
        path.write_text(json.dumps({"pid": 123, "port": 8732}), encoding="utf-8")
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = (
            b'{"semantic":"off","indexer":{"phase":"idle"}}')
        with mock.patch.object(legacy_cleanup, "WIN", False), \
                mock.patch.object(legacy_cleanup, "pid_alive", return_value=True), \
                mock.patch.object(legacy_cleanup, "process_start_identity",
                                  return_value="birth"), \
                mock.patch.object(legacy_cleanup.subprocess, "run",
                                  return_value=mock.Mock(
                                      stdout="python /opt/agrep/py/server.py")), \
                mock.patch("urllib.request.urlopen", return_value=response), \
                mock.patch.object(legacy_cleanup, "terminate_exact_process",
                                  return_value=True) as stop, \
                contextlib.redirect_stderr(io.StringIO()):
            legacy_cleanup.retire_removed_explorer()
        stop.assert_called_once_with(123, "birth")
        self.assertFalse(path.exists())

    def test_verified_legacy_owner_rechecks_birth_before_signalling(self) -> None:
        path = legacy_cleanup.DATA_DIR / ".server"
        body = json.dumps({"pid": 123, "port": 8732})
        path.write_text(body, encoding="utf-8")
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = (
            b'{"semantic":"off","indexer":{"phase":"idle"}}')
        with mock.patch.object(legacy_cleanup, "WIN", False), \
                mock.patch.object(legacy_cleanup, "pid_alive", return_value=True), \
                mock.patch.object(legacy_cleanup, "process_start_identity",
                                  side_effect=("birth", "recycled")), \
                mock.patch.object(legacy_cleanup.subprocess, "run",
                                  return_value=mock.Mock(
                                      stdout="python /opt/agrep/py/server.py")), \
                mock.patch("urllib.request.urlopen", return_value=response), \
                mock.patch.object(legacy_cleanup.os, "kill") as kill, \
                contextlib.redirect_stderr(io.StringIO()):
            legacy_cleanup.retire_removed_explorer()
        kill.assert_not_called()
        self.assertEqual(path.read_text(encoding="utf-8"), body)

    def test_descriptor_compare_and_swap_does_not_delete_replacement(self) -> None:
        path = legacy_cleanup.DATA_DIR / ".server"
        path.write_text(json.dumps({"pid": 123, "port": 8732,
                                    "process_start": "birth", "mode": "explorer"}),
                        encoding="utf-8")
        replacement = json.dumps({"pid": 456, "port": 9000,
                                  "process_start": "new", "mode": "explorer"})

        def replace_then_stop(_pid, _birth):
            path.write_text(replacement, encoding="utf-8")
            return True

        with mock.patch.object(legacy_cleanup, "pid_alive", return_value=True), \
                mock.patch.object(legacy_cleanup, "process_start_identity",
                                  return_value="birth"), \
                mock.patch.object(legacy_cleanup, "terminate_exact_process",
                                  side_effect=replace_then_stop), \
                contextlib.redirect_stderr(io.StringIO()):
            legacy_cleanup.retire_removed_explorer()
        self.assertEqual(path.read_text(encoding="utf-8"), replacement)

    def test_explorer_verbs_have_command_help(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env = {**dict(os.environ), "AGREP_DATA_DIR": td,
                   "AGREP_DATA_DIR_SOURCE": "env"}
            for verb in ("ui", "up", "serve"):
                proc = subprocess.run(
                    [sys.executable, str(ROOT / "cli.py"), verb, "--help"],
                    cwd=ROOT, env=env, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=15)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("private read-only history explorer", proc.stdout)
                self.assertIn("live Board", proc.stdout)
                self.assertIn("examples:", proc.stdout)
                public = "serve" if verb == "serve" else "ui"
                self.assertIn(f"agrep {public}", proc.stdout)

    def test_top_level_usage_lists_both_explorer_commands(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            proc = subprocess.run(
                [sys.executable, str(ROOT / "cli.py"), "--help"],
                cwd=ROOT, env={
                    **dict(os.environ), "AGREP_DATA_DIR": td,
                    "AGREP_DATA_DIR_SOURCE": "env",
                }, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        usage = "\n".join(proc.stdout.splitlines()[:4])
        self.assertIn("ui", usage)
        self.assertIn("serve", usage)
        self.assertIn("live              tail, board, ui, serve, run", proc.stdout)

    def test_explorer_verbs_never_fall_through_to_search(self) -> None:
        sys.path.insert(0, str(ROOT))
        import cli
        for verb in ("ui", "up", "serve"):
            with self.subTest(verb=verb), \
                    mock.patch.object(legacy_cleanup, "retire_removed_explorer"), \
                    mock.patch.object(cli, "cmd_explorer", return_value=2) as explorer, \
                    mock.patch.object(cli, "cmd_search") as search_cmd, \
                    mock.patch.object(sys, "argv", ["agrep", verb, "deadlock"]):
                self.assertEqual(cli._main(), 2)
            self.assertEqual(explorer.call_args.args[0].rest, ["deadlock"])
            self.assertEqual(
                explorer.call_args.kwargs["open_browser"], verb != "serve")
            search_cmd.assert_not_called()

    def test_explicit_search_can_still_query_explorer_command_words(self) -> None:
        sys.path.insert(0, str(ROOT))
        import cli
        for verb in ("ui", "up", "serve"):
            with self.subTest(verb=verb), \
                    mock.patch.object(legacy_cleanup, "retire_removed_explorer"), \
                    mock.patch.object(cli, "cmd_search", return_value=17) as search_cmd, \
                    mock.patch.object(sys, "argv", ["agrep", "search", verb]):
                self.assertEqual(cli._main(), 17)
            self.assertEqual(search_cmd.call_args.args[0].rest, [verb])


if __name__ == "__main__":
    unittest.main()
