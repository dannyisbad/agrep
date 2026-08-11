from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "semantic_worth_bench", ROOT / "bench" / "semantic_worth.py")
worth = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(worth)


class SemanticWorthTests(unittest.TestCase):
    def test_expected_rank_checks_hit_and_window(self):
        hits = [
            {"session": "other", "turn": 2, "window": []},
            {"session": "also-other", "turn": 3,
             "window": [{"session": "0199aaaa-deadbeef", "turn": 12}]},
        ]
        self.assertEqual(worth._expected_rank(hits, ["0199aaaa-d:12"]), 2)
        self.assertIsNone(worth._expected_rank(hits, ["missing:12"]))

    def test_snapshot_digest_tracks_source_bytes(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for name in ("messages.jsonl", "replies.jsonl", "corpus.db"):
                (root / name).write_bytes(name.encode("utf-8"))
            before = worth._snapshot_digest(root)
            (root / "messages.jsonl").write_bytes(b"moved")
            self.assertNotEqual(before, worth._snapshot_digest(root))


if __name__ == "__main__":
    unittest.main()
