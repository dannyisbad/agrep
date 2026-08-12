"""Real-process acceptance pins for cold machine output and FTS publication."""

from __future__ import annotations

import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

import proc as process_utils  # noqa: E402

CLI = ROOT / "cli.py"
RELEASE_BIN = ROOT / "target" / "release" / (
    "agrep-rs.exe" if sys.platform == "win32" else "agrep-rs"
)
SESSION = "88888888-8888-4888-8888-888888888888"


class FirstRunIntegrationTests(unittest.TestCase):
    def _fixture(self, root: Path, label: str) -> tuple[Path, Path, str]:
        home = root / "home"
        data = root / "data"
        project = home / ".claude" / "projects" / "firstuse-cold-proof"
        project.mkdir(parents=True)
        term = f"firstusecoldproof{label}x7q9"
        rows = [
            {
                "type": "user",
                "userType": "external",
                "sessionId": SESSION,
                "timestamp": "2026-07-25T12:00:00.000Z",
                "cwd": str(root / "work" / "firstuse-cold-proof"),
                "message": {"role": "user", "content": term},
            },
            {
                "type": "assistant",
                "sessionId": SESSION,
                "timestamp": "2026-07-25T12:00:01.000Z",
                "cwd": str(root / "work" / "firstuse-cold-proof"),
                "message": {
                    "role": "assistant",
                    "model": "claude-firstuse-fixture",
                    "content": [{"type": "text", "text": "cold proof reply"}],
                },
            },
        ]
        body = "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            for row in rows
        )
        (project / f"{SESSION}.jsonl").write_text(body, encoding="utf-8")
        return home, data, term

    def _env(self, home: Path, data: Path) -> dict[str, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("AGREP_")
            and key not in {
                "APPDATA", "CLINE_DIR", "CRUSH_GLOBAL_DATA", "LOCALAPPDATA",
                "OPENCODE_DB", "USERPROFILE", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
            }
        }
        env.update({
            "HOME": str(home),
            "USERPROFILE": str(home),
            "APPDATA": str(home / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(home / "AppData" / "Local"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "CLINE_DIR": str(home / ".cline"),
            "AGREP_HOME": str(home),
            "AGREP_DATA_DIR": str(data),
            "AGREP_DATA_DIR_SOURCE": "env",
            "AGREP_RS_BIN": str(RELEASE_BIN),
            "AGREP_NO_DAEMON": "1",
            "AGREP_NO_FETCH": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "RAYON_NUM_THREADS": "2",
        })
        return env

    def _run(
        self, home: Path, data: Path, *args: str,
    ) -> subprocess.CompletedProcess[str]:
        self.assertTrue(RELEASE_BIN.is_file(), f"release binary missing: {RELEASE_BIN}")
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=ROOT,
            env=self._env(home, data),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=90,
            check=False,
        )

    def _assert_publication(self, data: Path, term: str) -> None:
        required = (
            "messages.jsonl", "replies.jsonl", "sessions.jsonl", ".ingest.sig",
            ".derived_generation.json", "session_family.meta.json",
            "events/.store.sqlite3", "events/.generation", "corpus.db",
        )
        for name in required:
            path = data / name
            self.assertTrue(path.is_file(), f"missing publication artifact: {path}")
            self.assertGreater(path.stat().st_size, 0, f"empty publication artifact: {path}")
        self.assertIn(term, (data / "messages.jsonl").read_text(encoding="utf-8"))
        self.assertIn(SESSION, (data / "sessions.jsonl").read_text(encoding="utf-8"))
        db = sqlite3.connect(data / "corpus.db")
        try:
            count = db.execute(
                "SELECT count(*) FROM msgs_fts WHERE msgs_fts MATCH ?", (term,),
            ).fetchone()[0]
        finally:
            db.close()
        self.assertEqual(count, 1)

    def _assert_no_ingest_stdout(self, stdout: str) -> None:
        lowered = stdout.lower()
        for phrase in (
            "indexing transcripts", "indexing your agent stores", "[claude]",
            "phases:", "unchanged message set", "indexed 1 message",
        ):
            self.assertNotIn(phrase, lowered)

    def test_cold_machine_surfaces_preserve_their_entire_stdout_grammar(self) -> None:
        cases = (
            ("json", ("search", "--json")),
            ("count", ("search", "-c")),
            ("flat", ("search", "--flat")),
            ("recall", ("recall", "--json")),
        )
        for index, (grammar, argv) in enumerate(cases):
            with self.subTest(grammar=grammar), tempfile.TemporaryDirectory(
                prefix=f"agrep-firstuse-first-run-{grammar}-"
            ) as raw:
                home, data, term = self._fixture(Path(raw), str(index))
                self.assertFalse(data.exists())
                result = self._run(home, data, argv[0], term, *argv[1:])
                self.assertEqual(result.returncode, 0, result.stderr)
                self._assert_no_ingest_stdout(result.stdout)
                self._assert_publication(data, term)

                if grammar == "json":
                    records = [json.loads(line) for line in result.stdout.splitlines()]
                    self.assertGreater(len(records), 1)
                    self.assertEqual(records[0]["kind"], "agrep-meta")
                    self.assertTrue(all(
                        row.get("kind") != "agrep-meta" for row in records[1:]))
                    self.assertTrue(any(term in str(row.get("snippet") or "")
                                        for row in records[1:]))
                    self.assertTrue(all(
                        not ({"progress", "total", "row"} & set(row))
                        for row in records[1:]
                    ))
                elif grammar == "count":
                    self.assertRegex(result.stdout, r"^1\r?\n$")
                elif grammar == "flat":
                    rows = result.stdout.splitlines()
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0].count("\t"), 5)
                    self.assertIn(term, rows[0].split("\t", 5)[5])
                else:
                    payload = json.loads(result.stdout)
                    self.assertIsInstance(payload, dict)
                    self.assertEqual(payload.get("query"), term)
                    windows = [
                        row
                        for hit in payload.get("hits") or []
                        for row in hit.get("window") or []
                    ]
                    self.assertTrue(any(term in str(row.get("text") or "")
                                        for row in windows))

    def test_explicit_index_requires_a_real_fts_publication(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-firstuse-fts-ok-") as raw:
            home, data, term = self._fixture(Path(raw), "ftsok")
            result = self._run(home, data, "index")
            self.assertEqual(result.returncode, 0, result.stderr)
            self._assert_publication(data, term)

    def test_simultaneous_first_searches_never_fail_silently(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-firstuse-first-race-") as raw:
            home, data, term = self._fixture(Path(raw), "race")
            command = [sys.executable, str(CLI), "search", "--json", term]
            workers = [subprocess.Popen(
                command, cwd=ROOT, env=self._env(home, data),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="strict",
            ) for _ in range(8)]
            results = []
            try:
                for worker in workers:
                    stdout, stderr = worker.communicate(timeout=90)
                    results.append((worker.returncode, stdout, stderr))
            finally:
                for worker in workers:
                    if worker.poll() is None:
                        worker.kill()
                        worker.wait()
            for rc, stdout, stderr in results:
                if rc == 0:
                    self.assertIn(term, stdout)
                    continue
                self.assertEqual(rc, 2, stderr)
                self.assertTrue(stderr.strip(), "first-use loser exited silently")
                self.assertRegex(stderr.lower(), r"index|publish|retry")
            self._assert_publication(data, term)

    @unittest.skipIf(sys.platform == "win32", "POSIX signal delivery")
    def test_interrupted_cold_search_allows_an_immediate_retry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-firstuse-first-interrupt-") as raw:
            home, data, term = self._fixture(Path(raw), "interrupt")
            source = next((home / ".claude" / "projects").rglob("*.jsonl"))
            original = source.read_text(encoding="utf-8")
            source.write_text(
                original.replace(term, term + " " + "x" * (16 * 1024 * 1024)),
                encoding="utf-8",
            )
            command = [sys.executable, str(CLI), "search", term, "--classic"]
            worker = subprocess.Popen(
                command, cwd=ROOT, env=self._env(home, data),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="strict",
            )
            ingest_pid = None
            ingest_start = None
            try:
                deadline = time.monotonic() + 20.0
                claim = data / ".indexd.v2.lock"
                while time.monotonic() < deadline:
                    if worker.poll() is not None:
                        self.fail(
                            f"cold search exited before interruption: {worker.returncode}")
                    try:
                        body = claim.read_text(encoding="ascii")
                    except (FileNotFoundError, OSError, UnicodeError):
                        body = ""
                    pid_match = re.search(r"(?:^|\s)pid=(\d+)(?:\s|$)", body)
                    start_match = re.search(r"(?:^|\s)start=([^\s]+)(?:\s|$)", body)
                    if pid_match and start_match:
                        ingest_pid = int(pid_match.group(1))
                        ingest_start = start_match.group(1)
                        self.assertEqual(
                            process_utils.process_start_identity(ingest_pid),
                            ingest_start,
                        )
                        os.kill(ingest_pid, signal.SIGSTOP)
                        break
                    time.sleep(0.001)
                self.assertIsNotNone(
                    ingest_pid, "cold ingest never published its ownership fence")
                worker.send_signal(signal.SIGINT)
                _, stderr = worker.communicate(timeout=5)
                self.assertEqual(worker.returncode, 130, stderr)
                self.assertNotEqual(
                    process_utils.process_start_identity(ingest_pid),
                    ingest_start,
                    "interrupted CLI returned before reaping its ingest",
                )
                ingest_pid = None
                ingest_start = None
            finally:
                if worker.poll() is None:
                    worker.kill()
                    worker.wait()
                if ingest_pid is not None and ingest_start is not None:
                    for sent in (signal.SIGCONT, signal.SIGKILL):
                        if (process_utils.process_start_identity(ingest_pid)
                                != ingest_start):
                            break
                        try:
                            os.kill(ingest_pid, sent)
                        except ProcessLookupError:
                            break

            source.write_text(original, encoding="utf-8")
            started = time.monotonic()
            retry = self._run(home, data, "search", term, "--classic")
            elapsed = time.monotonic() - started
            self.assertEqual(retry.returncode, 0, retry.stderr)
            self.assertIn(term, retry.stdout)
            self.assertNotIn("another first-run indexer", retry.stderr.lower())
            self.assertLess(elapsed, 5.0, f"immediate retry stalled for {elapsed:.2f}s")
            self._assert_publication(data, term)

    def test_explicit_index_is_nonzero_when_real_fts_publication_fails(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agrep-firstuse-fts-fail-") as raw:
            home, data, term = self._fixture(Path(raw), "ftsfail")
            seeded = self._run(home, data, "index")
            self.assertEqual(seeded.returncode, 0, seeded.stderr)
            (data / "corpus.db").unlink()
            (data / "corpus.db").mkdir()
            source = next((home / ".claude" / "projects").rglob("*.jsonl"))
            prior_term = term
            term = f"{term}changed"
            source.write_text(
                source.read_text(encoding="utf-8").replace(prior_term, term),
                encoding="utf-8",
            )
            result = self._run(home, data, "index")
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((data / "messages.jsonl").is_file())
            self.assertIn(term, (data / "messages.jsonl").read_text(encoding="utf-8"))
            self.assertTrue((data / "corpus.db").is_dir())
            self.assertIsNone(re.search(r"^  \([0-9.]+s\)$", result.stdout, re.MULTILINE))


if __name__ == "__main__":
    unittest.main()
