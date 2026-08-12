"""Event-loader byte-accounting contracts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_support import isolate_data_dir  # noqa: E402

isolate_data_dir()
import display_policy  # noqa: E402
import events  # noqa: E402
import explore  # noqa: E402


class EventByteLoadingTests(unittest.TestCase):
    @staticmethod
    def _load(event: dict) -> dict:
        turns = [{"turn": 0, "ts": 1}]
        with mock.patch.object(explore, "get_events", return_value=[event]):
            return explore._events_for_turns(
                "codex", "session", turns, turns)[0]

    def test_event_store_stamp_uses_native_change_time(self) -> None:
        identity = (11, 22, 33, 44, 55)
        with mock.patch.object(
                events.fileops, "file_identity", return_value=identity):
            stamp = events._event_file_stamp(Path("event.sqlite3"))
        self.assertEqual(stamp, (44, 55, 33, 11, 22))

    def test_event_store_family_tracks_main_and_wal_change_time(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = Path(raw) / "event.sqlite3"
            store.write_bytes(b"main")
            wal = Path(f"{store}-wal")
            wal.write_bytes(b"side")
            main = (1, 2, 4, 3, 4)
            side = (5, 6, 4, 7, 8)

            def identities(main_identity, wal_identity):
                def identity(path):
                    if Path(path) == store:
                        return main_identity
                    if Path(path) == wal:
                        return wal_identity
                    raise FileNotFoundError(path)
                return identity

            with mock.patch.object(
                    events.fileops, "file_identity",
                    side_effect=identities(main, side)):
                before = events._event_store_stamp(store)
            with mock.patch.object(
                    events.fileops, "file_identity",
                    side_effect=identities((*main[:-1], 5), side)):
                main_after = events._event_store_stamp(store)
            with mock.patch.object(
                    events.fileops, "file_identity",
                    side_effect=identities(main, (*side[:-1], 9))):
                wal_after = events._event_store_stamp(store)

        self.assertNotEqual(before, main_after)
        self.assertNotEqual(before, wal_after)

    @unittest.skipUnless(
        sys.platform == "win32", "native Windows ChangeTime contract")
    def test_windows_event_store_rejects_restored_mtime_rewrites(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = Path(raw) / "event.sqlite3"
            wal = Path(f"{store}-wal")
            store.write_bytes(b"main-before")
            wal.write_bytes(b"wal-before")
            baseline = events._event_store_stamp(store)

            original = store.stat().st_mtime_ns
            store.write_bytes(b"main-after!")
            os.utime(store, ns=(original, original))
            main_after = events._event_store_stamp(store)

            store.write_bytes(b"main-before")
            os.utime(store, ns=(original, original))
            restored_main = events._event_store_stamp(store)
            original_wal = wal.stat().st_mtime_ns
            wal.write_bytes(b"wal-after!")
            os.utime(wal, ns=(original_wal, original_wal))
            wal_after = events._event_store_stamp(store)

        baseline_members = dict(baseline)
        main_members = dict(main_after)
        restored_members = dict(restored_main)
        wal_members = dict(wal_after)
        self.assertEqual(
            baseline_members[""][:1] + baseline_members[""][2:],
            main_members[""][:1] + main_members[""][2:],
        )
        self.assertNotEqual(
            baseline_members[""][1], main_members[""][1])
        self.assertEqual(
            restored_members["-wal"][:1] + restored_members["-wal"][2:],
            wal_members["-wal"][:1] + wal_members["-wal"][2:],
        )
        self.assertNotEqual(
            restored_members["-wal"][1], wal_members["-wal"][1])

    def test_stable_event_read_rejects_change_time_movement(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "event"
            path.write_bytes(b"payload")
            before = (11, 22, 7, 33, 44)
            after = (*before[:-1], 45)
            for reader in ("bytes", "stream"):
                with self.subTest(reader=reader), \
                        mock.patch.object(
                            events.fileops, "file_identity",
                            side_effect=(before, after)), \
                        mock.patch.object(
                            events.fileops, "file_identity_fd",
                            side_effect=(before, before)):
                    with self.assertRaisesRegex(
                            OSError, "changed while reading"):
                        if reader == "bytes":
                            events._read_regular_bytes(path)
                        else:
                            with events.open_regular_binary(path) as source:
                                self.assertEqual(source.read(), b"payload")

    def test_legacy_checkpoint_uses_open_file_change_time_stamp(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "event.jsonl"
            path.write_text(
                '{"ts":2,"kind":"tool","output":"kept"}\n',
                encoding="utf-8",
            )
            checkpoint_stamp = (1, 2, 3, 4, 5)
            opened_stamp = (1, 6, 3, 4, 5)
            with mock.patch.object(
                    explore.common, "event_store_blob",
                    return_value=(False, None)), \
                    mock.patch.object(
                        explore, "events_path", return_value=path), \
                    mock.patch.object(
                        explore.common, "_event_file_stamp",
                        return_value=checkpoint_stamp), \
                    mock.patch.object(
                        explore.common, "_event_fd_stamp",
                        return_value=opened_stamp), \
                    mock.patch.object(
                        explore, "_event_checkpoints",
                        return_value=(((1, 999, 99),), True)):
                loaded = explore.get_events(
                    "codex", "session", start_ts=2)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["output"], "kept")
        self.assertEqual(loaded[0]["i"], 0)

    def test_explicit_bytes_survive_truncated_unicode_excerpt(self) -> None:
        excerpt = ("é" * 800) + "…"
        loaded = self._load({
            "ts": 2,
            "kind": "tool",
            "output": excerpt,
            "output_chars": 900,
            "output_bytes": 1800,
            "output_truncated": True,
        })
        self.assertEqual(loaded["output_bytes"], 1800)
        self.assertNotEqual(
            loaded["output_bytes"], len(excerpt.encode("utf-8")))

    def test_one_replaced_character_still_preserves_exact_source_bytes(
            self) -> None:
        loaded = self._load({
            "ts": 2,
            "kind": "tool",
            "output": ("x" * 800) + "…",
            "output_chars": 801,
            "output_bytes": 801,
            "output_truncated": True,
        })
        self.assertEqual(loaded["output_bytes"], 801)

    def test_legacy_truncated_excerpt_keeps_bytes_unknown(self) -> None:
        loaded = self._load({
            "ts": 2,
            "kind": "tool",
            "output": ("é" * 800) + "…",
            "output_chars": 900,
            "output_truncated": True,
        })
        self.assertIsNone(loaded["output_bytes"])

    def test_complete_legacy_output_has_exact_utf8_fallback(self) -> None:
        loaded = self._load({
            "ts": 2,
            "kind": "tool",
            "output": "résumé",
            "output_chars": 6,
        })
        self.assertEqual(loaded["output_bytes"], len("résumé".encode("utf-8")))

    def test_malformed_counts_are_not_coerced_or_allowed_to_raise(self) -> None:
        invalid = ("10", 10.9, True, float("inf"), float("-inf"))
        for value in invalid:
            with self.subTest(value=value):
                loaded = self._load({
                    "ts": 2,
                    "kind": "tool",
                    "output": "partial",
                    "output_chars": value,
                    "output_bytes": value,
                    "output_truncated": True,
                })
                self.assertEqual(loaded["output_chars"], len("partial"))
                self.assertIsNone(loaded["output_bytes"])

    def test_truncated_bytes_require_canonical_char_and_marker_metadata(
            self) -> None:
        excerpt = ("x" * 800) + "…"
        invalid_rows = (
            {"output": excerpt, "output_bytes": 801,
             "output_truncated": True},
            {"output": excerpt, "output_chars": "801",
             "output_bytes": 801, "output_truncated": True},
            {"output": excerpt, "output_chars": 0,
             "output_bytes": 801, "output_truncated": True},
            {"output": excerpt, "output_chars": 801,
             "output_bytes": 801, "output_truncated": "true"},
            {"output": "x…", "output_chars": 2,
             "output_bytes": 2, "output_truncated": True},
        )
        for event in invalid_rows:
            with self.subTest(event=event):
                loaded = self._load({"ts": 2, "kind": "tool", **event})
                self.assertIsNone(loaded["output_bytes"])
                self.assertTrue(loaded["output_truncated"])

    def test_impossible_truncated_utf8_total_stays_unknown(self) -> None:
        loaded = self._load({
            "ts": 2,
            "kind": "tool",
            "output": "é",
            "output_chars": 10,
            "output_bytes": 10,
            "output_truncated": True,
        })
        self.assertIsNone(loaded["output_bytes"])

    def test_lone_surrogate_keeps_bytes_unknown_without_raising(self) -> None:
        loaded = self._load({
            "ts": 2,
            "kind": "tool",
            "output": "ok\ud800tail",
            "output_chars": 7,
        })
        self.assertIsNone(loaded["output_bytes"])
        self.assertEqual(loaded["output"], "")
        self.assertTrue(loaded["output_truncated"])

    def test_non_string_payload_does_not_become_display_prose(self) -> None:
        loaded = self._load({
            "ts": 2,
            "kind": "tool",
            "input": {"forged": "command"},
            "output": ["forged", "output"],
            "input_chars": 10,
            "output_chars": 10,
            "output_bytes": 10,
        })
        self.assertEqual(loaded["input"], "")
        self.assertEqual(loaded["output"], "")
        self.assertEqual(loaded["input_chars"], 0)
        self.assertEqual(loaded["output_chars"], 0)
        self.assertIsNone(loaded["output_bytes"])
        self.assertFalse(loaded["output_truncated"])
        self.assertIsNone(loaded["input_truncated"])

    def test_missing_or_malformed_kind_and_outcome_stay_unknown(self) -> None:
        for event in (
                {"ts": 2, "output": "result"},
                {"ts": 2, "kind": 7, "ok": 1, "output": "result"}):
            with self.subTest(event=event):
                loaded = self._load(event)
                self.assertEqual(loaded["kind"], "")
                self.assertIsNone(loaded["ok"])


class SearchEventCarriageTests(unittest.TestCase):
    def test_search_text_compatibility_and_exact_output_span(self) -> None:
        event = {
            "ts": 150,
            "kind": "tool",
            "name": "Bash",
            "input": "make test",
            "output": "\n  2 passed\ntrailing detail  \n",
            "input_chars": 9,
            "ok": True,
            "call_id": "call-1",
        }
        event["output_chars"] = len(event["output"])
        event["output_bytes"] = len(event["output"].encode("utf-8"))
        expected = (
            "Bash: make test\n\n  2 passed\ntrailing detail")
        self.assertEqual(events.tool_search_text(event), expected)
        payload = (json.dumps(event) + "\n").encode()
        row = events.tool_rows_from_payload(payload, [(100, 0)])[0]
        self.assertEqual(row["text"], expected)
        self.assertEqual(
            row["text"][slice(*row["payload_bounds"])],
            "\n  2 passed\ntrailing detail",
        )
        for field in (
                "kind", "name", "input", "output", "input_chars",
                "output_chars", "output_bytes", "ok", "call_id"):
            self.assertEqual(row[field], event[field])
        self.assertFalse(row["output_truncated"])

    def test_search_rows_do_not_manufacture_kind_or_success(self) -> None:
        event = {
            "ts": 150,
            "name": "mystery",
            "output": "payload",
            "ok": 1,
        }
        row = events.tool_rows_from_payload(
            (json.dumps(event) + "\n").encode(), [(100, 0)])[0]
        self.assertEqual(row["kind"], "")
        self.assertIsNone(row["ok"])
        self.assertIsNone(row["output_chars"])
        self.assertEqual(
            row["text"][slice(*row["payload_bounds"])], "payload")

    def test_missing_truncation_marker_is_derived_from_exact_legacy_shape(
            self) -> None:
        event = {
            "ts": 150,
            "kind": "tool",
            "name": "legacy",
            "output": ("é" * 800) + "…",
            "output_chars": 900,
            "output_bytes": 1800,
            "ok": True,
        }
        row = events.tool_rows_from_payload(
            (json.dumps(event) + "\n").encode(), [(100, 0)])[0]
        self.assertTrue(row["output_truncated"])
        preview = display_policy.tool_output_preview(row)
        self.assertEqual(preview.source_bytes, 1800)
        self.assertTrue(preview.truncated)

    def test_keyword_fallback_preserves_event_provenance_and_payload(
            self) -> None:
        event = {
            "ts": 150,
            "kind": "subagent_result",
            "name": "reviewer",
            "input": "inspect gate " + ("x" * 300),
            "output": "DECISIVE_REMEDY: rebuild exact artifact",
            "ok": True,
            "child": "child-1",
        }
        event["input_chars"] = len(event["input"])
        event["output_chars"] = len(event["output"])
        event["output_bytes"] = len(event["output"].encode("utf-8"))
        payload = (json.dumps(event) + "\n").encode()
        messages = {
            "session": [{
                "session": "session",
                "turn": 0,
                "ts": 100,
                "agent": "codex",
                "project": "agrep",
                "model": "",
            }],
        }
        with mock.patch.object(
                explore, "_messages_by_session", return_value=messages), \
                mock.patch.object(
                    explore, "_session_concept", return_value={}), \
                mock.patch.object(
                    explore.common, "event_blobs_bulk",
                    return_value=[("codex", "session", payload)]), \
                mock.patch.object(
                    explore.common, "setting", return_value="on"):
            rows = list(explore._iter_kw_corpus({"who": "tool"}))
            result = explore.keyword_search(
                "DECISIVE_REMEDY", flt={"who": "tool"})
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["event_kind"], "subagent_result")
        self.assertEqual(row["kind"], "subagent_result")
        self.assertEqual(row["who"], "tool")
        self.assertIs(row["ok"], True)
        self.assertEqual(row["child"], "child-1")
        self.assertEqual(
            row["text"][slice(*row["payload_bounds"])],
            event["output"],
        )
        self.assertEqual(result["total"], 1)
        hit = result["hits"][0]
        self.assertEqual(hit["event_kind"], "subagent_result")
        self.assertIs(hit["ok"], True)
        self.assertEqual(hit["output_bytes"], event["output_bytes"])
        self.assertIn("DECISIVE_REMEDY", hit["snippet"])
        self.assertIn("rebuild exact artifact", hit["snippet"])
        self.assertNotIn("inspect gate", hit["snippet"])
        self.assertNotIn("x" * 20, hit["snippet"])

    def test_malformed_payload_bounds_are_not_exported_as_authority(
            self) -> None:
        text = "tool input\nDECISIVE payload"
        base = {
            "session": "session",
            "agent": "codex",
            "project": "agrep",
            "concept": "",
            "model": "",
            "model_source": "tool",
            "turn": 0,
            "ts": 1,
            "who": "tool",
            "text": text,
            "low": text.lower(),
            "event_kind": "tool",
            "kind": "tool",
            "ok": True,
        }
        for bounds in ((2, 5), [11, len(text)], "11:27"):
            with self.subTest(bounds=bounds), \
                    mock.patch.object(explore, "_freshen"), \
                    mock.patch.object(
                        explore, "_iter_kw_corpus",
                        return_value=iter([{**base,
                                            "payload_bounds": bounds}])):
                hit = explore.keyword_search("DECISIVE")["hits"][0]
                self.assertNotIn("payload_bounds", hit)
                self.assertIn("DECISIVE", hit["snippet"])


if __name__ == "__main__":
    unittest.main()
