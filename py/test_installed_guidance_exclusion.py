"""A8: the block agrep installs into agent configs never returns as lived recall.

`agrep setup` writes NUDGE into every agent instruction file on the box. Codex
hands that file back to the model inside a composed `role:user` turn, so the
ingest lane sees agrep's own documentation in the exact position a typed
sentence occupies. This suite indexes a sandbox HOME whose rollouts each carry
an installed block and holds the corpus to the product's core guarantee: a row
is lived because of where it came from, never because of what it says.
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
sys.path.insert(0, str(ROOT))

from _test_support import isolate_data_dir  # noqa: E402

isolate_data_dir()

import common  # noqa: E402
import teach  # noqa: E402

BLOCKS = 6
TYPED = [
    "the cuda build dies with no kernel image is available for execution",
    "we deadlock in the writer lock again on the sm_120 wheel",
    "reindex takes 40 minutes on this box, can we shard it",
]


def _installed_block() -> str:
    """What codex actually replays: the instruction file, wrapped as it sends it."""
    return (
        "<recommended_plugins>\nplugins\n</recommended_plugins>\n"
        "# AGENTS.md instructions\n\n<INSTRUCTIONS>\n"
        + teach.NUDGE.strip()
        + "\n</INSTRUCTIONS>"
    )


def _rollout(path: Path, session: str, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    head = {
        "timestamp": "2026-07-28T00:00:00Z", "type": "session_meta",
        "payload": {"type": "session_meta", "id": session, "cwd": "/work/proj"},
    }
    body = [head, *records]
    path.write_text(
        "\n".join(json.dumps(r) for r in body) + "\n", encoding="utf-8")


def _item(text: str) -> dict:
    return {
        "timestamp": "2026-07-28T00:00:01Z", "type": "response_item",
        "payload": {"type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": text}]},
    }


def _submitted(text: str) -> dict:
    return {
        "timestamp": "2026-07-28T00:00:01Z", "type": "event_msg",
        "payload": {"type": "user_message", "message": text},
    }


def _sandbox_home(root: Path) -> Path:
    home = root / "home"
    day = home / ".codex" / "sessions" / "2026" / "07" / "28"
    block = _installed_block()
    for n in range(BLOCKS):
        session = f"019fbbbb-0000-7000-8000-00000000000{n}"
        records = [_item(block)]
        if n < len(TYPED):
            records += [_item(TYPED[n]), _submitted(TYPED[n])]
        _rollout(day / f"rollout-2026-07-28T00-00-0{n}-{session}.jsonl",
                 session, records)
    return home


def _index(home: Path, data: Path) -> list[dict]:
    binary = common.ingest_bin()
    env = {k: v for k, v in os.environ.items()
           if k not in ("USERPROFILE", "HOME", "APPDATA", "XDG_CONFIG_HOME")}
    env["AGREP_HOME"] = str(home)
    env["AGREP_DATA_DIR"] = str(data)
    run = subprocess.run(
        [str(binary), "index", "--agent", "codex", "--full"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=120)
    if run.returncode != 0:
        raise AssertionError(f"ingest rc={run.returncode}: {run.stderr}")
    rows_path = data / "messages.jsonl"
    return [json.loads(line)
            for line in rows_path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


class InstalledBlockIsNeverLivedTests(unittest.TestCase):
    def setUp(self) -> None:
        binary = common.ingest_bin()
        if not (binary and Path(str(binary)).exists()):
            self.skipTest("no ingest binary")
        self._tmp = tempfile.TemporaryDirectory(prefix="agrep-a8-")
        root = Path(self._tmp.name)
        self.rows = _index(_sandbox_home(root), root / "data")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_no_row_originates_in_the_installed_block(self) -> None:
        marker = teach.NUDGE.strip().splitlines()[0]
        carriers = [r for r in self.rows if marker in r.get("text", "")]
        self.assertEqual(carriers, [])

    def test_the_blocks_own_example_queries_find_nothing(self) -> None:
        # Every literal command the block teaches, searched over a corpus
        # holding BLOCKS copies of it.
        for line in teach.NUDGE.splitlines():
            stripped = line.strip()
            if not stripped.startswith("$ agrep"):
                continue
            phrase = stripped[len("$ agrep"):].split("#")[0].strip()
            with self.subTest(taught=phrase):
                hits = [r for r in self.rows if phrase in r.get("text", "")]
                self.assertEqual(hits, [], f"{phrase} recalls the block itself")

    def test_typed_turns_in_the_same_rollouts_survive_intact(self) -> None:
        texts = {r.get("text", "") for r in self.rows}
        self.assertEqual(sorted(texts), sorted(TYPED))


if __name__ == "__main__":
    unittest.main()
