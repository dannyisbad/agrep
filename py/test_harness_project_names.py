"""A project name alone never makes the user's prompts harness rows.

`control_plane`, `control_panel`, `_probe-tools` are ordinary project names. Only the
corpus-local policy (`<data dir>/harness_prefixes.txt`, `project:` lines) marks a
project's chats as harness traffic. Runs the real ingest binary in a sandbox.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

import surface_policy as surface  # noqa: E402

CLI = ROOT / "cli.py"
RELEASE_BIN = Path(os.environ.get("AGREP_RS_BIN") or ROOT / "target" / "release" / (
    "agrep-rs.exe" if sys.platform == "win32" else "agrep-rs"))
CONTROL = "aaaaaaaa-1111-4111-8111-111111111111"
PLAIN = "bbbbbbbb-2222-4222-8222-222222222222"
CONTROL_PROMPT = "rotate the etcd certificates before friday"
PLAIN_PROMPT = "rotate the vault certificates before friday"


def _transcript(session: str, cwd: str, prompt: str) -> str:
    rows = [
        {"type": "user", "userType": "external", "sessionId": session,
         "timestamp": "2026-07-25T12:00:00.000Z", "cwd": cwd,
         "message": {"role": "user", "content": prompt}},
        {"type": "assistant", "sessionId": session,
         "timestamp": "2026-07-25T12:00:01.000Z", "cwd": cwd,
         "message": {"role": "assistant", "model": "claude-fixture",
                     "content": [{"type": "text", "text": "renewing them now"}]}},
    ]
    return "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows)


@unittest.skipUnless(os.name == "posix", "the sandbox relies on POSIX paths")
class HarnessProjectNameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not RELEASE_BIN.is_file():
            raise unittest.SkipTest(f"release binary missing: {RELEASE_BIN}")
        temp = tempfile.TemporaryDirectory(prefix="harness-names-", dir=os.environ.get("TMPDIR"))
        cls.addClassCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        cls.home = root / "home"
        cls.data = root / "data"
        for name in ("home", "data", "tmp", "runtime"):
            (root / name).mkdir()
        (cls.data / "settings.json").write_text('{"embeddings":"off"}\n', encoding="utf-8")
        projects = cls.home / ".claude" / "projects"
        for slug, session, cwd, prompt in (
                ("-home-u-code-control-plane", CONTROL, "/home/u/code/control_plane",
                 CONTROL_PROMPT),
                ("-home-u-code-plane", PLAIN, "/home/u/code/plane", PLAIN_PROMPT)):
            (projects / slug).mkdir(parents=True)
            (projects / slug / f"{session}.jsonl").write_text(
                _transcript(session, cwd, prompt), encoding="utf-8")
        cls.env = {
            "HOME": str(cls.home), "AGREP_HOME": str(cls.home),
            "AGREP_DATA_DIR": str(cls.data), "TMPDIR": str(root / "tmp"),
            "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "TZ": "UTC", "COLUMNS": "120", "NO_COLOR": "1", "TERM": "dumb",
            "AGREP_RS_BIN": str(RELEASE_BIN),
            "AGREP_NO_DAEMON": "1", "AGREP_NO_SEM_WORKER": "1",
            "AGREP_NO_RESIDENT": "1", "AGREP_NO_FETCH": "1",
            "XDG_RUNTIME_DIR": str(root / "runtime"),
            "PYTHONNOUSERSITE": "1", "PYTHONUTF8": "1", "RAYON_NUM_THREADS": "2",
        }

    @classmethod
    def _run(cls, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CLI), *argv], cwd=cls.home, env=cls.env, input="",
            capture_output=True, text=True, encoding="utf-8", errors="strict",
            timeout=120, check=False)

    def _index(self) -> None:
        indexed = self._run("index")
        self.assertEqual(indexed.returncode, 0, indexed.stdout + indexed.stderr)

    def _who(self, prompt: str) -> str:
        rows = [json.loads(line) for line in
                (self.data / "messages.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()]
        matching = [row for row in rows if row.get("text") == prompt]
        self.assertEqual(len(matching), 1, rows)
        return matching[0]["who"]

    def _hit_who(self, term: str) -> list[str]:
        result = self._run(term, "--json", "--no-auto", "--lexical")
        self.assertIn(result.returncode, (0, 1), result.stdout + result.stderr)
        rows = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
        return [row["who"] for row in rows if term in str(row.get("snippet", ""))]

    def test_control_plane_prompts_are_user_rows_everywhere(self) -> None:
        self._index()
        self.assertEqual(self._who(CONTROL_PROMPT), "user")
        self.assertEqual(self._who(PLAIN_PROMPT), "user")
        # search label: the user's prompt is a `user` hit, not a demoted harness row
        self.assertEqual(self._hit_who("etcd"), ["user"])
        self.assertNotIn("user", surface.SEMANTIC_DEFAULT_EXCLUDED_ROLES)
        # recall keeps the typed prompt as the user's own
        recalled = self._run("recall", "etcd", "--json", "--no-auto", "--lexical")
        self.assertIn(recalled.returncode, (0, 1), recalled.stdout + recalled.stderr)
        self.assertNotIn("harness", recalled.stdout)
        self.assertIn("etcd", recalled.stdout)

    def test_a_configured_project_prefix_still_classifies(self) -> None:
        policy = self.data / "harness_prefixes.txt"
        policy.write_text("# benchmark projects\nproject:control_\n", encoding="utf-8")
        try:
            self._index()
            self.assertEqual(self._who(CONTROL_PROMPT), "harness")
            self.assertEqual(self._who(PLAIN_PROMPT), "user")
            self.assertEqual(self._hit_who("etcd"), ["harness"])
        finally:
            policy.unlink()
            self._index()
        self.assertEqual(self._who(CONTROL_PROMPT), "user")


if __name__ == "__main__":
    unittest.main()
