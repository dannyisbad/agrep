"""CLI orientation must not depend on writer-runtime inspection."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class StartupTests(unittest.TestCase):
    def test_help_remains_available_when_runtime_manifest_is_unreadable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-startup-") as temporary:
            root = Path(temporary)
            script = """\
from pathlib import Path
import runpy
import sys

read_text = Path.read_text

def unreadable_manifest(path, *args, **kwargs):
    if path.name == "runtime_manifest.json":
        raise PermissionError("fixture runtime manifest is unreadable")
    return read_text(path, *args, **kwargs)

Path.read_text = unreadable_manifest
sys.argv = ["agrep", "--help"]
runpy.run_module("agrep", run_name="__main__")
"""
            env = {
                key: value for key, value in os.environ.items()
                if not key.startswith("AGREP_")
            }
            env.update({
                "HOME": str(root),
                "AGREP_HOME": str(root / "home"),
                "AGREP_DATA_DIR": str(root / "data"),
                "AGREP_NO_DAEMON": "1",
                "AGREP_NO_SEM_WORKER": "1",
                "PYTHONPATH": os.pathsep.join((str(ROOT), str(ROOT / "py"))),
            })
            result = subprocess.run(
                [sys.executable, "-c", script], cwd=ROOT, env=env,
                capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertIn("usage: agrep", result.stdout)
            self.assertIn("agrep <command> --help", result.stdout)
            self.assertIn("agrep search index", result.stdout)
            self.assertTrue((root / "data").is_dir())


if __name__ == "__main__":
    unittest.main()
