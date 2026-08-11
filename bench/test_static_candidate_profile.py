from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROFILE_ID = "static-retrieval-mrl-en-v1-256"


def _load_selection():
    path = Path(__file__).with_name("embedder_selection.py")
    spec = importlib.util.spec_from_file_location("agrep_static_profile_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class StaticCandidateProfileTests(unittest.TestCase):
    def test_static_candidate_is_strict_and_immutably_pinned(self) -> None:
        manifest = _load_selection().load_manifest()
        matches = [row for row in manifest["profiles"] if row["id"] == PROFILE_ID]
        self.assertEqual(len(matches), 1)

        profile = matches[0]
        runtime = profile["runtime_profile"]
        self.assertEqual((profile["status"], profile["adoption_eligible"]),
                         ("runnable", True))
        self.assertEqual((runtime["dim"], runtime["native_dim"]), (256, 1024))
        self.assertEqual((runtime["max_seq"], profile["native_max_seq"]),
                         (1024, 1024))
        self.assertEqual(runtime["pooling"], "direct_2d")
        self.assertEqual(runtime["output"], {"name": "sentence_embedding"})
        self.assertEqual(runtime["pad_token"], "[PAD]")
        self.assertEqual(runtime["query_prefix"], "")
        self.assertEqual(runtime["document_prefix"], "")
        self.assertEqual(
            runtime["files"]["model_int8.onnx"]["sha256"],
            "6dfc7847e73fede1c6474f69ecd283e0ad8c83737d18a4997ffbd9d3396403f4",
        )
        self.assertEqual(
            runtime["files"]["tokenizer.json"]["sha256"],
            "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
        )


if __name__ == "__main__":
    unittest.main()
