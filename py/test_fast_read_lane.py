from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _test_support import daemon_spawns_allowed, isolate_data_dir


isolate_data_dir()
import common  # noqa: E402
import cli  # noqa: E402
import corpusdb  # noqa: E402
import explore  # noqa: E402
import indexd_runtime  # noqa: E402
import search  # noqa: E402
import segment_query  # noqa: E402
import semantic  # noqa: E402
import session_context  # noqa: E402
import settings  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "cli.py"
RELEASE_BIN = ROOT / "target" / "release" / (
    "agrep-rs.exe" if sys.platform == "win32" else "agrep-rs"
)
REAL_BUILD_ID = (
    indexd_runtime.derived_writer_build_id(RELEASE_BIN)
    if RELEASE_BIN.is_file() else None
)


class FastLaneTests(unittest.TestCase):
    @staticmethod
    def _reset_explore_caches() -> None:
        # These tests patch DATA_DIR beneath explore's process-wide caches.
        # Drive the central generation transition so no fixture rows escape
        # into subsequently discovered test modules.
        explore._GEN = ("fastlane-test-reset",)
        explore._freshen()

    def setUp(self) -> None:
        build_id = mock.patch.object(
            indexd_runtime, "derived_writer_build_id",
            return_value="a" * 20)
        build_id.start()
        self.addCleanup(build_id.stop)
        indexd_runtime._set_fts_delegated(False)
        indexd_runtime._clear_freshen_failure()
        self._reset_explore_caches()

    def tearDown(self) -> None:
        indexd_runtime._set_fts_delegated(False)
        indexd_runtime._clear_freshen_failure()
        self._reset_explore_caches()

    @staticmethod
    def _messages(root: Path) -> Path:
        path = root / "messages.jsonl"
        path.write_text(json.dumps({
            "id": "codex:s:1",
            "agent": "codex",
            "project": "repo",
            "session": "s",
            "turn": 1,
            "ts": 1,
            "who": "user",
            "text": "needle in the published snapshot",
        }, separators=(",", ":")) + "\n", encoding="utf-8")
        return path

    @staticmethod
    def _database(
            path: Path, stamp: str = "old", *,
            build_id: str | None = None,
            family_stamp: str | None = None,
            rows: list[tuple] | None = None) -> None:
        db = sqlite3.connect(path)
        db.executescript(corpusdb._SCHEMA_SQL)
        db.executemany(corpusdb._INS, rows if rows is not None else [(
            "s", 1, 1, "codex", "repo", "", "", "", "user",
            "needle in the published snapshot",
        )])
        db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
        db.execute(
            "INSERT INTO msgs_prose_fts(rowid, text) "
            "SELECT id, text FROM msgs WHERE who <> 'tool'")
        meta = [
            ("schema", corpusdb._SCHEMA),
            ("stamp", stamp),
            ("fts_triggers", corpusdb._TRIGGER_SCHEMA),
            ("build_id", build_id or indexd_runtime.derived_writer_build_id(
                require_binary=True)),
        ]
        if family_stamp is not None:
            meta.append(("family_stamp", family_stamp))
        db.executemany("INSERT INTO meta VALUES(?, ?)", meta)
        db.commit()
        db.close()

    @staticmethod
    def _proof_row(path: Path) -> dict:
        identity = corpusdb._proof_file_identity(path)
        size, modified_ns, changed, _device, _inode = identity
        if corpusdb._PLATFORM_NAME == "posix":
            change_token = {
                "Metadata": corpusdb._unix_change_token(changed)}
        elif corpusdb._PLATFORM_NAME == "nt":
            change_token = {
                "ContentSha256": list(
                    corpusdb._content_sha256(path, identity))}
        else:
            change_token = {"Metadata": 0}
        return {
            "name": path.name,
            "len": size,
            "modified_ns": modified_ns,
            "change_token": change_token,
            "edge_hash": corpusdb._edge_hash(path, size, identity),
        }

    def _published_jsonl_fixture(self, root: Path) -> Path:
        messages = self._messages(root)
        signature = "fastlane-fast-lane-publication"
        (root / ".ingest.sig").write_text(signature + "\n", encoding="utf-8")
        (root / "replies.jsonl").write_text("", encoding="utf-8")
        (root / "sessions.jsonl").write_text(
            json.dumps({
                "session": "s", "agent": "codex", "project": "repo",
                "first_ts": 1, "last_ts": 1, "n": 1, "parent": "",
            }, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        family_digest = common.session_family_digest([("s", "")])
        (root / common.SESSION_FAMILY_META_FILE).write_text(
            json.dumps({
                "version": common.SESSION_FAMILY_INDEX_VERSION,
                "algorithm": common.SESSION_FAMILY_DIGEST_ALGORITHM,
                "ingest_signature": signature,
                "count": 1,
                "digest": family_digest,
            }, separators=(",", ":")),
            encoding="utf-8",
        )
        for name in (
                "boundary_stats.json", ".boundary_stats.bin",
                "event_stats.json"):
            body = b"fixture" if name == ".boundary_stats.bin" else b"{}"
            (root / name).write_bytes(body)
        proof_files = [
            self._proof_row(root / name)
            for name in corpusdb._DERIVED_PROOF_NAMES
        ]
        (root / ".derived_generation.json").write_text(
            json.dumps({
                "version": corpusdb._DERIVED_PROOF_VERSION,
                "signature": signature,
                "files": proof_files,
            }, separators=(",", ":")),
            encoding="utf-8",
        )
        return messages

    def _published_fixture(self, root: Path) -> None:
        assert REAL_BUILD_ID is not None
        self._published_jsonl_fixture(root)
        with (
            mock.patch.object(common, "DATA_DIR", root),
            mock.patch.object(common, "MESSAGES_PATH", root / "messages.jsonl"),
            mock.patch.object(corpusdb, "DB_PATH", root / "corpus.db"),
            mock.patch.object(session_context, "DATA_DIR", root),
            mock.patch.object(settings, "DATA_DIR", root),
        ):
            family_stamp = common.session_family_source_stamp(root)
            self.assertIsNotNone(family_stamp)
            stamp = corpusdb._stamp()
        self._database(
            root / "corpus.db", stamp,
            build_id=REAL_BUILD_ID, family_stamp=family_stamp)
        (root / ".derived-owner.json").write_text(
            json.dumps({
                "version": 1,
                "build_id": REAL_BUILD_ID,
            }, separators=(",", ":")),
            encoding="utf-8",
        )

    def _assert_snapshot_freshness(
            self, freshness: dict, expected: str, readonly: bool) -> None:
        # deterministic under the isolated env: the readonly leg censuses the
        # drifted fixture (quantified verdict outranks the deferral); the
        # no-auto leg banned the census, so the recorded deferral is the story
        if readonly:
            self.assertEqual(freshness["code"], "index-behind")
        else:
            self.assertEqual(freshness["code"], "search-index-stale")
            self.assertIn(expected, freshness["reason"])
        self.assertTrue(freshness["may_be_stale"])
        self.assertEqual(freshness["checked"], readonly)

    @staticmethod
    def _surface_env(root: Path, *, readonly: bool) -> dict[str, str]:
        # isolated like test_perf_budgets._env: the drift census must read
        # the fixture's stores, never the developer box's real agent history
        home = root / "home"
        home.mkdir(exist_ok=True)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("AGREP_") and k not in {
                   "APPDATA", "LOCALAPPDATA", "USERPROFILE",
                   "XDG_CONFIG_HOME", "XDG_DATA_HOME", "CLINE_DIR"}}
        env.update({
            "HOME": os.fspath(home), "USERPROFILE": os.fspath(home),
            "XDG_CONFIG_HOME": os.fspath(home / ".config"),
            "XDG_DATA_HOME": os.fspath(home / ".local" / "share"),
            "AGREP_HOME": os.fspath(home),
            "AGREP_DATA_DIR": os.fspath(root),
            "AGREP_DATA_DIR_SOURCE": "test",
            "AGREP_RS_BIN": os.fspath(RELEASE_BIN),
            "AGREP_PROFILE": "classic",
        })
        if readonly:
            env["AGREP_DATA_READONLY"] = os.fspath(root)
        return env

    def test_allow_stale_serves_compatible_db_without_any_writer_primitive(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-fastlane-fast-db-") as raw:
            root = Path(raw)
            messages = self._messages(root)
            db_path = root / "corpus.db"
            self._database(db_path)
            before = (db_path.read_bytes(), db_path.stat().st_mtime_ns)
            stderr = io.StringIO()
            with (
                mock.patch.object(common, "DATA_DIR", root),
                mock.patch.object(common, "MESSAGES_PATH", messages),
                mock.patch.object(corpusdb, "DB_PATH", db_path),
                mock.patch.object(corpusdb, "_trigram_ok", return_value=True),
                mock.patch.object(corpusdb, "_stamp", return_value="new"),
                mock.patch.object(
                    corpusdb, "_derived_write_ownership",
                    return_value=corpusdb._DerivedWriteOwnership("current")),
                mock.patch.object(
                    corpusdb, "purge_legacy_build_temps",
                    side_effect=AssertionError("reader swept derived artifacts")),
                mock.patch.object(
                    corpusdb, "_valid_db",
                    side_effect=AssertionError("reader entered mutable validation")),
                mock.patch.object(
                    corpusdb, "_incremental",
                    side_effect=AssertionError("reader incremented FTS")),
                mock.patch.object(
                    corpusdb, "_build",
                    side_effect=AssertionError("reader rebuilt FTS")),
                mock.patch.object(
                    corpusdb, "_connect_read_alias",
                    side_effect=AssertionError("reader cloned SQLite")),
                mock.patch.object(
                    corpusdb, "_copy_sqlite_file",
                    side_effect=AssertionError("reader copied SQLite")),
                mock.patch.object(
                    corpusdb.os, "link",
                    side_effect=AssertionError("reader hardlinked SQLite")),
                mock.patch.object(
                    common, "IndexLock",
                    side_effect=AssertionError("reader entered writer lock")),
                contextlib.redirect_stderr(stderr),
            ):
                indexd_runtime._clear_freshen_failure()
                got = corpusdb.connect(quiet=True, allow_stale=True)
                self.assertIsNotNone(got)
                self.assertEqual(
                    got.execute("SELECT text FROM msgs").fetchone()[0],
                    "needle in the published snapshot")
                self.assertEqual(
                    Path(got.execute(
                        "PRAGMA database_list").fetchone()[2]).resolve(),
                    db_path.resolve())
                got.close()
                with mock.patch.object(
                        indexd_runtime, "indexing_failure", return_value=None):
                    disclosure = indexd_runtime.machine_freshness(checked=True)

            self.assertEqual(
                (db_path.read_bytes(), db_path.stat().st_mtime_ns), before)
            # The read lane records; only freshness_story renders
            self.assertNotIn("may be stale", stderr.getvalue())
            self.assertIn("published search snapshot", disclosure["reason"])
            self.assertEqual(disclosure["code"], "search-index-stale")

    def test_live_sqlite_contention_is_not_reported_as_foreign_owner(
            self) -> None:
        if REAL_BUILD_ID is None:
            self.skipTest("release ingest binary is unavailable")
        for dirty in (False, True):
            with self.subTest(dirty=dirty), tempfile.TemporaryDirectory(
                    prefix="agrep-fastlane-live-lock-") as raw:
                root = Path(raw)
                self._published_fixture(root)
                env = self._surface_env(root, readonly=False)
                locker = sqlite3.connect(root / "corpus.db", timeout=0)
                try:
                    locker.execute("BEGIN EXCLUSIVE")
                    if dirty:
                        locker.execute(
                            "UPDATE msgs SET text = text || ' pending' "
                            "WHERE id = 1")
                    completed = subprocess.run(
                        [sys.executable, os.fspath(CLI), "search", "needle",
                         "--json", "--lexical", "--no-auto", "--self",
                         "--who", "user"],
                        env=env, text=True, capture_output=True, timeout=10)
                finally:
                    locker.rollback()
                    locker.close()

                self.assertEqual(completed.returncode, 0, completed.stderr)
                rows = [
                    json.loads(line)
                    for line in completed.stdout.splitlines()
                ]
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0]["kind"], "agrep-meta")
                self.assertEqual(rows[1]["snippet"],
                                 "needle in the published snapshot")
                self.assertNotEqual(
                    rows[0]["freshness"].get("code"),
                    indexd_runtime.DERIVED_STORE_OWNER_CODE)
                self.assertNotIn(
                    "belongs to another agrep build", completed.stderr)
                self.assertFalse(
                    (root / ".corpusdb-rebuild").exists())

    def test_allow_stale_uses_direct_snapshot_when_fts_is_absent(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-fastlane-fast-jsonl-") as raw:
            root = Path(raw)
            messages = self._messages(root)
            db_path = root / "corpus.db"
            stderr = io.StringIO()
            with (
                mock.patch.object(common, "DATA_DIR", root),
                mock.patch.object(common, "MESSAGES_PATH", messages),
                mock.patch.object(corpusdb, "DB_PATH", db_path),
                mock.patch.object(corpusdb, "_trigram_ok", return_value=True),
                mock.patch.object(corpusdb, "_stamp", return_value="new"),
                mock.patch.object(
                    corpusdb, "purge_legacy_build_temps",
                    side_effect=AssertionError("reader swept derived artifacts")),
                mock.patch.object(
                    corpusdb, "_incremental",
                    side_effect=AssertionError("reader incremented FTS")),
                mock.patch.object(
                    corpusdb, "_build",
                    side_effect=AssertionError("reader rebuilt FTS")),
                mock.patch.object(
                    common, "IndexLock",
                    side_effect=AssertionError("reader entered writer lock")),
                contextlib.redirect_stderr(stderr),
            ):
                indexd_runtime._clear_freshen_failure()
                self.assertIsNone(corpusdb.connect(allow_stale=True))
                with mock.patch.object(
                        indexd_runtime, "indexing_failure", return_value=None):
                    disclosure = indexd_runtime.machine_freshness(checked=True)

            self.assertFalse(db_path.exists())
            # The read lane records; only freshness_story renders
            self.assertNotIn("may be stale", stderr.getvalue())
            # no anchor and no cache in this fixture: the truthful recorded
            # cause is the ownership refusal, not the served-lane clause the
            # old glued composition always appended
            self.assertIn(
                "derived-store ownership is not established",
                disclosure["reason"])

    def test_status_semantic_probe_never_enters_runtime_or_fts_lanes(
        self) -> None:
        with (
            mock.patch(
                "importlib.util.find_spec",
                side_effect=AssertionError(
                    "routine status performed dependency discovery")) as find_spec,
            mock.patch.object(
                semantic, "embedding_coherence",
                side_effect=AssertionError("routine status imported semantic")),
            mock.patch.object(
                semantic, "read_embed_state",
                side_effect=AssertionError("routine status read embed state")),
            mock.patch.object(
                semantic, "embed_running",
                side_effect=AssertionError("routine status inspected workers")),
            mock.patch.object(
                semantic, "ensure_fresh_async",
                side_effect=AssertionError("routine status started work")),
            mock.patch.object(
                corpusdb, "connect",
                side_effect=AssertionError("routine status opened FTS")) as connect,
        ):
            status = cli._status_semantic()

        self.assertEqual(
            status["semantic_status"],
            "not-inspected")
        self.assertIsNone(status["semantic_deps"])
        self.assertFalse(status["semantic_verified"])
        self.assertNotIn("semantic_coverage", status)
        find_spec.assert_not_called()
        connect.assert_not_called()

    def test_count_spawns_daemon_and_scans_current_snapshot_without_rebuild(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-fastlane-count-") as raw:
            root = Path(raw)
            messages = self._published_jsonl_fixture(root)
            db_path = root / "corpus.db"
            self._database(db_path)
            before = db_path.read_bytes()
            stdout, stderr = io.StringIO(), io.StringIO()
            explore._GEN = None
            with (
                mock.patch.object(common, "DATA_DIR", root),
                mock.patch.object(common, "MESSAGES_PATH", messages),
                mock.patch.object(
                    common, "INGEST_SIG_PATH", root / ".ingest.sig"),
                mock.patch.object(
                    common, "ingest_bin", return_value=messages),
                mock.patch.object(corpusdb, "DB_PATH", db_path),
                mock.patch.object(corpusdb, "_trigram_ok", return_value=True),
                mock.patch.object(corpusdb, "_stamp", return_value="new"),
                mock.patch.object(
                    corpusdb, "_derived_write_ownership",
                    return_value=corpusdb._DerivedWriteOwnership("current")),
                mock.patch.object(
                    indexd_runtime, "freshener_alive", return_value=False),
                daemon_spawns_allowed(),
                mock.patch.object(
                    indexd_runtime, "_spawn_indexd",
                    return_value=indexd_runtime._IndexdSpawnResult.IN_FLIGHT,
                ) as spawn,
                mock.patch.object(
                    indexd_runtime, "build_index",
                    side_effect=AssertionError("porcelain ran inline ingest")),
                mock.patch.object(
                    corpusdb, "_incremental",
                    side_effect=AssertionError("porcelain incremented FTS")),
                mock.patch.object(
                    corpusdb, "_build",
                    side_effect=AssertionError("porcelain rebuilt FTS")),
                mock.patch.object(
                    common, "IndexLock",
                    side_effect=AssertionError("porcelain entered writer lock")),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                rc = search.main([
                    "needle", "-c", "--lexical", "--who", "user"])

            self.assertEqual(rc, 0, stderr.getvalue())
            self.assertEqual(stdout.getvalue(), "1\n")
            # the record keeps its one cause: the owned delegated constant
            self.assertIn(
                indexd_runtime.REFRESH_DELEGATED_REASON, stderr.getvalue())
            spawn.assert_called_once()
            self.assertEqual(db_path.read_bytes(), before)

    def test_count_discloses_delegation_when_db_matches_published_messages(
            self) -> None:
        """Internal DB coherence must not hide staleness relative to live stores."""
        with tempfile.TemporaryDirectory(prefix="agrep-fastlane-count-current-") as raw:
            root = Path(raw)
            messages = self._published_jsonl_fixture(root)
            db_path = root / "corpus.db"
            self._database(db_path, stamp="published")
            before = db_path.read_bytes()
            stdout, stderr = io.StringIO(), io.StringIO()
            explore._GEN = None
            with (
                mock.patch.object(common, "DATA_DIR", root),
                mock.patch.object(common, "MESSAGES_PATH", messages),
                mock.patch.object(
                    common, "INGEST_SIG_PATH", root / ".ingest.sig"),
                mock.patch.object(
                    common, "ingest_bin", return_value=messages),
                mock.patch.object(corpusdb, "DB_PATH", db_path),
                mock.patch.object(corpusdb, "_trigram_ok", return_value=True),
                mock.patch.object(
                    corpusdb, "_stamp", return_value="published"),
                mock.patch.object(
                    corpusdb, "_derived_write_ownership",
                    return_value=corpusdb._DerivedWriteOwnership("current")),
                mock.patch.object(
                    indexd_runtime, "freshener_alive", return_value=False),
                daemon_spawns_allowed(),
                mock.patch.object(
                    indexd_runtime, "_spawn_indexd",
                    return_value=indexd_runtime._IndexdSpawnResult.IN_FLIGHT,
                ) as spawn,
                mock.patch.object(
                    indexd_runtime, "build_index",
                    side_effect=AssertionError("porcelain ran inline ingest")),
                mock.patch.object(
                    corpusdb, "_incremental",
                    side_effect=AssertionError("porcelain incremented FTS")),
                mock.patch.object(
                    corpusdb, "_build",
                    side_effect=AssertionError("porcelain rebuilt FTS")),
                mock.patch.object(
                    common, "IndexLock",
                    side_effect=AssertionError("porcelain entered writer lock")),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                rc = search.main([
                    "needle", "-c", "--lexical", "--who", "user"])
                with mock.patch.object(
                        indexd_runtime, "indexing_failure", return_value=None):
                    disclosure = indexd_runtime.machine_freshness(checked=True)

            self.assertEqual(rc, 0, stderr.getvalue())
            self.assertEqual(stdout.getvalue(), "1\n")
            self.assertIn(
                indexd_runtime.REFRESH_DELEGATED_REASON, stderr.getvalue())
            self.assertEqual(disclosure["code"], "index-behind")
            self.assertFalse(disclosure["failing"])
            self.assertEqual(
                disclosure["cause"], "publication-in-progress")
            self.assertTrue(disclosure["may_be_stale"])
            spawn.assert_called_once()
            self.assertEqual(db_path.read_bytes(), before)

    def test_source_publish_window_keeps_machine_search_on_last_good_db(
            self) -> None:
        if not corpusdb._trigram_ok():
            self.skipTest("SQLite trigram FTS5 is unavailable")
        with tempfile.TemporaryDirectory(
                prefix="agrep-fastlane-source-publish-") as raw:
            root = Path(raw)
            messages = root / "messages.jsonl"
            staged = root / "messages.jsonl.staged"
            db_path = root / "corpus.db"
            query = "publishneedle"

            def row(session: str, turn: int) -> dict:
                return {
                    "id": f"codex:{session}:{turn}", "agent": "codex",
                    "project": "repo", "session": session, "turn": turn,
                    "ts": turn, "who": "user",
                    "text": f"{query} from {session}",
                }

            old_rows = [row(f"old-{index}", index) for index in range(1, 4)]
            messages.write_text(
                "".join(json.dumps(item) + "\n" for item in old_rows),
                encoding="utf-8")
            with (
                mock.patch.object(common, "DATA_DIR", root),
                mock.patch.object(common, "MESSAGES_PATH", messages),
                mock.patch.object(corpusdb, "DB_PATH", db_path),
            ):
                old_stamp = corpusdb._stamp()
            self._database(
                db_path, stamp=old_stamp, rows=[(
                    item["session"], item["turn"], item["ts"],
                    item["agent"], item["project"], "", "", "",
                    item["who"], item["text"],
                ) for item in old_rows])
            staged.write_text(json.dumps(row("new-only", 9)) + "\n",
                              encoding="utf-8")
            real_stamp = corpusdb._stamp

            def publish_source() -> str:
                if staged.exists():
                    os.replace(staged, messages)
                return real_stamp()

            freshness = {
                "state": "unchecked", "failing": False, "checked": False,
                "may_be_stale": True, "code": "freshness-unchecked",
            }
            with (
                mock.patch.object(common, "DATA_DIR", root),
                mock.patch.object(common, "MESSAGES_PATH", messages),
                mock.patch.object(corpusdb, "DB_PATH", db_path),
                mock.patch.object(corpusdb, "_trigram_ok", return_value=True),
                mock.patch.object(corpusdb, "_stamp", side_effect=publish_source),
                mock.patch.object(
                    corpusdb, "_derived_write_ownership",
                    return_value=corpusdb._DerivedWriteOwnership("current")),
                mock.patch.object(indexd_runtime, "ensure_index", return_value=True),
                mock.patch.object(
                    indexd_runtime, "machine_freshness", return_value=freshness),
                mock.patch.object(
                    indexd_runtime, "agent_freshness_notice", return_value=""),
                mock.patch.object(
                    corpusdb, "machine_freshness_fields",
                    side_effect=lambda value, **_kwargs: {
                        "freshness": value, "corpus_age_s": 0.0}),
                mock.patch.object(common, "in_agent_context", return_value=False),
            ):
                result = search.run_query(
                    query, mode="keyword", limit=40, exact_totals=False)
                self.assertEqual(result["engine"], "corpusdb")
                self.assertEqual(result["total"], 3)
                self.assertTrue(result["totals_exact"])
                self.assertEqual(len(result["hits"]), 3)

                explore._GEN = None
                narrowed = explore.keyword_search(query, 40)
                self.assertEqual(narrowed["total"], 1)

                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), \
                        contextlib.redirect_stderr(stderr):
                    json_rc = search.main([
                        query, "--json", "--lexical", "--no-auto"])
                rendered = [json.loads(line) for line in stdout.getvalue().splitlines()]
                self.assertEqual(json_rc, 0, stderr.getvalue())
                self.assertEqual(len(rendered), 4)
                meta, hits = rendered[0], rendered[1:]
                self.assertEqual(meta["kind"], "agrep-meta")
                self.assertEqual(
                    {item["session"] for item in hits},
                    {"old-1", "old-2", "old-3"})
                self.assertEqual(meta["completeness"]["total"], 3)
                self.assertEqual(
                    meta["completeness"]["total_basis"], "exact")
                self.assertTrue(all("completeness" not in item for item in hits))

                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), \
                        contextlib.redirect_stderr(stderr):
                    count_rc = search.main([
                        query, "-c", "--lexical", "--no-auto"])
                self.assertEqual(count_rc, 2, stderr.getvalue())
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(
                    stderr.getvalue(),
                    "the published transcript snapshot could not be verified\n")

    def test_bounded_headline_count_stays_on_one_read_generation(self) -> None:
        if not corpusdb._trigram_ok():
            self.skipTest("SQLite trigram FTS5 is unavailable")

        class TrackedConnection(sqlite3.Connection):
            closed = False

            def close(self) -> None:
                self.closed = True
                super().close()

        with tempfile.TemporaryDirectory(
                prefix="agrep-fastlane-query-snapshot-") as raw:
            root = Path(raw)
            db_path = root / "corpus.db"
            query = "publishneedle"
            now_ms = int(search.time.time() * 1000)
            rows = []
            for index in range(1_200):
                session = f"old-{index:04d}"
                text = ((query + " ") * 8) + session
                rows.append((
                    session, index, now_ms - index * 21_600_000,
                    "codex", "repo", "", "", "", "agent", text,
                ))
            self._database(db_path, rows=rows)
            setup = sqlite3.connect(db_path)
            try:
                self.assertEqual(
                    setup.execute("PRAGMA journal_mode=WAL").fetchone()[0],
                    "wal")
                setup.executescript(corpusdb._TRIGGERS_SQL)
                setup.commit()
            finally:
                setup.close()

            writer = sqlite3.connect(db_path, timeout=2.0)
            readers: list[TrackedConnection] = []
            observed = {}
            real_bounded = search._bounded_keyword_rows

            def connect(**_kwargs):
                reader = sqlite3.connect(
                    db_path, timeout=2.0, factory=TrackedConnection)
                readers.append(reader)
                return reader

            def bounded_then_publish(db, *args, **kwargs):
                bounded = real_bounded(db, *args, **kwargs)
                self.assertIsNotNone(bounded)
                observed.update(
                    floor=bounded["total"], exact=bounded["totals_exact"],
                    transaction=db.in_transaction)
                writer.execute("DELETE FROM msgs WHERE id > 8")
                writer.commit()
                return bounded

            try:
                with (
                    mock.patch.object(corpusdb, "connect", side_effect=connect),
                    mock.patch.object(
                        search, "_bounded_keyword_rows",
                        side_effect=bounded_then_publish),
                    mock.patch.object(
                        search, "_BOUNDED_KEYWORD_MIN_CANDIDATES", 0),
                    mock.patch.object(search, "_BOUNDARY_REFINE_POOL", 68),
                    mock.patch.object(search, "_boundary_batch", return_value=False),
                ):
                    result = search.run_query(
                        query, mode="keyword", limit=40,
                        exact_totals=False, allow_fallback=False)

                self.assertEqual(observed, {
                    "floor": 68, "exact": False, "transaction": True})
                self.assertEqual(result["engine"], "corpusdb")
                self.assertEqual(result["total"], 1_200)
                self.assertTrue(result["totals_exact"])
                self.assertEqual(len(result["hits"]), 40)
                self.assertTrue(all(
                    hit["session"].startswith("old-")
                    for hit in result["hits"]))
                self.assertEqual(
                    writer.execute("SELECT count(*) FROM msgs").fetchone()[0],
                    8)
                self.assertEqual(len(readers), 1)
                self.assertTrue(readers[0].closed)
            finally:
                writer.close()

    def test_count_with_no_fts_scans_jsonl_and_leaves_db_absent(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-fastlane-count-jsonl-") as raw:
            root = Path(raw)
            messages = self._published_jsonl_fixture(root)
            db_path = root / "corpus.db"
            stdout, stderr = io.StringIO(), io.StringIO()
            explore._GEN = None
            with (
                mock.patch.object(common, "DATA_DIR", root),
                mock.patch.object(common, "MESSAGES_PATH", messages),
                mock.patch.object(
                    common, "INGEST_SIG_PATH", root / ".ingest.sig"),
                mock.patch.object(
                    common, "ingest_bin", return_value=messages),
                mock.patch.object(corpusdb, "DB_PATH", db_path),
                mock.patch.object(corpusdb, "_trigram_ok", return_value=True),
                mock.patch.object(corpusdb, "_stamp", return_value="new"),
                mock.patch.object(common, "setting", return_value="off"),
                mock.patch.object(
                    indexd_runtime, "freshener_alive", return_value=False),
                daemon_spawns_allowed(),
                mock.patch.object(
                    indexd_runtime, "_spawn_indexd",
                    return_value=indexd_runtime._IndexdSpawnResult.IN_FLIGHT,
                ) as spawn,
                mock.patch.object(
                    indexd_runtime, "build_index",
                    side_effect=AssertionError("porcelain ran inline ingest")),
                mock.patch.object(
                    corpusdb, "_incremental",
                    side_effect=AssertionError("porcelain incremented FTS")),
                mock.patch.object(
                    corpusdb, "_build",
                    side_effect=AssertionError("porcelain rebuilt FTS")),
                mock.patch.object(
                    common, "IndexLock",
                    side_effect=AssertionError("porcelain entered writer lock")),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                rc = search.main(["needle", "-c", "--lexical"])

            self.assertEqual(rc, 0, stderr.getvalue())
            self.assertEqual(stdout.getvalue(), "1\n")
            # this first-run fixture has no anchor to adopt and the spawn is
            # mocked, so the record truthfully carries the ownership refusal
            self.assertIn(
                "derived-store ownership is not established",
                stderr.getvalue())
            spawn.assert_called_once()
            self.assertFalse(db_path.exists())

    def test_count_without_trigram_discloses_delegated_direct_snapshot(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-fastlane-count-no-trigram-") as raw:
            root = Path(raw)
            messages = self._published_jsonl_fixture(root)
            db_path = root / "corpus.db"
            stdout, stderr = io.StringIO(), io.StringIO()
            explore._GEN = None
            with (
                mock.patch.object(common, "DATA_DIR", root),
                mock.patch.object(common, "MESSAGES_PATH", messages),
                mock.patch.object(
                    common, "INGEST_SIG_PATH", root / ".ingest.sig"),
                mock.patch.object(
                    common, "ingest_bin", return_value=messages),
                mock.patch.object(corpusdb, "DB_PATH", db_path),
                mock.patch.object(corpusdb, "_trigram_ok", return_value=False),
                mock.patch.object(common, "setting", return_value="off"),
                mock.patch.object(
                    corpusdb, "_derived_write_ownership",
                    return_value=corpusdb._DerivedWriteOwnership("current")),
                mock.patch.object(
                    indexd_runtime, "freshener_alive", return_value=False),
                daemon_spawns_allowed(),
                mock.patch.object(
                    indexd_runtime, "_spawn_indexd",
                    return_value=indexd_runtime._IndexdSpawnResult.IN_FLIGHT,
                ) as spawn,
                mock.patch.object(
                    indexd_runtime, "build_index",
                    side_effect=AssertionError("porcelain ran inline ingest")),
                mock.patch.object(
                    corpusdb, "_incremental",
                    side_effect=AssertionError("porcelain incremented FTS")),
                mock.patch.object(
                    corpusdb, "_build",
                    side_effect=AssertionError("porcelain rebuilt FTS")),
                mock.patch.object(
                    common, "IndexLock",
                    side_effect=AssertionError("porcelain entered writer lock")),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                rc = search.main(["needle", "-c", "--lexical"])
                with mock.patch.object(
                        indexd_runtime, "indexing_failure", return_value=None):
                    disclosure = indexd_runtime.machine_freshness(checked=True)

            self.assertEqual(rc, 0, stderr.getvalue())
            self.assertEqual(stdout.getvalue(), "1\n")
            self.assertIn(
                indexd_runtime.REFRESH_DELEGATED_REASON, stderr.getvalue())
            self.assertEqual(disclosure["code"], "index-behind")
            self.assertFalse(disclosure["failing"])
            self.assertEqual(
                disclosure["cause"], "publication-in-progress")
            self.assertTrue(disclosure["may_be_stale"])
            spawn.assert_called_once()
            self.assertFalse(db_path.exists())

    @unittest.skipUnless(
        REAL_BUILD_ID is not None,
        "fresh-process surface proof requires the release ingest binary",
    )
    def test_fresh_process_surfaces_disclose_readonly_and_no_auto_snapshots(
            self) -> None:
        surfaces = {
            "count": ["search", "needle", "-c", "--lexical"],
            "flat": ["search", "needle", "--flat", "--lexical"],
            "json": ["search", "needle", "--json", "--lexical"],
            "recall": [
                "recall", "needle", "--json", "--lexical", "--budget", "0",
            ],
        }
        for readonly in (True, False):
            policy = "readonly" if readonly else "no-auto"
            with (
                self.subTest(policy=policy),
                tempfile.TemporaryDirectory(
                    prefix=f"agrep-fastlane-{policy}-surface-") as raw,
            ):
                root = Path(raw)
                self._published_fixture(root)
                db_path = root / "corpus.db"
                before = (db_path.read_bytes(), db_path.stat().st_mtime_ns)
                for surface_name, base_argv in surfaces.items():
                    argv = list(base_argv)
                    if not readonly:
                        argv.append("--no-auto")
                    completed = subprocess.run(
                        [sys.executable, os.fspath(CLI), *argv],
                        cwd=ROOT,
                        env=self._surface_env(root, readonly=readonly),
                        capture_output=True,
                        text=True,
                        timeout=20,
                        check=False,
                    )
                    with self.subTest(
                            policy=policy, surface=surface_name):
                        self.assertEqual(
                            completed.returncode, 0, completed.stderr)
                        expected = (
                            "AGREP_DATA_READONLY"
                            if readonly else "--no-auto")
                        fresh_lines = [
                            line for line in completed.stderr.splitlines()
                            if "may be stale" in line or "behind" in line]
                        # Never more than the one story line
                        self.assertLessEqual(len(fresh_lines), 1, fresh_lines)
                        if readonly:
                            # the census owns the verdict: a verified-current
                            # readonly box says nothing rather than hedging
                            self.assertNotIn(
                                "history may be stale: AGREP_DATA_READONLY",
                                completed.stderr)
                        else:
                            # the hedge names its one cause (law 5); which
                            # lane served is the engine fact, not this line's
                            self.assertIn(
                                "history may be stale", completed.stderr)
                            self.assertIn(expected, completed.stderr)
                        self.assertNotIn(
                            "delegat", completed.stderr.lower())
                        if surface_name == "count":
                            self.assertEqual(completed.stdout, "1\n")
                        elif surface_name == "flat":
                            self.assertIn(
                                "needle in the published snapshot",
                                completed.stdout)
                        elif surface_name == "json":
                            rows = [
                                json.loads(line)
                                for line in completed.stdout.splitlines()
                                if line.strip()
                            ]
                            self.assertTrue(rows)
                            freshness = rows[0]["freshness"]
                            self._assert_snapshot_freshness(
                                freshness, expected, readonly)
                        else:
                            payload = json.loads(completed.stdout)
                            self._assert_snapshot_freshness(
                                payload["freshness"], expected, readonly)
                self.assertEqual(
                    (db_path.read_bytes(), db_path.stat().st_mtime_ns),
                    before,
                )
                self.assertFalse(any(
                    path.name.startswith(".indexd")
                    for path in root.iterdir()
                ))

    def test_semantic_ref_resolution_never_enters_the_fts_writer_lane(
            self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-fastlane-segment-") as raw:
            root = Path(raw)
            messages = self._messages(root)
            db_path = root / "corpus.db"
            self._database(db_path)
            before = (db_path.read_bytes(), db_path.stat().st_mtime_ns)
            refs = object.__new__(segment_query.SegmentRefStore)
            refs.rows = 1
            refs.live = segment_query.np.asarray([True])
            refs.assert_current = lambda: None
            refs.corpus_connect = refs._connect_corpus
            refs.corpus = None
            refs._corpus_has_session_index = None
            refs._corpus_has_digest = None
            refs._q8_eligibility_cache = {}
            refs._integrity_drops = set()
            refs._absent_drops = set()
            refs._resolve_considered = 0
            record = {
                "mid": "codex:s:1",
                "text_hash": segment_query._text_hash(
                    "needle in the published snapshot"),
                "agent": "codex",
                "project": "repo",
                "session": "s",
                "ts": 1,
                "turn": 1,
                "who": "user",
                "model": "",
                "model_source": "unknown",
                "family_id": 1,
                "side": False,
            }
            with (
                mock.patch.object(common, "DATA_DIR", root),
                mock.patch.object(common, "MESSAGES_PATH", messages),
                mock.patch.object(corpusdb, "DB_PATH", db_path),
                mock.patch.object(corpusdb, "_trigram_ok", return_value=True),
                mock.patch.object(corpusdb, "_stamp", return_value="new"),
                mock.patch.object(
                    corpusdb, "_derived_write_ownership",
                    return_value=corpusdb._DerivedWriteOwnership("current")),
                mock.patch.object(
                    refs, "_metadata", return_value={0: record}),
                mock.patch.object(
                    corpusdb, "purge_legacy_build_temps",
                    side_effect=AssertionError("semantic reader swept")),
                mock.patch.object(
                    corpusdb, "_valid_db",
                    side_effect=AssertionError("semantic reader validated")),
                mock.patch.object(
                    corpusdb, "_incremental",
                    side_effect=AssertionError("semantic reader incremented")),
                mock.patch.object(
                    corpusdb, "_build",
                    side_effect=AssertionError("semantic reader rebuilt")),
                mock.patch.object(
                    common, "IndexLock",
                    side_effect=AssertionError("semantic reader locked")),
            ):
                resolved = refs.resolve([0])
                refs.close()

            self.assertEqual(
                resolved[0]["text"], "needle in the published snapshot")
            self.assertEqual(
                (db_path.read_bytes(), db_path.stat().st_mtime_ns), before)


if __name__ == "__main__":
    unittest.main()
