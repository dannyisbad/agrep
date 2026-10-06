"""Scrubbed real transcript shapes ingest through the sealed CLI and hold the scale invariants."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bench" / "real_history"))

import invariants  # noqa: E402
import sandbox  # noqa: E402
import shapes  # noqa: E402
import snapshot  # noqa: E402

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
        check = invariants.check_search_first_lines(self.sandbox.runner, self.sessions, self.messages)
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

    def test_a_folder_named_like_a_container_is_its_own_label(self) -> None:
        with tempfile.TemporaryDirectory(dir=tempfile.gettempdir()) as temp:
            book: dict[str, dict] = {}
            sessions = []
            for session, cwd, label in (
                    ("01a10e65-7175-76a2-a9a0-881d2e8f05db",
                     "/Users/u/Documents/Codex/2026-10-05/t", "t"),
                    ("02b20e65-7175-76a2-a9a0-881d2e8f05db",
                     "/Users/u/projects/amber/src", "projects")):
                path = Path(temp) / f"rollout-2026-10-05T16-28-11-{session}.jsonl"
                meta = {"type": "session_meta", "payload": {"id": session, "cwd": cwd}}
                path.write_text(json.dumps(meta) + "\n", encoding="utf-8")
                book[str(path)] = {"agent": "codex"}
                sessions.append({"agent": "codex", "session": session, "project": label})
            check = invariants.check_project_labels(sessions[:1], book)
            self.assertTrue(check.ok, check.counts)
            self.assertEqual(check.counts["container_named_folder_labels"], {"codex": 1})
            check = invariants.check_project_labels(sessions, book)
            self.assertEqual(check.counts["generic_container_labels"], {"codex": 1})
            claude = Path(temp) / "claude.jsonl"
            claude.write_text(json.dumps(
                {"type": "user", "sessionId": "c1", "cwd": "/Users/u/Desktop/projects/amber/t"}) + "\n",
                encoding="utf-8")
            book[str(claude)] = {"agent": "claude"}
            check = invariants.check_project_labels(
                [{"agent": "claude", "session": "c1", "project": "t"}], book)
            self.assertEqual(check.counts["generic_container_labels"], {"claude": 1})

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

    def test_search_first_lines_reject_a_row_missing_from_search(self) -> None:
        # The publication layer still lists the first line while the search database has lost
        # its row: the check must report that session, not just a lower rank.
        if not FIXTURES.is_dir() or not (FIXTURES / "manifest.json").is_file():
            raise unittest.SkipTest("bench/fixtures/real_shapes has not been generated")
        box = RealShapeSandbox()
        try:
            runner = box.runner
            self.assertEqual(runner.cli(["index"]).returncode, 0)
            sessions = invariants.read_jsonl(box.data / "sessions.jsonl")
            messages = invariants.read_jsonl(box.data / "messages.jsonl")
            victim = next(row for row in sessions
                          if row["agent"] == "claude" and len(row.get("first_text", "")) >= 12)
            turn = invariants._first_line_turn(
                victim["first_text"], [row for row in messages if row["session"] == victim["session"]])
            self.assertIsNotNone(turn)
            check = invariants.check_search_first_lines(runner, [victim], messages, sample=1)
            self.assertTrue(check.ok, check.counts)
            with sqlite3.connect(box.data / "corpus.db") as corpus:
                deleted = corpus.execute("DELETE FROM msgs WHERE session = ? AND turn = ?",
                                         (victim["session"], turn)).rowcount
            self.assertGreater(deleted, 0)
            check = invariants.check_search_first_lines(runner, [victim], messages, sample=1)
            self.assertFalse(check.ok, check.counts)
            self.assertEqual(check.counts["outcomes"], {"no_hit_in_session": 1})
        finally:
            box.close()

    def test_handle_round_trip_expects_the_kind_a_handle_cites(self) -> None:
        # A tool hit at a recap turn reopens as its tool row alone; losing that row must still fail.
        session = "0123abcd-0000-4000-8000-000000000000"
        handle, turn = "@0123abcd:4.ab12", 4

        class Canned:
            json_rows = sandbox.CliRunner.json_rows

            def __init__(self, shown: list[dict]) -> None:
                self.shown = shown

            def cli(self, argv, **_options):
                rows = []
                if argv[0] == "search":
                    rows = [{"handle": handle, "session": session, "who": "tool"}]
                elif argv[0] == "around":
                    rows = [{"kind": "agrep-meta", "scope": {
                        "session": session, "selected_record_role": "tool"}}, *self.shown]
                stdout = "".join(json.dumps(row) + "\n" for row in rows)
                return subprocess.CompletedProcess(argv, 0, stdout, "")

        messages = [{"session": session, "turn": turn}]
        sessions = [{"session": session, "first_text": "lantern compass ledger"}]
        tool_row = {"kind": "tool", "session": session, "turn": turn}
        check = invariants.check_handle_round_trip(Canned([tool_row]), messages, sessions)
        self.assertTrue(check.ok, check.counts)
        check = invariants.check_handle_round_trip(Canned([]), messages, sessions)
        self.assertEqual(check.counts["failures"], {"tool:around_turn_missing": 1})


class SnapshotTests(unittest.TestCase):
    """Freezing a home clones file stores byte for byte and backs databases up consistently."""

    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="agrep-frozen-", dir=tempfile.gettempdir()))
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.home = self.temp / "home"
        store = self.home / ".claude" / "projects" / "-home-u-projects-x"
        store.mkdir(parents=True)
        self.transcript = store / "s1.jsonl"
        self.transcript.write_text('{"type":"user","cwd":"/home/u/projects/x"}\n', encoding="utf-8")
        os.utime(self.transcript, ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))
        self.database = self.home / sandbox.OPENCODE_DB
        self.database.parent.mkdir(parents=True)
        with sqlite3.connect(self.database) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE session_v2(id TEXT PRIMARY KEY)")
            db.execute("INSERT INTO session_v2 VALUES('a'), ('b')")
        self.discovered = [("claude", self.transcript), ("opencode", self.database)]
        self.dest = self.temp / "frozen"

    def freeze(self, home: Path | None = None, discovered: list | None = None) -> dict:
        try:
            return snapshot.freeze(home or self.home, self.dest,
                                   self.discovered if discovered is None else discovered)
        except snapshot.SnapshotError as error:
            if "copy-on-write clones need" in str(error):
                raise unittest.SkipTest(str(error))
            raise

    def test_clone_is_byte_identical_and_the_database_is_backed_up(self) -> None:
        manifest = self.freeze()
        frozen = self.dest / self.transcript.relative_to(self.home)
        self.assertEqual(frozen.read_bytes(), self.transcript.read_bytes())
        self.assertEqual(frozen.stat().st_mtime_ns, self.transcript.stat().st_mtime_ns)
        with sqlite3.connect(f"file:{self.dest / sandbox.OPENCODE_DB}?mode=ro", uri=True) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM session_v2").fetchone()[0], 2)
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        self.assertFalse((self.dest / sandbox.OPENCODE_DB).with_name("opencode.db-wal").exists())
        self.assertEqual({root["relative"]: root["kind"] for root in manifest["roots"]},
                         {".claude/projects": "clone", ".local/share/opencode": "sqlite-backup"})
        self.assertEqual(snapshot.read_manifest(self.dest)["source_home"], str(self.home.resolve()))
        # the source stays untouched: same bytes, same stat key, WAL sidecars left alone
        self.assertEqual(self.transcript.stat().st_mtime_ns, 1_600_000_000_000_000_000)
        self.assertTrue(snapshot.delete(self.dest))
        self.assertFalse(self.dest.exists())

    def test_content_outside_known_roots_is_refused_not_dropped(self) -> None:
        stray = self.home / ".unknown" / "chat.jsonl"
        stray.parent.mkdir()
        stray.write_text("{}\n", encoding="utf-8")
        self.discovered.append(("mystery", stray))
        with self.assertRaises(snapshot.SnapshotError):
            self.freeze()
        self.assertFalse(self.dest.exists())

    def test_home_reached_through_a_symlink_is_frozen(self) -> None:
        # macOS temp dirs live under /var -> /private/var, so a given path rarely equals its resolved one
        link = self.temp.parent / f"{self.temp.name}-link"
        try:
            link.symlink_to(self.temp, target_is_directory=True)
        except OSError as error:
            if os.name != "nt":
                raise
            raise unittest.SkipTest(f"symlinks need a privilege here: {error}") from error
        self.addCleanup(link.unlink)
        home = link / "home"
        discovered = [(adapter, home / path.relative_to(self.home)) for adapter, path in self.discovered]
        manifest = self.freeze(home, discovered)
        self.assertEqual({root["relative"] for root in manifest["roots"]},
                         {".claude/projects", ".local/share/opencode"})
        frozen = self.dest / self.transcript.relative_to(self.home)
        self.assertEqual(frozen.read_bytes(), self.transcript.read_bytes())

    def test_delete_refuses_a_directory_without_a_manifest(self) -> None:
        self.dest.mkdir()
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.delete(self.dest)
        self.assertTrue(self.dest.exists())


if __name__ == "__main__":
    unittest.main()
