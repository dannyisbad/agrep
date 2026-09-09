"""Generation and lookup contracts for the indexed session-family sidecar."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest import mock

from _test_support import isolate_data_dir


isolate_data_dir()
import common  # noqa: E402
import corpusdb  # noqa: E402
import explore  # noqa: E402
import session_context  # noqa: E402


@contextmanager
def _data_dir(root: Path):
    with mock.patch.object(common, "DATA_DIR", root), \
            mock.patch.object(session_context, "DATA_DIR", root):
        yield


class CorpusSessionFamilyTests(unittest.TestCase):
    @staticmethod
    def _write_family_meta(
            root: Path, rows: list[dict], signature: str = "2:fixture") -> None:
        pairs = [
            (str(row["session"]), str(row.get("parent") or ""),
             str(row.get("alias") or ""))
            for row in rows
        ]
        (root / common.SESSION_FAMILY_META_FILE).write_text(
            json.dumps({
                "version": common.SESSION_FAMILY_INDEX_VERSION,
                "algorithm": common.SESSION_FAMILY_DIGEST_ALGORITHM,
                "ingest_signature": signature,
                "count": len(rows),
                "digest": common.session_family_digest(sorted(pairs)),
            }),
            encoding="utf-8",
        )

    @classmethod
    def _publish_families(
            cls, root: Path, rows: list[dict],
            signature: str = "2:fixture") -> None:
        rows = sorted(rows, key=lambda row: str(row["session"]))
        cls._write_family_meta(root, rows, signature)
        (root / "sessions.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )
        (root / ".ingest.sig").write_bytes((signature + "\n").encode("utf-8"))

    def _paths(self, root: Path):
        return (
            _data_dir(root),
            mock.patch.object(common, "MESSAGES_PATH", root / "messages.jsonl"),
            mock.patch.object(corpusdb, "DB_PATH", root / "corpus.db"),
            mock.patch.object(
                corpusdb, "BOUNDARY_STATS_PATH", root / "boundary_stats.json"),
            mock.patch.object(corpusdb, "INGEST_SIG_PATH", root / ".ingest.sig"),
            mock.patch.object(
                corpusdb, "CHANGED_PATH", root / ".changed_sessions"),
            mock.patch.object(common, "setting", return_value="off"),
        )

    @staticmethod
    def _write_messages(root: Path) -> None:
        rows = [
            {
                "id": f"codex:{session}:1", "session": session,
                "agent": "codex", "turn": 1, "text": f"needle {session}",
            }
            for session in ("a", "b")
        ]
        (root / "messages.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )

    def test_full_build_releases_scan_rows_before_fts_rebuild(self) -> None:
        released: list[bool] = []
        case = self

        class TrackedRows(dict):
            def __del__(self):
                released.append(True)

        class FakeDb:
            def __init__(self):
                self.closed = False
                self.scripts = []

            def executescript(self, sql):
                self.scripts.append(sql)
                return None

            def executemany(self, _sql, rows):
                for _row in rows:
                    pass

            def execute(self, sql, _params=()):
                if "INSERT INTO msgs_fts(msgs_fts)" in sql:
                    case.assertTrue(released)
                return ()

            def commit(self):
                return None

            def close(self):
                self.closed = True

        row = ("a", 1, 2, "codex", "/project", "", "model",
               "explicit", "user", "needle", "0123")
        fake = FakeDb()
        snapshot = corpusdb._SessionFamilySnapshot(
            "family-stamp", frozenset({"a"}), {})

        def scan():
            return TrackedRows({"a": [row]})

        with mock.patch.object(corpusdb, "_protected_derived_target", return_value=False), \
                mock.patch.object(corpusdb, "_stamp", return_value="stable"), \
                mock.patch.object(corpusdb, "_read_session_families", return_value=snapshot), \
                mock.patch.object(corpusdb, "_scan", side_effect=scan), \
                mock.patch.object(corpusdb, "_read_boundary_stats", return_value=[]), \
                mock.patch.object(corpusdb.sqlite3, "connect", return_value=fake), \
                mock.patch.object(
                    corpusdb.indexd_runtime, "derived_writer_build_id",
                    return_value="a" * 20):
            corpusdb._build(Path("/fixture/corpus.db"), "stable")

        self.assertTrue(fake.closed)
        self.assertTrue(released)
        self.assertIn("PRAGMA cache_size=-32768", fake.scripts[0])

    def test_windowed_filter_sql_only_hides_the_callers_proven_window(self) -> None:
        db = sqlite3.connect(":memory:")
        db.executescript("""
            CREATE TABLE msgs(session TEXT, turn INTEGER, ts INTEGER, text TEXT);
            CREATE TABLE session_family(
                session TEXT PRIMARY KEY, root TEXT NOT NULL,
                side INTEGER NOT NULL CHECK(side IN (0, 1))) WITHOUT ROWID;
        """)
        db.executemany(
            "INSERT INTO session_family VALUES(?, ?, ?)",
            (("root", "root", 0), ("child", "root", 0),
             ("custom-side", "root", 1), ("agent-name-only", "root", 0),
             ("foreign-side", "other", 1)))
        db.executemany(
            "INSERT INTO msgs VALUES(?, ?, ?, ?)",
            (("root", 4, 1, "old caller"), ("root", 7, 2, "current echo"),
             ("custom-side", 0, 3, "delegated echo"),
             ("child", 20, 4, "older family"),
             ("agent-foreign", 0, 5, "independent sidechain"),
             ("root", None, 6, "missing caller turn"),
             ("root", "not-a-turn", 7, "malformed caller turn")))
        where, params = corpusdb._filter_sql(
            {"exclude_session": "root", "exclude_session_from_turn": 7})
        rows = {row[0] for row in db.execute(
            "SELECT text FROM msgs WHERE " + " AND ".join(where), params)}
        db.close()
        self.assertEqual(
            rows,
            {"old caller", "delegated echo", "older family",
             "independent sidechain", "missing caller turn",
             "malformed caller turn"})

    def test_exact_session_filter_scales_past_inline_sql_parameters(self) -> None:
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE msgs(session TEXT, text TEXT)")
        db.executemany(
            "INSERT INTO msgs VALUES(?, ?)",
            ((f"session-{index}", f"row {index}") for index in range(10)))
        excluded = tuple(f"session-{index}" for index in range(9))
        where, params = corpusdb._filter_sql({
            "_exclude_sessions": excluded,
        })
        rows = list(db.execute(
            "SELECT session FROM msgs WHERE " + " AND ".join(where), params))
        db.close()
        self.assertIn("json_each", where[0])
        self.assertEqual(len(params), 1)
        self.assertEqual(rows, [("session-9",)])

    def test_windowed_sql_keeps_related_sidechains_as_ordinary_history(self) -> None:
        # A caller that is itself a sidechain still excludes only its own
        # proven window. Related transcripts are independent history: SQL and
        # SelfExclusion must agree or the same query flips with engine choice.
        db = sqlite3.connect(":memory:")
        db.executescript("""
            CREATE TABLE msgs(session TEXT, turn INTEGER, ts INTEGER, text TEXT);
            CREATE TABLE session_family(
                session TEXT PRIMARY KEY, root TEXT NOT NULL,
                side INTEGER NOT NULL CHECK(side IN (0, 1))) WITHOUT ROWID;
        """)
        db.executemany(
            "INSERT INTO session_family VALUES(?, ?, ?)",
            (("root", "root", 0), ("custom-caller", "root", 1),
             ("custom-side", "root", 1)))
        db.executemany(
            "INSERT INTO msgs VALUES(?, ?, ?, ?)",
            (("custom-caller", 10, 1, "own pre-boundary"),
             ("custom-caller", 60, 2, "own current echo"),
             ("custom-side", 0, 3, "sibling sidechain"),
             ("root", 4, 4, "old caller root")))
        where, params = corpusdb._filter_sql(
            {"exclude_session": "custom-caller",
             "exclude_session_from_turn": 50})
        rows = {row[0] for row in db.execute(
            "SELECT text FROM msgs WHERE " + " AND ".join(where), params)}
        db.close()
        self.assertEqual(
            rows, {"own pre-boundary", "sibling sidechain", "old caller root"})
        family = session_context.CallingFamily(
            "custom-caller", "root",
            frozenset({"root", "custom-caller", "custom-side"}),
            True, 50, frozenset({"custom-caller", "custom-side"}))
        policy = session_context.SelfExclusion(family, 50, "recap")
        expected = {
            ("custom-caller", 10): False, ("custom-caller", 60): True,
            ("custom-side", 0): False, ("root", 4): False,
            ("custom-caller", None): False,
            ("custom-caller", "not-a-turn"): False,
        }
        for (session, turn), excluded in expected.items():
            self.assertEqual(policy.excludes(session, turn), excluded,
                             (session, turn))

    def test_recap_state_separates_no_rows_from_malformed_rows(self) -> None:
        db = sqlite3.connect(":memory:")
        db.executescript("""
            CREATE TABLE msgs(session TEXT, turn INTEGER, who TEXT);
            CREATE TABLE session_family(
                session TEXT PRIMARY KEY, root TEXT NOT NULL,
                side INTEGER NOT NULL CHECK(side IN (0, 1))) WITHOUT ROWID;
        """)
        db.executemany(
            "INSERT INTO session_family VALUES(?, ?, ?)",
            (("fresh", "fresh", 0), ("compacted", "compacted", 0),
             ("malformed", "malformed", 0), ("negative", "negative", 0)))
        db.executemany(
            "INSERT INTO msgs VALUES(?, ?, ?)",
            (("fresh", 0, "user"), ("fresh", 1, "agent"),
             ("compacted", 3, "recap"), ("compacted", 9, "recap"),
             ("compacted", 10, "user"),
             ("malformed", 7.5, "recap"),
             ("negative", -1, "recap")))
        cases = {
            "fresh": (None, True),
            "compacted": (9, False),
            "malformed": (None, False),
            "negative": (None, False),
            "unknown": (None, True),
        }
        for session, expected in cases.items():
            with self.subTest(session=session):
                self.assertEqual(
                    session_context._indexed_recap_boundary_in_db(db, session),
                    expected)
        # the resolved never-compacted caller becomes a window from turn 0
        state = session_context._indexed_calling_family_full_state_in_db(
            db, "fresh")
        self.assertEqual(
            state, ("fresh", "fresh", frozenset({"fresh"}), None, True))
        where, params = corpusdb._filter_sql(
            {"exclude_session": "fresh", "exclude_session_from_turn": 0})
        rows = {row[0] for row in db.execute(
            "SELECT session || ':' || turn FROM msgs WHERE "
            + " AND ".join(where), params)}
        db.close()
        self.assertNotIn("fresh:0", rows)
        self.assertNotIn("fresh:1", rows)
        self.assertIn("compacted:10", rows)

    def test_awaited_read_recovers_from_a_torn_republish(self) -> None:
        import threading

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._publish_families(root, [{"session": "root"}], "2:gen0")
            # tear the publication like a mid-republish ingest: new sessions
            # rows on disk, meta/signature still describing the old generation
            (root / "sessions.jsonl").write_text(
                json.dumps({"session": "root"}) + "\n"
                + json.dumps({"session": "twig", "parent": "root"}) + "\n",
                encoding="utf-8")
            with _data_dir(root):
                self.assertIsNone(common.strict_family_parent_map(root))
                repair = threading.Timer(0.15, lambda: self._publish_families(
                    root,
                    [{"session": "root"},
                     {"session": "twig", "parent": "root"}],
                    "2:gen1"))
                repair.start()
                try:
                    parents = common.await_family_publication(
                        lambda: common.strict_family_parent_map(root),
                        timeout_s=5.0)
                finally:
                    repair.join()
        self.assertEqual(parents, {"twig": "root"})

    def test_awaited_read_never_waits_on_an_absent_publication(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with _data_dir(root):
                started = time.monotonic()
                parents = common.await_family_publication(
                    lambda: common.strict_family_parent_map(root))
                elapsed = time.monotonic() - started
        self.assertEqual(parents, {})
        self.assertLess(elapsed, 0.5)

    def test_awaited_read_gives_up_on_a_stably_invalid_publication(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._publish_families(root, [{"session": "root"}])
            (root / "messages.jsonl").write_text("{}\n", encoding="utf-8")
            self._write_family_meta(
                root, [{"session": "root"}, {"session": "ghost"}])
            with _data_dir(root):
                parents = common.await_family_publication(
                    lambda: common.strict_family_parent_map(root),
                    timeout_s=0.2)
        self.assertIsNone(parents)

    def test_awaited_read_classifies_a_valid_meta_ahead_of_signature(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._publish_families(root, [{"session": "root"}], "2:gen0")
            (root / "messages.jsonl").write_text("{}\n", encoding="utf-8")
            self._write_family_meta(
                root,
                [{"session": "root"}, {"session": "twig", "parent": "root"}],
                "2:gen1",
            )
            with _data_dir(root), self.assertRaises(
                    common.TranscriptPublicationRace):
                common.await_family_publication(
                    lambda: common.strict_family_parent_map(root),
                    timeout_s=0.0,
                    data_dir=root,
                )

    def test_family_digest_matches_the_rust_fixture(self) -> None:
        # pinned by cache.rs session_family_proof_tracks_only_the_parent_census
        self.assertEqual(
            common.session_family_digest([("child", "root")]),
            "c2e7baea9bc8402efb6ddd59b11482b4158cdbe4544c6200",
        )
        self.assertEqual(
            common.session_family_digest([("child", "root", "")]),
            common.session_family_digest([("child", "root")]),
        )
        self.assertNotEqual(
            common.session_family_digest([("root", "", "file-id")]),
            common.session_family_digest([("root", "")]),
        )

    def test_restored_mtime_invalidates_the_cached_family_census(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rows = [
                {"session": "child", "parent": "root-a"},
                {"session": "root-a"},
            ]
            self._publish_families(root, rows)
            sessions = root / "sessions.jsonl"
            with _data_dir(root):
                first = common.read_session_family_census(root)
                self.assertIsNotNone(first)
                modified = sessions.stat().st_mtime_ns
                damaged = sessions.read_bytes().replace(b"root-a", b"root-b", 1)
                sessions.write_bytes(damaged)
                os.utime(sessions, ns=(modified, modified))
                self.assertIsNone(common.read_session_family_census(root))

    def test_streamed_family_digest_matches_a_large_reference(self) -> None:
        import hashlib

        rows = [
            (f"session-{index:05d}", f"parent-{index // 7:05d}",
             f"alias-{index:05d}" if index % 11 == 0 else "")
            for index in range(5_000)
        ]
        canonical = bytearray(b"agrep-session-family-v1\0")
        for row in rows:
            for value in row:
                encoded = value.encode()
                canonical.extend(len(encoded).to_bytes(8, "little"))
                canonical.extend(encoded)
        fnv = 0xCBF29CE484222325
        for byte in b"\1" + canonical:
            fnv ^= byte
            fnv = (fnv * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
        expected = (
            f"{hashlib.md5(canonical, usedforsecurity=False).hexdigest()}"
            f"{fnv:016x}"
        )
        self.assertEqual(common.session_family_digest(iter(rows)), expected)

    def test_family_derivation_version_invalidates_the_corpus_stamp(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._publish_families(root, [{"session": "root"}])
            with _data_dir(root), \
                    mock.patch.object(common, "setting", return_value="off"):
                first = corpusdb._stamp()
                with mock.patch.object(
                        session_context,
                        "SESSION_FAMILY_DERIVATION_VERSION",
                        common.SESSION_FAMILY_DERIVATION_VERSION + 1,
                ):
                    second = corpusdb._stamp()
        self.assertNotEqual(first, second)

    def test_reconcile_cannot_stamp_an_incomplete_family_table(self) -> None:
        with closing(sqlite3.connect(":memory:")) as db:
            db.execute(
                "CREATE TABLE session_family("
                "session TEXT PRIMARY KEY, root TEXT NOT NULL,"
                "side INTEGER NOT NULL CHECK(side IN (0, 1)))")
            db.execute(
                "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute(
                "CREATE TRIGGER reject_b BEFORE INSERT ON session_family "
                "WHEN NEW.session='b' BEGIN SELECT RAISE(IGNORE); END")
            snapshot = corpusdb._SessionFamilySnapshot(
                "fixture-stamp", frozenset({"a", "b"}), {})
            with self.assertRaisesRegex(
                    corpusdb._SourceMoved, "did not publish every session"):
                corpusdb._reconcile_session_families(db, snapshot, "*")
            self.assertIsNone(db.execute(
                "SELECT value FROM meta WHERE key='family_stamp'").fetchone())

    _ALIASED_ROWS = [
        # a pi root whose header id was rewritten: the filename (and every
        # sidechat child) still names the old id
        {"session": "header-id", "alias": "file-id"},
        {"session": "child", "parent": "header-id"},
        {"session": "later-child", "parent": "header-id"},
        {"session": "other"},
    ]
    _FAMILY_SCHEMA = """
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE session_family(
            session TEXT PRIMARY KEY, root TEXT NOT NULL,
            side INTEGER NOT NULL CHECK(side IN (0, 1))) WITHOUT ROWID;
        CREATE INDEX session_family_root ON session_family(root);
        CREATE TABLE msgs(session TEXT, who TEXT, turn INTEGER, ts INTEGER,
                          text TEXT);
    """

    def _family_table(self, db) -> dict[str, tuple[str, int]]:
        return {
            str(session): (str(root), int(side))
            for session, root, side in db.execute(
                "SELECT session, root, side FROM session_family")
        }

    def test_alias_rows_attach_the_filename_id_to_the_header_family(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._publish_families(root, self._ALIASED_ROWS)
            with _data_dir(root):
                census = common.read_session_family_census(root)
                self.assertEqual(dict(census.aliases), {"file-id": "header-id"})
                self.assertEqual(
                    dict(census.parents),
                    {"child": "header-id", "later-child": "header-id"})
                snapshot = corpusdb._read_session_families()
            expected = {
                "header-id": ("header-id", 0),
                "child": ("header-id", 1),
                "later-child": ("header-id", 1),
                "other": ("other", 0),
                "file-id": ("header-id", 0),
            }
            with closing(sqlite3.connect(":memory:")) as db:
                db.executescript(self._FAMILY_SCHEMA)
                corpusdb._replace_session_families(db, snapshot)
                self.assertEqual(self._family_table(db), expected)
                self.assertEqual(
                    json.loads(db.execute(
                        "SELECT value FROM meta WHERE key=?",
                        (common.FAMILY_ALIASES_META_KEY,)).fetchone()[0]),
                    {"file-id": "header-id"})
                # the index built before the header rewrite: children hang off
                # the filename id as a parent-only extra
                db.execute("DELETE FROM session_family")
                before = corpusdb._SessionFamilySnapshot(
                    "old-stamp", snapshot.sessions,
                    {"child": "file-id", "later-child": "file-id"})
                corpusdb._replace_session_families(db, before)
                self.assertEqual(
                    self._family_table(db)["later-child"], ("file-id", 1))
                # the alias arrives on a delta that names none of the children;
                # they re-root anyway
                corpusdb._reconcile_session_families(db, snapshot, {"other"})
                self.assertEqual(self._family_table(db), expected)
                # the stable alias keeps the incremental path incremental
                db.execute(
                    "UPDATE session_family SET root='file-id' "
                    "WHERE session='later-child'")
                corpusdb._reconcile_session_families(
                    db, snapshot, {"later-child"})
                self.assertEqual(self._family_table(db), expected)
                # an alias the publication dropped disappears with it
                plain = corpusdb._SessionFamilySnapshot(
                    "plain-stamp", snapshot.sessions, snapshot.parents)
                corpusdb._reconcile_session_families(db, plain, {"other"})
                self.assertNotIn("file-id", self._family_table(db))
                self.assertEqual(
                    json.loads(db.execute(
                        "SELECT value FROM meta WHERE key=?",
                        (common.FAMILY_ALIASES_META_KEY,)).fetchone()[0]),
                    {})

    def test_census_refuses_an_alias_that_collides_with_a_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for rows in (
                    [{"session": "a", "alias": "b"}, {"session": "b"}],
                    [{"session": "a", "alias": "x"}, {"session": "b", "alias": "x"}],
                    [{"session": "a", "alias": "a"}],
            ):
                with self.subTest(rows=rows):
                    self._publish_families(root, rows)
                    with _data_dir(root):
                        self.assertIsNone(common.read_session_family_census(root))

    def test_both_ids_resolve_to_the_canonical_family(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._publish_families(root, self._ALIASED_ROWS)
            db = sqlite3.connect(root / "corpus.db")
            db.executescript(self._FAMILY_SCHEMA)
            with _data_dir(root):
                corpusdb._replace_session_families(
                    db, corpusdb._read_session_families())
                db.executemany(
                    "INSERT INTO msgs VALUES(?, ?, ?, ?, ?)",
                    (("header-id", "user", 0, 10, "before"),
                     ("header-id", "recap", 5, 50, "recap"),
                     ("header-id", "user", 6, 60, "after"),
                     ("child", "user", 0, 20, "early child"),
                     ("later-child", "user", 0, 70, "post-surgery child"),
                     ("other", "user", 0, 30, "unrelated")))
                db.commit()
                db.close()
                for query in ("file-id", "header-id", "file", "header"):
                    self.assertEqual(
                        common.indexed_session_matches(query), ["header-id"],
                        query)
                self.assertEqual(
                    common.indexed_family_roots(("file-id", "later-child")),
                    {"file-id": "header-id", "later-child": "header-id"})
                members = frozenset({"header-id", "child", "later-child"})
                for caller in ("file-id", "header-id"):
                    self.assertEqual(
                        common.indexed_calling_family(caller),
                        ("header-id", members), caller)
                    with mock.patch.object(
                            session_context, "calling_identity",
                            return_value=common.CallerIdentity(caller, "pi")):
                        family = common.calling_family()
                        policy = common.calling_self_exclusion()
                    self.assertEqual(family.session, "header-id", caller)
                    self.assertEqual(family.root, "header-id")
                    self.assertEqual(family.members, members)
                    self.assertEqual(
                        family.descendants, frozenset({"child", "later-child"}))
                    # the live window starts at the header session's recap and
                    # hides only the child spawned after it
                    self.assertEqual(policy.boundary, 5)
                    self.assertTrue(policy.excludes("header-id", 6))
                    self.assertFalse(policy.excludes("header-id", 0))
                    self.assertTrue(policy.excludes("later-child", 0))
                    self.assertFalse(policy.excludes("child", 0))
                    self.assertFalse(policy.excludes("other", 0))
                    self.assertEqual(
                        policy.query_filters()["exclude_session"], "header-id")
                # SQL family exclusion accepts either spelling of the caller
                with closing(sqlite3.connect(root / "corpus.db")) as db:
                    for caller in ("file-id", "header-id"):
                        where, params = corpusdb._filter_sql(
                            {"exclude_session": caller})
                        remaining = {row[0] for row in db.execute(
                            "SELECT text FROM msgs WHERE " + " AND ".join(where),
                            params)}
                        self.assertEqual(remaining, {"unrelated"}, caller)

    def test_source_census_preserves_aliases_without_the_query_database(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._publish_families(root, self._ALIASED_ROWS)
            servable = {row["session"]: row for row in self._ALIASED_ROWS}
            with _data_dir(root), \
                    mock.patch.object(explore, "_freshen"), \
                    mock.patch.object(explore, "_session_index", return_value=servable):
                for query in ("file-id", "file", "header-id"):
                    self.assertEqual(explore.resolve_session(query), ["header-id"])
                members, sides = explore._native_family_members("file-id")
                self.assertEqual(members, frozenset({"header-id", "child", "later-child"}))
                self.assertEqual(sides, frozenset({"child", "later-child"}))

    def test_hot_family_stamp_never_opens_the_census(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._publish_families(root, [{"session": "root"}])
            (root / "messages.jsonl").write_text(
                '{"id":"codex:root:1","session":"root","text":"same"}\n',
                encoding="utf-8",
            )
            with mock.patch.object(
                    common, "read_session_family_census",
                    side_effect=AssertionError("hot stamp scanned sessions")):
                self.assertIsNotNone(common.session_family_source_stamp(root))
                self.assertIsNotNone(common.transcript_generation(root))

    def test_missing_family_stamp_never_retries_a_proof_read(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(
                    session_context, "read_session_family_proof",
                    side_effect=AssertionError("absent publication retried")):
                stamp = common.session_family_source_stamp(root)
        self.assertEqual(stamp, common.SESSION_FAMILY_MISSING_STAMP)

    def test_parent_only_publication_change_updates_incrementally(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "messages.jsonl").write_text(
                json.dumps({
                    "id": "codex:child:1",
                    "session": "child",
                    "agent": "codex",
                    "turn": 1,
                    "text": "needle",
                }) + "\n",
                encoding="utf-8",
            )
            sessions = root / "sessions.jsonl"
            self._publish_families(root, [
                {"session": "root"},
                {"session": "child", "parent": "root"},
            ])
            patches = self._paths(root)
            with patches[0], patches[1], patches[2], patches[3], \
                    patches[4], patches[5], patches[6]:
                first_stamp = corpusdb._stamp()
                corpusdb._build(corpusdb.DB_PATH, first_stamp)
                with closing(sqlite3.connect(corpusdb.DB_PATH)) as db:
                    self.assertEqual(
                        db.execute(
                            "SELECT root FROM session_family "
                            "WHERE session='child'"
                        ).fetchone(),
                        ("root",),
                    )

                self._publish_families(root, [
                    {"session": "other"},
                    {"session": "child", "parent": "other"},
                ], "2:changed")
                corpusdb.CHANGED_PATH.write_text("", encoding="utf-8")
                second_stamp = corpusdb._stamp()
                self.assertNotEqual(first_stamp, second_stamp)
                refreshed = corpusdb._incremental(second_stamp)
                self.assertIsNotNone(refreshed)
                refreshed.close()
                with closing(sqlite3.connect(corpusdb.DB_PATH)) as db:
                    self.assertEqual(
                        db.execute(
                            "SELECT root FROM session_family "
                            "WHERE session='child'"
                        ).fetchone(),
                        ("other",),
                    )

    def test_incremental_writer_keeps_spill_enabled_with_large_cache(self) -> None:
        observed: list[tuple[int, int]] = []

        class ObservedConnection:
            def __init__(self, inner):
                self.inner = inner

            def commit(self):
                cache_size = self.inner.execute(
                    "PRAGMA cache_size").fetchone()[0]
                cache_spill = self.inner.execute(
                    "PRAGMA cache_spill").fetchone()[0]
                observed.append((int(cache_size), int(cache_spill)))
                return self.inner.commit()

            def __getattr__(self, name):
                return getattr(self.inner, name)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "messages.jsonl").write_text(
                json.dumps({
                    "id": "codex:child:1",
                    "session": "child",
                    "agent": "codex",
                    "turn": 1,
                    "text": "needle",
                }) + "\n",
                encoding="utf-8",
            )
            self._publish_families(root, [
                {"session": "root"},
                {"session": "child", "parent": "root"},
            ])
            patches = self._paths(root)
            with patches[0], patches[1], patches[2], patches[3], \
                    patches[4], patches[5], patches[6]:
                corpusdb._build(corpusdb.DB_PATH, corpusdb._stamp())
                self._publish_families(root, [
                    {"session": "other"},
                    {"session": "child", "parent": "other"},
                ], "2:changed")
                corpusdb.CHANGED_PATH.write_text("", encoding="utf-8")
                real_connect = sqlite3.connect
                writer_claimed = False

                def connect(*args, **kwargs):
                    nonlocal writer_claimed
                    inner = real_connect(*args, **kwargs)
                    if not writer_claimed and not kwargs.get("uri"):
                        writer_claimed = True
                        return ObservedConnection(inner)
                    return inner

                with mock.patch.object(
                        corpusdb.sqlite3, "connect", side_effect=connect):
                    refreshed = corpusdb._incremental(corpusdb._stamp())
                self.assertIsNotNone(refreshed)
                refreshed.close()

        self.assertTrue(writer_claimed)
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0][0], -65536)
        self.assertGreater(observed[0][1], 0)

    def test_root_lookup_failure_rolls_back_family_rows_and_stamp(self) -> None:
        class FailingConnection:
            def __init__(self, inner):
                self.inner = inner
                self.rollback_called = False

            def execute(self, sql, params=()):
                cursor = self.inner.execute(sql, params)
                if "SELECT session, root FROM session_family" not in sql:
                    return cursor

                def interrupted():
                    iterator = iter(cursor)
                    try:
                        yield next(iterator)
                    except StopIteration:
                        pass
                    raise sqlite3.OperationalError("injected root lookup failure")

                return interrupted()

            def rollback(self):
                self.rollback_called = True
                return self.inner.rollback()

            def __getattr__(self, name):
                return getattr(self.inner, name)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_messages(root)
            self._publish_families(root, [
                {"session": "a"},
                {"session": "b", "parent": "a"},
            ], "2:first")
            patches = self._paths(root)
            with patches[0], patches[1], patches[2], patches[3], \
                    patches[4], patches[5], patches[6]:
                corpusdb._build(corpusdb.DB_PATH, corpusdb._stamp())
                with closing(sqlite3.connect(corpusdb.DB_PATH)) as db:
                    before_rows = db.execute(
                        "SELECT session,root FROM session_family "
                        "ORDER BY session").fetchall()
                    before_meta = dict(db.execute(
                        "SELECT key,value FROM meta "
                        "WHERE key IN ('family_stamp','stamp')"))
                self._publish_families(root, [
                    {"session": "a", "parent": "other"},
                    {"session": "b", "parent": "other"},
                ], "2:second")
                corpusdb.CHANGED_PATH.write_text(
                    "a\nb\n", encoding="utf-8")
                real_connect = sqlite3.connect
                failing = FailingConnection(real_connect(corpusdb.DB_PATH))
                with mock.patch.object(
                        corpusdb.sqlite3, "connect", return_value=failing):
                    self.assertIsNone(corpusdb._incremental(corpusdb._stamp()))
                self.assertTrue(failing.rollback_called)
                with closing(real_connect(corpusdb.DB_PATH)) as db:
                    self.assertEqual(
                        db.execute(
                            "SELECT session,root FROM session_family "
                            "ORDER BY session").fetchall(),
                        before_rows,
                    )
                    self.assertEqual(
                        dict(db.execute(
                            "SELECT key,value FROM meta "
                            "WHERE key IN ('family_stamp','stamp')")),
                        before_meta,
                    )

    def test_malformed_session_row_never_publishes_partial_families(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._publish_families(root, [{"session": "root"}])
            (root / "sessions.jsonl").write_text(
                '{"session":"root"}\nnot-json\n', encoding="utf-8")
            with _data_dir(root):
                snapshot = corpusdb._read_session_families()
            self.assertIsNone(snapshot.source_stamp)
            self.assertEqual(snapshot.sessions, frozenset())
            self.assertEqual(snapshot.parents, {})

    def test_unsorted_session_publication_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._publish_families(
                root, [{"session": "b"}, {"session": "a"}])
            (root / "sessions.jsonl").write_text(
                '{"session":"b"}\n{"session":"a"}\n', encoding="utf-8")
            with _data_dir(root):
                snapshot = corpusdb._read_session_families()
            self.assertIsNone(snapshot.source_stamp)
            self.assertFalse(snapshot.sessions)

    def test_invalid_utf8_session_publication_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._publish_families(root, [{"session": "a"}])
            (root / "sessions.jsonl").write_bytes(b'{"session":"a"}\\n\xff')
            with _data_dir(root):
                snapshot = corpusdb._read_session_families()
            self.assertIsNone(snapshot.source_stamp)
            self.assertFalse(snapshot.sessions)

    def test_moving_session_publication_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sessions = root / "sessions.jsonl"
            self._publish_families(root, [{"session": "root"}])
            stat = sessions.stat()
            before = (
                stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
                stat.st_dev, stat.st_ino,
            )
            moved = (
                stat.st_size + 1, stat.st_mtime_ns + 1, stat.st_ctime_ns + 1,
                stat.st_dev, stat.st_ino,
            )
            with _data_dir(root), \
                    mock.patch.object(
                        corpusdb, "INGEST_SIG_PATH", root / ".ingest.sig"), \
                    mock.patch.object(
                        session_context, "_session_family_file_identity",
                        side_effect=(before, moved) * 3):
                snapshot = corpusdb._read_session_families()
            self.assertIsNone(snapshot.source_stamp)
            self.assertFalse(snapshot.sessions)

    def test_malformed_census_cannot_delete_indexed_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_messages(root)
            sessions = root / "sessions.jsonl"
            self._publish_families(
                root, [{"session": "a"}, {"session": "b"}])
            patches = self._paths(root)
            with patches[0], patches[1], patches[2], patches[3], \
                    patches[4], patches[5], patches[6]:
                corpusdb._build(corpusdb.DB_PATH, corpusdb._stamp())
                sessions.write_text(
                    '{"session":"a"}\nnot-json\n', encoding="utf-8")
                corpusdb.CHANGED_PATH.write_text("", encoding="utf-8")
                self.assertIsNone(corpusdb._incremental(corpusdb._stamp()))
                with closing(sqlite3.connect(corpusdb.DB_PATH)) as db:
                    self.assertEqual(
                        {row[0] for row in db.execute(
                            "SELECT DISTINCT session FROM msgs")},
                        {"a", "b"},
                    )
                with self.assertRaises(corpusdb._SourceMoved):
                    corpusdb._build(root / "invalid.db", corpusdb._stamp())

    def test_valid_prefix_cannot_delete_omitted_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_messages(root)
            self._publish_families(
                root, [{"session": "a"}, {"session": "b"}])
            patches = self._paths(root)
            with patches[0], patches[1], patches[2], patches[3], \
                    patches[4], patches[5], patches[6]:
                corpusdb._build(corpusdb.DB_PATH, corpusdb._stamp())
                (root / "sessions.jsonl").write_text(
                    '{"session":"a"}\n', encoding="utf-8")
                corpusdb.CHANGED_PATH.write_text("", encoding="utf-8")
                self.assertIsNone(corpusdb._incremental(corpusdb._stamp()))
                with closing(sqlite3.connect(corpusdb.DB_PATH)) as db:
                    self.assertEqual(
                        {row[0] for row in db.execute(
                            "SELECT DISTINCT session FROM msgs")},
                        {"a", "b"},
                    )

    def test_same_signature_census_damage_cannot_reconcile(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_messages(root)
            self._publish_families(root, [{"session": "a"}])
            committed = common.session_family_source_stamp(root)
            (root / "sessions.jsonl").write_text(
                '{"session":"b"}\n', encoding="utf-8")
            self.assertEqual(common.session_family_source_stamp(root), committed)
            self.assertIsNone(common.read_session_family_census(root))
            self.assertEqual(
                common.transcript_generation(root, attempts=1)["family"],
                committed,
            )

    def test_changed_signature_commits_only_after_the_final_marker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old_rows = [{"session": "old"}]
            new_rows = [{"session": "new"}]
            self._publish_families(root, old_rows, "1:old")
            self._write_family_meta(root, new_rows, "1:new")
            self.assertIsNone(common.session_family_source_stamp(root))
            (root / "sessions.jsonl").write_text(
                '{"session":"new"}\n', encoding="utf-8")
            self.assertIsNone(common.session_family_source_stamp(root))
            (root / ".ingest.sig").write_text("1:new\n", encoding="utf-8")
            stamp = common.session_family_source_stamp(root)
            census = common.read_session_family_census(root)
            self.assertIsNotNone(stamp)
            self.assertIsNotNone(census)
            self.assertEqual(census.proof.stamp, stamp)

    def test_same_signature_meta_crash_cannot_delete_old_families(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_messages(root)
            self._publish_families(
                root, [{"session": "a"}, {"session": "b"}], "2:same")
            patches = self._paths(root)
            with patches[0], patches[1], patches[2], patches[3], \
                    patches[4], patches[5], patches[6]:
                corpusdb._build(corpusdb.DB_PATH, corpusdb._stamp())
                self._write_family_meta(
                    root, [{"session": "a"}], "2:same")
                moved_stamp = corpusdb._stamp()
                self.assertIsNone(corpusdb._incremental(moved_stamp))
                with closing(sqlite3.connect(corpusdb.DB_PATH)) as db:
                    self.assertEqual(
                        {row[0] for row in db.execute(
                            "SELECT DISTINCT session FROM msgs")},
                        {"a", "b"},
                    )

    def test_matching_proof_allows_a_legitimate_session_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_messages(root)
            self._publish_families(
                root, [{"session": "a"}, {"session": "b"}])
            patches = self._paths(root)
            with patches[0], patches[1], patches[2], patches[3], \
                    patches[4], patches[5], patches[6]:
                corpusdb._build(corpusdb.DB_PATH, corpusdb._stamp())
                (root / "messages.jsonl").write_text(
                    json.dumps({
                        "id": "codex:a:1", "session": "a",
                        "agent": "codex", "turn": 1, "text": "needle a",
                    }) + "\n",
                    encoding="utf-8",
                )
                self._publish_families(
                    root, [{"session": "a"}], "1:deleted")
                corpusdb.CHANGED_PATH.write_text("", encoding="utf-8")
                refreshed = corpusdb._incremental(corpusdb._stamp())
                self.assertIsNotNone(refreshed)
                refreshed.close()
                with closing(sqlite3.connect(corpusdb.DB_PATH)) as db:
                    self.assertEqual(
                        {row[0] for row in db.execute(
                            "SELECT DISTINCT session FROM msgs")},
                        {"a"},
                    )

    def test_non_family_rewrite_preserves_the_logical_family_stamp(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._publish_families(root, [
                {"session": "root", "n": 1},
                {"session": "child", "parent": "root", "n": 1},
            ], "2:first")
            with _data_dir(root):
                first = common.session_family_source_stamp()
            self._publish_families(root, [
                {"session": "root", "n": 99, "first_text": "changed"},
                {
                    "session": "child", "parent": "root",
                    "n": 88, "project": "moved",
                },
            ], "187:second")
            with _data_dir(root):
                second = common.session_family_source_stamp()
            self.assertEqual(first, second)

    def test_missing_census_falls_back_without_deleting_messages(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_messages(root)
            sessions = root / "sessions.jsonl"
            self._publish_families(
                root, [{"session": "a"}, {"session": "b"}])
            patches = self._paths(root)
            with patches[0], patches[1], patches[2], patches[3], \
                    patches[4], patches[5], patches[6]:
                corpusdb._build(corpusdb.DB_PATH, corpusdb._stamp())
                sessions.unlink()
                corpusdb.CHANGED_PATH.write_text("", encoding="utf-8")
                self.assertIsNone(corpusdb._incremental(corpusdb._stamp()))
                with self.assertRaises(corpusdb._SourceMoved):
                    corpusdb._build(root / "rebuilt.db", corpusdb._stamp())
                with closing(sqlite3.connect(corpusdb.DB_PATH)) as db:
                    self.assertEqual(
                        {row[0] for row in db.execute(
                            "SELECT DISTINCT session FROM msgs")},
                        {"a", "b"},
                    )

    def test_schema10_requires_a_clean_digest_schema_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_messages(root)
            self._publish_families(root, [
                {"session": "a"},
                {"session": "b", "parent": "a"},
            ])
            patches = self._paths(root)
            with patches[0], patches[1], patches[2], patches[3], \
                    patches[4], patches[5], patches[6]:
                corpusdb._build(corpusdb.DB_PATH, corpusdb._stamp())
                with closing(sqlite3.connect(corpusdb.DB_PATH)) as db:
                    db.execute("DROP INDEX session_family_root")
                    db.execute("DROP TABLE session_family")
                    db.execute("DELETE FROM meta WHERE key='family_stamp'")
                    db.execute(
                        "UPDATE meta SET value='10' WHERE key='schema'")
                    db.commit()
                corpusdb.CHANGED_PATH.write_text("", encoding="utf-8")
                refreshed = corpusdb._incremental(corpusdb._stamp())
                self.assertIsNone(refreshed)
                with closing(sqlite3.connect(corpusdb.DB_PATH)) as db:
                    self.assertEqual(
                        db.execute(
                            "SELECT value FROM meta WHERE key='schema'"
                        ).fetchall(),
                        [("10",)],
                    )

    def test_family_index_serves_the_committed_snapshot_under_a_live_writer(
            self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._publish_families(root, [
                {"session": "root"},
                {"session": "child", "parent": "root"},
            ])
            path = root / "corpus.db"
            with _data_dir(root):
                stamp = common.session_family_source_stamp()
                with closing(sqlite3.connect(path)) as db:
                    db.executescript("""
                        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
                        CREATE TABLE session_family(
                            session TEXT PRIMARY KEY, root TEXT NOT NULL,
                            side INTEGER NOT NULL CHECK(side IN (0, 1))
                        ) WITHOUT ROWID;
                    """)
                    db.execute(
                        "INSERT INTO meta VALUES('family_stamp', ?)", (stamp,))
                    db.executemany(
                        "INSERT INTO session_family VALUES(?, ?, ?)",
                        (("root", "root", 0), ("child", "root", 1)))
                    db.commit()
                writer = sqlite3.connect(path, isolation_level=None)
                try:
                    writer.execute("BEGIN IMMEDIATE")
                    writer.execute(
                        "INSERT INTO session_family VALUES('twig', 'root', 1)")
                    self.assertGreater(
                        Path(f"{path}-journal").stat().st_size, 0,
                        "fixture must hold a live rollback journal")
                    with mock.patch.object(
                            session_context, "_FAMILY_INDEX_BEHIND", False):
                        snapshot = session_context._open_session_family_index()
                        self.assertIsNotNone(snapshot)
                        try:
                            self.assertEqual(
                                sorted(row[0] for row in snapshot.execute(
                                    "SELECT session FROM session_family")),
                                ["child", "root"])
                        finally:
                            snapshot.close()
                        self.assertFalse(session_context.family_index_behind())
                    writer.execute("COMMIT")
                finally:
                    writer.close()
                self.assertEqual(
                    common.indexed_family_roots(("twig",)), {"twig": "root"})

    def test_outputs_ahead_of_the_commit_marker_keep_the_committed_generation(
            self) -> None:
        from _test_support import publish_derived_generation

        rows = [
            {
                "id": f"codex:{session}:1", "session": session,
                "agent": "codex", "project": "p", "turn": 1, "ts": 1,
                "who": "user", "text": f"needle {session}",
                "parent": parent,
            }
            for session, parent in (("child", "root"), ("root", ""))
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            publish_derived_generation(
                root, rows, common, corpusdb, signature="2:committed")
            path = root / "corpus.db"
            with _data_dir(root):
                committed = common.session_family_source_stamp()
                publication = common.read_family_publication()
                self.assertEqual(
                    publication,
                    common.FamilyPublication(
                        "2:committed",
                        hashlib.sha256(b"2:committed\n").hexdigest(),
                        committed, False))
                generation = common.transcript_generation(root)
                with closing(sqlite3.connect(path)) as db:
                    db.executescript("""
                        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
                        CREATE TABLE session_family(
                            session TEXT PRIMARY KEY, root TEXT NOT NULL,
                            side INTEGER NOT NULL CHECK(side IN (0, 1))
                        ) WITHOUT ROWID;
                    """)
                    db.execute(
                        "INSERT INTO meta VALUES('family_stamp', ?)",
                        (committed,))
                    db.executemany(
                        "INSERT INTO session_family VALUES(?, ?, ?)",
                        (("root", "root", 0), ("child", "root", 1)))
                    db.commit()

                (root / "messages.jsonl").write_text(
                    "".join(json.dumps(row) + "\n" for row in rows)
                    + json.dumps({**rows[0], "session": "twig",
                                  "id": "codex:twig:1"}) + "\n",
                    encoding="utf-8")
                (root / "sessions.jsonl").write_text(
                    json.dumps({"session": "child", "parent": "root"}) + "\n"
                    + json.dumps({"session": "root"}) + "\n"
                    + json.dumps({"session": "twig", "parent": "root"}) + "\n",
                    encoding="utf-8")

                self.assertEqual(common.session_family_source_stamp(), committed)
                moving = common.read_family_publication()
                self.assertEqual(
                    moving, publication._replace(moving=True))
                with self.assertRaisesRegex(
                        common.TranscriptPublicationRace,
                        "outputs precede their ingest signature"):
                    common.transcript_generation(root, attempts=1)
                self.assertIsNone(common.strict_family_parent_map(root))
                with mock.patch.object(
                        session_context, "_FAMILY_INDEX_BEHIND", False):
                    self.assertEqual(
                        common.indexed_family_roots(("child",)),
                        {"child": "root"})
                    self.assertFalse(session_context.family_index_behind())

                publish_derived_generation(
                    root, rows + [{**rows[0], "session": "twig",
                                   "id": "codex:twig:1"}],
                    common, corpusdb, signature="3:next")
                landed = common.read_family_publication()
                self.assertEqual(landed.signature, "3:next")
                self.assertFalse(landed.moving)
                self.assertNotEqual(landed.stamp, committed)
                self.assertNotEqual(common.transcript_generation(root), generation)
                self.assertIsNone(common.indexed_family_roots(("child",)))
                with mock.patch.object(
                        session_context, "_FAMILY_INDEX_BEHIND", False):
                    self.assertEqual(
                        common.indexed_family_roots(
                            ("child",), allow_behind=True),
                        {"child": "root"})
                    self.assertTrue(session_context.family_index_behind())

    def test_family_meta_ahead_of_the_signature_reads_as_moving(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._publish_families(root, [{"session": "root"}], "2:gen0")
            self._write_family_meta(
                root, [{"session": "root"}, {"session": "twig", "parent": "root"}],
                "2:gen1")
            with _data_dir(root):
                self.assertIsNone(common.session_family_source_stamp())
                self.assertEqual(
                    common.read_family_publication(),
                    common.FamilyPublication(
                        "2:gen0", hashlib.sha256(b"2:gen0\n").hexdigest(),
                        None, True))


if __name__ == "__main__":
    unittest.main()
