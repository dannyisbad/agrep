from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import install_lag


OLD = "1" * 40
MASTER = "a" * 40
DAY = 24 * 60 * 60


class _Distribution:
    def __init__(self, site: Path, direct_relative: str) -> None:
        self.site = site
        self.files = [direct_relative]

    def locate_file(self, item: object) -> Path:
        return self.site / str(item)


class InstallLagTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="agrep-install-lag-")
        self.root = Path(self.temporary.name)
        self.site = self.root / "site"
        self.package = self.site / "agrep"
        self.module = self.package / "py" / "install_lag.py"
        self.module.parent.mkdir(parents=True)
        self.module.write_text("# installed runtime\n", encoding="utf-8")
        relative = "agrep-0.2.0.dist-info/direct_url.json"
        self.direct = self.site / relative
        self.direct.parent.mkdir()
        self.source = self.root / "source"
        (self.source / "agrep").mkdir(parents=True)
        (self.source / ".git").mkdir()
        (self.source / "pyproject.toml").write_text(
            '[project]\nname = "agrep"\n', encoding="utf-8")
        (self.source / "agrep" / "__init__.py").write_text(
            '__version__ = "0.2.0"\n', encoding="utf-8")
        (self.source / "py").mkdir()
        self.installed_payload = self.package / "payload.txt"
        self.source_payload = self.source / "payload.txt"
        self.installed_payload.write_text("same payload\n", encoding="utf-8")
        self.source_payload.write_text("same payload\n", encoding="utf-8")
        manifest = {
            "version": 1,
            "files": [
                {"source": "payload.txt", "member": "agrep/payload.txt"},
                {
                    "source": "py/runtime_manifest.json",
                    "member": "agrep/py/runtime_manifest.json",
                },
            ],
        }
        manifest_body = json.dumps(manifest, sort_keys=True)
        (self.package / "py" / "runtime_manifest.json").write_text(
            manifest_body, encoding="utf-8")
        (self.source / "py" / "runtime_manifest.json").write_text(
            manifest_body, encoding="utf-8")
        self.distribution = _Distribution(self.site, relative)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _payload(self, installed_time: int, *, value: dict | None = None) -> None:
        payload = value or {"url": self.source.as_uri(), "dir_info": {}}
        self.direct.write_text(json.dumps(payload), encoding="utf-8")
        os.utime(self.direct, (installed_time, installed_time))

    def _run(self, *, installed_time: int, master_time: int,
             now: int | None = None, bound: int = 7 * DAY) -> dict:
        self._payload(installed_time)
        with mock.patch.object(
                install_lag, "_git_revision",
                return_value=(MASTER, master_time)):
            return install_lag.installed_master_lag(
                now=now or master_time + DAY,
                module_path=self.module,
                distribution=self.distribution,
                bound_seconds=bound,
            )

    def _run_vcs(self, *, installed_time: int, master_time: int,
                 now: int | None = None, bound: int = 7 * DAY) -> dict:
        self._payload(
            installed_time,
            value={"url": self.source.as_uri(),
                   "vcs_info": {"vcs": "git", "commit_id": OLD}},
        )

        def revision(
                _source: Path, name: str, **_kwargs: object) -> tuple[str, int] | None:
            return ((MASTER, master_time) if name == "refs/heads/master"
                    else (OLD, installed_time) if name == OLD else None)

        with (mock.patch.object(
                  install_lag, "_git_revision", side_effect=revision),
              mock.patch.object(
                  install_lag, "_git_is_ancestor", return_value=True)):
            return install_lag.installed_master_lag(
                now=now or master_time + DAY,
                module_path=self.module,
                distribution=self.distribution,
                bound_seconds=bound,
            )

    def test_matching_local_source_is_exactly_current(self) -> None:
        result = self._run(installed_time=10 * DAY, master_time=18 * DAY)
        self.assertEqual(result["state"], "current")
        self.assertEqual(result["installed_basis"], "distribution-content")
        self.assertIsNone(result["installed_commit"])
        self.assertEqual(
            result["installed_distribution_id"],
            result["source_distribution_id"],
        )
        self.assertIn("matches recorded local source exactly", result["detail"])
        self.assertNotIn("gap_days", result)

    def test_local_source_difference_warns_immediately(self) -> None:
        self._payload(19 * DAY)
        self.source_payload.write_text("changed payload\n", encoding="utf-8")
        with mock.patch.object(install_lag, "_git_revision") as revision:
            result = install_lag.installed_master_lag(
                now=20 * DAY, module_path=self.module,
                distribution=self.distribution)
        revision.assert_not_called()
        self.assertEqual(result["state"], "lagging")
        self.assertEqual(result["installed_basis"], "distribution-content")
        self.assertNotEqual(
            result["installed_distribution_id"],
            result["source_distribution_id"],
        )
        self.assertIn("differs from recorded local source", result["detail"])
        self.assertEqual(result["remedy"], "replace-installed-tool")

    def test_exactly_seven_days_is_inside_the_commit_bound(self) -> None:
        result = self._run_vcs(
            installed_time=10 * DAY, master_time=17 * DAY)
        self.assertEqual(result["state"], "current")
        self.assertEqual(result["gap_days"], 7.0)
        self.assertIn("within 7-day local-master bound", result["detail"])

    def test_one_second_past_commit_bound_is_lagging(self) -> None:
        result = self._run_vcs(
            installed_time=10 * DAY, master_time=17 * DAY + 1)
        self.assertEqual(result["state"], "lagging")

    def test_vcs_commit_time_beats_mutable_install_metadata_time(self) -> None:
        now = 30 * DAY
        self._payload(
            29 * DAY,
            value={"url": self.source.as_uri(),
                   "vcs_info": {"vcs": "git", "commit_id": OLD}},
        )

        def revision(
                _source: Path, name: str, **_kwargs: object) -> tuple[str, int] | None:
            return ((MASTER, 20 * DAY) if name == "refs/heads/master"
                    else (OLD, 10 * DAY) if name == OLD else None)

        with (mock.patch.object(install_lag, "_git_revision", side_effect=revision),
              mock.patch.object(install_lag, "_git_is_ancestor", return_value=True)):
            result = install_lag.installed_master_lag(
                now=now, module_path=self.module, distribution=self.distribution)
        self.assertEqual(result["state"], "lagging")
        self.assertEqual(result["installed_basis"], "commit")
        self.assertEqual(result["installed_commit"], OLD)
        self.assertEqual(result["gap_days"], 10.0)

    def test_divergent_vcs_revision_is_not_called_lagging(self) -> None:
        self._payload(
            29 * DAY,
            value={"url": self.source.as_uri(),
                   "vcs_info": {"vcs": "git", "commit_id": OLD}},
        )

        def revision(
                _source: Path, name: str, **_kwargs: object) -> tuple[str, int] | None:
            return (MASTER, 20 * DAY) if name == "refs/heads/master" else (OLD, 10 * DAY)

        with (mock.patch.object(install_lag, "_git_revision", side_effect=revision),
              mock.patch.object(install_lag, "_git_is_ancestor", return_value=False)):
            result = install_lag.installed_master_lag(
                now=30 * DAY, module_path=self.module,
                distribution=self.distribution)
        self.assertEqual(result["state"], "unavailable")
        self.assertIn("not an ancestor", result["detail"])

    def test_unverified_vcs_ancestry_is_not_called_divergent(self) -> None:
        self._payload(
            29 * DAY,
            value={"url": self.source.as_uri(),
                   "vcs_info": {"vcs": "git", "commit_id": OLD}},
        )

        def revision(
                _source: Path, name: str, **_kwargs: object) -> tuple[str, int]:
            return ((MASTER, 20 * DAY) if name == "refs/heads/master"
                    else (OLD, 10 * DAY))

        with (mock.patch.object(
                  install_lag, "_git_revision", side_effect=revision),
              mock.patch.object(
                  install_lag, "_git_is_ancestor", return_value=None)):
            result = install_lag.installed_master_lag(
                now=30 * DAY, module_path=self.module,
                distribution=self.distribution)
        self.assertEqual(result["state"], "unavailable")
        self.assertIn("ancestry could not be verified", result["detail"])
        self.assertNotIn("not an ancestor", result["detail"])
        self.assertNotIn("lagging", result["detail"])

    def test_git_ancestor_probe_is_tristate(self) -> None:
        for returncode, expected in ((0, True), (1, False), (2, None)):
            with self.subTest(returncode=returncode), \
                    mock.patch.object(
                        install_lag.shutil, "which", return_value="/git"), \
                    mock.patch.object(
                        install_lag.time, "monotonic", return_value=1.0), \
                    mock.patch.object(
                        install_lag.subprocess, "run",
                        return_value=subprocess.CompletedProcess(
                            [], returncode)):
                self.assertIs(
                    install_lag._git_is_ancestor(
                        self.source, OLD, MASTER, deadline=1.2),
                    expected,
                )
        with mock.patch.object(
                install_lag.shutil, "which", return_value=None):
            self.assertIsNone(install_lag._git_is_ancestor(
                self.source, OLD, MASTER, deadline=1.2))
        for error in (
                OSError("git unavailable"),
                subprocess.TimeoutExpired("git", 0.2),
        ):
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(
                        install_lag.shutil, "which", return_value="/git"), \
                    mock.patch.object(
                        install_lag.time, "monotonic", return_value=1.0), \
                    mock.patch.object(
                        install_lag.subprocess, "run", side_effect=error):
                self.assertIsNone(install_lag._git_is_ancestor(
                    self.source, OLD, MASTER, deadline=1.2))

    def test_source_checkout_is_not_confused_with_an_installed_distribution(self) -> None:
        self._payload(10 * DAY)
        outside = self.root / "checkout" / "py" / "install_lag.py"
        outside.parent.mkdir(parents=True)
        outside.write_text("# dev\n", encoding="utf-8")
        with mock.patch.object(install_lag, "_git_revision") as revision:
            result = install_lag.installed_master_lag(
                now=20 * DAY, module_path=outside,
                distribution=self.distribution)
        self.assertEqual(result["state"], "not-installed")
        revision.assert_not_called()

    def test_installed_runtime_without_direct_url_is_unavailable(self) -> None:
        distribution = _Distribution(
            self.site, "agrep-0.2.0.dist-info/direct_url.json")
        distribution.files = []
        with mock.patch.object(install_lag, "_git_revision") as revision:
            result = install_lag.installed_master_lag(
                now=20 * DAY, module_path=self.module,
                distribution=distribution)
        self.assertEqual(result["state"], "unavailable")
        self.assertIn("no unique local-source provenance", result["detail"])
        revision.assert_not_called()

    def test_remote_direct_url_never_triggers_git_or_network(self) -> None:
        self._payload(
            10 * DAY,
            value={"url": "https://example.test/agrep.git",
                   "vcs_info": {"vcs": "git", "commit_id": OLD}},
        )
        with mock.patch.object(install_lag, "_git_revision") as revision:
            result = install_lag.installed_master_lag(
                now=20 * DAY, module_path=self.module,
                distribution=self.distribution)
        self.assertEqual(result["state"], "unavailable")
        revision.assert_not_called()

    def test_non_git_vcs_provenance_is_unavailable(self) -> None:
        self._payload(
            10 * DAY,
            value={"url": self.source.as_uri(),
                   "vcs_info": {"vcs": "hg", "commit_id": OLD}},
        )
        with mock.patch.object(install_lag, "_git_revision") as revision:
            result = install_lag.installed_master_lag(
                now=20 * DAY, module_path=self.module,
                distribution=self.distribution)
        self.assertEqual(result["state"], "unavailable")
        revision.assert_not_called()

    def test_nonstandard_length_commit_is_unavailable(self) -> None:
        self._payload(
            10 * DAY,
            value={"url": self.source.as_uri(),
                   "vcs_info": {"vcs": "git", "commit_id": "a" * 41}},
        )
        with mock.patch.object(install_lag, "_git_revision") as revision:
            result = install_lag.installed_master_lag(
                now=20 * DAY, module_path=self.module,
                distribution=self.distribution)
        self.assertEqual(result["state"], "unavailable")
        revision.assert_not_called()

    def test_duplicate_direct_url_keys_are_unavailable(self) -> None:
        source = json.dumps(self.source.as_uri())
        self.direct.write_text(
            f'{{"url":{source},"url":{source},"dir_info":{{}}}}',
            encoding="utf-8")
        with mock.patch.object(install_lag, "_git_revision") as revision:
            result = install_lag.installed_master_lag(
                now=20 * DAY, module_path=self.module,
                distribution=self.distribution)
        self.assertEqual(result["state"], "unavailable")
        self.assertIn("malformed", result["detail"])
        revision.assert_not_called()

    def test_malformed_and_oversize_metadata_fail_closed(self) -> None:
        cases = ("malformed", "oversize")
        for case in cases:
            with self.subTest(case=case):
                self.direct.unlink(missing_ok=True)
                if case == "malformed":
                    self.direct.write_bytes(b"{")
                elif case == "oversize":
                    self.direct.write_bytes(b"x" * (16 * 1024 + 1))
                with mock.patch.object(install_lag, "_git_revision") as revision:
                    result = install_lag.installed_master_lag(
                        now=20 * DAY, module_path=self.module,
                        distribution=self.distribution)
                self.assertEqual(result["state"], "unavailable")
                revision.assert_not_called()

    def test_symlink_metadata_fails_closed_without_windows_privilege(self) -> None:
        self.direct.unlink(missing_ok=True)
        self.direct.write_bytes(b"not followed")

        def classify() -> dict:
            with mock.patch.object(
                    install_lag, "_git_revision") as revision:
                result = install_lag.installed_master_lag(
                    now=20 * DAY, module_path=self.module,
                    distribution=self.distribution)
            revision.assert_not_called()
            return result

        # Creating symlinks may require Developer Mode on Windows. Keep the
        # top-level provenance path in the test and emulate only the lstat
        # result that the unavailable privilege prevents us from creating.
        real_lstat = Path.lstat
        observed = self.direct.lstat()
        symlink_stat = SimpleNamespace(
            st_mode=stat.S_IFLNK | 0o777,
            st_size=observed.st_size,
        )

        def lstat(path: Path, *args: object, **kwargs: object) -> object:
            if path == self.direct:
                return symlink_stat
            return real_lstat(path, *args, **kwargs)

        with mock.patch.object(
                Path, "lstat", autospec=True, side_effect=lstat):
            result = classify()
        self.assertEqual(result["state"], "unavailable")

    @unittest.skipUnless(hasattr(os, "geteuid"), "POSIX ownership check")
    def test_another_uid_provenance_is_rejected_before_git(self) -> None:
        self._payload(10 * DAY)
        with (mock.patch.object(os, "geteuid", return_value=os.geteuid() + 1),
              mock.patch.object(install_lag, "_git_revision") as revision):
            result = install_lag.installed_master_lag(
                now=20 * DAY, module_path=self.module,
                distribution=self.distribution)
        self.assertEqual(result["state"], "unavailable")
        revision.assert_not_called()

    @unittest.skipIf(os.name == "nt", "POSIX provenance modes")
    def test_group_or_world_writable_provenance_is_rejected(self) -> None:
        targets = (
            self.direct, self.source, self.source / "pyproject.toml",
            self.source / "agrep" / "__init__.py", self.source / ".git",
        )
        for target in targets:
            with self.subTest(target=target.name):
                self._payload(10 * DAY)
                original = target.stat().st_mode & 0o777
                target.chmod(original | 0o022)
                try:
                    with mock.patch.object(install_lag, "_git_revision") as revision:
                        result = install_lag.installed_master_lag(
                            now=20 * DAY, module_path=self.module,
                            distribution=self.distribution)
                    self.assertEqual(result["state"], "unavailable")
                    revision.assert_not_called()
                finally:
                    target.chmod(original)

    def test_direct_url_mutation_during_read_is_rejected(self) -> None:
        self._payload(10 * DAY)
        real_fstat = os.fstat
        observations = 0

        def changing_fstat(descriptor: int) -> object:
            nonlocal observations
            observed = real_fstat(descriptor)
            observations += 1
            if observations == 1:
                return observed
            fields = {
                name: getattr(observed, name)
                for name in ("st_dev", "st_ino", "st_mode", "st_size",
                             "st_mtime_ns", "st_ctime_ns", "st_uid")
            }
            fields["st_mtime_ns"] += 100 if os.name == "nt" else 1
            return SimpleNamespace(**fields)

        with mock.patch.object(install_lag.os, "fstat", side_effect=changing_fstat):
            self.assertIsNone(
                install_lag._read_regular(self.direct, 16 * 1024))

    def test_windows_change_token_mutation_during_read_is_rejected(self) -> None:
        self.direct.write_bytes(b"payload")
        before = (1, 2, 7, 100, 111)
        after = (1, 2, 7, 100, 222)
        held = (1, 2, 7, 100, 333)
        with mock.patch.object(install_lag.os, "name", "nt"), \
                mock.patch.object(
                    install_lag.fileops, "change_sensitive_file_identity",
                    side_effect=(before, after)) as changed, \
                mock.patch.object(
                    install_lag.fileops, "file_identity_fd",
                    return_value=held):
            self.assertIsNone(
                install_lag._read_regular(self.direct, 16 * 1024))
        self.assertEqual(changed.call_count, 2)

    def test_windows_change_time_closes_a_restored_content_proof_aba(self) -> None:
        self.direct.write_bytes(b"payload")
        real_fstat = os.fstat
        observations = 0

        def changing_fstat(descriptor: int) -> object:
            nonlocal observations
            observed = real_fstat(descriptor)
            observations += 1
            if observations == 1:
                return observed
            fields = {
                name: getattr(observed, name)
                for name in ("st_dev", "st_ino", "st_mode", "st_size",
                             "st_mtime_ns", "st_ctime_ns", "st_uid")
            }
            fields["st_ctime_ns"] += 1_000
            return SimpleNamespace(**fields)

        unchanged = (1, 2, 7, 7, 111)
        held = (1, 2, 7, 7, 333)
        with mock.patch.object(install_lag.os, "name", "nt"), \
                mock.patch.object(
                    install_lag.os, "fstat", side_effect=changing_fstat), \
                mock.patch.object(
                    install_lag.fileops, "change_sensitive_file_identity",
                    side_effect=(unchanged, unchanged)), \
                mock.patch.object(
                    install_lag.fileops, "file_identity_fd",
                    return_value=held):
            self.assertIsNone(
                install_lag._read_regular(self.direct, 16 * 1024))

    @unittest.skipUnless(os.name == "nt", "Windows FILETIME identity")
    def test_windows_identity_separates_path_and_handle_change_clocks(self) -> None:
        fields = {
            "st_dev": 1, "st_ino": 2, "st_mode": stat.S_IFREG,
            "st_size": 3, "st_mtime_ns": 1_000,
            "st_ctime_ns": 2_000, "st_birthtime_ns": 1_500, "st_uid": 0,
        }
        first = SimpleNamespace(**fields)
        restated = SimpleNamespace(
            **(fields | {"st_mtime_ns": 1_001, "st_ctime_ns": 2_001}))
        changed_metadata = SimpleNamespace(
            **(fields | {"st_mtime_ns": 1_001, "st_ctime_ns": 2_100}))
        changed_content = SimpleNamespace(
            **(fields | {"st_mtime_ns": 1_100, "st_ctime_ns": 2_000}))
        self.assertEqual(
            install_lag._stat_identity(first),
            install_lag._stat_identity(changed_metadata))
        self.assertEqual(
            install_lag._stat_identity(first, change_sensitive=True),
            install_lag._stat_identity(restated, change_sensitive=True))
        self.assertNotEqual(
            install_lag._stat_identity(first, change_sensitive=True),
            install_lag._stat_identity(changed_metadata, change_sensitive=True))
        self.assertNotEqual(
            install_lag._stat_identity(first),
            install_lag._stat_identity(changed_content))

    def test_future_commit_timestamp_is_not_credible(self) -> None:
        result = self._run_vcs(
            installed_time=10 * DAY, master_time=30 * DAY,
            now=20 * DAY)
        self.assertEqual(result["state"], "unavailable")
        self.assertNotIn("gap_days", result)

    def test_git_probe_has_a_hard_timeout_and_discards_error_output(self) -> None:
        expired = subprocess.TimeoutExpired("git", install_lag._GIT_TOTAL_SECONDS)
        with (mock.patch.object(install_lag.shutil, "which", return_value="/git"),
              mock.patch.object(install_lag.time, "monotonic", return_value=10.0),
              mock.patch.object(install_lag.subprocess, "run", side_effect=expired) as run):
            self.assertIsNone(
                install_lag._git_revision(
                    self.source, "refs/heads/master", deadline=10.35))
        self.assertAlmostEqual(run.call_args.kwargs["timeout"], 0.35)
        self.assertIs(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_expired_caller_deadline_defers_before_metadata_or_git(self) -> None:
        with (
            mock.patch.object(
                install_lag.time, "monotonic", return_value=10.0),
            mock.patch.object(
                install_lag.metadata, "distribution") as distribution,
            mock.patch.object(install_lag, "_git_revision") as revision,
        ):
            result = install_lag.installed_master_lag(deadline=10.0)
        self.assertEqual(result["state"], "budget-exceeded")
        self.assertIn("shared routine diagnostic budget", result["detail"])
        distribution.assert_not_called()
        revision.assert_not_called()

    def test_caller_deadline_bounds_the_existing_git_deadline(self) -> None:
        self._payload(
            10 * DAY,
            value={"url": self.source.as_uri(),
                   "vcs_info": {"vcs": "git", "commit_id": OLD}},
        )
        deadlines: list[float] = []

        def revision(
                _source: Path, name: str, *,
                deadline: float) -> tuple[str, int]:
            deadlines.append(deadline)
            return ((MASTER, 10 * DAY) if name == "refs/heads/master"
                    else (OLD, 10 * DAY))

        with (
            mock.patch.object(
                install_lag.time, "monotonic",
                side_effect=(50.0, 50.01)),
            mock.patch.object(
                install_lag, "_git_revision", side_effect=revision),
            mock.patch.object(
                install_lag, "_git_is_ancestor", return_value=True),
        ):
            result = install_lag.installed_master_lag(
                now=20 * DAY, module_path=self.module,
                distribution=self.distribution, deadline=50.12)
        self.assertEqual(result["state"], "current")
        self.assertEqual(deadlines, [50.12, 50.12])

    def test_git_exhausting_caller_deadline_is_explicitly_deferred(self) -> None:
        self._payload(
            10 * DAY,
            value={"url": self.source.as_uri(),
                   "vcs_info": {"vcs": "git", "commit_id": OLD}},
        )
        with (
            mock.patch.object(
                install_lag.time, "monotonic",
                side_effect=(100.0, 100.0, 100.11)),
            mock.patch.object(
                install_lag, "_git_revision", return_value=None),
        ):
            result = install_lag.installed_master_lag(
                now=20 * DAY, module_path=self.module,
                distribution=self.distribution, deadline=100.1)
        self.assertEqual(result["state"], "budget-exceeded")
        self.assertNotIn("lagging", result["detail"])

    def test_vcs_git_probes_share_one_total_deadline(self) -> None:
        self._payload(
            29 * DAY,
            value={"url": self.source.as_uri(),
                   "vcs_info": {"vcs": "git", "commit_id": OLD}},
        )
        completed = subprocess.CompletedProcess(
            [], 0, stdout=f"{MASTER}\0{20 * DAY}\n".encode("ascii"))
        with (
            mock.patch.object(install_lag.shutil, "which", return_value="/git"),
            mock.patch.object(
                install_lag.time, "monotonic",
                side_effect=(100.0, 100.0, 100.36)),
            mock.patch.object(
                install_lag.subprocess, "run", return_value=completed) as run,
        ):
            result = install_lag.installed_master_lag(
                now=30 * DAY, module_path=self.module,
                distribution=self.distribution)
        self.assertEqual(result["state"], "unavailable")
        self.assertIn("cannot be verified", result["detail"])
        run.assert_called_once()

    def test_git_probe_strips_all_inherited_git_selectors(self) -> None:
        inherited = {
            "PATH": "/safe/path", "GIT_DIR": "/attacker/repo",
            "git_work_tree": "/attacker/tree", "GIT_OBJECT_DIRECTORY": "/objects",
        }
        with mock.patch.dict(install_lag.os.environ, inherited, clear=True):
            environment = install_lag._git_environment()
        self.assertEqual(environment["PATH"], "/safe/path")
        self.assertNotIn("GIT_DIR", environment)
        self.assertNotIn("git_work_tree", environment)
        self.assertNotIn("GIT_OBJECT_DIRECTORY", environment)
        self.assertEqual(environment["GIT_CONFIG_NOSYSTEM"], "1")

    def test_invalid_bound_is_rejected(self) -> None:
        for value in (-1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                install_lag.installed_master_lag(bound_seconds=value)  # type: ignore[arg-type]

    def test_invalid_shared_deadline_is_rejected(self) -> None:
        for value in (True, float("inf"), "soon"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                install_lag.installed_master_lag(  # type: ignore[arg-type]
                    deadline=value)


if __name__ == "__main__":
    unittest.main()
