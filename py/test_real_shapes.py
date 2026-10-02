"""Scrubbed real transcript shapes ingest through the sealed CLI and hold the scale invariants."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bench" / "real_history"))

import invariants  # noqa: E402
import sandbox  # noqa: E402
import shapes  # noqa: E402

FIXTURES = ROOT / "bench" / "fixtures" / "real_shapes"
EXPECTED_ADAPTERS = ("claude", "codex", "pi", "opencode")


class RealShapeSandbox:
    """Fixture stores under a fresh home; only the ingest binary location is inherited."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="agrep-real-shapes-", dir=tempfile.gettempdir()))
        self.home = self.root / "home"
        self.data = self.root / "data"
        self.home.mkdir(mode=0o700)
        self.data.mkdir(mode=0o700)
        self.fixture_files: list[Path] = []
        for adapter_dir in sorted(path for path in FIXTURES.iterdir() if path.is_dir()):
            if adapter_dir.name in sandbox.FIXTURE_HOME_DIRS:
                target = self.home / sandbox.FIXTURE_HOME_DIRS[adapter_dir.name]
                shutil.copytree(adapter_dir, target)
                for source in adapter_dir.rglob("*"):
                    if source.is_file():
                        self.fixture_files.append(target / source.relative_to(adapter_dir))
            elif adapter_dir.name == "opencode":
                db_path = self.home / sandbox.OPENCODE_DB
                db_path.parent.mkdir(parents=True)
                connection = sqlite3.connect(db_path)
                try:
                    for seed in sorted(adapter_dir.glob("seed*.sql")):
                        connection.executescript(seed.read_text(encoding="utf-8"))
                finally:
                    connection.close()
                self.fixture_files.append(db_path)
        (self.data / "settings.json").write_text('{"embeddings":"off"}\n', encoding="utf-8")
        env = sandbox.sealed_env(home=self.home, data=self.data, scratch=self.root,
                                 binary=sandbox.resolve_binary())
        self.runner = sandbox.CliRunner(env, cwd=self.home, default_timeout=300)

    def close(self) -> None:
        try:
            self.runner.reap()
        finally:
            shutil.rmtree(self.root, ignore_errors=True)


class RealShapeIngestTests(unittest.TestCase):
    sandbox: RealShapeSandbox

    @classmethod
    def setUpClass(cls) -> None:
        if not FIXTURES.is_dir() or not (FIXTURES / "manifest.json").is_file():
            raise unittest.SkipTest("bench/fixtures/real_shapes has not been generated")
        binary = sandbox.resolve_binary()
        if not binary.is_file():
            raise AssertionError(f"ingest binary missing: {binary}")
        cls.sandbox = RealShapeSandbox()
        try:
            runner = cls.sandbox.runner
            cold = runner.cli(["index"])
            if cold.returncode != 0:
                raise AssertionError(f"fixture indexing failed:\n{cold.stdout}{cold.stderr}")
            cls.before = invariants.artifact_hashes(cls.sandbox.data)
            warm = runner.cli(["index"])
            if warm.returncode != 0:
                raise AssertionError(f"warm reindex failed:\n{warm.stdout}{warm.stderr}")
            cls.after = invariants.artifact_hashes(cls.sandbox.data)
            listing = runner.rs(["stores", "--paths"])
            cls.discovered = [(row["name"], Path(row["path"])) for row in json.loads(listing.stdout)
                              if row.get("state") == "available"]
            cls.book = invariants.intake_book(cls.sandbox.data)
            cls.messages = invariants.read_jsonl(cls.sandbox.data / "messages.jsonl")
            cls.sessions = invariants.read_jsonl(cls.sandbox.data / "sessions.jsonl")
        except BaseException:
            cls.sandbox.close()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls.sandbox.close()

    def assertCheck(self, check: invariants.Check) -> None:
        self.assertTrue(check.ok, f"{check.name}: {json.dumps(check.counts, sort_keys=True)}")

    def test_fixture_grammar_is_discovered_by_the_registry(self) -> None:
        discovered = {path for _adapter, path in self.discovered}
        missing = sorted(str(path.relative_to(self.sandbox.home))
                         for path in self.sandbox.fixture_files if path not in discovered)
        self.assertEqual(missing, [])
        self.assertEqual({adapter for adapter, _path in self.discovered}, set(EXPECTED_ADAPTERS))

    def test_every_adapter_publishes_rows(self) -> None:
        per_agent = {agent: sum(1 for row in self.messages if row["agent"] == agent)
                     for agent in EXPECTED_ADAPTERS}
        self.assertTrue(all(per_agent.values()), per_agent)

    def test_intake_identity_holds_for_every_file(self) -> None:
        self.assertCheck(invariants.check_intake_identity(self.book))

    def test_synthetic_mirrors_never_become_rows(self) -> None:
        check = invariants.check_source_bounds(self.book, self.sandbox.home, shapes.JSONL_ADAPTERS)
        self.assertCheck(check)
        self.assertGreater(check.counts["synthetic_records"], 0,
                           "fixtures carry no synthetic mirror records; refresh them")

    def test_every_file_maps_to_a_session_or_a_recorded_skip(self) -> None:
        self.assertCheck(invariants.check_coverage(self.discovered, self.book, self.sessions))

    def test_published_rows_never_exceed_tallied_rows(self) -> None:
        self.assertCheck(invariants.check_per_adapter_bounds(self.messages, self.book))

    def test_message_ids_and_turns_are_unique(self) -> None:
        self.assertCheck(invariants.check_duplicate_ids(self.messages))

    def test_alias_and_family_closure(self) -> None:
        check = invariants.check_family_closure(self.messages, self.sessions,
                                                self.sandbox.data / "corpus.db")
        self.assertCheck(check)
        self.assertGreater(check.counts["side_sessions"], 0, "fixtures carry no side sessions")

    def test_project_labels_are_never_generic_containers(self) -> None:
        self.assertCheck(invariants.check_project_labels(self.sessions, self.book))

    def test_handles_round_trip_through_around(self) -> None:
        check = invariants.check_handle_round_trip(
            self.sandbox.runner, self.messages, self.sessions, corpus=self.sandbox.data / "corpus.db")
        self.assertCheck(check)
        self.assertGreater(check.counts["handles_checked"], 0)

    def test_warm_reindex_is_byte_identical(self) -> None:
        self.assertCheck(invariants.check_warm_identity(self.before, self.after))

    def test_search_finds_indexed_first_lines(self) -> None:
        check = invariants.check_search_first_lines(self.sandbox.runner, self.sessions)
        self.assertCheck(check)
        self.assertGreater(check.counts["queried"], 0)


class InvariantDetectionTests(unittest.TestCase):
    """The checks must reject the exact failure classes they exist for."""

    def test_identity_rejects_a_silently_dropped_record(self) -> None:
        book = {"/a": {"agent": "pi", "seen": 5, "rows": 2, "agent_rows": 1,
                       "skips": {"meta": 1}, "errors": 0}}
        self.assertFalse(invariants.check_intake_identity(book).ok)
        book["/a"]["skips"]["non_message"] = 1
        self.assertTrue(invariants.check_intake_identity(book).ok)

    def test_source_bounds_reject_mirrors_counted_as_rows(self) -> None:
        with tempfile.TemporaryDirectory(dir=tempfile.gettempdir()) as temp:
            path = Path(temp) / "s.jsonl"
            rows = [
                {"type": "session", "id": "s1", "version": 3, "cwd": "/home/u/projects/x"},
                {"type": "message", "id": "a", "parentId": None,
                 "message": {"role": "user", "synthetic": True, "content": [{"type": "text", "text": "m"}]}},
                {"type": "message", "id": "b", "parentId": "a",
                 "message": {"role": "assistant", "content": [{"type": "text", "text": "r"}]}},
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            entry = {"agent": "pi", "seen": 3, "rows": 2, "agent_rows": 0,
                     "skips": {"meta": 1}, "errors": 0}
            self.assertFalse(invariants.check_source_bounds({str(path): entry}, Path(temp)).ok)
            entry.update(rows=1, skips={"meta": 1, "sidechain": 1})
            self.assertTrue(invariants.check_source_bounds({str(path): entry}, Path(temp)).ok)

    def test_source_bounds_oracle_the_generation_the_tally_covered(self) -> None:
        # A live sidecar grows after its tally: appended mirrors were never counted, so the
        # check must read the prefix its stat key records, and still catch a leak inside it.
        with tempfile.TemporaryDirectory(dir=tempfile.gettempdir()) as temp:
            path = Path(temp) / "s.jsonl"
            tallied = [
                {"type": "session", "id": "s1", "version": 3, "cwd": "/home/u/projects/x"},
                {"type": "message", "id": "a", "parentId": None,
                 "message": {"role": "user", "synthetic": True, "content": [{"type": "text", "text": "m"}]}},
                {"type": "message", "id": "b", "parentId": "a",
                 "message": {"role": "assistant", "content": [{"type": "text", "text": "r"}]}},
            ]
            appended = [
                {"type": "message", "id": "c", "parentId": "b",
                 "message": {"role": "user", "synthetic": True, "content": [{"type": "text", "text": "m2"}]}},
                {"type": "message", "id": "d", "parentId": "c",
                 "message": {"role": "user", "content": [{"type": "text", "text": "typed later"}]}},
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in tallied), encoding="utf-8")
            stat = path.stat()
            key = f"s:{stat.st_mtime_ns // 1_000_000}:{stat.st_size}"
            with path.open("a", encoding="utf-8") as handle:
                handle.write("".join(json.dumps(row) + "\n" for row in appended))
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000))
            entry = {"agent": "pi", "key": key, "seen": 3, "rows": 1, "agent_rows": 0,
                     "skips": {"meta": 1, "sidechain": 1}, "errors": 0}
            check = invariants.check_source_bounds({str(path): entry}, Path(temp))
            self.assertTrue(check.ok, check.counts)
            self.assertEqual(check.counts["tallied_extent"], {"prefix": 1})
            entry.update(rows=2, skips={"meta": 1})
            check = invariants.check_source_bounds({str(path): entry}, Path(temp))
            self.assertIn("pi:synthetic_not_skipped", check.counts["violations"])
            path.write_text("", encoding="utf-8")
            check = invariants.check_source_bounds({str(path): entry}, Path(temp))
            self.assertTrue(check.ok, check.counts)
            self.assertEqual(check.counts["tallied_extent"], {"uncomparable": 1})

    def test_duplicate_ids_are_rejected(self) -> None:
        rows = [{"id": "pi:s:0", "session": "s", "turn": 0}, {"id": "pi:s:0", "session": "s", "turn": 1}]
        self.assertFalse(invariants.check_duplicate_ids(rows).ok)

    def test_generic_container_labels_are_rejected_for_name_labels_only(self) -> None:
        self.assertFalse(invariants.check_project_labels(
            [{"agent": "claude", "project": "projects"}]).ok)
        self.assertTrue(invariants.check_project_labels(
            [{"agent": "pi", "project": "/home/u/Desktop/projects"},
             {"agent": "claude", "project": "amber"}]).ok)

    def test_family_closure_rejects_an_alias_claimed_twice(self) -> None:
        with tempfile.TemporaryDirectory(dir=tempfile.gettempdir()) as temp:
            corpus = Path(temp) / "corpus.db"
            with sqlite3.connect(corpus) as db:
                db.execute("CREATE TABLE session_family(session TEXT PRIMARY KEY, root TEXT, side INTEGER)")
                db.executemany("INSERT INTO session_family VALUES(?,?,?)",
                               [("a", "a", 0), ("b", "b", 0), ("x", "a", 0)])
            messages = [{"session": "a", "turn": 0}, {"session": "b", "turn": 0}]
            sessions = [{"session": "a", "n": 1, "alias": "x"}, {"session": "b", "n": 1, "alias": "x"}]
            self.assertFalse(invariants.check_family_closure(messages, sessions, corpus).ok)
            sessions[1].pop("alias")
            self.assertTrue(invariants.check_family_closure(messages, sessions, corpus).ok)


if __name__ == "__main__":
    unittest.main()
