"""Cross-language adapter and hookless-capability contracts."""

from __future__ import annotations

import ast
import copy
import contextlib
import io
import json
import os
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))
sys.path.insert(0, str(ROOT))

from _test_support import isolate_data_dir  # noqa: E402


isolate_data_dir()
import cli  # noqa: E402
import common  # noqa: E402
import indexd_runtime  # noqa: E402
import legacy_cleanup  # noqa: E402
from hookless import live, native, registry  # noqa: E402
import livetui  # noqa: E402
import tail  # noqa: E402
import teach  # noqa: E402


def _registry_payload() -> dict:
    return json.loads(registry.MANIFEST_PATH.read_text(encoding="utf-8"))


class AgentRegistryTests(unittest.TestCase):
    def test_common_stays_below_indexd_runtime(self) -> None:
        tree = ast.parse(Path(common.__file__).read_text(encoding="utf-8"))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported.update(
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
        self.assertNotIn("indexd_runtime", imported)
        self.assertFalse(hasattr(common, "ensure_index"))
        self.assertFalse(hasattr(common, "INDEXD_BUILD_ID"))

    def test_python_surfaces_match_the_validated_manifest(self) -> None:
        self.assertEqual(
            tuple(common.KNOWN_AGENTS), registry.ADAPTER_INPUT_NAMES)
        self.assertEqual(tuple(live.LIVE_TICKS), registry.LIVE_AGENTS)
        self.assertEqual(tuple(native._RESUME), registry.NATIVE_RESUME_AGENTS)
        self.assertEqual(tuple(native._RESOLVERS), registry.NATIVE_RESUME_AGENTS)
        self.assertEqual(teach.MD_TARGETS, teach.manifest_targets("markdown"))
        self.assertEqual(teach.SKILL_TARGETS, teach.manifest_targets("skill"))
        self.assertTrue(all(
            hasattr(live.LiveWatcher, method)
            for method in live.LIVE_TICKS.values()
        ))

    def test_indexd_build_identity_covers_registry_contract(self) -> None:
        manifest = json.loads(
            (common.PY_DIR / "runtime_manifest.json").read_text(encoding="utf-8"))
        expected = {
            item["member"].removeprefix("agrep/py/")
            for item in manifest["files"]
            if item["member"].startswith("agrep/py/")
        }
        self.assertEqual(indexd_runtime.INDEXD_BUILD_FILES, tuple(sorted(expected)))
        self.assertIn("indexd.py", indexd_runtime.INDEXD_BUILD_FILES)
        self.assertEqual(
            indexd_runtime.INDEXD_BUILD_ID,
            indexd_runtime._python_runtime_digest().hex()[:20],
        )

    def test_indexd_build_identity_ignores_local_python_scratch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            py_dir = Path(td)
            (py_dir / "runtime_manifest.json").write_text(json.dumps({
                "version": 1,
                "files": [
                    {"source": "py/indexd.py", "member": "agrep/py/indexd.py"},
                    {
                        "source": "py/runtime_manifest.json",
                        "member": "agrep/py/runtime_manifest.json",
                    },
                ],
            }), encoding="utf-8")
            (py_dir / "indexd.py").touch()
            (py_dir / "_check.py").touch()
            with mock.patch.object(common, "PY_DIR", py_dir):
                self.assertEqual(
                    indexd_runtime._indexd_build_files(),
                    ("indexd.py", "runtime_manifest.json"),
                )

    def test_indexd_build_identity_rejects_collapsed_manifest(self) -> None:
        cases = (
            ([], "empty Python runtime manifest"),
            (
                [{"source": "py/indexd.py", "member": "agrep/py/indexd.py"}],
                "runtime manifest must list itself",
            ),
        )
        for files, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as td:
                py_dir = Path(td)
                (py_dir / "runtime_manifest.json").write_text(
                    json.dumps({"version": 1, "files": files}),
                    encoding="utf-8",
                )
                with mock.patch.object(common, "PY_DIR", py_dir), \
                        self.assertRaisesRegex(RuntimeError, message):
                    indexd_runtime._indexd_build_files()

    def test_capabilities_are_complete_partitions_with_reasons(self) -> None:
        all_agents = set(registry.ADAPTER_NAMES)
        for supported, unsupported in (
                (registry.LIVE_AGENTS, registry.LIVE_UNSUPPORTED),
                (registry.NATIVE_RESUME_AGENTS,
                 registry.NATIVE_RESUME_UNSUPPORTED),
                (registry.AGENT_CONTEXT_AGENTS,
                 registry.AGENT_CONTEXT_UNSUPPORTED),
                (registry.TEACH_AGENTS, registry.TEACH_UNSUPPORTED)):
            self.assertFalse(set(supported) & set(unsupported))
            self.assertEqual(set(supported) | set(unsupported), all_agents)
            self.assertTrue(all(reason.strip()
                                for reason in unsupported.values()))

    def test_agent_context_keys_are_derived_once_from_supported_adapters(self) -> None:
        expected = tuple(
            key
            for adapter in registry.REGISTRY.adapters
            for key in adapter.agent_context.env_keys
        )
        self.assertEqual(registry.AGENT_CONTEXT_ENV_KEYS, expected)
        self.assertEqual(len(expected), len(set(expected)))
        self.assertTrue(all(
            adapter.agent_context.env_keys
            for adapter in registry.REGISTRY.adapters
            if adapter.agent_context.supported
        ))

    def test_perf_fixtures_clear_manifest_agent_context(self) -> None:
        perf = runpy.run_path(
            str(common.REPO_ROOT / "bench" / "perf.py"),
            run_name="agrep_perf_contract",
        )
        ambient = {
            key: "fixture"
            for key in registry.AGENT_CONTEXT_ENV_KEYS
        }
        with mock.patch.dict(os.environ, ambient, clear=False):
            isolated = perf["_private_environment"]()
        self.assertTrue(
            set(registry.AGENT_CONTEXT_ENV_KEYS).isdisjoint(isolated))

    def test_agent_aliases_are_validated_and_normalized_by_the_manifest(self) -> None:
        aliases = {
            alias: adapter.name
            for adapter in registry.REGISTRY.adapters
            for alias in adapter.aliases
        }
        self.assertTrue(aliases)
        self.assertEqual(
            registry.ADAPTER_INPUT_NAMES,
            registry.REGISTRY.input_names,
        )
        for alias, canonical in aliases.items():
            self.assertEqual(registry.normalize_agent_name(alias), canonical)
            self.assertEqual(
                registry.capability_error("native_resume", alias),
                registry.capability_error("native_resume", canonical),
            )
        payload = _registry_payload()
        payload["adapters"][0]["aliases"] = [
            payload["adapters"][1]["name"]]
        with self.assertRaisesRegex(registry.RegistryError, "collide"):
            registry.registry_from_payload(payload)

    def test_omp_is_a_canonicalized_pi_filter_alias(self) -> None:
        self.assertIn("omp", common.KNOWN_AGENTS)
        self.assertEqual(common.normalize_agent_name("omp"), "pi")
        self.assertIsNone(common.agent_filter_error(["query", "--agent", "omp"]))

    def test_teach_targets_resolve_portably_from_the_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            for kind in ("markdown", "skill"):
                specs = tuple(
                    target for target in registry.TEACH_TARGETS
                    if target.kind == kind
                )
                resolved = teach.manifest_targets(
                    kind, home=home, environ={}, os_name="posix")
                self.assertEqual(
                    [label for label, _, _ in resolved],
                    [target.label for target in specs],
                )
                for (_, proof, target), spec in zip(resolved, specs):
                    proof_root = (
                        home / ".local" / "share" / "opencode"
                        if spec.proof.base == "opencode_data" else home
                    )
                    target_root = (
                        home / ".local" / "share" / "opencode"
                        if spec.target.base == "opencode_data" else home
                    )
                    self.assertEqual(proof, proof_root.joinpath(*spec.proof.parts))
                    self.assertEqual(target, target_root.joinpath(*spec.target.parts))

    def test_active_codex_home_is_an_additional_teach_target(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            active = home / "seat" / "codex-active"
            resolved = teach.manifest_targets(
                "markdown", home=home,
                environ={"CODEX_HOME": str(active)}, os_name="posix")
        codex = [(proof, target) for label, proof, target in resolved
                 if label == "codex"]
        self.assertEqual(codex, [
            (home / ".codex", home / ".codex" / "AGENTS.md"),
            (active, active / "AGENTS.md"),
        ])

    def test_every_teach_target_is_structurally_required(self) -> None:
        payload = _registry_payload()
        referenced = {
            target_id
            for owner in (
                *registry.REGISTRY.adapters,
                *registry.REGISTRY.teach_clients,
            )
            for target_id in owner.teach.target_ids
        }
        self.assertEqual(
            referenced,
            {target.target_id for target in registry.REGISTRY.teach_targets},
        )
        for target_id in referenced:
            mutated = copy.deepcopy(payload)
            mutated["teach_targets"] = [
                target for target in mutated["teach_targets"]
                if target["id"] != target_id
            ]
            with self.subTest(target_id=target_id), self.assertRaisesRegex(
                    registry.RegistryError, "missing teach targets"):
                registry.registry_from_payload(mutated)

    def test_unsupported_teach_capability_cannot_leave_an_active_target(self) -> None:
        for owner_group in ("adapters", "teach_clients"):
            payload = _registry_payload()
            owner = payload[owner_group][0]
            target_id = owner["teach"]["target_ids"][0]
            owner["teach"] = {
                "state": "unsupported",
                "reason": "fixture has no writable instruction surface",
            }
            with self.subTest(owner_group=owner_group), self.assertRaisesRegex(
                    registry.RegistryError, "unreferenced teach targets"):
                registry.registry_from_payload(payload)
            payload["teach_targets"] = [
                target for target in payload["teach_targets"]
                if target["id"] != target_id
            ]
            parsed = registry.registry_from_payload(payload)
            self.assertNotIn(
                target_id,
                {target.target_id for target in parsed.active_teach_targets},
            )

    def test_teach_target_labels_are_canonical_and_control_free(self) -> None:
        for label in (" leading", "trailing ", "delete\u007f"):
            payload = _registry_payload()
            payload["teach_targets"][0]["label"] = label
            with self.subTest(label=repr(label)), self.assertRaisesRegex(
                    registry.RegistryError, "label is invalid"):
                registry.registry_from_payload(payload)

    def test_tail_help_derives_the_live_filter_vocabulary(self) -> None:
        stdout = io.StringIO()
        with mock.patch.object(sys, "argv", ["agrep tail", "--help"]), \
                contextlib.redirect_stdout(stdout), \
                self.assertRaises(SystemExit) as stopped:
            tail.main()
        self.assertEqual(stopped.exception.code, 0)
        self.assertIn("/".join(registry.LIVE_AGENTS), stdout.getvalue())

    def test_tail_event_filter_vocabulary_matches_the_emitter(self) -> None:
        tree = ast.parse(Path(live.__file__).read_text(encoding="utf-8"))
        emitted = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if not (isinstance(key, ast.Constant) and key.value == "type"):
                    continue
                emitted.update(
                    item.value for item in ast.walk(value)
                    if isinstance(item, ast.Constant)
                    and isinstance(item.value, str)
                )
        self.assertEqual(tail.LIVE_EVENT_TYPES, emitted)

    def test_manifest_rejects_duplicate_names(self) -> None:
        payload = _registry_payload()
        payload["adapters"].append(copy.deepcopy(payload["adapters"][0]))
        with self.assertRaisesRegex(registry.RegistryError, "duplicate"):
            registry.registry_from_payload(payload)

    def test_manifest_rejects_unexplained_unsupported_capability(self) -> None:
        payload = _registry_payload()
        adapter = next(
            item for item in payload["adapters"]
            if item["live"]["state"] == "unsupported"
        )
        adapter["live"].pop("reason")
        with self.assertRaisesRegex(registry.RegistryError, "needs an unsupported reason"):
            registry.registry_from_payload(payload)

    def test_manifest_rejects_unknown_schema(self) -> None:
        payload = _registry_payload()
        payload["adapters"][0]["surprise"] = True
        with self.assertRaisesRegex(registry.RegistryError, "must contain"):
            registry.registry_from_payload(payload)

    def test_manifest_rejects_boolean_version(self) -> None:
        payload = _registry_payload()
        payload["version"] = True
        with self.assertRaisesRegex(
                registry.RegistryError, "unsupported agent registry version"):
            registry.registry_from_payload(payload)

    def test_run_dispatch_and_rejection_follow_native_resume_capability(self) -> None:
        retired = mock.patch.object(legacy_cleanup, "retire_removed_explorer")
        for agent in registry.NATIVE_RESUME_AGENTS:
            with self.subTest(agent=agent), retired, \
                    mock.patch.object(sys, "argv", ["agrep", "run", agent]), \
                    mock.patch.object(cli, "cmd_run", return_value=17) as run:
                self.assertEqual(cli._main(), 17)
                run.assert_called_once()
        for adapter in registry.REGISTRY.adapters:
            if not adapter.native_resume.supported:
                continue
            for alias in adapter.aliases:
                with self.subTest(alias=alias), \
                        mock.patch.object(
                            legacy_cleanup, "retire_removed_explorer"), \
                        mock.patch.object(
                            sys, "argv", ["agrep", "run", alias]), \
                        mock.patch.object(
                            cli, "cmd_run", return_value=17) as run:
                    self.assertEqual(cli._main(), 17)
                    run.assert_called_once()
        for agent, reason in registry.NATIVE_RESUME_UNSUPPORTED.items():
            stderr = io.StringIO()
            calls = []
            real_run = cli.cmd_run

            def run(args):
                calls.append(args)
                return real_run(args)

            with self.subTest(agent=agent), \
                    mock.patch.object(
                        legacy_cleanup, "retire_removed_explorer"), \
                    mock.patch.object(sys, "argv", ["agrep", "run", agent]), \
                    mock.patch.object(cli, "cmd_run", side_effect=run), \
                    contextlib.redirect_stderr(stderr):
                self.assertEqual(cli._main(), 2)
            self.assertEqual(len(calls), 1)
            self.assertIn(reason, stderr.getvalue())

    def test_known_unsupported_capabilities_explain_why(self) -> None:
        self.assertIn(
            registry.LIVE_UNSUPPORTED["kimi"],
            registry.capability_error("live", "kimi"),
        )
        self.assertIn(
            registry.NATIVE_RESUME_UNSUPPORTED["cursor"],
            registry.capability_error("native_resume", "cursor"),
        )
        self.assertIn(
            "supported for live observation",
            registry.capability_error("live", "not-an-agent"),
        )
        self.assertEqual(registry.capability_error("live", "codex"), "")

    def test_live_cli_filters_reject_unsupported_agents_before_startup(self) -> None:
        for entrypoint, argv in (
                (tail.main, ["agrep tail", "--agent", "kimi"]),
                (livetui.main, ["agrep board", "--agent", "kimi", "--once"])):
            stderr = io.StringIO()
            with self.subTest(entrypoint=entrypoint.__module__), \
                    mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(live, "watcher") as watcher, \
                    contextlib.redirect_stderr(stderr), \
                    self.assertRaises(SystemExit) as stopped:
                entrypoint()
            self.assertEqual(stopped.exception.code, 2)
            self.assertIn(registry.LIVE_UNSUPPORTED["kimi"],
                          stderr.getvalue())
            watcher.assert_not_called()

    def test_tail_snapshot_honors_agent_and_session_filters(self) -> None:
        watcher = mock.Mock()
        watcher.snapshot.return_value = {
            "n_subs": 9,
            "n_emitted": 100,
            "n_tracked": 30,
            "n_loops": 40,
            "last_err": "codex source outside this scope failed",
            "degraded_sources": [
                {"agent": "claude", "path": "claude-store", "error": "denied"},
                {"agent": "codex", "path": "codex-store", "error": "denied"},
            ],
            "sessions": [
                {"agent": "claude", "session": "keep-this", "working": True,
                 "recent": [{"type": "done"}]},
                {"agent": "claude", "session": "drop-this"},
                {"agent": "codex", "session": "keep-this"},
            ],
        }
        stdout = io.StringIO()
        with mock.patch.object(
                sys, "argv",
                ["agrep tail", "--snapshot", "--agent", "Claude",
                 "--session", "keep"]), \
                mock.patch.object(live, "watcher", return_value=watcher), \
                mock.patch.object(
                    tail.indexd_runtime, "resident_indexd_live_snapshot",
                    return_value=None), \
                contextlib.redirect_stdout(stdout):
            self.assertEqual(tail.main(), 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(
            payload["sessions"],
            [{"agent": "claude", "session": "keep-this", "working": True,
              "recent": [{"type": "done"}]}],
        )
        self.assertEqual(payload["type"], "snapshot")
        self.assertEqual(
            payload["counts"],
            {"sessions": 1, "working": 1, "recent_events": 1})
        for name in ("n_subs", "n_emitted", "n_tracked", "n_loops"):
            self.assertNotIn(name, payload)
        self.assertNotIn("last_err", payload)
        self.assertEqual(
            payload["degraded_sources"],
            [{"agent": "claude", "path": "claude-store", "error": "denied"}])

    def test_session_only_tail_snapshot_omits_unscoped_diagnostics(self) -> None:
        snapshot = {
            "sessions": [{"agent": "claude", "session": "keep-this"}],
            "last_err": "global failure",
            "degraded_sources": [
                {"agent": "codex", "path": "outside", "error": "denied"}],
        }
        stdout = io.StringIO()
        with mock.patch.object(
                tail.indexd_runtime, "resident_indexd_live_snapshot",
                return_value=snapshot), contextlib.redirect_stdout(stdout):
            self.assertEqual(
                tail.main(["--snapshot", "--session", "keep"]), 0)
        payload = json.loads(stdout.getvalue())
        self.assertNotIn("last_err", payload)
        self.assertNotIn("degraded_sources", payload)

    def test_live_cli_rejects_empty_filters_before_startup(self) -> None:
        cases = (
            (tail.main, ["agrep tail", "--agent", ","]),
            (tail.main, ["agrep tail", "--events", ""]),
            (tail.main, ["agrep tail", "--events", "typo"]),
            (tail.main, ["agrep tail", "--events", "all,done"]),
            (livetui.main, ["agrep board", "--agent", "", "--once"]),
            (livetui.main, ["agrep board", "--session", "", "--once"]),
        )
        for entrypoint, argv in cases:
            with self.subTest(argv=argv), \
                    mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(live, "watcher") as watcher, \
                    contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as stopped:
                entrypoint()
            self.assertEqual(stopped.exception.code, 2)
            watcher.assert_not_called()

    def test_tail_event_filter_is_case_insensitive(self) -> None:
        events = mock.Mock()
        events.get.side_effect = [
            {"type": "done", "agent": "codex", "session": "s"},
            KeyboardInterrupt,
        ]
        watcher = mock.Mock()
        watcher.subscribe.return_value = events
        stdout = io.StringIO()
        with mock.patch.object(
                sys, "argv", ["agrep tail", "--events", "Done"]), \
                mock.patch.object(live, "watcher", return_value=watcher), \
                contextlib.redirect_stdout(stdout):
            self.assertEqual(tail.main(), 130)
        payloads = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(payloads[0]["events"], ["done"])
        self.assertEqual(payloads[1]["type"], "done")
        watcher.unsubscribe.assert_called_once_with(events)

    def test_tail_unsubscribes_when_the_output_pipe_closes(self) -> None:
        events = mock.Mock()
        watcher = mock.Mock()
        watcher.subscribe.return_value = events
        with mock.patch.object(sys, "argv", ["agrep tail"]), \
                mock.patch.object(live, "watcher", return_value=watcher), \
                mock.patch.object(
                    tail, "_line", side_effect=BrokenPipeError):
            self.assertEqual(tail.main(), 0)
        watcher.unsubscribe.assert_called_once_with(events)

    @unittest.skipIf(sys.platform == "win32", "POSIX terminal contract")
    def test_board_restores_terminal_when_screen_entry_fails(self) -> None:
        class FailingScreen(io.StringIO):
            def isatty(self) -> bool:
                return True

            def write(self, value: str) -> int:
                if value == "\x1b[?1049h\x1b[?25l":
                    raise OSError("screen unavailable")
                return super().write(value)

        stdout = FailingScreen()
        terminal_state = ["saved"]
        with mock.patch.object(sys, "argv", ["agrep board"]), \
                mock.patch.object(sys, "stdout", stdout), \
                mock.patch.object(livetui, "_enable_ansi", return_value=True), \
                mock.patch.object(live, "watcher", return_value=mock.Mock()), \
                mock.patch.object(
                    livetui.termios, "tcgetattr", return_value=terminal_state), \
                mock.patch.object(livetui.tty, "setcbreak"), \
                mock.patch.object(livetui.termios, "tcsetattr") as restore, \
                self.assertRaisesRegex(OSError, "screen unavailable"):
            livetui.main()
        restore.assert_called_once_with(
            sys.stdin, livetui.termios.TCSADRAIN, terminal_state)


if __name__ == "__main__":
    unittest.main()
