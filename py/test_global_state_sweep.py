from __future__ import annotations

import io
import subprocess
import tempfile
from pathlib import Path
import unittest
from unittest import mock

import global_state_sweep as sweep


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
    def _state(tree: set[str]) -> dict:
        return {
            "signals": {}, "env": {}, "cwd": "repo", "syspath": set(),
            "locale": "C", "limits": {}, "atexit": 0, "tree": tree,
        }

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
