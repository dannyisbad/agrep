from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


def _load_scale():
    path = Path(__file__).with_name("semantic_q8_scale.py")
    spec = importlib.util.spec_from_file_location("agrep_semantic_q8_scale_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SemanticQ8ScaleContractTests(unittest.TestCase):
    def test_row_width_tracks_the_measured_dimension(self) -> None:
        scale = _load_scale()
        self.assertAlmostEqual(scale._expected_q8_bytes_per_row(256, 100_000),
                               260.00064)
        self.assertAlmostEqual(scale._expected_q8_bytes_per_row(768, 2_000_000),
                               772.000032)

    def test_row_width_rejects_invalid_shape(self) -> None:
        scale = _load_scale()
        with self.assertRaises(ValueError):
            scale._expected_q8_bytes_per_row(0, 100)


if __name__ == "__main__":
    unittest.main()
