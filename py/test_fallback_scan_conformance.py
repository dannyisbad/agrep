"""Pin the Rust fallback scanner to Python's canonical tool-search contract."""

from __future__ import annotations

import json
import re
import unittest
from unittest import mock
from pathlib import Path

from _test_support import isolate_data_dir


isolate_data_dir()

import console
import common
import events
import explore
import search


FIXTURE = Path(__file__).parent / "fixtures" / "fallback_scan_conformance.json"


def _single_occurrences(event: dict, query: str) -> int:
    lowered = query.lower()
    pattern = (re.compile(re.escape(query), re.I)
               if "i" in lowered or "s" in lowered else None)
    return events._tool_literal_occurrences(event, lowered, pattern)


class FallbackScanConformance(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        if cls.fixture.get("schema") != 2:
            raise AssertionError("unsupported fallback scanner fixture schema")

    def test_single_token_oracle(self) -> None:
        for case in self.fixture["cases"]:
            with self.subTest(case=case["name"]):
                self.assertEqual(
                    _single_occurrences(case["event"], case["query"]),
                    case["occurrences"])

    def test_encoded_json_oracle(self) -> None:
        for case in self.fixture["encoded_cases"]:
            with self.subTest(case=case["name"]):
                self.assertIn("\\u", case["event_json"])
                event = json.loads(case["event_json"])
                self.assertEqual(
                    _single_occurrences(event, case["query"]),
                    case["occurrences"])

    def test_multi_token_oracle(self) -> None:
        for case in self.fixture["multi_cases"]:
            with self.subTest(case=case["name"]):
                query = case["query"]
                tokens = [token for token in re.split(
                    r"[\s\-_]+", query.strip()) if token]
                text = events.tool_search_text(case["event"])
                phrase = re.compile(
                    r"[\W_]*".join(re.escape(token) for token in tokens),
                    re.I)
                self.assertEqual(
                    sum(1 for _match in phrase.finditer(text)),
                    case["phrase_occurrences"])
                lowered = text.lower()
                self.assertEqual(
                    all(console.insensitive_span(text, token, lowered) is not None
                        for token in tokens),
                    case["all_terms"])

    def test_exact_score_oracle(self) -> None:
        for case in self.fixture["exact_score_cases"]:
            with self.subTest(case=case["name"]):
                event = (json.loads(case["event_json"])
                         if "event_json" in case else case["event"])
                row = events.tool_row_from_event(event, event.get("ts", 0), 0)
                self.assertIsNotNone(row)
                text = row["text"]
                entry = {
                    "session": "one", "turn": 0, "ts": event.get("ts", 0),
                    "agent": "codex", "project": "product", "concept": "",
                    "model": "", "model_source": "tool", "who": "tool",
                    "text": text, "low": text.lower(),
                    **{field: row.get(field) for field in explore._SCAN_EVENT_FIELDS},
                }
                entry["event_kind"] = row.get("kind", "")
                if row.get("payload_bounds") is not None:
                    entry["payload_bounds"] = row["payload_bounds"]
                tokens = [token for token in re.split(
                    r"[\s\-_]+", case["query"].strip()) if token]
                if case["lane"] == "phrase":
                    match = explore._kw_pattern(case["query"]).search(text)
                    self.assertIsNotNone(match)
                    hit = explore.scan_hit(entry, *match.span())
                else:
                    spans = [common.insensitive_span(text, token, entry["low"])
                             for token in tokens]
                    self.assertTrue(all(span is not None for span in spans))
                    hit = {
                        **{field: entry[field] for field in explore._SCAN_HIT_FIELDS},
                        "content_digest": "0" * 64,
                        **explore.scan_event_columns(entry),
                        "snippet": common.snip_spans(text, spans),
                        "matched": "all-terms",
                    }
                with mock.patch.object(
                        search, "_native_boundary_scores", return_value=False):
                    search._rank(
                        [hit], case["query"], "keyword", "score",
                        boundary=search._prepare_boundary(
                            case["query"], "keyword", None),
                        top_k=1, now_ms=case.get("now_ms", 1000))
                self.assertEqual(hit["snippet"], case["snippet"])
                self.assertEqual(hit["score"], case["score"])


if __name__ == "__main__":
    unittest.main()
