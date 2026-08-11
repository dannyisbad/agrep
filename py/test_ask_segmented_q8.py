from __future__ import annotations

import json
import unittest
from unittest import mock

import numpy as np

import ask
import segment_query
import semantic_q8


_COVERAGE = {
    "indexed": 1_000_001,
    "total": 1_000_001,
    "pending": 0,
    "fraction": 1.0,
    "complete": True,
    "order": "complete",
}
_FILTERS = {
    "project": "alpha",
    "_exclude_who": tuple(sorted(ask._Q8_DEFAULT_EXCLUDED_ROLES)),
}


class _Refs:
    def __init__(self, eligible=(7, 11), *, rows=1_000_001) -> None:
        self.rows = rows
        self.expected = np.asarray(eligible, dtype=np.int64)
        self.filter_calls = []

    def eligible(self, filters):
        self.filter_calls.append(filters)
        return self.expected.copy()

    @staticmethod
    def resolve(ordinals):
        return [{
            "ordinal": int(ordinal),
            "session": f"session-{int(ordinal)}",
            "project": "alpha",
            "agent": "codex",
            "who": "user",
            "model": "fixture",
            "ts": int(ordinal),
            "turn": int(ordinal),
            "text": f"answer {int(ordinal)}",
        } for ordinal in ordinals]


class _DenseTrap(segment_query.SegmentedMatrix):
    def __init__(self, rows: int) -> None:
        self.shape = (rows, 2)
        self.manifest = {"next_row_ref": rows}
        self.matmul_calls = 0

    def __matmul__(self, _query):
        self.matmul_calls += 1
        raise AssertionError("segmented f32 scan must not run")


class _SmallDense(segment_query.SegmentedMatrix):
    def __init__(self) -> None:
        self.shape = (2, 2)
        self.manifest = {"next_row_ref": 2}
        self.matmul_calls = 0

    def __matmul__(self, _query):
        self.matmul_calls += 1
        return np.asarray([0.7, 0.9], dtype=np.float32)


class AskSegmentedQ8Tests(unittest.TestCase):
    def setUp(self) -> None:
        ask.clear_artifact_cache()

    def tearDown(self) -> None:
        ask.clear_artifact_cache()

    @staticmethod
    def _base_patches(refs, coverage=_COVERAGE):
        return (
            mock.patch.object(ask.common, "read_index_meta",
                              return_value=(2, "fixture")),
            mock.patch.object(ask, "_guard_embedder"),
            mock.patch.object(ask, "_require_current_message_index",
                              return_value=coverage),
            mock.patch.object(ask, "_message_refs_from_pointer",
                              return_value=refs),
            mock.patch.object(ask, "_embed_query",
                              return_value=np.asarray([1.0, 0.0], dtype=np.float32)),
            mock.patch.object(semantic_q8, "artifact_available", return_value=True),
            mock.patch.object(semantic_q8, "accelerator_coverage",
                              return_value=coverage),
            mock.patch.dict(ask._CURRENT_MESSAGE_STATE,
                            {"generation": "0d" * 16}, clear=False),
        )

    def test_flat_search_passes_exact_filtered_global_rowrefs_to_q8(self) -> None:
        refs = _Refs()
        matrix = _DenseTrap(_COVERAGE["indexed"])
        candidates = (
            np.asarray([7, 11], dtype=np.int64),
            np.asarray([0.9, 0.9], dtype=np.float32),
        )
        patches = self._base_patches(refs)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], patches[6], patches[7], \
                mock.patch.object(ask, "_message_artifacts",
                                  return_value=((), matrix, refs, _COVERAGE)), \
                mock.patch.object(semantic_q8, "exact_candidates",
                                  return_value=candidates) as native:
            payload = json.loads(ask.tool_search_messages(
                "query", k=2, filters=_FILTERS, envelope=True))

        native.assert_called_once()
        np.testing.assert_array_equal(
            native.call_args.kwargs["eligible"], refs.expected)
        self.assertEqual(matrix.matmul_calls, 0)
        self.assertEqual(refs.filter_calls, [_FILTERS])
        self.assertEqual(
            [row["session"] for row in payload["results"]],
            ["session-7", "session-11"],
        )

    def test_grouped_search_passes_filter_to_native_grouped_scan(self) -> None:
        refs = _Refs((4, 9, 15))
        matrix = _DenseTrap(_COVERAGE["indexed"])
        candidates = (
            np.asarray([9, 4], dtype=np.int64),
            np.asarray([0.95, 0.8], dtype=np.float32),
            np.asarray([8, 3], dtype=np.uint32),
            2,
        )
        patches = self._base_patches(refs)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], patches[6], patches[7], \
                mock.patch.object(ask, "_message_artifacts",
                                  return_value=((), matrix, refs, _COVERAGE)), \
                mock.patch.object(semantic_q8, "grouped_exact_candidates",
                                  return_value=candidates) as native:
            payload = json.loads(ask.tool_search_messages(
                "query", k=2, filters=_FILTERS,
                group_session=True, envelope=True))

        native.assert_called_once()
        np.testing.assert_array_equal(
            native.call_args.kwargs["eligible"], refs.expected)
        self.assertEqual(matrix.matmul_calls, 0)
        self.assertEqual(refs.filter_calls, [_FILTERS])
        self.assertEqual(
            [row["session"] for row in payload["results"]],
            ["session-9", "session-4"],
        )

    def test_hybrid_session_search_uses_filtered_grouped_q8(self) -> None:
        refs = _Refs((4, 9, 15))
        matrix = _DenseTrap(_COVERAGE["indexed"])
        candidates = (
            np.asarray([9, 4], dtype=np.int64),
            np.asarray([0.95, 0.8], dtype=np.float32),
            np.asarray([8, 3], dtype=np.uint32),
            2,
        )
        patches = self._base_patches(refs)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], patches[6], patches[7], \
                mock.patch.object(ask, "_message_artifacts",
                                  return_value=((), matrix, refs, _COVERAGE)), \
                mock.patch.object(ask, "_summary_artifacts",
                                  side_effect=RuntimeError("no summaries")), \
                mock.patch.object(semantic_q8, "grouped_exact_candidates",
                                  return_value=candidates) as native, \
                mock.patch("explore._session_concept", return_value={}):
            payload = json.loads(ask.tool_search_hybrid(
                "query", k=2, filters=_FILTERS, timing=True))

        native.assert_called_once()
        np.testing.assert_array_equal(
            native.call_args.kwargs["eligible"], refs.expected)
        self.assertEqual(matrix.matmul_calls, 0)
        self.assertEqual(refs.filter_calls, [_FILTERS])
        self.assertEqual(
            [row["session"] for row in payload["results"]],
            ["session-9", "session-4"],
        )
        self.assertIn("q8_retrieval", payload["_semantic_timing"]["phases_ms"])
        self.assertNotIn("matmul", payload["_semantic_timing"]["phases_ms"])

    def test_large_segmented_index_fails_closed_when_filtered_q8_is_missing(self) -> None:
        refs = _Refs()
        matrix = _DenseTrap(_COVERAGE["indexed"])
        patches = self._base_patches(refs)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], patches[6], patches[7], \
                mock.patch.object(ask, "_message_artifacts",
                                  return_value=((), matrix, refs, _COVERAGE)), \
                mock.patch.object(semantic_q8, "exact_candidates",
                                  return_value=None):
            with self.assertRaises((ask.MessageRefsUnavailable, RuntimeError)):
                ask.tool_search_messages(
                    "query", k=2, filters=_FILTERS, envelope=True)

        self.assertEqual(matrix.matmul_calls, 0)

    def test_small_segmented_fixture_keeps_dense_fallback(self) -> None:
        coverage = {
            "indexed": 2, "total": 2, "pending": 0,
            "fraction": 1.0, "complete": True, "order": "complete",
        }
        refs = _Refs((0, 1), rows=2)
        matrix = _SmallDense()
        patches = self._base_patches(refs, coverage)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], patches[6], patches[7], \
                mock.patch.object(ask, "_message_artifacts",
                                  return_value=((), matrix, refs, coverage)), \
                mock.patch.object(semantic_q8, "exact_candidates",
                                  return_value=None):
            payload = json.loads(ask.tool_search_messages(
                "query", k=1, filters=_FILTERS, envelope=True))

        self.assertEqual(matrix.matmul_calls, 1)
        self.assertEqual(payload["results"][0]["session"], "session-1")


if __name__ == "__main__":
    unittest.main()
