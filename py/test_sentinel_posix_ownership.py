"""Per-data-root identity and legacy-job ownership for the launchd/systemd sentinels."""

from __future__ import annotations

import contextlib
from pathlib import Path
import plistlib
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from _test_support import isolate_data_dir

isolate_data_dir()
import teach  # noqa: E402

LEGACY_LABEL = teach._LEGACY_LAUNCHD_LABEL
LEGACY_UNIT = teach._LEGACY_TASK_NAME
UID = 501


def _completed(argv, rc, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr=stderr)


class FakeLaunchd:
    """One per-uid gui domain: `jobs` maps a label to (plist, ProgramArguments)."""

    def __init__(self) -> None:
        self.jobs: dict[str, tuple[Path, list[str]]] = {}
        self.stuck: set[str] = set()
        self.calls: list[list[str]] = []

    @staticmethod
    def _label(target: str) -> str:
        return target.rsplit("/", 1)[1]

    def run(self, argv, **_kwargs):
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] != "launchctl":
            raise AssertionError(f"unexpected command: {argv}")
        verb = argv[1]
        if verb == "print":
            label = self._label(argv[2])
            if label not in self.jobs:
                return _completed(argv, 113, stderr=(
                    "Bad request.\nCould not find service "
                    f'"{label}" in domain for user gui: {UID}\n'))
            plist, arguments = self.jobs[label]
            report = [f"{argv[2]} = {{", "\tactive count = 0",
                      f"\tpath = {plist}", "\ttype = LaunchAgent",
                      "\tstate = not running", ""]
            if arguments:
                report += [f"\tprogram = {arguments[0]}", "\targuments = {",
                           *(f"\t\t{argument}" for argument in arguments),
                           "\t}", ""]
            report += ["\tdefault environment = {",
                       "\t\tPATH => /usr/bin:/bin:/usr/sbin:/sbin", "\t}", "}"]
            return _completed(argv, 0, stdout="\n".join(report) + "\n")
        if verb == "bootout":
            label = self._label(argv[2])
            if label not in self.jobs:
                return _completed(argv, 3,
                                  stderr="Boot-out failed: 3: No such process\n")
            if label not in self.stuck:
                del self.jobs[label]
            return _completed(argv, 0)
        if verb in ("bootstrap", "load"):
            plist = Path(argv[-1])
            with plist.open("rb") as stream:
                body = plistlib.load(stream)
            self.jobs[body["Label"]] = (plist, list(body["ProgramArguments"]))
            return _completed(argv, 0)
        if verb == "unload":
            plist = Path(argv[2])
            for label, (loaded_from, _) in list(self.jobs.items()):
                if loaded_from == plist and label not in self.stuck:
                    del self.jobs[label]
                    return _completed(argv, 0)
            return _completed(argv, 1,
                              stderr="Unload failed: 5: Input/output error\n")
        raise AssertionError(f"unexpected launchctl verb: {argv}")


class FakeSystemd:
    """A user manager: `services` maps a loaded service to its argv; wants
    links under `unit_dir` mirror enablement the way systemctl maintains them."""

    def __init__(self, unit_dir: Path) -> None:
        self.unit_dir = unit_dir
        self.services: dict[str, str] = {}
        self.enabled: set[str] = set()
        self.active: set[str] = set()
        self.hung: set[str] = set()
        self.calls: list[tuple[str, ...]] = []

    def link(self, unit: str) -> Path:
        group = ("paths.target.wants" if unit.endswith(".path")
                 else "timers.target.wants")
        return self.unit_dir / group / unit

    def systemctl(self, *args: str, timeout_s: float | None = None,
                  observation: dict | None = None) -> int:
        del timeout_s
        self.calls.append(args)
        verb = args[0]
        if verb in self.hung:
            if observation is not None:
                observation.update(
                    state="budget-exceeded",
                    detail="systemd sentinel verification exceeded its budget")
            return 1

        def done(rc: int, stdout: str = "") -> int:
            if observation is not None:
                observation.update(
                    state="complete", detail=f"systemctl returned {rc}",
                    stdout=stdout, stderr="")
            return rc

        unit = args[-1]
        if verb == "daemon-reload":
            return done(0)
        if verb == "is-system-running":
            return done(0, "running")
        if verb == "show":
            argv = self.services.get(args[1])
            if argv is None:
                return done(0, "LoadState=not-found")
            return done(0, (
                "LoadState=loaded\n"
                f"ExecStart={{ path=/bin/sh ; argv[]={argv} ; ignore_errors=no ;"
                " start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ;"
                " status=0/0 }"))
        if verb == "enable":
            self.enabled.add(unit)
            self.active.add(unit)
            link = self.link(unit)
            link.parent.mkdir(parents=True, exist_ok=True)
            if not link.is_symlink():
                link.symlink_to(self.unit_dir / unit)
            return done(0)
        if verb == "disable":
            for name in args[2:]:
                self.enabled.discard(name)
                self.active.discard(name)
                link = self.link(name)
                if link.is_symlink():
                    link.unlink()
            return done(0)
        if verb == "is-enabled":
            return done(0, "enabled") if unit in self.enabled else done(1, "disabled")
        if verb == "is-active":
            return done(0, "active") if unit in self.active else done(3, "inactive")
        raise AssertionError(f"unexpected systemctl verb: {args}")


def _legacy_plist(path: Path, script: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        plistlib.dump({
            "Label": LEGACY_LABEL,
            "ProgramArguments": ["/bin/sh", str(script)],
            "WatchPaths": ["/opt/agrep"],
            "StartInterval": 3600,
            "RunAtLoad": False,
        }, stream)


class PosixSentinelOwnershipTest(unittest.TestCase):
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
        self.root_a = self.root / "data-a"
        self.root_b = self.root / "data-b"
        self.root_a.mkdir()
        self.root_b.mkdir()
        self.use(self.root_b)

    def tearDown(self) -> None:
        for name, value in self.saved.items():
            setattr(teach, name, value)
        teach.common.DATA_DIR = self.saved_data
        self.temp.cleanup()

    def use(self, data: Path) -> None:
        teach.common.DATA_DIR = data
        teach.STATE_PATH = data / "teach.json"

    def legacy_plist(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{LEGACY_LABEL}.plist"

    def mac(self, launchd: FakeLaunchd) -> contextlib.ExitStack:
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(
            teach, "sys", SimpleNamespace(**{**vars(teach.sys), "platform": "darwin"})))
        stack.enter_context(mock.patch.object(
            teach.os, "getuid", return_value=UID, create=True))
        stack.enter_context(mock.patch.object(
            teach.subprocess, "run", side_effect=launchd.run))
        return stack

    def linux(self, systemd: FakeSystemd) -> contextlib.ExitStack:
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(
            teach, "sys", SimpleNamespace(**{**vars(teach.sys), "platform": "linux"})))
        stack.enter_context(mock.patch.object(
            teach, "_systemctl_user", side_effect=systemd.systemctl))
        return stack

    def legacy_units(self, script: Path) -> tuple[Path, Path, Path]:
        unit_dir = teach._systemd_unit_dir()
        unit_dir.mkdir(parents=True, exist_ok=True)
        service = unit_dir / f"{LEGACY_UNIT}.service"
        timer = unit_dir / f"{LEGACY_UNIT}.timer"
        path_unit = unit_dir / f"{LEGACY_UNIT}.path"
        service.write_text(teach._SYSTEMD_SERVICE.format(script=script),
                           encoding="utf-8")
        timer.write_text(teach._SYSTEMD_TIMER, encoding="utf-8")
        path_unit.write_text(teach._SYSTEMD_PATH.format(cli="/opt/agrep/cli.py"),
                             encoding="utf-8")
        return service, timer, path_unit

    def test_mac_setup_for_one_root_keeps_another_roots_legacy_job(self) -> None:
        # Same HOME, different data root: the legacy plist sits in this HOME
        # but launchd's report names root A's script, so B owns none of it.
        launchd = FakeLaunchd()
        foreign = self.root_a / "sentinel.sh"
        _legacy_plist(self.legacy_plist(), foreign)
        launchd.jobs[LEGACY_LABEL] = (self.legacy_plist(), ["/bin/sh", str(foreign)])
        with self.mac(launchd):
            self.assertTrue(teach._sentinel_install_mac([]))
        self.assertEqual(launchd.jobs[LEGACY_LABEL][1], ["/bin/sh", str(foreign)])
        self.assertTrue(self.legacy_plist().is_file())
        self.assertEqual(launchd.jobs[teach._launchd_label()][1],
                         ["/bin/sh", str(self.root_b / "sentinel.sh")])

    def test_mac_upgrade_retires_only_this_roots_legacy_job(self) -> None:
        launchd = FakeLaunchd()
        own = self.root_b / "sentinel.sh"
        own.write_text("#!/bin/sh\n", encoding="utf-8")
        _legacy_plist(self.legacy_plist(), own)
        launchd.jobs[LEGACY_LABEL] = (self.legacy_plist(), ["/bin/sh", str(own)])
        with self.mac(launchd):
            self.assertTrue(teach._sentinel_install_mac([]))
        self.assertEqual(set(launchd.jobs), {teach._launchd_label()})
        self.assertFalse(self.legacy_plist().exists())
        self.assertTrue(teach._plist_path().is_file())

    def test_mac_unloaded_legacy_plist_is_removed_only_by_its_own_root(self) -> None:
        launchd = FakeLaunchd()
        _legacy_plist(self.legacy_plist(), self.root_a / "sentinel.sh")
        with self.mac(launchd):
            self.assertTrue(teach._sentinel_install_mac([]))
            self.assertTrue(self.legacy_plist().is_file())
            self.use(self.root_a)
            self.assertTrue(teach._sentinel_install_mac([]))
        self.assertFalse(self.legacy_plist().exists())
        self.assertEqual(len(launchd.jobs), 2)

    def test_mac_setup_stops_before_a_legacy_job_of_unknown_ownership(self) -> None:
        launchd = FakeLaunchd()
        launchd.jobs[LEGACY_LABEL] = (self.legacy_plist(), [])
        script = self.root_b / "sentinel.sh"
        script.write_text("legacy\n", encoding="utf-8")
        with self.mac(launchd):
            self.assertFalse(teach._sentinel_install_mac([]))
        self.assertIn(LEGACY_LABEL, launchd.jobs)
        self.assertEqual(script.read_text(encoding="utf-8"), "legacy\n")
        self.assertFalse(teach._plist_path().exists())

    def test_mac_remove_for_one_root_leaves_the_other_roots_job(self) -> None:
        launchd = FakeLaunchd()
        with self.mac(launchd):
            self.use(self.root_a)
            self.assertTrue(teach._sentinel_install_mac([]))
            label_a, plist_a = teach._launchd_label(), teach._plist_path()
            self.use(self.root_b)
            self.assertTrue(teach._sentinel_install_mac([]))
            self.assertNotEqual(label_a, teach._launchd_label())
            self.assertTrue(teach._sentinel_remove())
        self.assertEqual(set(launchd.jobs), {label_a})
        self.assertTrue(plist_a.is_file())
        self.assertTrue((self.root_a / "sentinel.sh").is_file())
        self.assertFalse(teach._plist_path().exists())
        self.assertFalse((self.root_b / "sentinel.sh").exists())

    def test_mac_remove_fails_closed_when_its_legacy_job_survives_bootout(self) -> None:
        launchd = FakeLaunchd()
        own = self.root_b / "sentinel.sh"
        with self.mac(launchd):
            self.assertTrue(teach._sentinel_install_mac([]))
            _legacy_plist(self.legacy_plist(), own)
            launchd.jobs[LEGACY_LABEL] = (self.legacy_plist(), ["/bin/sh", str(own)])
            launchd.stuck.add(LEGACY_LABEL)
            self.assertFalse(teach._sentinel_remove())
        self.assertIn(LEGACY_LABEL, launchd.jobs)
        self.assertTrue(self.legacy_plist().is_file())
        self.assertTrue(own.is_file())
        self.assertTrue(teach._plist_path().is_file())

    def test_linux_upgrade_retires_only_this_roots_legacy_units(self) -> None:
        systemd = FakeSystemd(teach._systemd_unit_dir())
        own = self.root_b / "sentinel.sh"
        legacy = self.legacy_units(own)
        systemd.services[f"{LEGACY_UNIT}.service"] = f"/bin/sh {own}"
        systemd.systemctl("enable", "--now", f"{LEGACY_UNIT}.timer")
        systemd.systemctl("enable", "--now", f"{LEGACY_UNIT}.path")
        with self.linux(systemd):
            self.assertTrue(teach._sentinel_install_linux([]))
        scoped = teach._sentinel_task_name()
        self.assertEqual(systemd.enabled, {f"{scoped}.timer", f"{scoped}.path"})
        self.assertEqual(systemd.active, systemd.enabled)
        for path in (*legacy, systemd.link(f"{LEGACY_UNIT}.timer"),
                     systemd.link(f"{LEGACY_UNIT}.path")):
            self.assertFalse(path.exists() or path.is_symlink(), path)
        self.assertTrue((teach._systemd_unit_dir() / f"{scoped}.service").is_file())

    def test_linux_setup_for_one_root_keeps_another_roots_legacy_units(self) -> None:
        systemd = FakeSystemd(teach._systemd_unit_dir())
        foreign = self.root_a / "sentinel.sh"
        legacy = self.legacy_units(foreign)
        systemd.services[f"{LEGACY_UNIT}.service"] = f"/bin/sh {foreign}"
        systemd.systemctl("enable", "--now", f"{LEGACY_UNIT}.timer")
        systemd.systemctl("enable", "--now", f"{LEGACY_UNIT}.path")
        with self.linux(systemd):
            self.assertTrue(teach._sentinel_install_linux([]))
        scoped = teach._sentinel_task_name()
        self.assertEqual(systemd.enabled, {
            f"{LEGACY_UNIT}.timer", f"{LEGACY_UNIT}.path",
            f"{scoped}.timer", f"{scoped}.path"})
        for path in legacy:
            self.assertTrue(path.is_file(), path)
        self.assertEqual(systemd.services[f"{LEGACY_UNIT}.service"],
                         f"/bin/sh {foreign}")

    def test_linux_remove_for_one_root_leaves_the_other_roots_units(self) -> None:
        systemd = FakeSystemd(teach._systemd_unit_dir())
        with self.linux(systemd):
            self.use(self.root_a)
            self.assertTrue(teach._sentinel_install_linux([]))
            name_a = teach._sentinel_task_name()
            self.use(self.root_b)
            self.assertTrue(teach._sentinel_install_linux([]))
            self.assertNotEqual(name_a, teach._sentinel_task_name())
            self.assertTrue(teach._sentinel_remove())
        unit_dir = teach._systemd_unit_dir()
        self.assertEqual(systemd.enabled, {f"{name_a}.timer", f"{name_a}.path"})
        self.assertTrue((unit_dir / f"{name_a}.service").is_file())
        self.assertTrue((self.root_a / "sentinel.sh").is_file())
        self.assertFalse((unit_dir / f"{teach._sentinel_task_name()}.service").exists())
        self.assertFalse((self.root_b / "sentinel.sh").exists())

    def test_linux_remove_fails_closed_when_legacy_inspection_times_out(self) -> None:
        systemd = FakeSystemd(teach._systemd_unit_dir())
        own = self.root_b / "sentinel.sh"
        with self.linux(systemd):
            self.assertTrue(teach._sentinel_install_linux([]))
            self.legacy_units(own)
            systemd.services[f"{LEGACY_UNIT}.service"] = f"/bin/sh {own}"
            systemd.hung.add("show")
            self.assertFalse(teach._sentinel_remove())
        self.assertTrue(own.is_file())
        self.assertTrue((teach._systemd_unit_dir()
                         / f"{teach._sentinel_task_name()}.service").is_file())
        self.assertIn(f"{LEGACY_UNIT}.service", systemd.services)


if __name__ == "__main__":
    unittest.main()
