from __future__ import annotations

import unittest
from unittest import mock

import numpy as np

from _test_support import isolate_data_dir

isolate_data_dir()
import embed


class _FakeEmbedder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.codes: dict[str, int] = {}

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        self.calls.append(list(texts))
        rows = []
        for text in texts:
            code = self.codes.setdefault(str(text), len(self.codes) + 1)
            rows.append([float(code), float(code * 10)])
        return np.asarray(rows, dtype=np.float32)


class _CollidingText(str):
    def __hash__(self) -> int:
        return 7


class EmbeddingPassDedupTests(unittest.TestCase):
    def test_foreground_infers_each_exact_text_once_and_restores_order(self) -> None:
        fake = _FakeEmbedder()
        texts = ["alpha", "beta", "alpha", "gamma", "beta", "alpha"]
        stats: dict[str, int] = {}
        parts, done, moved = embed._embed_pending_chunks(
            fake, texts, None, background=False, stats=stats)

        matrix = np.concatenate(parts)
        self.assertEqual(fake.calls, [["alpha", "beta", "gamma"]])
        self.assertEqual(matrix[:, 0].tolist(), [1, 2, 1, 3, 2, 1])
        self.assertEqual((done, moved), (len(texts), False))
        self.assertEqual(stats, {"rows": 6, "inferred": 3, "reused": 3})

    def test_hash_collisions_do_not_alias_distinct_text(self) -> None:
        fake = _FakeEmbedder()
        texts = [_CollidingText("alpha"), _CollidingText("beta"),
                 _CollidingText("alpha")]
        parts, _, _ = embed._embed_pending_chunks(
            fake, texts, None, background=False)

        matrix = np.concatenate(parts)
        self.assertEqual(fake.calls, [["alpha", "beta"]])
        self.assertEqual(matrix[:, 0].tolist(), [1, 2, 1])

    def test_background_chunks_reuse_prior_vectors_without_moving_rows(self) -> None:
        fake = _FakeEmbedder()
        texts = ["alpha", "beta", "alpha", "beta", "alpha", "gamma", "alpha"]
        stats: dict[str, int] = {}
        source = {"generation": "same"}
        with mock.patch.object(embed, "_BACKGROUND_CHUNK", 3), \
                mock.patch.object(embed.semantic, "source_generation",
                                  return_value=source):
            parts, done, moved = embed._embed_pending_chunks(
                fake, texts, source, background=True, stats=stats)

        matrix = np.concatenate(parts)
        self.assertEqual(fake.calls, [["alpha", "beta"], ["gamma"]])
        self.assertEqual([len(part) for part in parts], [3, 3, 1])
        self.assertEqual(matrix[:, 0].tolist(), [1, 2, 1, 2, 1, 3, 1])
        self.assertEqual((done, moved), (len(texts), False))
        self.assertEqual(stats, {"rows": 7, "inferred": 3, "reused": 4})

    def test_background_movement_publishes_one_aligned_original_chunk(self) -> None:
        fake = _FakeEmbedder()
        texts = ["alpha", "beta", "alpha", "gamma"]
        stats: dict[str, int] = {}
        with mock.patch.object(embed, "_BACKGROUND_CHUNK", 3), \
                mock.patch.object(embed.semantic, "source_generation",
                                  return_value={"generation": "new"}):
            parts, done, moved = embed._embed_pending_chunks(
                fake, texts, {"generation": "old"}, background=True,
                stats=stats)

        self.assertEqual(fake.calls, [["alpha", "beta"]])
        self.assertEqual(np.concatenate(parts)[:, 0].tolist(), [1, 2, 1])
        self.assertEqual((done, moved), (3, True))
        self.assertEqual(stats, {"rows": 3, "inferred": 2, "reused": 1})


if __name__ == "__main__":
    unittest.main()
