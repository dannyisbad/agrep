"""Pin the content-bound handle: a citation carries a claim, not just an address.

docs/HANDLE_IDENTITY.md is the design. The failure this suite exists to
prevent is the one an address alone cannot catch - the cited turn still
resolves, in range, and holds different content.
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest import mock

from _test_support import isolate_data_dir

isolate_data_dir()
import around
import compact
import recall
import search


class ContentDigestCodec(unittest.TestCase):
    def test_digest_is_stable_across_processes(self) -> None:
        # frozen: this travels in saved notes and must not drift with a seed
        self.assertEqual(compact.content_digest("hello world"), "d2e7")
        # empty text is the FNV-1a offset basis, low 16 bits
        self.assertEqual(compact.content_digest(""), "2325")

    def test_handles_round_trip_with_and_without_a_digest(self) -> None:
        hit = {"session": "1a2b3c4d5e6f", "turn": 214}
        plain = compact.encode_result_handle(hit)
        bound = compact.encode_result_handle(hit, text="hello world")
        self.assertEqual(plain, "@1a2b3c4d:214")
        self.assertEqual(bound, "@1a2b3c4d:214.d2e7")
        self.assertEqual(compact.parse_result_handle_parts(plain),
                         ("1a2b3c4d", 214, None))
        self.assertEqual(compact.parse_result_handle_parts(bound),
                         ("1a2b3c4d", 214, "d2e7"))
        # the two-value parse stays the contract every existing caller uses
        self.assertEqual(compact.parse_result_handle(bound), ("1a2b3c4d", 214))

    def test_unprefixed_turn_syntax_needs_a_digest_to_be_a_handle(self) -> None:
        self.assertTrue(compact.is_result_handle("1a2b3c4d:214.d2e7"))
        self.assertFalse(compact.is_result_handle("1a2b3c4d:214"))

    def test_a_row_digest_mints_when_no_text_is_passed(self) -> None:
        hit = {"session": "1a2b3c4d5e6f", "turn": 7, "content_digest": "abcd"}
        self.assertEqual(compact.encode_result_handle(hit), "@1a2b3c4d:7.abcd")

    def test_opaque_sessions_carry_the_digest_identically(self) -> None:
        handle = compact.encode_result_handle(
            {"session": "has spaces", "turn": 3}, text="hello world")
        self.assertTrue(handle.endswith(":3.d2e7"))
        self.assertEqual(compact.parse_result_handle_parts(handle),
                         ("has spaces", 3, "d2e7"))


class HandleVerification(unittest.TestCase):
    def _window(self, turns):
        return {"turns": [{"turn": t, "text": text, "reply": reply}
                          for t, text, reply in turns]}

    def test_matching_content_verifies_in_place(self) -> None:
        window = self._window([(5, "hello world", "")])
        digest = compact.content_digest("hello world")
        self.assertEqual(around._verify_handle_digest(window, 5, digest), 5)

    def test_a_handle_may_cite_either_speaker(self) -> None:
        window = self._window([(5, "question", "hello world")])
        digest = compact.content_digest("hello world")
        self.assertEqual(around._verify_handle_digest(window, 5, digest), 5)

    def test_renumbering_is_rescued_to_the_turn_that_holds_it(self) -> None:
        window = self._window([(5, "someone else's row", ""),
                               (6, "hello world", "")])
        digest = compact.content_digest("hello world")
        self.assertEqual(around._verify_handle_digest(window, 5, digest), 6)

    def test_content_gone_refuses_instead_of_serving_the_address(self) -> None:
        window = self._window([(5, "a different conversation", "")])
        digest = compact.content_digest("hello world")
        self.assertIsNone(around._verify_handle_digest(window, 5, digest))

    def test_two_equal_claims_refuse_rather_than_guess(self) -> None:
        window = self._window([(5, "elsewhere", ""), (6, "hello world", ""),
                               (7, "hello world", "")])
        digest = compact.content_digest("hello world")
        self.assertIsNone(around._verify_handle_digest(window, 5, digest))

    def test_tool_event_claim_uses_the_searchable_event_text(self) -> None:
        event = {"turn": 5, "ts": 1, "kind": "tool", "name": "shell",
                 "input": "make test", "output": "ok", "ok": True}
        window = {**self._window([(5, "question", "answer")]),
                  "events": [event]}
        digest = compact.content_digest("shell: make test\nok")
        self.assertEqual(around._verify_handle_digest(window, 5, digest), 5)

    def test_resolver_scans_the_session_after_a_bounded_miss(self) -> None:
        initial = {**self._window([(5, "replacement", "")]),
                   "first_turn": 1, "last_turn": 20}
        full = {**self._window([(5, "replacement", ""),
                                (17, "claimed", "")]),
                "first_turn": 1, "last_turn": 20}
        digest = compact.content_digest("claimed")
        with mock.patch.object(around.explore, "get_window", return_value=full) as get:
            self.assertEqual(
                around._resolve_handle_digest(
                    initial, "abcdef012345", 5, digest), 17)
        get.assert_called_once_with("abcdef012345", 5, 15)

    def test_resolver_does_not_trust_a_locally_unique_rescue(self) -> None:
        initial = {**self._window([(5, "replacement", ""),
                                   (6, "claimed", "")]),
                   "first_turn": 1, "last_turn": 20}
        full = {**initial,
                "turns": [*initial["turns"],
                          {"turn": 17, "text": "claimed", "reply": ""}]}
        digest = compact.content_digest("claimed")
        with mock.patch.object(around.explore, "get_window", return_value=full):
            self.assertIsNone(around._resolve_handle_digest(
                initial, "abcdef012345", 5, digest))


class RenderedHandleIdentity(unittest.TestCase):
    SESSION = "abcdef012345"
    TEXT = "the full searchable row"
    DIGEST = compact.content_digest(TEXT)

    def _hit(self) -> dict:
        return {"session": self.SESSION, "turn": 7, "ts": 1,
                "who": "user", "agent": "codex", "project": "agrep",
                "matched": "phrase", "score": 1.0,
                "content_digest": self.DIGEST, "snippet": "searchable row"}

    def test_primary_compact_row_is_content_bound(self) -> None:
        index = compact.session_prefix_index((self.SESSION,))
        line = search._compact_line(self._hit(), index)
        self.assertTrue(line.startswith(f"@abcdef01:7.{self.DIGEST} "))
        with self.assertRaisesRegex(compact.CompactError, "content identity"):
            search._compact_line(
                {key: value for key, value in self._hit().items()
                 if key != "content_digest"}, index)

    def test_grouped_header_is_content_bound(self) -> None:
        index = compact.session_prefix_index((self.SESSION,))
        head = search._chat_head(
            self._hit(), 1, False, session_index=index)
        self.assertIn(f"@abcdef01:7.{self.DIGEST}", head)
        output = io.StringIO()
        with mock.patch.object(
                search.common, "indexed_session_prefix_candidates",
                return_value=index), \
                mock.patch.object(
                    search.common, "indexed_family_roots",
                    return_value={self.SESSION: self.SESSION}), \
                contextlib.redirect_stdout(output):
            search._emit_grouped([self._hit()], None, False)
        self.assertIn(f"@abcdef01:7.{self.DIGEST}", output.getvalue())

    def test_probe_pointer_is_content_bound(self) -> None:
        index = compact.session_prefix_index((self.SESSION,))
        line = recall._probe_line(
            ["searchable"], [self._hit()], "corpusdb", 1,
            session_index=index)
        self.assertIn(f"@abcdef01:7.{self.DIGEST}", line or "")

    def test_machine_rows_do_not_leak_the_internal_digest_field(self) -> None:
        hit = self._hit()
        self.assertNotIn("content_digest", search.public_rows([hit])[0])
        index = compact.session_prefix_index((self.SESSION,))
        with mock.patch.object(
                search.common, "indexed_session_prefix_candidates",
                return_value=index) as prefixes:
            row = search.public_rows([hit], result_handles=True)[0]
        prefixes.assert_called_once()
        self.assertEqual(row["handle"], f"@abcdef01:7.{self.DIGEST}")
        self.assertNotIn("content_digest", row)
        self.assertEqual(hit["content_digest"], self.DIGEST)

    def test_machine_handle_derives_only_from_authoritative_text(self) -> None:
        hit = self._hit()
        hit.pop("content_digest")
        hit["text"] = self.TEXT
        index = compact.session_prefix_index((self.SESSION,))
        with mock.patch.object(
                search.common, "indexed_session_prefix_candidates",
                return_value=index):
            derived = search.public_rows([hit], result_handles=True)[0]
        self.assertEqual(derived["handle"], f"@abcdef01:7.{self.DIGEST}")
        hit.pop("text")
        with mock.patch.object(
                search.common, "indexed_session_prefix_candidates",
                return_value=index):
            self.assertNotIn(
                "handle", search.public_rows([hit], result_handles=True)[0])

    def test_machine_handle_round_trips_and_rejects_mutated_content(self) -> None:
        index = compact.session_prefix_index((self.SESSION,))
        with mock.patch.object(
                search.common, "indexed_session_prefix_candidates",
                return_value=index):
            handle = search.public_rows(
                [self._hit()], result_handles=True)[0]["handle"]

        def run(text: str) -> tuple[int, list[dict], str]:
            window = {
                "session": self.SESSION, "center": 7,
                "first_turn": 7, "last_turn": 7,
                "agent": "codex", "project": "agrep",
                "concept": "", "title": "", "events": [],
                "turns": [{"turn": 7, "who": "user", "ts": 1,
                           "text": text, "reply": ""}],
            }
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.object(
                    around.common, "MESSAGES_PATH", Path(__file__)), \
                    mock.patch.object(
                        around.explore, "resolve_session",
                        return_value=[self.SESSION]), \
                    mock.patch.object(
                        around.explore, "get_window", return_value=window), \
                    mock.patch.object(
                        around.explore, "_session_index",
                        return_value={self.SESSION: {}}), \
                    contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(stderr):
                rc = around.main([handle, "--json", "--no-auto"])
            rows = [json.loads(line) for line in stdout.getvalue().splitlines()]
            return rc, rows, stderr.getvalue()

        rc, rows, stderr = run(self.TEXT)
        message = next(row for row in rows if row.get("kind") == "msg")
        self.assertEqual((rc, message["kind"], message["text"]),
                         (0, "msg", self.TEXT))
        self.assertEqual(stderr, "")
        rc, rows, stderr = run("mutated content")
        self.assertEqual(rc, 2)
        self.assertEqual(rows[0]["error"]["code"], "content-missing")
        self.assertEqual(stderr, "")


class DirectHandleConsumers(unittest.TestCase):
    SESSION = "abcdef012345"

    @staticmethod
    def _window(center: int, turns: list[tuple[int, str, str]]) -> dict:
        return {"session": DirectHandleConsumers.SESSION, "center": center,
                "first_turn": min(turn for turn, _text, _reply in turns),
                "last_turn": max(turn for turn, _text, _reply in turns),
                "agent": "codex", "project": "agrep", "concept": "",
                "title": "", "events": [],
                "turns": [{"turn": turn, "who": "user", "ts": 1,
                           "text": text, "reply": reply}
                          for turn, text, reply in turns]}

    def _recall(self, handle: str, windows: dict[int, dict],
                extra: tuple[str, ...] = ()) -> tuple[int, str, str]:
        def one(_session, turn, _radius):
            return windows[int(turn)]

        def many(requests):
            return [windows[int(turn)] for _session, turn, _radius in requests]

        out, err = io.StringIO(), io.StringIO()
        index = compact.session_prefix_index((self.SESSION,))
        with mock.patch.object(recall.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(recall.indexd_runtime, "agent_freshness_notice",
                                  return_value=None), \
                mock.patch.object(recall.common, "in_agent_context", return_value=False), \
                mock.patch.object(recall.common, "indexed_session_prefix_candidates",
                                  return_value=index), \
                mock.patch.object(recall.explore, "resolve_session",
                                  return_value=[self.SESSION]), \
                mock.patch.object(recall.explore, "get_window", side_effect=one), \
                mock.patch.object(recall.explore, "get_windows", side_effect=many), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = recall.main([handle, *extra, "--lexical", "--budget", "0"])
        return rc, out.getvalue(), err.getvalue()

    def test_recall_matching_digest_serves_in_place(self) -> None:
        digest = compact.content_digest("claimed")
        window = self._window(5, [(5, "claimed", "")])
        rc, out, err = self._recall(f"@abcdef01:5.{digest}", {5: window})
        self.assertEqual(rc, 0)
        self.assertIn(f"@abcdef01:5.{digest}", out)
        self.assertNotIn("moved", err)

    def test_recall_dispatches_an_unprefixed_bound_handle(self) -> None:
        digest = compact.content_digest("claimed")
        window = self._window(5, [(5, "claimed", "")])
        rc, out, err = self._recall(f"abcdef01:5.{digest}", {5: window})
        self.assertEqual(rc, 0, err)
        self.assertIn(f"@abcdef01:5.{digest}", out)

    def test_digest_only_tool_recall_recovers_only_verified_event(self) -> None:
        def event(ts: int, output: str) -> dict:
            return {
                "kind": "tool", "turn": 5, "ts": ts,
                "name": "exec_command", "input": f"command {ts}",
                "output": output, "ok": False,
                "input_chars": len(f"command {ts}"),
                "output_chars": len(output),
                "output_bytes": len(output.encode("utf-8")),
                "input_truncated": False, "output_truncated": False,
            }

        selected = event(2, "LEGACY_RECALL_SELECTED")
        window = self._window(5, [(5, "inspect tools", "root cause")])
        window["events"] = [
            event(1, "AMBIENT_RECALL_BEFORE"),
            selected,
            event(3, "AMBIENT_RECALL_AFTER"),
        ]
        searchable = recall.common.tool_search_text(selected)
        handle = compact.encode_result_handle(
            {"session": self.SESSION, "turn": 5}, text=searchable)
        self.assertNotIn("~", handle)

        rc, rendered, err = self._recall(handle, {5: window})
        self.assertEqual(rc, 0, err)
        self.assertIn("LEGACY_RECALL_SELECTED", rendered)
        self.assertNotIn("AMBIENT_RECALL_BEFORE", rendered)
        self.assertNotIn("AMBIENT_RECALL_AFTER", rendered)
        self.assertEqual(rendered.count("FAILED exec_command"), 1)

        rc, payload, err = self._recall(
            handle, {5: window}, extra=("--json",))
        self.assertEqual(rc, 0, err)
        tool_rows = [
            row for row in json.loads(payload)["hits"][0]["window"]
            if row.get("kind") == "tool"
        ]
        self.assertEqual([row["ts"] for row in tool_rows], [selected["ts"]])

    def test_recall_in_range_shift_is_disclosed_and_rescued(self) -> None:
        digest = compact.content_digest("claimed")
        initial = self._window(5, [(5, "replacement", ""),
                                   (6, "claimed", "")])
        moved = self._window(6, [(5, "replacement", ""),
                                 (6, "claimed", "")])
        rc, out, err = self._recall(
            f"@abcdef01:5.{digest}", {5: initial, 6: moved})
        self.assertEqual(rc, 0)
        self.assertIn(f"@abcdef01:6.{digest}", out)
        # both causes disclosed: a rescue and a digest collision look the same
        self.assertIn("handle digest matches turn 6, not turn 5", err)
        self.assertIn("or the digest is wrong", err)

    def test_recall_in_range_replacement_refuses(self) -> None:
        digest = compact.content_digest("claimed")
        window = self._window(5, [(5, "replacement", "")])
        rc, out, err = self._recall(f"@abcdef01:5.{digest}", {5: window})
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("no longer holds the content it cited", err)

    def test_legacy_recall_is_compatible_but_disclosed_unverified(self) -> None:
        window = self._window(5, [(5, "current content", "")])
        digest = compact.content_digest("current content")
        rc, out, err = self._recall("@abcdef01:5", {5: window})
        self.assertEqual(rc, 0)
        self.assertIn(f"@abcdef01:5.{digest}", out)
        self.assertIn("no content digest - matching by position only", err)

    def test_handle_row_states_real_identity_not_fabricated_blanks(self) -> None:
        digest = compact.content_digest("claimed")
        window = self._window(5, [(5, "claimed", "")])
        rc, out, _err = self._recall(
            f"@abcdef01:5.{digest}", {5: window}, extra=("--json",))
        self.assertEqual(rc, 0)
        row = json.loads(out)["hits"][0]
        # the window fixture holds the session's real facts; the served row
        # must state them (or honest null), never "" / 0 stand-ins
        self.assertEqual(row["agent"], "codex")
        self.assertEqual(row["project"], "agrep")
        self.assertEqual(row["ts"], 1)

    def test_handle_probe_pointer_carries_the_real_session_facts(self) -> None:
        digest = compact.content_digest("claimed")
        window = self._window(5, [(5, "claimed", "")])
        rc, out, _err = self._recall(
            f"@abcdef01:5.{digest}", {5: window}, extra=("--probe",))
        self.assertEqual(rc, 0)
        self.assertIn("codex", out)
        self.assertIn("agrep", out)

    def test_handle_wins_over_mismatching_filters_with_disclosure(self) -> None:
        digest = compact.content_digest("claimed")
        window = self._window(5, [(5, "claimed", "")])
        rc, out, err = self._recall(
            f"@abcdef01:5.{digest}", {5: window},
            extra=("--project", "elsewhere"))
        self.assertEqual(rc, 0)
        self.assertIn(f"@abcdef01:5.{digest}", out)
        self.assertIn("explicit result handle wins", err)
        self.assertIn("--project not applied", err)
        self.assertIn("--project would have excluded it", err)

    def test_direct_handle_ignores_unresolvable_chat_filter(self) -> None:
        digest = compact.content_digest("claimed")
        window = self._window(5, [(5, "claimed", "")])
        with mock.patch.object(
                recall.search, "_resolve_chat",
                side_effect=AssertionError(
                    "direct handle must bypass --chat resolution")):
            rc, out, err = self._recall(
                f"@abcdef01:5.{digest}", {5: window},
                extra=("--chat", "stale-or-ambiguous-prefix"))
        self.assertEqual(rc, 0, err)
        self.assertIn(f"@abcdef01:5.{digest}", out)
        self.assertIn("explicit result handle wins", err)
        self.assertIn("--chat not applied", err)

    def test_handle_json_filter_coverage_stays_truthful(self) -> None:
        digest = compact.content_digest("claimed")
        window = self._window(5, [(5, "claimed", "")])
        rc, out, _err = self._recall(
            f"@abcdef01:5.{digest}", {5: window},
            extra=("--json", "--project", "elsewhere"))
        self.assertEqual(rc, 0)
        coverage = json.loads(out)["filter_coverage"]
        # skipped filters must never be reported as checked (the lie the
        # disclosure record exists to prevent)
        self.assertFalse(coverage["checked"])
        self.assertIn("--project", coverage["reason"])
        rc, out, _err = self._recall(
            f"@abcdef01:5.{digest}", {5: window}, extra=("--json",))
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out)["filter_coverage"]["checked"])

    def test_handle_with_consistent_filters_still_discloses_the_skip(self) -> None:
        digest = compact.content_digest("claimed")
        window = self._window(5, [(5, "claimed", "")])
        rc, _out, err = self._recall(
            f"@abcdef01:5.{digest}", {5: window},
            extra=("--project", "agrep", "--agent", "codex"))
        self.assertEqual(rc, 0)
        self.assertIn("--agent, --project not applied", err)
        self.assertNotIn("would have excluded it", err)


class RescueOutcomeDistinction(unittest.TestCase):
    """HANDLE_IDENTITY.md's table keeps 'rescue finds several rows' and
    'rescue finds nothing' as distinct rows: an ambiguous rescue lists its
    candidates instead of reporting the content as lost."""

    def _window(self, turns):
        return {"turns": [{"turn": t, "text": text, "reply": reply}
                          for t, text, reply in turns],
                "first_turn": turns[0][0], "last_turn": turns[-1][0]}

    def test_claims_name_the_three_outcomes(self) -> None:
        digest = compact.content_digest("continue")
        several = self._window([(214, "something else", ""),
                                (215, "continue", ""), (220, "continue", "")])
        rescue = around._digest_claims(several, 214, digest)
        self.assertEqual(rescue.outcome, "ambiguous")
        self.assertEqual(rescue.candidates, (215, 220))
        gone = around._digest_claims(
            self._window([(214, "something else", "")]), 214, digest)
        self.assertEqual((gone.outcome, gone.candidates), ("absent", ()))
        held = around._digest_claims(
            self._window([(214, "continue", "")]), 214, digest)
        self.assertEqual((held.outcome, held.turn), ("unique", 214))

    def test_resolver_reports_ambiguity_not_loss(self) -> None:
        digest = compact.content_digest("continue")
        window = self._window([(214, "something else", ""),
                               (215, "continue", ""), (220, "continue", "")])
        with mock.patch.object(around.explore, "get_window",
                               return_value=window):
            rescue = around._resolve_handle_claims(
                window, "abcdef012345", 214, digest)
        self.assertEqual(rescue.outcome, "ambiguous")
        self.assertEqual(rescue.candidates, (215, 220))

    def test_around_ambiguous_handle_lists_candidates_and_refuses(self) -> None:
        digest = compact.content_digest("continue")
        window = {"session": "abcdef012345", "center": 214,
                  "first_turn": 210, "last_turn": 220, "agent": "codex",
                  "project": "agrep", "concept": "", "title": "",
                  "events": [],
                  "turns": [{"turn": t, "who": "user", "ts": 1, "text": text,
                             "reply": ""}
                            for t, text in ((214, "something else"),
                                            (215, "continue"),
                                            (220, "continue"))]}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(around.indexd_runtime, "ensure_index",
                               lambda auto=True, **_kw: True), \
                mock.patch.object(around.indexd_runtime, "machine_freshness",
                                  return_value={}), \
                mock.patch.object(around.explore, "resolve_session",
                                  lambda q: ["abcdef012345"]), \
                mock.patch.object(around.explore, "get_window",
                                  lambda *_a, **_kw: window), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = around.main([f"@abcdef01:214.{digest}", "--json"])
        self.assertEqual(rc, 2)
        error = json.loads(out.getvalue().splitlines()[0])["error"]
        self.assertEqual(error["code"], "content-ambiguous")
        self.assertEqual(error["candidates"], [215, 220])
        self.assertIn("refusing to guess", error["reason"])
        self.assertIn("215, 220", error["reason"])
        self.assertEqual(err.getvalue(), "")


class RecallRescueAndDisclosureParity(DirectHandleConsumers):
    """recall's handle lane mirrors around: ambiguity is not loss, and every
    verification outcome is a field a stdout-only pipe can read."""

    def test_recall_ambiguous_handle_refuses_with_candidates(self) -> None:
        digest = compact.content_digest("continue")
        window = self._window(214, [(214, "something else", ""),
                                    (215, "continue", ""),
                                    (220, "continue", "")])
        rc, out, err = self._recall(
            f"@abcdef01:214.{digest}", {214: window}, extra=("--json",))
        self.assertEqual(rc, 2)
        self.assertIn("215, 220", err)
        self.assertIn("refusing to guess", err)
        self.assertNotIn("no longer holds", err)
        payload = json.loads(out)
        self.assertEqual(payload["error"]["code"], "content-ambiguous")

    def test_recall_json_carries_the_unverified_resolve(self) -> None:
        window = self._window(5, [(5, "current content", "")])
        rc, out, err = self._recall("@abcdef01:5", {5: window},
                                    extra=("--json",))
        self.assertEqual(rc, 0)
        served = json.loads(out)["served"]
        self.assertFalse(served["served_as_requested"])
        kinds = [d["kind"] for d in served["divergences"]]
        self.assertEqual(kinds, ["handle_unverified"])
        # the pipe reads the same sentence the human got on stderr
        self.assertIn(served["divergences"][0]["note"], err)

    def test_recall_json_carries_the_rescued_move(self) -> None:
        digest = compact.content_digest("claimed")
        initial = self._window(5, [(5, "replacement", ""), (6, "claimed", "")])
        moved = self._window(6, [(5, "replacement", ""), (6, "claimed", "")])
        rc, out, _err = self._recall(
            f"@abcdef01:5.{digest}", {5: initial, 6: moved},
            extra=("--json",))
        self.assertEqual(rc, 0)
        served = json.loads(out)["served"]
        moved_note, = [d for d in served["divergences"]
                       if d["kind"] == "content_moved"]
        self.assertEqual((moved_note["requested"], moved_note["served"]),
                         (5, 6))

    def test_recall_verified_resolve_carries_no_served_record(self) -> None:
        digest = compact.content_digest("claimed")
        window = self._window(5, [(5, "claimed", "")])
        rc, out, _err = self._recall(
            f"@abcdef01:5.{digest}", {5: window}, extra=("--json",))
        self.assertEqual(rc, 0)
        self.assertNotIn("served", json.loads(out))


if __name__ == "__main__":
    unittest.main()
