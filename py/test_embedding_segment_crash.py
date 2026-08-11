from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import embedding_segments
from test_embedding_segments import _inputs


def _child(config_path: Path) -> int:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    root = Path(config["root"])
    meta = root / "embeddings.meta"
    artifacts, hashes, refs = _inputs(root, "child", ["b"])
    barrier = Path(config["barrier"])

    def stop(stage: str) -> None:
        if stage != config["stage"]:
            return
        barrier.write_text(stage, encoding="utf-8")
        while True:
            time.sleep(60)

    embedding_segments.publish_delta(
        meta, source={"ingest_signature": "two"}, artifacts=artifacts,
        ids=["b"], hashes=hashes, refs=refs, coverage={"total": 2},
        expected_generation=config["generation"], _on_stage=stop)
    return 0


class SegmentCrashPublicationTests(unittest.TestCase):
    @staticmethod
    def _wait(path: Path, child: subprocess.Popen) -> None:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if path.exists():
                return
            if child.poll() is not None:
                raise AssertionError(f"publisher exited before barrier: {child.returncode}")
            time.sleep(0.01)
        raise AssertionError("publisher did not reach its crash barrier")

    def _crash_at(self, stage: str) -> tuple[list[str], str, str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            meta = root / "embeddings.meta"
            artifacts, hashes, refs = _inputs(root, "base", ["a"])
            base = embedding_segments.publish_base(
                meta, source={"ingest_signature": "one"}, model_id="model", dim=2,
                artifacts=artifacts, ids=["a"], hashes=hashes, refs=refs,
                coverage={"total": 1})
            barrier = root / "barrier"
            config = root / "child.json"
            config.write_text(json.dumps({
                "root": str(root), "barrier": str(barrier), "stage": stage,
                "generation": base["generation"],
            }), encoding="utf-8")
            command = [sys.executable, str(Path(__file__).resolve()),
                       "--publish-child", str(config)]
            kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                      "stderr": subprocess.DEVNULL, "close_fds": True}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            child = subprocess.Popen(command, **kwargs)
            try:
                self._wait(barrier, child)
                child.kill()
                child.wait(timeout=5)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
            current = embedding_segments.load_manifest(
                meta, verify_hashes=True, validate_liveness=True)
            rows = [row["mid"] for row in embedding_segments.active_rows(current)]
            return rows, base["generation"], current["generation"]

    def test_force_kill_before_manifest_swap_keeps_prior_generation(self) -> None:
        rows, before, after = self._crash_at("before_manifest_replace")
        self.assertEqual(rows, ["a"])
        self.assertEqual(after, before)

    def test_force_kill_after_manifest_swap_keeps_new_generation_coherent(self) -> None:
        rows, before, after = self._crash_at("after_manifest_replace")
        self.assertEqual(rows, ["a", "b"])
        self.assertNotEqual(after, before)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--publish-child":
        raise SystemExit(_child(Path(sys.argv[2])))
    unittest.main()
