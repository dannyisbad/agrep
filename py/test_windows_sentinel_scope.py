"""Per-data-root identity and legacy-task ownership for the Windows sentinel."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
from xml.sax.saxutils import escape

from _test_support import isolate_data_dir

isolate_data_dir()
import teach  # noqa: E402

LEGACY = teach._LEGACY_TASK_NAME


def _task_xml(command: str, arguments: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\r\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
        "<Actions Context=\"Author\"><Exec>"
        f"<Command>{escape(command)}</Command>"
        f"<Arguments>{escape(arguments)}</Arguments>"
        "</Exec></Actions></Task>\r\n"
    ).encode("utf-8")


class FakeScheduler:
    """schtasks as a task table: /Create, /Delete, /Query and /Query /XML."""

    def __init__(self) -> None:
        self.tasks: dict[str, tuple[str, str]] = {}
        self.deleted: list[str] = []

    def register(self, name: str, command: str, watcher: Path) -> None:
        self.tasks[name] = (f'"{command}"', f'"{watcher}"')

    def run(self, argv, **kwargs):
        text = kwargs.get("text", False)
        empty = "" if text else b""
        if argv[0] != "schtasks":
            return subprocess.CompletedProcess(argv, 1, stdout=empty, stderr=empty)
        name = argv[argv.index("/TN") + 1]
        if argv[1] == "/Create":
            command, _, arguments = argv[argv.index("/TR") + 1].partition('" "')
            self.tasks[name] = (command + '"', '"' + arguments)
            return subprocess.CompletedProcess(argv, 0, stdout=empty, stderr=empty)
        if argv[1] == "/Delete":
            if self.tasks.pop(name, None) is None:
                return subprocess.CompletedProcess(argv, 1, stdout=empty, stderr=empty)
            self.deleted.append(name)
            return subprocess.CompletedProcess(argv, 0, stdout=empty, stderr=empty)
        if name not in self.tasks:
            return subprocess.CompletedProcess(argv, 1, stdout=empty, stderr=empty)
        if "/XML" in argv:
            return subprocess.CompletedProcess(
                argv, 0, stdout=_task_xml(*self.tasks[name]), stderr=b"")
        return subprocess.CompletedProcess(argv, 0, stdout=empty, stderr=empty)


class WindowsSentinelScopeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.saved = {name: getattr(teach, name) for name in (
            "HOME", "REPO", "STATE_PATH", "MD_TARGETS", "SKILL_TARGETS")}
        self.saved_data = teach.common.DATA_DIR
        teach.HOME = self.home
        teach.REPO = self.root / "repo"
        teach.REPO.mkdir()
        teach.MD_TARGETS = []
        teach.SKILL_TARGETS = []
        self.scheduler = FakeScheduler()

    def tearDown(self) -> None:
        for name, value in self.saved.items():
            setattr(teach, name, value)
        teach.common.DATA_DIR = self.saved_data
        self.temp.cleanup()

    def _use_root(self, name: str) -> Path:
        data = self.root / name
        data.mkdir(exist_ok=True)
        teach.common.DATA_DIR = data
        teach.STATE_PATH = data / "teach.json"
        return data

    def _install(self) -> bool:
        with mock.patch.object(teach.subprocess, "run", side_effect=self.scheduler.run), \
                mock.patch.object(teach.subprocess, "Popen"), \
                mock.patch.object(teach.common, "windows_background_child_flags",
                                  return_value=0), \
                mock.patch.object(teach, "_pythonw", return_value="pythonw.exe"), \
                mock.patch.object(teach, "sentinel_armed", return_value=True):
            return teach._sentinel_install_win([])

    def _retire(self) -> bool:
        with mock.patch.object(teach.subprocess, "run", side_effect=self.scheduler.run):
            return teach._retire_legacy_windows_sentinel()

    def _watcher_mutex(self, data: Path, *, already_exists: bool = False) -> str | None:
        parsed = ast.parse(teach._SENTINEL_WATCH_PY)
        prologue = []
        for node in parsed.body:
            if isinstance(node, ast.FunctionDef):
                break
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                prologue.append(node)
        kernel32 = mock.Mock()
        kernel32.GetLastError.return_value = 183 if already_exists else 0
        namespace = {
            "json": json, "sys": teach.sys, "Path": Path,
            "ctypes": mock.Mock(windll=mock.Mock(kernel32=kernel32)),
            "wintypes": mock.Mock(),
            "__file__": str(data / "sentinel_watch.py"),
        }
        try:
            exec(compile(ast.Module(body=prologue, type_ignores=[]), "<sentinel>", "exec"),
                 namespace)
        except SystemExit:
            return None
        return kernel32.CreateMutexW.call_args.args[2]

    def _seed_config(self, data: Path) -> None:
        (data / "sentinel.json").write_text(json.dumps({
            "cli": str(teach.REPO / "cli.py"), "task_name": teach._sentinel_task_name(),
        }), encoding="utf-8")

    def test_watchers_of_two_roots_hold_distinct_mutexes(self) -> None:
        names = []
        for root in ("a", "b"):
            data = self._use_root(root)
            self._seed_config(data)
            names.append(self._watcher_mutex(data))
        self.assertTrue(all(names))
        self.assertNotEqual(names[0], names[1])

    def test_second_watcher_of_the_same_root_exits(self) -> None:
        data = self._use_root("a")
        self._seed_config(data)
        self.assertIsNone(self._watcher_mutex(data, already_exists=True))

    def test_watcher_without_its_config_exits_before_claiming_identity(self) -> None:
        data = self._use_root("a")
        self.assertIsNone(self._watcher_mutex(data))

    def test_installed_config_names_the_scoped_task(self) -> None:
        data = self._use_root("a")
        self.assertTrue(self._install())
        cfg = json.loads((data / "sentinel.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["task_name"], teach._sentinel_task_name())
        self.assertNotEqual(cfg["task_name"], LEGACY)

    def test_install_for_another_root_preserves_a_foreign_legacy_task(self) -> None:
        a = self._use_root("a")
        self.scheduler.register(LEGACY, "pythonw.exe", a / "sentinel_watch.py")
        self._use_root("b")
        self.assertTrue(self._install())
        self.assertIn(LEGACY, self.scheduler.tasks)
        self.assertIn(teach._sentinel_task_name(), self.scheduler.tasks)
        self.assertEqual(self.scheduler.deleted, [])

    def test_install_migrates_an_owned_legacy_task_and_keeps_the_neighbour(self) -> None:
        a = self._use_root("a")
        self.scheduler.register(LEGACY, "pythonw.exe", a / "sentinel_watch.py")
        self._use_root("b")
        self.assertTrue(self._install())
        b_task = teach._sentinel_task_name()
        self._use_root("a")
        self.assertTrue(self._install())
        self.assertNotIn(LEGACY, self.scheduler.tasks)
        self.assertEqual(
            set(self.scheduler.tasks), {b_task, teach._sentinel_task_name()})
        self.assertEqual(self.scheduler.deleted, [LEGACY])

    def test_owned_legacy_task_is_recognised_through_another_spelling(self) -> None:
        a = self._use_root("a")
        alias = self.root / "alias"
        try:
            alias.symlink_to(a, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        self.scheduler.register(LEGACY, "pythonw.exe", alias / "sentinel_watch.py")
        self.assertTrue(self._retire())
        self.assertEqual(self.scheduler.deleted, [LEGACY])

    def _exported(self, xml: bytes) -> tuple[bool, list[list[str]]]:
        calls: list[list[str]] = []

        def run(argv, **_kwargs):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout=xml, stderr=b"")

        with mock.patch.object(teach.subprocess, "run", side_effect=run):
            return teach._retire_legacy_windows_sentinel(), calls

    def test_unreadable_legacy_task_blocks_the_cutover(self) -> None:
        self._use_root("a")
        retired, calls = self._exported(b"<Task><Actions>")
        self.assertFalse(retired)
        self.assertEqual([argv[1] for argv in calls], ["/Query"])

    def test_legacy_task_without_an_exec_action_is_left_alone(self) -> None:
        self._use_root("a")
        retired, calls = self._exported(
            b'<Task xmlns="x"><Actions><ComHandler/></Actions></Task>')
        self.assertTrue(retired)
        self.assertEqual([argv[1] for argv in calls], ["/Query"])

    def test_failed_deletion_of_an_owned_legacy_task_reports_failure(self) -> None:
        a = self._use_root("a")
        self.scheduler.register(LEGACY, "pythonw.exe", a / "sentinel_watch.py")
        real_run = self.scheduler.run

        def sticky(argv, **kwargs):
            if argv[1] == "/Delete":
                return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"")
            return real_run(argv, **kwargs)

        with mock.patch.object(teach.subprocess, "run", side_effect=sticky):
            self.assertFalse(teach._retire_legacy_windows_sentinel())
        self.assertIn(LEGACY, self.scheduler.tasks)

    def test_install_writes_nothing_when_the_legacy_task_cannot_be_retired(self) -> None:
        data = self._use_root("a")
        with mock.patch.object(teach, "_retire_legacy_windows_sentinel",
                               return_value=False), \
                mock.patch.object(teach.subprocess, "run") as run, \
                mock.patch.object(teach.subprocess, "Popen") as popen:
            self.assertFalse(teach._sentinel_install_win([]))
        run.assert_not_called()
        popen.assert_not_called()
        self.assertFalse((data / "sentinel.json").exists())
        self.assertFalse((data / "sentinel_watch.py").exists())

    def test_unavailable_schtasks_reports_failure(self) -> None:
        self._use_root("a")
        with mock.patch.object(teach.subprocess, "run",
                               side_effect=FileNotFoundError("schtasks")):
            self.assertFalse(teach._retire_legacy_windows_sentinel())


if __name__ == "__main__":
    unittest.main()
