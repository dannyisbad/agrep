"""Ownership, publication, and scheduler regressions for agent setup."""

from __future__ import annotations

import ast
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

from _test_support import isolate_data_dir

isolate_data_dir()
import teach  # noqa: E402


class TeachSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.data = self.root / "data"
        self.home.mkdir()
        self.data.mkdir()
        self.saved = {name: getattr(teach, name) for name in (
            "HOME", "REPO", "STATE_PATH", "MD_TARGETS", "SKILL_TARGETS")}
        self.saved_data = teach.common.DATA_DIR
        teach.HOME = self.home
        teach.REPO = self.root / "repo"
        teach.REPO.mkdir()
        teach.common.DATA_DIR = self.data
        teach.STATE_PATH = self.data / "teach.json"
        teach.MD_TARGETS = []
        teach.SKILL_TARGETS = []

    def tearDown(self) -> None:
        for name, value in self.saved.items():
            setattr(teach, name, value)
        teach.common.DATA_DIR = self.saved_data
        self.temp.cleanup()

    def _assert_phrase(self, needle: str, block: str) -> None:
        """Assert a phrase is present ignoring line-wrap position.

        The block is width-wrapped, so a phrase that fits on one line today
        can straddle a newline after any reword. Normalizing whitespace on
        both sides keeps these content assertions about words, not columns -
        they stopped failing on every rewrap once this replaced raw assertIn.
        """
        norm = lambda s: " ".join(s.split())
        self.assertIn(norm(needle), norm(block))

    def _install_win_calls(self, register_rc: int) -> list[list[str]]:
        """Run _sentinel_install_win with subprocess mocked; return argvs."""
        calls: list[list[str]] = []

        def fake_run(argv, **_kwargs):
            calls.append(list(argv))
            rc = register_rc if argv[0] == "powershell" else 0
            return subprocess.CompletedProcess(argv, rc, stdout="", stderr="")

        with mock.patch.object(teach.subprocess, "run",
                               side_effect=fake_run), \
                mock.patch.object(teach.subprocess, "Popen"), \
                mock.patch.object(teach.common,
                                  "windows_background_child_flags",
                                  return_value=0), \
                mock.patch.object(teach, "sentinel_armed",
                                  return_value=True):
            self.assertTrue(teach._sentinel_install_win([]))
        return calls

    def test_win_sentinel_registers_unelevated_before_schtasks(self) -> None:
        # schtasks /SC ONLOGON is denied without elevation (the npm
        # postinstall report): the COM path must lead, and its task must run
        # the same watcher the schtasks fallback would.
        calls = self._install_win_calls(register_rc=0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:4], [
            "powershell", "-NoProfile", "-NonInteractive", "-Command"])
        script = calls[0][4]
        self.assertIn("Register-ScheduledTask", script)
        self.assertIn("-AtLogOn", script)
        self.assertIn(teach.TASK_NAME, script)
        self.assertIn("sentinel_watch.py", script)

    def test_win_sentinel_falls_back_to_schtasks_on_register_failure(
            self) -> None:
        calls = self._install_win_calls(register_rc=1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], "powershell")
        self.assertEqual(calls[1][:5], [
            "schtasks", "/Create", "/F", "/TN", teach.TASK_NAME])
        self.assertIn("/SC", calls[1])
        self.assertIn("ONLOGON", calls[1])

    def test_sentinel_interpreter_prefers_the_base_executable(self) -> None:
        # A uv tool-run venv shim dies with `uv cache clean`; the logon task
        # must reference the managed base interpreter that survives it.
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "base" / "python.exe"
            base.parent.mkdir()
            base.write_bytes(b"")
            with mock.patch.object(teach.sys, "_base_executable",
                                   str(base), create=True):
                self.assertEqual(teach._pythonw(), str(base))
            basew = base.with_name("pythonw.exe")
            basew.write_bytes(b"")
            with mock.patch.object(teach.sys, "_base_executable",
                                   str(base), create=True):
                self.assertEqual(teach._pythonw(), str(basew))
        # a dangling base falls back to the running interpreter
        with mock.patch.object(teach.sys, "_base_executable",
                               str(Path("/nonexistent/python.exe")),
                               create=True):
            self.assertTrue(Path(teach._pythonw()).exists())

    def test_compaction_handoff_is_an_explicit_recall_trigger(self) -> None:
        # The claude block must keep the same-session recovery route: the
        # measured failure is a resumed agent reaching for recall out of
        # habit, so the boundary and the override must both be stated.
        block = teach._block(self.home / ".claude" / "CLAUDE.md")
        for phrase in (
            "`agrep postcompact`",
            "before a compaction",
            "Same-session recovery means postcompact",
            "`--self` overrides",
        ):
            self._assert_phrase(phrase, block)

    def test_missing_artifact_and_machine_output_routes_are_explicit(self) -> None:
        # The artifact route stays inline (the moment of need is mid-task);
        # flag-level machine-output detail is deliberately delegated to the
        # always-current per-command help instead of the block.
        block = teach._block(self.home / ".codex" / "AGENTS.md")
        for phrase in (
            "absent after one bounded filesystem lookup",
            "before concluding it is gone",
            "Run `agrep --help` once before first use",
            "`agrep <command> --help`",
        ):
            self._assert_phrase(phrase, block)

    def test_frontier_routing_separates_indexed_history_from_live_state(self) -> None:
        # Indexed history vs live activity is a routing decision both blocks
        # must carry; each speaks its own dialect.
        codex = teach._block(self.home / ".codex" / "AGENTS.md")
        for phrase in (
            "`agrep board --once` - live agent activity right now",
            "recent-history questions are `chats`",
            "`agrep recall "'"'"<distinctive phrase>"'"'"`",
        ):
            self._assert_phrase(phrase, codex)
        claude = teach._block(self.home / ".claude" / "CLAUDE.md")
        for phrase in (
            "running/active right now means `board --once`",
            "latest sessions means `chats` (indexed history, newest first)",
            "live agent activity on this box right now",
        ):
            self._assert_phrase(phrase, claude)

    def test_existing_unowned_cursor_skill_is_refused_byte_for_byte(self) -> None:
        proof = self.home / ".cursor"
        skill = proof / "skills" / "agrep-recall" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        original = "---\nname: my-own-skill\n---\nnever overwrite this\n"
        skill.write_text(original, encoding="utf-8")
        teach.SKILL_TARGETS = [("cursor", proof, skill)]
        with mock.patch.object(teach, "_sentinel_install") as install, \
                mock.patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(teach._install(), 1)
        self.assertEqual(skill.read_text(encoding="utf-8"), original)
        self.assertFalse(teach.STATE_PATH.exists())
        install.assert_not_called()

    def test_empty_or_malformed_receipt_does_not_bypass_consent(self) -> None:
        proof = self.home / ".codex"
        proof.mkdir()
        target = proof / "AGENTS.md"
        teach.MD_TARGETS = [("codex", proof, target)]
        receipts = (
            b"{}",
            json.dumps({
                "version": 2, "targets": [], "skills": [], "removing": [],
            }).encode("utf-8"),
            b"{not-json",
        )
        for raw in receipts:
            with self.subTest(receipt=raw):
                teach.STATE_PATH.write_bytes(raw)
                with mock.patch.object(
                        teach, "_consent", return_value=False) as consent, \
                        mock.patch.object(teach, "_install") as install:
                    self.assertEqual(teach.teach(), 0)
                consent.assert_called_once()
                install.assert_not_called()

    def test_dangling_dotfile_symlink_is_reported_without_traceback(self) -> None:
        proof = self.home / ".codex"
        proof.mkdir()
        target = proof / "AGENTS.md"
        try:
            target.symlink_to(self.root / "missing.md")
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        teach.MD_TARGETS = [("codex", proof, target)]
        output = io.StringIO()
        with mock.patch("sys.stdout", new=output):
            self.assertEqual(teach._install(), 1)
        self.assertTrue(target.is_symlink())
        self.assertIn("dangling symlink", output.getvalue())
        self.assertFalse(teach.STATE_PATH.exists())

    def test_remove_preserves_skill_additions_and_symlinked_dotfile(self) -> None:
        skill = self.home / ".cursor" / "skills" / "agrep-recall" / "SKILL.md"
        self.assertEqual(teach._write_skill(skill), "added")
        skill.write_text(skill.read_text(encoding="utf-8") + "my custom rule\n",
                         encoding="utf-8")
        addition = skill.parent / "notes.txt"
        addition.write_text("mine", encoding="utf-8")
        self.assertTrue(teach._remove_skill(skill))
        self.assertEqual(skill.read_text(encoding="utf-8"),
                         teach._SKILL_FRONT + "my custom rule\n")
        self.assertEqual(addition.read_text(encoding="utf-8"), "mine")

        exact = self.home / ".cursor" / "skills" / "agrep-owned" / "SKILL.md"
        self.assertEqual(teach._write_skill(exact), "added")
        neighbor = exact.parent / "user.txt"
        neighbor.write_text("mine", encoding="utf-8")
        self.assertTrue(teach._remove_skill(exact))
        self.assertFalse(exact.exists())
        self.assertEqual(neighbor.read_text(encoding="utf-8"), "mine")

        target = self.root / "dotfiles" / "AGENTS.md"
        target.parent.mkdir()
        target.write_text("my rules\n", encoding="utf-8")
        link = self.home / "AGENTS.md"
        try:
            link.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        teach._write_block(link)
        self.assertTrue(link.is_symlink())
        self.assertIn(teach.MARK_PREFIX, target.read_text(encoding="utf-8"))
        self.assertTrue(teach._remove_block(link))
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8").strip(), "my rules")

    def test_malformed_markers_refuse_every_mutation_without_span_swallow(self) -> None:
        proof = self.home / ".codex"
        proof.mkdir()
        target = proof / "AGENTS.md"
        original = (
            f"{teach.MARK_PREFIX} v1 -->\r\nold block lost its end\r\n"
            "USER CONTENT\r\n"
            f"{teach.MARK_PREFIX} v1 -->\r\nvalid later block\r\n"
            f"{teach.MARK_END}\r\nTAIL\r\n"
        ).encode("utf-8")
        target.write_bytes(original)
        teach.MD_TARGETS = [("codex", proof, target)]
        teach._save_state([target])

        with mock.patch.object(teach, "_sentinel_install") as sentinel, \
                mock.patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(teach._install(), 1)
        sentinel.assert_not_called()
        self.assertEqual(target.read_bytes(), original)
        with self.assertRaisesRegex(teach.BlockStructureError, "nested"):
            teach._remove_block(target)
        self.assertEqual(teach.reconcile(), [])
        self.assertEqual(target.read_bytes(), original)

    def test_mixed_legacy_and_future_blocks_are_never_rewritten(self) -> None:
        target = self.home / ".codex" / "AGENTS.md"
        target.parent.mkdir()
        original = (
            "USER\n"
            f"{teach.MARK_PREFIX} v1 -->\nlegacy custom body\n{teach.MARK_END}\n"
            f"{teach.MARK_PREFIX} v{teach.NUDGE_V + 1} -->\n"
            f"future custom body\n{teach.MARK_END}\n"
            "TAIL\n"
        ).encode("utf-8")
        target.write_bytes(original)
        teach.MD_TARGETS = [("codex", target.parent, target)]
        teach._save_state([target])

        self.assertEqual(teach._write_block(target), "kept")
        self.assertEqual(teach.reconcile(), [])
        self.assertEqual(target.read_bytes(), original)

        skill = self.home / ".cursor" / "skills" / "agrep-recall" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_bytes(original)
        teach.SKILL_TARGETS = [("cursor", skill.parents[2], skill)]
        teach._save_state([skill], [skill])
        self.assertEqual(teach._write_skill(skill), "kept")
        self.assertEqual(teach.reconcile(), [])
        self.assertEqual(skill.read_bytes(), original)

    def test_duplicate_marker_normalization_preserves_host_bytes(self) -> None:
        target = self.home / ".codex" / "AGENTS.md"
        target.parent.mkdir()
        current = teach._block(target).replace("\n", "\r\n").encode("utf-8")
        legacy = (
            f"{teach.MARK_PREFIX} v1 -->\r\nlegacy\r\n"
            f"{teach.MARK_END}\r\n"
        ).encode("utf-8")
        prefix = b"USER PREFIX\r\n"
        between = b"USER BETWEEN\t\r\n"
        suffix = b"USER TAIL  \r\n"
        cases = (
            (
                prefix + legacy + between + current + suffix,
                prefix + between + current + suffix,
            ),
            (
                prefix + current + between + current + suffix,
                prefix + current + between + suffix,
            ),
        )
        for original, expected in cases:
            with self.subTest(original=original):
                target.write_bytes(original)
                self.assertEqual(teach._write_block(target), "updated")
                self.assertEqual(target.read_bytes(), expected)

    def test_reconcile_removes_legacy_beside_current_without_rewriting_current(
            self) -> None:
        legacy = (
            f"{teach.MARK_PREFIX} v1 -->\r\nlegacy\r\n{teach.MARK_END}\r\n"
        ).encode("utf-8")
        current = (
            f"{teach.MARK_BEGIN}\r\ncurrent user edit  \r\n"
            f"{teach.MARK_END}\r\n"
        ).encode("utf-8")
        separator = b"USER BETWEEN\r\n"
        suffix = b"USER TAIL\t\r\n"
        md_proof = self.home / ".codex"
        md_proof.mkdir()
        target = md_proof / "AGENTS.md"
        prefix = b"USER PREFIX\r\n"
        target.write_bytes(prefix + legacy + separator + current + suffix)

        proof = self.home / ".cursor"
        proof.mkdir()
        skill = proof / "skills" / "agrep-recall" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        front = teach._SKILL_FRONT.replace("\n", "\r\n").encode("utf-8")
        skill.write_bytes(front + legacy + separator + current + suffix)
        teach.MD_TARGETS = [("codex", md_proof, target)]
        teach.SKILL_TARGETS = [("cursor", proof, skill)]
        teach._save_state([target, skill], [skill])

        self.assertEqual(teach.reconcile(), [])
        self.assertEqual(
            target.read_bytes(), prefix + legacy + separator + current + suffix)
        self.assertEqual(
            skill.read_bytes(), front + legacy + separator + current + suffix)
        self.assertEqual(
            [item["kind"] for item in
             teach.current_reconcile_health()["refusals"]],
            ["drifted", "drifted"])

    def test_reconcile_names_an_owned_legacy_version_without_rewriting_it(
            self) -> None:
        target = self.home / ".codex" / "AGENTS.md"
        target.parent.mkdir()
        original = (
            "USER\n"
            f"{teach.MARK_PREFIX} v24 -->\nold block\n{teach.MARK_END}\n"
        ).encode("utf-8")
        target.write_bytes(original)
        teach.MD_TARGETS = [("codex", target.parent, target)]
        teach._save_state([target])

        self.assertEqual(teach.reconcile(), [])
        self.assertEqual(target.read_bytes(), original)
        refusal = teach.current_reconcile_health()["refusals"][0]
        self.assertEqual(refusal["kind"], "drifted")
        self.assertEqual(
            refusal["reason"],
            f"installed agrep block v24; this build teaches "
            f"v{teach.NUDGE_V}; run agrep setup")

    def test_explicit_setup_upgrades_legacy_block_and_preserves_host_bytes(
            self) -> None:
        proof = self.home / ".codex"
        proof.mkdir()
        target = proof / "AGENTS.md"
        prefix = b"USER PREFIX\r\n"
        suffix = b"USER SUFFIX\t\r\n"
        old = (
            f"{teach.MARK_PREFIX} v24 -->\r\nold\r\n"
            f"{teach.MARK_END}\r\n"
        ).encode("utf-8")
        target.write_bytes(prefix + old + suffix)
        teach.MD_TARGETS = [("codex", proof, target)]
        output = io.StringIO()

        with mock.patch.object(
                teach, "_sentinel_install", return_value=True), \
                mock.patch("sys.stdout", new=output):
            self.assertEqual(teach._install(), 0)

        upgraded = target.read_bytes()
        self.assertTrue(upgraded.startswith(prefix))
        self.assertTrue(upgraded.endswith(suffix))
        self.assertEqual(upgraded.count(teach.MARK_BEGIN.encode()), 1)
        self.assertNotIn(f"{teach.MARK_PREFIX} v24 -->".encode(), upgraded)
        self.assertIn(
            f"updated block v24 -> v{teach.NUDGE_V}", output.getvalue())

    def test_reconcile_collapses_duplicate_current_blocks_byte_for_byte(
            self) -> None:
        first = (
            f"{teach.MARK_BEGIN}\r\nfirst user edit  \r\n"
            f"{teach.MARK_END}\r\n"
        ).encode("utf-8")
        duplicate = (
            f"{teach.MARK_BEGIN}\r\nredundant copy\r\n"
            f"{teach.MARK_END}\r\n"
        ).encode("utf-8")
        separator = b"USER BETWEEN\r\n"
        suffix = b"USER TAIL\t\r\n"
        proof = self.home / ".codex"
        proof.mkdir()
        target = proof / "AGENTS.md"
        prefix = b"USER PREFIX\r\n"
        target.write_bytes(prefix + first + separator + duplicate + suffix)

        skill_proof = self.home / ".cursor"
        skill_proof.mkdir()
        skill = skill_proof / "skills" / "agrep-recall" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        front = teach._SKILL_FRONT.replace("\n", "\r\n").encode("utf-8")
        skill.write_bytes(front + first + separator + duplicate + suffix)
        teach.MD_TARGETS = [("codex", proof, target)]
        teach.SKILL_TARGETS = [("cursor", skill_proof, skill)]
        teach._save_state([target, skill], [skill])

        self.assertEqual(teach.reconcile(), [])
        self.assertEqual(
            target.read_bytes(), prefix + first + separator + duplicate + suffix)
        self.assertEqual(
            skill.read_bytes(),
            front + first + separator + duplicate + suffix)
        self.assertEqual(
            [item["kind"] for item in
             teach.current_reconcile_health()["refusals"]],
            ["drifted", "drifted"])

    def test_reconcile_preserves_an_edit_between_snapshot_and_replace(self) -> None:
        target = self.home / ".codex" / "AGENTS.md"
        target.parent.mkdir()
        target.write_text(
            f"USER\n{teach.MARK_PREFIX} v1 -->\nold\n{teach.MARK_END}\n",
            encoding="utf-8")
        teach.MD_TARGETS = [("codex", target.parent, target)]
        teach._save_state([target])
        user_edit = b"NEW USER EDIT\n"
        snapshot = teach.ownerfile.snapshot(target)

        def race(_path):
            target.write_bytes(user_edit)
            return snapshot

        with mock.patch.object(
                teach, "_read_reconcile_target", side_effect=race), \
                mock.patch.object(teach, "_write_block") as write:
            self.assertEqual(teach.reconcile(), [])
        write.assert_not_called()
        self.assertEqual(target.read_bytes(), user_edit)
        health = teach.current_reconcile_health()
        self.assertEqual(health["state"], "refused")
        self.assertEqual(health["refusals"][0]["kind"], "drifted")

    def test_reconcile_never_replaces_a_newly_created_target(self) -> None:
        target = self.home / ".codex" / "AGENTS.md"
        target.parent.mkdir()
        teach.MD_TARGETS = [("codex", target.parent, target)]
        teach._save_state([target])
        user_edit = b"NEW USER FILE\n"

        def race(_path):
            target.write_bytes(user_edit)
            return None

        with mock.patch.object(
                teach, "_read_reconcile_target", side_effect=race):
            self.assertEqual(teach.reconcile(), [])
        self.assertEqual(target.read_bytes(), user_edit)
        health = teach.current_reconcile_health()
        self.assertEqual(health["state"], "refused")
        self.assertEqual(health["refusals"][0]["kind"], "concurrent-edit")

    def test_fresh_non_utf8_target_is_not_enrolled(self) -> None:
        proof = self.home / ".codex"
        proof.mkdir()
        target = proof / "AGENTS.md"
        original = b"user bytes \xff\n"
        target.write_bytes(original)
        teach.MD_TARGETS = [("codex", proof, target)]

        output = io.StringIO()
        with mock.patch.object(teach, "_sentinel_install") as sentinel, \
                mock.patch("sys.stdout", new=output):
            self.assertEqual(teach._install(), 1)

        self.assertEqual(target.read_bytes(), original)
        self.assertFalse(teach.STATE_PATH.exists())
        self.assertIn("not valid UTF-8", output.getvalue())
        sentinel.assert_not_called()

    def test_reinstall_keeps_malformed_target_enrolled_with_healthy_peer(self) -> None:
        codex = self.home / ".codex"
        claude = self.home / ".claude"
        codex.mkdir()
        claude.mkdir()
        malformed = codex / "AGENTS.md"
        healthy = claude / "CLAUDE.md"
        original = (
            f"{teach.MARK_PREFIX} v1 -->\ncustom body without an end\n"
        ).encode("utf-8")
        malformed.write_bytes(original)
        teach._write_block(healthy)
        teach.MD_TARGETS = [
            ("codex", codex, malformed),
            ("claude", claude, healthy),
        ]
        teach._save_state([malformed, healthy])

        with mock.patch.object(teach, "_sentinel_install", return_value=True) as sentinel, \
                mock.patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(teach._install(), 1)

        state = json.loads(teach.STATE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(set(state["targets"]), {str(malformed), str(healthy)})
        self.assertEqual(malformed.read_bytes(), original)
        sentinel.assert_called_once()
        self.assertEqual(set(map(str, sentinel.call_args.args[0])),
                         {str(malformed), str(healthy)})

    def _codex_sentinel(self, owned: bool) -> tuple[Path, Path]:
        """Seed ~/.codex/hooks.json and build a sentinel with no scheduler
        tail, so the test executes the cleanup body and nothing else."""
        import hookinstall
        (self.home / ".codex").mkdir()
        hooks = self.home / ".codex" / "hooks.json"
        if owned:
            hookinstall.install_codex_hooks(warn=False)
        else:
            hooks.write_text('{"hooks": {"SessionStart": []}, "mine": 1}\n',
                             encoding="utf-8")
        (teach.REPO / "cli.py").write_text("# cli\n", encoding="utf-8")
        script = teach._write_sentinel_sh("", teach._sh_subs([]))
        script.write_text(
            script.read_text(encoding="utf-8").replace("sleep 20", "sleep 1"),
            encoding="utf-8")
        return hooks, script

    def test_sentinel_snapshots_only_an_agrep_owned_codex_hook(self) -> None:
        hooks, script = self._codex_sentinel(owned=True)
        snapshot = self.data / "sentinel_codex_hooks"
        self.assertEqual(snapshot.read_bytes(), hooks.read_bytes())
        self.assertIn(str(hooks), script.read_text(encoding="utf-8"))

    def test_sentinel_never_snapshots_a_user_owned_codex_hook(self) -> None:
        # No snapshot and no path in the script: the sentinel is structurally
        # incapable of deleting a hooks.json agrep did not write.
        hooks, script = self._codex_sentinel(owned=False)
        self.assertFalse((self.data / "sentinel_codex_hooks").exists())
        self.assertNotIn(str(hooks), script.read_text(encoding="utf-8"))

    @unittest.skipIf(subprocess.os.name == "nt", "the sh sentinel is POSIX")
    def test_sentinel_removes_the_codex_hook_only_after_agrep_is_gone(
            self) -> None:
        hooks, script = self._codex_sentinel(owned=True)

        def fire() -> None:
            subprocess.run(["/bin/sh", str(script)], capture_output=True,
                           timeout=30)

        fire()
        self.assertTrue(hooks.is_file(), "fired while agrep was still installed")
        (teach.REPO / "cli.py").unlink()
        fire()
        self.assertFalse(hooks.exists(), "stranded the codex hook")

    @unittest.skipIf(subprocess.os.name == "nt", "the sh sentinel is POSIX")
    def test_sentinel_leaves_a_codex_hook_edited_after_install(self) -> None:
        # The snapshot exists but no longer matches: the file became the
        # user's between setup and uninstall, so it is not ours to delete.
        hooks, script = self._codex_sentinel(owned=True)
        hooks.write_text(hooks.read_text(encoding="utf-8") + "\n",
                         encoding="utf-8")
        (teach.REPO / "cli.py").unlink()
        subprocess.run(["/bin/sh", str(script)], capture_output=True, timeout=30)
        self.assertTrue(hooks.is_file())

    def test_remove_reports_malformed_target_and_keeps_sentinel(self) -> None:
        proof = self.home / ".codex"
        proof.mkdir()
        target = proof / "AGENTS.md"
        original = (
            f"{teach.MARK_PREFIX} v1 -->\r\nUSER CONTENT\r\n"
        ).encode("utf-8")
        target.write_bytes(original)
        teach.MD_TARGETS = [("codex", proof, target)]
        teach._save_state([target])
        fence = mock.Mock()
        output = io.StringIO()
        with mock.patch.object(
                teach.removal_fence, "acquire_background_removal_fence",
                return_value=fence), \
                mock.patch.object(
                    teach.removal_fence, "finish_background_removal_fence",
                    return_value=True), \
                mock.patch.object(teach, "_stop_daemons", return_value=True), \
                mock.patch.object(teach, "_sentinel_remove") as sentinel, \
                mock.patch("sys.stdout", new=output):
            self.assertEqual(teach._remove(), 1)
        self.assertEqual(target.read_bytes(), original)
        # audit B9: the failed target keeps its enrollment record so a retry
        # still sees the work instead of reporting a clean removal
        state = json.loads(teach.STATE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(state["targets"], [str(target)])
        self.assertIn("begin marker has no end marker", output.getvalue())
        sentinel.assert_not_called()

    def test_remove_fails_and_preserves_state_when_scheduler_remains(self) -> None:
        proof = self.home / ".codex"
        proof.mkdir()
        target = proof / "AGENTS.md"
        teach.MD_TARGETS = [("codex", proof, target)]
        teach._write_block(target)
        teach._save_state([target])
        artifact = self.data / "sentinel.sh"
        artifact.write_text("owned", encoding="utf-8")
        fence = mock.Mock()
        output = io.StringIO()
        with mock.patch.object(
                teach.removal_fence, "acquire_background_removal_fence",
                return_value=fence), \
                mock.patch.object(
                    teach.removal_fence, "finish_background_removal_fence",
                    return_value=True), \
                mock.patch.object(teach, "_stop_daemons", return_value=True), \
                mock.patch.object(teach, "_sentinel_remove", return_value=False), \
                mock.patch("sys.stdout", new=output):
            self.assertEqual(teach._remove(), 1)
        self.assertTrue(teach.STATE_PATH.is_file())
        self.assertTrue(artifact.is_file())
        self.assertIn("could not be deregistered", output.getvalue())

    def test_macos_sentinel_artifacts_survive_failed_deregistration(self) -> None:
        plist = teach._plist_path()
        plist.parent.mkdir(parents=True)
        plist.write_text("owned", encoding="utf-8")
        artifact = self.data / "sentinel.sh"
        artifact.write_text("owned", encoding="utf-8")
        outcomes = [
            mock.Mock(returncode=1, stdout="", stderr="failed"),
            mock.Mock(returncode=1, stdout="", stderr="failed"),
            mock.Mock(returncode=0, stdout="active", stderr=""),
        ]
        with mock.patch.object(teach.sys, "platform", "darwin"), \
                mock.patch.object(
                    teach.os, "getuid", return_value=501, create=True), \
                mock.patch.object(teach.subprocess, "run", side_effect=outcomes):
            self.assertFalse(teach._sentinel_remove())
        self.assertTrue(plist.is_file())
        self.assertTrue(artifact.is_file())

    def test_macos_sentinel_removal_requires_absence_probe(self) -> None:
        plist = teach._plist_path()
        plist.parent.mkdir(parents=True)
        plist.write_text("owned", encoding="utf-8")
        artifact = self.data / "sentinel.sh"
        artifact.write_text("owned", encoding="utf-8")
        outcomes = [
            mock.Mock(returncode=0, stdout="", stderr=""),
            mock.Mock(returncode=1, stdout="", stderr="not loaded"),
            mock.Mock(returncode=1, stdout="", stderr="not found"),
        ]
        with mock.patch.object(teach.sys, "platform", "darwin"), \
                mock.patch.object(
                    teach.os, "getuid", return_value=501, create=True), \
                mock.patch.object(teach.subprocess, "run", side_effect=outcomes):
            self.assertTrue(teach._sentinel_remove())
        self.assertFalse(plist.exists())
        self.assertFalse(artifact.exists())

    def test_linux_never_armed_sentinel_has_a_clean_remove_path(self) -> None:
        unit_dir = teach._systemd_unit_dir()
        calls: list[tuple[str, ...]] = []

        def unavailable(
                *args: str, timeout_s: float | None = None,
                observation: dict | None = None) -> int:
            del timeout_s
            calls.append(args)
            if args[0] == "enable":
                name = args[-1]
                target = unit_dir / name
                group = (
                    "paths.target.wants"
                    if name.endswith(".path") else "timers.target.wants")
                link = unit_dir / group / name
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(target)
            if observation is not None:
                observation.update(
                    state="complete", detail="systemctl returned 1",
                    stdout="", stderr="Failed to connect to bus")
            return 1

        with mock.patch.object(teach.sys, "platform", "linux"), \
                mock.patch.object(
                    teach, "_systemctl_user", side_effect=unavailable):
            self.assertFalse(teach._sentinel_install_linux([]))
            marker = self.data / teach._LINUX_UNARMED_MARKER
            self.assertEqual(marker.read_bytes(), b"not-armed\n")
            for path in (
                    unit_dir / f"{teach.TASK_NAME}.service",
                    unit_dir / f"{teach.TASK_NAME}.timer",
                    unit_dir / f"{teach.TASK_NAME}.path",
                    unit_dir / "paths.target.wants" / f"{teach.TASK_NAME}.path",
                    unit_dir / "timers.target.wants" / f"{teach.TASK_NAME}.timer",
                    self.data / "sentinel.sh",
                    self.data / "sentinel_strip.pl"):
                self.assertFalse(path.exists() or path.is_symlink(), path)

            before_remove = len(calls)
            self.assertTrue(teach._sentinel_remove())

        self.assertEqual(calls[before_remove:], [("daemon-reload",)])
        self.assertFalse(marker.exists())

    def test_interrupted_remove_cannot_be_reinjected(self) -> None:
        first = self.home / ".codex" / "AGENTS.md"
        second = self.home / ".claude" / "CLAUDE.md"
        first.parent.mkdir()
        second.parent.mkdir()
        teach.MD_TARGETS = [
            ("codex", first.parent, first),
            ("claude", second.parent, second),
        ]
        teach._write_block(first)
        teach._write_block(second)
        teach._save_state([first, second])
        real_remove = teach._remove_block

        def interrupt(path: Path) -> bool:
            real_remove(path)
            raise KeyboardInterrupt

        fence = mock.Mock()
        with mock.patch.object(
                teach.removal_fence, "acquire_background_removal_fence",
                return_value=fence), \
                mock.patch.object(
                    teach.removal_fence, "finish_background_removal_fence",
                    return_value=True), \
                mock.patch.object(teach, "_stop_daemons", return_value=True), \
                mock.patch.object(teach, "_remove_block", side_effect=interrupt), \
                mock.patch("sys.stdout", new=io.StringIO()), \
                self.assertRaises(KeyboardInterrupt):
            teach._remove()
        state = json.loads(teach.STATE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(state["targets"], [str(first), str(second)])
        self.assertEqual(state["removing"], [str(first)])
        self.assertFalse(first.exists())
        self.assertEqual(teach.reconcile(), [])
        self.assertFalse(first.exists())
        state = json.loads(teach.STATE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(state["targets"], [str(second)])
        self.assertEqual(state["removing"], [])

    def test_interrupted_before_remove_is_completed_by_reconcile(self) -> None:
        target = self.home / ".codex" / "AGENTS.md"
        target.parent.mkdir()
        teach.MD_TARGETS = [("codex", target.parent, target)]
        teach._write_block(target)
        teach._save_state([target])
        fence = mock.Mock()
        with mock.patch.object(
                teach.removal_fence, "acquire_background_removal_fence",
                return_value=fence), \
                mock.patch.object(
                    teach.removal_fence, "finish_background_removal_fence",
                    return_value=True), \
                mock.patch.object(teach, "_stop_daemons", return_value=True), \
                mock.patch.object(
                    teach, "_remove_block", side_effect=KeyboardInterrupt), \
                mock.patch("sys.stdout", new=io.StringIO()), \
                self.assertRaises(KeyboardInterrupt):
            teach._remove()
        state = json.loads(teach.STATE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(state["targets"], [str(target)])
        self.assertEqual(state["removing"], [str(target)])
        self.assertTrue(target.exists())
        self.assertEqual(teach.reconcile(), [])
        self.assertFalse(target.exists())
        self.assertFalse(teach.STATE_PATH.exists())

    def test_retired_pending_removal_health_survives_a_restart(self) -> None:
        target = self.home / ".retired-agent" / "AGENTS.md"
        target.parent.mkdir()
        target.write_text("owned\n", encoding="utf-8")
        teach._write_block(target)
        teach.MD_TARGETS = []
        teach._save_state([target], removing=[target])
        with mock.patch.object(
                teach, "_remove_block", side_effect=PermissionError("busy")):
            self.assertEqual(teach.reconcile(), [])
        immediate = teach.current_reconcile_health()
        self.assertEqual(immediate["state"], "refused")
        self.assertEqual(immediate["refusals"][0]["kind"], "removal-pending")
        teach._LAST_RECONCILE_HEALTH = None
        durable = teach.reconcile_health()
        self.assertEqual(durable["state"], "refused")
        self.assertEqual(durable["refusals"][0]["kind"], "removal-pending")
        self.assertIn("busy", durable["refusals"][0]["reason"])

    def test_remove_isolates_target_errors_and_continues(self) -> None:
        first = self.home / ".codex" / "AGENTS.md"
        second = self.home / ".claude" / "CLAUDE.md"
        first.parent.mkdir()
        second.parent.mkdir()
        first.write_bytes(b"FIRST\n")
        second.write_bytes(b"SECOND\n")
        teach.MD_TARGETS = [
            ("codex", first.parent, first),
            ("claude", second.parent, second),
        ]
        teach._write_block(first)
        teach._write_block(second)
        teach._save_state([first, second])
        first_before = first.read_bytes()
        real_remove = teach._remove_block

        def remove(path: Path) -> bool:
            if path == first:
                raise PermissionError("denied")
            return real_remove(path)

        fence = mock.Mock()
        output = io.StringIO()
        with mock.patch.object(
                teach.removal_fence, "acquire_background_removal_fence",
                return_value=fence), \
                mock.patch.object(
                    teach.removal_fence, "finish_background_removal_fence",
                    return_value=True), \
                mock.patch.object(teach, "_stop_daemons", return_value=True), \
                mock.patch.object(teach, "_remove_block", side_effect=remove), \
                mock.patch.object(teach, "_sentinel_remove") as sentinel, \
                mock.patch("sys.stdout", new=output):
            self.assertEqual(teach._remove(), 1)
        self.assertEqual(first.read_bytes(), first_before)
        self.assertEqual(second.read_bytes(), b"SECOND\n\n")
        # audit B9: only the removed target is un-enrolled; the failed one
        # keeps its record for the retry
        state = json.loads(teach.STATE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(state["targets"], [str(first)])
        self.assertIn("denied", output.getvalue())
        self.assertIn("claude: block removed", output.getvalue())
        sentinel.assert_not_called()

    def test_remove_cleans_enrolled_targets_retired_from_registry(self) -> None:
        markdown = self.home / ".retired-agent" / "RULES.md"
        markdown.parent.mkdir()
        markdown.write_text("MY RULE\n", encoding="utf-8")
        teach._write_block(markdown)
        skill = self.home / ".retired-agent" / "skills" / "SKILL.md"
        teach._write_skill(skill)
        teach._save_state([markdown, skill], [skill])

        fence = mock.Mock()
        output = io.StringIO()
        with mock.patch.object(
                teach.removal_fence, "acquire_background_removal_fence",
                return_value=fence), \
                mock.patch.object(
                    teach.removal_fence, "finish_background_removal_fence",
                    return_value=True), \
                mock.patch.object(teach, "_stop_daemons", return_value=True), \
                mock.patch.object(teach, "_sentinel_remove") as sentinel, \
                mock.patch("sys.stdout", new=output):
            self.assertEqual(teach._remove(), 0)

        self.assertEqual(markdown.read_text(encoding="utf-8"), "MY RULE\n\n")
        self.assertFalse(skill.exists())
        self.assertFalse(teach.STATE_PATH.exists())
        self.assertIn("retired: block removed", output.getvalue())
        self.assertIn("retired: skill removed", output.getvalue())
        sentinel.assert_called_once()

    def test_retired_state_paths_cannot_escape_home(self) -> None:
        retired = self.home / ".retired-agent" / "RULES.md"
        outside = self.root / "outside.md"
        relative = ".retired-agent/RULES.md"
        link = self.home / "outside-link.md"
        outside.write_text("outside", encoding="utf-8")
        candidates = [relative, str(outside)]
        try:
            link.symlink_to(outside)
            candidates.append(str(link))
        except OSError:
            pass
        candidates.append(str(retired))

        self.assertEqual(
            teach._state_paths(
                candidates,
                include_retired=True,
            ),
            [str(retired)],
        )

    def test_crlf_upgrade_and_remove_preserve_every_user_byte(self) -> None:
        target = self.home / ".codex" / "AGENTS.md"
        target.parent.mkdir()
        prefix = b"RULE  \r\n```text\r\n\r\n\r\nkeep spacing\r\n```\r\n"
        old_block = (
            f"{teach.MARK_PREFIX} v1 -->\r\nold\r\n{teach.MARK_END}\r\n"
        ).encode("utf-8")
        suffix = b"TAIL\t\r\n"
        target.write_bytes(prefix + old_block + suffix)
        self.assertEqual(teach._write_block(target), "updated")
        upgraded = target.read_bytes()
        self.assertTrue(upgraded.startswith(prefix))
        self.assertTrue(upgraded.endswith(suffix))
        self.assertNotIn(b"\n", upgraded.replace(b"\r\n", b""))
        self.assertTrue(teach._remove_block(target))
        self.assertEqual(target.read_bytes(), prefix + suffix)

        appended = self.home / ".claude" / "CLAUDE.md"
        appended.parent.mkdir()
        original = b"USER\r\n\r\n"
        appended.write_bytes(original)
        self.assertEqual(teach._write_block(appended), "added")
        self.assertTrue(appended.read_bytes().startswith(original))
        self.assertNotIn(b"\n", appended.read_bytes().replace(b"\r\n", b""))

    def test_remove_leaves_non_utf8_skill_in_place(self) -> None:
        proof = self.home / ".cursor"
        skill = self.home / ".cursor" / "skills" / "agrep-recall" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        original = teach.MARK_BEGIN.encode("ascii") + b"\n\xff\n"
        skill.write_bytes(original)
        teach.SKILL_TARGETS = [("cursor", proof, skill)]
        teach._save_state([skill], [skill])
        fence = mock.Mock()
        output = io.StringIO()
        with mock.patch.object(
                teach.removal_fence, "acquire_background_removal_fence",
                return_value=fence), \
                mock.patch.object(
                    teach.removal_fence, "finish_background_removal_fence",
                    return_value=True), \
                mock.patch.object(teach, "_stop_daemons", return_value=True), \
                mock.patch.object(teach, "_sentinel_remove") as sentinel, \
                mock.patch("sys.stdout", new=output):
            self.assertEqual(teach._remove(), 1)
        self.assertEqual(skill.read_bytes(), original)
        self.assertIn("not valid UTF-8", output.getvalue())
        # audit B9: the skill still on disk keeps its enrollment record
        state = json.loads(teach.STATE_PATH.read_text(encoding="utf-8"))
        self.assertEqual((state["targets"], state["skills"]),
                         ([str(skill)], [str(skill)]))
        sentinel.assert_not_called()

    def test_reconcile_ignores_malformed_or_relative_state_paths(self) -> None:
        relative = "agrep-reconcile-relative-target"
        rogue = self.root / "rogue.md"
        teach.STATE_PATH.write_text(json.dumps({
            "targets": relative,
            "skills": [["not", "hashable"]],
        }), encoding="utf-8")
        with mock.patch.object(teach, "_write_block") as write:
            self.assertEqual(teach.reconcile(), [])
        write.assert_not_called()

        teach.STATE_PATH.write_text(json.dumps({
            "targets": [relative, str(rogue), 7, None],
            "skills": [relative, str(rogue)],
        }), encoding="utf-8")
        with mock.patch.object(teach, "_write_block") as write:
            self.assertEqual(teach.reconcile(), [])
        write.assert_not_called()

    def test_reconcile_cannot_reclassify_a_known_markdown_target_as_a_skill(
            self) -> None:
        proof = self.home / ".codex"
        proof.mkdir()
        target = proof / "AGENTS.md"
        teach.MD_TARGETS = [("codex", proof, target)]
        teach.STATE_PATH.write_text(json.dumps({
            "targets": [str(target)],
            "skills": [str(target)],
        }), encoding="utf-8")
        with mock.patch.object(teach, "_write_block", return_value="added") as block, \
                mock.patch.object(teach, "_write_skill") as skill:
            self.assertEqual(teach.reconcile(), [str(target)])
        block.assert_called_once_with(target, expect_absent=True)
        skill.assert_not_called()

    def test_reconcile_recreates_missing_skill_with_frontmatter(self) -> None:
        proof = self.home / ".cursor"
        proof.mkdir()
        skill = proof / "skills" / "agrep-recall" / "SKILL.md"
        teach.SKILL_TARGETS = [("cursor", proof, skill)]
        teach._save_state([skill], [skill])
        self.assertEqual(teach.reconcile(), [str(skill)])
        text = skill.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(teach._SKILL_FRONT))
        self.assertIn(teach.MARK_PREFIX, text)

    def test_reconcile_restores_frontmatter_without_rewriting_current_block(
            self) -> None:
        proof = self.home / ".cursor"
        proof.mkdir()
        skill = proof / "skills" / "agrep-recall" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        damaged = teach._block(skill).replace("\n", "\r\n").encode("utf-8")
        skill.write_bytes(damaged)
        teach.SKILL_TARGETS = [("cursor", proof, skill)]
        teach._save_state([skill], [skill])

        self.assertEqual(teach.reconcile(), [])
        self.assertEqual(skill.read_bytes(), damaged)
        health = teach.current_reconcile_health()
        self.assertEqual(health["state"], "refused")
        self.assertEqual(health["refusals"][0]["kind"], "drifted")

    def test_sentinel_failure_warns_without_gating_enrollment(self) -> None:
        # arming fails from daemon-spawned shells (no Aqua bootstrap access);
        # the sentinel is uninstall hygiene and must not block setup's later
        # steps - enrollment stands, state stays recoverable, doctor shows it
        proof = self.home / ".codex"
        proof.mkdir()
        target = proof / "AGENTS.md"
        teach.MD_TARGETS = [("codex", proof, target)]
        out = io.StringIO()
        with mock.patch.object(teach, "_sentinel_install", return_value=False), \
                mock.patch("sys.stdout", new=out):
            self.assertEqual(teach._install(), 0)
        self.assertIn("sentinel could not be armed", out.getvalue())
        state = json.loads(teach.STATE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(state["targets"], [str(target)])

    def test_reinstall_keeps_owned_target_when_proof_temporarily_disappears(self) -> None:
        md_proof = self.home / ".codex"
        skill_proof = self.home / ".cursor-proof"
        md_target = self.home / ".codex-rules" / "AGENTS.md"
        skill = self.home / ".cursor" / "skills" / "agrep-recall" / "SKILL.md"
        md_proof.mkdir()
        skill_proof.mkdir()
        teach.MD_TARGETS = [("codex", md_proof, md_target)]
        teach.SKILL_TARGETS = [("cursor", skill_proof, skill)]
        with mock.patch.object(teach, "_sentinel_install", return_value=True) as install, \
                mock.patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(teach._install(), 0)
            skill_proof.rmdir()
            self.assertEqual(teach._install(), 0)
        state = json.loads(teach.STATE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(state["targets"], [str(md_target), str(skill)])
        self.assertEqual(state["skills"], [str(skill)])
        self.assertEqual(install.call_args_list[-1].args[0], [md_target, skill])
        self.assertIn(teach.MARK_PREFIX, skill.read_text(encoding="utf-8"))

        skill.write_text(skill.read_text(encoding="utf-8") + "mine\n", encoding="utf-8")
        self.assertTrue(teach._remove_skill(skill))
        with mock.patch.object(teach, "_sentinel_install", return_value=True) as install, \
                mock.patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(teach._install(), 0)
        state = json.loads(teach.STATE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(state["targets"], [str(md_target)])
        self.assertEqual(state["skills"], [])
        install.assert_called_once_with([md_target])
        self.assertNotIn(teach.MARK_PREFIX, skill.read_text(encoding="utf-8"))

    def test_install_cleans_targets_retired_from_registry(self) -> None:
        old_markdown = self.home / ".old-agent" / "RULES.md"
        old_markdown.parent.mkdir()
        old_markdown.write_text("MY RULE\n", encoding="utf-8")
        teach._write_block(old_markdown)
        old_skill = self.home / ".old-agent" / "skills" / "SKILL.md"
        teach._write_skill(old_skill)
        teach._save_state([old_markdown, old_skill], [old_skill])

        proof = self.home / ".new-agent"
        proof.mkdir()
        current = proof / "AGENTS.md"
        teach.MD_TARGETS = [("new", proof, current)]
        teach.SKILL_TARGETS = []
        with mock.patch.object(teach, "_sentinel_install", return_value=True) as sentinel, \
                mock.patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(teach._install(), 0)

        self.assertEqual(old_markdown.read_text(encoding="utf-8"), "MY RULE\n\n")
        self.assertFalse(old_skill.exists())
        self.assertIn(teach.MARK_PREFIX, current.read_text(encoding="utf-8"))
        state = json.loads(teach.STATE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(state["targets"], [str(current)])
        self.assertEqual(state["skills"], [])
        sentinel.assert_called_once_with([current])

    def test_install_retains_retired_skill_when_cleanup_is_unproven(self) -> None:
        retired = self.home / ".old-agent" / "skills" / "SKILL.md"
        retired.parent.mkdir(parents=True)
        original = (
            teach._SKILL_FRONT.encode("utf-8")
            + f"{teach.MARK_BEGIN}\nmissing end\n".encode("utf-8")
        )
        retired.write_bytes(original)
        teach._save_state([retired], [retired])

        proof = self.home / ".new-agent"
        proof.mkdir()
        current = proof / "AGENTS.md"
        teach.MD_TARGETS = [("new", proof, current)]
        teach.SKILL_TARGETS = []
        output = io.StringIO()
        with mock.patch.object(teach, "_sentinel_install", return_value=True) as sentinel, \
                mock.patch("sys.stdout", new=output):
            self.assertEqual(teach._install(), 1)

        self.assertEqual(retired.read_bytes(), original)
        state = json.loads(teach.STATE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(state["targets"], [str(current), str(retired)])
        self.assertEqual(state["skills"], [str(retired)])
        self.assertEqual(
            teach._sentinel_skill_paths([current, retired]), [retired])
        self.assertIn("cleanup deferred", output.getvalue())
        sentinel.assert_called_once_with([current, retired])

    def test_install_drops_missing_retired_skill_as_already_clean(self) -> None:
        retired = self.home / ".old-agent" / "skills" / "SKILL.md"
        teach._save_state([retired], [retired])
        proof = self.home / ".new-agent"
        proof.mkdir()
        current = proof / "AGENTS.md"
        teach.MD_TARGETS = [("new", proof, current)]
        teach.SKILL_TARGETS = []

        with mock.patch.object(teach, "_sentinel_install", return_value=True) as sentinel, \
                mock.patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(teach._install(), 0)

        state = json.loads(teach.STATE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(state["targets"], [str(current)])
        self.assertEqual(state["skills"], [])
        sentinel.assert_called_once_with([current])

    def test_launchd_values_are_xml_escaped(self) -> None:
        teach.HOME = self.root / "home & launch"
        teach.HOME.mkdir()
        teach.REPO = self.root / "repo & checkout"
        teach.REPO.mkdir()
        target = self.root / "rules & notes.md"
        with mock.patch.object(teach.subprocess, "run", return_value=mock.Mock(returncode=0)), \
                mock.patch.object(teach.os, "getuid", return_value=501, create=True), \
                mock.patch.object(teach, "sentinel_armed", return_value=True):
            self.assertTrue(teach._sentinel_install_mac([target]))
        ET.parse(teach._plist_path())
        body = teach._plist_path().read_text(encoding="utf-8")
        self.assertIn("repo &amp; checkout", body)
        self.assertNotIn("repo & checkout", body)

    def test_sentinel_probe_requires_scheduler_state(self) -> None:
        for name in ("sentinel.json", "sentinel_watch.py"):
            (self.data / name).write_text("{}", encoding="utf-8")
        with mock.patch.object(teach.sys, "platform", "win32"), \
                mock.patch.object(teach.subprocess, "run",
                                  return_value=mock.Mock(returncode=1)):
            self.assertFalse(teach.sentinel_armed())
        with mock.patch.object(teach.sys, "platform", "win32"), \
                mock.patch.object(teach.subprocess, "run",
                                  return_value=mock.Mock(returncode=0)):
            self.assertTrue(teach.sentinel_armed())

        plist = teach._plist_path()
        plist.parent.mkdir(parents=True)
        plist.write_text("plist", encoding="utf-8")
        (self.data / "sentinel.sh").write_text("script", encoding="utf-8")
        (self.data / "sentinel_strip.pl").write_text("script", encoding="utf-8")
        with mock.patch.object(teach.sys, "platform", "darwin"), \
                mock.patch.object(teach.os, "getuid", return_value=501, create=True), \
                mock.patch.object(teach.subprocess, "run",
                                  return_value=mock.Mock(returncode=1)):
            self.assertFalse(teach.sentinel_armed())
        with mock.patch.object(teach.sys, "platform", "darwin"), \
                mock.patch.object(teach.os, "getuid", return_value=501, create=True), \
                mock.patch.object(teach.subprocess, "run",
                                  return_value=mock.Mock(returncode=0)):
            self.assertTrue(teach.sentinel_armed())

        with mock.patch.object(teach.sys, "platform", "linux"), \
                mock.patch.object(teach, "_systemctl_user", return_value=1):
            self.assertFalse(teach.sentinel_armed())
        with mock.patch.object(teach.sys, "platform", "linux"), \
                mock.patch.object(teach, "_systemctl_user", return_value=0):
            self.assertTrue(teach.sentinel_armed())

    def test_windows_watcher_preserves_symlink_for_block_only_target(self) -> None:
        parsed = ast.parse(teach._SENTINEL_WATCH_PY)
        selected = [node for node in parsed.body
                    if isinstance(node, ast.FunctionDef) and node.name in ("_atomic", "strip")]
        namespace = {
            "os": __import__("os"), "re": __import__("re"),
            "tempfile": __import__("tempfile"), "Path": Path,
            "subprocess": mock.Mock(), "DIR": self.data,
            "__file__": str(self.data / "sentinel_watch.py"),
        }
        exec(compile(ast.Module(body=selected, type_ignores=[]), "<sentinel>", "exec"),
             namespace)
        target = self.root / "real-rules.md"
        target.write_text(teach._block(), encoding="utf-8")
        link = self.root / "linked-rules.md"
        try:
            link.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        Path(namespace["__file__"]).write_text("watcher", encoding="utf-8")
        namespace["strip"]({
            "mark_prefix": teach.MARK_PREFIX, "mark_end": teach.MARK_END,
            "targets": [str(link)], "skill_files": [], "task_name": teach.TASK_NAME,
        })
        self.assertTrue(link.is_symlink())
        self.assertTrue(target.exists())
        self.assertEqual(target.read_text(encoding="utf-8"), "")

    def test_windows_watcher_refuses_inconsistent_markers(self) -> None:
        parsed = ast.parse(teach._SENTINEL_WATCH_PY)
        selected = [node for node in parsed.body
                    if isinstance(node, ast.FunctionDef) and node.name in ("_atomic", "strip")]
        namespace = {
            "os": __import__("os"), "re": __import__("re"),
            "tempfile": __import__("tempfile"), "Path": Path,
            "subprocess": mock.Mock(), "DIR": self.data,
            "__file__": str(self.data / "sentinel_watch.py"),
        }
        exec(compile(ast.Module(body=selected, type_ignores=[]), "<sentinel>", "exec"),
             namespace)
        target = self.root / "AGENTS.md"
        original = (
            f"{teach.MARK_PREFIX} v1 -->\r\nUSER\r\n"
            f"{teach.MARK_PREFIX} v1 -->\r\nlater\r\n{teach.MARK_END}\r\n"
        ).encode("utf-8")
        target.write_bytes(original)
        Path(namespace["__file__"]).write_text("watcher", encoding="utf-8")
        namespace["strip"]({
            "mark_prefix": teach.MARK_PREFIX, "mark_end": teach.MARK_END,
            "targets": [str(target)], "skill_files": [], "task_name": teach.TASK_NAME,
        })
        self.assertEqual(target.read_bytes(), original)

    def test_posix_sentinel_splicer_refuses_malformed_and_preserves_crlf(self) -> None:
        perl = shutil.which("perl")
        if not perl:
            self.skipTest("perl unavailable")
        splicer = self.data / "sentinel_strip.pl"
        splicer.write_text(teach._SENTINEL_STRIP_PERL, encoding="utf-8")
        malformed = self.root / "malformed.md"
        damaged = (
            f"{teach.MARK_PREFIX} v1 -->\r\nUSER\r\n"
            f"{teach.MARK_PREFIX} v1 -->\r\nlater\r\n{teach.MARK_END}\r\n"
        ).encode("utf-8")
        malformed.write_bytes(damaged)
        bad = subprocess.run(
            [perl, str(splicer), str(malformed), teach.MARK_PREFIX, teach.MARK_END],
            capture_output=True)
        self.assertEqual(bad.returncode, 3)
        self.assertEqual(malformed.read_bytes(), damaged)

        valid = self.root / "valid.md"
        prefix = b"USER\r\n\r\n"
        block = (
            f"{teach.MARK_PREFIX} v1 -->\r\nowned\r\n{teach.MARK_END}\r\n"
        ).encode("utf-8")
        suffix = b"TAIL\r\n"
        valid.write_bytes(prefix + block + suffix)
        good = subprocess.run(
            [perl, str(splicer), str(valid), teach.MARK_PREFIX, teach.MARK_END],
            capture_output=True)
        self.assertEqual(good.returncode, 0)
        self.assertEqual(valid.read_bytes(), prefix + suffix)

    def _fenced_remove(self, output: io.StringIO) -> int:
        with mock.patch.object(
                teach.removal_fence, "acquire_background_removal_fence",
                return_value=mock.Mock()), \
                mock.patch.object(
                    teach.removal_fence, "finish_background_removal_fence",
                    return_value=True), \
                mock.patch.object(teach, "_stop_daemons", return_value=True), \
                mock.patch.object(teach, "_sentinel_remove", return_value=True), \
                mock.patch("sys.stdout", new=output):
            return teach._remove()

    # audit B9: a failed removal must keep its enrollment record - the old
    # unenroll-first order made the retry exit 0 with the block still on disk
    def test_failed_retired_removal_keeps_record_and_retry_stays_honest(self) -> None:
        retired = self.home / ".retired-agent" / "AGENTS.md"
        retired.parent.mkdir()
        block = teach._block(retired).encode("utf-8")
        retired.write_bytes(block + b"\ntrailing \xff\n")
        teach._save_state([retired])
        for attempt in ("first", "retry"):
            output = io.StringIO()
            with self.subTest(attempt=attempt):
                self.assertEqual(self._fenced_remove(output), 1)
                self.assertIn("could not remove", output.getvalue())
                self.assertTrue(teach._block_spans(retired.read_bytes()))
                state = json.loads(
                    teach.STATE_PATH.read_text(encoding="utf-8"))
                self.assertEqual(state["targets"], [str(retired)])
        retired.write_bytes(block + b"\ntrailing repaired\n")
        output = io.StringIO()
        self.assertEqual(self._fenced_remove(output), 0)
        self.assertIn("block removed", output.getvalue())
        self.assertEqual(teach._block_spans(retired.read_bytes()), [])
        self.assertFalse(teach.STATE_PATH.exists())


if __name__ == "__main__":
    unittest.main()
