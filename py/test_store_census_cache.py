"""Bounded cross-process census reuse without sharing freshness verdicts."""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from _test_support import isolate_data_dir

isolate_data_dir()

import common
import indexd_runtime as runtime


DISCOVERY_ENV = (
    "HOME", "AGREP_HOME", "USERPROFILE", "XDG_DATA_HOME", "XDG_CONFIG_HOME",
    "LOCALAPPDATA", "APPDATA", "CLINE_DIR", "OPENCODE_DB", "CRUSH_GLOBAL_DATA",
)


class StoreCensusCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="agrep-census-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.binary = self.root / "agrep-rs"
        self.binary.write_bytes(b"fixture binary")
        self.now = time.time()
        self.member = self.root / "session.jsonl"
        self.member.write_text("fixture transcript", encoding="utf-8")
        os.utime(self.member, (self.now - 1000, self.now - 1000))
        self.rows = [{"name": "pi", "state": "available", "files": 1,
                      "newest_mtime_ms": int((self.now - 1000) * 1000)}]
        self.paths = [{"name": "pi", "path": str(self.member)}]
        self.digest = runtime._store_change_digest([str(self.member)])
        self.messages = self.root / "messages.jsonl"
        self.messages.write_text("{}\n", encoding="utf-8")
        (self.root / ".ingest.sig").write_text("fixture", encoding="utf-8")
        self.record = self.root / runtime.VERIFIED_CURRENT_FILE
        self.record.write_text(json.dumps({
            "version": 1, "ts": self.now - 900,
            "census": {"pi": [1, self.rows[0]["newest_mtime_ms"]]},
            "digests": {"pi": self.digest}, "signature": "fixture",
        }), encoding="utf-8")
        for patch in (
            mock.patch.object(common, "DATA_DIR", self.root),
            mock.patch.object(common, "MESSAGES_PATH", self.messages),
            mock.patch.object(common, "ingest_bin", side_effect=lambda: self.binary),
            mock.patch.object(runtime, "_DRIFT_PROBE", threading.local()),
            mock.patch.object(runtime, "_DRIFT_CACHE", threading.local()),
            mock.patch.object(runtime.time, "time", return_value=self.now),
            mock.patch.object(runtime, "_refresh_owner_possible", return_value=True),
            mock.patch.object(runtime, "derived_writer_mutation_info",
                              return_value=mock.Mock(writable=True)),
            mock.patch.dict(runtime._CENSUS_WARM_RETRY,
                            {"at": 0.0, "delay": runtime.CENSUS_REFRESH_AFTER_S}),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        runtime._clear_freshen_failure()
        self.addCleanup(runtime._clear_freshen_failure)
        self.addCleanup(runtime._reap_drift_probes)
        self.cache = self.root / ".store-census.json"

    def key(self) -> dict:
        publication = runtime._drift_cache_key()
        identity = runtime._optional_drift_file_identity(self.binary)
        environment = {name: os.environ.get(name) for name in DISCOVERY_ENV}
        relative = (any(value and not os.path.isabs(value) for value in environment.values())
                    or not any(environment.get(name)
                               for name in ("HOME", "USERPROFILE", "AGREP_HOME")))
        return {
            "publication": [publication[0], *[
                list(value) if value is not None else None
                for value in publication[1:]]],
            "ingest": [str(self.binary), list(identity) if identity else None],
            "environment": environment,
            "cwd": os.getcwd() if relative else None,
        }

    def cache_payload(self, **changes) -> dict:
        payload = {"version": 1, "observed_at": self.now, "key": self.key(),
                   "rows": self.rows, "digests": {"pi": self.digest}}
        payload.update(changes)
        observed_at = payload["observed_at"]
        if ("observed_mono" not in payload and type(observed_at) in (int, float)
                and math.isfinite(observed_at)):
            payload["observed_mono"] = time.monotonic() - (self.now - observed_at)
        return payload

    def save(self, **changes) -> None:
        self.cache.write_text(json.dumps(self.cache_payload(**changes)), encoding="utf-8")

    def child(self, payload=None, *, returncode=0):
        process = mock.Mock(returncode=returncode)
        if payload is None:
            payload = {"version": 1, "stores": self.rows, "paths": self.paths}
        # Byte-compatible with agrep-rs, which prints compact serde_json.
        process.communicate.return_value = (json.dumps(payload, separators=(",", ":")), "")
        process.poll.return_value = returncode
        return process

    def query(self):
        runtime._DRIFT_CACHE.value = None
        runtime._arm_drift_probe()
        return runtime._drift_report(now=self.now)

    def test_matching_cache_spawns_nothing_and_matches_live_verdict(self) -> None:
        self.save()
        with mock.patch.object(runtime.subprocess, "Popen", return_value=self.child()) as spawn:
            report = self.query()
        spawn.assert_not_called()
        self.cache.unlink()
        with mock.patch.object(runtime.subprocess, "Popen", return_value=self.child()):
            expected = self.query()
        self.assertEqual(report, expected)

    def test_combined_census_is_preferred_and_persists_member_digest(self) -> None:
        with mock.patch.object(runtime.subprocess, "Popen", return_value=self.child()) as spawn:
            report = self.query()
        self.assertEqual(report.state, "current")
        self.assertEqual([call.args[0] for call in spawn.call_args_list],
                         [[str(self.binary), "stores", "--census"]])
        saved = json.loads(self.cache.read_text(encoding="utf-8"))
        self.assertEqual(saved["rows"], self.rows)
        self.assertEqual(saved["digests"], {"pi": self.digest})

    def assert_live(self) -> None:
        with mock.patch.object(runtime.subprocess, "Popen", return_value=self.child()) as spawn:
            report = self.query()
        spawn.assert_called_once()
        self.assertEqual(report.state, "current")

    def test_each_publication_identity_invalidates_cache(self) -> None:
        for path in (self.messages, self.root / ".ingest.sig", self.record):
            with self.subTest(path=path.name):
                self.save()
                metadata = path.stat()
                os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000))
                self.assert_live()

    def test_binary_identity_and_path_invalidate_cache(self) -> None:
        self.save()
        self.binary.write_bytes(b"replacement binary")
        self.assert_live()
        other = self.root / "other-binary"
        os.link(self.binary, other)
        self.save()
        self.binary = other
        self.assert_live()

    def test_binary_symlink_target_identity_invalidates_cache(self) -> None:
        target = self.binary
        link = self.root / "binary-link"
        try:
            link.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        self.binary = link
        with mock.patch.object(runtime.subprocess, "Popen", return_value=self.child()):
            self.query()
        target.write_bytes(b"new executable at same target")
        self.assert_live()

    def test_every_discovery_environment_variable_invalidates_cache(self) -> None:
        for name in DISCOVERY_ENV:
            with self.subTest(name=name):
                self.save()
                with mock.patch.dict(os.environ, {name: str(self.root / name)}):
                    self.assert_live()

    def test_data_directory_invalidates_even_identical_publication_files(self) -> None:
        other = self.root / "other-data"
        other.mkdir()
        for path in (self.messages, self.root / ".ingest.sig", self.record):
            os.link(path, other / path.name)
        self.save()
        (other / self.cache.name).write_bytes(self.cache.read_bytes())
        with mock.patch.object(common, "DATA_DIR", other):
            self.assert_live()

    def test_invalid_observations_force_live_census(self) -> None:
        invalid = (
            {"observed_at": self.now - 5.001},
            {"observed_at": self.now + 0.001},
            {"observed_at": self.now + runtime.FRESHNESS_WRITE_RATE_S + 1},
            {"observed_at": float("nan")},
            {"observed_at": True},
            {"version": 2},
            {"version": True},
            {"key": {}},
            {"rows": ["foreign"]},
            {"digests": {"pi": "invalid"}},
            {"observed_mono": None},
            {"observed_mono": float("inf")},
            {"observed_mono": True},
        )
        for changed in invalid:
            with self.subTest(changed=changed):
                self.save(**changed)
                self.assert_live()

    def test_corrupt_and_oversized_observations_force_live_census(self) -> None:
        for content in (b"{", b"\xff", b" " * (1024 * 1024 + 1)):
            with self.subTest(size=len(content)):
                self.cache.write_bytes(content)
                self.assert_live()

    def test_unreadable_observation_forces_live_census(self) -> None:
        self.cache.mkdir()
        self.assert_live()
        self.assertTrue(self.cache.is_dir())

    def test_reuse_includes_exact_five_second_boundary(self) -> None:
        self.save(observed_at=self.now - 5.0)
        with mock.patch.object(runtime.subprocess, "Popen") as spawn:
            report = self.query()
        spawn.assert_not_called()
        self.assertEqual(report.state, "current")

    def test_cached_observation_uses_current_clock_for_grace_and_age(self) -> None:
        written = self.now - runtime.DRIFT_GRACE_S - 1
        rows = [{**self.rows[0], "files": 2, "newest_mtime_ms": int(written * 1000)}]
        self.save(rows=rows, observed_at=self.now - 2)
        with mock.patch.object(runtime.subprocess, "Popen") as spawn:
            report = self.query()
        spawn.assert_not_called()
        self.assertEqual(report, runtime.DriftReport("drifted", 1, 900.0))

    def test_cached_member_digest_detects_same_census_rewrite(self) -> None:
        self.save(digests={"pi": "f" * 64})
        with mock.patch.object(runtime.subprocess, "Popen") as spawn:
            report = self.query()
        spawn.assert_not_called()
        self.assertEqual(report, runtime.DriftReport("drifted", 1, 900.0))

    def test_missing_cached_digest_claims_no_member_identity(self) -> None:
        self.save(digests={})
        with mock.patch.object(runtime.subprocess, "Popen") as spawn:
            report = self.query()
        spawn.assert_not_called()
        self.assertEqual(report, runtime.DriftReport("current"))

    def test_no_auto_neither_reads_nor_writes_cache(self) -> None:
        self.save()
        before = self.cache.read_bytes()
        with mock.patch.object(runtime.subprocess, "Popen") as spawn, \
                mock.patch.object(runtime.ownerfile, "snapshot",
                                  side_effect=AssertionError("no-auto read cache")):
            self.assertTrue(runtime.ensure_index(auto=False))
            report = runtime._drift_report()
        spawn.assert_not_called()
        self.assertEqual(report.code, "freshness-unchecked")
        self.assertEqual(self.cache.read_bytes(), before)

    def test_read_only_directory_can_reuse_but_never_writes_cache(self) -> None:
        self.save()
        with mock.patch.object(runtime, "_data_dir_readonly", return_value=True), \
                mock.patch.object(runtime.subprocess, "Popen", return_value=self.child()) as spawn:
            self.assertEqual(self.query().state, "current")
            spawn.assert_not_called()
            self.cache.unlink()
            self.assertEqual(self.query().state, "current")
        self.assertFalse(self.cache.exists())

    def test_unsupported_or_malformed_combined_output_falls_back(self) -> None:
        for preferred in (self.child({}, returncode=2), self.child([]),
                          self.child({"version": 1, "stores": self.rows, "paths": [None]})):
            with self.subTest(preferred=preferred):
                self.cache.unlink(missing_ok=True)
                children = [preferred, self.child(self.rows), self.child(self.paths)]
                with mock.patch.object(runtime.subprocess, "Popen", side_effect=children) as spawn:
                    report = self.query()
                self.assertEqual(report.state, "current")
                self.assertEqual([call.args[0][1:] for call in spawn.call_args_list],
                                 [["stores", "--census"], ["stores"], ["stores", "--paths"]])
                self.assertEqual(json.loads(self.cache.read_text())["digests"], {"pi": self.digest})

    def test_large_store_has_no_member_digest(self) -> None:
        self.rows[0]["files"] = runtime._STORE_DIGEST_MAX_FILES + 1
        with mock.patch.object(runtime.subprocess, "Popen", return_value=self.child()):
            runtime._store_census()
        self.assertEqual(json.loads(self.cache.read_text())["digests"], {})

    def test_oversized_census_keeps_rows_without_rewalking(self) -> None:
        many = [{"name": "pi", "path": str(self.root / f"m{i:05}.jsonl")} for i in range(400)]
        child = self.child({"version": 1, "stores": self.rows, "paths": many})
        with mock.patch.object(runtime, "_PATHS_PROBE_MAX_BYTES", 4096), \
                mock.patch.object(runtime.subprocess, "Popen", return_value=child) as spawn:
            report = self.query()
        self.assertEqual(report.state, "current")
        self.assertEqual([call.args[0][1:] for call in spawn.call_args_list],
                         [["stores", "--census"]])
        saved = json.loads(self.cache.read_text())
        self.assertEqual((saved["rows"], saved["digests"]), (self.rows, {}))

    def test_clock_step_since_observation_forces_live_census(self) -> None:
        for wall_age, mono_age in ((1.0, 4.0), (4.0, 1.0)):
            with self.subTest(wall_age=wall_age, mono_age=mono_age):
                self.save(observed_at=self.now - wall_age,
                          observed_mono=time.monotonic() - mono_age)
                self.assert_live()

    def test_relative_discovery_root_keys_on_working_directory(self) -> None:
        first, second = self.root / "first", self.root / "second"
        first.mkdir()
        second.mkdir()
        self.addCleanup(os.chdir, os.getcwd())
        with mock.patch.dict(os.environ, {"CLINE_DIR": "stores/cline"}):
            os.chdir(first)
            self.save()
            os.chdir(second)
            self.assert_live()

    def test_in_process_verdict_expires_with_reused_observation(self) -> None:
        self.save(observed_at=self.now - runtime.CENSUS_REUSE_S,
                  observed_mono=time.monotonic() - runtime.CENSUS_REUSE_S)
        runtime._DRIFT_CACHE.value = None
        with mock.patch.object(runtime.subprocess, "Popen", return_value=self.child()) as spawn:
            self.assertEqual(runtime._drift_report(now=self.now).state, "current")
            spawn.assert_not_called()
            self.cache.unlink()
            self.assertEqual(runtime._drift_report(now=self.now).state, "current")
        spawn.assert_called_once()

    def test_cache_publish_never_retries_on_query_path(self) -> None:
        attempts = []
        real_replace, real_publish = os.replace, runtime.common.replace_with_retry

        def replace(src, dst):
            if Path(dst) == self.cache:
                attempts.append(dst)
                raise PermissionError("held open by a reader")
            return real_replace(src, dst)

        def windows_publish(src, dst, **kwargs):
            with mock.patch.object(runtime.fileops, "WIN", True):
                return real_publish(src, dst, **kwargs)

        with mock.patch.object(runtime.common, "replace_with_retry", side_effect=windows_publish), \
                mock.patch.object(runtime.fileops.time, "sleep"), \
                mock.patch.object(runtime.fileops.os, "replace", side_effect=replace), \
                mock.patch.object(runtime.subprocess, "Popen", return_value=self.child()):
            self.assertEqual(self.query().state, "current")
        self.assertEqual(len(attempts), 1)
        self.assertFalse(self.cache.exists())

    def test_publication_moving_during_census_cannot_be_cached(self) -> None:
        process = self.child()
        payload = process.communicate.return_value

        def finish(**_kwargs):
            self.messages.write_text("changed publication", encoding="utf-8")
            return payload

        process.communicate.side_effect = finish
        with mock.patch.object(runtime.subprocess, "Popen", return_value=process):
            self.assertEqual(runtime._store_census(), self.rows)
        self.assertFalse(self.cache.exists())

    def test_doctor_observation_bypasses_reusable_cache(self) -> None:
        self.save(rows=[])
        with mock.patch.object(runtime.subprocess, "Popen", return_value=self.child()) as spawn:
            runtime.arm_store_census()
            rows, report = runtime.observe_store_drift()
        spawn.assert_called_once()
        self.assertEqual(rows, self.rows)
        self.assertEqual(report.state, "current")

    def test_diagnostic_observation_never_persists(self) -> None:
        with mock.patch.object(runtime.subprocess, "Popen", return_value=self.child()):
            runtime.arm_store_census()
            rows, report = runtime.observe_store_drift()
        self.assertEqual((rows, report.state), (self.rows, "current"))
        self.assertFalse(self.cache.exists())

    def test_verified_stamp_bypasses_cache_and_uses_one_walk(self) -> None:
        self.save(rows=[])
        with mock.patch.object(runtime.subprocess, "Popen", return_value=self.child()) as spawn, \
                mock.patch.object(runtime, "indexd_failing", return_value=(0, "")):
            self.assertTrue(runtime.stamp_verified_current())
        spawn.assert_called_once()
        self.assertEqual(json.loads(self.record.read_text())["census"],
                         {"pi": [1, self.rows[0]["newest_mtime_ms"]]})

    def test_absence_member_listing_stays_live(self) -> None:
        self.save()
        with mock.patch.object(runtime.subprocess, "Popen", return_value=self.child(self.paths)) as spawn:
            members = runtime._store_paths_census(timeout_s=0.5)
        self.assertEqual(spawn.call_args.args[0], [str(self.binary), "stores", "--paths"])
        self.assertEqual(members, {"pi": [str(self.member)]})

    def test_cache_expiring_during_query_is_observed_again(self) -> None:
        self.save(observed_at=self.now - 4)
        with mock.patch.object(runtime.subprocess, "Popen", return_value=self.child()) as spawn:
            runtime._arm_drift_probe()
            spawn.assert_not_called()
            report = runtime._compute_drift_report(now=self.now + 2)
        spawn.assert_called_once()
        self.assertEqual(report.state, "current")

    @unittest.skipUnless(hasattr(os, "fork"), "fork is POSIX-only")
    def test_forked_reader_reuses_census_but_not_parent_verdict(self) -> None:
        self.save()
        runtime._DRIFT_CACHE.value = (
            runtime._drift_cache_key(), time.monotonic(), runtime.DriftReport("unknown"))
        read_fd, write_fd = os.pipe()
        try:
            with mock.patch.object(runtime.subprocess, "Popen",
                                   side_effect=AssertionError("cached child spawned")):
                child = os.fork()
                if child == 0:
                    os.close(read_fd)
                    try:
                        result = runtime._drift_report()._asdict()
                        os.write(write_fd, json.dumps(result).encode("utf-8"))
                    finally:
                        os._exit(0)
                os.close(write_fd)
                write_fd = -1
                raw = os.read(read_fd, 4096)
                _, status = os.waitpid(child, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            self.assertEqual(json.loads(raw)["state"], "current")
            self.assertEqual(runtime._drift_report().state, "unknown")
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)

    def beat(self, age: float) -> Path:
        path = self.root / "search-beat"
        path.touch()
        os.utime(path, (self.now - age, self.now - age))
        return path

    def test_daemon_warms_stale_cache_for_recent_reader(self) -> None:
        for age in (3.0, 6.0):
            with self.subTest(age=age):
                self.save(observed_at=self.now - age)
                with mock.patch.object(runtime, "SEARCH_BEAT_PATH", self.beat(2)), \
                        mock.patch.object(runtime.subprocess, "Popen", return_value=self.child()) as spawn:
                    runtime.warm_store_census()
                spawn.assert_called_once()
                self.assertEqual(json.loads(self.cache.read_text())["observed_at"], self.now)

    def test_daemon_leaves_fresh_cache_and_inactive_readers_alone(self) -> None:
        for beat_age, cache_age in ((2, 2.9), (121, 6), (-1, 6)):
            with self.subTest(beat_age=beat_age, cache_age=cache_age):
                self.save(observed_at=self.now - cache_age)
                before = self.cache.read_bytes()
                with mock.patch.object(runtime, "SEARCH_BEAT_PATH", self.beat(beat_age)), \
                        mock.patch.object(runtime.subprocess, "Popen") as spawn:
                    runtime.warm_store_census()
                spawn.assert_not_called()
                self.assertEqual(self.cache.read_bytes(), before)

    def test_daemon_warming_respects_both_write_fences(self) -> None:
        for readonly, writable in ((True, True), (False, False)):
            with self.subTest(readonly=readonly, writable=writable), \
                    mock.patch.object(runtime, "SEARCH_BEAT_PATH", self.beat(2)), \
                    mock.patch.object(runtime, "_data_dir_readonly", return_value=readonly), \
                    mock.patch.object(runtime, "derived_writer_mutation_info",
                                      return_value=mock.Mock(writable=writable)), \
                    mock.patch.object(runtime.subprocess, "Popen") as spawn:
                runtime.warm_store_census()
                spawn.assert_not_called()
                self.assertFalse(self.cache.exists())

    def run_daemon_tick(self) -> None:
        import indexer
        import ownerfile

        with mock.patch.object(runtime, "indexd_failure_state", return_value=(0, "", 0.0)), \
                mock.patch.object(runtime, "auto_index_escalated", return_value=False):
            daemon = indexer.AutoIndexer(
                mock.Mock(), owns_lifetime=lambda: True,
                owner_snapshot=ownerfile.Snapshot((1, 2, 0, 0), 0.0, b""))
        daemon._stop_requested = mock.Mock()
        daemon._stop_requested.is_set.return_value = False
        daemon._stop_requested.wait.side_effect = (False, True)
        with mock.patch.object(runtime, "derived_writes_permitted", return_value=True), \
                mock.patch.object(indexer, "INGEST", self.root / "missing"), \
                mock.patch.object(daemon, "_serve_recovery_requests"), \
                mock.patch.object(daemon, "_run_housekeeping"), \
                mock.patch.object(daemon, "_check_derived_health"), \
                mock.patch.object(daemon, "_should_run", return_value=False), \
                mock.patch.object(daemon, "_maybe_verify_current"), \
                mock.patch.object(daemon, "_serve_index_request"):
            daemon.run()

    def test_daemon_loop_refreshes_missing_cache(self) -> None:
        with mock.patch.object(runtime, "SEARCH_BEAT_PATH", self.beat(2)), \
                mock.patch.object(runtime.subprocess, "Popen", return_value=self.child()):
            self.run_daemon_tick()
        self.assertTrue(self.cache.exists())
        self.assertEqual(json.loads(self.cache.read_text())["rows"], self.rows)

    def test_census_failure_does_not_escape_daemon_loop(self) -> None:
        with mock.patch.object(runtime, "SEARCH_BEAT_PATH", self.beat(2)), \
                mock.patch.object(runtime, "_store_census", side_effect=RuntimeError("census failed")), \
                mock.patch.object(common, "dbg") as logged:
            self.run_daemon_tick()
        self.assertFalse(self.cache.exists())
        self.assertTrue(any("census failed" in str(call.args) for call in logged.call_args_list))

    def test_daemon_warming_backs_off_after_failure(self) -> None:
        with mock.patch.object(runtime, "SEARCH_BEAT_PATH", self.beat(2)), \
                mock.patch.object(runtime, "_store_census", return_value=None) as census:
            runtime.warm_store_census()
            runtime.warm_store_census()
            self.assertEqual(census.call_count, 1)
            first_delay = runtime._CENSUS_WARM_RETRY["delay"]
            runtime._CENSUS_WARM_RETRY["at"] = 0.0
            runtime.warm_store_census()
        self.assertEqual(census.call_count, 2)
        self.assertGreater(runtime._CENSUS_WARM_RETRY["delay"], first_delay)
        with mock.patch.object(runtime, "SEARCH_BEAT_PATH", self.beat(2)), \
                mock.patch.object(runtime.subprocess, "Popen", return_value=self.child()):
            runtime._CENSUS_WARM_RETRY["at"] = 0.0
            runtime.warm_store_census()
        self.assertEqual(runtime._CENSUS_WARM_RETRY,
                         {"at": 0.0, "delay": runtime.CENSUS_REFRESH_AFTER_S})


if __name__ == "__main__":
    unittest.main()
