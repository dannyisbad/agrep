"""Current Gemini CLI sessions reach search and `agrep summary` through the real ingest.

Gemini CLI records `session-*.jsonl` (a metadata line, message records re-written under the
same id, and `$set`/`$rewindTo`/`$patch` records) with Part[] prompts such as
`[{"text": ...}]`. The fixture under py/fixtures/gemini_jsonl holds one such session and one
legacy `.json` whose prompt is a Part[]; `python cli.py index` ingests them in a sandbox.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "py" / "fixtures" / "gemini_jsonl" / "tmp"
CURRENT = "6e6e0001-0414-4000-8000-000000000414"
WINDOW = ("--since", "2026-02-01", "--until", "2026-04-30")


@unittest.skipUnless(os.name == "posix", "the sandbox relies on POSIX paths")
class GeminiJsonlTests(unittest.TestCase):
    home: Path
    env: dict

    @classmethod
    def setUpClass(cls) -> None:
        temp = tempfile.TemporaryDirectory(prefix="gemini-jsonl-", dir=os.environ.get("TMPDIR"))
        cls.addClassCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        for name in ("home", "data", "tmp", "config", "cache", "share", "models", "runtime"):
            (root / name).mkdir()
        cls.home = root / "home"
        shutil.copytree(FIXTURE, cls.home / ".gemini" / "tmp")
        (root / "data" / "settings.json").write_text('{"embeddings":"off"}\n', encoding="utf-8")
        cls.env = {
            "HOME": str(cls.home), "AGREP_HOME": str(cls.home),
            "AGREP_DATA_DIR": str(root / "data"), "TMPDIR": str(root / "tmp"),
            "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "TZ": "UTC", "COLUMNS": "120", "NO_COLOR": "1", "TERM": "dumb",
            "AGREP_NO_DAEMON": "1", "AGREP_NO_SEM_WORKER": "1",
            "AGREP_NO_RESIDENT": "1", "AGREP_NO_FETCH": "1",
            "AGREP_CALLER_PUBLICATION_DIR": str(root / "no-callers"),
            "XDG_RUNTIME_DIR": str(root / "runtime"),
            "PYTHONNOUSERSITE": "1", "PYTHONUTF8": "1",
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_DATA_HOME": str(root / "share"),
            "AGREP_MODEL_DIR": str(root / "models"),
        }
        if "AGREP_RS_BIN" in os.environ:
            cls.env["AGREP_RS_BIN"] = os.environ["AGREP_RS_BIN"]
        indexed = cls._run(ROOT / "cli.py", "index")
        if indexed.returncode:
            raise AssertionError(f"fixture indexing failed:\n{indexed.stdout}{indexed.stderr}")

    @classmethod
    def _run(cls, script: Path, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(script), *argv], cwd=cls.home, env=cls.env, input="",
            capture_output=True, text=True, encoding="utf-8", errors="strict",
            timeout=120, check=False)

    def _count(self, query: str) -> int:
        result = self._run(ROOT / "cli.py", "-c", query, "--agent", "gemini", "--no-auto")
        return int(result.stdout.strip() or "-1")

    def _summary(self, mode: str) -> list[dict]:
        result = self._run(ROOT / "py" / "summary.py", mode, *WINDOW, "--agent", "gemini",
                           "--json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = [json.loads(line) for line in result.stdout.splitlines() if line]
        self.assertEqual(lines[0]["kind"], "agrep-meta", result.stdout)
        return lines[1:]

    def test_jsonl_and_part_list_prompts_are_searchable(self) -> None:
        self.assertGreater(self._count("quokka"), 0)
        self.assertGreater(self._count("wombat"), 0)
        # the rewound prompt and the environment preamble are not the human's history
        self.assertEqual(self._count("zeppelin"), 0)
        self.assertEqual(self._count("setting up the context"), 0)

    def test_write_todos_from_the_jsonl_reach_pending(self) -> None:
        pending = {item["session"]: item for item in self._summary("pending")}
        item = pending[CURRENT]
        self.assertEqual((item["status"], item["agent"], item["items"]),
                         ("todo_open", "gemini",
                          ["Port the yaml loader", "Update the loader docs"]))
        self.assertEqual(
            item["first_text"],
            "Port the env and yaml loaders to the quokka config API and update the loader docs.")

    def test_active_time_follows_the_folded_history(self) -> None:
        table = {(row["period"], row["project"]): row for row in self._summary("time")}
        # prompt 09:00:00 -> write_todos 09:00:20; the rewound 09:15 prompt adds nothing
        self.assertEqual(table[("2026-04-14", "gemini")]["estimated_active_ms"], 20_000)
        self.assertEqual(table[("2026-04-14", "gemini")]["chats"], 1)


if __name__ == "__main__":
    unittest.main()
