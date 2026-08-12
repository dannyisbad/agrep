"""Contract tests for the native half of the JSONL fallback lane."""

from __future__ import annotations

import contextlib
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _test_support import isolate_data_dir


isolate_data_dir()

import explore
import corpusdb
import search


def _filter(**updates) -> dict:
    flt = {
        "agent": None, "project": None, "who": None, "model": None,
        "chat": None, "include_tools": True, "exclude_session": None,
        "exclude_session_from_turn": None,
    }
    flt.update(updates)
    return flt


def _spec(query: str = "deadlock") -> search.QuerySpec:
    return search.QuerySpec(
        q=query, mode="keyword", limit=4, sort="score", agent=None,
        project=None, who=None, model=None, model_soft=False, chat=None,
        since_ms=None, until_ms=None, exhaustive=False, session_limit=None,
        include_tools=True, exclude_session=None,
        exclude_session_from_turn=None, allow_fallback=True,
        exact_totals=False, family_diverse=False,
        semantic_timeout_s=None, exclude_project=None)


def _response(query: str, *, owners: list[dict] | None = None) -> dict:
    owners = owners or [{"agent": "codex", "session": "one", "project": "p"}]
    digest = hashlib.sha256()
    for owner in owners:
        for field in ("agent", "session", "project"):
            body = owner[field].encode()
            digest.update(len(body).to_bytes(8, "little"))
            digest.update(body)
    event = {"ts": 7, "kind": "tool", "name": query}
    return {
        "protocol": 2, "state": "ok", "ingest_generation": "i",
        "event_generation": "e", "envelope_complete": True,
        "best_omitted": None, "next_after": None,
        "scanned": {
            "sessions": 1, "events": 1, "bytes": 32,
            "refined_matches": 0, "conservative_matches": 1,
        },
        "matches": {
            "tools": 1, "phrase_tools": 1, "all_terms_tools": 0,
            "all_terms_additions": 0, "matched_sessions": 1,
            "eligible_sessions": len(owners), "matched_owner_bitmap": "01",
            "phrase_owner_bitmap": "01",
            "owner_order_sha256": digest.hexdigest(),
        },
        "candidates": [{
            "agent": "codex", "session": "one", "ordinal": 0, "ts": 7,
            "event": event, "matched": "phrase", "occurrences": 1,
            "lower_score": 0.01, "upper_score": 0.2, "refined_score": False,
        }],
    }


class NativeResponseValidation(unittest.TestCase):
    def test_query_specific_candidate_and_bitmap_contract(self) -> None:
        owners = [{"agent": "codex", "session": "one", "project": "p"}]
        response = _response("deadlock", owners=owners)
        self.assertTrue(explore._valid_native_event_response(
            response, "deadlock", owners))
        for field, value in (
                ("ordinal", 1), ("occurrences", 0),
                ("lower_score", float("nan")), ("ts", 8)):
            broken = json.loads(json.dumps(response))
            broken["candidates"][0][field] = value
            self.assertFalse(explore._valid_native_event_response(
                broken, "deadlock", owners), field)

    def test_multi_lane_totals_are_independent_and_complete(self) -> None:
        response = _response("same command")
        response["matches"].update(
            all_terms_tools=1, all_terms_additions=0)
        self.assertTrue(explore._valid_native_event_response(
            response, "same command"))
        response["matches"]["all_terms_tools"] = 0
        self.assertFalse(explore._valid_native_event_response(
            response, "same command"))

    def test_compact_event_reference_and_failure_scan_totals_are_validated(self) -> None:
        response = _response("deadlock")
        candidate = response["candidates"][0]
        candidate.pop("event")
        candidate["event_ordinal"] = 3
        self.assertTrue(explore._valid_native_event_response(
            response, "deadlock"))
        candidate["event_ordinal"] = -1
        self.assertFalse(explore._valid_native_event_response(
            response, "deadlock"))

        failed = {
            "protocol": 2, "state": "unsupported", "candidates": [],
            "best_omitted": None, "next_after": None,
            "scanned": {
                "sessions": 0, "events": 0, "bytes": 0,
                "refined_matches": 0, "conservative_matches": 0,
            },
        }
        self.assertTrue(explore._valid_native_event_response(
            failed, "deadlock"))
        failed.pop("scanned")
        self.assertFalse(explore._valid_native_event_response(
            failed, "deadlock"))

    def test_continuation_page_order_and_cursors_are_strict(self) -> None:
        response = _response("deadlock")
        candidate = response["candidates"][0]
        response.update(
            envelope_complete=False,
            next_after={field: candidate[field] for field in (
                "matched", "upper_score", "ts", "session", "ordinal")},
            best_omitted={
                "matched": "phrase", "upper_score": 0.1, "ts": 6,
                "session": "one", "ordinal": 1,
            },
        )
        response["matches"].update(tools=2, phrase_tools=2)
        response["scanned"]["conservative_matches"] = 2
        self.assertTrue(explore._valid_native_event_response(
            response, "deadlock", candidate_limit=1))

        broken = json.loads(json.dumps(response))
        broken["best_omitted"]["upper_score"] = 0.3
        self.assertFalse(explore._valid_native_event_response(
            broken, "deadlock", candidate_limit=1))
        self.assertFalse(explore._valid_native_event_response(
            response, "deadlock", candidate_limit=0))

        after = response["next_after"]
        tail = _response("deadlock")
        tail["matches"].update(tools=2, phrase_tools=2)
        tail["scanned"]["conservative_matches"] = 2
        tail_candidate = tail["candidates"][0]
        tail_candidate.update(
            ordinal=1, ts=6, upper_score=0.1,
            event={"ts": 6, "kind": "tool", "name": "deadlock"})
        self.assertTrue(explore._valid_native_event_response(
            tail, "deadlock", candidate_limit=1, after=after))
        tail_candidate.update(
            ordinal=0, ts=7, upper_score=0.2,
            event={"ts": 7, "kind": "tool", "name": "deadlock"})
        self.assertFalse(explore._valid_native_event_response(
            tail, "deadlock", candidate_limit=1, after=after))


class NativeHydrationProof(unittest.TestCase):
    def test_candidate_project_must_match_the_pinned_owner(self) -> None:
        response = _response("deadlock")
        response["_owner_order"] = [
            {"agent": "codex", "session": "one", "project": "product"},
        ]
        messages = {"one": [{
            "agent": "codex", "project": "fixtures", "turn": 1, "ts": 7,
        }]}
        with mock.patch.object(
                explore, "_native_messages_for_sessions",
                return_value=messages), \
                mock.patch.object(explore, "_session_concept", return_value={}):
            self.assertIsNone(explore.native_event_candidate_hits(
                "deadlock", response))

    def test_compact_references_hydrate_under_one_event_generation(self) -> None:
        response = _response("deadlock")
        response["event_generation"] = "event-g"
        candidate = response["candidates"][0]
        candidate.pop("event")
        candidate["event_ordinal"] = 1
        payload = (
            b'{"ts":1,"kind":"tool","name":"other"}\n'
            b'{"ts":7,"kind":"tool","name":"deadlock"}\n')
        with mock.patch.object(
                explore, "_read_native_generation",
                side_effect=[b"event-g", b"event-g"]), \
                mock.patch.object(
                    explore.common, "event_blobs_bulk",
                    return_value=iter([("codex", "one", payload)])):
            hydrated = explore._native_candidate_events(response)
        self.assertEqual(hydrated[id(candidate)]["name"], "deadlock")

    def test_message_hydration_requires_the_committed_derived_proof(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            messages = root / "messages.jsonl"
            messages.write_text(
                '{"session":"one","agent":"codex","turn":1,"ts":7}\n'
                '{"session":"other","agent":"claude","turn":2,"ts":8}\n')
            (root / ".ingest.sig").write_bytes(b"sig")
            for name in corpusdb._DERIVED_PROOF_NAMES:
                path = root / name
                if not path.exists():
                    path.write_bytes(b"")
            files = []
            for name in corpusdb._DERIVED_PROOF_NAMES:
                path = root / name
                identity = corpusdb._proof_file_identity(path)
                if corpusdb._PLATFORM_NAME == "posix":
                    change_token = {
                        "Metadata": corpusdb._unix_change_token(identity[2])}
                elif corpusdb._PLATFORM_NAME == "nt":
                    try:
                        _change_time, usn = corpusdb._windows_file_state(
                            path, include_usn=True)
                        if usn is None:
                            raise OSError("filesystem did not return a USN")
                        change_token = {"Metadata": usn}
                    except OSError:
                        change_token = {
                            "ContentSha256": list(
                                corpusdb._content_sha256(path, identity))}
                else:
                    change_token = {"Metadata": 0}
                files.append({
                    "name": name, "len": identity[0],
                    "modified_ns": identity[1],
                    "change_token": change_token,
                    "edge_hash": corpusdb._edge_hash(
                        path, identity[0], identity),
                })
            (root / ".derived_generation.json").write_text(json.dumps({
                "version": corpusdb._DERIVED_PROOF_VERSION,
                "signature": "sig", "files": files,
            }))
            generation = hashlib.sha256(b"sig").hexdigest()
            with mock.patch.object(explore.common, "DATA_DIR", root), \
                    mock.patch.object(explore.common, "MESSAGES_PATH", messages):
                rows = explore._native_messages_for_sessions({"one"}, generation)
                self.assertEqual([row["session"] for row in rows["one"]], ["one"])
                messages.write_text(messages.read_text() + " ")
                self.assertIsNone(explore._native_messages_for_sessions(
                    {"one"}, generation))


class NativeRoutingContract(unittest.TestCase):
    def test_v9_proof_rejects_before_owner_or_message_work(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            binary = root / "agrep-rs"
            binary.write_bytes(b"x")
            (root / ".events_complete.codex.json").write_text(
                json.dumps({"version": 9, "agents": ["codex"]}))
            with mock.patch.object(explore.common, "DATA_DIR", root), \
                    mock.patch.object(explore.common, "ingest_bin", return_value=binary), \
                    mock.patch.object(explore.common, "setting", return_value="on"), \
                    mock.patch.object(
                        explore, "_native_event_owners",
                        side_effect=AssertionError("owners must stay cold")), \
                    mock.patch.object(explore, "_kick_derived_repair") as repair:
                self.assertFalse(explore.native_event_scan_preflight(_filter()))
            repair.assert_called_once()

    def test_window_keeps_structural_sides_in_the_native_lane(self) -> None:
        rows = {
            "caller": {"agent": "codex", "project": "p"},
            "custom-side": {"agent": "codex", "project": "p"},
            "agent-name-only": {"agent": "codex", "project": "p"},
            "other": {"agent": "claude", "project": "q"},
        }
        family = (
            "caller",
            frozenset({"caller", "custom-side", "agent-name-only"}),
            frozenset({"custom-side"}),
        )
        flt = _filter(
            exclude_session="caller", exclude_session_from_turn=3)
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(explore.common, "DATA_DIR", Path(td)), \
                mock.patch.object(explore.common, "setting", return_value="on"), \
                mock.patch.object(
                    explore, "_native_session_owner_rows",
                    return_value=[{"session": session, **row}
                                  for session, row in rows.items()]), \
                mock.patch.object(
                    explore.common, "indexed_calling_family_with_sides",
                    return_value=family), \
                mock.patch.object(explore, "_messages_by_session", return_value={
                    "caller": [
                        {"ts": 100, "turn": 1}, {"ts": 300, "turn": 3},
                    ]}):
            (Path(td) / "sessions.jsonl").write_text("{}\n")
            owners = explore._native_event_owners(flt)
            window = explore._native_caller_event_window(flt)
        self.assertEqual(
            owners,
            [{"agent": "claude", "session": "other", "project": "q"},
             {"agent": "codex", "session": "agent-name-only", "project": "p"},
             {"agent": "codex", "session": "caller", "project": "p"},
             {"agent": "codex", "session": "custom-side", "project": "p"}])
        self.assertEqual(window, {
            "session": "caller", "boundary": 3,
            "marks": [{"ts": 100, "turn": 1}, {"ts": 300, "turn": 3}],
        })

    def test_malformed_window_boundary_fails_open(self) -> None:
        with mock.patch.object(
                explore, "_messages_by_session",
                side_effect=AssertionError("malformed boundary must stay cold")):
            for boundary in ("3", 3.0, True):
                with self.subTest(boundary=boundary):
                    self.assertIsNone(explore._native_caller_event_window(_filter(
                        exclude_session="caller",
                        exclude_session_from_turn=boundary)))

    def test_started_timeout_requests_the_outer_python_fallback(self) -> None:
        prose = {
            "hits": [], "total": 0, "chats": 0, "phrase_chats": 0,
            "tool_hits": 0, "totals_exact": True, "_matched_sessions": set(),
        }
        response = {
            "protocol": 2, "state": "unsupported", "candidates": [],
            "detail": "timed out", "_native_started": True,
        }
        generation = {"generations": ("i", "e")}
        with mock.patch.object(
                explore, "native_event_scan_snapshot",
                return_value=generation), \
                mock.patch.object(explore, "_freshen"), \
                mock.patch.object(
                    explore, "freeze_native_event_filter",
                    side_effect=lambda flt: dict(flt)), \
                mock.patch.object(
                    explore, "native_prose_snapshot_attempt",
                    side_effect=lambda _snapshot: contextlib.nullcontext()), \
                mock.patch.object(
                    explore, "native_event_owner_census_matches",
                    return_value=True), \
                mock.patch.object(
                explore, "native_event_scan_preflight", return_value=True), \
                mock.patch.object(
                    search, "_jsonl_bounded_single_keyword_rows",
                    return_value=prose), \
                mock.patch.object(
                    explore, "native_event_keyword_scan", return_value=response), \
                mock.patch.object(explore, "keyword_search") as python_scan, \
                mock.patch.object(search.indexd_runtime, "kick_background_repair"):
            self.assertIsNone(search._jsonl_native_keyword_once(
                _spec(), _filter(), boundary=None, big=10_000))
        python_scan.assert_not_called()

    def test_proven_zero_work_unsupported_can_fallback(self) -> None:
        prose = {
            "hits": [], "total": 0, "chats": 0, "phrase_chats": 0,
            "tool_hits": 0, "totals_exact": True, "_matched_sessions": set(),
        }
        response = {
            "protocol": 2, "state": "unsupported", "candidates": [],
            "detail": "proof upgrade needed", "_native_started": True,
            "_native_no_payload_work": True,
        }
        generation = {"generations": ("i", "e")}
        with mock.patch.object(
                explore, "native_event_scan_snapshot",
                return_value=generation), \
                mock.patch.object(explore, "_freshen"), \
                mock.patch.object(
                    explore, "freeze_native_event_filter",
                    side_effect=lambda flt: dict(flt)), \
                mock.patch.object(
                    explore, "native_prose_snapshot_attempt",
                    side_effect=lambda _snapshot: contextlib.nullcontext()), \
                mock.patch.object(
                    explore, "native_event_owner_census_matches",
                    return_value=True), \
                mock.patch.object(
                explore, "native_event_scan_preflight", return_value=True), \
                mock.patch.object(
                    search, "_jsonl_bounded_single_keyword_rows",
                    return_value=prose), \
                mock.patch.object(
                    explore, "native_event_keyword_scan", return_value=response):
            self.assertIsNone(search._jsonl_native_keyword_once(
                _spec(), _filter(), boundary=None, big=10_000))

    def test_pre_spawn_oserror_is_zero_work(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            binary = Path(td) / "agrep-rs"
            binary.write_bytes(b"x")
            generations = [b"ingest", b"events", b"ingest"]
            with mock.patch.object(explore.common, "ingest_bin", return_value=binary), \
                    mock.patch.object(explore.common, "setting", return_value="on"), \
                    mock.patch.object(
                        explore, "_read_native_generation", side_effect=generations), \
                    mock.patch.object(explore, "_native_event_owners", return_value=[{
                        "agent": "codex", "session": "one", "project": "p"}]), \
                    mock.patch.object(explore, "_native_owner_filter", return_value={}), \
                    mock.patch.object(explore, "_native_caller_event_window", return_value=None), \
                    mock.patch.object(subprocess, "Popen", side_effect=OSError("no exec")):
                failed = explore.native_event_keyword_scan(
                    "deadlock", 4, _filter())
            self.assertFalse(failed.get("_native_started", False))

    def test_continuation_rejects_moved_generations_before_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            binary = Path(td) / "agrep-rs"
            binary.write_bytes(b"x")
            with mock.patch.object(explore.common, "ingest_bin", return_value=binary), \
                    mock.patch.object(explore.common, "setting", return_value="on"), \
                    mock.patch.object(
                        explore, "_read_native_generation",
                        side_effect=[b"ingest", b"events"]), \
                    mock.patch.object(subprocess, "Popen") as popen:
                response = explore.native_event_keyword_scan(
                    "deadlock", 4, _filter(),
                    expected_generations=("stale", "events"))
            self.assertEqual(response["state"], "generation_moved")
            popen.assert_not_called()

    def test_missing_torn_and_empty_session_owner_states_are_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(explore.common, "DATA_DIR", Path(td)), \
                mock.patch.object(explore.common, "setting", return_value="on"):
            self.assertIsNone(explore._native_event_owners(_filter()))
            sessions = Path(td) / "sessions.jsonl"
            sessions.write_bytes(b'{"session":')
            self.assertIsNone(explore._native_event_owners(_filter()))
            sessions.write_bytes(b"")
            self.assertEqual(explore._native_event_owners(_filter()), [])


if __name__ == "__main__":
    unittest.main()
