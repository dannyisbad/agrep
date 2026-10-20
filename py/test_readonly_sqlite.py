from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from _test_support import isolate_data_dir


isolate_data_dir()
import audit  # noqa: E402
import events  # noqa: E402


class ReadonlySqliteTests(unittest.TestCase):
    @staticmethod
    def _store(
            events_dir: Path, rows: list[tuple[str, str, str, bytes]]) -> None:
        events_dir.mkdir()
        hashes = [
            events._event_payload_hash(payload)
            for _name, _agent, _session, payload in rows
        ]
        manifest = json.dumps(
            {
                name: value
                for (name, *_rest), value in zip(rows, hashes)
            },
            separators=(",", ":"), sort_keys=True,
        ).encode()
        connection = sqlite3.connect(events_dir / events.EVENT_STORE_NAME)
        connection.executescript(
            "CREATE TABLE event_sessions ("
            "name TEXT PRIMARY KEY, agent TEXT NOT NULL, "
            "session TEXT NOT NULL, hash INTEGER NOT NULL, "
            "n_events INTEGER NOT NULL, payload BLOB NOT NULL, "
            "digest BLOB NOT NULL, stats BLOB NOT NULL) WITHOUT ROWID;"
            "CREATE TABLE event_meta ("
            "key TEXT PRIMARY KEY, value BLOB NOT NULL) WITHOUT ROWID;"
        )
        connection.executemany(
            "INSERT INTO event_sessions VALUES(?,?,?,?,?,?,?,?)",
            [
                (
                    name, agent, session,
                    value if value < 1 << 63 else value - (1 << 64),
                    payload.count(b"\n"), payload,
                    events._event_payload_digest(payload),
                    b'{"agent":"fixture"}',
                )
                for (name, agent, session, payload), value
                in zip(rows, hashes)
            ],
        )
        connection.execute(
            "INSERT INTO event_meta VALUES('manifest',?)", (manifest,))
        connection.execute(
            "INSERT INTO event_meta VALUES('generation',?)",
            (events._event_generation_token(manifest),),
        )
        connection.commit()
        connection.close()
        (events_dir / events.EVENT_GENERATION_NAME).write_bytes(
            events._event_generation_token(manifest))

    def test_protected_wal_event_read_uses_no_live_shm_or_source_alias(
            self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="agrep-events-wal-") as raw:
            root = Path(raw)
            seed_dir = root / "seed"
            protected = root / "protected"
            events_dir = protected / "events"
            seed_name = events.event_filename("codex", "seed")
            seed_payload = b'{"ts":1,"kind":"tool","name":"seed"}\n'
            self._store(seed_dir, [
                (seed_name, "codex", "seed", seed_payload),
            ])
            seed_store = seed_dir / events.EVENT_STORE_NAME
            writer = sqlite3.connect(seed_store)
            try:
                self.assertEqual(
                    writer.execute("PRAGMA journal_mode=WAL").fetchone()[0],
                    "wal")
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                hidden_name = events.event_filename("codex", "hidden")
                hidden_payload = b'{"ts":2,"kind":"tool","name":"hidden"}\n'
                hashes = {
                    seed_name: events._event_payload_hash(seed_payload),
                    hidden_name: events._event_payload_hash(hidden_payload),
                }
                manifest = json.dumps(
                    hashes, separators=(",", ":"), sort_keys=True).encode()
                hidden_hash = hashes[hidden_name]
                writer.execute(
                    "INSERT INTO event_sessions VALUES(?,?,?,?,?,?,?,?)",
                    (
                        hidden_name, "codex", "hidden",
                        (hidden_hash if hidden_hash < 1 << 63
                         else hidden_hash - (1 << 64)),
                        hidden_payload.count(b"\n"), hidden_payload,
                        events._event_payload_digest(hidden_payload),
                        b'{"agent":"codex"}',
                    ),
                )
                writer.execute(
                    "UPDATE event_meta SET value=? WHERE key='manifest'",
                    (manifest,))
                generation = events._event_generation_token(manifest)
                writer.execute(
                    "UPDATE event_meta SET value=? WHERE key='generation'",
                    (generation,))
                writer.commit()
                seed_wal = Path(f"{seed_store}-wal")
                self.assertTrue(seed_wal.exists())
                events_dir.mkdir(parents=True)
                store = events_dir / events.EVENT_STORE_NAME
                shutil.copyfile(seed_store, store)
                shutil.copyfile(seed_wal, Path(f"{store}-wal"))
                (events_dir / events.EVENT_GENERATION_NAME).write_bytes(
                    generation)
                self.assertFalse(Path(f"{store}-shm").exists())

                def snapshot() -> tuple:
                    entries = tuple(
                        (
                            path.name, path.read_bytes(), path.stat().st_mode,
                            path.stat().st_size, path.stat().st_mtime_ns,
                            path.stat().st_ctime_ns, path.stat().st_nlink,
                        )
                        for path in sorted(events_dir.iterdir())
                    )
                    found = events_dir.stat()
                    return (
                        found.st_mode, found.st_mtime_ns, found.st_ctime_ns,
                        entries,
                    )

                before = snapshot()
                with (
                    mock.patch.object(events, "EVENTS_DIR", events_dir),
                    mock.patch.dict(
                        os.environ,
                        {"AGREP_DATA_READONLY": os.fspath(protected)},
                        clear=False),
                ):
                    names = events.event_names(events_dir)
                    payload = events.event_blob("codex", "hidden")
                    events._close_event_reader()
                self.assertIn(hidden_name, names)
                self.assertEqual(payload, hidden_payload)
                self.assertEqual(snapshot(), before)
                self.assertFalse(Path(f"{store}-shm").exists())
            finally:
                writer.close()

    def test_protected_audit_wal_read_never_hardlinks_or_mutates_source(
            self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="agrep-audit-wal-") as raw:
            root = Path(raw)
            path = root / "corpus.db"
            writer = sqlite3.connect(path)
            try:
                self.assertEqual(
                    writer.execute("PRAGMA journal_mode=WAL").fetchone()[0],
                    "wal")
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute("CREATE TABLE msgs(agent TEXT)")
                writer.commit()
                writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                writer.execute("INSERT INTO msgs VALUES('hidden-agent')")
                writer.commit()

                def snapshot() -> tuple:
                    entries = tuple(
                        (
                            item.name, item.read_bytes(), item.stat().st_mode,
                            item.stat().st_size, item.stat().st_mtime_ns,
                            item.stat().st_ctime_ns, item.stat().st_nlink,
                        )
                        for item in sorted(root.iterdir())
                    )
                    found = root.stat()
                    return (
                        found.st_mode, found.st_mtime_ns, found.st_ctime_ns,
                        entries,
                    )

                before = snapshot()
                with (
                    mock.patch.object(audit.common, "DATA_DIR", root),
                    mock.patch.dict(
                        os.environ,
                        {"AGREP_DATA_READONLY": os.fspath(root)},
                        clear=False),
                ):
                    self.assertEqual(
                        audit._indexed_agents(), {"hidden-agent"})
                self.assertEqual(snapshot(), before)
            finally:
                writer.close()


class SnapshotOrphanReclaimTests(unittest.TestCase):
    """A killed reader's snapshot copy is the next reader's to reap.

    Snapshot dirs are pid-stamped so a later process can prove the owner is
    dead; the sweep leaves live owners and fresh legacy dirs alone. This is
    the 35 GB-of-temp incident pinned: nothing may depend on a process dying
    politely to reclaim its copies.
    """

    def _sweep_into(self, fake_temp: Path) -> None:
        with mock.patch.object(
                events.tempfile, "gettempdir", return_value=str(fake_temp)):
            events._SNAPSHOT_SWEEP_DONE = False
            try:
                events._sweep_dead_snapshot_dirs()
            finally:
                events._SNAPSHOT_SWEEP_DONE = False

    def test_dead_owner_snapshot_dirs_are_reaped(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fake_temp = Path(raw)
            prefix = events._SNAPSHOT_DIR_PREFIX
            import proc
            dead_pid = 4194000
            while proc.pid_alive(dead_pid):
                dead_pid -= 7
            dead = fake_temp / f"{prefix}{dead_pid}-abc123"
            alive = fake_temp / f"{prefix}{os.getpid()}-def456"
            stale_legacy = fake_temp / f"{prefix}ghi789"
            fresh_legacy = fake_temp / f"{prefix}jkl012"
            for directory in (dead, alive, stale_legacy, fresh_legacy):
                directory.mkdir()
                (directory / ".store.sqlite3").write_bytes(b"x" * 8)
            old = events.time.time() - events._SNAPSHOT_LEGACY_REAP_AGE_S - 60
            os.utime(stale_legacy, (old, old))
            unrelated = fake_temp / "other-tool-tmp"
            unrelated.mkdir()

            self._sweep_into(fake_temp)

            self.assertFalse(dead.exists(), "dead owner's snapshot survived")
            if os.name == "nt":
                # Locks make a live legacy reader's files undeletable, so an
                # aged ownerless dir is safe to attempt only on Windows.
                self.assertFalse(
                    stale_legacy.exists(), "aged ownerless snapshot survived")
            else:
                self.assertTrue(
                    stale_legacy.exists(),
                    "an ownerless snapshot was taken under a possibly live "
                    "POSIX reader")
            self.assertTrue(alive.exists(), "a live owner's snapshot was taken")
            self.assertTrue(
                fresh_legacy.exists(),
                "a fresh ownerless snapshot was taken from a possible live reader")
            self.assertTrue(unrelated.exists())

    def test_wal_snapshot_dir_names_its_owner_and_dies_with_the_reader(
            self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = root / ".store.sqlite3"
            writer = sqlite3.connect(store)
            try:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute("CREATE TABLE t (v TEXT)")
                writer.execute("INSERT INTO t VALUES ('row')")
                writer.commit()
                fake_temp = root / "faketemp"
                fake_temp.mkdir()
                with mock.patch.object(
                        events.tempfile, "gettempdir",
                        return_value=str(fake_temp)):
                    events._SNAPSHOT_SWEEP_DONE = False
                    connection = events.open_sqlite_snapshot(store)
                    try:
                        created = [
                            entry.name for entry in fake_temp.iterdir()]
                        self.assertEqual(len(created), 1, created)
                        self.assertTrue(created[0].startswith(
                            f"{events._SNAPSHOT_DIR_PREFIX}{os.getpid()}-"),
                            created[0])
                        self.assertEqual(
                            connection.execute(
                                "SELECT v FROM t").fetchone(), ("row",))
                    finally:
                        connection.close()
                    self.assertEqual(list(fake_temp.iterdir()), [],
                                     "closing the reader must remove its copy")
            finally:
                writer.close()
                events._SNAPSHOT_SWEEP_DONE = False


def _copied_bytes(events_module, fn):
    """Run `fn` with the reader's copy counted; returns (result, bytes copied)."""
    copied = 0

    def counting(src, dst, length=16 * 1024):
        nonlocal copied
        while True:
            chunk = src.read(length)
            if not chunk:
                break
            dst.write(chunk)
            copied += len(chunk)
    with mock.patch.object(events_module.shutil, "copyfileobj", counting):
        result = fn()
    return result, copied


class PublishedStoreReadsDirectlyTests(unittest.TestCase):
    """A publication is a checkpointed DELETE-mode store, so every reader opens it
    in place and copies nothing; a store still mid-publication (WAL with frames)
    is snapshotted, consistently, exactly as before. The copy was costing every
    `around`/`summary`/`recall` process the whole store at real-history scale.
    """

    @staticmethod
    def _rows():
        return [("codex-s1.jsonl", "codex", "s1", b'{"kind":"tool"}\n')]

    def test_a_delete_mode_publication_opens_in_place_with_no_copy(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            events_dir = Path(raw) / "events"
            ReadonlySqliteTests._store(events_dir, self._rows())
            store = events_dir / events.EVENT_STORE_NAME
            self.assertEqual(store.read_bytes()[18:20], b"\x01\x01")
            fake_temp = Path(raw) / "faketemp"
            fake_temp.mkdir()
            with mock.patch.object(events.tempfile, "gettempdir",
                                   return_value=str(fake_temp)):
                events._SNAPSHOT_SWEEP_DONE = False
                connection, copied = _copied_bytes(
                    events, lambda: events.open_sqlite_snapshot(store))
                try:
                    self.assertEqual(copied, 0)
                    self.assertEqual(list(fake_temp.iterdir()), [])
                    self.assertEqual(
                        connection.execute(
                            "SELECT name FROM event_sessions").fetchone(),
                        ("codex-s1.jsonl",))
                    self.assertIsNone(connection._snapshot_cleanup)
                finally:
                    connection.close()
                    events._SNAPSHOT_SWEEP_DONE = False
            # a sandboxed reader (data dir read-only, -shm uncreatable) still reads
            os.chmod(events_dir, 0o555)
            try:
                if os.access(events_dir, os.W_OK):
                    self.skipTest("mode bits ignored (privileged runner)")
                with mock.patch.object(events.tempfile, "gettempdir",
                                       return_value=str(fake_temp)):
                    events._SNAPSHOT_SWEEP_DONE = False
                    connection, copied = _copied_bytes(
                        events, lambda: events.open_sqlite_snapshot(store))
                    try:
                        self.assertEqual(copied, 0)
                        self.assertEqual(
                            connection.execute(
                                "SELECT count(*) FROM event_sessions").fetchone(),
                            (1,))
                    finally:
                        connection.close()
                        events._SNAPSHOT_SWEEP_DONE = False
            finally:
                os.chmod(events_dir, 0o755)

    def test_a_store_mid_publication_is_snapshotted_consistently(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            events_dir = Path(raw) / "events"
            ReadonlySqliteTests._store(events_dir, self._rows())
            store = events_dir / events.EVENT_STORE_NAME
            writer = sqlite3.connect(store)
            try:
                # the writer's WAL transaction: frames in the WAL, main file stale
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute(
                    "UPDATE event_sessions SET payload=? WHERE name=?",
                    (b'{"kind":"tool","n":2}\n', "codex-s1.jsonl"))
                writer.commit()
                self.assertGreater((Path(f"{store}-wal")).stat().st_size, 0)
                fake_temp = Path(raw) / "faketemp"
                fake_temp.mkdir()
                with mock.patch.object(events.tempfile, "gettempdir",
                                       return_value=str(fake_temp)):
                    events._SNAPSHOT_SWEEP_DONE = False
                    connection, copied = _copied_bytes(
                        events, lambda: events.open_sqlite_snapshot(store))
                    try:
                        self.assertGreater(copied, store.stat().st_size)
                        self.assertEqual(len(list(fake_temp.iterdir())), 1)
                        self.assertEqual(
                            connection.execute(
                                "SELECT payload FROM event_sessions").fetchone(),
                            (b'{"kind":"tool","n":2}\n',),
                            "the snapshot must include the committed WAL frames")
                    finally:
                        connection.close()
                        events._SNAPSHOT_SWEEP_DONE = False
                    self.assertEqual(list(fake_temp.iterdir()), [])
            finally:
                writer.close()

    @unittest.skipUnless(
        os.environ.get("AGREP_RS_BIN") and Path(os.environ["AGREP_RS_BIN"]).is_file(),
        "needs the built ingest binary (AGREP_RS_BIN)")
    def test_the_ingest_publishes_a_store_every_reader_opens_in_place(self) -> None:
        import subprocess

        fixture_home = Path(__file__).resolve().parents[1] / "crates" / "agrep-cli" \
            / "tests" / "fixtures" / "claude" / "home"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "home"
            shutil.copytree(fixture_home, home, symlinks=True)
            data = root / "data"
            data.mkdir()
            env = {
                "HOME": os.fspath(home), "PATH": "/usr/bin:/bin",
                "TMPDIR": os.fspath(root), "AGREP_HOME": os.fspath(home),
                "AGREP_DATA_DIR": os.fspath(data),
            }
            for full in (True, False):
                args = [os.environ["AGREP_RS_BIN"], "index", "--agent", "claude"]
                if full:
                    args.append("--full")
                completed = subprocess.run(
                    args, env=env, capture_output=True, text=True, timeout=300)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                store = data / "events" / events.EVENT_STORE_NAME
                self.assertEqual(
                    store.read_bytes()[18:20], b"\x01\x01",
                    f"full={full}: the published store is not in DELETE journal mode")
                for suffix in ("-wal", "-shm", "-journal"):
                    self.assertFalse(Path(f"{store}{suffix}").exists(), suffix)
                connection, copied = _copied_bytes(
                    events, lambda: events.open_sqlite_snapshot(store))
                try:
                    self.assertEqual(copied, 0, f"full={full}: the reader copied the store")
                    self.assertGreater(
                        connection.execute(
                            "SELECT count(*) FROM event_sessions").fetchone()[0], 0)
                finally:
                    connection.close()


class WriterInProgressReadsTests(unittest.TestCase):
    """A direct reader serves the committed store while a writer holds its rollback
    journal: SQLite's locks fence the pages. Before, any non-empty `-journal` made
    every lookup BLOCKED for the writer's whole transaction (10-17 s for a repair
    at real-history scale), and `around` reported those windows as an exact zero
    tool events. A hot journal - a crashed writer's - is still refused, as
    unavailable rather than absent, and the snapshot path still refuses a journal
    its copy would omit.
    """

    PAYLOAD = b'{"kind":"tool"}\n'

    def _publish(self, raw: str) -> Path:
        events_dir = Path(raw) / "events"
        ReadonlySqliteTests._store(
            events_dir, [(events.event_filename("codex", "s1"),
                          "codex", "s1", self.PAYLOAD)])
        events._close_event_reader()
        self.addCleanup(events._close_event_reader)
        return events_dir
    @contextmanager
    def _temporary_events(self):
        with tempfile.TemporaryDirectory() as raw:
            try:
                yield raw
            finally:
                events._close_event_reader()

    def test_a_held_write_transaction_does_not_blind_direct_readers(self) -> None:
        with self._temporary_events() as raw, \
                mock.patch.object(events, "EVENTS_DIR", Path(raw) / "events"):
            events_dir = self._publish(raw)
            store = events_dir / events.EVENT_STORE_NAME
            self.assertEqual(events.event_blob("codex", "s1"), self.PAYLOAD)
            cached = events._EVENT_READER_LOCAL.state["connection"]
            writer = sqlite3.connect(store, isolation_level=None)
            try:
                writer.execute("BEGIN IMMEDIATE")
                writer.execute(
                    "UPDATE event_sessions SET stats=?",
                    (b'{"agent":"held"}',))
                self.assertGreater(Path(f"{store}-journal").stat().st_size, 0)
                # the cached reader is reused, not closed and refused
                self.assertEqual(events.event_blob("codex", "s1"), self.PAYLOAD)
                self.assertIs(events._EVENT_READER_LOCAL.state["connection"], cached)
                self.assertEqual(
                    events.event_store_blob("codex", "s1"),
                    (events.EVENTS_PRESENT, self.PAYLOAD))
                self.assertTrue(events.event_store_readable())
                # a fresh reader (another process) opens directly, copying nothing
                events._close_event_reader()
                payload, copied = _copied_bytes(
                    events, lambda: events.event_blob("codex", "s1"))
                self.assertEqual((payload, copied), (self.PAYLOAD, 0))
                # bulk and name reads span the held transaction unharmed
                self.assertEqual(
                    list(events.event_blobs_bulk([("codex", "s1")], full=True)),
                    [("codex", "s1", self.PAYLOAD)])
                self.assertEqual(
                    events.event_names(events_dir),
                    {events.event_filename("codex", "s1")})
            finally:
                writer.execute("ROLLBACK")
                writer.close()
            self.assertFalse(Path(f"{store}-journal").exists())
            self.assertEqual(events.event_blob("codex", "s1"), self.PAYLOAD)

    def test_a_writer_arriving_mid_bulk_read_does_not_fail_it(self) -> None:
        with self._temporary_events() as raw, \
                mock.patch.object(events, "EVENTS_DIR", Path(raw) / "events"):
            events_dir = self._publish(raw)
            store = events_dir / events.EVENT_STORE_NAME
            writer = sqlite3.connect(store, isolation_level=None)
            try:
                rows = events.event_blobs_bulk([("codex", "s1")], full=True)
                first = next(rows)
                # the writer starts (journal appears) while the read is open
                writer.execute("BEGIN IMMEDIATE")
                writer.execute(
                    "UPDATE event_sessions SET stats=?",
                    (b'{"agent":"held"}',))
                self.assertEqual([first, *rows], [("codex", "s1", self.PAYLOAD)])
            finally:
                writer.execute("ROLLBACK")
                writer.close()

    def test_a_crashed_writers_hot_journal_reads_as_unavailable_not_absent(
            self) -> None:
        with self._temporary_events() as raw, \
                mock.patch.object(events, "EVENTS_DIR", Path(raw) / "events"):
            events_dir = self._publish(raw)
            store = events_dir / events.EVENT_STORE_NAME
            journal = Path(f"{store}-journal")
            writer = sqlite3.connect(store, isolation_level=None)
            writer.execute("BEGIN IMMEDIATE")
            writer.execute(
                "UPDATE event_sessions SET stats=?",
                (b'{"agent":"hot"}',))
            hot = bytearray(journal.read_bytes())
            hot[:8] = b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7"
            hot[8:12] = (2).to_bytes(4, "big")
            hot = bytes(hot)
            writer.execute("ROLLBACK")
            writer.close()
            # the journal a writer killed mid-transaction leaves, with no lock held
            journal.write_bytes(hot)
            try:
                self.assertEqual(
                    events.event_store_blob("codex", "s1"), (events.EVENTS_UNAVAILABLE, None))
                self.assertFalse(events.event_store_readable())
                self.assertIsNone(events.event_blob("codex", "s1"))
                with self.assertRaises(RuntimeError):
                    list(events.event_blobs_bulk([("codex", "s1")], full=True))
                self.assertTrue(journal.exists(), "a read-only reader must not roll back")
            finally:
                journal.unlink()
            events._close_event_reader()
            self.assertEqual(
                events.event_store_blob("codex", "s1"), (events.EVENTS_PRESENT, self.PAYLOAD))

    def _hot_journal(self, store: Path) -> None:
        journal = Path(f"{store}-journal")
        writer = sqlite3.connect(store, isolation_level=None)
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE event_sessions SET stats=?", (b'{"agent":"hot"}',))
        hot = bytearray(journal.read_bytes())
        hot[:8] = b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7"
        hot[8:12] = (2).to_bytes(4, "big")
        writer.execute("ROLLBACK")
        writer.close()
        journal.write_bytes(bytes(hot))

    def test_a_read_blocked_by_an_interrupted_pass_asks_for_the_next_pass(self) -> None:
        # Only bulk scans asked for repair, so with an idle daemon a killed pass's journal
        # outlived every around, recall, summary and explorer read.
        import index_lock
        import proc
        with tempfile.TemporaryDirectory() as raw, \
                mock.patch.object(events, "EVENTS_DIR", Path(raw) / "events"), \
                mock.patch.object(events, "DATA_DIR", Path(raw)), \
                mock.patch.object(index_lock, "INDEX_LOCK_PATH", Path(raw) / ".index.lock"), \
                mock.patch.object(events, "_EVENT_REPAIR_REQUESTED", False), \
                mock.patch.object(events, "_INTERRUPTED_REPAIR_ATTEMPT", None), \
                mock.patch.object(events, "_INTERRUPTED_REPAIR_INTERVAL_S", 0.0):
            store = self._publish(raw) / events.EVENT_STORE_NAME
            self._hot_journal(store)
            repair = mock.Mock(return_value=False)
            with mock.patch.object(events, "_EVENT_REPAIR_CALLBACK", repair):
                lock = Path(raw) / ".index.lock"
                lock.write_bytes(index_lock.render_record(
                    pid=os.getpid(), start=proc.process_start_identity(os.getpid()),
                    token="a" * 32, label="agrep-rs", time_ms=time.time_ns() // 1_000_000))
                # a live pass's journal ends with it; nothing to ask for
                self.assertEqual(
                    events.event_store_blob("codex", "s1"), (events.EVENTS_UNAVAILABLE, None))
                repair.assert_not_called()
                lock.unlink()
                events._close_event_reader()
                self.assertEqual(
                    events.event_store_blob("codex", "s1"), (events.EVENTS_UNAVAILABLE, None))
                self.assertFalse(events.event_store_readable())
            self.assertGreaterEqual(repair.call_count, 1)
            Path(f"{store}-journal").unlink()
            events._close_event_reader()

    def test_a_summary_over_many_chats_probes_and_kicks_once(self) -> None:
        # Every blocked lookup re-probed the journal and re-ran a declined kick, and the explorer
        # asked a second time: a summary over 5,616 chats ran 11,232 of each.
        import index_lock
        import indexd_runtime
        import proc
        import summary
        chats = [summary._Chat(session=f"s{index}", agent="codex", project="", root="",
                               side=False, first_ts=0, last_ts=0, first_text="")
                 for index in range(200)]
        declined = indexd_runtime.RepairKick(False, "owner-unverifiable")
        real_kick = indexd_runtime.kick_background_repair
        for case, kick in (("daemonless", {"wraps": real_kick}),
                           ("declined", {"return_value": declined})):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw, \
                    mock.patch.object(events, "EVENTS_DIR", Path(raw) / "events"), \
                    mock.patch.object(events, "DATA_DIR", Path(raw)), \
                    mock.patch.object(index_lock, "INDEX_LOCK_PATH", Path(raw) / ".index.lock"), \
                    mock.patch.object(events, "_INTERRUPTED_REPAIR_ATTEMPT", None), \
                    mock.patch.object(events, "_EVENT_REPAIR_CALLBACK",
                                      indexd_runtime._repair_event_store), \
                    mock.patch.dict(os.environ, {"AGREP_NO_DAEMON": "1"}), \
                    mock.patch.object(indexd_runtime, "kick_background_repair", **kick) as kicks, \
                    mock.patch.object(index_lock, "rollback_journal_state",
                                      wraps=index_lock.rollback_journal_state) as probes, \
                    mock.patch.object(index_lock, "open_regular_binary",
                                      wraps=index_lock.open_regular_binary) as opens:
                store = self._publish(raw) / events.EVENT_STORE_NAME
                self._hot_journal(store)

                def summarize() -> tuple[int, int, int]:
                    for chat in chats:
                        chat.events_unavailable = False
                        summary._load_events(chat)
                    self.assertTrue(all(chat.events_unavailable for chat in chats))
                    journal_opens = sum(
                        1 for call in opens.call_args_list
                        if str(call.args[0]).endswith("-journal"))
                    return probes.call_count, kicks.call_count, journal_opens

                lock = Path(raw) / ".index.lock"
                lock.write_bytes(index_lock.render_record(
                    pid=os.getpid(), start=proc.process_start_identity(os.getpid()),
                    token="a" * 32, label="agrep-rs", time_ms=time.time_ns() // 1_000_000))
                # a live pass: at most one look at its lock, never at its journal
                live_probes, live_kicks, live_opens = summarize()
                self.assertLessEqual(live_probes, 1)
                self.assertEqual((live_kicks, live_opens), (0, 0))
                lock.unlink()
                for counter in (probes, kicks, opens):
                    counter.reset_mock()
                with mock.patch.object(events, "_INTERRUPTED_REPAIR_ATTEMPT", None):
                    self.assertEqual(summarize(), (1, 1, 1))
                    # past the interval the same journal is not asked about again; a new one is
                    with mock.patch.object(events, "_INTERRUPTED_REPAIR_INTERVAL_S", 0.0):
                        self.assertEqual(summarize()[:2], (1, 1))
                        journal = Path(f"{store}-journal")
                        journal.write_bytes(journal.read_bytes() + bytes(512))
                        self.assertEqual(summarize()[:2], (2, 2))
                Path(f"{store}-journal").unlink()
                events._close_event_reader()

    def test_the_explorer_installs_the_repair_request_it_needs(self) -> None:
        probe = (
            "import events, explore\n"
            "before = events._EVENT_REPAIR_CALLBACK is None\n"
            "explore._kick_interrupted_pass_repair()\n"
            "print(before, events._EVENT_REPAIR_CALLBACK is None)\n")
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
        result = subprocess.run([sys.executable, "-c", probe], env=env,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual((result.returncode, result.stdout), (0, "True False\n"), result.stderr)

    def test_an_idle_daemon_repairs_an_interrupted_pass(self) -> None:
        import common
        import corpusdb
        with tempfile.TemporaryDirectory() as raw, \
                mock.patch.object(common, "DATA_DIR", Path(raw)), \
                mock.patch.object(common, "INDEX_LOCK_PATH", Path(raw) / ".index.lock"), \
                mock.patch.object(corpusdb, "DB_PATH", Path(raw) / "corpus.db"), \
                mock.patch.object(corpusdb, "search_generation_health",
                                  return_value={"state": "ready"}), \
                mock.patch.object(common, "index_summary", return_value={"sessions": 1}):
            store = self._publish(raw) / events.EVENT_STORE_NAME
            self.assertEqual(corpusdb.derived_repair_plan(), corpusdb.NO_REPAIR)
            journal = Path(f"{store}-journal")
            # an inert event-store journal blocks no reader; any corpus.db journal blocks writers
            journal.write_bytes(bytes(512))
            self.assertEqual(corpusdb.derived_repair_plan(), corpusdb.NO_REPAIR)
            journal.write_bytes(b"\x01" + bytes(511))
            plan = corpusdb.derived_repair_plan()
            self.assertEqual((plan.code, plan.scope), ("interrupted-pass", "publish"))
            journal.unlink()
            Path(f"{raw}/corpus.db-journal").write_bytes(bytes(512))
            self.assertEqual(corpusdb.derived_repair_plan().code, "interrupted-pass")
            self.assertTrue(corpusdb.repairs_itself("interrupted-pass"))

    def test_a_refused_snapshot_names_the_file_it_refused(self) -> None:
        # The audit opens corpus.db through this path, and its refusal named the event store.
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "corpus.db"
            writer = sqlite3.connect(path, isolation_level=None)
            try:
                writer.execute("CREATE TABLE t (v)")
                writer.execute("BEGIN IMMEDIATE")
                writer.execute("INSERT INTO t VALUES (1)")
                self.assertGreater(Path(f"{path}-journal").stat().st_size, 0)
                with self.assertRaisesRegex(
                        OSError, r"^corpus\.db has a live rollback journal$"):
                    events.open_sqlite_snapshot(path)
            finally:
                writer.execute("ROLLBACK")
                writer.close()

    def test_the_snapshot_path_still_refuses_a_journal_its_copy_would_omit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            events_dir = self._publish(raw)
            store = events_dir / events.EVENT_STORE_NAME
            writer = sqlite3.connect(store)
            writer.execute("PRAGMA journal_mode=WAL")
            writer.close()
            self.assertEqual(store.read_bytes()[18:20], b"\x02\x02")
            Path(f"{store}-journal").write_bytes(b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7" + bytes(24))
            with self.assertRaisesRegex(OSError, "live rollback journal"):
                events.open_sqlite_snapshot(store)

    def test_an_idle_cached_reader_is_released_and_reopened(self) -> None:
        with self._temporary_events() as raw, \
                mock.patch.object(events, "EVENTS_DIR", Path(raw) / "events"):
            self._publish(raw)
            self.assertEqual(events.event_blob("codex", "s1"), self.PAYLOAD)
            state = events._EVENT_READER_LOCAL.state
            cached = state["connection"]
            state["used_at"] -= events._EVENT_READER_IDLE_S + 1
            self.assertEqual(events.event_blob("codex", "s1"), self.PAYLOAD)
            self.assertIsNot(events._EVENT_READER_LOCAL.state["connection"], cached)
            with self.assertRaises(sqlite3.ProgrammingError):
                cached.execute("SELECT 1")

    @staticmethod
    def _wait_closed(state: dict, limit_s: float = 5.0) -> bool:
        # Poll the flag, not the handle: using it here would race the reaper's close.
        deadline = time.monotonic() + limit_s
        while not state.get("closed") and time.monotonic() < deadline:
            time.sleep(0.05)
        try:
            state["connection"].execute("SELECT 1")
        except sqlite3.ProgrammingError:
            return True
        return False

    def test_an_idle_cached_reader_closes_without_another_lookup(self) -> None:
        # A long-lived process (explorer server, daemon) used to keep the store open
        # until its next lookup, which on Windows blocks the corrupt-store rebuild.
        with self._temporary_events() as raw, \
                mock.patch.object(events, "EVENTS_DIR", Path(raw) / "events"), \
                mock.patch.object(events, "_EVENT_READER_IDLE_S", 0.2):
            self._publish(raw)
            self.assertEqual(events.event_blob("codex", "s1"), self.PAYLOAD)
            state = events._EVENT_READER_LOCAL.state
            cached = state["connection"]
            self.assertTrue(self._wait_closed(state), "the idle reader stayed open")
            self.assertEqual(events.event_blob("codex", "s1"), self.PAYLOAD)
            self.assertIsNot(events._EVENT_READER_LOCAL.state["connection"], cached)

    def test_the_idle_reaper_never_closes_a_reader_mid_lookup(self) -> None:
        with self._temporary_events() as raw, \
                mock.patch.object(events, "EVENTS_DIR", Path(raw) / "events"), \
                mock.patch.object(events, "_EVENT_READER_IDLE_S", 0.2):
            events_dir = self._publish(raw)
            state = events._event_reader_connection(events_dir / events.EVENT_STORE_NAME)
            time.sleep(0.8)
            try:
                self.assertEqual(
                    state["connection"].execute("SELECT count(*) FROM event_sessions")
                    .fetchone(), (1,))
            finally:
                events._release_event_reader(state)
            self.assertTrue(self._wait_closed(state))

    def test_an_open_racing_a_commit_waits_instead_of_reporting_unavailable(self) -> None:
        # A commit rewrites the main file's pages (about 70 ms for a 25 MB payload); an
        # open whose identity recheck lands inside it used to read as unavailable at once.
        with self._temporary_events() as raw, \
                mock.patch.object(events, "EVENTS_DIR", Path(raw) / "events"):
            self._publish(raw)
            real = events._event_store_stamp
            calls = []

            def moving(store):
                stamp = real(store)
                calls.append(store)
                if len(calls) == 2:
                    (suffix, (modified, *rest)), *others = stamp
                    return ((suffix, (modified + 1, *rest)), *others)
                return stamp

            with mock.patch.object(events, "_event_store_stamp", side_effect=moving):
                self.assertEqual(events.event_blob("codex", "s1"), self.PAYLOAD)
            self.assertGreater(len(calls), 2)

    def test_a_lookup_between_commit_and_marker_waits_for_the_marker(self) -> None:
        # The writer publishes the marker about 10 ms after its commit; a lookup that read
        # the new generation first used to report unavailable after three 1 ms retries.
        import threading

        with self._temporary_events() as raw, \
                mock.patch.object(events, "EVENTS_DIR", Path(raw) / "events"):
            events_dir = self._publish(raw)
            marker = events_dir / events.EVENT_GENERATION_NAME
            published = marker.read_bytes()
            marker.write_bytes(b"0" * len(published))
            staged = events_dir / "marker.staged"
            staged.write_bytes(published)
            # The writer replaces the marker atomically, as here.
            late = threading.Timer(0.02, os.replace, (staged, marker))
            late.start()
            try:
                self.assertEqual(events.event_blob("codex", "s1"), self.PAYLOAD)
            finally:
                late.join()

    def test_a_marker_left_behind_fails_fast_after_one_full_wait(self) -> None:
        # A writer that dies between its commit and its marker left every lookup waiting
        # the whole marker loop (about 0.2 s): `summary` at real scale took 558 s, not 14 s.
        import threading

        with self._temporary_events() as raw, \
                mock.patch.object(events, "EVENTS_DIR", Path(raw) / "events"):
            events_dir = self._publish(raw)
            marker = events_dir / events.EVENT_GENERATION_NAME
            published = marker.read_bytes()
            marker.write_bytes(b"0" * len(published))
            started = time.monotonic()
            for _ in range(20):
                self.assertIsNone(events.event_blob("codex", "s1"))
                self.assertEqual(
                    events.event_store_blob("codex", "s1"), (events.EVENTS_UNAVAILABLE, None))
                self.assertFalse(events.event_store_readable())
            self.assertLess(time.monotonic() - started, 1.0)
            # A writer publishing a new marker re-arms the short commit-to-marker wait.
            staged = events_dir / "marker.staged"
            staged.write_bytes(published)
            marker.write_bytes(b"1" * len(published))
            late = threading.Timer(0.02, os.replace, (staged, marker))
            late.start()
            try:
                self.assertEqual(events.event_blob("codex", "s1"), self.PAYLOAD)
            finally:
                late.join()
            self.assertEqual(
                events.event_store_blob("codex", "s1"), (events.EVENTS_PRESENT, self.PAYLOAD))

    @unittest.skipUnless(hasattr(os, "fork"), "needs os.fork")
    def test_a_fork_while_the_reaper_closes_a_reader_leaves_the_child_usable(self) -> None:
        # A child forked mid-close inherited the reaper's held reader lock and hung forever.
        import signal
        import threading
        import warnings

        with self._temporary_events() as raw, \
                mock.patch.object(events, "EVENTS_DIR", Path(raw) / "events"), \
                mock.patch.object(events, "_EVENT_READER_IDLE_S", 0.2):
            self._publish(raw)
            self.assertEqual(events.event_blob("codex", "s1"), self.PAYLOAD)
            closing = threading.Event()
            real_close = events._EventSnapshotConnection.close

            def slow_close(connection):
                closing.set()
                time.sleep(1.5)
                return real_close(connection)

            with mock.patch.object(events._EventSnapshotConnection, "close", slow_close):
                self.assertTrue(closing.wait(5), "the reaper never retired the reader")
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    pid = os.fork()
                if pid == 0:
                    code = 1
                    try:
                        signal.alarm(5)
                        code = 0 if events.event_blob("codex", "s1") == self.PAYLOAD else 2
                    finally:
                        os._exit(code)
            _pid, status = os.waitpid(pid, 0)
            self.assertTrue(os.WIFEXITED(status), "the child hung on an inherited reader lock")
            self.assertEqual(os.WEXITSTATUS(status), 0)


@unittest.skipUnless(
    os.environ.get("AGREP_RS_BIN") and Path(os.environ["AGREP_RS_BIN"]).is_file(),
    "needs the built ingest binary (AGREP_RS_BIN)")
class FailedEventWriteTests(unittest.TestCase):
    """An index pass whose event write gives up on a reader held past its 30 s deadline
    used to fail after it had already renamed messages.jsonl and the other derived files,
    so `around` exited 2 (the derived proof no longer matched) until the next pass."""

    SESSION = "11111111-1111-4111-8111-111111111111"

    def test_a_failed_event_write_leaves_around_serving(self) -> None:
        import subprocess
        import sys

        root_dir = Path(__file__).resolve().parents[1]
        fixture_home = root_dir / "crates" / "agrep-cli" / "tests" / "fixtures" / "claude" \
            / "home"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "home"
            shutil.copytree(fixture_home, home, symlinks=True)
            data = root / "data"
            data.mkdir()
            env = {
                "HOME": os.fspath(home), "PATH": "/usr/bin:/bin",
                "TMPDIR": os.fspath(root), "AGREP_HOME": os.fspath(home),
                "AGREP_DATA_DIR": os.fspath(data), "AGREP_NO_DAEMON": "1",
                "AGREP_NO_RESIDENT": "1",
                "PYTHONPATH": os.pathsep.join(
                    (os.fspath(root_dir), os.fspath(root_dir / "py"))),
            }

            def run(args: list[str]) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    args, env=env, capture_output=True, text=True, timeout=300, input="")

            def around() -> int:
                served = run([sys.executable, os.fspath(root_dir / "cli.py"), "around",
                              self.SESSION, "1", "--json", "--no-auto"])
                self.assertEqual(served.returncode, 0, served.stderr)
                meta = json.loads(served.stdout.splitlines()[0])["scope"]
                self.assertEqual(meta["counts_state"], "exact")
                return meta["tools"]["available"]

            index = [os.environ["AGREP_RS_BIN"], "index", "--agent", "claude"]
            first = run([*index, "--full"])
            self.assertEqual(first.returncode, 0, first.stderr)
            tools = around()
            transcript = home / ".claude" / "projects" / "proj-alpha" / "sess-claude-0001.jsonl"
            stamp = {"sessionId": self.SESSION, "cwd": "/work/alpha"}
            with transcript.open("a", encoding="utf-8") as sink:
                for line in (
                    {"type": "user", "timestamp": "2026-01-02T10:03:00.000Z",
                     "message": {"role": "user", "content": "now run the whole suite"}},
                    {"type": "assistant", "timestamp": "2026-01-02T10:03:05.000Z",
                     "message": {"role": "assistant", "model": "claude-fable-5", "content": [
                         {"type": "tool_use", "id": "toolu_a9", "name": "Bash",
                          "input": {"command": "cargo test"}}]}},
                    {"type": "user", "timestamp": "2026-01-02T10:03:10.000Z",
                     "message": {"role": "user", "content": [
                         {"type": "tool_result", "tool_use_id": "toolu_a9",
                          "content": "all passed", "is_error": False}]}},
                ):
                    sink.write(json.dumps(stamp | line) + "\n")
            store = data / "events" / events.EVENT_STORE_NAME
            reader = sqlite3.connect(f"file:{store}?mode=ro", uri=True, isolation_level=None)
            try:
                reader.execute("BEGIN")
                reader.execute("SELECT count(*) FROM event_sessions").fetchone()
                held = run(index)
            finally:
                reader.execute("ROLLBACK")
                reader.close()
            self.assertNotEqual(held.returncode, 0, held.stderr)
            self.assertIn("write deadline", held.stderr)
            # The previous generation, still whole: derived files, proof and events agree.
            self.assertEqual(around(), tools)
            recovered = run(index)
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertEqual(around(), tools + 1)


@unittest.skipUnless(
    os.environ.get("AGREP_RS_BIN") and Path(os.environ["AGREP_RS_BIN"]).is_file(),
    "needs the built ingest binary (AGREP_RS_BIN)")
class UnreadableEventStoreSurfacesTests(unittest.TestCase):
    """With the event store unreadable (its marker behind the database, as a writer killed
    between commit and marker leaves it), the JSONL lane's keyword search and recall exited 2
    and a tool-event handle read as stale. Each must serve its prose and say why tools are
    missing, as windows do."""

    PHRASE = "tool events are unavailable right now"

    @classmethod
    def setUpClass(cls) -> None:
        cls.root_dir = Path(__file__).resolve().parents[1]
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        home = root / "home"
        shutil.copytree(
            cls.root_dir / "crates" / "agrep-cli" / "tests" / "fixtures" / "claude" / "home",
            home, symlinks=True)
        data = root / "data"
        data.mkdir()
        cls.env = {
            "HOME": os.fspath(home), "PATH": "/usr/bin:/bin", "TMPDIR": os.fspath(root),
            "AGREP_HOME": os.fspath(home), "AGREP_DATA_DIR": os.fspath(data),
            "AGREP_RS_BIN": os.environ["AGREP_RS_BIN"], "AGREP_NO_DAEMON": "1",
            "AGREP_NO_RESIDENT": "1", "AGREP_CI": "1",
            "PYTHONPATH": os.pathsep.join(
                (os.fspath(cls.root_dir), os.fspath(cls.root_dir / "py"))),
        }
        cls.cwd = home
        indexed = cls._cli("index")
        if indexed.returncode:
            raise AssertionError(indexed.stderr)
        found = cls._cli("search", "cargo", "--json", "--no-auto")
        if found.returncode:
            raise AssertionError(found.stderr)
        tool = next(row for row in map(json.loads, found.stdout.splitlines()[1:])
                    if row.get("who") == "tool")
        cls.handle = tool["handle"]
        (data / "corpus.db").rename(root / "corpus.db.aside")
        marker = data / "events" / events.EVENT_GENERATION_NAME
        marker.write_bytes(b"0" * len(marker.read_bytes()))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    @classmethod
    def _cli(cls, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, os.fspath(cls.root_dir / "cli.py"), *args], env=cls.env,
            cwd=cls.cwd, capture_output=True, text=True, timeout=300, input="")

    def test_jsonl_keyword_search_serves_prose_and_discloses(self) -> None:
        human = self._cli("search", "timer", "--no-auto")
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn("flaky timer test", human.stdout)
        self.assertIn(self.PHRASE, human.stderr)
        machine = self._cli("search", "timer", "--json", "--no-auto")
        self.assertEqual(machine.returncode, 0, machine.stderr)
        head, *rows = map(json.loads, machine.stdout.splitlines())
        self.assertEqual(head["tools_excluded"]["reason"], "events-unavailable")
        self.assertIn(self.PHRASE, head["tools_excluded"]["note"])
        self.assertTrue(rows)
        self.assertTrue(all(row["who"] != "tool" for row in rows))

    def test_an_excluded_tool_lane_claims_no_tool_count(self) -> None:
        # completeness.tool_rows read 0 for rows the scan never read, and the floor notes
        # offered `-c counts exactly` although -c is a floor in this lane too.
        machine = self._cli("search", "timer", "--json", "--no-auto")
        self.assertEqual(machine.returncode, 0, machine.stderr)
        head = json.loads(machine.stdout.splitlines()[0])
        self.assertEqual(head["completeness"]["total_basis"], "floor")
        self.assertNotIn("tool_rows", head["completeness"])
        for args in (("timer",), ("timer", "-l")):
            with self.subTest(args=args):
                human = self._cli("search", *args, "--no-auto")
                self.assertEqual(human.returncode, 0, human.stderr)
                self.assertNotIn("-c counts exactly", human.stderr)
                self.assertNotIn("-c exact", human.stderr)

    def test_jsonl_keyword_recall_serves_prose_and_discloses(self) -> None:
        human = self._cli("recall", "timer", "--no-auto")
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn("flaky timer test", human.stdout)
        self.assertIn(self.PHRASE, human.stderr)
        machine = self._cli("recall", "timer", "--json", "--no-auto")
        self.assertEqual(machine.returncode, 0, machine.stderr)
        self.assertEqual(
            json.loads(machine.stdout)["tools_excluded"]["reason"], "events-unavailable")

    def test_a_tool_handle_serves_its_turn_and_discloses(self) -> None:
        for command in ("around", "recall"):
            with self.subTest(command=command):
                served = self._cli(command, self.handle, "--no-auto")
                self.assertEqual(served.returncode, 0, served.stderr)
                self.assertIn("flaky timer test", served.stdout)
                self.assertIn(self.PHRASE, served.stderr)
                self.assertNotIn("rerun the search", served.stderr)


@unittest.skipUnless(
    os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() != 0,
    "chmod must deny this user")
class InaccessibleSidecarProbeTests(unittest.TestCase):
    """A sidecar an interrupted `sudo agrep` left behind (root's, 0644) or one chmod 000 denies
    is named from its metadata alone: it is never opened, and it is not a killed pass's journal
    or lost ownership."""

    def _family(self, raw: str) -> tuple[Path, Path]:
        db = Path(raw) / "corpus.db"
        writer = sqlite3.connect(db)
        writer.execute("CREATE TABLE t(v)")
        writer.commit()
        writer.close()
        journal = Path(f"{db}-journal")
        journal.write_bytes(b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7" + bytes(504))
        return db, journal

    def test_the_probe_names_it_without_opening_more_than_a_journal_byte(self) -> None:
        import index_lock
        for mode in (0o000, 0o444):
            with self.subTest(mode=oct(mode)), tempfile.TemporaryDirectory() as raw:
                db, journal = self._family(raw)
                journal.chmod(mode)
                try:
                    with mock.patch.object(index_lock, "open_regular_binary",
                                           wraps=index_lock.open_regular_binary) as opened:
                        sidecars = index_lock.inaccessible_sidecars(db, "search index")
                        state = index_lock.rollback_journal_state(db, Path(raw) / ".lock")
                    # an unreadable journal is never opened; a readable one only for its byte
                    self.assertEqual({call.args[0] for call in opened.call_args_list},
                                     set() if mode == 0 else {journal})
                    self.assertEqual(sidecars, [{
                        "store": "search index", "kind": "rollback journal",
                        "path": str(journal), "access": "mode", "owner": None}])
                    self.assertEqual(state, {"state": "inaccessible", "bytes": 512, "hot": False})
                    # another user's file names its owner
                    with mock.patch.object(index_lock.os, "getuid",
                                           return_value=os.getuid() + 1):
                        owned = index_lock.inaccessible_sidecars(db, "search index")
                    self.assertEqual(owned[0]["owner"], index_lock._user_name(os.getuid()))
                    self.assertEqual(owned[0]["access"], "owner")
                finally:
                    journal.chmod(0o600)
                self.assertEqual(index_lock.inaccessible_sidecars(db, "search index"), [])

    def test_shapes_the_writer_cleans_up_itself_never_block(self) -> None:
        # A readable -shm beside a DELETE-mode store (SQLite ignores it) and a readable inert
        # journal (discarded as debris) used to stop every pass.
        import index_lock
        with tempfile.TemporaryDirectory() as raw:
            db, journal = self._family(raw)
            shm = Path(f"{db}-shm")
            shm.write_bytes(bytes(32768))
            shm.chmod(0o444)
            journal.write_bytes(bytes(512))
            journal.chmod(0o444)
            try:
                self.assertEqual(index_lock.inaccessible_sidecars(db, "search index"), [])
                self.assertEqual(index_lock.rollback_journal_state(db, Path(raw) / ".lock"),
                                 {"state": "interrupted-pass", "bytes": 512, "hot": False})
                # a -wal makes the -shm part of the store, and a hot journal needs a rollback
                wal = Path(f"{db}-wal")
                wal.write_bytes(b"")
                journal.chmod(0o600)
                journal.write_bytes(b"\x01" + bytes(511))
                journal.chmod(0o444)
                self.assertEqual(
                    [item["kind"] for item in index_lock.inaccessible_sidecars(db, "x")],
                    ["rollback journal", "shared-memory file"])
                wal.unlink()
                # and so does a WAL-mode header, with or without its -wal
                header = bytearray(db.read_bytes())
                header[18:20] = b"\x02\x02"
                db.write_bytes(bytes(header))
                self.assertIn("shared-memory file",
                              [item["kind"] for item in index_lock.inaccessible_sidecars(db, "x")])
                # an unreadable journal blocks however it starts
                journal.chmod(0o600)
                journal.write_bytes(bytes(512))
                journal.chmod(0)
                self.assertIn("rollback journal",
                              [item["kind"] for item in index_lock.inaccessible_sidecars(db, "x")])
            finally:
                shm.chmod(0o600)
                journal.chmod(0o600)

    def test_each_store_blocks_on_what_its_own_writer_cannot_get_past(self) -> None:
        # The Rust corpus.db ownership probe reads every sidecar's first bytes, so an unreadable
        # -shm there blocks; the event-store writer removes an unused -shm and settles an empty
        # -wal beside a DELETE-mode header, so neither blocks there.
        import index_lock
        with tempfile.TemporaryDirectory() as raw:
            corpus, journal = self._family(raw)
            journal.unlink()
            events_dir = Path(raw) / "events"
            events_dir.mkdir()
            store = events_dir / events.EVENT_STORE_NAME
            shutil.copyfile(corpus, store)
            sidecars = [Path(f"{db}{suffix}") for db in (corpus, store)
                        for suffix in ("-shm", "-wal")]
            for path in sidecars:
                path.write_bytes(bytes(32768) if path.name.endswith("-shm") else b"")
                path.chmod(0)
            try:
                self.assertEqual(
                    [item["kind"] for item in index_lock.inaccessible_sidecars(corpus, "x")],
                    ["write-ahead log", "shared-memory file"])
                self.assertEqual(index_lock.inaccessible_sidecars(store, "x"), [])
                # a non-empty -wal, or a WAL-mode header, makes both part of the store
                wal = Path(f"{store}-wal")
                wal.chmod(0o600)
                wal.write_bytes(b"x" * 32)
                wal.chmod(0)
                self.assertEqual(
                    [item["kind"] for item in index_lock.inaccessible_sidecars(store, "x")],
                    ["write-ahead log", "shared-memory file"])
                wal.chmod(0o600)
                wal.write_bytes(b"")
                wal.chmod(0)
                header = bytearray(store.read_bytes())
                header[18:20] = b"\x02\x02"
                store.write_bytes(bytes(header))
                self.assertEqual(
                    [item["kind"] for item in index_lock.inaccessible_sidecars(store, "x")],
                    ["write-ahead log", "shared-memory file"])
            finally:
                for path in sidecars:
                    path.chmod(0o600)

    def test_a_sidecar_that_cannot_be_opened_never_makes_ownership_uncertain(self) -> None:
        # The writer preflight took every sidecar's identity by opening it, so any unreadable
        # one - blocking or not - read as 'uncertain' ownership and stopped the daemon.
        import corpusdb
        with tempfile.TemporaryDirectory() as raw:
            db, journal = self._family(raw)
            journal.unlink()
            owner = sqlite3.connect(db)
            owner.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
            owner.execute("INSERT INTO meta VALUES('build_id', ?)", ("0" * 20,))
            owner.commit()
            owner.close()
            shm = Path(f"{db}-shm")
            shm.write_bytes(bytes(32768))
            shm.chmod(0)
            try:
                identity = corpusdb._optional_sqlite_identity(shm)
                info = shm.lstat()
                with mock.patch.object(corpusdb.index_lock, "inaccessible_sidecars",
                                       return_value=[]):
                    state, _owner, _reason = corpusdb._database_build_id(path=db)
                stamp = dict(events._event_store_stamp(db))
            finally:
                shm.chmod(0o600)
        self.assertEqual(identity, (info.st_size, info.st_mtime_ns, info.st_ctime_ns,
                                    info.st_dev, info.st_ino))
        self.assertEqual(state, "owned")
        self.assertEqual(stamp["-shm"][2], 32768)

    def test_a_flag_or_acl_denial_gets_a_fix_that_clears_it(self) -> None:
        # `chmod u+rw` fixes neither an immutable flag nor a deny ACL on a file mode 0644 allows.
        import index_lock
        import source_health
        with tempfile.TemporaryDirectory() as raw:
            db, journal = self._family(raw)
            journal.chmod(0o644)
            real_access = os.access
            with mock.patch.object(index_lock.os, "access",
                                   side_effect=lambda path, mode: (
                                       Path(path) != journal and real_access(path, mode))):
                sidecars = index_lock.inaccessible_sidecars(db, "search index")
        self.assertEqual(sidecars[0]["access"], "flags")
        line = source_health.inaccessible_sidecar_line(sidecars[0], "agrep index")
        self.assertIn("is closed to you by a file flag or an access-control list", line)
        self.assertNotIn("chmod u+rw", line)
        expected = ("chflags nouchg" if sys.platform == "darwin"
                    else ("fix its owner or permissions" if os.name == "nt"
                          else "chattr -i"))
        self.assertIn(expected, line)

    def test_the_writer_preflight_calls_it_a_blocked_sidecar_not_an_owner(self) -> None:
        import corpusdb
        with tempfile.TemporaryDirectory() as raw:
            db, journal = self._family(raw)
            journal.chmod(0)
            try:
                state, owner, reason = corpusdb._database_build_id(path=db)
            finally:
                journal.chmod(0o600)
        self.assertEqual((state, owner), ("sidecar-inaccessible", None))
        self.assertIn(f"the search index cannot be updated - its rollback journal at {journal} "
                      "is not readable and writable by you, so no index pass can use it: "
                      "`chmod u+rw ", reason)
        self.assertRegex(reason, r"`, then `[^`]* index`$")


if __name__ == "__main__":
    unittest.main()
