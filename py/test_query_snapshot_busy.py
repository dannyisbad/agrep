from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _test_support import isolate_data_dir


isolate_data_dir()
import corpusdb  # noqa: E402
import explore  # noqa: E402
import indexd_runtime  # noqa: E402
import search  # noqa: E402


class QuerySnapshotBusyTests(unittest.TestCase):
    BUILD_ID = "0123456789abcdef0123"

    @staticmethod
    def _coded_error(message: str, code: int) -> sqlite3.OperationalError:
        error = sqlite3.OperationalError(message)
        error.sqlite_errorcode = code
        return error

    @classmethod
    def _publication(cls, path: Path, stamp: str = "fixture-stamp") -> None:
        db = sqlite3.connect(path)
        try:
            db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
            db.executemany("INSERT INTO meta VALUES(?, ?)", (
                ("schema", corpusdb._SCHEMA),
                ("build_id", cls.BUILD_ID),
                ("stamp", stamp),
            ))
            db.commit()
        finally:
            db.close()

    @classmethod
    def _bind(cls, error: sqlite3.DatabaseError, path: Path) -> None:
        error._agrep_source_identity = corpusdb._optional_sqlite_identity(path)
        error._agrep_source_build_id = cls.BUILD_ID

    def test_busy_and_locked_reads_retry_without_requesting_rebuild(self) -> None:
        codes = (
            ("busy", getattr(sqlite3, "SQLITE_BUSY", 5)),
            ("locked", getattr(sqlite3, "SQLITE_LOCKED", 6)),
            ("busy-snapshot", getattr(sqlite3, "SQLITE_BUSY_SNAPSHOT", 517)),
            ("locked-shared-cache",
             getattr(sqlite3, "SQLITE_LOCKED_SHAREDCACHE", 262)),
        )
        for label, code in codes:
            with self.subTest(label=label), tempfile.TemporaryDirectory(
                    prefix="agrep-query-busy-") as raw:
                db_path = Path(raw) / "corpus.db"
                expected = search.LaneResult(hits=[], engine="corpusdb")
                failure = self._coded_error("database is temporarily busy", code)
                with (
                    mock.patch.object(search, "corpusdb", corpusdb),
                    mock.patch.object(corpusdb, "DB_PATH", db_path),
                    mock.patch.object(
                        corpusdb, "_derived_write_ownership",
                        return_value=corpusdb._DerivedWriteOwnership(
                            "current")) as ownership,
                    mock.patch.object(
                        search, "_keyword_candidates_once",
                        side_effect=(failure, expected)) as candidates,
                    mock.patch.object(
                        indexd_runtime, "kick_background_repair") as repair,
                ):
                    actual = search._keyword_candidates(mock.Mock())

                self.assertIs(actual, expected)
                self.assertEqual(candidates.call_count, 2)
                ownership.assert_not_called()
                repair.assert_not_called()
                self.assertFalse(
                    db_path.with_name(".corpusdb-rebuild").exists())

    def test_busy_then_structural_failure_still_uses_jsonl_fallback(self) -> None:
        busy = self._coded_error(
            "database is busy", getattr(sqlite3, "SQLITE_BUSY", 5))
        corrupt = self._coded_error(
            "database is corrupt", getattr(sqlite3, "SQLITE_CORRUPT", 11))
        expected = search.LaneResult(hits=[], engine="jsonl")
        with (
            mock.patch.object(search, "corpusdb", corpusdb),
            mock.patch.object(
                search, "_keyword_candidates_once",
                side_effect=(busy, corrupt, expected)) as candidates,
            mock.patch.object(corpusdb, "record_query_database_error") as record,
        ):
            actual = search._keyword_candidates(mock.Mock())

        self.assertIs(actual, expected)
        self.assertEqual(candidates.call_count, 3)
        self.assertEqual(
            record.call_args_list, [mock.call(busy), mock.call(corrupt)])

    def test_database_failure_is_bound_before_its_handle_closes(self) -> None:
        failure = self._coded_error(
            "database is busy", getattr(sqlite3, "SQLITE_BUSY", 5))
        db = mock.MagicMock()
        db.in_transaction = True
        calls = mock.Mock()
        with (
            mock.patch.object(corpusdb, "connect", return_value=db),
            mock.patch.object(search, "_prepare_boundary", side_effect=failure),
            mock.patch.object(
                corpusdb, "bind_query_database_error") as bind,
            self.assertRaises(sqlite3.OperationalError) as raised,
        ):
            calls.attach_mock(bind, "bind")
            calls.attach_mock(db.close, "close")
            search._keyword_candidates_once(mock.Mock(mode="keyword", q="needle"))

        self.assertIs(raised.exception, failure)
        self.assertEqual(calls.mock_calls, [
            mock.call.bind(failure, db), mock.call.close(),
        ])

    def test_structural_read_error_still_requests_rebuild(self) -> None:
        codes = (
            ("corrupt", getattr(sqlite3, "SQLITE_CORRUPT", 11)),
            ("not-a-database", getattr(sqlite3, "SQLITE_NOTADB", 26)),
        )
        for label, code in codes:
            with self.subTest(label=label), tempfile.TemporaryDirectory(
                    prefix="agrep-query-corrupt-") as raw:
                db_path = Path(raw) / "corpus.db"
                self._publication(db_path)
                expected = search.LaneResult(hits=[], engine="jsonl")
                failure = sqlite3.DatabaseError("database is structurally bad")
                failure.sqlite_errorcode = code
                self._bind(failure, db_path)
                with (
                    mock.patch.object(search, "corpusdb", corpusdb),
                    mock.patch.object(corpusdb, "DB_PATH", db_path),
                    mock.patch.object(
                        corpusdb, "_derived_write_ownership",
                        return_value=corpusdb._DerivedWriteOwnership("current")),
                    mock.patch.object(
                        search, "_keyword_candidates_once",
                        side_effect=(failure, expected)) as candidates,
                ):
                    actual = search._keyword_candidates(mock.Mock())

                self.assertIs(actual, expected)
                self.assertEqual(candidates.call_count, 2)
                self.assertTrue(
                    db_path.with_name(".corpusdb-rebuild").is_file())
                with mock.patch.object(corpusdb, "DB_PATH", db_path):
                    key = corpusdb._query_rebuild_key()
                    corpusdb._QUERY_REBUILD_LOCAL.discard(key)
                    corpusdb._FOREIGN_QUERY_FAILURE_LOCAL.pop(key, None)
                    self.assertTrue(corpusdb.query_rebuild_required())
                    corpusdb._clear_query_rebuild_request()

    def test_codeless_operational_error_never_authorizes_rebuild(self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="agrep-query-uncoded-") as raw:
            db_path = Path(raw) / "corpus.db"
            expected = search.LaneResult(hits=[], engine="jsonl")
            failure = sqlite3.OperationalError(
                "unattributed SQLite operational failure")
            with (
                mock.patch.object(search, "corpusdb", corpusdb),
                mock.patch.object(corpusdb, "DB_PATH", db_path),
                mock.patch.object(
                    corpusdb, "_derived_write_ownership",
                    return_value=corpusdb._DerivedWriteOwnership(
                        "current")) as ownership,
                mock.patch.object(
                    search, "_keyword_candidates_once",
                    side_effect=(failure, expected)) as candidates,
                mock.patch.object(
                    indexd_runtime, "kick_background_repair") as repair,
            ):
                actual = search._keyword_candidates(mock.Mock())

            self.assertIs(actual, expected)
            self.assertEqual(candidates.call_count, 2)
            ownership.assert_not_called()
            repair.assert_not_called()
            self.assertFalse(
                db_path.with_name(".corpusdb-rebuild").exists())

    def test_codeless_exact_database_error_never_authorizes_rebuild(self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="agrep-query-uncoded-structural-") as raw:
            db_path = Path(raw) / "corpus.db"
            expected = search.LaneResult(hits=[], engine="jsonl")
            failure = sqlite3.DatabaseError("unattributed database failure")
            with (
                mock.patch.object(search, "corpusdb", corpusdb),
                mock.patch.object(corpusdb, "DB_PATH", db_path),
                mock.patch.object(
                    corpusdb, "_derived_write_ownership",
                    return_value=corpusdb._DerivedWriteOwnership(
                        "current")) as ownership,
                mock.patch.object(
                    search, "_keyword_candidates_once",
                    side_effect=(failure, expected)) as candidates,
            ):
                actual = search._keyword_candidates(mock.Mock())

            self.assertIs(actual, expected)
            self.assertEqual(candidates.call_count, 2)
            ownership.assert_not_called()
            self.assertFalse(
                db_path.with_name(".corpusdb-rebuild").exists())

    def test_other_coded_error_never_authorizes_durable_rebuild(self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="agrep-query-ioerr-") as raw:
            db_path = Path(raw) / "corpus.db"
            expected = search.LaneResult(hits=[], engine="jsonl")
            failure = self._coded_error(
                "disk I/O error", getattr(sqlite3, "SQLITE_IOERR", 10))
            with (
                mock.patch.object(search, "corpusdb", corpusdb),
                mock.patch.object(corpusdb, "DB_PATH", db_path),
                mock.patch.object(
                    corpusdb, "_derived_write_ownership",
                    return_value=corpusdb._DerivedWriteOwnership(
                        "current")) as ownership,
                mock.patch.object(
                    search, "_keyword_candidates_once",
                    side_effect=(failure, expected)) as candidates,
                mock.patch.object(
                    indexd_runtime, "kick_background_repair") as repair,
            ):
                actual = search._keyword_candidates(mock.Mock())

            self.assertIs(actual, expected)
            self.assertEqual(candidates.call_count, 2)
            ownership.assert_not_called()
            repair.assert_not_called()
            self.assertFalse(
                db_path.with_name(".corpusdb-rebuild").exists())

    def test_unavailable_publication_is_bypassed_until_successor(self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="agrep-query-bypass-") as raw:
            root = Path(raw)
            db_path = root / "corpus.db"
            messages = root / "messages.jsonl"
            messages.write_text("{}\n", encoding="utf-8")
            self._publication(db_path)
            failure = self._coded_error(
                "disk I/O error", getattr(sqlite3, "SQLITE_IOERR", 10))
            self._bind(failure, db_path)
            with (
                mock.patch.object(corpusdb, "DB_PATH", db_path),
                mock.patch.object(corpusdb.common, "MESSAGES_PATH", messages),
                mock.patch.object(corpusdb, "_trigram_ok", return_value=True),
                mock.patch.object(
                    corpusdb, "_derived_write_ownership",
                    return_value=corpusdb._DerivedWriteOwnership(
                        "current")) as ownership,
            ):
                corpusdb.record_query_database_error(failure)
                self.assertTrue(corpusdb._query_failure_matches_current())
                self.assertIsNone(
                    corpusdb.connect(quiet=True, allow_stale=True))
                ownership.assert_not_called()

                successor = root / "successor.db"
                self._publication(successor)
                successor.replace(db_path)
                self.assertFalse(corpusdb._query_failure_matches_current())
                corpusdb._clear_query_rebuild_request()

    def test_structural_failure_cannot_mark_a_successor_publication(self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="agrep-query-marker-race-") as raw:
            root = Path(raw)
            db_path = root / "corpus.db"
            self._publication(db_path)
            failure = self._coded_error(
                "database is corrupt", getattr(sqlite3, "SQLITE_CORRUPT", 11))
            self._bind(failure, db_path)
            successor = root / "successor.db"
            self._publication(successor)
            successor.replace(db_path)
            with (
                mock.patch.object(corpusdb, "DB_PATH", db_path),
                mock.patch.object(
                    corpusdb, "_derived_write_ownership",
                    return_value=corpusdb._DerivedWriteOwnership("current")),
            ):
                corpusdb.record_query_database_error(failure)
                self.assertFalse(corpusdb.query_rebuild_required())
                self.assertFalse(
                    db_path.with_name(".corpusdb-rebuild").exists())

    def test_bound_marker_is_ignored_after_publication_replacement(self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="agrep-query-marker-successor-") as raw:
            root = Path(raw)
            db_path = root / "corpus.db"
            self._publication(db_path)
            failure = self._coded_error(
                "database is corrupt", getattr(sqlite3, "SQLITE_CORRUPT", 11))
            self._bind(failure, db_path)
            with (
                mock.patch.object(corpusdb, "DB_PATH", db_path),
                mock.patch.object(
                    corpusdb, "_derived_write_ownership",
                    return_value=corpusdb._DerivedWriteOwnership("current")),
            ):
                corpusdb.record_query_database_error(failure)
                self.assertTrue(corpusdb.query_rebuild_required())
                successor = root / "successor.db"
                self._publication(successor)
                successor.replace(db_path)
                self.assertFalse(corpusdb.query_rebuild_required())
                corpusdb._clear_query_rebuild_request()

    def test_unbound_or_malformed_markers_have_no_rebuild_authority(self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="agrep-query-marker-invalid-") as raw:
            db_path = Path(raw) / "corpus.db"
            marker = db_path.with_name(".corpusdb-rebuild")
            self._publication(db_path)
            with mock.patch.object(corpusdb, "DB_PATH", db_path):
                for body in (
                        b"query database error\n", b"not-json\n",
                        b"x" * (corpusdb._QUERY_REBUILD_MARKER_MAX_BYTES + 1)):
                    marker.write_bytes(body)
                    self.assertFalse(corpusdb.query_rebuild_required())
                marker.unlink()

    def test_repeated_database_failure_is_a_bounded_named_cli_error(self) -> None:
        cases = (
            (
                "busy", getattr(sqlite3, "SQLITE_BUSY", 5),
                getattr(sqlite3, "SQLITE_LOCKED_SHAREDCACHE", 262),
                "search index is busy updating; retry",
            ),
            (
                "unavailable", getattr(sqlite3, "SQLITE_IOERR", 10),
                getattr(sqlite3, "SQLITE_READONLY", 8),
                "search index is temporarily unavailable; retry",
            ),
        )
        for label, first_code, second_code, message in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory(
                    prefix="agrep-query-repeat-") as raw:
                db_path = Path(raw) / "corpus.db"
                failures = (
                    self._coded_error("first database failure", first_code),
                    self._coded_error("second database failure", second_code),
                )
                stdout, stderr = io.StringIO(), io.StringIO()
                with (
                    mock.patch.object(search, "corpusdb", corpusdb),
                    mock.patch.object(corpusdb, "DB_PATH", db_path),
                    mock.patch.object(
                        corpusdb, "_derived_write_ownership",
                        return_value=corpusdb._DerivedWriteOwnership(
                            "current")) as ownership,
                    mock.patch.object(
                        search, "_keyword_candidates_once",
                        side_effect=failures) as candidates,
                    mock.patch.object(
                        indexd_runtime, "ensure_index", return_value=True),
                    mock.patch.object(
                        indexd_runtime, "kick_background_repair") as repair,
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                ):
                    try:
                        rc = search.main([
                            "needle", "--lexical", "--no-auto", "--classic"])
                    except sqlite3.Error as exc:
                        self.fail(f"raw SQLite failure escaped the CLI: {exc}")

                self.assertEqual(rc, 2)
                self.assertEqual(candidates.call_count, 2)
                ownership.assert_not_called()
                repair.assert_not_called()
                self.assertFalse(
                    db_path.with_name(".corpusdb-rebuild").exists())
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(stderr.getvalue(), message + "\n")

    def test_repeated_database_failure_has_structured_json_error(self) -> None:
        cases = (
            (
                "busy", getattr(sqlite3, "SQLITE_BUSY", 5),
                "search-index-busy", "corpusdb:busy",
                "search index is busy updating; retry",
            ),
            (
                "unavailable", getattr(sqlite3, "SQLITE_IOERR", 10),
                "search-index-unavailable", "corpusdb:unavailable",
                "search index is temporarily unavailable; retry",
            ),
        )
        for label, code, error_code, engine, message in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory(
                    prefix="agrep-query-json-error-") as raw:
                db_path = Path(raw) / "corpus.db"
                failures = (
                    self._coded_error("first database failure", code),
                    self._coded_error("second database failure", code),
                )
                stdout, stderr = io.StringIO(), io.StringIO()
                with (
                    mock.patch.object(search, "corpusdb", corpusdb),
                    mock.patch.object(corpusdb, "DB_PATH", db_path),
                    mock.patch.object(
                        search, "_keyword_candidates_once",
                        side_effect=failures),
                    mock.patch.object(
                        indexd_runtime, "ensure_index", return_value=True),
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                ):
                    rc = search.main([
                        "needle", "--lexical", "--no-auto", "--json"])

                self.assertEqual(rc, 2)
                self.assertEqual(stderr.getvalue(), message + "\n")
                payload = json.loads(stdout.getvalue())
                self.assertEqual(payload["error"], {
                    "code": error_code, "reason": message,
                })
                self.assertEqual(payload["engine"], engine)

    def test_publication_timeout_has_one_actionable_machine_error(self) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(indexd_runtime, "ensure_index", return_value=True),
            mock.patch.object(
                search, "run_query",
                side_effect=search.SnapshotPublicationTimeout(
                    search._QUERY_PUBLICATION_TIMEOUT)),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            rc = search.main([
                "needle", "--lexical", "--no-auto", "--json"])

        self.assertEqual(rc, 2)
        self.assertEqual(stderr.getvalue(), "")
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["error"], {
            "code": "snapshot-publication-timeout",
            "reason": search._QUERY_PUBLICATION_TIMEOUT,
        })
        self.assertEqual(payload["hits"], [])

    def test_regex_worker_preserves_named_database_failures(self) -> None:
        cases = (
            (
                "query-database-busy", search.QueryDatabaseBusyError,
                "search index is busy updating; retry",
            ),
            (
                "query-database-unavailable",
                search.QueryDatabaseUnavailableError,
                "search index is temporarily unavailable; retry",
            ),
        )
        for kind, error_type, message in cases:
            with self.subTest(kind=kind):
                receive, send, process, context = (
                    mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock())
                receive.poll.return_value = True
                receive.recv.return_value = (kind, message)
                process.is_alive.return_value = False
                context.Pipe.return_value = (receive, send)
                context.Process.return_value = process
                with (
                    mock.patch.object(search.common, "WIN", False),
                    mock.patch(
                        "multiprocessing.get_context", return_value=context),
                    self.assertRaisesRegex(error_type, message),
                ):
                    search._guarded_regex_query(mock.sentinel.spec)

    def test_optional_coverage_retries_one_busy_read(self) -> None:
        failure = self._coded_error(
            "coverage database is busy",
            getattr(sqlite3, "SQLITE_BUSY_SNAPSHOT", 517))
        first, second = mock.Mock(), mock.Mock()
        with (
            mock.patch.object(
                corpusdb, "connect", side_effect=(first, second)) as connect,
            mock.patch.object(
                corpusdb, "coverage_rank", side_effect=(failure, [])) as rank,
            mock.patch.object(
                corpusdb, "record_query_database_error") as record,
        ):
            attempt = search._overspec_retry_attempt(
                "wordy query with enough distinct terms here", {}, [], None,
                force=True)

        self.assertEqual(attempt.state, search._COVERAGE_SCANNED)
        self.assertIsNone(attempt.block)
        self.assertEqual(connect.call_count, 2)
        self.assertEqual(rank.call_count, 2)
        record.assert_called_once_with(failure, first)
        first.close.assert_called_once_with()
        second.close.assert_called_once_with()

    def test_explicit_coverage_busy_is_a_clean_cli_error(self) -> None:
        result = {
            "hits": [{
                "session": "s", "turn": 1, "who": "user", "text": "needle",
                "snippet": "needle", "ts": 1, "score": 1.0,
            }],
            "total": 1, "chats": 1, "engine": "corpusdb",
            "totals_exact": True, "truncated": False, "tool_hits": 0,
        }
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(indexd_runtime, "ensure_index", return_value=True),
            mock.patch.object(search, "run_query", return_value=result),
            mock.patch.object(
                search, "_overspec_retry_attempt",
                return_value=search._CoverageRetry(search._COVERAGE_BUSY)),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            rc = search.main([
                "needle", "--coverage", "--classic", "--no-auto", "--self"])

        self.assertEqual(rc, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            stderr.getvalue(), "search index is busy updating; retry\n")

    def test_automatic_coverage_names_bounded_degradation(self) -> None:
        logged = []
        attempt = search._CoverageRetry(search._COVERAGE_BUSY)
        with mock.patch.object(search.common, "log", logged.append):
            search._emit_overspec_block(
                "wordy query with enough distinct terms here", {}, [], None,
                attempt=attempt)
        self.assertEqual(logged, [
            "coverage retry skipped: search index is busy; "
            "original results unchanged",
        ])

    def test_boundary_snapshot_read_does_not_swallow_busy(self) -> None:
        for reader in (
                corpusdb.boundary_token_stats,
                corpusdb.boundary_token_qualities):
            with self.subTest(reader=reader.__name__):
                failure = self._coded_error(
                    "database is busy",
                    getattr(sqlite3, "SQLITE_BUSY_SNAPSHOT", 517))
                db = mock.Mock()
                db.execute.side_effect = failure
                with self.assertRaises(sqlite3.OperationalError) as raised:
                    reader(db, ["id"])
                self.assertIs(raised.exception, failure)

    def test_malformed_boundary_rows_use_cold_priors(self) -> None:
        cases = (
            (corpusdb.boundary_token_stats, [("id", "bad", 1)]),
            (corpusdb.boundary_token_qualities, [("id", "bad")]),
        )
        for reader, rows in cases:
            with self.subTest(reader=reader.__name__):
                db = mock.Mock()
                db.execute.return_value = rows
                self.assertEqual(reader(db, ["id"]), {})


if __name__ == "__main__":
    unittest.main()
