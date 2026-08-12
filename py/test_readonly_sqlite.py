from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
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


if __name__ == "__main__":
    unittest.main()
