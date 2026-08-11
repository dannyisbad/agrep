"""Hook integration ownership: agrep upgrades its own bytes, never a user's.

Claude's generated structure, Codex's hooks.json, and the shared pi/OMP
extension payload are all judged exactly. The one failure this file exists to
prevent is `agrep setup` replacing a hook or extension the user wrote by hand.

Isolation is one patch: hookinstall derives every home path from teach.HOME
at call time, so pointing teach.HOME into a sandbox isolates install() and
remove() completely. Patching path constants per-test is the pattern that
once let remove() reach the developer's real settings.
"""

from __future__ import annotations

import copy
import contextlib
import hashlib
import json
import io
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from types import SimpleNamespace

import hookinstall
import cli
import teach


class ClaudeHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        home = Path(self._tmp.name)
        (home / ".claude").mkdir()
        patcher = mock.patch.object(teach, "HOME", home)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.hooks_dir = home / ".claude" / "hooks"
        self.target = self.hooks_dir / "compact-contract.md"
        self.settings = home / ".claude" / "settings.json"
        self.shipped = hookinstall.COMPACT_CONTRACT.read_bytes()

    def _registered_command(self) -> str:
        value = json.loads(self.settings.read_text(encoding="utf-8"))
        [entry] = value["hooks"]["PreCompact"]
        [hook] = entry["hooks"]
        return hook["command"]

    def test_paths_follow_the_sandboxed_home(self) -> None:
        # The regression this file must never repeat: a sandbox that patches
        # teach.HOME must also move every hookinstall path.
        root = Path(self._tmp.name)
        self.assertEqual(hookinstall.claude_settings_path(), self.settings)
        self.assertEqual(hookinstall.claude_hook_target(), self.target)
        for path in hookinstall.codex_hooks_paths():
            self.assertTrue(str(path).startswith(str(root)))
        for _, path in hookinstall.pi_extension_paths():
            self.assertTrue(str(path).startswith(str(root)))

    def test_fresh_install_writes_payload_and_registers(self) -> None:
        self.settings.write_text("{}", encoding="utf-8")
        self.assertTrue(hookinstall.install_claude_hook(warn=False))
        self.assertEqual(self.target.read_bytes(), self.shipped)
        self.assertIn("compact-contract.md", self._registered_command())

    def test_settings_sidecars_are_never_touched(self) -> None:
        self.settings.write_text("{}", encoding="utf-8")
        backup = self.settings.with_suffix(".json.bak")
        fixed_tmp = self.settings.with_suffix(".json.tmp")
        backup.write_bytes(b"user backup")
        fixed_tmp.write_bytes(b"user temporary file")
        self.assertTrue(hookinstall.install_claude_hook(warn=False))
        self.assertEqual(backup.read_bytes(), b"user backup")
        self.assertEqual(fixed_tmp.read_bytes(), b"user temporary file")

    def test_symlinked_settings_and_payload_are_user_owned(self) -> None:
        backing_settings = self.settings.parent / "settings-source.json"
        backing_settings.write_text("{}", encoding="utf-8")
        try:
            self.settings.symlink_to(backing_settings)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        self.assertFalse(hookinstall.install_claude_hook(warn=False))
        self.assertTrue(self.settings.is_symlink())
        self.assertEqual(backing_settings.read_text(encoding="utf-8"), "{}")

        self.settings.unlink()
        self.settings.write_text("{}", encoding="utf-8")
        self.target.unlink()
        self.hooks_dir.mkdir(parents=True, exist_ok=True)
        backing_payload = self.settings.parent / "payload-source.md"
        backing_payload.write_bytes(self.shipped)
        self.target.symlink_to(backing_payload)
        self.assertFalse(hookinstall.install_claude_hook(warn=False))
        self.assertTrue(self.target.is_symlink())
        self.assertEqual(backing_payload.read_bytes(), self.shipped)

    def test_registered_command_is_shell_quoted(self) -> None:
        self.settings.write_text("{}", encoding="utf-8")
        self.assertTrue(hookinstall.install_claude_hook(warn=False))
        command = self._registered_command()
        self.assertEqual(command, f"cat '{self.target}'")

    def test_prior_shipped_payload_is_upgraded(self) -> None:
        self.hooks_dir.mkdir(parents=True)
        stale = b"the previously shipped payload\n"
        self.target.write_bytes(stale)
        digest = hashlib.sha256(stale).hexdigest()
        with mock.patch.object(
                hookinstall, "PRIOR_PAYLOAD_HASHES", frozenset({digest})):
            self.settings.write_text("{}", encoding="utf-8")
            self.assertTrue(hookinstall.install_claude_hook(warn=False))
        self.assertEqual(self.target.read_bytes(), self.shipped)

    def test_user_edited_payload_is_never_overwritten(self) -> None:
        self.hooks_dir.mkdir(parents=True)
        edited = self.shipped + b"\n13. My own section.\n"
        self.target.write_bytes(edited)
        self.settings.write_text("{}", encoding="utf-8")
        self.assertTrue(hookinstall.install_claude_hook(warn=False))
        self.assertEqual(self.target.read_bytes(), edited)
        # Registration still proceeds around the user's file.
        self.assertIn("compact-contract.md", self._registered_command())

    def test_existing_precompact_registration_is_left_alone(self) -> None:
        original = {"hooks": {"PreCompact": [{"matcher": "manual",
                                             "hooks": []}]}}
        self.settings.write_text(json.dumps(original), encoding="utf-8")
        self.assertTrue(hookinstall.install_claude_hook(warn=False))
        value = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(value, original)

    def test_non_object_hooks_field_is_never_overwritten(self) -> None:
        for value in (None, [], "mine"):
            with self.subTest(value=value):
                original = json.dumps({"hooks": value})
                self.settings.write_text(original, encoding="utf-8")
                self.assertFalse(
                    hookinstall.install_claude_hook(warn=False))
                self.assertEqual(
                    self.settings.read_text(encoding="utf-8"), original)
                self.assertEqual(
                    hookinstall._claude_hook_state(), "unreadable")

    def test_near_collision_precompact_hooks_survive_setup_and_remove(
            self) -> None:
        self.settings.write_text("{}", encoding="utf-8")
        self.assertTrue(hookinstall.install_claude_hook(warn=False))
        generated = json.loads(
            self.settings.read_text(encoding="utf-8")
        )["hooks"]["PreCompact"]

        other_path = copy.deepcopy(generated)
        other_path[0]["hooks"][0]["command"] = (
            "cat '/tmp/user/compact-contract.md'")
        altered_args = copy.deepcopy(generated)
        altered_args[0]["hooks"][0]["command"] += " --user-argument"
        altered_type = copy.deepcopy(generated)
        altered_type[0]["hooks"][0]["type"] = "prompt"
        altered_matcher = copy.deepcopy(generated)
        altered_matcher[0]["matcher"] = "auto"
        extra_fields = copy.deepcopy(generated)
        extra_fields[0]["hooks"][0]["userField"] = "keep"

        for name, candidate in {
                "other_path": other_path,
                "altered_args": altered_args,
                "altered_type": altered_type,
                "altered_matcher": altered_matcher,
                "extra_fields": extra_fields,
        }.items():
            with self.subTest(name=name):
                self.assertFalse(
                    hookinstall._our_precompact_entry(candidate))
                settings = {"hooks": {"PreCompact": candidate}}
                raw = json.dumps(settings)
                self.settings.write_text(raw, encoding="utf-8")
                self.assertTrue(
                    hookinstall.install_claude_hook(warn=False))
                self.assertEqual(
                    self.settings.read_text(encoding="utf-8"), raw)
                self.assertEqual(hookinstall.remove(warn=False), 0)
                self.assertEqual(
                    self.settings.read_text(encoding="utf-8"), raw)

    def test_current_payload_is_not_rewritten(self) -> None:
        self.hooks_dir.mkdir(parents=True)
        self.target.write_bytes(self.shipped)
        before = self.target.stat().st_mtime_ns
        self.settings.write_text("{}", encoding="utf-8")
        self.assertTrue(hookinstall.install_claude_hook(warn=False))
        self.assertEqual(self.target.stat().st_mtime_ns, before)

    def test_missing_settings_file_is_a_fresh_box_not_an_error(self) -> None:
        # no settings.json at all: the hook block becomes its first content
        self.assertTrue(hookinstall.install_claude_hook(warn=False))
        self.assertIn("compact-contract.md", self._registered_command())

    def test_remove_unregisters_ours_and_leaves_a_user_hook(self) -> None:
        self.settings.write_text("{}", encoding="utf-8")
        self.assertTrue(hookinstall.install_claude_hook(warn=False))
        self.assertEqual(hookinstall.remove(warn=False), 0)
        value = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertNotIn("PreCompact", value.get("hooks", {}))
        self.assertFalse(self.target.exists())
        # a user's own PreCompact block survives remove()
        user = {"hooks": {"PreCompact": [{"matcher": "*", "hooks": [
            {"type": "command", "command": "echo user"}]}]}}
        self.settings.write_text(json.dumps(user), encoding="utf-8")
        self.assertEqual(hookinstall.remove(warn=False), 0)
        self.assertEqual(
            json.loads(self.settings.read_text(encoding="utf-8")), user)


class CodexHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        home = Path(self._tmp.name)
        (home / ".claude").mkdir()
        home_patcher = mock.patch.object(teach, "HOME", home)
        home_patcher.start()
        self.addCleanup(home_patcher.stop)
        # Two copies keep the multi-path loop covered even though the real
        # install carries one; the sandboxed home keeps remove()'s claude
        # walk inside the tempdir.
        self.paths = (home / "a" / "hooks.json", home / "b" / "hooks.json")
        # a real box has the agent home already; setup never fabricates one
        for path in self.paths:
            path.parent.mkdir(parents=True)
        patcher = mock.patch.object(
            hookinstall, "codex_hooks_paths", lambda: self.paths)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_fresh_install_writes_both_copies(self) -> None:
        self.assertTrue(hookinstall.install_codex_hooks(warn=False))
        for path in self.paths:
            self.assertTrue(hookinstall._codex_hooks_owned(
                path.read_text(encoding="utf-8")))

    def test_symlinked_hooks_json_is_never_classified_or_removed_as_ours(
            self) -> None:
        backing = self.paths[0].parent / "user-hooks.json"
        body = hookinstall._codex_hook_payload()
        backing.write_text(body, encoding="utf-8")
        try:
            self.paths[0].symlink_to(backing)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        self.assertTrue(hookinstall.install_codex_hooks(warn=False))
        self.assertTrue(self.paths[0].is_symlink())
        self.assertEqual(backing.read_text(encoding="utf-8"), body)
        self.assertEqual(hookinstall._codex_hook_state(), "user")
        self.assertEqual(hookinstall.remove(warn=False), 0)
        self.assertTrue(self.paths[0].is_symlink())
        self.assertEqual(backing.read_text(encoding="utf-8"), body)

    def test_a_box_without_codex_is_left_untouched(self) -> None:
        import shutil
        shutil.rmtree(self.paths[0].parent)
        self.assertTrue(hookinstall.install_codex_hooks(warn=False))
        self.assertFalse(self.paths[0].parent.exists())
        self.assertTrue(self.paths[1].exists())

    def test_remove_takes_out_only_agrep_owned_hooks(self) -> None:
        self.assertTrue(hookinstall.install_codex_hooks(warn=False))
        user = json.dumps({"hooks": {"SessionStart": [
            {"matcher": "startup", "hooks": [{"type": "command", "command": "echo mine"}]}]}})
        self.paths[1].write_text(user, encoding="utf-8")
        self.assertEqual(hookinstall.remove(warn=False), 0)
        self.assertFalse(self.paths[0].exists())
        self.assertEqual(self.paths[1].read_text(encoding="utf-8"), user)

    def test_exact_legacy_install_is_rewritten(self) -> None:
        python = "/opt/Python Builds/bin/python"
        with mock.patch.object(
                hookinstall, "_codex_python", return_value=python), \
                mock.patch.object(hookinstall.console, "WIN", False):
            current = json.loads(hookinstall._codex_hook_payload())
            legacy = copy.deepcopy(current)
            legacy["hooks"]["SessionStart"][0]["hooks"][0]["command"] = (
                f"{python} "
                f"{hookinstall._shell_quote(str(hookinstall.CODEX_PAYLOAD))}")
            raw = json.dumps(legacy)
            self.assertNotEqual(legacy, current)
            self.assertTrue(hookinstall._codex_hooks_owned(raw))
            self.paths[0].write_text(raw, encoding="utf-8")
            self.assertTrue(hookinstall.install_codex_hooks(warn=False))
            self.assertEqual(
                self.paths[0].read_text(encoding="utf-8"),
                hookinstall._codex_hook_payload())

    def test_codex_command_quotes_spaced_interpreter_as_one_argv_token(
            self) -> None:
        python = "/opt/Python Builds/$release;stable/bin/python"
        with mock.patch.object(
                hookinstall, "_codex_python", return_value=python), \
                mock.patch.object(hookinstall.console, "WIN", False):
            value = json.loads(hookinstall._codex_hook_payload())
        command = value["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertEqual(
            shlex.split(command),
            [python, str(hookinstall.CODEX_PAYLOAD)])

    def test_user_hooks_json_is_never_overwritten(self) -> None:
        user = json.dumps({"hooks": {"SessionStart": [
            {"matcher": "startup", "hooks": [{"type": "command",
                                              "command": "echo mine"}]}]}})
        self.paths[0].parent.mkdir(parents=True, exist_ok=True)
        self.paths[0].write_text(user, encoding="utf-8")
        self.assertTrue(hookinstall.install_codex_hooks(warn=False))
        self.assertEqual(
            self.paths[0].read_text(encoding="utf-8"), user)
        # The untouched second copy still gets ours.
        self.assertTrue(hookinstall._codex_hooks_owned(
            self.paths[1].read_text(encoding="utf-8")))

    def test_near_collision_codex_hooks_survive_setup_and_remove(
            self) -> None:
        self.assertTrue(hookinstall.install_codex_hooks(warn=False))
        generated = json.loads(
            self.paths[0].read_text(encoding="utf-8"))
        command = generated[
            "hooks"]["SessionStart"][0]["hooks"][0]["command"]

        other_path = copy.deepcopy(generated)
        other_path["hooks"]["SessionStart"][0]["hooks"][0]["command"] = (
            "/user/python '/user/hooks/codex_compact_payload.py'")
        altered_args = copy.deepcopy(generated)
        altered_args["hooks"]["SessionStart"][0]["hooks"][0]["command"] = (
            command + " --user-argument")
        altered_type = copy.deepcopy(generated)
        altered_type["hooks"]["SessionStart"][0]["hooks"][0]["type"] = (
            "prompt")
        altered_matcher = copy.deepcopy(generated)
        altered_matcher["hooks"]["SessionStart"][0]["matcher"] = "compact"
        extra_fields = copy.deepcopy(generated)
        extra_fields["hooks"]["SessionStart"][0]["hooks"][0]["userField"] = (
            "keep")

        for name, candidate in {
                "other_path": other_path,
                "altered_args": altered_args,
                "altered_type": altered_type,
                "altered_matcher": altered_matcher,
                "extra_fields": extra_fields,
        }.items():
            with self.subTest(name=name):
                raw = json.dumps(candidate, indent=2) + "\n"
                self.assertFalse(hookinstall._codex_hooks_owned(raw))
                self.paths[0].write_text(raw, encoding="utf-8")
                self.assertTrue(
                    hookinstall.install_codex_hooks(warn=False))
                self.assertEqual(
                    self.paths[0].read_text(encoding="utf-8"), raw)
                self.assertEqual(hookinstall.remove(warn=False), 0)
                self.assertTrue(self.paths[0].exists())
                self.assertEqual(
                    self.paths[0].read_text(encoding="utf-8"), raw)

    def test_unparseable_hooks_json_is_treated_as_users(self) -> None:
        self.paths[0].parent.mkdir(parents=True, exist_ok=True)
        self.paths[0].write_text("{not json", encoding="utf-8")
        self.assertTrue(hookinstall.install_codex_hooks(warn=False))
        self.assertEqual(
            self.paths[0].read_text(encoding="utf-8"), "{not json")




class PiOmpExtensionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        (self.home / ".pi" / "agent").mkdir(parents=True)
        (self.home / ".omp" / "agent").mkdir(parents=True)
        patcher = mock.patch.object(teach, "HOME", self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.paths = dict(hookinstall.pi_extension_paths())
        self.shipped = hookinstall.PI_EXTENSION.read_bytes()

    def test_fresh_install_writes_byte_identical_copies(self) -> None:
        self.assertTrue(hookinstall.install_pi_extensions(warn=False))
        self.assertEqual(set(self.paths), {"pi", "omp"})
        for path in self.paths.values():
            self.assertEqual(path.read_bytes(), self.shipped)
        self.assertTrue(hookinstall.hooks_enrolled())

    def test_current_payload_is_not_rewritten(self) -> None:
        self.assertTrue(hookinstall.install_pi_extensions(warn=False))
        before = {
            agent: path.stat().st_mtime_ns
            for agent, path in self.paths.items()
        }
        self.assertTrue(hookinstall.install_pi_extensions(warn=False))
        self.assertEqual(
            {agent: path.stat().st_mtime_ns
             for agent, path in self.paths.items()},
            before,
        )

    def test_prior_shipped_payload_is_upgraded(self) -> None:
        stale = b"previous agrep pi extension\n"
        for path in self.paths.values():
            path.parent.mkdir()
            path.write_bytes(stale)
        digest = hashlib.sha256(stale).hexdigest()
        with mock.patch.object(
                hookinstall, "PRIOR_PI_EXTENSION_HASHES",
                frozenset({digest})):
            self.assertTrue(hookinstall.install_pi_extensions(warn=False))
        for path in self.paths.values():
            self.assertEqual(path.read_bytes(), self.shipped)

    def test_user_extension_is_preserved_while_other_agent_installs(
            self) -> None:
        user = b"export default function mine() {}\n"
        pi = self.paths["pi"]
        pi.parent.mkdir()
        pi.write_bytes(user)
        self.assertTrue(hookinstall.install_pi_extensions(warn=False))
        self.assertEqual(pi.read_bytes(), user)
        self.assertEqual(self.paths["omp"].read_bytes(), self.shipped)
        self.assertEqual(hookinstall._pi_extension_state(pi), "user")

    def test_remove_deletes_only_owned_extension_bytes(self) -> None:
        self.assertTrue(hookinstall.install_pi_extensions(warn=False))
        edited = self.shipped + b"\n// mine\n"
        self.paths["omp"].write_bytes(edited)
        self.assertEqual(hookinstall.remove(warn=False), 0)
        self.assertFalse(self.paths["pi"].exists())
        self.assertEqual(self.paths["omp"].read_bytes(), edited)

    def test_omp_profile_selects_profile_scoped_agent_root(self) -> None:
        profile_root = self.home / ".omp" / "profiles" / "work" / "agent"
        profile_root.mkdir(parents=True)
        with mock.patch.dict("os.environ", {"OMP_PROFILE": "work"}):
            paths = dict(hookinstall.pi_extension_paths())
            self.assertEqual(
                paths["omp"],
                profile_root / "extensions" / hookinstall.PI_EXTENSION_NAME,
            )
            self.assertTrue(hookinstall.install_pi_extensions(warn=False))
            self.assertEqual(paths["omp"].read_bytes(), self.shipped)

    def test_new_extension_requires_its_own_setup_consent(self) -> None:
        self.assertTrue(hookinstall.hooks_need_consent())
        self.assertTrue(hookinstall.install_pi_extensions(warn=False))
        self.assertFalse(hookinstall.hooks_need_consent())


class _TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


class SetupConsentTests(unittest.TestCase):
    def _run(self, answers: str) -> tuple[int, str, mock.Mock, mock.Mock]:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            data = root / "data"
            (home / ".pi" / "agent").mkdir(parents=True)
            (home / ".omp" / "agent").mkdir(parents=True)
            data.mkdir()
            teach_call = mock.Mock(return_value=0)
            hook_call = mock.Mock(return_value=0)
            stdout = io.StringIO()
            args = SimpleNamespace(
                rest=[],
                yes=False,
                no_teach=False,
                no_hook=False,
                no_semantic=True,
                archive=False,
                no_archive=True,
            )
            with mock.patch.object(teach, "HOME", home), \
                    mock.patch.object(teach, "STATE_PATH", data / "teach.json"), \
                    mock.patch.object(cli.common, "DATA_DIR", data), \
                    mock.patch.object(cli, "_setup_consentable_writes",
                                      return_value=True), \
                    mock.patch("doctor.main", return_value=0), \
                    mock.patch.object(teach, "teach", teach_call), \
                    mock.patch.object(
                        hookinstall, "hooks_need_consent", return_value=True), \
                    mock.patch.object(hookinstall, "install", hook_call), \
                    mock.patch.object(
                        teach, "refresh_sentinel", return_value=True), \
                    mock.patch.object(
                        cli, "_setup_index_state",
                        return_value=({"messages": 1, "sessions": 1}, False)), \
                    mock.patch.object(cli, "_setup_archive"), \
                    mock.patch.object(
                        cli, "_core_evidence_path", return_value="proof"), \
                    mock.patch.object(cli.sys, "stdin", _TTY(answers)), \
                    contextlib.redirect_stdout(stdout):
                rc = cli.cmd_setup(args)
        return rc, stdout.getvalue(), teach_call, hook_call

    def test_declining_instructions_still_offers_and_installs_hooks(self) -> None:
        rc, rendered, teach_call, hook_call = self._run("n\n\n")
        self.assertEqual(rc, 0)
        teach_call.assert_not_called()
        hook_call.assert_called_once_with(yes=True)
        self.assertLess(
            rendered.index("agent instructions untouched"),
            rendered.index("post-compact recovery integrations"),
        )
        self.assertIn("none of these run on ordinary user messages", rendered)

    def test_accepting_instructions_does_not_preapprove_hooks(self) -> None:
        rc, rendered, teach_call, hook_call = self._run("\nn\n")
        self.assertEqual(rc, 0)
        teach_call.assert_called_once_with(yes=True)
        hook_call.assert_not_called()
        self.assertIn("post-compact integrations skipped", rendered)
if __name__ == "__main__":
    unittest.main()
