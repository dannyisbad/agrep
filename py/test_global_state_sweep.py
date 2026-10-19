from __future__ import annotations

import io
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest
from unittest import mock

import global_state_sweep as sweep


class PlatformImportTests(unittest.TestCase):
    def test_module_imports_without_posix_resource(self) -> None:
        result = subprocess.run(
            [
                sys.executable, "-c",
                "import sys; sys.modules['resource'] = None; "
                "import global_state_sweep as sweep; "
                "assert sweep._LIMITS == ()",
            ],
            cwd=Path(sweep.__file__).resolve().parent,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class SourceInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="agrep-state-inventory-")
        self.repo = Path(self._tmp.name)
        subprocess.run(
            ["git", "init", "-q"], cwd=self.repo, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (self.repo / ".gitignore").write_text(
            ".venv/\nbuild/\n", encoding="utf-8", newline="\n")
        (self.repo / "tracked.py").write_text("VALUE = 1\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", ".gitignore", "tracked.py"], cwd=self.repo,
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def _state(tree: set[str], namespaces: set[str] = frozenset()) -> dict:
        return {
            "signals": {}, "env": {}, "cwd": "repo", "syspath": set(),
            "locale": "C", "limits": {}, "atexit": 0, "tree": tree,
            "namespaces": set(namespaces),
        }

    def test_a_namespace_minted_in_the_shared_base_is_a_leak(self) -> None:
        tree = sweep._python_source_inventory(self.repo)
        kept = {"agrep-semantic-v1-501-" + "a" * 20}
        minted = {"agrep-semantic-v1-501-" + "b" * 20}
        self.assertEqual(sweep._leaks(
            self._state(tree, kept), self._state(tree, kept | minted)), [
                "semantic coordination namespace left in the shared base: "
                + next(iter(minted)),
            ])
        # One removed by the module (the harness scrubbing its own) is not a leak.
        self.assertEqual(sweep._leaks(
            self._state(tree, kept | minted), self._state(tree, kept)), [])

    def test_only_registered_data_dirs_are_observed(self) -> None:
        """A stranger minting in the shared base during a module is never
        attributed to it; a data dir this process handed out is."""
        import _test_support
        base = _test_support.coordination_base_path()
        with tempfile.TemporaryDirectory(prefix="agrep-sweep-attrib-") as raw:
            root = Path(raw)
            (root / "mine").mkdir()
            mine = _test_support.register_data_dir(root / "mine").name
            # A stranger's data dir is not under any fixture tree this process made.
            theirs = _test_support.coordination_namespace_for(
                Path("/nonexistent/other-worktree/data")).name
            before = sweep._coordination_namespaces()
            self.assertNotIn(mine, before)
            try:
                (base / theirs).mkdir(mode=0o700)   # a concurrent stranger
                (base / mine).mkdir(mode=0o700)     # this module's own data dir
                after = sweep._coordination_namespaces()
                self.assertIn(mine, after - before)
                self.assertNotIn(theirs, after)
            finally:
                for name in (mine, theirs):
                    try:
                        (base / name).rmdir()
                    except OSError:
                        pass

    def test_namespace_leak_line_names_its_data_dir_and_module(self) -> None:
        """A namespace minted late by module A's detached process surfaces while
        module B runs; the line must still point at A's data dir and A."""
        import _test_support
        base = _test_support.coordination_base_path()
        with tempfile.TemporaryDirectory(prefix="agrep-sweep-origin-") as raw:
            data_dir = Path(raw) / "data"
            data_dir.mkdir()
            with _test_support.registering_for("test_module_a"):
                name = _test_support.register_data_dir(data_dir).name
            before = sweep._snapshot()
            self.assertNotIn(name, before["namespaces"])
            try:
                with _test_support.registering_for("test_module_b"):
                    (base / name).mkdir(mode=0o700)
                    after = sweep._snapshot()
                    lines = [line for line in sweep._leaks(before, after) if name in line]
            finally:
                (base / name).rmdir()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn(str(data_dir), lines[0])
        self.assertIn("test_module_a", lines[0])
        self.assertNotIn("test_module_b", lines[0])

    def test_inventory_excludes_ignored_dependency_trees(self) -> None:
        for directory in (".venv", "build"):
            path = self.repo / directory
            path.mkdir()
            (path / "generated.py").write_text("VALUE = 2\n", encoding="utf-8")
        (self.repo / "untracked.py").write_text("VALUE = 3\n", encoding="utf-8")

        self.assertEqual(
            sweep._python_source_inventory(self.repo),
            {"tracked.py", "untracked.py"})

    def test_added_and_removed_sources_are_reported_as_a_leak(self) -> None:
        before = sweep._python_source_inventory(self.repo)
        (self.repo / "tracked.py").unlink()
        (self.repo / "added.py").write_text("VALUE = 4\n", encoding="utf-8")
        after = sweep._python_source_inventory(self.repo)

        self.assertEqual(sweep._leaks(
            self._state(before), self._state(after)), [
                "repo .py files added ['added.py'] removed ['tracked.py']",
            ])


class ResultFailureTests(unittest.TestCase):
    def test_main_fails_when_a_discovered_module_cannot_load(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-state-loader-") as td:
            root = Path(td)
            (root / "test_agrep_missing_sweep_target.py").write_text(
                "", encoding="utf-8")
            with mock.patch.object(sweep, "PY_DIR", root), \
                    mock.patch.object(sweep, "MIN_MODULES", 1), \
                    mock.patch.object(
                        sweep, "_snapshot", return_value={"atexit": 0}), \
                    mock.patch.object(sweep, "_leaks", return_value=[]), \
                    mock.patch("_test_support.isolate_data_dir"), \
                    mock.patch("builtins.print") as printed:
                rc = sweep.main()

        output = "\n".join(str(call.args[0]) for call in printed.call_args_list)
        self.assertEqual(rc, 1)
        self.assertIn(
            "test_agrep_missing_sweep_target: test run failed", output)

    def test_loader_failed_test_is_a_sweep_failure(self) -> None:
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromName(
            "agrep_definitely_missing_global_state_module")
        result = unittest.TextTestRunner(
            stream=io.StringIO(), verbosity=0).run(suite)

        self.assertFalse(result.wasSuccessful())
        failures = sweep._result_failures("missing", result)
        self.assertEqual(
            failures[0],
            "missing: test run failed (0 failure(s), 1 error(s))")
        self.assertEqual(len(failures), 2)
        self.assertRegex(
            failures[1],
            r"^missing: error .*agrep_definitely_missing_global_state_module$")

    def test_assertion_failure_is_a_sweep_failure(self) -> None:
        class FailingCase(unittest.TestCase):
            def runTest(self) -> None:
                self.fail("fixture failure")

        result = unittest.TextTestRunner(
            stream=io.StringIO(), verbosity=0).run(FailingCase())

        failures = sweep._result_failures("failing", result)
        self.assertEqual(
            failures[0],
            "failing: test run failed (1 failure(s), 0 error(s))")
        self.assertEqual(len(failures), 2)
        self.assertRegex(failures[1], r"^failing: failure .*FailingCase\.runTest$")


if __name__ == "__main__":
    unittest.main()
