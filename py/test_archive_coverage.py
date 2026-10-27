"""Archive coverage and capture-behaviour pins.

Every store file the Rust adapters index is a capture source (pi/omp nested sidecars,
`.jsonl.gz` sessions and the cold `archive/sessions` roots included); a discovered file
the archive cannot classify is a recorded failure, never a silent gap. Capture takes the
stable prefix of a transcript that is still being appended to, reports deferred sqlite
stores as deferred, rebases long append chains, and `restore <session>` brings a whole
session back in its layout.
"""

from __future__ import annotations

from contextlib import redirect_stdout
import gzip
import hashlib
import io
import json
import os
import lzma
from pathlib import Path
import signal
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from _test_support import isolate_data_dir

isolate_data_dir()
import archive  # noqa: E402


LINE = b'{"type":"message","role":"user","content":"%d"}\n'


def _stop_command(pid: int) -> str:
    return f"taskkill /PID {pid} /F" if os.name == "nt" else f"kill {pid}"


class _ArchiveSandbox(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.saved = {name: getattr(archive, name) for name in (
            "HOME", "ARCHIVE_DIR", "MANIFEST", "STORE", "CONFIG", "HEALTH", "ROOTS",
            "_SQLITE_SETTLE_S", "_MAX_APPEND_CHAIN") if hasattr(archive, name)}
        archive.HOME = self.home
        archive.ARCHIVE_DIR = self.base / "archive"
        archive.MANIFEST = archive.ARCHIVE_DIR / "manifest.jsonl"
        archive.STORE = archive.ARCHIVE_DIR / "store"
        archive.CONFIG = archive.ARCHIVE_DIR / "config.json"
        archive.HEALTH = archive.ARCHIVE_DIR / "capture-health.json"
        archive._SQLITE_SETTLE_S = 0
        self.env = mock.patch.dict(os.environ, {
            "APPDATA": "", "CRUSH_GLOBAL_DATA": "", "XDG_DATA_HOME": "", "LOCALAPPDATA": "",
            "OPENCODE_DB": "", "CLINE_DIR": str(self.base / "no-cline")}, clear=False)
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        for name, value in self.saved.items():
            setattr(archive, name, value)
        self.temp.cleanup()

    def _write(self, relative: str, data: bytes = b"") -> Path:
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data or LINE % 1)
        return path

    def _discovered(self) -> set[Path]:
        return {path for _agent, path, _sqlite in archive._discovered_sources()}

    def _restore(self, *args, **kwargs) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            rc = archive.restore(*args, **kwargs)
        return rc, out.getvalue()


class PiOmpCoverageTests(_ArchiveSandbox):
    """Discovery mirrors pi.rs: files at any depth under a cwd slug, both suffixes,
    under sessions/ and archive/sessions/ of ~/.pi and ~/.omp."""

    def _pi_tree(self) -> dict[str, Path]:
        sid = "a529d312-1111-4111-8111-111111111111"
        child = "b1b1b1b1-2222-4222-8222-222222222222"
        other = "c3c3c3c3-3333-4333-8333-333333333333"
        slug = ".omp/agent/sessions/-home-u-projects-hollow"
        stamp = "2001-03-11T07-35-49-183Z"
        files = {
            "main": self._write(f"{slug}/{stamp}_{sid}.jsonl"),
            "sidecar": self._write(f"{slug}/{stamp}_{sid}/{stamp}_{child}.jsonl"),
            "deep": self._write(f"{slug}/{stamp}_{sid}/advisor/{stamp}_{child}.jsonl"),
            "gz": self._write(f"{slug}/{stamp}_{child}.jsonl.gz",
                              gzip.compress(LINE % 2)),
            "cold": self._write(
                f".omp/agent/archive/sessions/-home-u-old/{stamp}_{child}.jsonl"),
            "pi": self._write(f".pi/agent/sessions/-home-u-x/{stamp}_{other}.jsonl"),
            "pi_cold": self._write(
                f".pi/agent/archive/sessions/-home-u-x/{stamp}_{other}/{stamp}_{child}.jsonl.gz",
                gzip.compress(LINE % 3)),
        }
        # the Rust store predicate takes any .jsonl(.gz) under the roots, top level
        # included; other suffixes are not store files
        files["top"] = self._write(".omp/agent/sessions/stray.jsonl")
        self._write(f"{slug}/{stamp}_{sid}/notes.txt")
        return files

    def test_every_pi_store_file_the_adapter_indexes_is_discovered(self) -> None:
        files = self._pi_tree()
        self.assertEqual(self._discovered(), set(files.values()))
        stats = archive.capture()
        self.assertEqual((stats["full"], stats["failed"]), (len(files), 0))
        self.assertEqual({rec["path"] for rec in archive._records()},
                         {str(path) for path in files.values()})

    def test_restore_of_a_pi_session_brings_back_its_sidecar_files(self) -> None:
        files = self._pi_tree()
        archive.capture()
        originals = {name: path.read_bytes() for name, path in files.items()}
        for name in ("main", "sidecar", "deep"):
            files[name].unlink()
        rc, out = self._restore("a529d312")
        self.assertEqual(rc, 0, out)
        for name in ("main", "sidecar", "deep"):
            self.assertEqual(files[name].read_bytes(), originals[name])

    def test_portable_record_shape_accepts_nested_and_gz_pi_paths(self) -> None:
        for value in (
                "/h/.pi/agent/sessions/slug/2001_x.jsonl",
                "/h/.pi/agent/sessions/slug/2001_x/2001_y.jsonl",
                "/h/.omp/agent/sessions/slug/a/b/c.jsonl.gz",
                "/h/.omp/agent/archive/sessions/slug/2001_x.jsonl",
                r"C:\Users\u\.pi\agent\archive\sessions\slug\deep\2001_x.jsonl.gz"):
            self.assertIsNotNone(
                archive._portable_source_relative(value, "pi", False), value)
        self.assertIsNotNone(
            archive._portable_source_relative("/h/.pi/agent/sessions/top.jsonl", "pi", False))
        self.assertIsNone(archive._portable_source_relative(
            "/h/.pi/agent/sessions/slug/notes.txt", "pi", False))


class OtherAdapterCoverageTests(_ArchiveSandbox):
    def test_gemini_subagent_chats_and_codex_rollouts_at_any_depth(self) -> None:
        parent = "c0c0c0c0-0414-4000-8000-000000000414"
        session = self._write(f".gemini/tmp/hash1/chats/session-2026-04-14-{parent}.jsonl")
        child = self._write(f".gemini/tmp/hash1/chats/{parent}/agent-1.jsonl")
        telemetry = self._write(f".gemini/tmp/hash1/logs/session-{parent}.jsonl")
        shallow = self._write(".codex/sessions/rollout-2025-01-01T00-00-00-x.jsonl")
        dated = self._write(".codex/sessions/2026/07/06/rollout-2026-07-06T00-00-00-y.jsonl")
        odd = self._write(".codex/sessions/2026/rollout-2026-01-01T00-00-00-z.jsonl")
        found = self._discovered()
        self.assertEqual(found, {session, child, shallow, dated, odd})
        self.assertNotIn(telemetry, found)
        self.assertEqual(archive.capture()["full"], 5)

    def test_a_discovered_file_the_archive_cannot_classify_is_a_recorded_failure(self) -> None:
        planted = self._write(".codex/sessions/rollout-2026-01-01T00-00-00-q.jsonl")
        real = archive._source_location
        with mock.patch.object(
                archive, "_source_location",
                side_effect=lambda value: None if value == str(planted) else real(value)):
            stats = archive.capture()
        self.assertEqual(stats["failed"], 1, stats)
        self.assertEqual(stats["failures"][0]["path"], str(planted))
        self.assertIn("not recognized", stats["failures"][0]["error"])

    def test_glob_double_star_spans_zero_or_more_levels(self) -> None:
        for relative in ("a/x.jsonl", "a/b/x.jsonl", "a/b/c/x.jsonl"):
            self._write(f".kimi/sessions/{relative}")
        failures: list[dict] = []
        found = archive._glob_paths(self.home, ".kimi/sessions/*/**/*.jsonl", failures)
        self.assertEqual(sorted(p.relative_to(self.home).as_posix() for p in found),
                         [".kimi/sessions/a/b/c/x.jsonl", ".kimi/sessions/a/b/x.jsonl",
                          ".kimi/sessions/a/x.jsonl"])
        self.assertEqual(failures, [])
        self.assertTrue(archive._match_root_pattern(
            Path(".kimi/sessions/a/x.jsonl"), ".kimi/sessions/*/**/*.jsonl"))
        self.assertFalse(archive._match_root_pattern(
            Path(".kimi/sessions/x.jsonl"), ".kimi/sessions/*/**/*.jsonl"))


class LiveAppendCaptureTests(_ArchiveSandbox):
    def setUp(self) -> None:
        super().setUp()
        archive.ROOTS = [("codex", ".codex/sessions/rollout-*.jsonl", False)]
        self.source = self._write(".codex/sessions/rollout-live.jsonl",
                                  b"".join(LINE % i for i in range(40_000)))
        self.late = b'{"type":"message","role":"user","content":"late"}\n'

    def _write_after_copy(self, mutate):
        """Run `mutate` once the point-in-time copy of the source has been taken,
        before its identity is re-checked: the copy step is the only read of the
        live file, so this is the only instant a writer can race the capture."""
        real_run = subprocess.run
        real_copy = shutil.copyfileobj
        state = {"done": False}

        def fire() -> None:
            if not state["done"]:
                state["done"] = True
                mutate()

        def run(command, *args, **kwargs):
            result = real_run(command, *args, **kwargs)
            if "-c" in command and str(self.source) in command:
                fire()
            return result

        def copy(src, dst, *args, **kwargs):
            real_copy(src, dst, *args, **kwargs)
            if getattr(src, "name", "") == str(self.source):
                if os.name == "nt":
                    # Windows refuses to replace a file held open, so a writer's
                    # window there opens when the copy's read handle closes
                    src.close()
                fire()

        return mock.patch.multiple(archive, subprocess=mock.Mock(
            run=run, DEVNULL=subprocess.DEVNULL, Popen=subprocess.Popen),
            shutil=mock.Mock(copyfileobj=copy, disk_usage=shutil.disk_usage))

    def test_a_write_during_the_copy_defers_and_the_next_pass_captures(self) -> None:
        before = self.source.read_bytes()

        def append() -> None:
            with self.source.open("ab") as stream:
                stream.write(self.late)

        with self._write_after_copy(append):
            first = archive.capture()
        self.assertEqual((first["full"], first["failed"], first["deferred"]), (0, 0, 1), first)
        self.assertEqual(first["deferrals"][0]["kind"], "busy")
        self.assertEqual(archive._records(), [])
        second = archive.capture()
        self.assertEqual((second["full"], second["failed"]), (1, 0), second)
        record = archive._records()[-1]
        self.assertEqual(record["size"], len(before) + len(self.late))
        self.assertEqual(archive._reconstruct(record), before + self.late)
        self.assertEqual([p.name for p in archive.ARCHIVE_DIR.iterdir()
                          if p.name.startswith(archive._TEMP_PREFIXES)], [])

    def test_an_append_during_the_suffix_read_keeps_the_stable_prefix(self) -> None:
        archive.capture()
        before = self.source.read_bytes()
        with self.source.open("ab") as stream:
            stream.write(self.late)
        real_read = os.read
        ino = self.source.stat().st_ino
        state = {"done": False}

        def read(fd: int, size: int) -> bytes:
            data = real_read(fd, size)
            if not state["done"] and data and os.fstat(fd).st_ino == ino:
                state["done"] = True
                with self.source.open("ab") as stream:
                    stream.write(self.late)
            return data

        with mock.patch.object(archive.os, "read", read):
            stats = archive.capture()
        self.assertEqual((stats["appended"], stats["failed"]), (1, 0), stats)
        self.assertEqual(archive._reconstruct(archive._records()[-1]), before + self.late)
        self.assertEqual(archive.capture()["appended"], 1)
        self.assertEqual(archive._reconstruct(archive._records()[-1]),
                         before + self.late + self.late)

    def test_a_truncation_or_swap_during_the_copy_is_never_published(self) -> None:
        def truncate() -> None:
            with self.source.open("r+b") as stream:
                stream.truncate(100)

        with self._write_after_copy(truncate):
            stats = archive.capture()
        self.assertEqual((stats["full"], stats["deferred"]), (0, 1), stats)
        self.assertEqual(archive._records(), [])
        self.source.write_bytes(b"".join(LINE % i for i in range(40_000)))

        def replace() -> None:
            swap = self.source.with_name("swap.jsonl")
            swap.write_bytes(b"".join(LINE % (i + 7) for i in range(40_000)))
            os.replace(swap, self.source)

        with self._write_after_copy(replace):
            stats = archive.capture()
        self.assertEqual((stats["full"], stats["deferred"]), (0, 1), stats)
        self.assertIn("written while being copied", stats["deferrals"][0]["reason"])
        self.assertEqual(archive.capture()["full"], 1)
        self.assertEqual(archive._reconstruct(archive._records()[-1]),
                         self.source.read_bytes())

    def test_a_zero_byte_instant_is_never_published(self) -> None:
        # An in-place rewriter truncates before it writes; a capture that lands in
        # that gap must neither create an empty record nor bury the last real one.
        before = self.source.read_bytes()
        self.source.write_bytes(b"")
        self.assertEqual(archive.capture()["full"], 0)
        self.assertEqual(archive._records(), [])
        self.source.write_bytes(before)
        self.assertEqual(archive.capture()["full"], 1)
        self.source.write_bytes(b"")
        stats = archive.capture()
        self.assertEqual((stats["full"], stats["failed"]), (0, 0), stats)
        self.assertEqual(archive._reconstruct(archive._records()[-1]), before)

    def test_an_actively_rewritten_document_is_captured_consistent(self) -> None:
        """A cline history rewritten in place on every message, compression slower
        than the gap between rewrites: the published bytes are one instant's
        document (they parse), captured at most once per window on the tick."""
        document = self._write(".codex/sessions/rollout-doc.jsonl")
        self.source.unlink()
        stop = threading.Event()

        def rewriter() -> None:
            n = 0
            while not stop.is_set():
                n += 1
                body = json.dumps([{"role": "user", "content": "x" * 400, "n": n}] * 3000)
                with document.open("r+b") as stream:
                    stream.truncate(0)
                    stream.write(body.encode("ascii"))
                time.sleep(0.05)

        real_compressor = lzma.LZMACompressor

        class Slow:
            def __init__(self, *args, **kwargs):
                self.inner = real_compressor(*args, **kwargs)

            def compress(self, data):
                time.sleep(0.03)
                return self.inner.compress(data)

            def flush(self):
                return self.inner.flush()

        writer = threading.Thread(target=rewriter, daemon=True)
        writer.start()
        try:
            with mock.patch.object(archive.lzma, "LZMACompressor", Slow):
                published = None
                for _ in range(40):
                    stats = archive.capture(scheduled=True)
                    self.assertEqual(stats["failed"], 0, stats)
                    if stats["full"]:
                        published = archive._records()[-1]
                        break
                self.assertIsNotNone(published, "never captured within the window")
                json.loads(archive._reconstruct(published))
                throttled = archive.capture(scheduled=True)
                self.assertEqual((throttled["full"], throttled["deferred"]), (0, 1), throttled)
                self.assertEqual(throttled["deferrals"][0]["kind"], "rewrite")
                with mock.patch.object(archive, "_REWRITE_RECAPTURE_MS", 0):
                    for _ in range(40):
                        again = archive.capture(scheduled=True)
                        if again["full"]:
                            break
                self.assertEqual(again["full"], 1, again)
                json.loads(archive._reconstruct(archive._records()[-1]))
        finally:
            stop.set()
            writer.join()

    def test_crash_leftovers_are_swept_by_the_next_pass_and_by_purge(self) -> None:
        archive.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        orphans = [archive.ARCHIVE_DIR / ".snapshot-crashed.db",
                   archive.ARCHIVE_DIR / ".copy-crashed.src"]
        for path in orphans:
            path.write_bytes(b"left behind")
        archive.capture()
        for path in orphans:
            self.assertFalse(path.exists(), path)
            path.write_bytes(b"left behind again")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(archive._main(["--purge", "--yes"]), 0)
        self.assertEqual(sorted(p.name for p in archive.ARCHIVE_DIR.iterdir()),
                         ["config.json"] if archive.CONFIG.exists() else [])

    def test_purge_sweeps_a_crash_leftover_even_before_any_manifest(self) -> None:
        self.source.unlink()
        archive.set_enabled(False)
        orphan = archive.ARCHIVE_DIR / ".snapshot-crashed-first.db"
        orphan.write_bytes(b"x" * 4096)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(archive._main(["--purge", "--yes"]), 0)
        self.assertNotIn("nothing to purge", out.getvalue())
        self.assertIn("purged", out.getvalue())
        self.assertEqual([p.name for p in archive.ARCHIVE_DIR.iterdir()], ["config.json"])
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(archive._main(["--purge", "--yes"]), 0)
        self.assertIn("nothing to purge", out.getvalue())

    def test_append_only_growth_never_copies_the_file(self) -> None:
        archive.capture()
        with self.source.open("ab") as stream:
            stream.write(self.late)
        with mock.patch.object(archive, "_point_in_time_copy",
                               side_effect=AssertionError("copied an append")):
            stats = archive.capture()
        self.assertEqual((stats["appended"], stats["failed"]), (1, 0), stats)
        self.assertEqual(archive._reconstruct(archive._records()[-1]), self.source.read_bytes())

    def _short_volume(self, free: int):
        usage = shutil.disk_usage(archive.ARCHIVE_DIR.parent)._replace(free=free)
        return mock.patch.object(archive.shutil, "disk_usage", return_value=usage)

    def test_a_byte_copy_on_a_short_volume_is_a_verdict_reported_once(self) -> None:
        archive.set_enabled(True)
        other = self._write(".codex/sessions/rollout-other.jsonl", LINE % 9)
        with self._short_volume(archive._COPY_SPACE_RESERVE), \
                mock.patch.object(archive.sys, "platform", "linux"):
            stats = archive.capture()
        self.assertEqual((stats["full"], stats["deferred"]), (0, 2), stats)
        self.assertEqual({d["kind"] for d in stats["deferrals"]}, {"disk"})
        self.assertIn("short", stats["deferrals"][0]["reason"])
        self.assertEqual(archive._records(), [])
        self.assertEqual([p.name for p in archive.ARCHIVE_DIR.iterdir()
                          if p.name.startswith(archive._TEMP_PREFIXES)], [])
        verdict = archive.status()
        self.assertEqual(verdict["state"], "disk-full")
        self.assertIn("free space there", verdict["detail"])
        self.assertNotIn("agrep archive", verdict["detail"])
        out = io.StringIO()
        with redirect_stdout(out):
            rc = archive._main(["--status"])
        self.assertEqual(rc, 1, out.getvalue())
        self.assertEqual(out.getvalue().count("too little free space"), 1)
        self.assertEqual(out.getvalue().count(str(other)), 0)
        self.assertEqual(archive.capture()["full"], 2)
        self.assertEqual(archive.status()["state"], "healthy")

    @unittest.skipUnless(sys.platform == "darwin", "APFS clones cost no space")
    def test_a_clone_is_never_charged_against_the_reserve(self) -> None:
        # a clone costs nothing: only the xz chunk (<= source + framing) is charged
        with self._short_volume(archive._COPY_SPACE_RESERVE + self.source.stat().st_size
                                + 64 * 1024):
            stats = archive.capture()
        self.assertEqual((stats["full"], stats["deferred"]), (1, 0), stats)

    def test_a_compressed_chunk_never_writes_below_the_reserve(self) -> None:
        # the clone costs nothing, but the xz output does: on a volume at the reserve
        # the capture defers before a byte of chunk lands, on either copy path
        host_platform = sys.platform
        real_run = subprocess.run

        def clone(command, *args, **kwargs):
            # off macOS there is no `/bin/cp -c`: stand in a clone that skips the
            # space check, as APFS would
            if host_platform != "darwin" and command[:2] == ["/bin/cp", "-c"]:
                shutil.copyfile(command[-2], command[-1])
                return subprocess.CompletedProcess(command, 0)
            return real_run(command, *args, **kwargs)

        for platform in ("darwin", "linux"):
            with self.subTest(platform=platform), \
                    self._short_volume(archive._COPY_SPACE_RESERVE + 1024), \
                    mock.patch.object(archive.sys, "platform", platform), \
                    mock.patch.object(archive.subprocess, "run", clone):
                stats = archive.capture()
            self.assertEqual((stats["full"], stats["deferred"]), (0, 1), stats)
            self.assertEqual(stats["deferrals"][0]["kind"], "disk")
            self.assertFalse(archive.STORE.exists())
            self.assertEqual([p.name for p in archive.ARCHIVE_DIR.iterdir()
                              if p.name.startswith(archive._TEMP_PREFIXES)], [])

    def test_a_sqlite_backup_checks_db_plus_wal_space_before_writing_a_page(self) -> None:
        archive.ROOTS = []
        db = self.home / ".local" / "share" / "opencode" / "opencode.db"
        db.parent.mkdir(parents=True)
        connection = sqlite3.connect(db)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE message(body TEXT)")
        connection.commit()
        reader = sqlite3.connect(db)
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM message").fetchone()
        try:
            connection.executemany("INSERT INTO message VALUES(?)",
                                   [("x" * 1000,) for _ in range(2000)])
            connection.commit()
            connection.close()
            wal = db.with_name(db.name + "-wal")
            self.assertGreater(wal.stat().st_size, 0)
            needed = db.stat().st_size + wal.stat().st_size
            with self._short_volume(needed + archive._COPY_SPACE_RESERVE - 1):
                stats = archive.capture()
            self.assertEqual((stats["full"], stats["deferred"], stats["failed"]),
                             (0, 1, 0), stats)
            self.assertEqual(stats["deferrals"][0]["kind"], "disk")
            self.assertIn(archive._human_bytes(needed), stats["deferrals"][0]["reason"])
            self.assertEqual([p.name for p in archive.ARCHIVE_DIR.iterdir()
                              if p.name.startswith(".snapshot-")], [])
            with self._short_volume(needed + archive._COPY_SPACE_RESERVE + 64 * 1024):
                self.assertEqual(archive.capture()["full"], 1)
        finally:
            reader.close()

    def test_an_explicit_pass_reports_a_running_capture(self) -> None:
        holder = archive._try_lock()
        self.assertIsNotNone(holder)
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                rc = archive._main([])
            self.assertEqual(rc, 1)
            self.assertIn(f"capture running (pid {os.getpid()}, since", out.getvalue())
            archive.set_enabled(True)
            self.assertEqual(archive.status()["detail"][:16], "capture running ")
        finally:
            archive._unlock(holder)


class DaemonCaptureChildTests(unittest.TestCase):
    """The daemon only spawns the capture pass; a niced child in its own process
    group does the work, so indexing never waits on xz and `agrep remove`, which
    terminates the daemon's exact process tree, stops it too."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.home = root / "home"
        self.data = root / "data"
        self.home.mkdir()
        self.data.mkdir()
        self.saved = {name: getattr(archive, name) for name in (
            "HOME", "ARCHIVE_DIR", "MANIFEST", "STORE", "CONFIG", "HEALTH")}
        archive.HOME = self.home
        archive.ARCHIVE_DIR = self.data / "archive"
        archive.MANIFEST = archive.ARCHIVE_DIR / "manifest.jsonl"
        archive.STORE = archive.ARCHIVE_DIR / "store"
        archive.CONFIG = archive.ARCHIVE_DIR / "config.json"
        archive.HEALTH = archive.ARCHIVE_DIR / "capture-health.json"
        self.env = mock.patch.dict(os.environ, {
            "HOME": str(self.home), "AGREP_HOME": str(self.home),
            "AGREP_DATA_DIR": str(self.data), "AGREP_NO_DAEMON": "1",
            "APPDATA": "", "CRUSH_GLOBAL_DATA": "", "XDG_DATA_HOME": "",
            "LOCALAPPDATA": "", "OPENCODE_DB": "", "CLINE_DIR": str(root / "no-cline")})
        self.env.start()
        archive.set_enabled(True)

    def tearDown(self) -> None:
        self.env.stop()
        for name, value in self.saved.items():
            setattr(archive, name, value)
        self.temp.cleanup()

    def test_a_small_sqlite_store_is_captured_on_the_first_daemon_pass(self) -> None:
        db = self.home / ".local" / "share" / "opencode" / "opencode.db"
        db.parent.mkdir(parents=True)
        connection = sqlite3.connect(db)
        connection.execute("CREATE TABLE message(body TEXT)")
        connection.execute("INSERT INTO message VALUES('small')")
        connection.commit()
        connection.close()
        quiet = time.time() - archive._SQLITE_SETTLE_S - 60
        os.utime(db, (quiet, quiet))
        child = archive.spawn_capture_child()
        self.assertIsNotNone(child)
        if hasattr(os, "getpgid"):  # Windows: test_windows_child_uses_the_daemon_no_window_policy
            self.assertEqual(os.getpgid(child.pid), os.getpgid(os.getpid()))
        self.assertEqual(child.wait(timeout=60), 0)
        records = archive._records()
        self.assertEqual([rec["path"] for rec in records], [str(db)])
        self.assertTrue(records[0]["sqlite"])
        self.assertEqual(archive._capture_health()["outcome"], "healthy")

    def test_the_indexer_returns_at_once_and_stops_the_child_with_the_daemon(self) -> None:
        import indexer
        owner = types.SimpleNamespace(_archive_child=None)
        slow = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            with mock.patch.object(archive, "spawn_capture_child",
                                   return_value=slow) as spawn:
                started = time.monotonic()
                indexer.AutoIndexer._capture_archive(owner)
                indexer.AutoIndexer._capture_archive(owner)
                self.assertLess(time.monotonic() - started, 1.0)
            self.assertEqual(spawn.call_count, 1)
            self.assertIs(owner._archive_child, slow)
            self.assertIsNone(slow.poll())
            indexer.AutoIndexer._stop_archive_child(owner)
            self.assertIsNotNone(slow.wait(timeout=10))
            self.assertIsNone(owner._archive_child)
        finally:
            if slow.poll() is None:
                slow.kill()

    def _daemon(self):
        """An AutoIndexer stand-in holding the real capture hooks and one child."""
        import indexer
        owner = types.SimpleNamespace(_archive_child=None, _archive_watch=None)
        owner._stop_archive_child = lambda: indexer.AutoIndexer._stop_archive_child(owner)
        owner.tick = lambda: indexer.AutoIndexer._capture_archive(owner)
        return owner

    @unittest.skipUnless(hasattr(signal, "SIGSTOP"), "SIGSTOP is POSIX-only")
    def test_a_stopped_child_is_terminated_after_the_no_progress_bound(self) -> None:
        import indexer
        archive.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        stuck = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        owner = self._daemon()
        try:
            archive._progress(1, force=True)
            with mock.patch.object(archive, "spawn_capture_child", return_value=stuck), \
                    mock.patch.object(archive, "STALL_BOUND_S", 0.2), \
                    mock.patch.object(indexer.common, "log") as log:
                owner.tick()
                self.assertIs(owner._archive_child, stuck)
                os.kill(stuck.pid, signal.SIGSTOP)
                owner.tick()  # inside the bound: kept
                self.assertIs(owner._archive_child, stuck)
                time.sleep(0.3)
                owner.tick()  # past it in monotonic time: stopped by handle
            self.assertIsNone(owner._archive_child)
            self.assertIsNotNone(stuck.wait(timeout=10))  # the daemon's stop ended it
            self.assertIn("made no progress", log.call_args_list[0].args[0])
            self.assertIn(str(stuck.pid), log.call_args_list[0].args[0])
            self.assertEqual(archive._recorded_stall(stuck.pid), 0.0)  # idle < 1 s recorded
        finally:
            if stuck.poll() is None:
                os.kill(stuck.pid, signal.SIGCONT)
                stuck.kill()

    def test_a_clock_jump_never_stops_a_child_whose_beat_advances(self) -> None:
        """Sleep/wake moves the daemon's wall clock 1,200 s past the child's last
        stamp: the child is healthy however old that stamp looks, and only a beat
        that stays unchanged for the bound of monotonic time is a stall."""
        archive.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        slow = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        owner = self._daemon()
        real_time = time.time
        try:
            with mock.patch.object(archive, "spawn_capture_child", return_value=slow):
                owner.tick()
                archive._progress(1, force=True)  # the child's beat, stamped before the wake
                with mock.patch.object(archive.time, "time", lambda: real_time() + 1200.0):
                    self.assertGreater(archive.progress_beat()[1], 1000.0)  # looks stale
                    owner.tick()  # the daemon wakes: judged on monotonic time, kept
                    self.assertIs(owner._archive_child, slow)
                    self.assertIsNone(slow.poll())
                    archive._progress(2, force=True)  # and the beat goes on advancing
                    owner.tick()
                    self.assertIs(owner._archive_child, slow)
                # an unchanged beat over the bound in monotonic time is still a stall
                with mock.patch.object(archive, "STALL_BOUND_S", 0.2):
                    owner.tick()
                    time.sleep(0.3)
                    owner.tick()
                self.assertIsNone(owner._archive_child)
                self.assertIsNotNone(slow.wait(timeout=10))
        finally:
            if slow.poll() is None:
                slow.kill()
                slow.wait(timeout=10)

    def test_a_monotonic_jump_across_a_suspend_never_stops_a_healthy_child(self) -> None:
        """On Windows the monotonic clock counts standby too: a laptop asleep for
        20 min mid-capture wakes to one observation, never a stall."""
        import indexer
        archive.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        slow = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        owner = self._daemon()
        real_monotonic = time.monotonic
        try:
            with mock.patch.object(archive, "spawn_capture_child", return_value=slow), \
                    mock.patch.object(archive, "_tick_interval_s", return_value=60.0), \
                    mock.patch.object(indexer.common, "log") as log:
                owner.tick()
                archive._progress(1, force=True)
                owner.tick()  # observation 1 of this token, before the sleep
                with mock.patch.object(archive.time, "monotonic",
                                       lambda: real_monotonic() + 1200.0):
                    owner.tick()  # the wake: well past the bound, but a few ticks only
                    self.assertIs(owner._archive_child, slow)
                    self.assertIsNone(slow.poll())
                    watch = owner._archive_watch
                    self.assertGreater(archive.time.monotonic() - watch.since,
                                       archive.STALL_BOUND_S)  # what the tick saw
                    self.assertLess(watch.observations,
                                    archive.StallWatch.observations_required())
            log.assert_not_called()
            self.assertIsNone(archive._recorded_stall(slow.pid))
        finally:
            slow.kill()
            slow.wait(timeout=10)

    def test_a_stuck_token_over_enough_ticks_and_the_bound_still_stops(self) -> None:
        archive.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        stuck = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        owner = self._daemon()
        real_monotonic = time.monotonic
        try:
            with mock.patch.object(archive, "spawn_capture_child", return_value=stuck), \
                    mock.patch.object(archive, "STALL_BOUND_S", 900.0), \
                    mock.patch.object(archive, "_tick_interval_s", return_value=60.0):
                required = archive.StallWatch.observations_required()
                self.assertEqual(required, 15)  # ceil(900 / 60)
                owner.tick()
                archive._progress(1, force=True)
                owner.tick()  # observation 1: the token is first seen now
                # the bound passes at once (a long suspend); the ticks do not: kept
                with mock.patch.object(archive.time, "monotonic",
                                       lambda: real_monotonic() + 1200.0):
                    for _ in range(required - 2):
                        owner.tick()
                        self.assertIs(owner._archive_child, stuck)
                    owner.tick()  # the N-th unchanged observation past the bound
                self.assertIsNone(owner._archive_child)
                self.assertIsNotNone(stuck.wait(timeout=10))
                self.assertGreaterEqual(archive._recorded_stall(stuck.pid), 1200.0)
        finally:
            if stuck.poll() is None:
                stuck.kill()
                stuck.wait(timeout=10)

    def test_the_observation_count_resets_when_the_token_changes(self) -> None:
        archive.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        clock = [0.0]
        with mock.patch.object(archive, "STALL_BOUND_S", 150.0), \
                mock.patch.object(archive, "_tick_interval_s", return_value=60.0), \
                mock.patch.object(archive.time, "monotonic", lambda: clock[0]):
            self.assertEqual(archive.StallWatch.observations_required(), 3)
            watch = archive.StallWatch()
            archive._progress(1, force=True)
            for clock[0], expected in ((200.0, (False, 1)), (400.0, (False, 2))):
                self.assertEqual((watch.observe()[1], watch.observations), expected)
            archive._progress(2, force=True)  # a new token: the count starts over
            for clock[0], expected in ((600.0, (False, 1)), (800.0, (False, 2)),
                                       (1000.0, (True, 3))):
                self.assertEqual((watch.observe()[1], watch.observations), expected)

    def test_a_slow_child_that_keeps_reporting_progress_is_left_alone(self) -> None:
        archive.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        slow = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        owner = self._daemon()
        try:
            with mock.patch.object(archive, "spawn_capture_child", return_value=slow), \
                    mock.patch.object(archive, "STALL_BOUND_S", 0.2):
                owner.tick()
                for step in range(1, 4):
                    time.sleep(0.15)
                    archive._progress(step, force=True)  # what the pass does per step
                    owner.tick()
                    self.assertIs(owner._archive_child, slow)
                    self.assertIsNone(slow.poll())
        finally:
            slow.kill()
            slow.wait(timeout=10)

    def test_an_orphaned_holder_is_judged_by_wall_age_and_names_the_action(self) -> None:
        holder = archive._try_lock()  # no daemon spawned this holder: nothing else judges it
        self.assertIsNotNone(holder)
        try:
            archive._progress(1, force=True)
            lock = archive._lock_status()
            self.assertEqual((lock["state"], lock["stalled"]), ("busy", False))
            with mock.patch.object(archive, "STALL_BOUND_S", 0.0):
                time.sleep(0.01)
                lock = archive._lock_status()
                self.assertTrue(lock["stalled"])
                self.assertFalse(lock["daemon_live"])
                verdict = archive.status()
                self.assertEqual(verdict["state"], "stalled")
                self.assertIn(f"capture stalled (pid {os.getpid()}", verdict["detail"])
                self.assertIn(f"stop it with `{_stop_command(os.getpid())}`", verdict["detail"])
                self.assertNotIn("daemon", verdict["detail"])
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(archive._main(["--status"]), 1)
                self.assertIn("archive stalled: capture stalled (pid", out.getvalue())
        finally:
            archive._unlock(holder)

    def test_a_spawned_child_shows_stalled_only_on_its_daemons_verdict(self) -> None:
        daemon_pid = 4_000_000
        liveness = {daemon_pid: True}
        real_alive = archive.common.pid_alive
        real_start = archive.common.process_start_identity
        with mock.patch.dict(os.environ, {archive._SPAWNER_ENV: f"{daemon_pid}:proc_d"}):
            holder = archive._try_lock()  # this process stands in for the spawned child
        self.assertIsNotNone(holder)
        try:
            self.assertIn(b" daemon=4000000:proc_d", (archive.ARCHIVE_DIR / "lock").read_bytes())
            with mock.patch.object(archive, "STALL_BOUND_S", 0.0), \
                    mock.patch.object(archive.common, "pid_alive",
                                      lambda pid: liveness.get(pid, real_alive(pid))), \
                    mock.patch.object(archive.common, "process_start_identity",
                                      lambda pid: "proc_d" if pid == daemon_pid
                                      else real_start(pid)):
                archive._progress(1, force=True)
                time.sleep(0.01)
                # an old wall stamp alone (a wake, a clock jump) is not a stall: the
                # daemon judges by its own clock and status waits for that verdict
                lock = archive._lock_status()
                self.assertEqual((lock["stalled"], lock["daemon_live"]), (False, True), lock)
                self.assertEqual(archive.status()["state"], "busy")
                archive.record_stall(os.getpid(), 1234.0)
                lock = archive._lock_status()
                self.assertTrue(lock["stalled"] and lock["daemon_live"], lock)
                self.assertIn("the daemon that started it stops it", archive._running_line(lock))
                # the spawning daemon is gone: the promise goes with it, wall age judges
                liveness[daemon_pid] = False
                lock = archive._lock_status()
                self.assertEqual((lock["state"], lock["daemon_live"]), ("busy", False))
                self.assertIn(f"`{_stop_command(os.getpid())}`", archive._running_line(lock))
        finally:
            archive._unlock(holder)
        # a fresh pass starts without a verdict hanging over it
        archive.capture()
        self.assertIsNone(archive._recorded_stall(os.getpid()))

    def test_the_beat_count_per_pass_is_bounded_independent_of_source_count(self) -> None:
        sessions = self.home / ".codex" / "sessions"
        sessions.mkdir(parents=True)
        for i in range(400):
            (sessions / f"rollout-{i:04d}.jsonl").write_bytes(b'{"n":%d}\n' % i)
        real_replace = os.replace
        beats: list[str] = []

        def replace(src, dst, *args, **kwargs):
            if Path(dst).name == archive.PROGRESS_NAME:
                beats.append(str(src))
            return real_replace(src, dst, *args, **kwargs)

        # the rate limit reads a frozen monotonic clock, so the count depends on the
        # source count alone, never on how slowly the box ran the pass
        with mock.patch.object(archive, "ROOTS",
                               [("codex", ".codex/sessions/rollout-*.jsonl", False)]), \
                mock.patch.object(archive.os, "replace", replace), \
                mock.patch.object(archive.time, "monotonic", return_value=1_000_000.0):
            archive._last_beat = 0.0
            stats = archive.capture()
        self.assertEqual(stats["full"], 400, stats)
        self.assertEqual(len(beats), 1, beats)  # the forced first beat only
        self.assertIsNotNone(archive.progress_beat())
        # the beat itself never syncs: the atomic replace is all a reader needs
        with mock.patch.object(archive.os, "fsync", side_effect=AssertionError("beat fsync")):
            archive._progress(7, force=True)
        self.assertEqual((archive.ARCHIVE_DIR / archive.PROGRESS_NAME).read_text().split()[1], "7")

    def test_no_second_child_while_a_pass_holds_the_lock(self) -> None:
        holder = archive._try_lock()
        try:
            self.assertIsNone(archive.spawn_capture_child())
        finally:
            archive._unlock(holder)

    def test_windows_child_uses_the_daemon_no_window_policy(self) -> None:
        with mock.patch.object(archive.os, "name", "nt"), \
                mock.patch.object(archive, "capture_child_command",
                                  return_value=["py", "archive.py", "--scheduled-pass"]), \
                mock.patch.object(archive.subprocess, "Popen") as popen:
            archive.spawn_capture_child()
        self.assertEqual(popen.call_args.kwargs["creationflags"], 0x08004000)
        self.assertNotIn("nice", popen.call_args.args[0][0])

    def test_a_lock_of_a_dead_holder_is_an_interrupted_pass_not_a_wedge(self) -> None:
        archive.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        lock = archive.ARCHIVE_DIR / "lock"
        lock.write_bytes(b"pid=4000000000 start=proc_1 token=" + b"0" * 32 + b"\n")
        stale = time.time() - 600
        os.utime(lock, (stale, stale))
        with mock.patch.object(archive.common, "pid_alive", return_value=False):
            self.assertEqual(archive._lock_status()["state"], "interrupted")
            verdict = archive.status()
            self.assertEqual(verdict["state"], "interrupted")
            out = io.StringIO()
            with redirect_stdout(out):
                rc = archive._main(["--status"])
            self.assertEqual(rc, 0, out.getvalue())
            self.assertIn("interrupted; the next pass resumes", out.getvalue())
            # a malformed record past the grace is still a wedge
            lock.write_bytes(b"garbage\n")
            os.utime(lock, (stale, stale))
            self.assertEqual(archive._lock_status()["state"], "lock-wedged")
        lock.unlink()
        holder = archive._try_lock()
        try:
            self.assertEqual(archive._lock_status()["state"], "busy")
        finally:
            archive._unlock(holder)


class AppendChainRebaseTests(_ArchiveSandbox):
    def test_a_long_chain_is_rebased_onto_a_fresh_whole_file_capture(self) -> None:
        archive.ROOTS = [("codex", ".codex/sessions/rollout-*.jsonl", False)]
        archive._MAX_APPEND_CHAIN = 4
        source = self._write(".codex/sessions/rollout-chain.jsonl", LINE % 0)
        archive.capture()
        for index in range(1, 8):
            with source.open("ab") as stream:
                stream.write(LINE % index)
            os.utime(source, ns=(0, 1_000_000_000 * index))
            stats = archive.capture()
            self.assertEqual(stats["failed"], 0, stats)
            chain = len(archive._records()[-1]["chunks"])
            self.assertLessEqual(chain, archive._MAX_APPEND_CHAIN, index)
            if chain == 1 and index > 1:
                self.assertEqual(stats["full"], 1, (index, stats))
        self.assertEqual(archive._reconstruct(archive._records()[-1]), source.read_bytes())
        self.assertTrue(any(len(rec["chunks"]) == archive._MAX_APPEND_CHAIN
                            for rec in archive._records()))


class SqliteDeferralTests(_ArchiveSandbox):
    def _opencode_db(self) -> Path:
        db = self.home / ".local" / "share" / "opencode" / "opencode.db"
        db.parent.mkdir(parents=True)
        connection = sqlite3.connect(db)
        connection.execute("CREATE TABLE message(body TEXT)")
        connection.execute("INSERT INTO message VALUES('fresh')")
        connection.commit()
        connection.close()
        return db

    def test_a_freshly_written_store_is_reported_deferred_not_unchanged(self) -> None:
        archive.ROOTS = []
        archive._SQLITE_SETTLE_S = 900
        db = self._opencode_db()
        stats = archive.capture()
        self.assertEqual((stats["unchanged"], stats["deferred"]), (0, 1), stats)
        self.assertEqual(stats["deferrals"][0]["path"], str(db))
        self.assertIn("15 min", stats["deferrals"][0]["reason"])
        out = io.StringIO()
        with redirect_stdout(out):
            rc = archive._main([])
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("1 deferred", out.getvalue())
        self.assertIn(str(db), out.getvalue())
        rc, text = self._restore("opencode.db")
        self.assertEqual(rc, 1)
        self.assertIn("deferred", text)
        self.assertIn("15 min", text)


class StatusWordingTests(_ArchiveSandbox):
    def _status_lines(self) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            rc = archive._main(["--status"])
        return rc, out.getvalue()

    def test_waiting_sources_are_visible_in_status(self) -> None:
        archive.ROOTS = [("codex", ".codex/sessions/rollout-*.jsonl", False)]
        big = self._write(".codex/sessions/rollout-big.jsonl",
                          b"".join(LINE % i for i in range(20_000)))
        small = self._write(".codex/sessions/rollout-small.jsonl", LINE % 1)
        archive.set_enabled(True)
        archive.capture()
        for path in (big, small):
            path.write_bytes(path.read_bytes()[::-1])
            os.utime(path, ns=(0, 7_000_000_000))
        stats = archive.capture(scheduled=True)
        self.assertEqual({item["kind"] for item in stats["deferrals"]}, {"rewrite"}, stats)
        verdict = archive.status()
        self.assertEqual(verdict["state"], "healthy")
        self.assertEqual(verdict["waiting"]["by_kind"], {"rewrite": 2})
        self.assertEqual(verdict["waiting"]["count"], 2)
        rc, text = self._status_lines()
        self.assertEqual(rc, 0, text)
        self.assertIn("2 source(s) not captured yet: 2 rewritten in place; "
                      "next capture within 10 min", text)
        # informational kinds never become a verdict, however old
        health = json.loads(archive.HEALTH.read_text(encoding="utf-8"))
        for item in health["deferrals"].values():
            item["since_ms"] -= 48 * 3_600_000
        archive.HEALTH.write_text(json.dumps(health), encoding="utf-8")
        self.assertEqual(archive.status()["state"], "healthy")
        # a settle wait says so, and does not claim `agrep archive` captures it now
        health["deferrals"] = {str(big): {"since_ms": 1, "kind": "settle"}}
        archive.HEALTH.write_text(json.dumps(health), encoding="utf-8")
        rc, text = self._status_lines()
        self.assertEqual(rc, 0, text)
        self.assertIn("in use; captured once quiet for 15 min", text)
        self.assertNotIn("captures them now", text)
        # a copy race is informational as well and names the retry
        health["deferrals"] = {str(big): {"since_ms": 1, "kind": "busy"}}
        archive.HEALTH.write_text(json.dumps(health), encoding="utf-8")
        self.assertEqual(archive.status()["state"], "healthy")
        rc, text = self._status_lines()
        self.assertEqual(rc, 0, text)
        self.assertIn("written while being copied; retried next pass", text)
        self.assertIn("captures them now", text)
        # an explicit pass drains everything
        self.assertEqual(archive.capture()["full"], 2)
        self.assertIsNone(archive.status()["waiting"])

    def test_the_deferral_map_is_trimmed_to_the_reader_bound_keeping_the_oldest(self) -> None:
        archive.ARCHIVE_DIR.mkdir(parents=True)
        deferrals = {f"/h/.omp/agent/sessions/{'x' * 300}/{i:05d}.jsonl":
                     {"since_ms": 1_000_000 + i, "kind": "rewrite"} for i in range(5000)}
        archive._write_capture_health("healthy", completed=True, deferrals=deferrals)
        self.assertLessEqual(archive.HEALTH.stat().st_size, archive._HEALTH_MAX_BYTES)
        health = archive._capture_health()
        self.assertEqual(health["outcome"], "healthy")
        kept = health["deferrals"]
        self.assertLess(len(kept), 5000)
        self.assertEqual(min(item["since_ms"] for item in kept.values()), 1_000_000)
        self.assertEqual(max(item["since_ms"] for item in kept.values()),
                         1_000_000 + len(kept) - 1)

    def test_human_sizes_and_keep_every_version(self) -> None:
        archive.ROOTS = [("codex", ".codex/sessions/rollout-*.jsonl", False)]
        self._write(".codex/sessions/rollout-size.jsonl", b"".join(LINE % i for i in range(5000)))
        archive.set_enabled(True)
        archive.capture()
        archive._set_config(keep=0)
        rc, text = self._status_lines()
        self.assertEqual(rc, 0, text)
        self.assertIn("keeps every version (never prunes)", text)
        self.assertNotIn("0.00 GB", text)
        self.assertRegex(text, r"\d+(\.\d)? [KM]B stored")
        archive._set_config(keep=2)
        self.assertIn("keeps the last 2 version(s)", self._status_lines()[1])
        self.assertEqual(archive._human_bytes(0), "0 B")
        self.assertEqual(archive._human_bytes(51_200), "51.2 KB")
        self.assertEqual(archive._human_bytes(2_500_000_000), "2.50 GB")

    def test_status_after_off_names_retained_data_and_purge_deletes_it(self) -> None:
        archive.ROOTS = [("codex", ".codex/sessions/rollout-*.jsonl", False)]
        self._write(".codex/sessions/rollout-retained.jsonl", b"".join(LINE % i for i in range(500)))
        archive.set_enabled(True)
        archive.capture()
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(archive._main(["--off"]), 0)
        self.assertIn("copies already taken stay", out.getvalue())
        self.assertIn("archive --purge", out.getvalue())
        verdict = archive.status()
        self.assertEqual(verdict["state"], "disabled")
        self.assertGreater(verdict["stored_bytes"], 0)
        self.assertIn(archive._human_bytes(verdict["stored_bytes"]), out.getvalue())
        self.assertIn("--purge", verdict["detail"])
        rc, text = self._status_lines()
        self.assertEqual(rc, 0, text)
        self.assertIn("retention is off", text)
        self.assertIn("still stored", text)
        self.assertIn(str(archive.ARCHIVE_DIR), text)
        self.assertIn("archive --purge", text)

        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(archive.sys.stdin, "isatty",
                                                     return_value=False):
            self.assertEqual(archive._main(["--purge"]), 1)
        self.assertIn("--yes", out.getvalue())
        self.assertTrue(archive.MANIFEST.exists())

        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(archive._main(["--purge", "--yes"]), 0)
        self.assertIn("purged", out.getvalue())
        self.assertFalse(archive.MANIFEST.exists())
        self.assertFalse(archive.STORE.exists())
        self.assertTrue(archive.CONFIG.exists())
        self.assertFalse(archive._captures_retained())
        self.assertEqual(archive.status()["stored_bytes"], archive.CONFIG.stat().st_size)
        self.assertEqual(archive.status()["detail"], "automatic capture is disabled")
        rc, text = self._status_lines()
        self.assertNotIn("still stored", text)


class SessionRestoreTests(_ArchiveSandbox):
    def _claude_session(self) -> tuple[str, Path, Path, Path]:
        sid = "86273555-1111-4111-8111-111111111111"
        project = ".claude/projects/-home-u-projects-hollow"
        main = self._write(f"{project}/{sid}.jsonl", LINE % 1)
        agent_a = self._write(f"{project}/{sid}/subagents/agent-a1b2c3d4.jsonl", LINE % 2)
        agent_b = self._write(f"{project}/{sid}/subagents/agent-e5f6a7b8.jsonl", LINE % 3)
        return sid, main, agent_a, agent_b

    def test_a_session_id_restores_every_file_of_the_session_in_place(self) -> None:
        sid, main, agent_a, agent_b = self._claude_session()
        archive.capture()
        originals = {path: path.read_bytes() for path in (main, agent_a, agent_b)}
        for path in originals:
            path.unlink()
        rc, text = self._restore(sid[:8])
        self.assertEqual(rc, 0, text)
        for path, data in originals.items():
            self.assertEqual(path.read_bytes(), data)
        self.assertEqual(text.count("restored "), 3)

    def test_an_existing_file_of_the_session_is_not_overwritten_without_force(self) -> None:
        sid, main, agent_a, agent_b = self._claude_session()
        archive.capture()
        agent_a.unlink()
        main.write_bytes(b"edited after capture\n")
        rc, text = self._restore(sid[:8])
        self.assertEqual(rc, 1, text)
        self.assertEqual(main.read_bytes(), b"edited after capture\n")
        self.assertEqual(agent_a.read_bytes(), LINE % 2)
        self.assertIn("--force", text)
        rc, text = self._restore(sid[:8], force=True)
        self.assertEqual(rc, 0, text)
        self.assertEqual(main.read_bytes(), LINE % 1)

    def test_to_keeps_the_store_layout(self) -> None:
        sid, main, agent_a, agent_b = self._claude_session()
        archive.capture()
        out = self.base / "out"
        rc, text = self._restore(sid[:8], to=str(out))
        self.assertEqual(rc, 0, text)
        for path in (main, agent_a, agent_b):
            self.assertEqual((out / path.relative_to(self.home)).read_bytes(),
                             path.read_bytes())

    def test_unrelated_multi_matches_are_still_refused_in_place(self) -> None:
        archive.ROOTS = [("codex", ".codex/sessions/rollout-*.jsonl", False)]
        self._write(".codex/sessions/rollout-2026-01-01T00-00-00-x.jsonl", LINE % 1)
        self._write(".codex/sessions/rollout-2026-01-02T00-00-00-y.jsonl", LINE % 2)
        archive.capture()
        rc, text = self._restore("rollout-2026")
        self.assertEqual(rc, 1)
        self.assertIn("narrow the match", text)


if __name__ == "__main__":
    unittest.main()
