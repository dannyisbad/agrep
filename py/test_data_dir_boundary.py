from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PY_DIR = Path(__file__).resolve().parent


class DataDirBoundaryTests(unittest.TestCase):
    @staticmethod
    def _import_events(configured: Path) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "AGREP_DATA_DIR": str(configured),
            "AGREP_DATA_DIR_SOURCE": "default",
            "AGREP_DATA_READONLY": str(configured),
            "PYTHONPATH": str(PY_DIR),
        }
        return subprocess.run(
            [
                sys.executable, "-c",
                "import events; print(events.DATA_DIR)",
            ],
            env=env, capture_output=True, text=True, check=True, timeout=15)

    def test_readonly_import_does_not_create_an_absent_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            configured = Path(temporary) / "protected-absent"
            completed = self._import_events(configured)
            self.assertEqual(completed.stdout.strip(), str(configured))
            self.assertFalse(configured.exists())

    def test_readonly_import_preserves_existing_data_dir_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            configured = Path(temporary) / "protected-existing"
            configured.mkdir(mode=0o700)
            sentinel = configured / "sentinel"
            sentinel.write_bytes(b"writer-owned bytes")
            changed = 1_700_000_000_000_000_000
            os.utime(sentinel, ns=(changed, changed))
            os.utime(configured, ns=(changed, changed))

            def snapshot() -> tuple:
                root = configured.stat()
                entries = tuple(
                    (
                        path.name,
                        path.stat().st_mode,
                        path.stat().st_mtime_ns,
                        path.stat().st_ctime_ns,
                        path.read_bytes(),
                    )
                    for path in sorted(configured.iterdir())
                )
                return (
                    root.st_mode, root.st_mtime_ns, root.st_ctime_ns, entries,
                )

            before = snapshot()
            completed = self._import_events(configured)
            self.assertEqual(completed.stdout.strip(), str(configured))
            self.assertEqual(snapshot(), before)

    def test_readonly_guard_normalizes_windows_path_case(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            configured = Path(temporary) / "protected"
            script = (
                "import os; from pathlib import Path; from unittest import mock; "
                "import events; "
                "guard=r'C:\\\\Users\\\\Example\\\\AppData\\\\Local\\\\agrep'; "
                "selected=Path(r'c:\\\\users\\\\example\\\\appdata\\\\local\\\\AGREP'); "
                "p1=mock.patch.dict(os.environ,{'AGREP_DATA_READONLY':guard},clear=False); "
                "p2=mock.patch.object(events.os.path,'realpath',"
                "side_effect=lambda value: os.fspath(value)); "
                "p3=mock.patch.object(events.os.path,'normcase',"
                "side_effect=lambda value: value.lower()); "
                "p1.start();p2.start();p3.start(); "
                "print(events.data_dir_readonly(selected))"
            )
            env = {
                **os.environ,
                "AGREP_DATA_DIR": str(configured),
                "AGREP_DATA_DIR_SOURCE": "env",
                "AGREP_DATA_READONLY": str(configured),
                "PYTHONPATH": str(PY_DIR),
            }
            completed = subprocess.run(
                [sys.executable, "-c", script], env=env,
                capture_output=True, text=True, check=True, timeout=15)
            self.assertEqual(completed.stdout.strip(), "True")
            self.assertFalse(configured.exists())

    def test_relative_setting_is_exported_absolute_before_child_cwd_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            child = (
                "import json,os,events; "
                "print(json.dumps({'data':str(events.DATA_DIR),"
                "'env':os.environ['AGREP_DATA_DIR']}))"
            )
            parent = (
                "import json,os,subprocess,sys,events; "
                "p=subprocess.run([sys.executable,'-c',os.environ['CHILD']],"
                "cwd=os.environ['SECOND'],env=dict(os.environ),capture_output=True,"
                "text=True,check=True); "
                "print(json.dumps({'data':str(events.DATA_DIR),"
                "'env':os.environ['AGREP_DATA_DIR'],'child':json.loads(p.stdout)}))"
            )
            env = {
                **os.environ,
                "AGREP_DATA_DIR": "relative-data",
                "AGREP_DATA_DIR_SOURCE": "env",
                "PYTHONPATH": str(PY_DIR),
                "CHILD": child,
                "SECOND": str(second),
            }
            completed = subprocess.run(
                [sys.executable, "-c", parent], cwd=first, env=env,
                capture_output=True, text=True, check=True, timeout=15)
            result = json.loads(completed.stdout)
            expected = str((first / "relative-data").resolve())
            self.assertEqual(result["data"], expected)
            self.assertEqual(result["env"], expected)
            self.assertEqual(result["child"], {"data": expected, "env": expected})
            self.assertFalse((second / "relative-data").exists())

    def test_absolute_setting_keeps_its_location(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            configured = Path(temporary) / "chosen"
            env = {
                **os.environ,
                "AGREP_DATA_DIR": str(configured),
                "AGREP_DATA_DIR_SOURCE": "env",
                "PYTHONPATH": str(PY_DIR),
            }
            completed = subprocess.run(
                [sys.executable, "-c", "import events;print(events.DATA_DIR)"],
                cwd=Path(temporary), env=env, capture_output=True, text=True,
                check=True, timeout=15)
            self.assertEqual(completed.stdout.strip(), str(configured))

    @unittest.skipIf(os.name == "nt", "POSIX privacy mode")
    def test_explicit_owned_data_dir_is_tightened_before_use(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            configured = Path(temporary) / "chosen"
            configured.mkdir(mode=0o755)
            configured.chmod(0o755)
            env = {
                **os.environ,
                "AGREP_DATA_DIR": str(configured),
                "AGREP_DATA_DIR_SOURCE": "env",
                "PYTHONPATH": str(PY_DIR),
            }
            completed = subprocess.run(
                [sys.executable, "-c", "import events;print(events.DATA_DIR)"],
                env=env, capture_output=True, text=True, check=True, timeout=15)
            self.assertEqual(completed.stdout.strip(), str(configured))
            self.assertEqual(configured.stat().st_mode & 0o777, 0o700)

    def test_symlink_data_dir_is_refused_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.mkdir(mode=0o755)
            target.chmod(0o755)
            configured = root / "chosen"
            configured.symlink_to(target, target_is_directory=True)
            env = {
                **os.environ,
                "AGREP_DATA_DIR": str(configured),
                "AGREP_DATA_DIR_SOURCE": "env",
            }
            completed = subprocess.run(
                [sys.executable, str(PY_DIR.parent / "cli.py"), "--help"],
                env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(completed.returncode, 2)
            self.assertEqual(completed.stdout, "")
            self.assertEqual(completed.stderr.count("\n"), 1)
            self.assertIn("not a symlink", completed.stderr)
            if os.name != "nt":
                self.assertEqual(target.stat().st_mode & 0o777, 0o755)

    @unittest.skipIf(os.name == "nt", "POSIX privacy mode")
    def test_unfixable_data_dir_mode_fails_before_product_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            configured = Path(temporary) / "chosen"
            configured.mkdir(mode=0o755)
            configured.chmod(0o755)
            script = (
                "import os,runpy; "
                "os.fchmod=lambda *_args: (_ for _ in ()).throw("
                "PermissionError('denied')); "
                "runpy.run_path(os.environ['AGREP_CLI'],run_name='__main__')"
            )
            env = {
                **os.environ,
                "AGREP_DATA_DIR": str(configured),
                "AGREP_DATA_DIR_SOURCE": "env",
                "AGREP_CLI": str(PY_DIR.parent / "cli.py"),
            }
            completed = subprocess.run(
                [sys.executable, "-c", script], env=env,
                capture_output=True, text=True, timeout=15)
            self.assertEqual(completed.returncode, 2)
            self.assertEqual(completed.stdout, "")
            self.assertEqual(completed.stderr.count("\n"), 1)
            self.assertIn("data directory unavailable", completed.stderr)
            self.assertEqual(configured.stat().st_mode & 0o777, 0o755)

    def test_deleted_startup_cwd_has_actionable_cli_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vanished = root / "vanished"
            vanished.mkdir()
            if os.name == "nt":
                script = (
                    "import os,pathlib,runpy; "
                    "from unittest import mock; "
                    "mock.patch.object(pathlib.Path,'cwd',"
                    "side_effect=OSError('unavailable')).start(); "
                    "runpy.run_path(os.environ['AGREP_CLI'],run_name='__main__')"
                )
            else:
                script = (
                    "import os,runpy; "
                    "os.chdir(os.environ['VANISHED']); "
                    "os.rmdir(os.environ['VANISHED']); "
                    "runpy.run_path(os.environ['AGREP_CLI'],run_name='__main__')"
                )
            env = {
                **os.environ,
                "AGREP_DATA_DIR": "relative-data",
                "AGREP_DATA_DIR_SOURCE": "env",
                "AGREP_CLI": str(PY_DIR.parent / "cli.py"),
                "VANISHED": str(vanished),
            }
            completed = subprocess.run(
                [sys.executable, "-c", script], cwd=root, env=env,
                capture_output=True, text=True, timeout=15)
            self.assertEqual(completed.returncode, 2)
            self.assertEqual(completed.stdout, "")
            self.assertNotIn("Traceback", completed.stderr)
            self.assertEqual(completed.stderr.count("\n"), 1)
            self.assertIn("startup working directory is unavailable",
                          completed.stderr)
            self.assertIn("use an absolute AGREP_DATA_DIR", completed.stderr)


if __name__ == "__main__":
    unittest.main()
