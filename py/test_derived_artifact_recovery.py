from __future__ import annotations

import contextlib
import io
import errno
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _test_support import isolate_data_dir, publish_derived_generation

isolate_data_dir()

import ask  # noqa: E402
import around  # noqa: E402
import common  # noqa: E402
import corpusdb  # noqa: E402
import embedder  # noqa: E402
import embedding_segments  # noqa: E402
import events  # noqa: E402
import explore  # noqa: E402
import index_lock  # noqa: E402
import recall  # noqa: E402
import search  # noqa: E402
import segment_query  # noqa: E402
import semantic  # noqa: E402
import session_context  # noqa: E402
import settings  # noqa: E402
import surface_policy as surface  # noqa: E402


class DerivedArtifactRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        corpusdb.indexd_runtime._clear_freshen_failure()

    def tearDown(self) -> None:
        corpusdb.indexd_runtime._clear_freshen_failure()

    def test_first_freshen_invalidates_cache_seeded_by_direct_caller(
            self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            stale = root / "stale"
            current = root / "current"
            stale.mkdir()
            current.mkdir()
            stale_messages = stale / "messages.jsonl"
            current_messages = current / "messages.jsonl"
            stale_messages.write_text("", encoding="utf-8")
            row = {
                "id": "codex:current:1",
                "agent": "codex",
                "project": "cache-generation",
                "session": "current",
                "ts": 1,
                "turn": 1,
                "text": "current generation",
                "who": "user",
            }
            current_messages.write_text(
                json.dumps(row) + "\n", encoding="utf-8")

            explore._session_index.cache_clear()
            explore._messages_by_session.cache_clear()
            explore._GEN = None
            with (
                mock.patch.object(common, "DATA_DIR", stale),
                mock.patch.object(common, "MESSAGES_PATH", stale_messages),
            ):
                self.assertEqual(explore._session_index(), {})
            with (
                mock.patch.object(common, "DATA_DIR", current),
                mock.patch.object(common, "MESSAGES_PATH", current_messages),
            ):
                explore._freshen()
                self.assertIn("current", explore._session_index())

        explore._GEN = ("force-clear",)
        explore._freshen()

    @unittest.skipIf(os.name == "nt", "POSIX mode bits provide the read-only fixture")
    def test_search_recall_and_around_serve_with_data_dir_writes_denied(
            self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            session = "11111111-1111-7111-8111-111111111111"
            message = {
                "id": f"codex:{session}:1",
                "agent": "codex",
                "project": "read-only-fixture",
                "session": session,
                "ts": 1_700_000_000_000,
                "turn": 1,
                "text": "needle answer from a read-only corpus",
                "who": "user",
                "model": "",
                "model_source": "unknown",
            }
            messages = publish_derived_generation(
                root, [message], common, corpusdb,
                signature="read-only-generation", mode=0o444)
            readonly_files = [path for path in root.iterdir() if path.is_file()]
            root.chmod(0o555)

            outputs: dict[str, tuple[int, str, str]] = {}
            try:
                with (
                    mock.patch.object(common, "DATA_DIR", root),
                    mock.patch.object(common, "MESSAGES_PATH", messages),
                    mock.patch.object(
                        common, "INGEST_SIG_PATH", root / ".ingest.sig"),
                    mock.patch.object(corpusdb, "DB_PATH", root / "corpus.db"),
                    mock.patch.object(
                        corpusdb, "INGEST_SIG_PATH", root / ".ingest.sig"),
                    mock.patch.object(
                        corpusdb, "BOUNDARY_STATS_PATH",
                        root / "boundary_stats.json"),
                    mock.patch.object(
                        corpusdb, "CHANGED_PATH", root / ".changed_sessions"),
                    mock.patch.object(
                        corpusdb, "_live_refresh_lock", return_value=False),
                    mock.patch.object(
                        corpusdb.indexd_runtime, "freshener_alive",
                        return_value=False),
                    mock.patch.object(
                        corpusdb.indexd_runtime, "fts_delegation_active",
                        return_value=False),
                    mock.patch.object(
                        index_lock, "INDEX_LOCK_PATH", root / ".index.lock"),
                    mock.patch.object(
                        common, "INDEX_LOCK_PATH", root / ".index.lock"),
                    mock.patch.object(session_context, "DATA_DIR", root),
                    mock.patch.object(settings, "SETTINGS_PATH",
                                      root / "settings.json"),
                    mock.patch.object(events, "EVENTS_DIR", root / "events"),
                ):
                    explore._GEN = ("force-clear",)
                    cases = (
                        (
                            "search",
                            search.main,
                            [
                                "needle", "--classic", "--no-auto", "--self",
                                "--color", "never",
                            ],
                        ),
                        (
                            "recall",
                            recall.main,
                            [
                                "needle", "--lexical", "--json", "--no-auto",
                                "--self", "--budget", "8000",
                            ],
                        ),
                        (
                            "around",
                            around.main,
                            [session, "1", "--json", "--no-auto"],
                        ),
                    )
                    for name, command, argv in cases:
                        stdout, stderr = io.StringIO(), io.StringIO()
                        with (
                            contextlib.redirect_stdout(stdout),
                            contextlib.redirect_stderr(stderr),
                        ):
                            rc = command(argv)
                        outputs[name] = (
                            rc, stdout.getvalue(), stderr.getvalue())
            finally:
                root.chmod(0o755)
                for path in readonly_files:
                    path.chmod(0o644)
                explore._GEN = ("force-clear",)
                explore._freshen()

        for name, (rc, stdout, stderr) in outputs.items():
            with self.subTest(verb=name):
                self.assertEqual(rc, 0, stderr)
                self.assertIn("needle answer", stdout)
                self.assertNotIn("Traceback", stderr)
        self.assertFalse((root / ".index.lock").exists())

    def test_interactive_reads_never_touch_the_writer_lock(
            self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            messages = root / "messages.jsonl"
            messages.write_text("{}\n", encoding="utf-8")
            denied = mock.MagicMock()
            denied.__enter__.side_effect = PermissionError("read only")
            with (
                mock.patch.object(common, "MESSAGES_PATH", messages),
                mock.patch.object(corpusdb, "DB_PATH", root / "corpus.db"),
                mock.patch.object(
                    corpusdb, "purge_legacy_build_temps",
                    return_value={
                        "removed": 0, "removed_bytes": 0,
                        "found": 0, "found_bytes": 0, "deferred": 0,
                    }),
                mock.patch.object(corpusdb, "_trigram_ok", return_value=True),
                mock.patch.object(corpusdb, "_stamp", return_value="stamp"),
                mock.patch.object(corpusdb, "_valid_db", return_value=None),
                mock.patch.object(
                    corpusdb, "_live_refresh_lock", return_value=False),
                mock.patch.object(
                    corpusdb.indexd_runtime, "freshener_alive",
                    return_value=False),
                mock.patch.object(
                    corpusdb.indexd_runtime, "fts_delegation_active",
                    return_value=False),
                mock.patch.object(
                    common, "IndexLock", return_value=denied),
            ):
                self.assertIsNone(
                    corpusdb.connect(quiet=True, allow_stale=True))
                self.assertIsNone(
                    corpusdb.connect(quiet=True, allow_stale=False))
                common.IndexLock.assert_not_called()

    def test_interactive_snapshot_does_not_probe_read_only_or_busy_writer_locks(
            self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            messages = root / "messages.jsonl"
            messages.write_text("{}\n", encoding="utf-8")
            for error in (
                    OSError(errno.EROFS, "read-only file system"),
                    TimeoutError("busy owner")):
                denied = mock.MagicMock()
                denied.__enter__.side_effect = error
                with (
                    self.subTest(error=type(error).__name__),
                    mock.patch.object(common, "MESSAGES_PATH", messages),
                    mock.patch.object(
                        corpusdb, "DB_PATH", root / "corpus.db"),
                    mock.patch.object(
                        corpusdb, "purge_legacy_build_temps",
                        return_value={
                            "removed": 0, "removed_bytes": 0,
                            "found": 0, "found_bytes": 0, "deferred": 0,
                        }),
                    mock.patch.object(
                        corpusdb, "_trigram_ok", return_value=True),
                    mock.patch.object(
                        corpusdb, "_stamp", return_value="stamp"),
                    mock.patch.object(
                        corpusdb, "_valid_db", return_value=None),
                    mock.patch.object(
                        corpusdb, "_live_refresh_lock", return_value=False),
                    mock.patch.object(
                        corpusdb.indexd_runtime, "freshener_alive",
                        return_value=False),
                    mock.patch.object(
                        corpusdb.indexd_runtime, "fts_delegation_active",
                        return_value=False),
                    mock.patch.object(
                        common, "IndexLock", return_value=denied) as lock,
                ):
                    self.assertIsNone(
                        corpusdb.connect(quiet=True, allow_stale=True))
                    lock.assert_not_called()

    def test_query_database_error_retries_through_the_jsonl_seam(self) -> None:
        expected = search.LaneResult(hits=[], engine="jsonl")
        failure = sqlite3.DatabaseError("database disk image is malformed")
        with (
            mock.patch.object(search, "corpusdb", corpusdb),
            mock.patch.object(
                search, "_keyword_candidates_once",
                side_effect=(failure, expected)) as candidates,
            mock.patch.object(
                search.corpusdb, "record_query_database_error") as record,
        ):
            actual = search._keyword_candidates(mock.Mock())
        self.assertIs(actual, expected)
        self.assertEqual(candidates.call_count, 2)
        record.assert_called_once_with(failure)

    def test_ownerless_interior_fts_page_damage_stays_read_only_on_jsonl(
            self) -> None:
        if not corpusdb._trigram_ok():
            self.skipTest("SQLite trigram FTS5 is unavailable")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            message = {
                "id": "codex:fallback:1",
                "agent": "codex",
                "project": "p",
                "session": "fallback",
                "ts": 1,
                "turn": 1,
                "text": "needle fallback survives",
                "who": "user",
                "model": "",
                "model_source": "unknown",
            }
            messages = publish_derived_generation(
                root, [message], common, corpusdb,
                signature="damaged-fts-generation")
            db_path = root / "corpus.db"
            stamp = json.dumps([None] * len(corpusdb._SOURCES))
            db = sqlite3.connect(db_path)
            try:
                db.execute("PRAGMA page_size=512")
                db.executescript(corpusdb._SCHEMA_SQL)
                rows = [
                    (
                        f"session-{index}", index, index, "codex", "p", "",
                        "", "unknown", "user",
                        "needle " + (f"token{index:06d} " * 8) + ("x" * 80),
                    )
                    for index in range(200)
                ]
                db.executemany(corpusdb._INS, rows)
                db.execute(
                    "INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
                db.execute(
                    "INSERT INTO msgs_prose_fts(rowid,text) "
                    "SELECT id,text FROM msgs WHERE who <> 'tool'")
                db.execute("INSERT INTO meta VALUES('stamp', ?)", (stamp,))
                db.execute(
                    "INSERT INTO meta VALUES('schema', ?)",
                    (corpusdb._SCHEMA,))
                db.execute(
                    "INSERT INTO meta VALUES('fts_triggers', ?)",
                    (corpusdb._TRIGGER_SCHEMA,))
                db.commit()
                page_size = int(
                    db.execute("PRAGMA page_size").fetchone()[0])
                root_page = int(db.execute(
                    "SELECT rootpage FROM sqlite_schema "
                    "WHERE name='msgs_fts_data'").fetchone()[0])
            finally:
                db.close()

            before_size = db_path.stat().st_size
            offset = (root_page - 1) * page_size
            with db_path.open("r+b") as stream:
                stream.seek(offset)
                self.assertEqual(stream.read(1), b"\x05")
                stream.seek(offset)
                stream.write(b"\x00")
            self.assertEqual(db_path.stat().st_size, before_size)

            damaged = sqlite3.connect(db_path)
            try:
                with self.assertRaises(sqlite3.DatabaseError):
                    damaged.execute(
                        "SELECT rowid FROM msgs_fts "
                        "WHERE msgs_fts MATCH 'needle' LIMIT 1").fetchone()
            finally:
                damaged.close()

            spec = search.QuerySpec(
                q="needle", mode="keyword", limit=0, sort="position",
                agent=None, project=None, who=None, model=None,
                model_soft=False, chat=None, since_ms=None, until_ms=None,
                exhaustive=False, session_limit=None, include_tools=True,
                exclude_session=None, exclude_session_from_turn=None,
                allow_fallback=False, exact_totals=False,
                family_diverse=False, semantic_timeout_s=None)
            with (
                mock.patch.object(common, "DATA_DIR", root),
                mock.patch.object(common, "MESSAGES_PATH", messages),
                mock.patch.object(corpusdb, "DB_PATH", db_path),
                mock.patch.object(corpusdb, "_stamp", return_value=stamp),
                mock.patch.object(search, "corpusdb", corpusdb),
                mock.patch.object(common, "setting", return_value="off"),
            ):
                try:
                    result = search._keyword_candidates(spec)
                    self.assertEqual(result.engine, "jsonl")
                    self.assertEqual(
                        [hit["session"] for hit in result.hits],
                        ["fallback"])
                    self.assertFalse(
                        db_path.with_name(".corpusdb-rebuild").exists())
                    self.assertFalse(corpusdb.query_rebuild_required())
                finally:
                    corpusdb._clear_query_rebuild_request()

    def test_query_error_marker_forces_nonblocking_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            messages = root / "messages.jsonl"
            messages.write_text("{}\n", encoding="utf-8")
            db_path = root / "corpus.db"
            build_id = "0123456789abcdef0123"
            db = sqlite3.connect(db_path)
            db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
            db.executemany("INSERT INTO meta VALUES(?, ?)", (
                ("schema", corpusdb._SCHEMA), ("build_id", build_id),
            ))
            db.commit()
            db.close()
            damage = sqlite3.DatabaseError("malformed")
            damage.sqlite_errorcode = getattr(sqlite3, "SQLITE_CORRUPT", 11)
            damage._agrep_source_identity = corpusdb._optional_sqlite_identity(
                db_path)
            damage._agrep_source_build_id = build_id
            with (
                mock.patch.object(common, "MESSAGES_PATH", messages),
                mock.patch.object(corpusdb, "DB_PATH", db_path),
                mock.patch.object(
                    corpusdb, "purge_legacy_build_temps",
                    return_value={
                        "removed": 0, "removed_bytes": 0,
                        "found": 0, "found_bytes": 0, "deferred": 0,
                    }),
                mock.patch.object(corpusdb, "_trigram_ok", return_value=True),
                mock.patch.object(
                    corpusdb, "_derived_write_ownership",
                    return_value=corpusdb._DerivedWriteOwnership("current")),
                mock.patch.object(common, "IndexLock") as lock,
            ):
                corpusdb.record_query_database_error(damage)
                self.assertTrue(
                    db_path.with_name(".corpusdb-rebuild").is_file())
                self.assertIsNone(
                    corpusdb.connect(quiet=True, allow_stale=True))
                lock.assert_not_called()
                corpusdb._clear_query_rebuild_request()

    def test_query_error_is_process_local_for_a_protected_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "corpus.db"
            build_id = "0123456789abcdef0123"
            db = sqlite3.connect(db_path)
            db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
            db.execute("INSERT INTO meta VALUES('build_id', ?)", (build_id,))
            db.commit()
            db.close()
            marker = root / ".corpusdb-rebuild"
            marker.write_bytes(b"existing writer-owned request\n")
            damage = sqlite3.DatabaseError("protected")
            damage.sqlite_errorcode = getattr(sqlite3, "SQLITE_CORRUPT", 11)
            damage._agrep_source_identity = corpusdb._optional_sqlite_identity(
                db_path)
            damage._agrep_source_build_id = build_id

            def snapshot() -> tuple[int, tuple[tuple[str, int, bytes], ...]]:
                entries = tuple(
                    (path.name, path.stat().st_mtime_ns, path.read_bytes())
                    for path in sorted(root.iterdir())
                )
                return root.stat().st_mtime_ns, entries

            with (
                mock.patch.object(common, "DATA_DIR", root),
                mock.patch.object(corpusdb, "DB_PATH", db_path),
                mock.patch.dict(
                    os.environ, {"AGREP_DATA_READONLY": os.fspath(root)},
                    clear=False),
            ):
                key = corpusdb._query_rebuild_key()
                corpusdb._QUERY_REBUILD_LOCAL.discard(key)
                corpusdb._FOREIGN_QUERY_FAILURE_LOCAL.pop(key, None)
                before = snapshot()
                corpusdb.record_query_database_error(damage)
                self.assertEqual(snapshot(), before)
                self.assertNotIn(key, corpusdb._QUERY_REBUILD_LOCAL)
                self.assertIn(key, corpusdb._FOREIGN_QUERY_FAILURE_LOCAL)
                self.assertFalse(corpusdb.query_rebuild_required())
                self.assertIsNone(
                    corpusdb.connect(quiet=True, allow_stale=True))
                corpusdb._QUERY_REBUILD_LOCAL.discard(key)
                corpusdb._FOREIGN_QUERY_FAILURE_LOCAL.pop(key, None)

    def test_recall_expansion_drops_to_existing_windows_on_db_damage(self) -> None:
        hit = {"session": "s", "turn": 1}
        window = {
            "session": "s", "center": 1, "first_turn": 1,
            "last_turn": 1, "turns": [],
        }
        pairs = [(hit, window)]
        db = mock.MagicMock()
        damage = sqlite3.DatabaseError("malformed")
        with (
            mock.patch.object(
                recall.corpusdb, "connect", return_value=db),
            mock.patch.object(
                recall.corpusdb, "session_text_sizes",
                side_effect=damage),
            mock.patch.object(
                recall.corpusdb, "record_query_database_error") as record,
        ):
            result = recall._expand(
                pairs, ["query words"], budget=5000, context=2)
        self.assertIs(result, pairs)
        record.assert_called_once_with(damage, db)
        db.close.assert_called_once_with()

    def test_window_reads_fall_back_to_jsonl_on_db_damage(self) -> None:
        request = [("s", 1, 2)]
        expected = [{"session": "s", "turns": []}]
        db = mock.MagicMock()
        damage = sqlite3.DatabaseError("malformed")
        db.execute.side_effect = damage
        with (
            mock.patch.object(explore, "_freshen"),
            mock.patch.object(
                corpusdb, "connect", return_value=db),
            mock.patch.object(
                explore, "direct_snapshot_attempt",
                return_value=contextlib.nullcontext({})) as snapshot,
            mock.patch.object(
                explore, "_legacy_windows", return_value=expected) as fallback,
            mock.patch.object(
                corpusdb, "record_query_database_error") as record,
        ):
            result = explore.get_windows(request)
        self.assertEqual(result, expected)
        fallback.assert_called_once_with(request, include_events=False)
        record.assert_called_once_with(damage, db)
        snapshot.assert_called_once_with(include_events=False)

    def test_window_reads_retry_busy_then_use_a_verified_fallback(self) -> None:
        request = [("s", 1, 2)]
        expected = [{"session": "s", "turns": []}]
        busy = sqlite3.OperationalError("database is busy")
        busy.sqlite_errorcode = getattr(sqlite3, "SQLITE_BUSY", 5)
        locked = sqlite3.OperationalError("database is locked")
        locked.sqlite_errorcode = getattr(sqlite3, "SQLITE_LOCKED", 6)
        first, second = mock.MagicMock(), mock.MagicMock()
        first.execute.side_effect = busy
        second.execute.side_effect = locked
        with (
            mock.patch.object(explore, "_freshen"),
            mock.patch.object(
                corpusdb, "connect", side_effect=(first, second)) as connect,
            mock.patch.object(
                explore, "direct_snapshot_attempt",
                return_value=contextlib.nullcontext({})) as snapshot,
            mock.patch.object(
                explore, "_legacy_windows", return_value=expected) as fallback,
            mock.patch.object(
                corpusdb, "record_query_database_error") as record,
        ):
            result = explore.get_windows(request)

        self.assertEqual(result, expected)
        self.assertEqual(connect.call_count, 2)
        self.assertEqual(record.call_args_list, [
            mock.call(busy, first), mock.call(locked, second),
        ])
        fallback.assert_called_once_with(request, include_events=False)
        snapshot.assert_called_once_with(include_events=False)

    def test_window_fallback_never_reads_without_generation_proof(self) -> None:
        request = [("s", 1, 2)]
        damage = explore.DirectSnapshotError("proof unavailable")
        with (
            mock.patch.object(explore, "_freshen"),
            mock.patch.object(corpusdb, "connect", return_value=None),
            mock.patch.object(
                explore, "direct_snapshot_attempt", side_effect=damage),
            mock.patch.object(explore, "_legacy_windows") as fallback,
            self.assertRaises(explore.DirectSnapshotError) as raised,
        ):
            explore.get_windows(request)

        self.assertIs(raised.exception, damage)
        fallback.assert_not_called()

    def test_integrity_request_persists_and_spawns_a_full_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = io.BytesIO()
            with (
                mock.patch.object(common, "DATA_DIR", root),
                mock.patch.object(
                    semantic, "_background_refresh_disabled",
                    return_value=None),
                mock.patch.object(
                    semantic, "runtime_dependencies_available",
                    return_value=True),
                mock.patch.object(embedder, "ensure_model"),
                mock.patch.object(
                    semantic, "embedding_coherence",
                    return_value={
                        "coherent": True, "searchable": True,
                        "state": "current",
                    }),
                mock.patch.object(
                    semantic, "embed_running", return_value=False),
                mock.patch.object(
                    semantic, "read_embed_state", return_value={}),
                mock.patch.object(
                    semantic, "_needs_unverified_bundle_rebuild",
                    return_value=False),
                mock.patch.object(
                    common, "open_bounded_log", return_value=log),
                mock.patch.object(semantic.subprocess, "Popen") as spawn,
            ):
                result = semantic.request_full_rebuild("digest mismatch")
                marker = semantic.integrity_rebuild_path()
                self.assertTrue(marker.is_file())
                command = spawn.call_args.args[0]
                self.assertIn("--full", command)
                self.assertEqual(result["state"], "running")
                semantic.clear_integrity_rebuild_request()
                self.assertFalse(marker.exists())
                self.assertFalse(semantic.integrity_rebuild_requested())

    def test_deterministic_query_integrity_failure_requests_full_rebuild(
            self) -> None:
        coherence = {
            "coherent": True, "searchable": True,
            "state": "current", "coverage": {"complete": True},
        }
        damage = segment_query.SegmentIntegrityError("refs page damaged")
        repair = {"state": "running", "persistent": True}
        with (
            mock.patch.object(semantic, "note_semantic_use"),
            mock.patch.object(
                semantic, "embedding_coherence", return_value=coherence),
            mock.patch.object(embedder, "get"),
            mock.patch.object(
                ask, "tool_search_hybrid", side_effect=damage),
            mock.patch.object(
                semantic, "request_full_rebuild",
                return_value=repair) as request,
        ):
            result = semantic.search(
                "query", refresh_if_stale=False, timing=False)
        request.assert_called_once_with("refs page damaged")
        self.assertEqual(result["results"], [])
        self.assertTrue(result["semantic_unavailable"])
        self.assertTrue(result["partial"])
        self.assertEqual(result["semantic_integrity"], {
            "state": "generation-rejected",
            "dropped": 0,
            "reason": "refs page damaged",
            "repair": "full-rebuild-requested",
            "repair_state": "running",
            "repair_persistent": True,
        })

    def test_recorded_corrupt_generation_keeps_structured_rejection(self) -> None:
        coherence = {
            "coherent": False, "searchable": False,
            "state": "corrupt-embeddings", "coverage": None,
            "reason": "a deterministic integrity failure requires a full rebuild",
        }
        repair = {"state": "running", "persistent": True}
        with (
            mock.patch.object(semantic, "note_semantic_use"),
            mock.patch.object(
                semantic, "embedding_coherence", return_value=coherence),
            mock.patch.object(
                semantic, "request_full_rebuild",
                return_value=repair) as request,
        ):
            result = semantic.search(
                "query", refresh_if_stale=False, timing=False)
        request.assert_called_once_with(coherence["reason"])
        self.assertTrue(result["semantic_unavailable"])
        self.assertEqual(
            result["semantic_integrity"]["state"], "generation-rejected")
        self.assertEqual(
            result["semantic_integrity"]["repair"],
            "full-rebuild-requested")
        self.assertTrue(result["semantic_integrity"]["repair_persistent"])

    def test_dropped_text_proof_rows_disclose_and_request_full_rebuild(
            self) -> None:
        coherence = {
            "coherent": True, "searchable": True,
            "state": "current", "coverage": {"complete": True},
        }
        payload = {
            "hits": [{"session": "survivor"}],
            "semantic_integrity": {
                "state": "rows-dropped",
                "dropped": 4,
                "mismatched": 4,
                "absent": 0,
                "considered": 5,
                "reason": "semantic text proof failed",
            },
        }
        repair = {"state": "running", "persistent": True}
        with (
            mock.patch.object(semantic, "note_semantic_use"),
            mock.patch.object(
                semantic, "embedding_coherence", return_value=coherence),
            mock.patch.object(embedder, "get"),
            mock.patch.object(
                ask, "tool_search_hybrid",
                return_value=json.dumps(payload)),
            mock.patch.object(
                semantic, "request_full_rebuild",
                return_value=repair) as request,
        ):
            result = semantic.search(
                "query", refresh_if_stale=False, timing=False)
        request.assert_called_once_with("semantic text proof failed")
        integrity = result["semantic_integrity"]
        self.assertEqual(integrity["repair_state"], "running")
        self.assertTrue(integrity["repair_persistent"])
        self.assertEqual(result["hits"], [{"session": "survivor"}])

    def _search_with_integrity(self, integrity: dict, request) -> dict:
        coherence = {
            "coherent": True, "searchable": True,
            "state": "current", "coverage": {"complete": True},
        }
        payload = {"hits": [{"session": "survivor"}],
                   "semantic_integrity": dict(integrity)}
        with (
            mock.patch.object(semantic, "note_semantic_use"),
            mock.patch.object(
                semantic, "embedding_coherence", return_value=coherence),
            mock.patch.object(embedder, "get"),
            mock.patch.object(
                ask, "tool_search_hybrid", return_value=json.dumps(payload)),
            mock.patch.object(
                semantic, "request_full_rebuild", side_effect=request),
        ):
            return semantic.search("query", refresh_if_stale=False, timing=False)

    def test_rows_absent_from_a_lagging_corpus_never_discard_the_generation(
            self) -> None:
        request = mock.Mock(
            side_effect=AssertionError("coverage lag requested a rebuild"))
        result = self._search_with_integrity({
            "state": "rows-uncorroborated", "dropped": 2, "mismatched": 0,
            "absent": 2, "considered": 2,
            "reason": "the derived corpus has not mirrored these rows yet",
        }, request)
        integrity = result["semantic_integrity"]
        request.assert_not_called()
        self.assertEqual(integrity["repair_state"], "not-requested")
        self.assertFalse(integrity["repair_persistent"])
        self.assertEqual(result["hits"], [{"session": "survivor"}])
        self.assertIn(
            "has not mirrored them yet",
            surface.semantic_integrity_notice(integrity))

    def test_one_surprising_row_does_not_discard_a_sound_generation(
            self) -> None:
        request = mock.Mock(
            side_effect=AssertionError("one row requested a rebuild"))
        result = self._search_with_integrity({
            "state": "rows-dropped", "dropped": 1, "mismatched": 1,
            "absent": 0, "considered": 40,
            "reason": "semantic text proof failed",
        }, request)
        request.assert_not_called()
        self.assertEqual(
            result["semantic_integrity"]["repair_state"], "not-requested")

    def test_a_publication_race_degrades_without_requesting_a_rebuild(
            self) -> None:
        coherence = {
            "coherent": True, "searchable": True,
            "state": "current", "coverage": {"complete": True},
        }
        race = segment_query.SegmentArtifactMoved(
            "active semantic artifact was republished under this reader")
        with (
            mock.patch.object(semantic, "note_semantic_use"),
            mock.patch.object(
                semantic, "embedding_coherence", return_value=coherence),
            mock.patch.object(embedder, "get"),
            mock.patch.object(ask, "tool_search_hybrid", side_effect=race),
            mock.patch.object(
                semantic, "request_full_rebuild",
                side_effect=AssertionError("a race requested a rebuild")),
            self.assertRaises(semantic.SemanticUnavailable),
        ):
            semantic.search("query", refresh_if_stale=False, timing=False)
        self.assertFalse(semantic._deterministic_integrity_error(race))
        self.assertFalse(semantic._deterministic_integrity_error(
            embedding_segments.SegmentPublicationRace("prefix changed")))


if __name__ == "__main__":
    unittest.main()
