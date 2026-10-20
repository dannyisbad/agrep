"""Audit on a machine where agents keep writing: moved evidence is live, not wrong."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import re
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))
sys.path.insert(0, str(ROOT))

from _test_support import (  # noqa: E402
    isolate_data_dir, kill_mid_write, wait_for_next_ctime_tick)

isolate_data_dir()

import audit  # noqa: E402
import index_lock  # noqa: E402

HOUR_NS = 3_600 * 1_000_000_000


def _entry(agent: str, path: Path, *, seen: int | None = None) -> dict:
    info = path.stat()
    if seen is None:
        seen = sum(1 for line in path.read_bytes().split(b"\n") if line.strip())
    return {
        "agent": agent,
        "key": f"s:{int(info.st_mtime_ns // 1_000_000)}:{info.st_size}",
        "seen": seen, "rows": seen, "agent_rows": 0, "events": 0,
        "errors": 0, "skips": {}, "first_error": None,
    }


def _age(path: Path, hours: float) -> None:
    stamp = time.time_ns() - int(hours * HOUR_NS)
    os.utime(path, ns=(stamp, stamp))


def _write_book(path: Path, files: dict, *, hours_old: float) -> None:
    path.write_text(json.dumps({"version": audit.BOOK_VERSION, "files": files}),
                    encoding="utf-8")
    _age(path, hours_old)


def _corpus(path: Path, agents: tuple[str, ...]) -> None:
    db = sqlite3.connect(path)
    try:
        db.execute("CREATE TABLE msgs(agent TEXT)")
        db.executemany("INSERT INTO msgs VALUES(?)", [(agent,) for agent in agents])
        db.commit()
    finally:
        db.close()


def _bincode_str(value: str) -> bytes:
    body = value.encode("utf-8")
    return struct.pack("<Q", len(body)) + body


def _bincode_path(path: str, os_name: str | None = None) -> bytes:
    """registry.rs `SourceFile.path`: UTF-8 bytes, or UTF-16 units under Windows'
    `windows_path_serde`. Every other snapshot string is UTF-8 on every platform."""
    if (os_name or os.name) == "nt":
        units = path.encode("utf-16-le")
        return struct.pack("<Q", len(units) // 2) + units
    return _bincode_str(path)


def _write_inventory(data: Path, paths: list[str], *, selection: str = "all",
                     complete: bool = True,
                     version: int = audit._SOURCE_SNAPSHOT_VERSION) -> None:
    """A published source snapshot: bincode header, listed paths, trailing complete flag."""
    body = struct.pack("<II", version, 37) + _bincode_str(selection)
    body += struct.pack("<Q", len(paths))
    for path in paths:
        body += _bincode_str("agent") + _bincode_path(path) + struct.pack("<QQI", 1, 1, 0)
    body += struct.pack("<?", complete)
    (data / ".source_snapshot.bin").write_bytes(body)


def _wait_past_inventory(data: Path) -> None:
    """Let a later write stamp strictly after the published inventory's mtime, which
    canaries are judged against: Linux ctime ticks coarsely, and an NTFS write time
    can share the inventory's clock tick."""
    snapshot = data / ".source_snapshot.bin"
    if os.name != "nt":
        wait_for_next_ctime_tick(snapshot)
        return
    before = snapshot.stat().st_mtime_ns
    probe = data / ".mtime-probe"
    deadline = time.monotonic() + 5.0
    try:
        while True:
            probe.write_bytes(b"tick")
            if probe.stat().st_mtime_ns > before:
                return
            if time.monotonic() > deadline:
                raise AssertionError(f"mtime never advanced past {snapshot} within 5 s")
            time.sleep(0.001)
    finally:
        probe.unlink(missing_ok=True)


class LiveEvidenceTests(unittest.TestCase):
    def _run(self, root: Path, discovered, argv, *, indexed=None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(audit.common, "DATA_DIR", root))
            stack.enter_context(
                mock.patch.object(audit, "BOOK_PATH", root / "intake_stats.json"))
            stack.enter_context(mock.patch.object(audit, "_LIVE_SETTLE_S", 0.0))
            stack.enter_context(
                mock.patch.object(audit, "_census_cache_path", return_value=None))
            stack.enter_context(
                mock.patch.object(audit, "_discovered", return_value=list(discovered)))
            stack.enter_context(mock.patch.object(audit, "_live_tokens", return_value={}))
            if indexed is not None:
                stack.enter_context(mock.patch.object(
                    audit, "_indexed_agents", return_value=set(indexed)))
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            rc = audit.main([*argv, "--json"])
        return rc, json.loads(stdout.getvalue()), stderr.getvalue()

    def test_sources_written_after_the_last_index_pass_are_live_not_gaps(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            behind = root / "behind.jsonl"
            fresh_new = root / "new.jsonl"
            old_untallied = root / "old-untallied.jsonl"
            old_stale = root / "old-stale.jsonl"
            quiet = root / "quiet.jsonl"
            for path in (behind, fresh_new, old_untallied, old_stale, quiet):
                path.write_text("{}\n", encoding="utf-8")
            _age(old_untallied, 3)
            _age(old_stale, 3)
            _age(quiet, 3)
            book = {
                str(behind): _entry("codex", behind),
                str(quiet): _entry("codex", quiet),
                str(old_stale): dict(_entry("codex", old_stale), key="s:1:2"),
            }
            _write_book(root / "intake_stats.json", book, hours_old=1)
            with behind.open("a", encoding="utf-8") as stream:
                stream.write("{}\n")
            discovered = [("codex", str(path)) for path in (
                behind, fresh_new, old_untallied, old_stale, quiet)]
            for argv in ([], ["--full"]):
                with self.subTest(argv=argv):
                    rc, payload, _ = self._run(root, discovered, argv, indexed=())
                    self.assertEqual(rc, 1, payload)
                    self.assertEqual(payload["problems"], [])
                    self.assertEqual(payload["live"], 2)
                    self.assertEqual(payload["gaps"], 2)
                    live = "\n".join(payload["live_details"])
                    gaps = "\n".join(payload["gap_details"])
                    self.assertIn(f"tally behind the source (written after the last "
                                  f"index pass): {behind}", live)
                    self.assertIn(f"not yet tallied (written after the last "
                                  f"index pass): {fresh_new}", live)
                    self.assertIn(f"never tallied: {old_untallied}", gaps)
                    self.assertIn(f"stale tally (source moved): {old_stale}", gaps)
                    self.assertEqual(payload["agents"]["codex"]["live"], 2)
                    self.assertEqual(payload["agents"]["codex"]["stale"], 1)
                    self.assertEqual(payload["agents"]["codex"]["untallied"], 1)
            strict_rc, strict_payload, _ = self._run(
                root, discovered, ["--strict"], indexed=())
            self.assertEqual(strict_rc, 2, strict_payload)
            self.assertEqual(strict_payload["problems"], [])

    def test_live_writer_during_full_audit_is_uncertified_while_still_files_certify(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            live_source = root / "live.jsonl"
            quiet = root / "quiet.jsonl"
            live_source.write_text("{}\n" * 50, encoding="utf-8")
            quiet.write_text("{}\n" * 3, encoding="utf-8")
            _age(live_source, 3)
            _age(quiet, 3)
            book = {
                str(live_source): _entry("claude", live_source),
                str(quiet): _entry("claude", quiet),
            }
            _write_book(root / "intake_stats.json", book, hours_old=1)
            stop = threading.Event()
            started = threading.Event()

            def writer():
                with live_source.open("a", encoding="utf-8") as stream:
                    while not stop.is_set():
                        stream.write("{}\n")
                        stream.flush()
                        started.set()
                        time.sleep(0.002)

            thread = threading.Thread(target=writer, daemon=True)
            thread.start()
            try:
                self.assertTrue(started.wait(timeout=10))
                rc, payload, stderr = self._run(
                    root, [("claude", str(live_source)), ("claude", str(quiet))],
                    ["--full"], indexed=("claude",))
            finally:
                stop.set()
                thread.join(timeout=10)
        self.assertEqual(rc, 1, payload)
        self.assertEqual(payload["problems"], [])
        self.assertEqual(payload["live"], 1)
        self.assertIn(str(live_source), payload["live_details"][0])
        self.assertNotIn(str(quiet), "\n".join(payload["live_details"]))
        self.assertEqual(payload["gaps"], 0)
        self.assertEqual(payload["agents"]["claude"]["live"], 1)
        self.assertEqual(payload["census"]["recounted_files"], 1)
        self.assertIn("status warning", stderr)

    def test_stable_census_mismatch_stays_an_error_beside_live_sources(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            wrong = root / "wrong.jsonl"
            live_source = root / "live.jsonl"
            wrong.write_text("{}\n{}\n{}\n", encoding="utf-8")
            live_source.write_text("{}\n", encoding="utf-8")
            _age(wrong, 3)
            book = {
                str(wrong): _entry("codex", wrong, seen=2),
                str(live_source): _entry("codex", live_source),
            }
            _write_book(root / "intake_stats.json", book, hours_old=1)
            with live_source.open("a", encoding="utf-8") as stream:
                stream.write("{}\n")
            discovered = [("codex", str(wrong)), ("codex", str(live_source))]
            for argv in ([], ["--full"]):
                with self.subTest(argv=argv):
                    rc, payload, _ = self._run(root, discovered, argv, indexed=())
                    self.assertEqual(rc, 2, payload)
                    self.assertTrue(any(
                        "CENSUS MISMATCH" in item and str(wrong) in item
                        for item in payload["problems"]), payload)
                    self.assertEqual(payload["live"], 1)
                    self.assertIn(str(live_source), payload["live_details"][0])

    def test_republished_book_and_corpus_are_retried_then_certified(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "rollout.jsonl"
            source.write_text("{}\n", encoding="utf-8")
            _age(source, 3)
            book_path = root / "intake_stats.json"
            _write_book(book_path, {str(source): _entry("codex", source)}, hours_old=1)
            corpus = root / "corpus.db"
            _corpus(corpus, ("codex",))
            republished = []

            def republish_once(_path, *, deadline=None):
                # An index pass lands between the audit's first and final observations.
                if not republished:
                    republished.append(True)
                    replacement = root / "book.tmp"
                    replacement.write_bytes(book_path.read_bytes())
                    os.replace(replacement, book_path)
                    db_replacement = root / "corpus.tmp"
                    db_replacement.write_bytes(corpus.read_bytes())
                    os.replace(db_replacement, corpus)
                return 1

            with mock.patch.object(audit, "_census_jsonl", side_effect=republish_once):
                rc, payload, stderr = self._run(
                    root, [("codex", str(source))], ["--full"])
        self.assertEqual(rc, 0, payload)
        self.assertEqual(payload["status"], "clean")
        self.assertEqual(payload["live"], 0)
        self.assertIn("evidence moved during round 1", stderr)
        self.assertEqual(len(republished), 1)

    def test_evidence_that_keeps_moving_is_reported_live_after_bounded_rounds(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "rollout.jsonl"
            source.write_text("{}\n", encoding="utf-8")
            _age(source, 3)
            book_path = root / "intake_stats.json"
            _write_book(book_path, {str(source): _entry("codex", source)}, hours_old=1)
            real_stat = audit._stat_evidence

            def republish_on_observation(path):
                # The indexer never pauses: every round finds a newly published book. Each one is a
                # byte longer: a reused inode in one coarse tick would otherwise repeat an identity.
                replacement = root / "book.tmp"
                replacement.write_bytes(book_path.read_bytes() + b" ")
                os.replace(replacement, book_path)
                return real_stat(path)

            with mock.patch.object(
                    audit, "_stat_evidence", side_effect=republish_on_observation):
                rc, payload, stderr = self._run(
                    root, [("codex", str(source))], ["--full"], indexed=())
            census_reads = payload["census"]["recounted_files"]
        self.assertEqual(rc, 1, payload)
        self.assertEqual(payload["problems"], [])
        self.assertEqual(stderr.count("evidence moved during round"), audit._LIVE_SETTLE_ROUNDS)
        self.assertEqual(census_reads, 1)
        self.assertTrue(any(
            "evidence book was republished" in item
            for item in payload["live_details"]), payload)

    @staticmethod
    def _publisher_record(pid: int, start: str) -> bytes:
        return index_lock.render_record(
            pid=pid, start=start, token="a" * 32, label="corpusdb",
            time_ms=time.time_ns() // 1_000_000)

    def test_open_write_transaction_on_corpus_is_live_not_an_error(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "rollout.jsonl"
            source.write_text("{}\n", encoding="utf-8")
            _age(source, 3)
            _write_book(root / "intake_stats.json",
                        {str(source): _entry("codex", source)}, hours_old=1)
            corpus = root / "corpus.db"
            _corpus(corpus, ("codex",))
            lock = root / ".index.lock"
            start = audit.common.process_start_identity(os.getpid())
            self.assertIsNotNone(start)
            lock.write_bytes(self._publisher_record(os.getpid(), start))
            writer = sqlite3.connect(corpus, isolation_level=None)
            try:
                writer.execute("PRAGMA journal_mode=DELETE")
                writer.execute("BEGIN IMMEDIATE")
                writer.execute("INSERT INTO msgs VALUES('codex')")
                self.assertGreater(Path(f"{corpus}-journal").stat().st_size, 0)
                with mock.patch.object(audit.common, "INDEX_LOCK_PATH", lock):
                    with mock.patch.object(audit.common, "DATA_DIR", root):
                        with self.assertRaises(audit.AuditEvidenceMoved):
                            audit._indexed_agents()
                    rc, payload, _ = self._run(root, [("codex", str(source))], [])
                self.assertEqual(rc, 1, payload)
                self.assertEqual(payload["problems"], [])
                self.assertTrue(any(
                    "is being written" in item for item in payload["live_details"]),
                    payload)
                writer.execute("COMMIT")
            finally:
                writer.close()
            rc, payload, _ = self._run(root, [("codex", str(source))], [])
        self.assertEqual(rc, 0, payload)

    @unittest.skipUnless(os.name == "posix", "needs SIGKILL")
    def test_a_killed_writers_journal_is_an_interrupted_pass_not_live(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "rollout.jsonl"
            source.write_text("{}\n", encoding="utf-8")
            _age(source, 3)
            _write_book(root / "intake_stats.json",
                        {str(source): _entry("codex", source)}, hours_old=1)
            corpus = root / "corpus.db"
            _corpus(corpus, ("codex",))
            kill_mid_write(
                corpus,
                "INSERT INTO msgs WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL "
                "SELECT i + 1 FROM n WHERE i < 400) SELECT hex(randomblob(400)) FROM n")
            # the killed pass's lock record names a process that has exited
            gone = subprocess.Popen([sys.executable, "-c", ""])
            gone.wait()
            lock = root / ".index.lock"
            lock.write_bytes(self._publisher_record(gone.pid, "1"))
            with mock.patch.object(audit.common, "INDEX_LOCK_PATH", lock):
                with mock.patch.object(audit.common, "DATA_DIR", root):
                    with self.assertRaises(audit.AuditEvidenceInterrupted):
                        audit._indexed_agents()
                for argv in ([], ["--full"]):
                    with self.subTest(argv=argv), mock.patch.object(
                            audit.source_health.dist, "cli_invocation",
                            side_effect=lambda *a: ("uvx", "agrep", *map(str, a))):
                        rc, payload, stderr = self._run(
                            root, [("codex", str(source))], argv)
                        self.assertEqual(rc, 1, payload)
                        self.assertEqual(payload["status"], "warning")
                        self.assertEqual(payload["problems"], [])
                        self.assertEqual(payload["live_details"], [])
                        self.assertNotIn("evidence moved during round", stderr)
                        interrupted = [item for item in payload["gap_details"]
                                       if "awaits the next index pass" in item]
                        self.assertEqual(len(interrupted), 1, payload)
                        # searches keep serving: the wording status and doctor give
                        self.assertIn(
                            "an interrupted index pass left the search index mid-update - "
                            "searches serve its last complete update until the next one (",
                            interrupted[0])
                        self.assertNotIn("unreadable", interrupted[0])
                        self.assertIn("run `uvx agrep index`", interrupted[0])
                        self.assertNotIn("being written", interrupted[0])
            # the next pass's first write-capable open rolls the journal back
            repair = sqlite3.connect(corpus, isolation_level=None)
            try:
                repair.execute("BEGIN EXCLUSIVE")
                repair.execute("COMMIT")
            finally:
                repair.close()
            self.assertFalse(Path(f"{corpus}-journal").exists())
            rc, payload, _ = self._run(root, [("codex", str(source))], ["--full"])
        self.assertEqual(rc, 0, payload)

    def test_an_inert_journal_is_named_as_uncertifiable_not_unreadable(self):
        # A pass killed before its journal's first sync leaves a zeroed header: SQLite never
        # replays it and corpus.db reads fine, but audit's snapshot still refuses it.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "rollout.jsonl"
            source.write_text("{}\n", encoding="utf-8")
            _age(source, 3)
            _write_book(root / "intake_stats.json",
                        {str(source): _entry("codex", source)}, hours_old=1)
            corpus = root / "corpus.db"
            _corpus(corpus, ("codex",))
            Path(f"{corpus}-journal").write_bytes(bytes(12_800))
            lock = root / ".index.lock"
            stdout = io.StringIO()
            with mock.patch.object(audit.common, "INDEX_LOCK_PATH", lock), \
                    mock.patch.object(audit.source_health.dist, "cli_invocation",
                                      side_effect=lambda *a: ("uvx", "agrep", *map(str, a))):
                rc, payload, _ = self._run(root, [("codex", str(source))], ["--full"])
                with ExitStack() as stack:
                    stack.enter_context(mock.patch.object(audit.common, "DATA_DIR", root))
                    stack.enter_context(
                        mock.patch.object(audit, "BOOK_PATH", root / "intake_stats.json"))
                    stack.enter_context(
                        mock.patch.object(audit, "_census_cache_path", return_value=None))
                    stack.enter_context(mock.patch.object(
                        audit, "_discovered", return_value=[("codex", str(source))]))
                    stack.enter_context(
                        mock.patch.object(audit, "_live_tokens", return_value={}))
                    stack.enter_context(redirect_stdout(stdout))
                    stack.enter_context(redirect_stderr(io.StringIO()))
                    human_rc = audit.main([])
        self.assertEqual((rc, human_rc), (1, 1), payload)
        self.assertEqual(payload["gap_details"], [
            f"indexed-row evidence {corpus} awaits the next index pass: an interrupted index "
            "pass left the search index mid-update - searches serve its last complete update "
            "until the next one (12 KiB rollback journal), and audit cannot certify past it; "
            "run `uvx agrep index`; no clean result was certified"])
        rendered = stdout.getvalue()
        self.assertIn(
            "1 audit evidence gap(s) (a plain `uvx agrep index` clears it, as does a background "
            "indexer's next pass) - warnings", rendered)
        self.assertNotIn("seeds the book", rendered)
        self.assertNotIn("unreadable", rendered)

    @unittest.skipUnless(
        os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() != 0,
        "chmod must deny this user")
    def test_a_journal_this_user_cannot_write_is_named_with_its_fix(self):
        # An interrupted `sudo agrep` leaves root's 0644 journal: readable, yet no pass can roll
        # it back, so 'a plain index clears it' would never come true.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "rollout.jsonl"
            source.write_text("{}\n", encoding="utf-8")
            _age(source, 3)
            _write_book(root / "intake_stats.json",
                        {str(source): _entry("codex", source)}, hours_old=1)
            corpus = root / "corpus.db"
            _corpus(corpus, ("codex",))
            journal = Path(f"{corpus}-journal")
            journal.write_bytes(b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7" + bytes(504))
            journal.chmod(0o444)
            try:
                with mock.patch.object(audit.common, "INDEX_LOCK_PATH", root / ".index.lock"):
                    rc, payload, _ = self._run(root, [("codex", str(source))], ["--full"])
            finally:
                journal.chmod(0o600)
        self.assertEqual((rc, payload["status"]), (1, "warning"), payload)
        self.assertEqual(payload["live_details"], [])
        self.assertEqual(len(payload["gap_details"]), 1, payload)
        gap = payload["gap_details"][0]
        self.assertIn(f"indexed-row evidence {corpus} cannot be certified: the search index "
                      f"cannot be updated - its rollback journal at {journal} is not readable "
                      "and writable by you, so no index pass can use it: `chmod u+rw ", gap)
        self.assertNotIn("awaits the next index pass", gap)

    @unittest.skipUnless(
        os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() != 0,
        "chmod must deny this user")
    def test_a_sidecar_this_user_cannot_open_is_a_gap_with_its_fix_not_an_error(self):
        # Audit took each sidecar's identity by opening it: a chmod-000 -journal, -wal or -shm
        # was '✗ cannot stat indexed-row evidence', exit 2, three times over.
        for suffix, kind in (("-journal", "rollback journal"), ("-wal", "write-ahead log"),
                             ("-shm", "shared-memory file")):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                source = root / "rollout.jsonl"
                source.write_text("{}\n", encoding="utf-8")
                _age(source, 3)
                _write_book(root / "intake_stats.json",
                            {str(source): _entry("codex", source)}, hours_old=1)
                corpus = root / "corpus.db"
                _corpus(corpus, ("codex",))
                if suffix == "-shm":
                    Path(f"{corpus}-wal").write_bytes(b"")
                sidecar = Path(f"{corpus}{suffix}")
                sidecar.write_bytes(b"\x01" + bytes(511))
                sidecar.chmod(0)
                stdout = io.StringIO()
                try:
                    with mock.patch.object(audit.common, "INDEX_LOCK_PATH", root / ".lock"):
                        rc, payload, _ = self._run(
                            root, [("codex", str(source))], ["--full"])
                        with ExitStack() as stack:
                            stack.enter_context(
                                mock.patch.object(audit.common, "DATA_DIR", root))
                            stack.enter_context(mock.patch.object(
                                audit, "BOOK_PATH", root / "intake_stats.json"))
                            stack.enter_context(mock.patch.object(
                                audit, "_census_cache_path", return_value=None))
                            stack.enter_context(mock.patch.object(
                                audit, "_discovered",
                                return_value=[("codex", str(source))]))
                            stack.enter_context(
                                mock.patch.object(audit, "_live_tokens", return_value={}))
                            stack.enter_context(redirect_stdout(stdout))
                            stack.enter_context(redirect_stderr(io.StringIO()))
                            human_rc = audit.main([])
                finally:
                    sidecar.chmod(0o600)
                self.assertEqual((rc, human_rc, payload["problems"]), (1, 1, []), payload)
                self.assertEqual(payload["gap_details"], [
                    f"indexed-row evidence {corpus} cannot be certified: the search index "
                    f"cannot be updated - its {kind} at {sidecar} is not readable and writable "
                    f"by you, so no index pass can use it: `chmod u+rw {sidecar}`, then "
                    f"`{audit.source_health.cli_command('index')}`; no clean result was "
                    "certified"])
                rendered = stdout.getvalue()
                self.assertIn(f"1 audit evidence gap(s) (index updates wait on {sidecar}: "
                              f"`chmod u+rw {sidecar}`, then `", rendered)
                self.assertNotIn("seeds the book", rendered)
                self.assertNotIn("cannot stat", rendered)

    @unittest.skipUnless(
        os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() != 0,
        "chmod must deny this user")
    def test_an_event_store_sidecar_blocking_every_pass_heads_the_interrupted_gap(self):
        # A hot corpus.db journal waits for the next pass, but a chmod-000 event-store WAL stops
        # every pass: the gap header gives that fix, not 'a plain index clears it', hold or not.
        for held in (False, True):
            with self.subTest(held=held), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                source = root / "rollout.jsonl"
                source.write_text("{}\n", encoding="utf-8")
                _age(source, 3)
                _write_book(root / "intake_stats.json",
                            {str(source): _entry("codex", source)}, hours_old=1)
                corpus = root / "corpus.db"
                _corpus(corpus, ("codex",))
                Path(f"{corpus}-journal").write_bytes(
                    b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7" + bytes(504))
                events = root / "events"
                events.mkdir()
                store = events / audit.common.EVENT_STORE_NAME
                store.write_bytes(b"")
                wal = Path(f"{store}-wal")
                wal.write_bytes(b"\x01" + bytes(511))
                wal.chmod(0)
                health = ([{"agent": "codex", "path": str(source), "kind": "permission-denied",
                            "reason": "denied", "blocks_publication": True}] if held else [])
                stdout = io.StringIO()
                try:
                    with ExitStack() as stack:
                        for target, name, value in (
                                (audit.common, "INDEX_LOCK_PATH", root / ".lock"),
                                (audit.common, "DATA_DIR", root),
                                (audit, "BOOK_PATH", root / "intake_stats.json")):
                            stack.enter_context(mock.patch.object(target, name, value))
                        for name, value in (("_census_cache_path", None),
                                            ("_discovered", [("codex", str(source))]),
                                            ("_live_tokens", {})):
                            stack.enter_context(mock.patch.object(audit, name, return_value=value))
                        stack.enter_context(mock.patch.object(
                            audit.source_health, "source_health_issues", return_value=health))
                        stack.enter_context(redirect_stdout(stdout))
                        stack.enter_context(redirect_stderr(io.StringIO()))
                        audit.main(["--full"])
                finally:
                    wal.chmod(0o600)
                header = next(line for line in stdout.getvalue().splitlines()
                              if "audit evidence gap(s) (" in line)
                self.assertIn(f"(index updates wait on {wal}: `chmod u+rw {wal}`, then `",
                              header)
                self.assertNotIn("a plain `", header)

    def test_corrupt_corpus_is_still_an_evidence_error(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "corpus.db").write_bytes(b"SQLite format 3\0" + b"\0" * 84)
            with mock.patch.object(audit.common, "DATA_DIR", root):
                with self.assertRaises(audit.AuditEvidenceError) as caught:
                    audit._indexed_agents()
        self.assertNotIsInstance(caught.exception, audit.AuditEvidenceMoved)

    def test_full_audit_with_only_live_gaps_publishes_indexed_agent_evidence(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "rollout.jsonl"
            source.write_text("{}\n", encoding="utf-8")
            _age(source, 3)
            book_path = root / "intake_stats.json"
            _write_book(book_path, {str(source): _entry("codex", source)}, hours_old=1)
            corpus = root / "corpus.db"
            _corpus(corpus, ("codex",))
            cache = root / "audit.json"
            with source.open("a", encoding="utf-8") as stream:
                stream.write("{}\n")

            def run(argv):
                stdout = io.StringIO()
                with mock.patch.object(audit.common, "DATA_DIR", root), \
                        mock.patch.object(audit, "BOOK_PATH", book_path), \
                        mock.patch.object(audit, "_LIVE_SETTLE_S", 0.0), \
                        mock.patch.object(audit, "_ROUTINE_INDEXED_SCAN_MAX_BYTES", 0), \
                        mock.patch.object(
                            audit, "_census_cache_path", return_value=cache), \
                        mock.patch.object(
                            audit.tempfile, "gettempdir",
                            return_value=os.fspath(root / "shared-temp")), \
                        mock.patch.object(
                            audit, "_discovered",
                            return_value=[("codex", str(source))]), \
                        mock.patch.object(audit, "_live_tokens", return_value={}), \
                        mock.patch.object(
                            audit.common, "index_summary", return_value=None), \
                        redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                    rc = audit.main([*argv, "--json"])
                return rc, json.loads(stdout.getvalue())

            deferred_rc, deferred = run([])
            full_rc, full = run(["--full"])
            still_live_rc, still_live = run([])
            # The writer stops and an index pass tallies the file; corpus.db identity holds.
            _write_book(book_path, {str(source): _entry("codex", source)}, hours_old=0)
            clean_rc, clean = run([])

        self.assertEqual(deferred_rc, 1, deferred)
        self.assertIn("unindexed scan tier", deferred["routine"]["deferred_reason"])
        self.assertEqual(full_rc, 1, full)
        self.assertEqual(full["problems"], [])
        self.assertEqual(full["live"], 1)
        self.assertEqual(full["census"]["cache"],
                         "updated with completed identity-bound evidence")
        self.assertEqual(still_live_rc, 1, still_live)
        self.assertTrue(still_live["routine"]["complete"], still_live)
        self.assertTrue(still_live["routine"]["indexed_agents_from_cache"])
        self.assertEqual(clean_rc, 0, clean)
        self.assertEqual(clean["status"], "clean")
        self.assertTrue(clean["routine"]["indexed_agents_from_cache"])

    def test_routine_reads_indexed_agents_from_the_session_census_when_scan_is_deferred(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "rollout.jsonl"
            source.write_text("{}\n", encoding="utf-8")
            _age(source, 3)
            book_path = root / "intake_stats.json"
            _write_book(book_path, {str(source): _entry("codex", source)}, hours_old=1)
            _corpus(root / "corpus.db", ("codex", "cursor"))
            summary = {"sessions": 2, "messages": 3, "agents": ["codex", "cursor"],
                       "per_agent": [{"agent": "codex", "messages": 2, "sessions": 1},
                                     {"agent": "cursor", "messages": 1, "sessions": 1}]}
            stdout = io.StringIO()
            with mock.patch.object(audit.common, "DATA_DIR", root), \
                    mock.patch.object(audit, "BOOK_PATH", book_path), \
                    mock.patch.object(audit, "_ROUTINE_INDEXED_SCAN_MAX_BYTES", 0), \
                    mock.patch.object(audit, "_census_cache_path", return_value=None), \
                    mock.patch.object(
                        audit, "_discovered", return_value=[("codex", str(source))]), \
                    mock.patch.object(audit, "_live_tokens", return_value={}), \
                    mock.patch.object(
                        audit.common, "index_summary", return_value=summary), \
                    redirect_stdout(stdout):
                rc = audit.main(["--json"])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(rc, 1, payload)
        self.assertTrue(payload["routine"]["complete"], payload)
        self.assertEqual(payload["routine"]["indexed_agents_source"], "session-census")
        self.assertIn("session census", payload["census"]["disclosure"])
        self.assertTrue(any(
            "cursor: zero source files discovered despite existing indexed rows" in item
            for item in payload["gap_details"]), payload)

    def test_clean_full_audit_prunes_unlisted_absences_only_under_the_index_lock(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            data = root / "data"
            store = root / "store"
            data.mkdir()
            store.mkdir()
            source = store / "rollout.jsonl"
            source.write_text("{}\n", encoding="utf-8")
            _age(source, 3)
            gone = store / "sessions" / "2026" / "rotated.jsonl"
            still_listed = store / "sessions" / "2026" / "walked.jsonl"
            cursor_db = store / "state.vscdb"
            conversations = [
                audit._TOKEN_ID_PREFIX + json.dumps(
                    [str(cursor_db), f"conversation-{index}"], separators=(",", ":"))
                for index in range(3)]
            book = {
                str(source): _entry("codex", source),
                str(gone): {"agent": "codex", "key": "s:1:2", "seen": 2, "rows": 2,
                            "agent_rows": 0, "events": 0, "errors": 0, "skips": {}},
                str(still_listed): {"agent": "codex", "key": "s:1:2", "seen": 2, "rows": 2,
                                    "agent_rows": 0, "events": 0, "errors": 0, "skips": {}},
            }
            for key in conversations:
                book[key] = {"agent": "cursor", "key": "token", "seen": 1, "rows": 1,
                             "agent_rows": 0, "events": 0, "errors": 0, "skips": {}}
            book_path = data / "intake_stats.json"
            _write_book(book_path, book, hours_old=1)
            # The last pass walked the source and `walked.jsonl`; it never saw the others.
            _write_inventory(data, [str(source), str(still_listed)])
            published_mtime = book_path.stat().st_mtime_ns
            discovered = [("codex", str(source))]
            lock = mock.patch.object(index_lock, "INDEX_LOCK_PATH", data / ".index.lock")

            routine_rc, routine, _ = self._run(data, discovered, [], indexed=())
            self.assertEqual(routine_rc, 0, routine)
            self.assertEqual(routine["orphaned_tallies"], 3)
            self.assertEqual(routine["orphaned_tallies_pruned"], 0)
            self.assertEqual(set(json.loads(book_path.read_text())["files"]), set(book))

            with lock, index_lock.IndexLock("fixture-index"):
                held_rc, held, _ = self._run(data, discovered, ["--full"], indexed=())
            self.assertEqual(held_rc, 0, held)
            self.assertEqual(held["orphaned_tallies_pruned"], 0)
            self.assertEqual(set(json.loads(book_path.read_text())["files"]), set(book))

            with lock:
                full_rc, full, _ = self._run(data, discovered, ["--full"], indexed=())
            self.assertEqual(full_rc, 0, full)
            self.assertEqual(full["orphaned_tallies"], 3)
            # Two source paths pruned (the cursor store's three conversations are one path).
            self.assertEqual(full["orphaned_tallies_pruned"], 2)
            remaining = json.loads(book_path.read_text(encoding="utf-8"))
            self.assertEqual(remaining["version"], audit.BOOK_VERSION)
            self.assertEqual(set(remaining["files"]), {str(source), str(still_listed)})
            self.assertEqual(book_path.stat().st_mtime_ns, published_mtime)
            self.assertFalse((data / ".index.lock").exists())

            broken = dict(book)
            broken[str(source)] = dict(book[str(source)], rows=0)
            _write_book(book_path, broken, hours_old=1)
            with lock:
                error_rc, errored, _ = self._run(data, discovered, ["--full"], indexed=())
            self.assertEqual(error_rc, 2, errored)
            self.assertEqual(errored["orphaned_tallies_pruned"], 0)
            self.assertEqual(set(json.loads(book_path.read_text())["files"]), set(broken))

    def test_a_path_the_last_pass_walked_is_never_pruned(self):
        # A subtree that vanished between the pass's walk and its book commit is still in the
        # published inventory and the parse cache; directory mtimes (restored or unbumped)
        # prove nothing. Only a pass that walked without the path licenses the prune.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            data = root / "data"
            store = root / "store"
            data.mkdir()
            store.mkdir()
            source = store / "rollout.jsonl"
            source.write_text("{}\n", encoding="utf-8")
            _age(source, 3)
            project = store / "projects" / "cedar"
            project.mkdir(parents=True)
            moved_away = project / "session.jsonl"
            book = {
                str(source): _entry("codex", source),
                str(moved_away): {"agent": "codex", "key": "s:1:2", "seen": 2, "rows": 2,
                                  "agent_rows": 0, "events": 0, "errors": 0, "skips": {}},
            }
            book_path = data / "intake_stats.json"
            _write_book(book_path, book, hours_old=1)
            _write_inventory(data, [str(source), str(moved_away)])
            os.rename(project, store / "cedar-away")
            # The ancestor's mtime is restored to before the book, as an unmount or a
            # mtime-preserving tool would leave it.
            _age(store / "projects", 2)
            _age(store, 2)
            discovered = [("codex", str(source))]
            with mock.patch.object(index_lock, "INDEX_LOCK_PATH", data / ".index.lock"):
                rc, payload, _ = self._run(data, discovered, ["--full"], indexed=())
                self.assertEqual(rc, 0, payload)
                self.assertEqual(payload["orphaned_tallies"], 1)
                self.assertEqual(payload["orphaned_tallies_pruned"], 0)
                self.assertEqual(set(json.loads(book_path.read_text())["files"]), set(book))
                # A later pass walks without the path and publishes that inventory.
                _write_book(book_path, book, hours_old=0)
                _write_inventory(data, [str(source)])
                rc, payload, _ = self._run(data, discovered, ["--full"], indexed=())
                self.assertEqual(rc, 0, payload)
                self.assertEqual(payload["orphaned_tallies_pruned"], 1)
                self.assertEqual(set(json.loads(book_path.read_text())["files"]), {str(source)})

    def test_pruning_needs_a_complete_published_inventory_of_the_right_scope(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            data = root / "data"
            store = root / "store"
            data.mkdir()
            store.mkdir()
            source = store / "rollout.jsonl"
            source.write_text("{}\n", encoding="utf-8")
            _age(source, 3)
            gone = store / "gone.jsonl"
            book = {
                str(source): _entry("codex", source),
                str(gone): {"agent": "codex", "key": "s:1:2", "seen": 2, "rows": 2,
                            "agent_rows": 0, "events": 0, "errors": 0, "skips": {}},
            }
            book_path = data / "intake_stats.json"
            discovered = [("codex", str(source))]
            cases = {
                "no inventory": lambda: None,
                "incomplete walk": lambda: _write_inventory(
                    data, [str(source)], complete=False),
                "another agent's pass": lambda: _write_inventory(
                    data, [str(source)], selection="claude"),
                "pending pass": lambda: (
                    _write_inventory(data, [str(source)]),
                    (data / ".ingest_pending.bin").write_bytes(b"preflight")),
                "absence pending": lambda: (
                    _write_inventory(data, [str(source)]),
                    (data / ".source_absence_pending").write_bytes(b"")),
                "unparseable": lambda: (data / ".source_snapshot.bin").write_bytes(
                    b"\x0b\x00\x00\x00" + b"\xff" * 40),
                "older format": lambda: _write_inventory(
                    data, [str(source)], version=audit._SOURCE_SNAPSHOT_VERSION - 1),
                "newer format": lambda: _write_inventory(
                    data, [str(source)], version=audit._SOURCE_SNAPSHOT_VERSION + 1),
                "wild format": lambda: _write_inventory(data, [str(source)], version=99),
            }
            with mock.patch.object(index_lock, "INDEX_LOCK_PATH", data / ".index.lock"):
                for label, arrange in cases.items():
                    with self.subTest(label=label):
                        for stale in (".source_snapshot.bin", ".ingest_pending.bin",
                                      ".source_absence_pending"):
                            (data / stale).unlink(missing_ok=True)
                        _write_book(book_path, book, hours_old=1)
                        arrange()
                        rc, payload, _ = self._run(data, discovered, ["--full"], indexed=())
                        self.assertEqual(rc, 0, payload)
                        self.assertEqual(payload["orphaned_tallies"], 1)
                        self.assertEqual(payload["orphaned_tallies_pruned"], 0)
                        self.assertEqual(
                            set(json.loads(book_path.read_text())["files"]), set(book))
                for stale in (".ingest_pending.bin", ".source_absence_pending"):
                    (data / stale).unlink(missing_ok=True)
                # The orphan's own agent's pass is enough scope.
                _write_inventory(data, [str(source)], selection="codex")
                rc, payload, _ = self._run(data, discovered, ["--full"], indexed=())
                self.assertEqual(rc, 0, payload)
                self.assertEqual(payload["orphaned_tallies_pruned"], 1)

    def test_snapshot_version_matches_the_rust_constant(self):
        source = (ROOT / "crates" / "agrep-core" / "src" / "ingest" / "registry.rs").read_text(
            encoding="utf-8")
        declared = re.findall(r"^const SOURCE_SNAPSHOT_VERSION: u32 = (\d+);", source, re.M)
        self.assertEqual(len(declared), 1, declared)
        # The Python inventory reader parses this layout; a Rust bump must update audit.py.
        self.assertEqual(int(declared[0]), audit._SOURCE_SNAPSHOT_VERSION)

    def test_inventory_scan_matches_whole_buffer_search_exactly(self):
        listed_paths = [
            "/h/.claude/projects/-a/" + "x" * (length - 24) + ".jsonl"
            for length in (40, 255, 256, 257, 300, 512)]
        listed_paths += ["/h/\u00e9t\u00e9/caf\u00e9.jsonl", "/h/\u65e5\u672c/\u8a9e.jsonl",
                         "/h/e\u0301/n.jsonl", "/h/" + "\x00" * 0 + "zero-free.jsonl"]
        absent_paths = [
            "/h/.claude/projects/-b/" + "y" * 30 + ".jsonl",
            "/h/\u00e9t\u00e9/caf\u00e9.jsonl".replace("caf", "car"),
            "/h/" + "x" * 70000 + ".jsonl",
            "/h/lossy\ufffd.jsonl",
            "/h/.claude/projects/-a/" + "y" * 16 + ".jsonl",  # same length bucket, other body
        ]
        for os_name in ("posix", "nt"):
            with self.subTest(os_name=os_name), mock.patch.object(audit.os, "name", os_name):
                encoded = {path: audit._encoded_path(path) for path in listed_paths}
                buffer = (struct.pack("<II", audit._SOURCE_SNAPSHOT_VERSION, 37)
                          + _bincode_str("all") + struct.pack("<Q", len(listed_paths)))
                for path in listed_paths:
                    # Zero-heavy neighbours: a small u64 file length and a None option tag.
                    buffer += (_bincode_str("claude") + _bincode_path(path, os_name)
                               + struct.pack("<QQIIQ?", 1, 0, 0, 0, 0, False))
                buffer += struct.pack("<?", True)
                listed = audit._inventory_lists(buffer, set(listed_paths) | set(absent_paths))
                for path in listed_paths:
                    self.assertIs(listed[path], True, path)
                    length, units = encoded[path]
                    self.assertIn(struct.pack("<Q", length) + units, buffer)
                self.assertIs(listed[absent_paths[0]], False)
                self.assertIs(listed[absent_paths[1]], False)
                self.assertIs(listed[absent_paths[2]], None)
                self.assertIs(listed[absent_paths[3]], None)
                self.assertIs(listed[absent_paths[4]], False)

    def test_inventory_scan_is_one_pass_over_the_snapshot(self):
        paths = [f"/h/.codex/sessions/2026/{index:05d}/rollout-{index:05d}.jsonl"
                 for index in range(8_000)]
        body = struct.pack("<II", audit._SOURCE_SNAPSHOT_VERSION, 37) + _bincode_str("all")
        body += struct.pack("<Q", len(paths))
        for path in paths:
            body += (_bincode_str("codex") + _bincode_path(path)
                     + struct.pack("<QQIIQ?", 1, 0, 0, 0, 0, False))
        body += struct.pack("<?", True)
        orphans = {p.replace("rollout", "rotated") for p in paths[:4_000]} | set(paths[4_000:])
        searches = []

        class Counting(bytes):
            def find(self, *args):
                searches.append(("find", args[0]))
                return bytes.find(self, *args)

            def __contains__(self, item):
                searches.append(("in", item))
                return bytes.__contains__(self, item)

        passes = []
        real_pattern = audit._ZERO_RUN

        class CountingPattern:
            def finditer(self, buffer):
                passes.append(len(buffer))
                return real_pattern.finditer(buffer)

        with mock.patch.object(audit, "_ZERO_RUN", CountingPattern()):
            listed = audit._inventory_lists(Counting(body), orphans)
        self.assertEqual(sum(1 for state in listed.values() if state is True), 4_000)
        self.assertEqual(sum(1 for state in listed.values() if state is False), 4_000)
        # One pass over the whole buffer; no search per orphan.
        self.assertEqual(passes, [len(body)])
        self.assertEqual(searches, [])

    def test_prune_scans_before_the_lock_and_rechecks_the_inventory_under_it(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            data = root / "data"
            store = root / "store"
            data.mkdir()
            store.mkdir()
            source = store / "rollout.jsonl"
            source.write_text("{}\n", encoding="utf-8")
            _age(source, 3)
            gone = store / "gone.jsonl"
            book = {
                str(source): _entry("codex", source),
                str(gone): {"agent": "codex", "key": "s:1:2", "seen": 2, "rows": 2,
                            "agent_rows": 0, "events": 0, "errors": 0, "skips": {}},
            }
            book_path = data / "intake_stats.json"
            _write_book(book_path, book, hours_old=1)
            _write_inventory(data, [str(source)])
            events = []
            real_scan = audit._inventory_lists
            real_enter = index_lock.IndexLock.__enter__

            def scan(inventory, paths):
                events.append("scan")
                return real_scan(inventory, paths)

            def enter(self):
                events.append("lock")
                handle = real_enter(self)
                if events.count("lock") == 2:
                    # An index pass republishes the inventory between scan and lock.
                    _write_inventory(data, [str(source), str(gone)])
                return handle

            lock = mock.patch.object(index_lock, "INDEX_LOCK_PATH", data / ".index.lock")
            with lock, mock.patch.object(audit, "_inventory_lists", side_effect=scan), \
                    mock.patch.object(index_lock.IndexLock, "__enter__", enter):
                rc, payload, _ = self._run(data, [("codex", str(source))], ["--full"], indexed=())
                self.assertEqual(rc, 0, payload)
                self.assertEqual(payload["orphaned_tallies_pruned"], 1)
                self.assertEqual(events, ["scan", "lock"])
                _write_book(book_path, book, hours_old=1)
                _write_inventory(data, [str(source)])
                rc, payload, _ = self._run(data, [("codex", str(source))], ["--full"], indexed=())
            self.assertEqual(rc, 0, payload)
            self.assertEqual(payload["orphaned_tallies_pruned"], 0)
            self.assertEqual(events, ["scan", "lock", "scan", "lock"])
            self.assertEqual(set(json.loads(book_path.read_text())["files"]), set(book))

    def _orphan_fixture(self, root: Path) -> tuple:
        data = root / "data"
        store = root / "store"
        data.mkdir()
        store.mkdir()
        source = store / "rollout.jsonl"
        source.write_text("{}\n", encoding="utf-8")
        _age(source, 3)
        gone = store / "gone.jsonl"
        book = {
            str(source): _entry("codex", source),
            str(gone): {"agent": "codex", "key": "s:1:2", "seen": 2, "rows": 2,
                        "agent_rows": 0, "events": 0, "errors": 0, "skips": {}},
        }
        book_path = data / "intake_stats.json"
        _write_book(book_path, book, hours_old=1)
        _write_inventory(data, [str(source)])
        return data, store, source, book_path, book

    def test_pruning_refuses_when_present_sources_are_not_found_in_the_inventory(self):
        # The scan proves its own encoding on sources the last pass must have walked. A
        # reader whose encoding differs from the writer's (the other platform's path
        # encoding here) finds none of them, and must prune nothing.
        real_scan = audit._inventory_lists
        foreign = "posix" if os.name == "nt" else "nt"

        def foreign_scan(inventory, paths):
            with mock.patch.object(audit.os, "name", foreign):
                return real_scan(inventory, paths)

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            data, _store, source, book_path, book = self._orphan_fixture(root)
            discovered = [("codex", str(source))]
            with mock.patch.object(index_lock, "INDEX_LOCK_PATH", data / ".index.lock"):
                with mock.patch.object(audit, "_inventory_lists", side_effect=foreign_scan):
                    rc, payload, _ = self._run(data, discovered, ["--full"], indexed=())
                self.assertEqual(rc, 0, payload)
                self.assertEqual(payload["orphaned_tallies"], 1)
                self.assertEqual(payload["orphaned_tallies_pruned"], 0)
                note = payload["orphaned_tallies_note"]
                self.assertIn("1 present tallied source", note)
                self.assertIn(str(source), note)
                self.assertIn("not in the last index pass's inventory", note)
                self.assertIn("moved back", note)
                self.assertIn("nothing is pruned", note)
                self.assertEqual(set(json.loads(book_path.read_text())["files"]), set(book))
                stdout = io.StringIO()
                with mock.patch.object(audit.common, "DATA_DIR", data), \
                        mock.patch.object(audit, "BOOK_PATH", book_path), \
                        mock.patch.object(audit, "_census_cache_path", return_value=None), \
                        mock.patch.object(audit, "_discovered", return_value=discovered), \
                        mock.patch.object(audit, "_live_tokens", return_value={}), \
                        mock.patch.object(audit, "_indexed_agents", return_value=set()), \
                        mock.patch.object(audit, "_inventory_lists", side_effect=foreign_scan), \
                        redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                    audit.main(["--full"])
                self.assertIn("not pruned: 1 present tallied source", stdout.getvalue())
                # The same inventory read with the writer's encoding prunes.
                rc, payload, _ = self._run(data, discovered, ["--full"], indexed=())
                self.assertEqual(rc, 0, payload)
                self.assertEqual(payload["orphaned_tallies_pruned"], 1)
                self.assertIsNone(payload["orphaned_tallies_note"])

    def test_present_sources_newer_than_the_inventory_do_not_block_pruning(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            data, store, source, book_path, book = self._orphan_fixture(root)
            # Written after the inventory: the last pass could not have walked it.
            _wait_past_inventory(data)
            fresh = store / "fresh.jsonl"
            fresh.write_text("{}\n", encoding="utf-8")
            # Restored after the inventory: an old mtime, a new change time.
            restored = store / "restored.jsonl"
            restored.write_text("{}\n", encoding="utf-8")
            _age(restored, 3)
            for path in (fresh, restored):
                book[str(path)] = _entry("codex", path)
            _write_book(book_path, book, hours_old=1)
            discovered = [("codex", str(path)) for path in (source, fresh, restored)]
            with mock.patch.object(index_lock, "INDEX_LOCK_PATH", data / ".index.lock"):
                rc, payload, _ = self._run(data, discovered, ["--full"], indexed=())
        self.assertEqual(rc, 0, payload)
        self.assertEqual(payload["problems"], [])
        self.assertEqual(payload["orphaned_tallies"], 1)
        self.assertEqual(payload["orphaned_tallies_pruned"], 1)
        self.assertIsNone(payload["orphaned_tallies_note"])

    @unittest.skipIf(os.name == "nt", "control characters are not legal in Windows names")
    def test_human_output_escapes_store_derived_paths_and_names(self):
        hostile = "\x1b]0;pwned\x07\x1b[31mred"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            data = root / "data"
            store = root / f"-proj-{hostile}"
            data.mkdir()
            store.mkdir()
            source = store / "rollout.jsonl"
            source.write_text("{}\n", encoding="utf-8")
            _age(source, 3)
            gone = store / "gone.jsonl"
            book = {
                # A skip name is a book string; the parser never writes one like this.
                str(source): dict(_entry("codex", source), rows=0, skips={f"meta{hostile}": 1}),
                str(gone): {"agent": "codex", "key": "s:1:2", "seen": 2, "rows": 2,
                            "agent_rows": 0, "events": 0, "errors": 0, "skips": {}},
            }
            book_path = data / "intake_stats.json"
            _write_book(book_path, book, hours_old=1)
            # The last pass listed nothing from this store (moved back since), so the
            # prune note names the present source, hostile directory and all.
            _write_inventory(data, [])
            stdout = io.StringIO()
            with mock.patch.object(audit.common, "DATA_DIR", data), \
                    mock.patch.object(audit, "BOOK_PATH", book_path), \
                    mock.patch.object(index_lock, "INDEX_LOCK_PATH", data / ".index.lock"), \
                    mock.patch.object(audit, "_census_cache_path", return_value=None), \
                    mock.patch.object(
                        audit, "_discovered", return_value=[("codex", str(source))]), \
                    mock.patch.object(audit, "_live_tokens", return_value={}), \
                    mock.patch.object(audit, "_indexed_agents", return_value=set()), \
                    redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                rc = audit.main(["--full"])
        rendered = stdout.getvalue()
        self.assertEqual(rc, 0, rendered)
        self.assertIn("not pruned: 1 present tallied source", rendered)
        self.assertIn("skips: meta", rendered)
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\x07", rendered)
        self.assertIn("\\u001b", rendered)

    def test_pruning_needs_a_present_source_to_vouch_for_the_inventory(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            data, _store, source, book_path, book = self._orphan_fixture(root)
            _wait_past_inventory(data)
            with source.open("a", encoding="utf-8") as stream:
                stream.write("{}\n")
            book[str(source)] = _entry("codex", source)
            _write_book(book_path, book, hours_old=1)
            with mock.patch.object(index_lock, "INDEX_LOCK_PATH", data / ".index.lock"):
                rc, payload, _ = self._run(
                    data, [("codex", str(source))], ["--full"], indexed=())
            self.assertEqual(rc, 0, payload)
            self.assertEqual(payload["orphaned_tallies_pruned"], 0)
            self.assertIn("can vouch for it", payload["orphaned_tallies_note"])
            self.assertEqual(set(json.loads(book_path.read_text())["files"]), set(book))

    def test_coverage_gaps_block_pruning(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            data = root / "data"
            store = root / "store"
            data.mkdir()
            store.mkdir()
            source = store / "rollout.jsonl"
            source.write_text("{}\n", encoding="utf-8")
            _age(source, 3)
            gone = store / "gone.jsonl"
            book = {
                str(source): _entry("codex", source),
                str(gone): {"agent": "codex", "key": "s:1:2", "seen": 2, "rows": 2,
                            "agent_rows": 0, "events": 0, "errors": 0, "skips": {}},
            }
            book_path = data / "intake_stats.json"
            _write_book(book_path, book, hours_old=1)
            _write_inventory(data, [str(source)])
            # claude rows are indexed but its store is not discovered: a coverage gap.
            with mock.patch.object(index_lock, "INDEX_LOCK_PATH", data / ".index.lock"):
                rc, payload, _ = self._run(
                    data, [("codex", str(source))], ["--full"], indexed=("claude",))
                self.assertEqual(rc, 1, payload)
                self.assertTrue(any("claude: zero source files discovered" in item
                                    for item in payload["gap_details"]), payload)
                self.assertEqual(payload["orphaned_tallies"], 1)
                self.assertEqual(payload["orphaned_tallies_pruned"], 0)
                self.assertEqual(set(json.loads(book_path.read_text())["files"]), set(book))
                rc, payload, _ = self._run(
                    data, [("codex", str(source))], ["--full"], indexed=())
                self.assertEqual(rc, 0, payload)
                self.assertEqual(payload["orphaned_tallies_pruned"], 1)

    def test_human_output_lists_live_items_and_pruning(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            data = root / "data"
            store = root / "store"
            data.mkdir()
            store.mkdir()
            source = store / "rollout.jsonl"
            source.write_text("{}\n", encoding="utf-8")
            quiet = store / "quiet.jsonl"
            quiet.write_text("{}\n", encoding="utf-8")
            _age(quiet, 3)
            gone = store / "gone.jsonl"
            book = {
                str(source): _entry("codex", source),
                str(quiet): _entry("codex", quiet),
                str(gone): {"agent": "codex", "key": "s:1:2", "seen": 2, "rows": 2,
                            "agent_rows": 0, "events": 0, "errors": 0, "skips": {}},
            }
            book_path = data / "intake_stats.json"
            _write_book(book_path, book, hours_old=1)
            _write_inventory(data, [str(source), str(quiet)])
            with source.open("a", encoding="utf-8") as stream:
                stream.write("{}\n")
            outputs = {}
            for argv in (["--full"], []):
                stdout = io.StringIO()
                with mock.patch.object(audit.common, "DATA_DIR", data), \
                        mock.patch.object(audit, "BOOK_PATH", book_path), \
                        mock.patch.object(
                            index_lock, "INDEX_LOCK_PATH", data / ".index.lock"), \
                        mock.patch.object(audit, "_census_cache_path", return_value=None), \
                        mock.patch.object(
                            audit, "_discovered",
                            return_value=[("codex", str(source)), ("codex", str(quiet))]), \
                        mock.patch.object(audit, "_live_tokens", return_value={}), \
                        mock.patch.object(audit, "_indexed_agents", return_value=set()), \
                        redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                    outputs[tuple(argv)] = (audit.main(argv), stdout.getvalue())
                if argv:
                    # Restore the pruned tally so the routine run reports the orphan too.
                    _write_book(book_path, book, hours_old=1)
        rc, rendered = outputs[("--full",)]
        self.assertEqual(rc, 1, rendered)
        self.assertIn("1 live/uncertified evidence item", rendered)
        self.assertIn("tally behind the source", rendered)
        self.assertIn("1 tallied file no longer on disk (rotated/deleted); 1 tally pruned",
                      rendered)
        self.assertNotIn("accounting/evidence error", rendered)
        routine_rc, routine_rendered = outputs[()]
        self.assertEqual(routine_rc, 1, routine_rendered)
        # The routine note states the real gate: errors and coverage gaps block pruning, live
        # evidence does not.
        self.assertIn("1 tallied file no longer on disk (rotated/deleted); not pruned: "
                      "a full audit with no errors and no coverage gaps prunes them",
                      routine_rendered)

    def test_live_file_does_not_hide_a_held_still_stale_agent(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            stale = root / "old.jsonl"
            active = root / "active.jsonl"
            for path in (stale, active):
                path.write_text("{}\n", encoding="utf-8")
            _age(stale, 3)
            book = {
                str(stale): dict(_entry("codex", stale), key="s:1:2"),
                str(active): _entry("codex", active),
            }
            _write_book(root / "intake_stats.json", book, hours_old=1)
            with active.open("a", encoding="utf-8") as stream:
                stream.write("{}\n")
            discovered = [("codex", str(stale)), ("codex", str(active))]
            for argv in ([], ["--full"]):
                with self.subTest(argv=argv):
                    rc, payload, _ = self._run(root, discovered, argv, indexed=())
                    self.assertEqual(rc, 2, payload)
                    self.assertTrue(any(
                        "no tallied source file had fresh readable evidence" in item
                        for item in payload["problems"]), payload)
                    self.assertEqual(payload["live"], 1)
                    self.assertEqual(payload["agents"]["codex"]["stale"], 1)
            # With nothing held still and stale, the live file alone explains the agent.
            rc, payload, _ = self._run(root, [("codex", str(active))], [], indexed=())
            self.assertEqual(rc, 1, payload)
            self.assertEqual(payload["problems"], [])

    def test_agent_scoped_full_audit_publishes_every_indexed_agent(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "rollout.jsonl"
            source.write_text("{}\n", encoding="utf-8")
            _age(source, 3)
            book_path = root / "intake_stats.json"
            _write_book(book_path, {str(source): _entry("codex", source)}, hours_old=1)
            _corpus(root / "corpus.db", ("codex", "claude"))
            cache = root / "audit.json"
            republished = []

            def republish_book_once(_path, *, deadline=None):
                # An index pass lands in round 1; corpus.db holds still throughout.
                if not republished:
                    republished.append(True)
                    replacement = root / "book.tmp"
                    replacement.write_bytes(book_path.read_bytes())
                    os.replace(replacement, book_path)
                return 1

            def run(argv, census=None):
                stdout = io.StringIO()
                with ExitStack() as stack:
                    stack.enter_context(mock.patch.object(audit.common, "DATA_DIR", root))
                    stack.enter_context(mock.patch.object(audit, "BOOK_PATH", book_path))
                    stack.enter_context(mock.patch.object(audit, "_LIVE_SETTLE_S", 0.0))
                    stack.enter_context(mock.patch.object(
                        audit, "_census_cache_path", return_value=cache))
                    stack.enter_context(mock.patch.object(
                        audit.tempfile, "gettempdir",
                        return_value=os.fspath(root / "shared-temp")))
                    stack.enter_context(mock.patch.object(
                        audit, "_discovered", return_value=[("codex", str(source))]))
                    stack.enter_context(mock.patch.object(
                        audit, "_live_tokens", return_value={}))
                    if census is not None:
                        stack.enter_context(mock.patch.object(
                            audit, "_census_jsonl", side_effect=census))
                    stack.enter_context(redirect_stdout(stdout))
                    stack.enter_context(redirect_stderr(io.StringIO()))
                    rc = audit.main([*argv, "--json"])
                return rc, json.loads(stdout.getvalue())

            scoped_rc, scoped = run(["--full", "--agent", "codex"], republish_book_once)
            published = json.loads(cache.read_text(encoding="utf-8"))["indexed_agents"]
            routine_rc, routine = run([])
        self.assertEqual(scoped_rc, 0, scoped)
        self.assertEqual(len(republished), 1)
        self.assertEqual(published["agents"], ["claude", "codex"])
        self.assertEqual(routine_rc, 1, routine)
        self.assertTrue(routine["routine"]["indexed_agents_from_cache"])
        self.assertTrue(any(
            "claude: zero source files discovered despite existing indexed rows" in item
            for item in routine["gap_details"]), routine)

    def test_session_census_fallback_keeps_the_routine_source_budget(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            sources = []
            book = {}
            for index in range(20):
                source = root / f"rollout-{index}.jsonl"
                source.write_text("{}\n", encoding="utf-8")
                _age(source, 3)
                sources.append(source)
                book[str(source)] = _entry("codex", source)
            _write_book(root / "intake_stats.json", book, hours_old=1)
            deadlines = []
            clock = {"now": 1_000.0}

            def slow_summary(*, deadline=None):
                # A large session census spends exactly the deadline it was given.
                deadlines.append((clock["now"], deadline))
                clock["now"] = deadline
                raise TimeoutError("index summary budget exceeded")

            def scan_tier(**_kwargs):
                raise audit.AuditRoutineBudget(
                    "routine indexed-agent DISTINCT census deferred: exceeds the "
                    "unindexed scan tier")

            with mock.patch.object(audit.time, "monotonic", lambda: clock["now"]), \
                    mock.patch.object(
                        audit.common, "index_summary", side_effect=slow_summary), \
                    mock.patch.object(audit, "_indexed_agents", side_effect=scan_tier):
                rc, payload, _ = self._run(
                    root, [("codex", str(path)) for path in sources], [])
        self.assertEqual(rc, 1, payload)
        self.assertEqual(deadlines, [(1_000.0, 1_000.0 + audit._ROUTINE_INDEXED_QUERY_BUDGET_S)])
        self.assertIn("unindexed scan tier", payload["routine"]["deferred_reason"])
        # The fallback took only its own share: every source was still recounted.
        self.assertEqual(payload["census"]["recounted_files"], 20)
        self.assertEqual(payload["census"]["pending_files"], 0)

    def test_settle_rounds_reuse_the_prior_snapshot_book_and_distinct_census(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "rollout.jsonl"
            source.write_text("{}\n", encoding="utf-8")
            _age(source, 3)
            book_path = root / "intake_stats.json"
            entry = _entry("codex", source)
            _write_book(book_path, {str(source): entry}, hours_old=1)
            _corpus(root / "corpus.db", ("codex",))
            boundaries = iter(f"{index:064x}" for index in range(1, 100))
            witness = "f" * 64

            def snapshot(*, selection, timeout_s):
                # A sidecar keeps the boundary moving; the content witness holds still.
                del timeout_s
                return {
                    "snapshot_sha256": "a" * 64,
                    "boundary_sha256": next(boundaries),
                    "selection": selection,
                    "discovered": [("codex", str(source))],
                    "tokens": {},
                    "witnesses": {("codex", str(source)): (entry["key"], witness)},
                }

            real_snapshot = audit.ownerfile.snapshot
            book_reads = []

            def counting_snapshot(path, **kwargs):
                if Path(path) == book_path:
                    book_reads.append(path)
                return real_snapshot(path, **kwargs)

            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch.object(audit.common, "DATA_DIR", root), \
                    mock.patch.object(audit, "BOOK_PATH", book_path), \
                    mock.patch.object(audit, "_LIVE_SETTLE_S", 0.0), \
                    mock.patch.object(audit, "_census_cache_path", return_value=None), \
                    mock.patch.object(
                        audit, "_combined_store_snapshot", side_effect=snapshot) as stores, \
                    mock.patch.object(
                        audit.ownerfile, "snapshot", side_effect=counting_snapshot), \
                    mock.patch.object(
                        audit.events, "open_sqlite_snapshot",
                        wraps=audit.events.open_sqlite_snapshot) as distinct, \
                    mock.patch.object(
                        audit.time, "time_ns", return_value=self._settled_clock(root)), \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                rc = audit.main(["--full", "--json"])
            payload = json.loads(stdout.getvalue())
        rounds = 1 + audit._LIVE_SETTLE_ROUNDS
        self.assertEqual(rc, 1, payload)
        self.assertEqual(payload["problems"], [])
        self.assertEqual(stderr.getvalue().count("evidence moved during round"), rounds - 1)
        self.assertEqual(stores.call_count, rounds + 1)
        self.assertEqual(len(book_reads), 1)
        self.assertEqual(distinct.call_count, 1)
        self.assertEqual(payload["census"]["recounted_files"], 1)

    def _full_at_round_clocks(self, root: Path, source: Path, clocks, *,
                              between_rounds=None, frozen=()):
        """--full over every settle round, each round on its own wall clock.

        A moving store boundary forces the settle rounds; ``frozen`` patches pin identities so a
        rewrite between rounds lands in the same tick, as a coarse clock lets it."""
        entry = json.loads((root / "intake_stats.json").read_text(
            encoding="utf-8"))["files"][str(source)]
        boundaries = iter(f"{index:064x}" for index in range(1, 100))

        def snapshot(*, selection, timeout_s):
            del timeout_s
            return {
                "snapshot_sha256": "a" * 64,
                "boundary_sha256": next(boundaries),
                "selection": selection,
                "discovered": [("codex", str(source))],
                "tokens": {},
                "witnesses": {("codex", str(source)): (entry["key"], "f" * 64)},
            }

        real_round = audit._audit_round
        round_clocks = list(clocks)
        rounds_run = []

        def round_at(*args, **kwargs):
            index = kwargs["round_index"]
            rounds_run.append(index)
            with mock.patch.object(
                    audit.time, "time_ns", return_value=round_clocks[index]):
                result = real_round(*args, **kwargs)
            if between_rounds is not None:
                between_rounds(index)
            return result

        cache = root / "cache" / "audit.json"
        stdout = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(audit.common, "DATA_DIR", root))
            stack.enter_context(
                mock.patch.object(audit, "BOOK_PATH", root / "intake_stats.json"))
            stack.enter_context(mock.patch.object(audit, "_LIVE_SETTLE_S", 0.0))
            stack.enter_context(
                mock.patch.object(audit, "_census_cache_path", return_value=cache))
            # Outside the simulated Windows shared temp root, so the fixture cache is trusted.
            stack.enter_context(mock.patch.object(
                audit.tempfile, "gettempdir", return_value=str(root / "shared-temp")))
            stack.enter_context(mock.patch.object(
                audit, "_combined_store_snapshot", side_effect=snapshot))
            stack.enter_context(
                mock.patch.object(audit, "_audit_round", side_effect=round_at))
            census = stack.enter_context(mock.patch.object(
                audit, "_census_jsonl", wraps=audit._census_jsonl))
            distinct = stack.enter_context(mock.patch.object(
                audit.events, "open_sqlite_snapshot",
                wraps=audit.events.open_sqlite_snapshot))
            for patch in frozen:
                stack.enter_context(patch)
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(io.StringIO()))
            rc = audit.main(["--full", "--json"])
        self.assertEqual(rounds_run, list(range(1 + audit._LIVE_SETTLE_ROUNDS)))
        files, indexed = (
            audit._read_audit_cache(cache) if cache.exists() else ({}, None))
        return (rc, json.loads(stdout.getvalue()), files, indexed,
                census.call_count, distinct.call_count)

    @staticmethod
    def _settle_fixture(root: Path, content: str) -> Path:
        source = root / "rollout.jsonl"
        source.write_text(content, encoding="utf-8", newline="\n")
        _age(source, 3)
        _write_book(root / "intake_stats.json",
                    {str(source): _entry("codex", source)}, hours_old=1)
        _corpus(root / "corpus.db", ("codex",))
        return source

    @staticmethod
    def _settled_clock(root: Path) -> int:
        return 3_000_000_000 + max(
            stamp for path in root.iterdir() if path.is_file()
            for stamp in (path.stat().st_mtime_ns, path.stat().st_ctime_ns))

    @unittest.skipIf(audit.fileops.WIN, "Windows identities carry a change token; no racy window")
    def test_settle_rounds_reread_a_count_taken_inside_the_racy_window(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = self._settle_fixture(root, "{}\n{}\n")
            observed = audit._stat_evidence(str(source))
            real_stat = audit._stat_evidence
            racy = max(observed[1][3:5]) - 1
            settled = self._settled_clock(root)

            def rewrite_in_the_same_tick(index):
                if index == 0:
                    source.write_text("{}\n\n\n\n", encoding="utf-8", newline="\n")

            rc, payload, files, _indexed, census_reads, _distinct = (
                self._full_at_round_clocks(
                    root, source, [racy, settled, settled],
                    between_rounds=rewrite_in_the_same_tick,
                    frozen=[mock.patch.object(
                        audit, "_stat_evidence",
                        side_effect=lambda path: (
                            observed if path == str(source) else real_stat(path)))]))
        self.assertEqual(census_reads, 2, payload)
        self.assertEqual(files[str(source)]["nonblank_lines"], 1, payload)
        self.assertEqual(rc, 2, payload)

    @unittest.skipIf(audit.fileops.WIN, "Windows identities carry a change token; no racy window")
    def test_settle_rounds_never_memoise_a_racy_indexed_scan(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = self._settle_fixture(root, "{}\n")
            corpus = root / "corpus.db"
            identity = audit._sqlite_family_identity(corpus)
            real_identity = audit._sqlite_family_identity
            racy = identity[0][1][3] - 1
            settled = self._settled_clock(root)

            def republish_in_the_same_tick(index):
                if index == 0:
                    corpus.unlink()
                    _corpus(corpus, ("crush",))

            rc, payload, _files, indexed, _census, distinct_scans = (
                self._full_at_round_clocks(
                    root, source, [racy, settled, settled],
                    between_rounds=republish_in_the_same_tick,
                    frozen=[mock.patch.object(
                        audit, "_sqlite_family_identity",
                        side_effect=lambda path: (
                            identity if Path(path) == corpus else real_identity(path)))]))
        self.assertEqual(distinct_scans, 2, payload)
        self.assertIsNotNone(indexed, payload)
        self.assertEqual(indexed["agents"], ["crush"], payload)
        self.assertEqual(rc, 1, payload)

    @unittest.skipIf(audit.fileops.WIN, "Windows identities carry a change token; no racy window")
    def test_settle_rounds_judge_an_indexed_memo_on_their_own_clock(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = self._settle_fixture(root, "{}\n")
            identity = audit._sqlite_family_identity(root / "corpus.db")
            settled = self._settled_clock(root)
            racy = identity[0][1][3] - 1
            # The wall clock steps back after round 0, so its memo is racy to the later rounds.
            rc, payload, _files, indexed, _census, distinct_scans = (
                self._full_at_round_clocks(root, source, [settled, racy, racy]))
        self.assertEqual(distinct_scans, 2, payload)
        self.assertIsNone(indexed, payload)
        self.assertEqual(rc, 1, payload)

    def test_a_corpus_republished_every_round_is_rescanned_once(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "rollout.jsonl"
            source.write_text("{}\n", encoding="utf-8")
            _age(source, 3)
            book_path = root / "intake_stats.json"
            _write_book(book_path, {str(source): _entry("codex", source)}, hours_old=1)
            corpus = root / "corpus.db"
            _corpus(corpus, ("codex",))
            real_stat = audit._stat_evidence
            aside = root / "replaced"
            aside.mkdir()

            def republish_corpus(path):
                # The replaced copy stays linked: a freed inode reused within one coarse tick
                # would repeat the identity and hide the move.
                os.link(corpus, aside / f"corpus{len(list(aside.iterdir()))}.db")
                replacement = root / "corpus.tmp"
                replacement.write_bytes(corpus.read_bytes())
                os.replace(replacement, corpus)
                return real_stat(path)

            # Every republish lands inside the racy window, as Linux's coarse ctime makes it.
            with mock.patch.object(audit, "_stat_evidence", side_effect=republish_corpus), \
                    mock.patch.object(
                        audit.time, "time_ns",
                        side_effect=lambda: corpus.stat().st_mtime_ns - 1), \
                    mock.patch.object(
                        audit.events, "open_sqlite_snapshot",
                        wraps=audit.events.open_sqlite_snapshot) as distinct:
                rc, payload, stderr = self._run(root, [("codex", str(source))], ["--full"])
        self.assertEqual(rc, 1, payload)
        self.assertEqual(payload["problems"], [])
        self.assertEqual(stderr.count("evidence moved during round"), audit._LIVE_SETTLE_ROUNDS)
        self.assertEqual(distinct.call_count, 2)
        self.assertTrue(any(
            "indexed rows were republished" in item
            for item in payload["live_details"]), payload)


class LiveMachineCliTests(unittest.TestCase):
    """The owner's observation, end to end: a writer and an indexer run during the audit."""

    @classmethod
    def setUpClass(cls) -> None:
        env_binary = os.environ.get("AGREP_RS_BIN")
        binary = Path(env_binary) if env_binary else ROOT / "target" / "release" / (
            "agrep-rs.exe" if os.name == "nt" else "agrep-rs")
        if not binary.is_file():
            raise unittest.SkipTest("release agrep-rs is not available")
        cls.binary = binary

    def _env(self, root: Path) -> dict[str, str]:
        env = {
            key: value for key, value in os.environ.items()
            if not key.startswith("AGREP_")}
        env.update({
            "HOME": str(root / "home"), "USERPROFILE": str(root / "home"),
            "AGREP_HOME": str(root / "home"),
            "AGREP_DATA_DIR": str(root / "data"),
            "AGREP_DATA_DIR_SOURCE": "env",
            "AGREP_NO_DAEMON": "1", "AGREP_NO_FETCH": "1",
            "AGREP_RS_BIN": os.fspath(self.binary),
            "PYTHONNOUSERSITE": "1", "RAYON_NUM_THREADS": "2",
        })
        return env

    def _cli(self, root: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ROOT / "cli.py"), *args], cwd=str(ROOT),
            env=self._env(root), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=600, check=False)

    def _routine_audit(self, root: Path, *, stall_s: float = 0.0,
                       widen: bool = True) -> subprocess.CompletedProcess[str]:
        """A routine audit in the sandbox whose budgets cannot expire on a slow host.

        The routine tier observes under sub-second deadlines that a loaded box
        can overrun; what these tests measure is classification, so the child
        widens them. ``stall_s`` delays the store snapshot to prove that."""
        budget = 60.0 if widen else audit._ROUTINE_BUDGET_S
        store = 60.0 if widen else audit._ROUTINE_STORE_TIMEOUT_S
        query = 60.0 if widen else audit._ROUTINE_INDEXED_QUERY_BUDGET_S
        script = (
            "import time, audit\n"
            f"audit._ROUTINE_BUDGET_S = {budget!r}\n"
            f"audit._ROUTINE_STORE_TIMEOUT_S = {store!r}\n"
            f"audit._ROUTINE_INDEXED_QUERY_BUDGET_S = {query!r}\n"
            "real_snapshot = audit._combined_store_snapshot\n"
            "def stalled_snapshot(**kwargs):\n"
            f"    time.sleep({stall_s!r})\n"
            "    return real_snapshot(**kwargs)\n"
            "audit._combined_store_snapshot = stalled_snapshot\n"
            "raise SystemExit(audit.main(['--json']))\n"
        )
        env = self._env(root)
        env["PYTHONPATH"] = os.pathsep.join((str(ROOT / "py"), str(ROOT)))
        return subprocess.run(
            [sys.executable, "-c", script], cwd=str(ROOT), env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=600, check=False)

    @staticmethod
    def _rows(start: int, count: int, session: str) -> str:
        rows = []
        for index in range(start, start + count):
            rows.append({
                "type": "user", "userType": "external", "sessionId": session,
                "timestamp": f"2026-07-25T12:{index % 60:02d}:00.000Z",
                "message": {"role": "user", "content": f"probe {index} lantern"}})
            rows.append({
                "type": "assistant", "sessionId": session,
                "timestamp": f"2026-07-25T12:{index % 60:02d}:01.000Z",
                "message": {"role": "assistant", "model": "claude-fixture",
                            "content": [{"type": "text", "text": f"reply {index}"}]}})
        return "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows)

    @staticmethod
    def _wait_for_next_mtime_millisecond(stamp_path: Path, probe_dir: Path) -> None:
        """The book's clock is millisecond mtimes: a write in the same millisecond as the
        last pass is not newer than it. Probe the filesystem clock, not the wall clock."""
        stamp = stamp_path.stat().st_mtime_ns // 1_000_000
        probe = probe_dir / "mtime-probe"
        deadline = time.monotonic() + 5.0
        try:
            while True:
                probe.write_bytes(b"tick")
                if probe.stat().st_mtime_ns // 1_000_000 > stamp:
                    return
                if time.monotonic() > deadline:
                    raise AssertionError(f"mtime never advanced past {stamp_path} within 5 s")
                time.sleep(0.001)
        finally:
            probe.unlink(missing_ok=True)

    def _written_after_the_pass(self, root: Path) -> tuple[Path, Path, str]:
        """Index two sessions, certify them clean, then append to one after the pass."""
        project = root / "home" / ".claude" / "projects" / "-work-fixture"
        project.mkdir(parents=True)
        (root / "data").mkdir()
        live_session = "11111111-1111-4111-8111-111111111111"
        quiet_session = "22222222-2222-4222-8222-222222222222"
        live_source = project / f"{live_session}.jsonl"
        quiet_source = project / f"{quiet_session}.jsonl"
        live_source.write_text(self._rows(0, 20, live_session), encoding="utf-8")
        quiet_source.write_text(self._rows(0, 5, quiet_session), encoding="utf-8")
        build = self._cli(root, "index")
        self.assertEqual(build.returncode, 0, build.stderr[-600:])
        quiet = self._cli(root, "audit", "--full", "--json")
        self.assertEqual(quiet.returncode, 0, quiet.stdout + quiet.stderr[-600:])
        self._wait_for_next_mtime_millisecond(root / "data" / "intake_stats.json", root)
        with live_source.open("a", encoding="utf-8") as stream:
            stream.write(self._rows(500, 2, live_session))
        return live_source, quiet_source, live_session

    def _assert_only_the_written_source_is_live(
            self, result: subprocess.CompletedProcess[str],
            live_source: Path, quiet_source: Path) -> None:
        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 1, payload)
        self.assertEqual(payload["problems"], [], payload)
        self.assertEqual(payload["gaps"], 0, payload)
        self.assertTrue(payload["routine"]["complete"], payload)
        self.assertEqual(payload["live"], 1, payload)
        self.assertIn(str(live_source), payload["live_details"][0])
        self.assertNotIn(str(quiet_source), payload["live_details"][0])
        self.assertEqual(payload["agents"]["claude"]["live"], 1, payload)

    def test_full_and_routine_audits_warn_but_never_error_while_agents_write(self):
        with tempfile.TemporaryDirectory(prefix="agrep-audit-live-") as raw:
            root = Path(raw)
            live_source, quiet_source, live_session = self._written_after_the_pass(root)

            # Phase 1, deterministic: the agent wrote after the last pass and nothing
            # moves while the audits run, so the live item is exactly that source.
            # The routine run's budgets are widened: its verdict is classification.
            behind = {
                "full": self._cli(root, "audit", "--full", "--json"),
                "routine": self._routine_audit(root),
            }

            # Phase 2: a writer and an indexer run during the audits. What the audit
            # sees move depends on scheduling; the contract does not.
            stop = threading.Event()
            index_results = []

            def writer():
                index = 1000
                with live_source.open("a", encoding="utf-8") as stream:
                    while not stop.is_set():
                        stream.write(self._rows(index, 1, live_session))
                        stream.flush()
                        index += 1
                        time.sleep(0.05)

            def indexer():
                while not stop.is_set():
                    index_results.append(self._cli(root, "index").returncode)
                    time.sleep(0.5)

            threads = [threading.Thread(target=writer, daemon=True),
                       threading.Thread(target=indexer, daemon=True)]
            for thread in threads:
                thread.start()
            try:
                racing = {
                    "full": self._cli(root, "audit", "--full", "--json"),
                    "routine": self._cli(root, "audit", "--json"),
                }
            finally:
                stop.set()
                for thread in threads:
                    thread.join(timeout=600)

        for label, result in behind.items():
            with self.subTest(phase="written after the pass", label=label):
                self._assert_only_the_written_source_is_live(
                    result, live_source, quiet_source)
        for label, result in racing.items():
            with self.subTest(phase="writer and indexer racing", label=label):
                payload = json.loads(result.stdout)
                self.assertNotEqual(result.returncode, 2, payload)
                self.assertEqual(payload["problems"], [], payload)
                self.assertNotIn(str(quiet_source), "\n".join(payload["live_details"]))
                # A republished book or corpus is a valid live item on its own; any
                # source-level item must name the file the writer is appending to.
                source_items = [
                    item for item in payload["live_details"] if item.startswith("claude: ")]
                for item in source_items:
                    self.assertIn(str(live_source), item)
        self.assertTrue(index_results and all(code == 0 for code in index_results),
                        index_results)

    def test_routine_classification_survives_a_slow_store_snapshot(self):
        # A store snapshot slower than the whole production routine budget still
        # yields the classification verdict, not a deferral gap, in this suite.
        with tempfile.TemporaryDirectory(prefix="agrep-audit-slow-") as raw:
            root = Path(raw)
            live_source, quiet_source, _session = self._written_after_the_pass(root)
            stall_s = audit._ROUTINE_BUDGET_S + 0.5
            stalled = self._routine_audit(root, stall_s=stall_s)
            budgeted = self._routine_audit(root, stall_s=stall_s, widen=False)
        self._assert_only_the_written_source_is_live(stalled, live_source, quiet_source)
        budgeted_payload = json.loads(budgeted.stdout)
        self.assertFalse(budgeted_payload["routine"]["complete"], budgeted_payload)
        self.assertGreaterEqual(budgeted_payload["gaps"], 1, budgeted_payload)

    def test_a_store_root_moved_away_after_the_last_pass_is_not_pruned(self):
        with tempfile.TemporaryDirectory(prefix="agrep-audit-root-") as raw:
            root = Path(raw)
            claude = root / "home" / ".claude"
            project = claude / "projects" / "-work-fixture"
            project.mkdir(parents=True)
            (root / "data").mkdir()
            sessions = [f"{index:08d}-1111-4111-8111-111111111111" for index in range(3)]
            for session in sessions:
                (project / f"{session}.jsonl").write_text(
                    self._rows(0, 3, session), encoding="utf-8")
            build = self._cli(root, "index")
            self.assertEqual(build.returncode, 0, build.stderr[-600:])
            book_path = root / "data" / "intake_stats.json"
            tallied = set(json.loads(book_path.read_text(encoding="utf-8"))["files"])
            self.assertEqual(len(tallied), 3)

            os.rename(claude, root / "home" / ".claude-away")
            gone = self._cli(root, "audit", "--full", "--json")
            gone_payload = json.loads(gone.stdout)
            self.assertNotEqual(gone.returncode, 2, gone_payload)
            self.assertEqual(gone_payload["orphaned_tallies"], 3)
            self.assertEqual(gone_payload["orphaned_tallies_pruned"], 0)
            self.assertEqual(
                set(json.loads(book_path.read_text(encoding="utf-8"))["files"]), tallied)

            os.rename(root / "home" / ".claude-away", claude)
            back = self._cli(root, "index")
            self.assertEqual(back.returncode, 0, back.stderr[-600:])
            self.assertIn("unchanged since last index", back.stdout)
            healed = self._cli(root, "audit", "--json")
            healed_payload = json.loads(healed.stdout)
            self.assertEqual(healed.returncode, 0, healed_payload)
            self.assertEqual(healed_payload["status"], "clean")
            self.assertEqual(healed_payload["gaps"], 0)

            # A session deleted before a pass is walked as absent; that pass's published
            # inventory omits it, so the next full audit prunes exactly that tally.
            deleted = project / f"{sessions[0]}.jsonl"
            deleted.unlink()
            unpruned = self._cli(root, "audit", "--full", "--json")
            unpruned_payload = json.loads(unpruned.stdout)
            self.assertEqual(unpruned_payload["orphaned_tallies"], 1, unpruned_payload)
            self.assertEqual(unpruned_payload["orphaned_tallies_pruned"], 0, unpruned_payload)
            observed = self._cli(root, "index")
            self.assertEqual(observed.returncode, 0, observed.stderr[-600:])
            pruned = self._cli(root, "audit", "--full", "--json")
            pruned_payload = json.loads(pruned.stdout)
            remaining = set(json.loads(book_path.read_text(encoding="utf-8"))["files"])
        self.assertEqual(pruned.returncode, 0, pruned_payload)
        self.assertEqual(pruned_payload["orphaned_tallies"], 1, pruned_payload)
        self.assertEqual(pruned_payload["orphaned_tallies_pruned"], 1, pruned_payload)
        # The remaining sessions, walked by the real pass, vouched for the encoding.
        self.assertIsNone(pruned_payload["orphaned_tallies_note"], pruned_payload)
        self.assertEqual(remaining, tallied - {str(deleted)})


if __name__ == "__main__":
    unittest.main()
