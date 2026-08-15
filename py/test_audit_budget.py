"""Audit evidence-book cache, disclosure, and exit-code contracts."""

from __future__ import annotations

import io
import json
import os
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


def _entry(path: Path, *, seen: int) -> dict:
    info = path.stat()
    return {
        "agent": "codex",
        "key": f"s:{int(info.st_mtime_ns // 1_000_000)}:{info.st_size}",
        "seen": seen,
        "rows": seen,
        "agent_rows": 0,
        "events": 0,
        "errors": 0,
        "skips": {},
        "first_error": None,
    }


def _store_snapshot(
        *, selection: str = "all", boundary: str = "b" * 64,
        paths: list[dict] | None = None,
        tokens: list[dict] | None = None,
        issues: list[dict] | None = None,
        complete: bool = True) -> dict:
    return {
        "schema": "agrep.store-audit",
        "version": 1,
        "selection": selection,
        "snapshot_sha256": "a" * 64,
        "boundary_sha256": boundary,
        "paths": paths or [],
        "tokens": tokens or [],
        "issues": issues or [],
        "complete": complete,
    }


class AuditBudgetTests(unittest.TestCase):
    @staticmethod
    def _cache_fixture_is_private(path: Path):
        """Place the fixture outside the simulated Windows shared temp root."""
        shared_temp = path.parent.with_name(
            f"{path.parent.name}-shared-temp")
        return mock.patch.object(
            audit.tempfile, "gettempdir", return_value=os.fspath(shared_temp))

    def _run(
            self, source: Path, book: dict, cache: Path, *argv: str,
            census_side_effect=None) -> tuple[int, dict, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        census_patch = (
            mock.patch.object(
                audit, "_census_jsonl", side_effect=census_side_effect)
            if census_side_effect is not None
            else mock.patch.object(audit, "_census_jsonl", wraps=audit._census_jsonl)
        )
        with mock.patch.object(
                audit, "_census_cache_path", return_value=cache), \
                self._cache_fixture_is_private(cache), \
                mock.patch.object(audit, "_book", return_value=book), \
                mock.patch.object(
                    audit, "_discovered",
                    return_value=[("codex", str(source))]), \
                mock.patch.object(audit, "_indexed_agents", return_value=set()), \
                mock.patch.object(audit, "_live_tokens", return_value={}), \
                census_patch as census, \
                redirect_stdout(stdout), redirect_stderr(stderr):
            rc = audit.main(["--json", *argv])
        payload = json.loads(stdout.getvalue())
        payload["_census_calls"] = census.call_count
        return rc, payload, stderr.getvalue()

    def _run_priority_census_fixture(
            self, *, final_boundary: str = "b" * 64,
            cached_prefix: int = 7,
            census_success_limit: int | None = None,
            ) -> tuple[int, dict, list[str], dict, bytes]:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cache = root / "audit.json"
            book = {}
            discovered = []
            witnesses = {}
            cached = {}
            for index in range(8):
                path = root / f"rollout-{index}.jsonl"
                # newline pinned: pending_bytes assertions count source bytes,
                # and Windows CRLF translation would grow each file to 4 bytes.
                path.write_text("{}\n", encoding="utf-8", newline="\n")
                entry = _entry(path, seen=1)
                book[str(path)] = entry
                discovered.append(("codex", str(path)))
                witness = f"{index + 1:064x}"
                witnesses[("codex", str(path))] = (
                    entry["key"], witness)
                if index < cached_prefix:
                    identity = audit._stat_evidence(str(path))[1]
                    cached[str(path)] = {
                        "identity": list(identity),
                        "identity_sha256": witness,
                        "nonblank_lines": 1,
                    }
            with self._cache_fixture_is_private(cache):
                self.assertTrue(audit._write_census_cache(cache, cached))
            original_cache = cache.read_bytes()
            before = {
                "snapshot_sha256": "a" * 64,
                "boundary_sha256": "b" * 64,
                "selection": "all",
                "discovered": discovered,
                "tokens": {},
                "witnesses": witnesses,
            }
            after = dict(before, boundary_sha256=final_boundary)
            census_paths = []
            real_census = audit._census_jsonl
            source_joins = 0

            def bounded_deadline(deadline, label):
                nonlocal source_joins
                del deadline
                if label == "per-source tally join":
                    source_joins += 1
                    if source_joins > 2:
                        raise audit.AuditRoutineBudget(
                            "synthetic accounting cutoff")

            def observed_census(path, *, deadline=None):
                census_paths.append(path)
                if (census_success_limit is not None
                        and len(census_paths) > census_success_limit):
                    raise audit.AuditRoutineBudget(
                        "synthetic priority census cutoff")
                return real_census(path, deadline=deadline)

            stdout = io.StringIO()
            with mock.patch.object(audit.common, "DATA_DIR", root), \
                    mock.patch.object(audit, "BOOK_PATH", root / "book.json"), \
                    mock.patch.object(
                        audit, "_census_cache_path", return_value=cache), \
                    self._cache_fixture_is_private(cache), \
                    mock.patch.object(audit, "_book", return_value=book), \
                    mock.patch.object(
                        audit, "_combined_store_snapshot",
                        side_effect=[before, after]), \
                    mock.patch.object(
                        audit, "_indexed_agents", return_value=set()), \
                    mock.patch.object(
                        audit, "_deadline_check",
                        side_effect=bounded_deadline), \
                    mock.patch.object(
                        audit, "_census_jsonl",
                        side_effect=observed_census), \
                    redirect_stdout(stdout):
                rc = audit.main(["--json"])
            payload = json.loads(stdout.getvalue())
            stored = json.loads(cache.read_text(encoding="utf-8"))
            return rc, payload, census_paths, stored, original_cache

    def test_cold_routine_recounts_then_full_ignores_and_warm_reuses_cache(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "rollout.jsonl"
            cache = root / "cache" / "audit.json"
            source.write_text("{}\n{}\n", encoding="utf-8")
            book = {str(source): _entry(source, seen=2)}

            cold_rc, cold, _ = self._run(source, book, cache)
            cache_after_cold = cache.exists()
            full_rc, full, _ = self._run(source, book, cache, "--full")
            warm_rc, warm, _ = self._run(
                source, book, cache,
                census_side_effect=AssertionError(
                    "unchanged cached source must not be recounted"))

        self.assertEqual(cold_rc, 0, cold)
        self.assertEqual(cold["census"]["cached_files"], 0)
        self.assertEqual(cold["census"]["recounted_files"], 1)
        self.assertEqual(cold["census"]["pending_files"], 0)
        self.assertEqual(cold["_census_calls"], 1)
        self.assertTrue(cache_after_cold)
        self.assertIn("routine recounted 1", cold["census"]["disclosure"])
        self.assertEqual(full_rc, 0, full)
        self.assertEqual(full["census"]["recounted_files"], 1)
        self.assertEqual(full["_census_calls"], 1)
        self.assertEqual(warm_rc, 0, warm)
        self.assertEqual(warm["census"]["cached_files"], 1)
        self.assertEqual(warm["census"]["recounted_files"], 0)
        self.assertEqual(warm["census"]["pending_files"], 0)
        self.assertEqual(warm["_census_calls"], 0)
        self.assertIn("1 file verified from cache", warm["census"]["disclosure"])
        self.assertIn("--full ignores cache", warm["census"]["disclosure"])

    def test_restored_mtime_does_not_fool_identity_keyed_cache(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "rollout.jsonl"
            cache = root / "cache" / "audit.json"
            source.write_text("a\n", encoding="utf-8")
            original_mtime = source.stat().st_mtime_ns
            first_book = {str(source): _entry(source, seen=1)}
            self.assertEqual(
                self._run(source, first_book, cache, "--full")[0], 0)

            source.write_text("b\n", encoding="utf-8")
            os.utime(source, ns=(original_mtime, original_mtime))
            second_book = {str(source): _entry(source, seen=1)}
            rc, payload, _ = self._run(source, second_book, cache)

        self.assertEqual(rc, 0, payload)
        self.assertEqual(payload["census"]["cached_files"], 0)
        self.assertEqual(payload["census"]["recounted_files"], 1)
        self.assertEqual(payload["census"]["pending_files"], 0)
        self.assertEqual(payload["_census_calls"], 1)

    def test_optional_and_sqlite_boundaries_use_change_sensitive_identity(self):
        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "corpus.db"
            database.write_bytes(b"same-size database")
            wal = Path(f"{database}-wal")
            wal.write_bytes(b"same-size wal")
            before = (11, 22, database.stat().st_size, 33, 44)
            wal_before = (55, 66, wal.stat().st_size, 77, 88)

            def identities(main, sidecar):
                def identity(path):
                    if Path(path) == database:
                        return main
                    if Path(path) == wal:
                        return sidecar
                    raise FileNotFoundError(path)
                return identity

            with mock.patch.object(
                    audit.fileops, "change_sensitive_file_identity",
                    side_effect=identities(before, wal_before)):
                optional_before = audit._optional_plain_identity(database)
                family_before = audit._sqlite_family_identity(database)
            main_after = (*before[:-1], 45)
            with mock.patch.object(
                    audit.fileops, "change_sensitive_file_identity",
                    side_effect=identities(main_after, wal_before)):
                optional_after = audit._optional_plain_identity(database)
                family_main_after = audit._sqlite_family_identity(database)
            wal_after = (*wal_before[:-1], 89)
            with mock.patch.object(
                    audit.fileops, "change_sensitive_file_identity",
                    side_effect=identities(before, wal_after)):
                family_wal_after = audit._sqlite_family_identity(database)

        self.assertEqual(optional_before, before)
        self.assertEqual(optional_after, main_after)
        self.assertNotEqual(optional_before, optional_after)
        self.assertNotEqual(family_before, family_main_after)
        self.assertNotEqual(family_before, family_wal_after)
        cached = audit._indexed_cache_record(family_before, {"codex"})
        self.assertTrue(audit._indexed_cache_matches(cached, family_before))
        self.assertFalse(
            audit._indexed_cache_matches(cached, family_main_after))
        self.assertFalse(
            audit._indexed_cache_matches(cached, family_wal_after))

    @unittest.skipUnless(os.name == "nt", "native Windows change-token contract")
    def test_windows_sqlite_family_rejects_restored_mtime_rewrites(self):
        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "corpus.db"
            wal = Path(f"{database}-wal")
            database.write_bytes(b"main-before")
            wal.write_bytes(b"wal-before")
            baseline = audit._sqlite_family_identity(database)

            original = database.stat().st_mtime_ns
            database.write_bytes(b"main-after!")
            os.utime(database, ns=(original, original))
            main_after = audit._sqlite_family_identity(database)

            database.write_bytes(b"main-before")
            os.utime(database, ns=(original, original))
            restored_main = audit._sqlite_family_identity(database)
            original_wal = wal.stat().st_mtime_ns
            wal.write_bytes(b"wal-after!")
            os.utime(wal, ns=(original_wal, original_wal))
            wal_after = audit._sqlite_family_identity(database)

        before_members = dict(baseline)
        main_members = dict(main_after)
        restored_members = dict(restored_main)
        wal_members = dict(wal_after)
        self.assertEqual(before_members[""][:4], main_members[""][:4])
        self.assertNotEqual(before_members[""][4], main_members[""][4])
        self.assertEqual(
            restored_members["-wal"][:4], wal_members["-wal"][:4])
        self.assertNotEqual(
            restored_members["-wal"][4], wal_members["-wal"][4])

    def test_routine_budget_expiry_is_a_visible_pending_census(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "rollout.jsonl"
            cache = root / "cache" / "audit.json"
            source.write_text("{}\n", encoding="utf-8")
            book = {str(source): _entry(source, seen=1)}

            rc, payload, _ = self._run(
                source, book, cache,
                census_side_effect=audit.AuditRoutineBudget(
                    "routine audit census budget exhausted"))
            cache_created = cache.exists()

        self.assertEqual(rc, 1, payload)
        self.assertEqual(payload["census"]["recounted_files"], 0)
        self.assertEqual(payload["census"]["pending_files"], 1)
        self.assertEqual(payload["_census_calls"], 1)
        self.assertFalse(cache_created)
        self.assertIn("routine budget", payload["gap_details"][0])
        self.assertIn("run agrep audit --full", payload["gap_details"][0])

    def test_chunked_census_preserves_nonblank_line_semantics(self):
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "rollout.jsonl"
            source.write_bytes(b"\n \t\r\n{}\r\nabc\n  def  ")
            with mock.patch.object(audit, "_CENSUS_CHUNK_BYTES", 3):
                self.assertEqual(audit._census_jsonl(str(source)), 3)

    def test_source_change_during_recount_is_an_error_and_is_not_cached(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "rollout.jsonl"
            cache = root / "cache" / "audit.json"
            source.write_text("{}\n", encoding="utf-8")
            book = {str(source): _entry(source, seen=1)}

            def mutate_during_census(_path, *, deadline=None):
                del deadline
                source.write_text("{}\n{}\n", encoding="utf-8")
                return 2

            rc, payload, _stderr = self._run(
                source, book, cache, "--full",
                census_side_effect=mutate_during_census)

            self.assertEqual(rc, 2, payload)
            self.assertTrue(any(
                "source changed while census evidence was collected" in problem
                for problem in payload["problems"]))
            self.assertFalse(cache.exists())

    def test_cache_substitution_during_snapshot_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cache = root / "audit.json"
            replacement = root / "replacement.json"
            cache.write_text(
                '{"version":1,"files":{"/source":{"identity":[1,2,3,4,5],'
                '"nonblank_lines":1}}}\n', encoding="utf-8")
            replacement.write_text(
                '{"version":1,"files":{"/source":{"identity":[1,2,3,4,5],'
                '"nonblank_lines":999}}}\n', encoding="utf-8")
            real_snapshot = audit.ownerfile.snapshot

            def substitute_during_snapshot(path, *, max_bytes):
                snapshot = real_snapshot(path, max_bytes=max_bytes)
                os.replace(replacement, cache)
                return snapshot

            with (
                self._cache_fixture_is_private(cache),
                mock.patch.object(
                    audit.ownerfile, "snapshot",
                    side_effect=substitute_during_snapshot),
            ):
                self.assertEqual(audit._read_census_cache(cache), {})

    def test_cache_snapshot_must_name_the_bracketed_entry(self):
        with tempfile.TemporaryDirectory() as raw:
            cache = Path(raw) / "audit.json"
            cache.write_text(
                '{"version":1,"files":{}}\n', encoding="utf-8")
            identity = audit.fileops.file_identity(cache)
            substituted = audit.ownerfile.Snapshot(
                (identity[0], identity[1] + 1, identity[2], identity[3]),
                cache.stat().st_mtime,
                b'{"version":1,"files":{"/forged":{"identity":'
                b'[1,2,3,4,5],"nonblank_lines":99}}}\n',
            )
            with (
                self._cache_fixture_is_private(cache),
                mock.patch.object(
                    audit.fileops, "change_sensitive_file_identity",
                    side_effect=(identity, identity)),
                mock.patch.object(
                    audit.ownerfile, "snapshot",
                    return_value=substituted),
            ):
                self.assertEqual(audit._read_census_cache(cache), {})

    def test_windows_cache_snapshot_accepts_equivalent_device_encodings(self):
        with tempfile.TemporaryDirectory() as raw:
            cache = Path(raw) / "audit.json"
            payload = (
                b'{"version":1,"files":{"/source":{"identity":'
                b'[1,2,3,4,5],"nonblank_lines":1}}}\n')
            cache.write_bytes(payload)
            strong = (606183915, 23362423067208041, len(payload), 1234, 5678)
            snapshot = audit.ownerfile.Snapshot(
                (10818809305384460779, *strong[1:4]), 0.0, payload)
            with mock.patch.object(audit.os, "name", "nt"), \
                    self._cache_fixture_is_private(cache), \
                    mock.patch.object(
                        audit.fileops, "change_sensitive_file_identity",
                        side_effect=(strong, strong)), \
                    mock.patch.object(
                        audit.ownerfile, "snapshot", return_value=snapshot):
                cached = audit._read_census_cache(cache)

            substituted = audit.ownerfile.Snapshot(
                (snapshot.identity[0], strong[1] + 1, *strong[2:4]),
                0.0, payload)
            with mock.patch.object(audit.os, "name", "nt"), \
                    self._cache_fixture_is_private(cache), \
                    mock.patch.object(
                        audit.fileops, "change_sensitive_file_identity",
                        side_effect=(strong, strong)), \
                    mock.patch.object(
                        audit.ownerfile, "snapshot", return_value=substituted):
                rejected = audit._read_census_cache(cache)

        self.assertEqual(cached["/source"]["nonblank_lines"], 1)
        self.assertEqual(rejected, {})

    def test_successful_full_prunes_deleted_rotated_and_stale_entries(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "rollout.jsonl"
            cache = root / "cache" / "audit.json"
            source.write_text("{}\n", encoding="utf-8")
            cache.parent.mkdir()
            cache.write_text(json.dumps({
                "version": audit.CACHE_VERSION,
                "files": {
                    str(source): {
                        "identity": [1, 2, 3, 4, 5],
                        "nonblank_lines": 99,
                    },
                    str(root / "deleted.jsonl"): {
                        "identity": [6, 7, 8, 9, 10],
                        "nonblank_lines": 8,
                    },
                },
            }) + "\n", encoding="utf-8")
            book = {str(source): _entry(source, seen=1)}

            rc, payload, _stderr = self._run(
                source, book, cache, "--full")
            stored = json.loads(cache.read_text(encoding="utf-8"))

        self.assertEqual(rc, 0, payload)
        self.assertEqual(list(stored["files"]), [str(source)])
        self.assertNotEqual(
            stored["files"][str(source)]["identity"], [1, 2, 3, 4, 5])
        self.assertEqual(
            stored["files"][str(source)]["nonblank_lines"], 1)

    def test_full_is_trustless_recount_with_printed_estimate(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "rollout.jsonl"
            cache = root / "cache" / "audit.json"
            source.write_text("{}\n", encoding="utf-8")
            book = {str(source): _entry(source, seen=1)}
            self.assertEqual(self._run(source, book, cache, "--full")[0], 0)

            rc, payload, stderr = self._run(source, book, cache, "--full")

        self.assertEqual(rc, 0, payload)
        self.assertTrue(payload["census"]["full"])
        self.assertEqual(payload["census"]["cached_files"], 0)
        self.assertEqual(payload["census"]["recounted_files"], 1)
        self.assertEqual(payload["_census_calls"], 1)
        self.assertIn("full audit:", stderr)
        self.assertIn("estimated", stderr)
        self.assertNotIn("exit 0", stderr)
        self.assertIn("trustless recount", payload["census"]["disclosure"])

    def test_readonly_data_dir_never_uses_an_unsafe_scratch_cache(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            protected = root / "protected"
            protected.mkdir()
            source = root / "rollout.jsonl"
            source.write_text("{}\n", encoding="utf-8")
            book = {str(source): _entry(source, seen=1)}
            protected_cache = protected / ".audit-census.json"
            scratch = root / "scratch"
            scratch.mkdir()
            stdout = io.StringIO()
            with mock.patch.object(audit.common, "DATA_DIR", protected), \
                    mock.patch.object(audit, "CACHE_PATH", protected_cache), \
                    mock.patch.object(
                        audit.tempfile, "gettempdir", return_value=str(scratch)), \
                    mock.patch.dict(
                        os.environ,
                        {"AGREP_DATA_READONLY": str(protected)}, clear=False), \
                    mock.patch.object(audit, "_book", return_value=book), \
                    mock.patch.object(
                        audit, "_discovered",
                        return_value=[("codex", str(source))]), \
                    mock.patch.object(
                        audit, "_indexed_agents", return_value=set()), \
                    mock.patch.object(audit, "_live_tokens", return_value={}), \
                    redirect_stdout(stdout):
                rc = audit.main(["--json", "--full"])
            payload = json.loads(stdout.getvalue())

            self.assertEqual(rc, 0, payload)
            self.assertFalse(protected_cache.exists())
            scratch_files = list(scratch.rglob("*.json"))
            if os.name == "nt":
                self.assertEqual(scratch_files, [])
                self.assertIn(
                    "census cache unavailable",
                    payload["census"]["disclosure"])
            else:
                self.assertEqual(len(scratch_files), 1)
                cached = json.loads(
                    scratch_files[0].read_text(encoding="utf-8"))
                self.assertEqual(cached["version"], audit.CACHE_VERSION)

    @unittest.skipUnless(hasattr(os, "geteuid"), "uid ownership is POSIX-only")
    def test_scratch_cache_from_another_uid_is_never_trusted_or_replaced(self):
        with tempfile.TemporaryDirectory() as raw:
            cache = Path(raw) / "audit.json"
            cache.write_text(
                '{"version":1,"files":{"/source":{"identity":[1,2,3,4,5],'
                '"nonblank_lines":99}}}\n', encoding="utf-8")
            original = cache.read_bytes()
            foreign_uid = int(cache.stat().st_uid) + 1
            with mock.patch.object(
                    audit.os, "geteuid", return_value=foreign_uid):
                self.assertEqual(audit._read_census_cache(cache), {})
                self.assertFalse(audit._write_census_cache(cache, {}))
            self.assertEqual(cache.read_bytes(), original)

    def test_windows_shared_temp_cache_is_never_trusted_or_replaced(self):
        with tempfile.TemporaryDirectory() as raw:
            cache = Path(raw) / "audit.json"
            cache.write_text(
                '{"version":1,"files":{"/source":{"identity":[1,2,3,4,5],'
                '"nonblank_lines":99}}}\n', encoding="utf-8")
            cache.chmod(0o666)
            original = cache.read_bytes()
            with mock.patch.object(audit.os, "name", "nt"), \
                    mock.patch.object(
                        audit.tempfile, "gettempdir", return_value=raw):
                self.assertFalse(audit._cache_entry_owned(cache))
                self.assertEqual(audit._read_census_cache(cache), {})
                self.assertFalse(audit._write_census_cache(cache, {}))
            self.assertEqual(cache.read_bytes(), original)

    @unittest.skipIf(os.name == "nt", "POSIX provenance modes")
    def test_group_or_world_writable_cache_is_never_authoritative(self):
        with tempfile.TemporaryDirectory() as raw:
            cache = Path(raw) / "audit.json"
            cache.write_text('{"version":1,"files":{}}\n', encoding="utf-8")
            cache.chmod(0o666)
            self.assertEqual(audit._read_census_cache(cache), {})
            self.assertFalse(audit._write_census_cache(cache, {}))

    def test_store_snapshots_time_out_as_explicit_evidence_errors(self):
        expired = subprocess.TimeoutExpired(
            ["agrep-rs", "stores", "--paths"], 0.025)
        with mock.patch.object(audit.subprocess, "run", side_effect=expired):
            with self.assertRaisesRegex(
                    audit.AuditEvidenceError,
                    "store discovery unavailable.*timed out after 0.025s"):
                audit._discovered(timeout_s=0.025)

    def test_combined_store_snapshot_schema_is_strict(self):
        path = {
            "name": "codex",
            "path": "/history/rollout.jsonl",
            "stat_key": "s:1:2",
            "identity_sha256": "c" * 64,
        }
        token = {"name": "cursor", "id": "row", "key": "token"}
        valid = _store_snapshot(paths=[path], tokens=[token])
        parsed = audit._parse_store_audit_payload(json.dumps(valid))
        self.assertEqual(parsed["discovered"], [
            ("codex", "/history/rollout.jsonl")])
        self.assertEqual(
            parsed["witnesses"][("codex", "/history/rollout.jsonl")],
            ("s:1:2", "c" * 64))
        self.assertEqual(parsed["tokens"], {"row": ("cursor", "token")})

        invalid_payloads = []
        extra = dict(valid, extra=True)
        invalid_payloads.append(extra)
        uppercase = dict(valid, snapshot_sha256="A" * 64)
        invalid_payloads.append(uppercase)
        duplicate = dict(valid, paths=[path, dict(path)])
        invalid_payloads.append(duplicate)
        malformed_row = dict(valid, paths=[dict(path, surprise=True)])
        invalid_payloads.append(malformed_row)
        wrong_selection = dict(valid, selection="codex")
        invalid_payloads.append(wrong_selection)
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(
                    audit.AuditEvidenceError):
                audit._parse_store_audit_payload(json.dumps(payload))
        with self.assertRaisesRegex(
                audit.AuditEvidenceError, "duplicate evidence field"):
            audit._parse_store_audit_payload(
                '{"schema":"agrep.store-audit","schema":"duplicate"}')

    def test_combined_snapshot_degradation_is_evidence_error(self):
        issue = {
            "agent": "cursor",
            "path": "/cursor/state.vscdb",
            "kind": "permission-denied",
            "reason": "denied",
        }
        with self.assertRaisesRegex(
                audit.AuditEvidenceError,
                "store audit snapshot is degraded.*permission-denied"):
            audit._parse_store_audit_payload(json.dumps(_store_snapshot(
                issues=[issue], complete=False)))

    def test_production_audit_dispatch_collects_two_combined_boundaries(self):
        parsed = {
            "snapshot_sha256": "a" * 64,
            "boundary_sha256": "b" * 64,
            "selection": "all",
            "discovered": [],
            "tokens": {},
            "witnesses": {},
        }
        stdout = io.StringIO()
        with mock.patch.object(audit, "_census_cache_path", return_value=None), \
                mock.patch.object(audit, "_book", return_value={}), \
                mock.patch.object(
                    audit, "_combined_store_snapshot", return_value=parsed) as combined, \
                mock.patch.object(audit, "_indexed_agents", return_value=set()), \
                redirect_stdout(stdout):
            audit.main(["--json"])
        self.assertEqual(combined.call_count, 2)
        self.assertTrue(all(
            call.kwargs["selection"] == "all"
            and 0 < call.kwargs["timeout_s"] <= audit._ROUTINE_STORE_TIMEOUT_S
            for call in combined.call_args_list))

    def test_cache_witness_must_match_and_retains_native_identity(self):
        cache = {
            "/history/rollout.jsonl": {
                "identity": [1, 2, 3, 4, 5],
                "identity_sha256": "a" * 64,
                "nonblank_lines": 7,
            },
        }
        self.assertEqual(audit._cache_count(
            cache, "/history/rollout.jsonl",
            identity_sha256="a" * 64), 7)
        self.assertIsNone(audit._cache_count(
            cache, "/history/rollout.jsonl",
            identity_sha256="b" * 64))
        self.assertEqual(
            cache["/history/rollout.jsonl"]["identity"],
            [1, 2, 3, 4, 5])

    def test_priority_census_progresses_past_a_cached_prefix(self):
        rc, payload, census_paths, stored, _original = (
            self._run_priority_census_fixture())
        suffix = census_paths[0]
        self.assertEqual(rc, 1, payload)
        self.assertEqual(census_paths, [suffix])
        self.assertTrue(suffix.endswith("rollout-7.jsonl"))
        self.assertEqual(payload["census"]["cached_files"], 7)
        self.assertEqual(payload["census"]["recounted_files"], 1)
        self.assertEqual(payload["census"]["pending_files"], 0)
        self.assertEqual(payload["census"]["pending_bytes"], 0)
        self.assertIn(suffix, stored["files"])
        self.assertIn("synthetic accounting cutoff",
                      payload["routine"]["deferred_reason"])

    def test_priority_census_cutoff_reports_only_the_missing_remainder(self):
        rc, payload, census_paths, stored, _original = (
            self._run_priority_census_fixture(
                cached_prefix=5, census_success_limit=1))
        self.assertEqual(rc, 1, payload)
        self.assertEqual(len(census_paths), 2)
        self.assertTrue(census_paths[0].endswith("rollout-5.jsonl"))
        self.assertEqual(payload["census"]["cached_files"], 5)
        self.assertEqual(payload["census"]["recounted_files"], 1)
        self.assertEqual(payload["census"]["pending_files"], 2)
        self.assertEqual(payload["census"]["pending_bytes"], 6)
        self.assertEqual(len(stored["files"]), 6)
        self.assertIn(
            "synthetic priority census cutoff",
            payload["routine"]["deferred_reason"])

    def test_priority_census_cache_update_is_withheld_on_boundary_change(self):
        rc, payload, census_paths, stored, original = (
            self._run_priority_census_fixture(final_boundary="c" * 64))
        self.assertEqual(rc, 2, payload)
        self.assertEqual(len(census_paths), 1)
        self.assertEqual(
            stored,
            json.loads(original.decode("utf-8")))
        self.assertEqual(
            payload["census"]["cache"],
            "update withheld: store boundary was not stable")
        self.assertTrue(any(
            "store source evidence changed during audit" in problem
            for problem in payload["problems"]))

    def test_real_combined_snapshot_matches_legacy_path_and_token_sets(self):
        binary = ROOT / "target" / "release" / (
            "agrep-rs.exe" if os.name == "nt" else "agrep-rs")
        if not binary.is_file():
            self.skipTest("release agrep-rs has not been built")

        # A hang guard, not a budget: this audits the box's real stores (0.3s
        # of work) and what it pins is snapshot consistency. How fast the audit
        # runs is bench/perf.py's contract, measured on an idle machine.
        def run(*flags: str):
            result = subprocess.run(
                [str(binary), "stores", *flags],
                capture_output=True, text=True, encoding="utf-8",
                errors="strict", timeout=120.0, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        mismatch = None
        for _attempt in range(3):
            combined = run("--audit", "--agent", "all")
            legacy_paths = run("--paths")
            legacy_tokens = run("--tokens")
            combined_paths = {
                (row["name"], row["path"]) for row in combined["paths"]}
            expected_paths = {
                (row["name"], row["path"]) for row in legacy_paths
                if row.get("state") == "available"}
            combined_tokens = {
                (row["name"], row["id"], row["key"])
                for row in combined["tokens"]}
            expected_tokens = {
                (row["name"], row["id"], row["key"])
                for row in legacy_tokens if row.get("state") == "token"}
            if (combined_paths == expected_paths
                    and combined_tokens == expected_tokens):
                mismatch = None
                break
            mismatch = (
                combined_paths ^ expected_paths,
                combined_tokens ^ expected_tokens)
        self.assertIsNone(mismatch, mismatch)

    def test_routine_store_timeout_is_one_deferred_warning(self):
        expired = subprocess.TimeoutExpired(
            ["agrep-rs", "stores", "--tokens"], 0.15)
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "rollout.jsonl"
            source.write_text("{}\n", encoding="utf-8")
            stdout = io.StringIO()
            with mock.patch.object(
                    audit, "_census_cache_path", return_value=None), \
                    mock.patch.object(
                        audit, "_book",
                        return_value={str(source): _entry(source, seen=1)}), \
                    mock.patch.object(
                        audit.subprocess, "run", side_effect=expired), \
                    redirect_stdout(stdout):
                rc = audit.main(["--json", "--strict"])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(rc, 1, payload)
        self.assertEqual(payload["problem_count"], 0)
        self.assertEqual(payload["gaps"], 1)
        self.assertIn("store snapshot timed out", payload["gap_details"][0])

    def test_routine_store_snapshot_caps_share_one_hard_deadline(self):
        observed = []

        def discovered(*, timeout_s):
            observed.append(("discovery", timeout_s))
            return []

        def tokens(*, timeout_s):
            observed.append(("tokens", timeout_s))
            return {}

        stdout = io.StringIO()
        with mock.patch.object(audit, "_census_cache_path", return_value=None), \
                mock.patch.object(audit, "_book", return_value={}), \
                mock.patch.object(audit, "_discovered", side_effect=discovered), \
                mock.patch.object(audit, "_indexed_agents", return_value=set()), \
                mock.patch.object(audit, "_live_tokens", side_effect=tokens), \
                redirect_stdout(stdout):
            rc = audit.main(["--json"])

        self.assertEqual(rc, 2, stdout.getvalue())
        self.assertEqual(
            [name for name, _timeout in observed],
            ["tokens", "discovery", "tokens", "discovery"])
        self.assertTrue(all(
            0 < timeout <= audit._ROUTINE_STORE_TIMEOUT_S
            for _name, timeout in observed))

    def test_expired_routine_deadline_does_not_launch_another_snapshot(self):
        with mock.patch.object(audit.time, "monotonic", return_value=10.0):
            with self.assertRaisesRegex(
                    audit.AuditEvidenceError,
                    "token census unavailable: routine audit budget exhausted"):
                audit._store_snapshot_timeout(9.0, "token census")

    def test_oversized_routine_book_defers_before_json_parse(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            book_path = root / "intake_stats.json"
            with book_path.open("wb") as stream:
                stream.truncate(audit._ROUTINE_JSON_PARSE_MAX_BYTES + 1)
            stdout = io.StringIO()
            started = time.monotonic()
            with mock.patch.object(audit, "BOOK_PATH", book_path), \
                    mock.patch.object(audit, "_census_cache_path", return_value=None), \
                    mock.patch.object(
                        audit.ownerfile, "snapshot",
                        side_effect=AssertionError("oversized JSON was parsed")), \
                    mock.patch.object(
                        audit, "_discovered",
                        side_effect=AssertionError("work continued after cutoff")), \
                    mock.patch.object(
                        audit, "_live_tokens",
                        side_effect=AssertionError("work continued after cutoff")), \
                    redirect_stdout(stdout):
                rc = audit.main(["--json", "--strict"])
            elapsed = time.monotonic() - started
        payload = json.loads(stdout.getvalue())
        self.assertEqual(rc, 1, payload)
        self.assertLess(elapsed, 0.5)
        self.assertEqual(payload["problem_count"], 0)
        self.assertEqual(payload["gaps"], 1)
        self.assertFalse(payload["routine"]["complete"])
        self.assertIn(
            "pending total was not established",
            payload["census"]["disclosure"])

    def test_oversized_cache_defers_acceleration_but_can_recount_cleanly(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "rollout.jsonl"
            source.write_text("{}\n", encoding="utf-8")
            cache = root / "audit.json"
            with cache.open("wb") as stream:
                stream.truncate(audit._ROUTINE_JSON_PARSE_MAX_BYTES + 1)
            rc, payload, _stderr = self._run(
                source, {str(source): _entry(source, seen=1)}, cache)
        self.assertEqual(rc, 0, payload)
        self.assertIn(
            "census-cache acceleration deferred",
            payload["census"]["disclosure"])
        self.assertEqual(payload["census"]["recounted_files"], 1)

    def test_large_unproven_or_unindexed_sqlite_never_opens_snapshot(self):
        cases = (
            (
                b"not a sqlite header!!",
                audit._ROUTINE_SQLITE_COPY_MAX_BYTES + 1,
                "routine copy tier",
            ),
            (
                b"SQLite format 3\0\x00\x00\x01\x01",
                audit._ROUTINE_INDEXED_SCAN_MAX_BYTES + 1,
                "unindexed scan tier",
            ),
        )
        for header, size, phrase in cases:
            with self.subTest(phrase=phrase), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                database = root / "corpus.db"
                with database.open("wb") as stream:
                    stream.write(header)
                    stream.truncate(size)
                started = time.monotonic()
                with mock.patch.object(audit.common, "DATA_DIR", root), \
                        mock.patch.object(
                            audit.events, "open_sqlite_snapshot",
                            side_effect=AssertionError(
                                "an unbounded snapshot was opened")):
                    with self.assertRaisesRegex(
                            audit.AuditRoutineBudget, phrase):
                        audit._indexed_agents(
                            deadline=time.monotonic() + 0.5)
                self.assertLess(time.monotonic() - started, 0.2)

    def test_distinct_progress_handler_interrupts_at_deadline(self):
        class InterruptibleDb:
            def __init__(self):
                self.progress = None
                self.cleared = False
                self.closed = False

            def set_progress_handler(self, callback, _ops):
                self.progress = callback
                if callback is None:
                    self.cleared = True

            def execute(self, _query):
                while self.progress is not None and not self.progress():
                    pass
                raise sqlite3.OperationalError("interrupted")

            def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            database = root / "corpus.db"
            database.write_bytes(
                b"SQLite format 3\0\x00\x00\x01\x01")
            connection = InterruptibleDb()
            with mock.patch.object(audit.common, "DATA_DIR", root), \
                    mock.patch.object(
                        audit.events, "open_sqlite_snapshot",
                        return_value=connection):
                with self.assertRaisesRegex(
                        audit.AuditRoutineBudget, "DISTINCT census"):
                    audit._indexed_agents(
                        deadline=time.monotonic() + 0.005)
        self.assertTrue(connection.cleared)
        self.assertTrue(connection.closed)

    def test_matching_indexed_identity_cache_skips_distinct_query(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "rollout.jsonl"
            source.write_text("{}\n", encoding="utf-8")
            book_path = root / "intake_stats.json"
            book_path.write_text(json.dumps({
                "version": audit.BOOK_VERSION,
                "files": {str(source): _entry(source, seen=1)},
            }), encoding="utf-8")
            database = root / "corpus.db"
            db = sqlite3.connect(database)
            try:
                db.execute("CREATE TABLE msgs(agent TEXT)")
                db.execute("INSERT INTO msgs VALUES('codex')")
                db.commit()
            finally:
                db.close()
            cache = root / "audit.json"
            identity = audit._sqlite_family_identity(database)
            source_identity = audit._stat_evidence(str(source))[1]
            with self._cache_fixture_is_private(cache):
                self.assertTrue(audit._write_census_cache(
                    cache,
                    {str(source): {
                        "identity": list(source_identity),
                        "nonblank_lines": 1,
                    }},
                    indexed_agents=audit._indexed_cache_record(
                        identity, {"codex"})))
            stdout = io.StringIO()
            with mock.patch.object(audit.common, "DATA_DIR", root), \
                    mock.patch.object(audit, "BOOK_PATH", book_path), \
                    mock.patch.object(
                        audit, "_census_cache_path", return_value=cache), \
                    self._cache_fixture_is_private(cache), \
                    mock.patch.object(
                        audit, "_discovered",
                        return_value=[("codex", str(source))]), \
                    mock.patch.object(audit, "_live_tokens", return_value={}), \
                    mock.patch.object(
                        audit.events, "open_sqlite_snapshot",
                        side_effect=AssertionError("DISTINCT query was rerun")), \
                    redirect_stdout(stdout):
                rc = audit.main(["--json"])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(rc, 0, payload)
        self.assertTrue(payload["routine"]["indexed_agents_from_cache"])

    def test_unrelated_error_still_publishes_completed_file_census(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "rollout.jsonl"
            source.write_text("{}\n", encoding="utf-8")
            cache = root / "audit.json"
            broken = _entry(source, seen=1)
            broken["rows"] = 0
            first_rc, first, _stderr = self._run(
                source, {str(source): broken}, cache)
            second_rc, second, _stderr = self._run(
                source, {str(source): broken}, cache,
                census_side_effect=AssertionError(
                    "completed identity-bound census was withheld"))
            cache_exists = cache.exists()
        self.assertEqual(first_rc, 2, first)
        self.assertEqual(first["census"]["recounted_files"], 1)
        self.assertTrue(cache_exists)
        self.assertEqual(second_rc, 2, second)
        self.assertEqual(second["census"]["cached_files"], 1)
        self.assertEqual(second["_census_calls"], 0)

    def test_final_identities_are_checked_without_reparse_or_requery(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            book_path = root / "intake_stats.json"
            book_path.write_text(
                '{"version":1,"files":{}}', encoding="utf-8")
            database = root / "corpus.db"
            database.write_bytes(b"before")
            book_reader = mock.Mock(return_value={})
            indexed_reader = mock.Mock(return_value=set())
            discovery_calls = 0

            def discovery(*, timeout_s):
                nonlocal discovery_calls
                del timeout_s
                discovery_calls += 1
                if discovery_calls == 2:
                    replacement = root / "replacement-book"
                    replacement.write_text(
                        '{"version":1,"files":{"changed":{}}}',
                        encoding="utf-8")
                    os.replace(replacement, book_path)
                    replacement_db = root / "replacement-db"
                    replacement_db.write_bytes(b"after")
                    os.replace(replacement_db, database)
                return []

            stdout = io.StringIO()
            with mock.patch.object(audit.common, "DATA_DIR", root), \
                    mock.patch.object(audit, "BOOK_PATH", book_path), \
                    mock.patch.object(audit, "_census_cache_path", return_value=None), \
                    mock.patch.object(audit, "_book", book_reader), \
                    mock.patch.object(audit, "_indexed_agents", indexed_reader), \
                    mock.patch.object(audit, "_discovered", side_effect=discovery), \
                    mock.patch.object(audit, "_live_tokens", return_value={}), \
                    redirect_stdout(stdout):
                rc = audit.main(["--json"])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(rc, 2, payload)
        self.assertEqual(book_reader.call_count, 1)
        self.assertEqual(indexed_reader.call_count, 1)
        self.assertTrue(any(
            "evidence book changed" in item for item in payload["problems"]))
        self.assertTrue(any(
            "indexed-row evidence changed" in item
            for item in payload["problems"]))

    def test_final_change_time_movement_is_an_evidence_error(self):
        book_before = (1, 2, 3, 4, 5)
        book_after = (1, 2, 3, 4, 6)
        indexed_before = (("", (7, 8, 9, 10, 11)),)
        indexed_after = (("", (7, 8, 9, 10, 12)),)
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with mock.patch.object(audit.common, "DATA_DIR", root), \
                    mock.patch.object(audit, "_census_cache_path", return_value=None), \
                    mock.patch.object(audit, "_book", return_value={}), \
                    mock.patch.object(audit, "_indexed_agents", return_value=set()), \
                    mock.patch.object(audit, "_discovered", return_value=[]), \
                    mock.patch.object(audit, "_live_tokens", return_value={}), \
                    mock.patch.object(
                        audit, "_optional_plain_identity",
                        side_effect=(book_before, book_after)), \
                    mock.patch.object(
                        audit, "_sqlite_family_identity",
                        side_effect=(indexed_before, indexed_after)), \
                    redirect_stdout(stdout):
                rc = audit.main(["--json"])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(rc, 2, payload)
        self.assertTrue(any(
            "evidence book changed" in item for item in payload["problems"]))
        self.assertTrue(any(
            "indexed-row evidence changed" in item
            for item in payload["problems"]))
        self.assertFalse(payload["routine"]["indexed_agents_from_cache"])

    def test_thousands_of_gaps_keep_exact_total_and_bounded_details(self):
        discovered = [
            ("codex", f"/missing/source-{index:05d}.jsonl")
            for index in range(5_000)
        ]
        stdout = io.StringIO()
        with mock.patch.object(audit, "_census_cache_path", return_value=None), \
                mock.patch.object(audit, "_book", return_value={}), \
                mock.patch.object(
                    audit, "_discovered", return_value=discovered), \
                mock.patch.object(audit, "_indexed_agents", return_value=set()), \
                mock.patch.object(audit, "_live_tokens", return_value={}), \
                redirect_stdout(stdout):
            rc = audit.main(["--json"])
        rendered = stdout.getvalue()
        payload = json.loads(rendered)
        self.assertEqual(rc, 1, payload)
        self.assertEqual(payload["gaps"], 5_000)
        self.assertEqual(
            len(payload["gap_details"]), audit._GAP_SAMPLE_LIMIT)
        self.assertEqual(
            payload["gap_details_omitted"],
            5_000 - audit._GAP_SAMPLE_LIMIT)
        self.assertLess(len(rendered.encode("utf-8")), 128 * 1024)

    def test_join_cutoff_is_one_warning_and_accounts_untouched_sources(self):
        class SlowBook:
            def items(self):
                for index in range(100):
                    time.sleep(0.002)
                    token = audit._TOKEN_ID_PREFIX + json.dumps(
                        [f"/source-{index}.jsonl", f"session-{index}"])
                    yield token, {
                        "agent": "codex", "key": "token", "seen": 1,
                        "rows": 1, "agent_rows": 0, "events": 0,
                        "errors": 0, "skips": {},
                    }

        discovered = [
            ("codex", f"/source-{index}.jsonl") for index in range(100)
        ]
        stdout = io.StringIO()
        with mock.patch.object(audit, "_ROUTINE_BUDGET_S", 0.08), \
                mock.patch.object(
                    audit, "_ROUTINE_FINAL_SNAPSHOT_RESERVE_S", 0.03), \
                mock.patch.object(audit, "_census_cache_path", return_value=None), \
                mock.patch.object(audit, "_book", return_value=SlowBook()), \
                mock.patch.object(
                    audit, "_discovered", return_value=discovered), \
                mock.patch.object(audit, "_indexed_agents", return_value=set()), \
                mock.patch.object(audit, "_live_tokens", return_value={}), \
                redirect_stdout(stdout):
            started = time.monotonic()
            rc = audit.main(["--json", "--strict"])
            elapsed = time.monotonic() - started
        payload = json.loads(stdout.getvalue())
        self.assertEqual(rc, 1, payload)
        self.assertLess(elapsed, 0.3)
        self.assertEqual(payload["problem_count"], 0)
        self.assertEqual(payload["gaps"], 1)
        self.assertEqual(payload["census"]["pending_files"], 100)

    def test_human_gap_copy_distinguishes_warnings_from_errors(self):
        stdout = io.StringIO()
        with mock.patch.object(audit, "_census_cache_path", return_value=None), \
                mock.patch.object(
                    audit, "_book",
                    side_effect=audit.AuditEvidenceError("broken book")), \
                mock.patch.object(
                    audit, "_discovered",
                    return_value=[("codex", "/missing-tally.jsonl")]), \
                mock.patch.object(audit, "_indexed_agents", return_value=set()), \
                mock.patch.object(audit, "_live_tokens", return_value={}), \
                redirect_stdout(stdout):
            rc = audit.main([])
        rendered = stdout.getvalue()
        self.assertEqual(rc, 2, rendered)
        self.assertIn("warnings in addition to the errors above", rendered)
        self.assertNotIn("exit 2", rendered)
        self.assertNotIn("exit 1", rendered)

    def test_warnings_are_one_and_errors_are_two(self):
        stdout = io.StringIO()
        with mock.patch.object(audit, "_census_cache_path", return_value=None), \
                mock.patch.object(audit, "_book", return_value={}), \
                mock.patch.object(
                    audit, "_discovered",
                    return_value=[("codex", "/missing-tally.jsonl")]), \
                mock.patch.object(audit, "_indexed_agents", return_value=set()), \
                mock.patch.object(audit, "_live_tokens", return_value={}), \
                redirect_stdout(stdout):
            warning_rc = audit.main(["--json"])
        self.assertEqual(warning_rc, 1, stdout.getvalue())

        stdout = io.StringIO()
        with mock.patch.object(audit, "_census_cache_path", return_value=None), \
                mock.patch.object(
                    audit, "_book",
                    side_effect=audit.AuditEvidenceError("broken book")), \
                mock.patch.object(audit, "_discovered", return_value=[]), \
                mock.patch.object(audit, "_indexed_agents", return_value=set()), \
                mock.patch.object(audit, "_live_tokens", return_value={}), \
                redirect_stdout(stdout):
            error_rc = audit.main(["--json"])
        self.assertEqual(error_rc, 2, stdout.getvalue())

    def test_agent_filter_rejects_unknown_and_repeated_values_before_work(self):
        for argv, expected in (
                (["--agent", "zzznotanagent"], "must name one of"),
                (["--agent", "codex", "--agent", "claude"],
                 "may be supplied only once")):
            stderr = io.StringIO()
            with self.subTest(argv=argv), \
                    mock.patch.object(
                        audit, "_store_snapshot",
                        side_effect=AssertionError("audit work started")), \
                    redirect_stderr(stderr), \
                    self.assertRaises(SystemExit) as raised:
                audit.main(argv)
            self.assertEqual(raised.exception.code, 2)
            self.assertIn(expected, stderr.getvalue())

    def test_empty_human_audit_is_one_concise_error(self):
        for argv, expected in (([], "no supported source files"),
                               (["--agent", "codex"], "no codex source files")):
            stdout = io.StringIO()
            with self.subTest(argv=argv), \
                    mock.patch.object(
                        audit, "_census_cache_path", return_value=None), \
                    mock.patch.object(audit, "_book", return_value={}), \
                    mock.patch.object(audit, "_discovered", return_value=[]), \
                    mock.patch.object(
                        audit, "_indexed_agents", return_value=set()), \
                    mock.patch.object(audit, "_live_tokens", return_value={}), \
                    redirect_stdout(stdout):
                rc = audit.main(argv)
            self.assertEqual(rc, 2)
            self.assertEqual(len(stdout.getvalue().strip().splitlines()), 1)
            self.assertIn(expected, stdout.getvalue())
            self.assertNotIn("exit 2", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
