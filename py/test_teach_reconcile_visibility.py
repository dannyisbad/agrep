"""Visible, non-destructive outcomes for ambient instruction reconciliation."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from _test_support import isolate_data_dir

isolate_data_dir()

import common  # noqa: E402
import doctor  # noqa: E402
import indexer  # noqa: E402
import ownerfile  # noqa: E402
import teach  # noqa: E402


class TeachReconcileVisibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.data = self.root / "data"
        self.home.mkdir()
        self.data.mkdir()
        self.saved = {name: getattr(teach, name) for name in (
            "HOME", "STATE_PATH", "MD_TARGETS", "SKILL_TARGETS",
            "_LAST_RECONCILE_HEALTH", "_PENDING_RECONCILE_HEALTH")}
        self.saved_data = common.DATA_DIR
        teach.HOME = self.home
        common.DATA_DIR = self.data
        teach.STATE_PATH = self.data / "teach.json"
        teach.MD_TARGETS = []
        teach.SKILL_TARGETS = []
        teach._LAST_RECONCILE_HEALTH = None
        teach._PENDING_RECONCILE_HEALTH = None

    def tearDown(self) -> None:
        for name, value in self.saved.items():
            setattr(teach, name, value)
        common.DATA_DIR = self.saved_data
        self.temp.cleanup()

    def _indexer(self) -> indexer.AutoIndexer:
        raw = b"owner"
        snapshot = ownerfile.Snapshot((1, 2, len(raw), 3), 0.0, raw)
        return indexer.AutoIndexer(
            mock.Mock(), owns_lifetime=lambda: True,
            owner_snapshot=snapshot)

    def test_malformed_refusal_does_not_block_healthy_repair(self) -> None:
        codex = self.home / ".codex"
        claude = self.home / ".claude"
        codex.mkdir()
        claude.mkdir()
        malformed = codex / "AGENTS.md"
        healthy = claude / "CLAUDE.md"
        damaged = f"{teach.MARK_PREFIX} v1 -->\nmissing end\n".encode()
        malformed.write_bytes(damaged)
        healthy.write_bytes(b"user rules\n")
        teach.MD_TARGETS = [
            ("codex", codex, malformed),
            ("claude", claude, healthy),
        ]
        teach._save_state([malformed, healthy])

        self.assertEqual(teach.reconcile(), [])
        self.assertEqual(malformed.read_bytes(), damaged)
        self.assertEqual(healthy.read_bytes(), b"user rules\n")
        health = teach.reconcile_health()
        self.assertEqual(health["state"], "refused")
        self.assertEqual(health["repaired"], [])
        self.assertEqual(health["refusals"][0]["kind"], "malformed-markers")
        self.assertEqual(health["refusals"][1]["kind"], "drifted")
        self.assertIn("begin marker has no end marker", health["refusals"][0]["reason"])
        unsafe = teach._reconcile_issue(
            Path("bad-\x1b[31m.md"), "fixture", "reason\x1b")
        self.assertNotIn("\x1b", unsafe["path"] + unsafe["reason"])
        self.assertIn("\\u001b", unsafe["path"])

        owner = self._indexer()
        with mock.patch.object(indexer.common, "log") as log:
            owner._reassert_teach()
            owner._reassert_teach()
        refusal_logs = [call for call in log.call_args_list
                        if "reconcile refused" in str(call)]
        self.assertEqual(len(refusal_logs), 2)
        self.assertEqual(owner.status()["teach_reconcile"]["state"], "refused")
        self.assertEqual(
            doctor._teach_reconcile_probe()["refusals"], health["refusals"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            doctor._report_teach_reconcile(health)
        rendered = output.getvalue()
        self.assertIn("instructions sync", rendered)
        self.assertIn("refused", rendered)
        self.assertNotIn("\x1b", rendered)

        malformed.unlink()
        healthy.unlink()
        self.assertEqual(teach.reconcile(), [str(malformed), str(healthy)])
        recovered = teach.reconcile_health()
        self.assertEqual(recovered["state"], "repaired")
        self.assertEqual(recovered["refusals"], [])

    def test_newer_block_is_preserved_and_classified_without_a_warning(self) -> None:
        proof = self.home / ".codex"
        proof.mkdir()
        target = proof / "AGENTS.md"
        original = (
            f"{teach.MARK_PREFIX} v{teach.NUDGE_V + 1} -->\n"
            f"future body\n{teach.MARK_END}\n"
        ).encode()
        target.write_bytes(original)
        teach.MD_TARGETS = [("codex", proof, target)]
        teach._save_state([target])

        self.assertEqual(teach.reconcile(), [])
        self.assertEqual(target.read_bytes(), original)
        health = teach.reconcile_health()
        self.assertEqual(health["state"], "preserved-newer")
        self.assertEqual(health["refusals"], [])
        self.assertEqual(
            health["preserved_newer"][0]["kind"], "preserved-newer")
        self.assertEqual(
            doctor._teach_reconcile_probe()["state"], "preserved-newer")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            doctor._report_teach_reconcile(health)
        self.assertIn("preserved-newer", output.getvalue())
        self.assertNotIn("warn", output.getvalue().lower())

    def test_clean_target_has_no_reconciliation_warning(self) -> None:
        proof = self.home / ".codex"
        proof.mkdir()
        target = proof / "AGENTS.md"
        self.assertEqual(teach._write_block(target), "added")
        teach.MD_TARGETS = [("codex", proof, target)]
        teach._save_state([target])
        before = (target.read_bytes(), target.stat().st_ino,
                  target.stat().st_mtime_ns)

        self.assertEqual(teach.reconcile(), [])
        self.assertEqual(
            (target.read_bytes(), target.stat().st_ino,
             target.stat().st_mtime_ns), before)
        health = teach.reconcile_health()
        self.assertEqual(health["state"], "clean")
        self.assertEqual(health["refusals"], [])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            doctor._report_teach_reconcile(health)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(doctor._teach_reconcile_probe()["state"], "clean")

        with mock.patch.object(doctor, "_semantic_probe", return_value={}), \
                mock.patch.object(doctor, "_orphan_inventory",
                                  return_value={"count": 0, "bytes": 0}), \
                mock.patch.object(doctor, "_index_summary_state",
                                  return_value={"state": "never-built"}), \
                mock.patch.object(doctor, "_store_counts", return_value=[]), \
                mock.patch.object(doctor, "_corpus_db_readiness",
                                  return_value={"state": "missing"}), \
                mock.patch.object(doctor, "_archive_probe",
                                  return_value={"state": "disabled"}), \
                mock.patch.object(doctor, "_model_attribution",
                                  return_value={"state": "unavailable"}), \
                mock.patch.object(doctor.common, "data_dir_usage",
                                  return_value={"files": 0, "bytes": 0}), \
                mock.patch.object(doctor.indexd_runtime, "indexd_resource_status",
                                  return_value={"running": False}), \
                mock.patch.object(doctor.indexd_runtime, "machine_freshness",
                                  return_value={"state": "no-known-failure"}), \
                mock.patch("corpusdb.machine_freshness_fields",
                           return_value={"freshness": {"state": "no-known-failure"}}), \
                mock.patch.object(doctor.common, "detected_stores", return_value=[]):
            payload = doctor.probe()
        self.assertEqual(payload["teach_reconcile"]["state"], "clean")

    def test_unreadable_health_is_bounded_and_reported(self) -> None:
        teach.STATE_PATH.write_text('{"targets":[]}', encoding="utf-8")
        path = self.data / teach.RECONCILE_HEALTH
        path.write_bytes(b"{not-json\x1b")

        health = teach.reconcile_health()
        self.assertEqual(health["state"], "unreadable")
        self.assertEqual(health["refusals"][0]["kind"], "health-unreadable")
        self.assertNotIn("\x1b", health["refusals"][0]["reason"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            doctor._report_teach_reconcile(health)
        self.assertIn("instructions sync", output.getvalue())

    @unittest.skipIf(os.name == "nt", "symlink creation is privilege-dependent")
    def test_dangling_enrollment_link_is_unreadable_not_unenrolled(self) -> None:
        teach.STATE_PATH.symlink_to(self.data / "missing-state")
        health = doctor._teach_reconcile_probe()
        self.assertEqual(health["state"], "unreadable")
        self.assertEqual(health["refusals"][0]["path"], str(teach.STATE_PATH))
        self.assertIn("not a regular file", health["refusals"][0]["reason"])

    def test_health_reader_refuses_sparse_and_special_sidecars(self) -> None:
        path = self.data / teach.RECONCILE_HEALTH
        with path.open("wb") as stream:
            stream.seek(2 * 1024 * 1024)
            stream.write(b"x")
        before = path.stat()
        self.assertEqual(teach.reconcile_health()["state"], "unreadable")
        after = path.stat()
        self.assertEqual((after.st_size, after.st_ino),
                         (before.st_size, before.st_ino))
        path.unlink()
        if hasattr(os, "mkfifo"):
            os.mkfifo(path)
            started = time.perf_counter()
            self.assertEqual(teach.reconcile_health()["state"], "unreadable")
            self.assertLess(time.perf_counter() - started, 0.5)

    def test_durable_health_rejects_impossible_or_terminal_active_records(self) -> None:
        target = self.home / ".codex" / "AGENTS.md"
        teach.MD_TARGETS = [("codex", target.parent, target)]
        issue = {"path": str(target), "kind": "fixture", "reason": "blocked"}
        valid = {
            "version": 1, "state": "clean", "repaired": [],
            "refusals": [], "preserved_newer": [],
        }
        records = [
            {**valid, "version": True},
            {**valid, "version": 1.0},
            {**valid, "state": "clean", "refusals": [issue]},
            {**valid, "state": "repaired"},
            {**valid, "state": "refused"},
            {**valid, "state": "preserved-newer"},
            {**valid, "state": "repaired",
             "repaired": [str(target), str(target)]},
            {**valid, "state": "repaired",
             "repaired": [str(target) + "\x1b[31m"]},
            {**valid, "state": "refused", "refusals": [
                {**issue, "reason": "ring\x07"},
            ]},
        ]
        path = self.data / teach.RECONCILE_HEALTH
        for record in records:
            with self.subTest(record=record):
                path.write_text(json.dumps(record), encoding="utf-8")
                self.assertEqual(teach.reconcile_health()["state"], "unreadable")
        path.write_text(
            '{"version":1,"state":"repaired","state":"clean",'
            '"repaired":[],"refusals":[],"preserved_newer":[]}',
            encoding="utf-8")
        self.assertEqual(teach.reconcile_health()["state"], "unreadable")
        path.write_text(
            '{"version":1,"state":"refused","repaired":[],"refusals":'
            '[{"path":"x","kind":"target-unreadable",'
            '"kind":"malformed-markers","reason":"r"}],'
            '"preserved_newer":[]}', encoding="utf-8")
        self.assertEqual(teach.reconcile_health()["state"], "unreadable")
        path.write_text("[" * 2000 + "]" * 2000, encoding="utf-8")
        self.assertEqual(teach.reconcile_health()["state"], "unreadable")

    def test_symlink_and_oversized_targets_are_refused_untouched(self) -> None:
        proof = self.home / ".codex"
        proof.mkdir()
        backing = self.root / "real-agents.md"
        backing.write_bytes(b"user-owned\n")
        link = proof / "AGENTS.md"
        try:
            link.symlink_to(backing)
        except (NotImplementedError, OSError):
            self.skipTest("symlinks unavailable")
        teach.MD_TARGETS = [("codex", proof, link)]
        teach._save_state([link])
        self.assertEqual(teach.reconcile(), [])
        self.assertTrue(link.is_symlink())
        self.assertEqual(backing.read_bytes(), b"user-owned\n")
        self.assertEqual(
            teach.current_reconcile_health()["refusals"][0]["kind"],
            "target-unreadable")

        link.unlink()
        with link.open("wb") as stream:
            stream.seek(2 * 1024 * 1024)
            stream.write(b"x")
        before = link.stat()
        self.assertEqual(teach.reconcile(), [])
        after = link.stat()
        self.assertEqual((after.st_size, after.st_ino),
                         (before.st_size, before.st_ino))
        self.assertEqual(
            teach.current_reconcile_health()["refusals"][0]["kind"],
            "target-unreadable")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO is POSIX-only")
    def test_fifo_target_never_blocks_reconciliation(self) -> None:
        home = self.root / "fifo-home"
        data = self.root / "fifo-data"
        target = home / ".codex" / "AGENTS.md"
        target.parent.mkdir(parents=True)
        data.mkdir()
        os.mkfifo(target)
        (data / "teach.json").write_text(json.dumps({
            "version": 2, "targets": [str(target)], "skills": [],
        }), encoding="utf-8")
        env = {
            **os.environ, "HOME": str(home), "USERPROFILE": str(home),
            "AGREP_DATA_DIR": str(data),
        }
        started = time.perf_counter()
        try:
            run = subprocess.run(
                [sys.executable, "-c",
                 "import json, teach; teach.reconcile(); "
                 "print(json.dumps(teach.current_reconcile_health()))"],
                cwd=Path(__file__).parent, env=env, capture_output=True,
                text=True, timeout=1.0)
        except subprocess.TimeoutExpired as exc:
            self.fail(f"reconcile blocked on FIFO: {exc}")
        self.assertLess(time.perf_counter() - started, 1.0)
        self.assertEqual(run.returncode, 0, run.stderr)
        health = json.loads(run.stdout)
        self.assertEqual(health["state"], "refused")
        self.assertEqual(health["refusals"][0]["kind"], "target-unreadable")

    def test_persistent_reconcile_exception_logs_only_on_state_change(self) -> None:
        owner = self._indexer()
        with mock.patch.object(
                teach, "reconcile", side_effect=RuntimeError("persistent")), \
                mock.patch.object(indexer.common, "log") as log:
            owner._reassert_teach()
            owner._reassert_teach()
        unavailable = [call for call in log.call_args_list
                       if "reconcile unavailable" in str(call)]
        self.assertEqual(len(unavailable), 1)
        self.assertEqual(
            owner.status()["teach_reconcile"]["state"], "unavailable")

    def test_health_persistence_failure_never_blocks_repair(self) -> None:
        proof = self.home / ".codex"
        proof.mkdir()
        target = proof / "AGENTS.md"
        teach.MD_TARGETS = [("codex", proof, target)]
        teach._save_state([target])

        write_health = teach._write_reconcile_health
        calls = 0

        def fail_twice(value):
            nonlocal calls
            calls += 1
            if calls <= 2:
                raise OSError("ledger unavailable")
            return write_health(value)

        with mock.patch.object(teach, "_write_reconcile_health",
                               side_effect=fail_twice):
            self.assertEqual(teach.reconcile(), [str(target)])
        health = teach.current_reconcile_health()
        self.assertEqual(health["state"], "health-unavailable")
        self.assertEqual(health["repaired"], [str(target)])
        self.assertEqual(health["refusals"][0]["kind"], "health-unavailable")
        self.assertEqual(teach.reconcile_health()["state"], "not-checked")
        self.assertEqual(teach.reconcile(), [])
        self.assertEqual(teach.current_reconcile_health(), health)
        self.assertEqual(teach.reconcile_health(), health)
        self.assertEqual(teach.reconcile(), [])
        self.assertEqual(teach.current_reconcile_health()["state"], "clean")
        self.assertEqual(teach.reconcile_health()["state"], "clean")
        self.assertIn(teach.MARK_BEGIN, target.read_text(encoding="utf-8"))
        self.assertEqual(list(self.data.glob(".teach-reconcile.json.*.tmp")), [])

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            doctor._report_teach_reconcile(health)
        self.assertIn("health unavailable", output.getvalue())

        owner = self._indexer()
        with mock.patch.object(teach, "reconcile", return_value=[]), \
                mock.patch.object(
                    teach, "current_reconcile_health", return_value=health), \
                mock.patch.object(indexer.common, "log") as log:
            owner._reassert_teach()
            owner._reassert_teach()
        unavailable = [call for call in log.call_args_list
                       if "health unavailable" in str(call)]
        self.assertEqual(len(unavailable), 1)


if __name__ == "__main__":
    unittest.main()
