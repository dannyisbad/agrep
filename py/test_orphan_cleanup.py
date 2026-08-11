from pathlib import Path
import os
import re
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

import corpusdb
import doctor


class OrphanCleanupTests(unittest.TestCase):
    def test_doctor_staging_artifacts_match_rust_writer_inventory(self):
        rust = (Path(__file__).parents[1] / "crates" / "agrep-cli" / "src" / "main.rs")
        source = rust.read_text(encoding="utf-8")
        block = re.search(
            r"const ROOT_STAGING_ARTIFACTS: &\[&str\] = &\[(.*?)\];",
            source,
            re.DOTALL,
        )
        self.assertIsNotNone(block)
        expected = frozenset(re.findall(r'"([^"]+)"', block.group(1)))
        self.assertEqual(doctor._RUST_STAGING_ARTIFACTS, expected)

    def test_legacy_purge_reclaims_every_per_attempt_temp_regardless_of_pid(self):
        # Nothing writes the per-attempt shape any more, so a live PID in one of these
        # names is a coincidence, not an owner: keeping it was what leaked GBs.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            dead = root / "corpus.db.41.9.tmp"
            journal = root / "corpus.db.41.9.tmp-journal"
            live_pid = root / f"corpus.db.{os.getpid()}.9.tmp"
            building = root / "corpus.db.building"
            unrelated = root / "corpus.db.backup"
            for path, body in (
                (dead, b"dead"),
                (journal, b"journal"),
                (live_pid, b"live"),
                (building, b"building"),
                (unrelated, b"keep"),
            ):
                path.write_bytes(body)
            with mock.patch.object(corpusdb, "DB_PATH", root / "corpus.db"):
                inventory = corpusdb.orphan_temp_artifacts()
                result = corpusdb.purge_legacy_build_temps()

            self.assertEqual(inventory["count"], 3)
            self.assertEqual(inventory["bytes"], 15)
            self.assertEqual(result["removed"], 3)
            self.assertFalse(dead.exists())
            self.assertFalse(journal.exists())
            self.assertFalse(live_pid.exists())
            # the live build path and unrelated neighbours are not the legacy shape
            self.assertTrue(building.exists())
            self.assertTrue(unrelated.exists())

    def test_the_unique_temp_name_mechanism_is_deleted(self):
        # The leak was structural: a per-attempt name is one no later attempt can
        # reclaim. Pinned gone, so a regression must consciously reintroduce it.
        self.assertFalse(hasattr(corpusdb, "sweep_orphan_temp_artifacts"))
        self.assertFalse(hasattr(corpusdb, "_TMP_DB_RE"))
        first, second = corpusdb._tmp_db_path(), corpusdb._tmp_db_path()
        self.assertEqual(first, second)
        self.assertEqual(first.name, f"{corpusdb.DB_PATH.name}.building")

    def test_a_failed_rebuild_returns_the_data_dir_to_its_prior_footprint(self):
        def footprint(root: Path) -> int:
            return sum(p.stat().st_size for p in root.iterdir() if p.is_file())

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "corpus.db"
            db_path.write_bytes(b"published")
            before = footprint(root)

            def full_disk(dst, expected_stamp=None):
                Path(dst).write_bytes(b"x" * 4096)
                raise sqlite3.OperationalError("database or disk is full")

            with (
                mock.patch.object(corpusdb, "DB_PATH", db_path),
                mock.patch.object(corpusdb, "_build", full_disk),
            ):
                for _ in range(3):
                    with self.assertRaises(sqlite3.OperationalError):
                        corpusdb._rebuild_and_publish("stamp", False)
                    self.assertEqual(footprint(root), before)

                # SIGKILL leaves the temp behind with no unwinding at all; only a
                # well-known name lets the next attempt reclaim those bytes.
                corpusdb._tmp_db_path().write_bytes(b"x" * 8192)
                with self.assertRaises(sqlite3.OperationalError):
                    corpusdb._rebuild_and_publish("stamp", False)
                self.assertEqual(footprint(root), before)

    def test_doctor_inventories_dead_rust_staging_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            dead = root / "messages.jsonl.tmp.51.9"
            journal = root / ".ingest_cache.bin.journal.tmp.53.9.4"
            corpus = root / "corpus.db.tmp.51.9.5"
            corpus_wal = root / "corpus.db.tmp.51.9.5-wal"
            live = root / ".ingest_cache.bin.tmp.52.9.3"
            malformed = root / "messages.jsonl.tmp.51.not-a-clock"
            unrelated = root / "notes.tmp.51.9"
            published = root / "corpus.db"
            published_wal = root / "corpus.db-wal"
            dead.write_bytes(b"dead")
            journal.write_bytes(b"journal")
            corpus.write_bytes(b"private")
            corpus_wal.write_bytes(b"wal")
            live.write_bytes(b"live")
            malformed.write_bytes(b"keep")
            unrelated.write_bytes(b"keep")
            published.write_bytes(b"published")
            published_wal.write_bytes(b"published wal")
            with (
                mock.patch.object(doctor.common, "DATA_DIR", root),
                mock.patch.object(
                    doctor.common, "pid_alive", side_effect=lambda pid: pid == 52),
            ):
                inventory = doctor._rust_staging_orphans()

            self.assertEqual(inventory["count"], 4)
            self.assertEqual(inventory["bytes"], 21)
            self.assertEqual(
                set(inventory["paths"]),
                {dead, journal, corpus, corpus_wal},
            )
            self.assertNotIn(published, inventory["paths"])
            self.assertNotIn(published_wal, inventory["paths"])
            self.assertTrue(unrelated.exists())

    def test_private_corpus_stage_parser_matches_rust_family_grammar(self):
        accepted = {
            "corpus.db.tmp.41.9": 41,
            "corpus.db.tmp.41.9.5": 41,
            "corpus.db.tmp.41.9.5-journal": 41,
            "corpus.db.tmp.41.9.5-wal": 41,
            "corpus.db.tmp.41.9.5-shm": 41,
        }
        for name, pid in accepted.items():
            with self.subTest(name=name):
                self.assertEqual(doctor._rust_staging_owner(name), pid)

        rejected = (
            "corpus.db",
            "corpus.db-wal",
            "corpus.db.tmp.41",
            "corpus.db.tmp.41.9.5-extra",
            "messages.jsonl.tmp.41.9.5-wal",
            "corpus.db.tmp.4294967296.9.5",
        )
        for name in rejected:
            with self.subTest(name=name):
                self.assertIsNone(doctor._rust_staging_owner(name))

    def test_doctor_orphan_totals_preserve_each_artifact_class(self):
        group = lambda count, size: {
            "count": count, "bytes": size, "paths": (),
        }
        with (
            mock.patch.object(
                corpusdb, "orphan_temp_artifacts", return_value=group(2, 20)),
            mock.patch.object(
                doctor, "_rust_staging_orphans", return_value=group(3, 30)),
            mock.patch.object(Path, "is_file", return_value=True),
            mock.patch(
                "embedding_segments.orphan_artifacts", return_value=group(4, 40)),
        ):
            inventory = doctor._orphan_inventory()

        self.assertEqual(inventory["count"], 9)
        self.assertEqual(inventory["bytes"], 90)
        self.assertEqual(inventory["corpus"]["count"], 2)
        self.assertEqual(inventory["rust_staging"]["count"], 3)
        self.assertEqual(inventory["embeddings"]["count"], 4)

    def test_doctor_counts_mature_embedding_generation_without_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            segment_dir = root / "embedding-segments"
            segment_dir.mkdir()
            orphan = segment_dir / "seg-crashed.f32"
            orphan.write_bytes(b"x" * 123)
            old = time.time() - 600
            os.utime(orphan, (old, old))

            with (
                mock.patch.object(
                    doctor.common, "EMBEDDINGS_PATH",
                    root / "embeddings.f32"),
                mock.patch.object(
                    corpusdb, "orphan_temp_artifacts",
                    return_value={"count": 0, "bytes": 0, "paths": ()}),
                mock.patch.object(
                    doctor, "_rust_staging_orphans",
                    return_value={"count": 0, "bytes": 0, "paths": ()}),
            ):
                inventory = doctor._orphan_inventory()

        self.assertEqual(inventory["embeddings"]["count"], 1)
        self.assertEqual(inventory["embeddings"]["bytes"], 123)
        self.assertEqual(inventory["count"], 1)


if __name__ == "__main__":
    unittest.main()
