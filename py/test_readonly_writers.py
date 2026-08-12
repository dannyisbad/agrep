"""Exact no-mutation proofs for Python writers under AGREP_DATA_READONLY."""

from __future__ import annotations

from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest
from unittest import mock

from _test_support import isolate_data_dir


isolate_data_dir()

import archive  # noqa: E402
import cli  # noqa: E402
import dist  # noqa: E402
import index_lock  # noqa: E402
import indexd  # noqa: E402
import indexd_runtime  # noqa: E402
import indexer  # noqa: E402
import legacy_cleanup  # noqa: E402
import lifetime  # noqa: E402
import removal_fence  # noqa: E402
import resources  # noqa: E402
import settings  # noqa: E402
import teach  # noqa: E402


def _census(root: Path) -> tuple:
    """Names, bytes, modes, and change metadata for one bounded fixture tree."""
    rows = []
    for path in (root, *sorted(root.rglob("*"))):
        info = path.lstat()
        relative = "." if path == root else path.relative_to(root).as_posix()
        if stat.S_ISREG(info.st_mode):
            payload = path.read_bytes()
        elif stat.S_ISLNK(info.st_mode):
            payload = os.readlink(path)
        else:
            payload = None
        rows.append((
            relative,
            stat.S_IFMT(info.st_mode),
            stat.S_IMODE(info.st_mode),
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
            payload,
        ))
    return tuple(rows)


def _capture(call) -> tuple:
    """Make return-vs-refusal part of one explicit composite assertion."""
    try:
        return "return", call()
    except Exception as exc:  # noqa: BLE001 -- the exception class is the proof value
        return "raise", type(exc)


class ReadonlyWriterBoundaryTests(unittest.TestCase):
    def test_cmd_index_refuses_before_binary_build_or_fetch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-ro-cli-") as raw:
            root = Path(raw)
            marker = root / "live"
            marker.write_bytes(b"preserve")
            before = _census(root)
            with mock.patch.object(cli.common, "DATA_DIR", root), \
                    mock.patch.dict(
                        os.environ, {"AGREP_DATA_READONLY": str(root)}), \
                    mock.patch.object(cli, "_ensure_binary") as ensure, \
                    mock.patch.object(cli, "_index") as run_index:
                results = []
                for full in (False, True):
                    with self.subTest(full=full):
                        args = type(
                            "Args", (), {"rest": [], "full": full})()
                        results.append(cli.cmd_index(args))
            self.assertEqual(
                (results, ensure.mock_calls, run_index.mock_calls, _census(root)),
                ([1, 1], [], [], before))

    def test_binary_fetch_refuses_before_network_or_parent_creation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-ro-dist-") as raw:
            root = Path(raw)
            (root / "live").write_bytes(b"preserve")
            destination = root / "bin" / "v-test" / "agrep-rs"
            before = _census(root)
            with mock.patch.object(dist, "DATA_DIR", root), \
                    mock.patch.object(dist, "FETCHED_BIN_DIR", root / "bin"), \
                    mock.patch.object(dist, "_platform_asset",
                                      return_value="agrep-rs-test"), \
                    mock.patch.object(dist, "_fetch_base_url",
                                      return_value="https://invalid.example/"), \
                    mock.patch.object(dist, "package_version",
                                      return_value="test"), \
                    mock.patch.dict(
                        os.environ, {"AGREP_DATA_READONLY": str(root)}), \
                    mock.patch(
                        "urllib.request.urlopen",
                        side_effect=AssertionError("network must not run")) as request:
                outcomes = (
                    dist.fetch_binary(assume_yes=True),
                    dist._download_binary(
                        "https://invalid.example/agrep-rs-test", destination),
                )
            self.assertEqual(
                (outcomes, request.mock_calls, destination.parent.exists(),
                 _census(root)),
                ((None, None), [], False, before))

    def test_stale_legacy_server_is_neither_reaped_nor_terminated(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-ro-server-") as raw:
            root = Path(raw)
            descriptor = root / ".server"
            descriptor.write_text(json.dumps({
                "pid": 2_000_000_000,
                "port": 65432,
                "process_start": "old",
                "mode": "explorer",
            }), encoding="utf-8")
            before = _census(root)
            with mock.patch.object(legacy_cleanup, "DATA_DIR", root), \
                    mock.patch.object(
                        legacy_cleanup, "_REMOVED_EXPLORER_CHECKED", False), \
                    mock.patch.dict(
                        os.environ, {"AGREP_DATA_READONLY": str(root)}), \
                    mock.patch.object(legacy_cleanup, "pid_alive") as alive, \
                    mock.patch.object(
                        legacy_cleanup, "terminate_exact_process") as terminate, \
                    mock.patch.object(
                        legacy_cleanup, "_unlink_if_unchanged") as unlink:
                legacy_cleanup.retire_removed_explorer()
            self.assertEqual(
                (alive.mock_calls, terminate.mock_calls, unlink.mock_calls,
                 _census(root)),
                ([], [], [], before))

    def test_stale_removal_fences_are_classified_without_cleanup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-ro-removal-") as raw:
            root = Path(raw)
            stale = root / ".background-removal"
            stale.write_bytes(b"malformed stale owner")
            old = time.time() - 120
            os.utime(stale, (old, old))
            before = _census(root)
            owner = mock.Mock()
            with mock.patch.object(removal_fence, "DATA_DIR", root), \
                    mock.patch.dict(
                        os.environ, {"AGREP_DATA_READONLY": str(root)}), \
                    mock.patch.object(
                        removal_fence.ownerfile, "remove_exact") as remove:
                outcomes = (
                    removal_fence.background_removal_active(),
                    removal_fence.acquire_background_removal_fence(),
                    removal_fence.clear_background_removal_fence(),
                    removal_fence.finish_background_removal_fence(owner),
                )
            self.assertEqual(
                (outcomes, remove.mock_calls, owner.close.mock_calls,
                 owner.touch.mock_calls, owner.release.mock_calls,
                 _census(root)),
                ((False, None, False, False), [], [mock.call()], [], [],
                 before))

    def test_settings_and_index_lock_refuse_before_stale_lock_cleanup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-ro-lock-") as raw:
            root = Path(raw)
            settings_path = root / "settings.json"
            settings_path.write_bytes(b'{"embeddings":"off"}')
            lock_path = root / ".index.lock"
            lock_path.write_bytes(b"malformed stale lock\n")
            old = time.time() - 120
            os.utime(lock_path, (old, old))
            before = _census(root)
            mutate = mock.Mock(return_value="on")
            with mock.patch.object(settings, "SETTINGS_PATH", settings_path), \
                    mock.patch.object(index_lock, "INDEX_LOCK_PATH", lock_path), \
                    mock.patch.dict(
                        os.environ, {"AGREP_DATA_READONLY": str(root)}), \
                    mock.patch.object(
                        index_lock.ownerfile, "remove_exact") as remove:
                def enter_lock():
                    with index_lock.IndexLock("readonly-writer"):
                        return "acquired"

                outcomes = (
                    _capture(lambda: settings.update_setting(
                        "embeddings", mutate)),
                    _capture(enter_lock),
                )
            self.assertEqual(
                (outcomes, mutate.mock_calls, remove.mock_calls, _census(root)),
                ((("raise", settings.SettingsError),
                  ("raise", PermissionError)), [], [], before))

    def test_archive_writers_refuse_without_lock_or_health_cleanup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-ro-archive-") as raw:
            root = Path(raw)
            archive_dir = root / "archive"
            archive_dir.mkdir()
            config = archive_dir / "config.json"
            health = archive_dir / "capture-health.json"
            lock = archive_dir / "lock"
            config.write_bytes(b'{"enabled":true,"keep":3}')
            health.write_bytes(b'{"outcome":"healthy"}')
            lock.write_bytes(b"malformed stale lock")
            old = time.time() - 120
            os.utime(lock, (old, old))
            before = _census(root)
            patches = (
                mock.patch.object(archive, "ARCHIVE_DIR", archive_dir),
                mock.patch.object(archive, "MANIFEST",
                                  archive_dir / "manifest.jsonl"),
                mock.patch.object(archive, "STORE", archive_dir / "store"),
                mock.patch.object(archive, "CONFIG", config),
                mock.patch.object(archive, "HEALTH", health),
                mock.patch.dict(
                    os.environ, {"AGREP_DATA_READONLY": str(root)}),
                mock.patch.object(archive.ownerfile, "remove_exact"),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], \
                    patches[5], patches[6] as remove:
                outcomes = (
                    _capture(archive._try_lock),
                    _capture(lambda: archive.set_enabled(False)),
                    _capture(archive.capture),
                    _capture(archive._prune),
                    _capture(lambda: archive._write_capture_health("busy")),
                )
            self.assertEqual(
                (outcomes, remove.mock_calls, _census(root)),
                ((("return", None),
                  ("raise", PermissionError),
                  ("raise", PermissionError),
                  ("raise", PermissionError),
                  ("raise", PermissionError)), [], before))

    def test_teach_refuses_data_and_instruction_mutations(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-ro-teach-") as raw:
            root = Path(raw)
            data = root / "data"
            proof = root / "home" / ".codex"
            data.mkdir()
            proof.mkdir(parents=True)
            target = proof / "AGENTS.md"
            state = data / "teach.json"
            state.write_text(json.dumps({
                "version": 2,
                "targets": [str(target)],
                "skills": [],
            }), encoding="utf-8")
            before = _census(root)
            with mock.patch.object(teach.common, "DATA_DIR", data), \
                    mock.patch.object(teach, "STATE_PATH", state), \
                    mock.patch.object(
                        teach, "MD_TARGETS", [("codex", proof, target)]), \
                    mock.patch.object(teach, "SKILL_TARGETS", []), \
                    mock.patch.dict(
                        os.environ, {"AGREP_DATA_READONLY": str(data)}), \
                    mock.patch.object(teach, "_sentinel_install") as sentinel, \
                    mock.patch.object(
                        teach.indexd_runtime,
                        "stop_indexers_for_removal") as stop_indexers, \
                    mock.patch("sys.stdout", new=io.StringIO()):
                outcomes = (
                    _capture(teach.reconcile),
                    _capture(lambda: teach.teach(yes=True)),
                    _capture(teach._install),
                    _capture(teach._remove),
                    _capture(lambda: teach._write_block(target)),
                    _capture(lambda: teach._write_reconcile_health({
                        "version": 1, "state": "clean", "repaired": [],
                        "refusals": [], "preserved_newer": [],
                    })),
                )
            self.assertEqual(
                (outcomes, sentinel.mock_calls, stop_indexers.mock_calls,
                 target.exists(), _census(root)),
                ((("return", []),
                  ("return", 1),
                  ("return", 1),
                  ("return", 1),
                  ("raise", PermissionError),
                  ("raise", PermissionError)), [], [], False, before))

    def test_auto_index_health_refuses_health_temp_and_source_utime(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-ro-health-") as raw:
            root = Path(raw)
            health = root / indexd_runtime.AUTO_INDEX_HEALTH
            source = root / ".ingest.sig"
            health.write_bytes(b'{"streak":0}')
            source.write_bytes(b"generation")
            before = _census(root)
            with mock.patch.object(indexd_runtime.common, "DATA_DIR", root), \
                    mock.patch.object(
                        indexd_runtime.common, "INGEST_SIG_PATH", source), \
                    mock.patch.dict(
                        os.environ, {"AGREP_DATA_READONLY": str(root)}), \
                    mock.patch.object(
                        indexd_runtime.common, "replace_with_retry") as replace, \
                    mock.patch.object(indexd_runtime.os, "utime") as utime:
                indexd_runtime.record_auto_index_health(
                    9, "must not persist", escalated=True)
            self.assertEqual(
                (replace.mock_calls, utime.mock_calls, _census(root)),
                ([], [], before))

    def test_bounded_logs_refuse_rotation_but_allow_an_outside_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-ro-log-") as raw:
            fixture = Path(raw)
            root = fixture / "protected"
            outside = fixture / "outside"
            root.mkdir()
            outside.mkdir()
            log = root / "indexd.log"
            rotated = root / "indexd.log.1"
            log.write_bytes(b"current-log")
            rotated.write_bytes(b"prior-log")
            before = _census(root)
            with mock.patch.object(resources, "DATA_DIR", root), \
                    mock.patch.object(indexd_runtime.common, "DATA_DIR", root), \
                    mock.patch.dict(
                        os.environ, {"AGREP_DATA_READONLY": str(root)}):
                outcomes = (
                    _capture(lambda: indexd_runtime.common.open_bounded_log(
                        "indexd.log", max_bytes=1)),
                    _capture(lambda: resources.open_bounded_log(
                        log, max_bytes=1)),
                )
                stream = resources.open_bounded_log(
                    outside / "allowed.log", max_bytes=1)
                try:
                    stream.write(b"outside")
                finally:
                    stream.close()
            self.assertEqual(
                (outcomes, (outside / "allowed.log").read_bytes(),
                 _census(root)),
                ((("raise", PermissionError), ("raise", PermissionError)),
                 b"outside", before))

    def test_indexd_and_post_index_entrypoints_refuse_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-ro-indexd-") as raw:
            root = Path(raw)
            for name, body in (
                    (".indexd.v2.lock", b"stale owner"),
                    (".indexd.v2.ready", b"stale ready"),
                    (".indexd.v2.spawn", b"stale spawn"),
                    ("indexd.log", b"stale log")):
                (root / name).write_bytes(body)
            before = _census(root)
            owner = mock.Mock()
            guarded_hooks = indexer.run_post_index_hooks
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(
                    indexd_runtime.common, "DATA_DIR", root))
                stack.enter_context(mock.patch.dict(
                    os.environ, {"AGREP_DATA_READONLY": str(root)}))
                clear = stack.enter_context(mock.patch.object(
                    indexd_runtime, "_clear_own_spawn_guard"))
                retire = stack.enter_context(mock.patch.object(
                    indexd_runtime, "_retire_legacy_indexd"))
                inspect = stack.enter_context(mock.patch.object(
                    indexd_runtime, "_inspect_indexd_owner"))
                open_log = stack.enter_context(mock.patch.object(
                    indexd_runtime.common, "open_bounded_log"))
                popen = stack.enter_context(mock.patch.object(
                    indexd_runtime.subprocess, "Popen"))
                owner_body = stack.enter_context(mock.patch.object(
                    indexd_runtime, "indexd_owner_body"))
                settle = stack.enter_context(mock.patch.object(
                    indexd_runtime, "_settle_indexd_owner"))
                delegate = stack.enter_context(mock.patch.object(
                    indexd_runtime, "_delegate_fts_build"))
                set_delegated = stack.enter_context(mock.patch.object(
                    indexd_runtime, "_set_fts_delegated"))
                hooks = stack.enter_context(mock.patch.object(
                    indexer, "run_post_index_hooks"))
                configured = stack.enter_context(mock.patch.object(
                    indexer, "configured_post_index_hooks"))
                run_hook = stack.enter_context(mock.patch.object(
                    indexer.subprocess, "run"))
                enable_log = stack.enter_context(mock.patch.object(
                    indexd.common, "enable_log_timestamps"))
                ingest = stack.enter_context(mock.patch.object(
                    indexd.common, "ingest_bin"))
                configure = stack.enter_context(mock.patch.object(
                    indexd.indexer, "configure_indexd_mode"))
                bind = stack.enter_context(mock.patch.object(
                    indexd.common, "bind_descendants_to_process_lifetime"))
                acquire = stack.enter_context(mock.patch.object(
                    indexd, "_acquire"))
                log = stack.enter_context(mock.patch.object(
                    indexd.common, "log"))

                outcomes = (
                    _capture(indexd_runtime._spawn_indexd),
                    _capture(indexd_runtime.acquire_indexd_owner),
                    _capture(lambda: indexd_runtime._create_indexd_owner(
                        b"owner")),
                    _capture(lambda: indexd_runtime.heartbeat_indexd_owner(
                        owner)),
                    _capture(lambda: indexd_runtime.publish_indexd_ready(
                        owner)),
                    _capture(indexd_runtime.finish_streamed_index),
                    _capture(guarded_hooks),
                    _capture(lambda: lifetime._guard(
                        50, root / ".indexd.child", "d" * 32, ["fixture"])),
                    _capture(lambda: lifetime.main(
                        ["--exec-fd", "51", "--", "fixture"])),
                    _capture(indexd.main),
                )

            blocked_calls = tuple(
                blocked_call.mock_calls
                for blocked_call in (
                    clear, retire, inspect, open_log, popen, owner_body, settle,
                    delegate, set_delegated, hooks, configured, run_hook,
                    enable_log, ingest, configure, bind, acquire))
            self.assertEqual(
                (outcomes, owner.touch.mock_calls, owner.verify.mock_calls,
                 blocked_calls, log.mock_calls, _census(root)),
                ((("return", indexd_runtime._IndexdSpawnResult.BLOCKED),
                  ("return", None),
                  ("return", None),
                  ("return", None),
                  ("raise", PermissionError),
                  ("return", None),
                  ("return", None),
                  ("return", 125),
                  ("return", 125),
                  ("return", 0)),
                 [], [],
                 ([], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                  [], []),
                 [mock.call(
                     "indexd: AGREP_DATA_READONLY protects this data "
                     "directory; exiting.")],
                 before))


if __name__ == "__main__":
    unittest.main()
