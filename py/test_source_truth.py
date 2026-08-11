from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from _test_support import isolate_data_dir


isolate_data_dir()
import common  # noqa: E402
import doctor  # noqa: E402
import indexd_runtime  # noqa: E402


class SourceTruthTests(unittest.TestCase):
    def test_huge_integer_source_health_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-source-health-") as raw:
            root = Path(raw)
            (root / ".source-health.json").write_text(
                '{"code":"source-unreadable","issues":['
                + ('9' * 5000) + ']}', encoding="utf-8")
            with mock.patch.object(common, "DATA_DIR", root):
                failure = indexd_runtime._source_health_failure()
        self.assertIsNotNone(failure)
        self.assertEqual(failure.code, "source-unreadable")

    def test_doctor_distinguishes_unreadable_from_absent_indexed_store(self) -> None:
        unreadable = [{
            "name": "claude",
            "files": 0,
            "state": "source-unreadable",
            "issues": [{
                "path": "/fixture/.claude/projects",
                "kind": "permission-denied",
            }],
        }]
        with mock.patch.object(
                doctor.common, "store_freshness", return_value=unreadable):
            rows = doctor._store_counts(("claude",))
        self.assertEqual(rows[0]["state"], "source-unreadable")
        self.assertEqual(
            rows[0]["issues"][0]["kind"], "permission-denied")
        with mock.patch.object(
                doctor.common, "store_freshness", return_value=[]):
            rows = doctor._store_counts(("claude",))
        self.assertEqual(rows, [{
            "agent": "claude",
            "found": 0,
            "state": "absent-indexed",
            "issues": [],
        }])

    def test_doctor_store_status_uses_normal_absence_path(self) -> None:
        status, detail = doctor._store_status([])
        self.assertEqual(status, doctor.MISS)
        self.assertEqual(
            detail,
            "none under ~ - start a supported agent session, then re-run doctor",
        )

    def test_never_installed_agent_stays_normal_absence_end_to_end(self) -> None:
        exe = (
            common.REPO_ROOT / "target" / "release"
            / ("agrep-rs.exe" if os.name == "nt" else "agrep-rs")
        )
        if not exe.exists():
            self.skipTest("release ingest binary is required for the cross-surface proof")
        with tempfile.TemporaryDirectory(prefix="agrep-never-installed-home-") as home_raw, \
                tempfile.TemporaryDirectory(
                    prefix="agrep-never-installed-data-") as data_raw:
            home = Path(home_raw)
            data = Path(data_raw)
            self.assertFalse((home / ".cline").exists())
            env = os.environ.copy()
            for key in (
                    "USERPROFILE", "HOME", "APPDATA", "CLINE_DIR",
                    "CRUSH_GLOBAL_DATA", "LOCALAPPDATA", "OPENCODE_DB",
                    "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
                env.pop(key, None)
            env.update({
                "AGREP_HOME": str(home),
                "AGREP_DATA_DIR": str(data),
                "AGREP_RS_BIN": str(exe),
            })
            indexed = subprocess.run(
                [str(exe), "index", "--agent", "cline"],
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
                check=False,
            )
            self.assertEqual(indexed.returncode, 0, indexed.stderr)
            self.assertFalse((data / ".source-health.json").exists())
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(common, "DATA_DIR", data), \
                    mock.patch.object(common, "ingest_bin", return_value=exe):
                rows = doctor._store_counts()
                status, detail = doctor._store_status(rows)
            self.assertEqual(rows, [])
            self.assertEqual(status, doctor.MISS)
            self.assertEqual(
                detail,
                "none under ~ - start a supported agent session, then re-run doctor",
            )

    def test_doctor_store_status_keeps_permission_remedy_for_chmod_failure(self) -> None:
        status, detail = doctor._store_status([{
            "agent": "claude",
            "found": 0,
            "state": "source-unreadable",
            "issues": [{
                "path": "/fixture/.claude/projects",
                "kind": "permission-denied",
            }],
        }])
        self.assertEqual(status, doctor.MISS)
        self.assertIn(
            "restore read permission / grant disk access",
            detail,
        )

    def test_doctor_store_status_gives_incomplete_read_its_own_remedy(self) -> None:
        status, detail = doctor._store_status([{
            "agent": "cline",
            "found": 0,
            "state": "source-unreadable",
            "issues": [{
                "path": "/fixture/.cline",
                "kind": "source-read-incomplete",
            }],
        }])
        self.assertEqual(status, doctor.MISS)
        self.assertIn(f"retry `{doctor._cli_command('index')}`", detail)
        self.assertNotIn(
            "restore read permission / grant disk access",
            detail,
        )

    def test_doctor_store_status_renders_each_distinct_issue_remedy(self) -> None:
        status, detail = doctor._store_status([{
            "agent": "cline",
            "found": 0,
            "state": "source-unreadable",
            "issues": [
                {
                    "path": "/fixture/.cline/locked",
                    "kind": "permission-denied",
                },
                {
                    "path": "/fixture/.cline/moving",
                    "kind": "source-read-incomplete",
                },
            ],
        }])
        self.assertEqual(status, doctor.MISS)
        self.assertIn("restore read permission / grant disk access", detail)
        self.assertIn(f"retry `{doctor._cli_command('index')}`", detail)
        self.assertIn("/fixture/.cline/locked", detail)
        self.assertIn("/fixture/.cline/moving", detail)

    @unittest.skipUnless(
        os.name == "posix" and hasattr(os, "chmod"),
        "chmod-000 source proof requires POSIX permissions",
    )
    def test_chmod_000_store_reaches_doctor_permission_remedy(self) -> None:
        exe = (
            common.REPO_ROOT / "target" / "release"
            / ("agrep-rs.exe" if os.name == "nt" else "agrep-rs")
        )
        if not exe.exists():
            self.skipTest("release ingest binary is required for the cross-surface proof")
        with tempfile.TemporaryDirectory(prefix="agrep-chmod-store-home-") as home_raw, \
                tempfile.TemporaryDirectory(
                    prefix="agrep-chmod-store-data-") as data_raw:
            home = Path(home_raw)
            data = Path(data_raw)
            state = home / ".cline" / "data" / "state"
            state.mkdir(parents=True)
            (state / "taskHistory.json").write_text("[]", encoding="utf-8")
            store = home / ".cline"
            env = os.environ.copy()
            for key in (
                    "USERPROFILE", "HOME", "APPDATA", "CLINE_DIR",
                    "CRUSH_GLOBAL_DATA", "LOCALAPPDATA", "OPENCODE_DB",
                    "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
                env.pop(key, None)
            env.update({
                "AGREP_HOME": str(home),
                "AGREP_DATA_DIR": str(data),
                "AGREP_RS_BIN": str(exe),
                "CLINE_DIR": str(store),
            })
            original_mode = store.stat().st_mode
            try:
                store.chmod(0)
                indexed = subprocess.run(
                    [str(exe), "index", "--agent", "cline"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env=env,
                    check=False,
                )
            finally:
                store.chmod(original_mode)
            self.assertEqual(indexed.returncode, 0, indexed.stderr)
            health = json.loads(
                (data / ".source-health.json").read_text(encoding="utf-8"))
            self.assertTrue(any(
                issue.get("kind") == "permission-denied"
                for issue in health["issues"]
            ), health)
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(common, "DATA_DIR", data), \
                    mock.patch.object(common, "ingest_bin", return_value=exe):
                rows = doctor._store_counts()
                status, detail = doctor._store_status(rows)
            self.assertEqual(status, doctor.MISS)
            self.assertIn(
                "restore read permission / grant disk access",
                detail,
            )

    def test_doctor_overlays_durable_file_read_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-source-store-") as raw:
            root = Path(raw)
            failed = root / "session.jsonl"
            (root / ".source-health.json").write_text(json.dumps({
                "code": "source-unreadable",
                "issues": [{
                    "agent": "claude",
                    "path": str(failed),
                    "kind": "source-read-failed",
                    "reason": "permission denied",
                }],
            }), encoding="utf-8")
            registry = [{
                "name": "claude", "files": 1,
                "state": "available", "issues": [],
            }]
            with mock.patch.object(doctor.common, "DATA_DIR", root), \
                    mock.patch.object(
                        doctor.common, "store_freshness", return_value=registry):
                rows = doctor._store_counts(("claude",))
        self.assertEqual(rows[0]["state"], "source-unreadable")
        self.assertEqual(rows[0]["issues"][0]["path"], str(failed))

    def test_doctor_fails_closed_on_malformed_source_health(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-source-store-") as raw:
            root = Path(raw)
            health = root / ".source-health.json"
            health.write_text("{", encoding="utf-8")
            with mock.patch.object(doctor.common, "DATA_DIR", root), \
                    mock.patch.object(
                        doctor.common, "store_freshness", return_value=[]):
                rows = doctor._store_counts(("claude",))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["agent"], "all")
        self.assertEqual(rows[0]["state"], "source-unreadable")
        self.assertEqual(
            rows[0]["issues"][0]["kind"], "source-health-unavailable")

    def test_doctor_fails_closed_when_source_health_cannot_be_read(self) -> None:
        error = PermissionError("permission denied")
        with mock.patch.object(doctor.ownerfile, "snapshot", side_effect=error), \
                mock.patch.object(
                    doctor.common, "store_freshness", return_value=[]):
            rows = doctor._store_counts(("claude",))
        self.assertEqual(rows[0]["state"], "source-unreadable")
        self.assertEqual(rows[0]["issues"][0]["path"], str(
            doctor.common.DATA_DIR / ".source-health.json"))
        self.assertEqual(len(rows), 1)

    def test_source_unreadable_marker_cannot_rearm_green(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-source-health-") as raw:
            root = Path(raw)
            messages = root / "messages.jsonl"
            signature = root / ".ingest.sig"
            health = root / ".source-health.json"
            messages.write_text("{}\n", encoding="utf-8")
            signature.write_text("1:fixture\n", encoding="utf-8")
            health.write_text(json.dumps({
                "code": "source-unreadable",
                "issues": [{
                    "agent": "claude",
                    "path": "/fixture/.claude/projects",
                    "kind": "permission-denied",
                    "reason": "permission denied",
                }],
            }), encoding="utf-8")
            body = health.read_bytes()
            missing = indexd_runtime._IndexdOwnerInspection(
                indexd_runtime._IndexdOwnerState.ABSENT,
                None,
                None,
                None,
            )
            with mock.patch.object(common, "DATA_DIR", root), \
                    mock.patch.object(common, "MESSAGES_PATH", messages), \
                    mock.patch.object(common, "INGEST_SIG_PATH", signature), \
                    mock.patch.object(
                        common, "ingest_bin", return_value=Path(__file__)), \
                    mock.patch.object(
                        indexd_runtime, "indexd_failing", return_value=(0, "")), \
                    mock.patch.object(
                        indexd_runtime, "indexd_resource_status",
                        return_value={"running": False}), \
                    mock.patch.object(
                        indexd_runtime, "_store_census", return_value=[]), \
                    mock.patch.object(
                        indexd_runtime, "_inspect_indexd_owner",
                        return_value=missing):
                indexd_runtime._clear_freshen_failure()
                first = indexd_runtime.machine_freshness(checked=True)
                second = indexd_runtime.machine_freshness(checked=True)
                self.assertEqual(first["code"], "source-unreadable")
                self.assertEqual(second["code"], "source-unreadable")
                self.assertIn("/fixture/.claude/projects", first["reason"])
                self.assertEqual(health.read_bytes(), body)
                health.unlink()
                recovered = indexd_runtime.machine_freshness(checked=True)
            self.assertFalse(recovered["failing"])


if __name__ == "__main__":
    unittest.main()
