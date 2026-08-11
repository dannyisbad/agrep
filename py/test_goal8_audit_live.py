"""Goal 8 fail-closed audit and live-observation proofs."""

from __future__ import annotations

import io
import json
import os
import queue
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))
sys.path.insert(0, str(ROOT))

from _test_support import isolate_data_dir  # noqa: E402

isolate_data_dir()

import audit  # noqa: E402
import livetui  # noqa: E402
import tail  # noqa: E402
from hookless import live as live_mod, procscan  # noqa: E402
from hookless.live import LiveWatcher  # noqa: E402


def _entry(agent: str, path: Path, *, seen: int = 1) -> dict:
    st = path.stat()
    return {
        "agent": agent,
        "key": f"s:{int(st.st_mtime_ns // 1_000_000)}:{st.st_size}",
        "seen": seen, "rows": seen, "agent_rows": 0, "events": 0,
        "errors": 0, "skips": {}, "first_error": None,
    }


def _token_id(path: Path, session: str) -> str:
    return audit._TOKEN_ID_PREFIX + json.dumps(
        [str(path), session], separators=(",", ":"))


class AuditFailClosedTests(unittest.TestCase):
    def _run(self, book, discovered, indexed=(), live_tokens=None):
        out = io.StringIO()
        with mock.patch.object(audit, "_book", side_effect=book) \
                if isinstance(book, Exception) else mock.patch.object(
                    audit, "_book", return_value=book), \
                mock.patch.object(audit, "_discovered", return_value=discovered), \
                mock.patch.object(audit, "_indexed_agents", return_value=set(indexed)), \
                mock.patch.object(
                    audit, "_live_tokens", return_value=live_tokens or {}), \
                redirect_stdout(out):
            rc = audit.main(["--strict", "--json"])
        return rc, json.loads(out.getvalue())

    def test_corrupt_and_unreadable_books_are_json_problems(self):
        for error in (
                audit.AuditEvidenceError("evidence book is corrupt"),
                audit.AuditEvidenceError("cannot read evidence book: denied")):
            with self.subTest(error=str(error)):
                rc, payload = self._run(error, [])
                self.assertEqual(rc, 2)
                self.assertTrue(any(str(error) in item for item in payload["problems"]))

    def test_truncated_book_reader_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "intake_stats.json"
            path.write_text('{"version":1,"files":', encoding="utf-8")
            with mock.patch.object(audit, "BOOK_PATH", path):
                with self.assertRaises(audit.AuditEvidenceError):
                    audit._book()

    def test_huge_integer_book_reader_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "intake_stats.json"
            path.write_text(
                '{"version":' + ('9' * 5000) + ',"files":{}}',
                encoding="utf-8")
            with mock.patch.object(audit, "BOOK_PATH", path):
                with self.assertRaises(audit.AuditEvidenceError):
                    audit._book()

    def test_duplicate_book_fields_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "intake_stats.json"
            path.write_text(
                '{"version":1,"version":1,"files":{}}', encoding="utf-8")
            with mock.patch.object(audit, "BOOK_PATH", path):
                with self.assertRaises(audit.AuditEvidenceError):
                    audit._book()

    def test_book_replacement_during_audit_cannot_certify_clean(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "rollout.jsonl"
            source.write_text("{}\n", encoding="utf-8")
            book_path = root / "intake_stats.json"
            book = {str(source): _entry("codex", source)}
            book_path.write_text(json.dumps({
                "version": audit.BOOK_VERSION, "files": book,
            }), encoding="utf-8")

            def replace_book():
                book_path.write_text("{", encoding="utf-8")
                return set()

            stdout = io.StringIO()
            with mock.patch.object(audit, "BOOK_PATH", book_path), \
                    mock.patch.object(
                        audit, "_discovered",
                        return_value=[("codex", str(source))]), \
                    mock.patch.object(audit, "_live_tokens", return_value={}), \
                    mock.patch.object(
                        audit, "_indexed_agents", side_effect=replace_book), \
                    redirect_stdout(stdout):
                rc = audit.main(["--strict", "--json"])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(rc, 2)
        self.assertTrue(any("evidence book" in row for row in payload["problems"]))

    def test_book_replacement_during_raw_census_cannot_certify_clean(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "rollout.jsonl"
            source.write_text("{}\n", encoding="utf-8")
            book_path = root / "intake_stats.json"
            book_path.write_text(json.dumps({
                "version": audit.BOOK_VERSION,
                "files": {str(source): _entry("codex", source)},
            }), encoding="utf-8")

            def replace_during_census(_path, *, deadline=None):
                del deadline
                book_path.write_text("{", encoding="utf-8")
                return 1

            stdout = io.StringIO()
            with mock.patch.object(audit, "BOOK_PATH", book_path), \
                    mock.patch.object(
                        audit, "_discovered",
                        return_value=[("codex", str(source))]), \
                    mock.patch.object(audit, "_live_tokens", return_value={}), \
                    mock.patch.object(audit, "_indexed_agents", return_value=set()), \
                    mock.patch.object(
                        audit, "_census_jsonl",
                        side_effect=replace_during_census), \
                    redirect_stdout(stdout):
                rc = audit.main(["--strict", "--json", "--full"])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(rc, 2)
        self.assertTrue(any("evidence book" in row for row in payload["problems"]))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO contract is POSIX-only")
    def test_special_or_oversized_evidence_book_is_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fifo = root / "book.fifo"
            os.mkfifo(fifo)
            script = (
                "from pathlib import Path\n"
                "import audit\n"
                f"audit.BOOK_PATH = Path({str(fifo)!r})\n"
                "try:\n"
                "    audit._book()\n"
                "except audit.AuditEvidenceError:\n"
                "    raise SystemExit(0)\n"
                "raise SystemExit(1)\n"
            )
            completed = subprocess.run(
                [sys.executable, "-c", script], cwd=ROOT / "py",
                capture_output=True, text=True, timeout=2)
            self.assertEqual(completed.returncode, 0, completed.stderr)

            book = root / "book.json"
            book.write_bytes(b"{}")
            with mock.patch.object(audit, "BOOK_PATH", book), \
                    mock.patch.object(audit, "_BOOK_MAX_BYTES", 1):
                with self.assertRaises(audit.AuditEvidenceError):
                    audit._book()

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO contract is POSIX-only")
    def test_all_audit_file_readers_reject_special_files_without_blocking(self):
        with tempfile.TemporaryDirectory() as td:
            fifo = Path(td) / "source"
            os.mkfifo(fifo)
            for reader in (audit._stat_key, audit._census_jsonl):
                with self.subTest(reader=reader.__name__), \
                        self.assertRaises(audit.AuditEvidenceError):
                    started = time.perf_counter()
                    reader(str(fifo))
                self.assertLess(time.perf_counter() - started, 0.5)
            with mock.patch.object(audit.common, "DATA_DIR", Path(td)):
                fifo.rename(Path(td) / "corpus.db")
                started = time.perf_counter()
                with self.assertRaises(audit.AuditEvidenceError):
                    audit._indexed_agents()
                self.assertLess(time.perf_counter() - started, 0.5)

    def test_indexed_agents_include_uncheckpointed_wal_rows(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "corpus.db"
            writer = sqlite3.connect(path)
            try:
                self.assertEqual(
                    writer.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute("CREATE TABLE msgs(agent TEXT)")
                writer.commit()
                writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                writer.execute("INSERT INTO msgs VALUES('hidden-agent')")
                writer.commit()
                with mock.patch.object(audit.common, "DATA_DIR", Path(td)):
                    self.assertEqual(audit._indexed_agents(), {"hidden-agent"})
            finally:
                writer.close()

    def test_registry_instrumentation_makes_missing_tally_a_gap(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rollout.jsonl"
            path.write_text("{}\n", encoding="utf-8")
            rc, payload = self._run({}, [("codex", str(path))])
        self.assertEqual(rc, 2)
        self.assertEqual(payload["gaps"], 1)
        self.assertIn("codex: never tallied", payload["gap_details"][0])

    def test_zero_discovery_with_tallies_or_indexed_rows_is_a_gap(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rollout.jsonl"
            path.write_text("{}\n", encoding="utf-8")
            cases = (({str(path): _entry("codex", path)}, ()), ({}, ("codex",)))
            for book, indexed in cases:
                with self.subTest(indexed=bool(indexed)):
                    rc, payload = self._run(book, [], indexed)
                    self.assertEqual(rc, 2)
                    self.assertEqual(payload["gaps"], 1)
                    self.assertIn("zero source files discovered",
                                  payload["gap_details"][0])

    def test_unreadable_tallied_path_is_not_reported_as_deleted(self):
        book = {"/denied/rollout.jsonl": {
            "agent": "codex", "key": "s:1:1", "seen": 1, "rows": 1,
            "agent_rows": 0, "events": 0, "errors": 0, "skips": {},
        }}
        with mock.patch.object(audit, "_path_state",
                               return_value=("unreadable", "permission denied")):
            rc, payload = self._run(book, [])
        self.assertEqual(rc, 2)
        self.assertEqual(payload["orphaned_tallies"], 0)
        self.assertTrue(any("permission denied" in item
                            for item in payload["problems"]))

    def test_clean_banner_requires_and_accepts_a_real_census(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rollout.jsonl"
            path.write_text("{}\n", encoding="utf-8")
            book = {str(path): _entry("codex", path)}
            out = io.StringIO()
            with mock.patch.object(audit, "_book", return_value=book), \
                    mock.patch.object(audit, "_discovered",
                                      return_value=[("codex", str(path))]), \
                    mock.patch.object(audit, "_indexed_agents", return_value=set()), \
                    mock.patch.object(audit, "_live_tokens", return_value={}), \
                    redirect_stdout(out):
                rc = audit.main(["--full"])
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("audit clean: every seen record is accounted for", out.getvalue())

    def test_plain_source_uses_one_kernel_identity_across_crt_ctime_drift(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rollout.jsonl"
            path.write_bytes(b"{}\n")
            crt = audit._entry_identity(path.stat())
            native = (crt[0] + 10, crt[1] + 20, crt[2], crt[3], crt[4] + 30)
            with mock.patch.object(
                    audit.fileops, "file_identity", return_value=native) as by_path, \
                    mock.patch.object(
                        audit.fileops, "file_identity_fd", return_value=native) as by_fd:
                with audit._plain_binary(path) as stream:
                    self.assertEqual(stream.read(), b"{}\n")

        self.assertEqual(by_path.call_args_list, [mock.call(path), mock.call(path)])
        self.assertEqual(by_fd.call_count, 2)

    def test_token_store_requires_exact_live_conversation_census(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.vscdb"
            path.write_bytes(b"sqlite fixture")
            item = {
                "agent": "cursor", "key": "token-old", "seen": 1,
                "rows": 1, "agent_rows": 0, "events": 0,
                "errors": 0, "skips": {},
            }
            row_id = _token_id(path, "conversation")
            book = {row_id: item}
            stale, stale_payload = self._run(
                book, [("cursor", str(path))],
                live_tokens={row_id: ("cursor", "token-new")})
            self.assertEqual(stale, 2)
            self.assertEqual(stale_payload["agents"]["cursor"]["stale"], 1)
            self.assertIn("token census mismatch", stale_payload["gap_details"][0])

            current, current_payload = self._run(
                book, [("cursor", str(path))],
                live_tokens={row_id: ("cursor", "token-old")})
            self.assertEqual(current, 0)
            self.assertEqual(current_payload["problems"], [])
            self.assertEqual(current_payload["gaps"], 0)

    def test_token_identity_preserves_hashes_in_paths_and_sessions(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state#part.vscdb"
            path.write_bytes(b"sqlite fixture")
            session = "session#part"
            canonical = _token_id(path, session)
            item = {
                "agent": "cursor", "key": "token", "seen": 1,
                "rows": 1, "agent_rows": 0, "events": 0,
                "errors": 0, "skips": {},
            }
            rc, payload = self._run(
                {canonical: item}, [("cursor", str(path))],
                live_tokens={canonical: ("cursor", "token")})
            self.assertEqual(rc, 0, payload)

            legacy = f"{path}#{session}"
            rc, payload = self._run(
                {legacy: item}, [("cursor", str(path))],
                live_tokens={canonical: ("cursor", "token")})
            self.assertEqual(rc, 2, payload)
            self.assertTrue(any("invalid token identity" in problem
                                for problem in payload["problems"]))

    def test_legacy_cursor_composite_identity_joins_only_through_live_census(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.vscdb"
            path.write_bytes(b"sqlite fixture")
            session = "02ea9b0a-e792-4925-9984-62c8ec0dc918"
            canonical = _token_id(path, session)
            legacy = f"{path}#{session}"
            item = {
                "agent": "cursor", "key": "token", "seen": 1,
                "rows": 1, "agent_rows": 0, "events": 0,
                "errors": 0, "skips": {},
            }

            rc, payload = self._run(
                {legacy: item}, [("cursor", str(path))],
                live_tokens={canonical: ("cursor", "token")})
            self.assertEqual(rc, 0, payload)

            rc, payload = self._run(
                {canonical: item, legacy: dict(item)},
                [("cursor", str(path))],
                live_tokens={canonical: ("cursor", "token")})
            self.assertEqual(rc, 0, payload)

            conflicting = dict(item, seen=2, rows=2)
            rc, payload = self._run(
                {canonical: item, legacy: conflicting},
                [("cursor", str(path))],
                live_tokens={canonical: ("cursor", "token")})
            self.assertEqual(rc, 2, payload)
            self.assertTrue(any(
                "conflicting tallies for duplicate token identity" in problem
                for problem in payload["problems"]))

            rc, payload = self._run(
                {legacy: item}, [("cursor", str(path))],
                live_tokens={})
            self.assertEqual(rc, 2, payload)
            self.assertTrue(any(
                "invalid token identity" in problem
                for problem in payload["problems"]))

    def test_live_token_source_cannot_hide_outside_discovery(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rollout.jsonl"
            path.write_text("{}\n", encoding="utf-8")
            book = {str(path): _entry("codex", path)}
            ghost = str(Path(td) / "state.vscdb")
            rc, payload = self._run(
                book, [("codex", str(path))],
                live_tokens={_token_id(Path(ghost), "s"):
                             ("cursor", "token")})
        self.assertEqual(rc, 2)
        self.assertTrue(any("token source omitted from discovery" in row
                            for row in payload["gap_details"]))

    def test_agent_scoped_audit_ignores_unrelated_token_churn(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "rollout.jsonl"
            source.write_text("{}\n", encoding="utf-8")
            book = {str(source): _entry("codex", source)}
            cursor = Path(td) / "state.vscdb"
            first = {_token_id(cursor, "s"): ("cursor", "old")}
            second = {_token_id(cursor, "s"): ("cursor", "new")}
            stdout = io.StringIO()
            with mock.patch.object(audit, "_book", return_value=book), \
                    mock.patch.object(
                        audit, "_discovered",
                        return_value=[("codex", str(source))]), \
                    mock.patch.object(
                        audit, "_live_tokens", side_effect=[first, second]), \
                    mock.patch.object(audit, "_indexed_agents", return_value=set()), \
                    redirect_stdout(stdout):
                rc = audit.main([
                    "--agent", "codex", "--strict", "--json", "--full"])
        self.assertEqual(rc, 0, json.loads(stdout.getvalue()))

    def test_token_census_rejects_degraded_and_duplicate_rows(self):
        cases = (
            [{"name": "cursor", "path": "/db", "state": "source-unreadable",
              "kind": "token-census-unreadable", "reason": "denied"}],
            [{"name": "cursor", "id": "/db#s", "key": "a", "state": "token"},
             {"name": "cursor", "id": "/db#s", "key": "b", "state": "token"}],
        )
        for payload in cases:
            with self.subTest(payload=payload), mock.patch.object(
                    audit.subprocess, "run", return_value=mock.Mock(
                        returncode=0, stdout=json.dumps(payload), stderr="")):
                with self.assertRaises(audit.AuditEvidenceError):
                    audit._live_tokens()

    def test_discovery_rejects_global_source_health_issue(self):
        payload = [{"name": "codex", "path": "/ok.jsonl", "state": "available"}, {
            "name": "all", "path": "/data/.source-health.json",
            "state": "source-unreadable", "kind": "health-record-unreadable",
            "reason": "corrupt",
        }]
        completed = mock.Mock(returncode=0, stdout=json.dumps(payload), stderr="")
        with mock.patch.object(audit.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(
                    audit.AuditEvidenceError,
                    "all: /data/.source-health.json: health-record-unreadable: corrupt"):
                audit._discovered()

    def test_real_stores_output_cannot_hide_corrupt_health_record(self):
        binary = ROOT / "target" / "release" / (
            "agrep-rs.exe" if os.name == "nt" else "agrep-rs")
        if not binary.is_file():
            self.skipTest("release agrep-rs has not been built")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = root / "data"
            home = root / "home"
            data.mkdir()
            source = home / ".codex" / "sessions" / "2026" / "07" / "25"
            source.mkdir(parents=True)
            (source / "rollout-fixture.jsonl").write_text("{}\n", encoding="utf-8")
            (data / ".source-health.json").write_text("{", encoding="utf-8")
            with mock.patch.object(audit.common, "ingest_bin", return_value=binary), \
                    mock.patch.dict(os.environ, {
                        "AGREP_DATA_DIR": str(data), "AGREP_HOME": str(home),
                    }, clear=False):
                with self.assertRaisesRegex(
                        audit.AuditEvidenceError, "health-record-unreadable"):
                    audit._discovered()


class LiveIntegrityTests(unittest.TestCase):
    def test_delta_offset_advances_only_after_each_dispatch(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "live.jsonl"
            path.write_bytes(b"one\ntwo\n")
            watcher = LiveWatcher()
            calls = []

            def first(line):
                calls.append(line)
                if line == "two":
                    raise RuntimeError("dispatch failed")

            with self.assertRaisesRegex(RuntimeError, "dispatch failed"):
                watcher._dispatch_delta(str(path), path.stat().st_size, 0.0, first)
            watcher._dispatch_delta(
                str(path), path.stat().st_size, 0.0, calls.append)
        self.assertEqual(calls, ["one", "two", "two"])

    def test_nondict_claude_residue_does_not_drop_batch_completion(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(live_mod, "HOME", td):
            root = Path(td) / ".claude" / "projects" / "proj"
            root.mkdir(parents=True)
            path = root / "session.jsonl"
            ts = "2026-07-25T12:00:00Z"
            records = [
                {"type": "assistant", "sessionId": "session", "timestamp": ts,
                 "message": {"content": [{"type": "tool_use", "id": "c1",
                                             "name": "Bash", "input": {}}]}},
                [],
                {"type": "user", "sessionId": "session", "timestamp": ts,
                 "message": {"content": [{"type": "tool_result",
                                             "tool_use_id": "c1", "content": "ok"}]}},
                {"type": "assistant", "sessionId": "session", "timestamp": ts,
                 "message": {"content": [{"type": "text", "text": "done"}],
                             "stop_reason": "end_turn"}},
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in records),
                            encoding="utf-8")
            expected_size = path.stat().st_size
            watcher = LiveWatcher()
            watcher._booted = True
            events = watcher.subscribe()
            watcher._tick_claude(time.time())
            pushed = []
            while not events.empty():
                pushed.append(events.get_nowait())
        self.assertTrue(any(event.get("type") == "tool_done" for event in watcher.ring))
        self.assertTrue(any(event.get("type") == "done" for event in pushed))
        self.assertEqual(watcher._offsets[str(path)], expected_size)

    def test_snapshot_exposes_boot_and_source_degradation(self):
        watcher = LiveWatcher()
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(live_mod.os, "scandir",
                                  side_effect=PermissionError("denied")):
            self.assertFalse(watcher._refresh_source_health("claude", [td]))
        snapshot = watcher.snapshot()
        self.assertTrue(snapshot["booting"])
        self.assertEqual(snapshot["degraded_sources"][0]["agent"], "claude")
        frame = livetui._frame(
            [], snapshot["now"], True, "all", 120, 30, False,
            last_err=snapshot["last_err"],
            degraded_sources=snapshot["degraded_sources"])
        self.assertIn("live coverage degraded", frame)
        self.assertIn("denied", frame)
        self.assertNotIn("no sessions active", frame)
        plain = livetui._frame(
            [], snapshot["now"], True, "all", 120, 30, False,
            last_err="claude tick failed")
        self.assertIn("live watcher: claude tick failed", plain)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO contract is POSIX-only")
    def test_source_health_rejects_links_and_special_files_without_blocking(self):
        watcher = LiveWatcher()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "target"
            link = root / "link"
            fifo = root / "fifo"
            target.write_bytes(b"safe")
            link.symlink_to(target)
            os.mkfifo(fifo)
            started = time.monotonic()
            self.assertFalse(watcher._refresh_source_health(
                "cursor", [str(link), str(fifo)]))
            self.assertLess(time.monotonic() - started, 0.5)
        paths = {row["path"] for row in watcher.snapshot()["degraded_sources"]}
        self.assertEqual(paths, {str(link), str(fifo)})

    @unittest.skipIf(os.name == "nt", "O_NOFOLLOW is POSIX-only")
    def test_source_probe_rejects_regular_to_link_swap(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source"
            target = root / "target"
            source.write_bytes(b"before")
            target.write_bytes(b"target")
            real_open = live_mod.os.open

            def swap_then_open(path, flags):
                source.unlink()
                source.symlink_to(target)
                return real_open(path, flags)

            with mock.patch.object(live_mod.os, "open", side_effect=swap_then_open):
                with self.assertRaises(OSError):
                    live_mod._probe_source(str(source))

    def test_windows_directory_probe_avoids_crt_directory_open(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(live_mod.os, "name", "nt"), \
                mock.patch.object(live_mod.os, "open") as opened:
            found = live_mod._probe_source(td)
        self.assertTrue(live_mod.stat.S_ISDIR(found.st_mode))
        opened.assert_not_called()

    def test_unhealthy_database_roots_never_reach_pollers(self):
        watcher = LiveWatcher()
        with mock.patch.object(
                watcher, "_refresh_source_health", return_value={}), \
                mock.patch.object(live_mod, "store_roots", return_value=["root"]), \
                mock.patch.object(live_mod, "opencode_db_paths") as opaths, \
                mock.patch.object(live_mod, "_cursor_db_paths", return_value=["db"]), \
                mock.patch.object(watcher, "_poll_opencode_db") as opoll, \
                mock.patch.object(watcher, "_poll_cursor_db") as cpoll:
            watcher._tick_opencode(time.time())
            watcher._tick_cursor(time.time())
        opaths.assert_not_called()
        opoll.assert_not_called()
        cpoll.assert_not_called()

    @unittest.skipIf(os.name == "nt", "symlink creation is privilege-dependent")
    def test_opencode_polls_only_plain_discovered_databases(self):
        watcher = LiveWatcher()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            good = root / "opencode.db"
            bad = root / "opencode-bad.db"
            good.write_bytes(b"db")
            bad.symlink_to(good)
            with mock.patch.object(
                    live_mod, "store_roots", return_value=[str(root)]), \
                    mock.patch.object(
                        live_mod, "opencode_db_paths",
                        return_value=[str(good), str(bad)]), \
                    mock.patch.object(watcher, "_poll_opencode_db") as poll:
                watcher._tick_opencode(time.time())
        poll.assert_called_once_with(str(good), True)
        degraded = watcher.snapshot()["degraded_sources"]
        self.assertEqual([row["path"] for row in degraded], [str(bad)])

    def test_empty_board_discloses_discovery_horizon(self):
        frame = livetui._frame([], 1, True, "all", 120, 30, False)
        self.assertIn("sessions active in the last 90s", frame)
        self.assertIn("quiet long tool calls appear on their next write", frame)

    def test_dead_exact_writer_emits_interrupted_done(self):
        watcher = LiveWatcher()
        watcher._booted = True
        events = watcher.subscribe()
        now = time.time()
        watcher._emit({"agent": "codex", "session": "s", "ts": int(now * 1000),
                       "type": "tool", "name": "shell"})
        events.get_nowait()
        watcher._writer_identity["codex:s"] = (99, "time:1.000000", "codex")
        with mock.patch.object(procscan, "scan_agents_checked", return_value=([], "")), \
                mock.patch.object(live_mod.proc, "pid_alive", return_value=False):
            watcher._refresh_writer_liveness(now)
        event = events.get_nowait()
        self.assertEqual((event["type"], event["why"]), ("done", "interrupted"))
        self.assertFalse(watcher.sessions["codex:s"]["working"])

    def test_transient_process_census_miss_keeps_exact_live_writer(self):
        watcher = LiveWatcher()
        watcher._booted = True
        events = watcher.subscribe()
        now = time.time()
        watcher._emit({"agent": "codex", "session": "s", "ts": int(now * 1000),
                       "type": "tool", "name": "cargo"})
        events.get_nowait()
        watcher._writer_identity["codex:s"] = (99, "proc_birth", "codex")
        with mock.patch.object(procscan, "scan_agents_checked", return_value=([], "")), \
                mock.patch.object(live_mod.proc, "pid_alive", return_value=True), \
                mock.patch.object(
                    live_mod.proc, "process_start_identity", return_value="proc_birth"):
            watcher._refresh_writer_liveness(now)
        self.assertTrue(watcher.sessions["codex:s"]["working"])
        self.assertEqual(watcher.sessions["codex:s"]["live_ts"], now * 1000)
        self.assertIn("codex:s", watcher._writer_identity)
        self.assertTrue(events.empty())

    def test_unverifiable_live_writer_is_not_false_terminalized(self):
        watcher = LiveWatcher()
        watcher._booted = True
        events = watcher.subscribe()
        now = time.time()
        watcher._emit({"agent": "codex", "session": "s", "ts": int(now * 1000),
                       "type": "tool", "name": "cargo"})
        events.get_nowait()
        watcher._writer_identity["codex:s"] = (99, "time:1.000000", "codex")
        with mock.patch.object(procscan, "scan_agents_checked", return_value=([], "")), \
                mock.patch.object(live_mod.proc, "pid_alive", return_value=True), \
                mock.patch.object(
                    live_mod.proc, "process_start_identity", return_value=None):
            watcher._refresh_writer_liveness(now)
        self.assertTrue(watcher.sessions["codex:s"]["working"])
        self.assertEqual(watcher.sessions["codex:s"]["live_ts"], now * 1000)
        self.assertIn("codex:s", watcher._writer_identity)
        self.assertTrue(events.empty())

    def test_reused_writer_pid_emits_interrupted_done(self):
        watcher = LiveWatcher()
        watcher._booted = True
        events = watcher.subscribe()
        now = time.time()
        watcher._emit({"agent": "codex", "session": "s", "ts": int(now * 1000),
                       "type": "tool", "name": "cargo"})
        events.get_nowait()
        watcher._writer_identity["codex:s"] = (99, "proc_old", "codex")
        with mock.patch.object(procscan, "scan_agents_checked", return_value=([], "")), \
                mock.patch.object(live_mod.proc, "pid_alive", return_value=True), \
                mock.patch.object(
                    live_mod.proc, "process_start_identity", return_value="proc_reused"):
            watcher._refresh_writer_liveness(now)
        event = events.get_nowait()
        self.assertEqual((event["type"], event["why"]), ("done", "interrupted"))
        self.assertFalse(watcher.sessions["codex:s"]["working"])

    def test_same_project_process_does_not_claim_an_old_session(self):
        watcher = LiveWatcher()
        watcher._booted = True
        events = watcher.subscribe()
        now = time.time()
        watcher._emit({"agent": "codex", "session": "s", "project": "alpha",
                       "ts": int(now * 1000), "type": "tool", "name": "shell"})
        events.get_nowait()
        process = {"pid": 7, "process_start": "darwin_1_2", "agent": "codex",
                   "session": None, "cwd": "/tmp/alpha", "name": "codex",
                   "cmd": "codex"}
        with mock.patch.object(procscan, "scan_agents_checked",
                               return_value=([process], "")):
            watcher._refresh_writer_liveness(now)
        self.assertNotIn("codex:s", watcher._writer_identity)
        self.assertEqual(watcher.sessions["codex:s"].get("live_ts", 0), 0)
        self.assertTrue(events.empty())

    def test_unidentified_process_does_not_claim_old_working_session(self):
        watcher = LiveWatcher()
        watcher._booted = True
        events = watcher.subscribe()
        now = time.time()
        watcher._emit({"agent": "codex", "session": "dead-a",
                       "ts": int(now * 1000), "type": "tool", "name": "shell"})
        events.get_nowait()
        process = {"pid": 7, "process_start": "darwin_1_2", "agent": "codex",
                   "session": None, "cwd": "", "name": "codex", "cmd": "codex"}
        with mock.patch.object(procscan, "scan_agents_checked",
                               return_value=([process], "")):
            watcher._refresh_writer_liveness(now)
        self.assertNotIn("codex:dead-a", watcher._writer_identity)
        self.assertEqual(watcher.sessions["codex:dead-a"].get("live_ts", 0), 0)
        self.assertTrue(events.empty())

    def test_unique_fallback_does_not_override_contradictory_cwd(self):
        watcher = LiveWatcher()
        watcher._booted = True
        events = watcher.subscribe()
        now = time.time()
        watcher._emit({"agent": "codex", "session": "dead-a",
                       "project": "alpha", "ts": int(now * 1000),
                       "type": "tool", "name": "shell"})
        events.get_nowait()
        process = {"pid": 7, "process_start": "darwin_1_2", "agent": "codex",
                   "session": None, "cwd": "/tmp/beta", "name": "codex",
                   "cmd": "codex"}
        with mock.patch.object(procscan, "scan_agents_checked",
                               return_value=([process], "")):
            watcher._refresh_writer_liveness(now)
        self.assertNotIn("codex:dead-a", watcher._writer_identity)
        self.assertTrue(watcher.sessions["codex:dead-a"]["working"])
        self.assertTrue(events.empty())

    def test_unrelated_new_process_cannot_pin_bound_dead_writer(self):
        watcher = LiveWatcher()
        watcher._booted = True
        events = watcher.subscribe()
        now = time.time()
        watcher._emit({"agent": "codex", "session": "dead-a",
                       "project": "alpha", "ts": int(now * 1000),
                       "type": "tool", "name": "shell"})
        events.get_nowait()
        watcher._writer_identity["codex:dead-a"] = (
            6, "darwin_0_1", "codex")
        replacement = {"pid": 7, "process_start": "darwin_1_2",
                       "agent": "codex", "session": None,
                       "cwd": "/tmp/beta", "name": "codex", "cmd": "codex"}
        with mock.patch.object(procscan, "scan_agents_checked",
                               return_value=([replacement], "")):
            watcher._refresh_writer_liveness(now)
        event = events.get_nowait()
        self.assertEqual((event["type"], event["why"]), ("done", "interrupted"))
        self.assertFalse(watcher.sessions["codex:dead-a"]["working"])

    def test_transcript_read_failure_is_rendered(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "live.jsonl"
            path.write_text("{}\n", encoding="utf-8")
            watcher = LiveWatcher()
            with mock.patch("builtins.open", side_effect=PermissionError("denied")):
                self.assertEqual(watcher._dispatch_delta(
                    str(path), path.stat().st_size, 0.0, lambda _line: None,
                    agent="codex"), 0)
        snapshot = watcher.snapshot()
        self.assertIn("denied", snapshot["last_err"])
        self.assertEqual(snapshot["degraded_sources"][0]["agent"], "codex")
        self.assertEqual(snapshot["degraded_sources"][0]["path"], str(path))

    def test_root_enumeration_failure_is_rendered(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(live_mod, "HOME", td):
            root = Path(td) / ".claude" / "projects"
            root.mkdir(parents=True)
            watcher = LiveWatcher()
            healthy_probe = mock.MagicMock()
            healthy_probe.__enter__.return_value = iter(())
            healthy_probe.__exit__.return_value = False
            with mock.patch.object(
                    live_mod.os, "scandir",
                    side_effect=(healthy_probe, PermissionError("enumeration denied"))):
                watcher._tick_claude(time.time())
        snapshot = watcher.snapshot()
        self.assertIn("enumeration denied", snapshot["last_err"])
        self.assertEqual(snapshot["degraded_sources"][0]["agent"], "claude")
        self.assertEqual(snapshot["degraded_sources"][0]["path"], str(root))

    def test_source_error_clears_after_the_source_recovers(self):
        with tempfile.TemporaryDirectory() as td:
            watcher = LiveWatcher()
            with mock.patch.object(
                    live_mod, "_probe_source",
                    side_effect=PermissionError("denied")):
                watcher._refresh_source_health("claude", [td])
            self.assertIn("denied", watcher.snapshot()["last_err"])
            watcher._refresh_source_health("claude", [td])
        snapshot = watcher.snapshot()
        self.assertEqual(snapshot["last_err"], "")
        self.assertEqual(snapshot["degraded_sources"], [])

    def test_procscan_error_clears_only_after_a_successful_probe(self):
        watcher = LiveWatcher()
        with mock.patch.object(
                procscan, "scan_agents_checked",
                return_value=([], "denied")):
            watcher._refresh_writer_liveness(time.time())
        self.assertIn("procscan unavailable", watcher.snapshot()["last_err"])
        watcher._process_scan_due = 0
        with mock.patch.object(
                procscan, "scan_agents_checked", return_value=([], "")):
            watcher._refresh_writer_liveness(time.time())
        self.assertEqual(watcher.snapshot()["last_err"], "")

    def test_live_writer_prevents_expiry_during_long_tool(self):
        watcher = LiveWatcher()
        watcher._booted = True
        events = watcher.subscribe()
        now = time.time()
        old = int((now - live_mod._SNAPSHOT_EXPIRE_S - 30) * 1000)
        watcher._emit({"agent": "codex", "session": "s", "ts": old,
                       "type": "tool", "name": "cargo"})
        events.get_nowait()
        process = {"pid": 7, "create_ts": 2.0, "agent": "codex",
                   "session": "s", "cwd": "", "name": "codex", "cmd": "codex"}
        with mock.patch.object(procscan, "scan_agents_checked",
                               return_value=([process], "")):
            watcher._refresh_writer_liveness(now)
        snapshot = watcher.snapshot()
        self.assertTrue(snapshot["sessions"][0]["working"])
        self.assertTrue(events.empty())

    def test_working_snapshot_expiry_emits_terminal_event(self):
        watcher = LiveWatcher()
        watcher._booted = True
        events = watcher.subscribe()
        old = int((time.time() - live_mod._SNAPSHOT_EXPIRE_S - 1) * 1000)
        watcher._offsets["/fixture/session.jsonl"] = 17
        watcher._emit({"agent": "claude", "session": "s", "ts": old,
                       "type": "tool", "name": "shell"})
        events.get_nowait()
        snapshot = watcher.snapshot()
        event = events.get_nowait()
        self.assertEqual((event["type"], event["why"]), ("done", "expired"))
        self.assertEqual(event["ts"], old + 1)
        self.assertEqual(snapshot["sessions"], [])
        self.assertNotIn("claude:s", watcher.sessions)
        self.assertEqual(watcher._offsets["/fixture/session.jsonl"], 17)

    def test_pruning_keeps_the_stale_parent_of_an_active_child(self):
        watcher = LiveWatcher()
        watcher._booted = True
        old = int((time.time() - live_mod._SNAPSHOT_EXPIRE_S - 1) * 1000)
        watcher._emit({"agent": "codex", "session": "parent", "ts": old,
                       "type": "user", "text": "old parent"})
        watcher._emit({
            "agent": "codex", "session": "child", "ts": int(time.time() * 1000),
            "type": "user", "text": "active child", "sub_session": True,
            "parent": "parent",
        })
        snapshot = watcher.snapshot()
        sessions = {row["session"]: row for row in snapshot["sessions"]}
        self.assertEqual(set(sessions), {"parent", "child"})
        self.assertFalse(sessions["parent"]["working"])

    def test_headless_boot_drops_touched_stale_history_and_tails_its_delta(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(live_mod, "HOME", td):
            project = Path(td) / ".claude" / "projects" / "fixture"
            project.mkdir(parents=True)
            path = project / "stale.jsonl"
            stale = {
                "type": "assistant", "sessionId": "stale",
                "timestamp": "2020-01-01T00:00:00Z", "cwd": "/work/fixture",
                "message": {"role": "assistant", "content": [{
                    "type": "tool_use", "id": "old-call", "name": "Read",
                    "input": {"file_path": "/work/old.txt"},
                }]},
            }
            path.write_text(json.dumps(stale) + "\n", encoding="utf-8")
            watcher = LiveWatcher(headless_indexd=True)
            watcher._tick_claude(time.time())
            seeded_offset = path.stat().st_size
            self.assertEqual(watcher._offsets[str(path)], seeded_offset)
            self.assertEqual(len(watcher.sessions), 0)
            self.assertEqual(len(watcher.ring), 0)
            self.assertEqual(watcher._claude_pending, {})

            watcher._booted = True
            current = {
                "type": "user", "userType": "external", "sessionId": "stale",
                "timestamp": time.strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                "cwd": "/work/fixture",
                "message": {"role": "user", "content": "new delta"},
            }
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(current) + "\n")
            watcher._tick_claude(time.time())
            final_size = path.stat().st_size
            snapshot = watcher.snapshot()
        self.assertEqual(watcher._offsets[str(path)], final_size)
        self.assertEqual(snapshot["sessions"][0]["session"], "stale")
        self.assertEqual(snapshot["sessions"][0]["recent"][-1]["text"], "new delta")

    def test_macos_process_census_preserves_session_identity(self):
        session = "12345678-1234-1234-1234-123456789abc"
        output = ("  42 Fri Jul 25 12:00:00 2026 /usr/local/bin/codex "
                  f"codex resume {session}\n")
        completed = mock.Mock(returncode=0, stdout=output, stderr="")
        with mock.patch.object(procscan.subprocess, "run", return_value=completed):
            rows = procscan._scan_macos_ps()
        self.assertEqual(rows[0]["session"], session)
        self.assertGreater(rows[0]["create_ts"], 0)

    @unittest.skipUnless(sys.platform == "darwin", "macOS process ABI only")
    def test_macos_native_process_details_are_kernel_backed(self):
        import ctypes

        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        libproc.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int,
                                         ctypes.c_uint64, ctypes.c_void_p,
                                         ctypes.c_int]
        libproc.proc_pidinfo.restype = ctypes.c_int
        self.assertEqual(Path(procscan._mac_cwd(libproc, os.getpid())).resolve(),
                         Path.cwd().resolve())
        process_start, created = procscan._process_birth(os.getpid())
        self.assertTrue(process_start.startswith("darwin_"))
        self.assertGreater(created, 0)


class LiveBootBarrierTests(unittest.TestCase):
    def test_board_once_waits_for_boot_before_rendering(self):
        calls = []

        class Watcher:
            def wait_boot(self, _timeout=None):
                calls.append("boot")
                return True

            def snapshot(self):
                calls.append("snapshot")
                return {"booting": False, "sessions": [], "last_err": "",
                        "degraded_sources": [], "now": 1}

        out = io.StringIO()
        # the barrier under test is the watcher's; a resident daemon's snapshot
        # would answer instead and never reach it
        with mock.patch.object(livetui.live, "watcher", return_value=Watcher()), \
                mock.patch.object(
                    livetui.indexd_runtime, "resident_indexd_live_snapshot",
                    return_value=None), \
                mock.patch.object(livetui.common, "setting", return_value=None), \
                mock.patch.object(livetui, "_enable_ansi", return_value=False), \
                mock.patch.object(sys, "argv", ["livetui.py", "--once"]), \
                redirect_stdout(out), redirect_stderr(io.StringIO()):
            rc = livetui.main()
        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["boot", "snapshot"])
        self.assertIn("last 90s", out.getvalue())

    def test_tail_snapshot_waits_for_boot_before_reading_model(self):
        calls = []

        class Watcher:
            def wait_boot(self, _timeout=None):
                calls.append("boot")
                return True

            def snapshot(self):
                calls.append("snapshot")
                return {"booting": False, "sessions": [], "last_err": ""}

        out = io.StringIO()
        with mock.patch.object(tail.live, "watcher", return_value=Watcher()), \
                mock.patch.object(
                    tail.indexd_runtime, "resident_indexd_live_snapshot",
                    return_value=None), \
                mock.patch.object(sys, "argv", ["tail.py", "--snapshot"]), \
                redirect_stdout(out):
            rc = tail.main()
        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["boot", "snapshot"])
        self.assertFalse(json.loads(out.getvalue())["booting"])

    def test_tail_ready_is_after_subscription_and_boot(self):
        calls = []

        class Watcher:
            def subscribe(self):
                calls.append("subscribe")
                return queue.Queue()

            def wait_boot(self):
                calls.append("boot")
                return True

            def unsubscribe(self, _events):
                calls.append("unsubscribe")

        def stop(obj):
            calls.append(obj["type"])
            raise BrokenPipeError

        with mock.patch.object(tail.live, "watcher", return_value=Watcher()), \
                mock.patch.object(tail, "_line", side_effect=stop), \
                mock.patch.object(sys, "argv", ["tail.py"]):
            rc = tail.main()
        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["subscribe", "boot", "tail_ready", "unsubscribe"])


class DeletionActivityTests(unittest.TestCase):
    """A deletion is activity: shrunk, vanished, or row-dropped stores must stamp
    the wall clock the auto-indexer's scheduling gate reads, or the pass that
    would notice the deletion never gets scheduled."""

    def test_shrunk_store_stamps_activity_with_zero_dispatched_lines(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "live.jsonl"
            path.write_bytes(b"one\ntwo\n")
            watcher = LiveWatcher()
            watcher._booted = True
            watcher._dispatch_delta(str(path), 8, 0.0, lambda _line: None)
            watcher._last_event_wall = 0.0
            path.write_bytes(b"")
            self.assertEqual(
                watcher._dispatch_delta(str(path), 0, 0.0, lambda _line: None),
                0)
            self.assertGreater(watcher._last_event_wall, 0.0)

    def test_boot_seed_rotation_does_not_fake_liveness(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "live.jsonl"
            path.write_bytes(b"one\ntwo\n")
            watcher = LiveWatcher()
            watcher._dispatch_delta(str(path), 8, 0.0, lambda _line: None)
            path.write_bytes(b"")
            watcher._dispatch_delta(str(path), 0, 0.0, lambda _line: None)
            self.assertEqual(watcher._last_event_wall, 0.0)

    def test_vanished_tailed_file_stamps_activity_once(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "live.jsonl"
            path.write_bytes(b"one\n")
            watcher = LiveWatcher()
            watcher._booted = True
            watcher._dispatch_delta(str(path), 4, 0.0, lambda _line: None)
            path.unlink()
            watcher._last_event_wall = 0.0
            watcher._sweep_vanished_paths(time.time())
            self.assertGreater(watcher._last_event_wall, 0.0)
            self.assertNotIn(str(path), watcher._offsets)
            # the second sweep is quiet: this deletion was already reported
            watcher._last_event_wall = 0.0
            watcher._sweep_vanished_paths(time.time() + 60)
            self.assertEqual(watcher._last_event_wall, 0.0)

    def test_cursor_bubble_deletion_stamps_activity_and_reopens_watermark(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "state.vscdb"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)")
            for i in range(3):
                conn.execute("INSERT INTO cursorDiskKV VALUES (?, ?)",
                             (f"bubbleId:c1:b{i}", "not json"))
            conn.commit()
            conn.close()
            watcher = LiveWatcher()
            watcher._booted = True
            watcher._poll_cursor_db(str(db), True, time.time())
            self.assertEqual(watcher._cur_bubbles[str(db)], 3)
            watcher._last_event_wall = 0.0
            conn = sqlite3.connect(db)
            conn.execute("DELETE FROM cursorDiskKV")
            conn.commit()
            conn.close()
            watcher._poll_cursor_db(str(db), False, time.time())
            self.assertGreater(watcher._last_event_wall, 0.0)
            self.assertEqual(watcher._cur_bubbles[str(db)], 0)
            # reused tail rowids stay visible: the watermark reopened to MAX(rowid)
            self.assertEqual(watcher._cur_rowid[str(db)], 0)


if __name__ == "__main__":
    unittest.main()
