"""Goal 10 F2/F5/F7 policy below the Claude-owned render seam."""

from __future__ import annotations

from pathlib import Path
import random
import sys
import time
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))

import display_policy  # noqa: E402


def _row(session: str, text: str, *, who: str = "subagent",
         family: str = "root", digest: str | None = None,
         near_key: str | None = None) -> dict:
    row = {
        "session": session,
        "turn": 0,
        "who": who,
        "snippet": text,
        "_family_root": family,
    }
    if digest is not None:
        row["content_digest"] = digest
    if near_key is not None:
        row["_near_dedup_key"] = near_key
    return row


class LineageDedupTests(unittest.TestCase):
    def test_near_identical_sibling_prompts_fold(self) -> None:
        common = (
            "You are validating the ENOSPC incident. Inspect the original "
            "session and return the exact cause, remedy, and proof boundary."
        )
        rows = [
            _row("child-a", common + " Worker token 81af.",
                 near_key="spawn-template-1"),
            _row("child-b", common + " Worker token 92be.",
                 near_key="spawn-template-1"),
            _row("child-c", common + " Worker token a03c.",
                 near_key="spawn-template-1"),
        ]
        kept = display_policy.collapse_display_rows(rows)
        self.assertEqual(kept, [rows[0]])
        self.assertEqual(rows[0]["_dup_chats"], 2)
        self.assertTrue(rows[0]["_dup_lineage"])

    def test_fuzzy_prose_without_a_structural_repeat_key_stays_visible(
            self) -> None:
        common = (
            "You are validating the ENOSPC incident. Inspect the original "
            "session and return the exact cause, remedy, and proof boundary."
        )
        rows = [
            _row("child-a", common + " Worker token 81af."),
            _row("child-b", common + " Worker token 92be."),
        ]
        self.assertEqual(display_policy.collapse_display_rows(rows), rows)

    def test_repeat_key_must_be_exact_nonempty_string_identity(self) -> None:
        common = (
            "Sibling machinery repeats this bounded task with one volatile "
            "worker token while preserving every semantic instruction. "
        )
        invalid_pairs = (
            ("different", "keys"),
            ("", ""),
            (1, 1),
            (True, True),
        )
        for left_key, right_key in invalid_pairs:
            with self.subTest(keys=(left_key, right_key)):
                rows = [
                    _row("a", common + "token 1111"),
                    _row("b", common + "token 2222"),
                ]
                rows[0]["_near_dedup_key"] = left_key
                rows[1]["_near_dedup_key"] = right_key
                self.assertEqual(
                    display_policy.collapse_display_rows(rows), rows)

    def test_semantic_verdicts_have_distinct_structural_template_keys(
            self) -> None:
        base = (
            "Production authorization decision from canonical policy engine "
            "with verified identity, matching scope, current revision, and "
            "final result: "
        )
        rows = [
            _row("a", base + "allow", near_key="verdict:allow"),
            _row("b", base + "block", near_key="verdict:block"),
        ]
        self.assertTrue(display_policy._profile_similarity(
            display_policy._text_profile(rows[0]["snippet"]),
            display_policy._text_profile(rows[1]["snippet"]),
        ))
        self.assertEqual(display_policy.collapse_display_rows(rows), rows)

    def test_unsampled_middle_payload_has_distinct_structural_key(
            self) -> None:
        head = (
            "shared diagnostic header with provenance and stable context "
            "fields "
        ) * 5
        tail = (
            " shared verification footer with citations and reproducible "
            "boundary"
        ) * 5
        rows = [
            _row(
                "a",
                head
                + (" ALLOW destructive deployment because policy check "
                   "passed " * 3)
                + tail,
                near_key="deployment:allow",
            ),
            _row(
                "b",
                head
                + (" BLOCK destructive deployment because policy check "
                   "failed " * 3)
                + tail,
                near_key="deployment:block",
            ),
        ]
        self.assertEqual(
            display_policy._similarity_sample(rows[0]["snippet"]),
            display_policy._similarity_sample(rows[1]["snippet"]),
        )
        self.assertEqual(display_policy.collapse_display_rows(rows), rows)

    def test_equal_repeat_key_does_not_override_gross_text_mismatch(
            self) -> None:
        rows = [
            _row("a", "alpha evidence " * 20, near_key="stale-key"),
            _row("b", "omega outcome " * 20, near_key="stale-key"),
        ]
        self.assertEqual(display_policy.collapse_display_rows(rows), rows)

    def test_near_rows_from_unrelated_families_stay_visible(self) -> None:
        text = "Investigate the production failure and report exact evidence. " * 2
        rows = [_row("a", text + "one", family="root-a",
                     near_key="same-template"),
                _row("b", text + "two", family="root-b",
                     near_key="same-template")]
        self.assertEqual(display_policy.collapse_display_rows(rows), rows)

    def test_lived_prose_and_failures_never_near_fold(self) -> None:
        text = "The disk filled because stale campaign scratch consumed 20GB. " * 2
        lived = [_row("a", text + "A", who="user",
                      near_key="same-template"),
                 _row("b", text + "B", who="user",
                      near_key="same-template")]
        failed = [_row("c", text + "C", near_key="same-template"),
                  _row("d", text + "D", near_key="same-template")]
        failed[0]["ok"] = failed[1]["ok"] = False
        self.assertEqual(display_policy.collapse_display_rows(lived), lived)
        self.assertEqual(display_policy.collapse_display_rows(failed), failed)

    def test_large_lived_page_never_enters_similarity_matching(self) -> None:
        rows = [
            _row(str(index), "lived prose remains independently visible " * 2,
                 who="agent")
            for index in range(2_000)
        ]
        with mock.patch.object(
                display_policy, "_text_profile",
                side_effect=AssertionError("lived rows reached near-fold")):
            self.assertEqual(display_policy.collapse_display_rows(rows), rows)

    def test_similarity_profiles_sample_bounded_text_for_large_rows(self) -> None:
        prefix = "inspect the same production incident and preserve evidence "
        rows = [
            _row("a", (prefix * 10_000) + "worker token 1111",
                 near_key="large-template"),
            _row("b", (prefix * 10_000) + "worker token 2222",
                 near_key="large-template"),
        ]
        real_sample = display_policy._similarity_sample
        observed: list[int] = []

        def bounded_sample(text):
            sampled = real_sample(text)
            observed.append(len(sampled))
            return sampled

        with mock.patch.object(
                display_policy, "_similarity_sample",
                side_effect=bounded_sample):
            self.assertEqual(
                display_policy.collapse_display_rows(rows), [rows[0]])
        self.assertTrue(observed)
        self.assertLessEqual(
            max(observed), display_policy._SIMILARITY_MAX_CHARS)

    def test_identical_failures_remain_visible_even_with_a_digest(self) -> None:
        rows = [
            _row("a", "same failed output", digest="dead"),
            _row("b", "same failed output", digest="dead"),
        ]
        rows[0]["ok"] = rows[1]["ok"] = False
        self.assertEqual(display_policy.collapse_display_rows(rows), rows)

    def test_exact_digest_collision_still_checks_visible_text(self) -> None:
        rows = [_row("a", "first", digest="dead"),
                _row("b", "second", digest="dead")]
        self.assertEqual(display_policy.collapse_display_rows(rows), rows)

    def test_exact_cross_family_fold_belongs_to_the_caller(self) -> None:
        text = (
            "Identical fixture output can occur in unrelated campaigns; the "
            "upstream exact fold decides whether those copies are hidden."
        )
        rows = [_row("a", text, family="root-a", digest="same"),
                _row("b", text, family="root-b", digest="same")]
        self.assertEqual(display_policy.collapse_display_rows(rows), rows)

    def test_distinct_subagent_event_structure_stays_visible(self) -> None:
        text = "Subagent completed the same bounded investigation with evidence. " * 2
        started = _row("a", text, digest="same")
        started["kind"] = "subagent_start"
        finished = _row("b", text, digest="same")
        finished["kind"] = "subagent_result"
        self.assertEqual(
            display_policy.collapse_display_rows([started, finished]),
            [started, finished],
        )

    def test_meta_provenance_can_fold_even_when_speaker_is_user(self) -> None:
        text = "Fixture repeats the incident symptoms for regression scoring. " * 2
        rows = [_row("a", text, who="user", near_key="fixture-template"),
                _row("b", text + " ", who="user",
                     near_key="fixture-template")]
        for row in rows:
            row["_meta_row"] = True
        self.assertEqual(display_policy.collapse_display_rows(rows), [rows[0]])

    def test_meta_marks_do_not_overwrite_structural_sidechain_origin(self) -> None:
        row = _row("child", "spawned task", who="subagent")
        row["_meta_row"] = True
        self.assertEqual(display_policy.row_origin(row), "sidechain")

    def test_canonical_message_roles_and_event_kinds_have_structural_origins(
            self) -> None:
        self.assertEqual(display_policy.row_origin({"who": "agent"}), "lived")
        self.assertEqual(
            display_policy.row_origin({"who": "subagent"}), "sidechain")
        self.assertEqual(
            display_policy.row_origin({"who": "harness"}), "fixture")
        self.assertEqual(
            display_policy.row_origin({"kind": "tool"}), "tool-output")
        self.assertEqual(
            display_policy.row_origin({"kind": "control"}), "synthetic")
        self.assertEqual(
            display_policy.row_origin({"kind": "subagent_start"}), "sidechain")
        self.assertEqual(
            display_policy.row_origin({"kind": "subagent_result"}), "sidechain")
        # Event provenance wins over a flattened speaker fallback.
        self.assertEqual(
            display_policy.row_origin({
                "who": "tool", "kind": "subagent_result",
            }),
            "sidechain",
        )
        self.assertEqual(
            display_policy.row_origin({
                "who": "tool",
                "event_kind": "tool",
                "kind": "subagent_result",
            }),
            "unknown",
        )

    def test_canonical_events_require_explicit_success_before_near_fold(
            self) -> None:
        text = (
            "Subagent returned the bounded incident analysis with exact "
            "evidence and a preserved proof boundary. "
        )
        unknown = [
            {**_row("a", text + "token 1111",
                    near_key="event-template"),
             "kind": "subagent_result"},
            {**_row("b", text + "token 2222",
                    near_key="event-template"),
             "kind": "subagent_result"},
        ]
        self.assertEqual(
            display_policy.collapse_display_rows(unknown), unknown)
        successful = [{**row, "ok": True} for row in unknown]
        self.assertEqual(
            display_policy.collapse_display_rows(successful),
            [successful[0]],
        )

    def test_malformed_identity_and_conflicting_event_kinds_never_fold(
            self) -> None:
        text = (
            "Sidechain machinery repeats this structured report while "
            "preserving its exact outcome and proof boundary. "
        )
        mutations = (
            {"session": 7},
            {"_family_root": 9},
            {"kind": 3},
            {"event_kind": "tool", "kind": "subagent_result", "ok": True},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                rows = [
                    {**_row("a", text + "token 1111",
                            near_key="event-template"), **mutation},
                    {**_row("b", text + "token 2222",
                            near_key="event-template"), **mutation},
                ]
                self.assertEqual(
                    display_policy.collapse_display_rows(rows), rows)

    def test_malformed_outcome_markers_never_authorize_a_fold(self) -> None:
        text = (
            "Tool result repeats the same structured report while preserving "
            "the exact incident outcome and its verification boundary. "
        )
        for field, value in (
                ("is_error", 1),
                ("is_error", "true"),
                ("status", " failed "),
                ("status", 1),
                ("ok", 1)):
            with self.subTest(field=field, value=value):
                rows = [
                    {**_row("a", text + "token 1111",
                            who="tool", near_key="tool-template"),
                     "kind": "tool", "ok": True, field: value},
                    {**_row("b", text + "token 2222",
                            who="tool", near_key="tool-template"),
                     "kind": "tool", "ok": True, field: value},
                ]
                self.assertEqual(
                    display_policy.collapse_display_rows(rows), rows)

    def test_lineage_fold_adds_to_the_existing_exact_fold_count(self) -> None:
        common = (
            "Fixture dispatch asks a sibling to inspect the same production "
            "incident and return the cause, remedy, and proof boundary."
        )
        representative = _row(
            "a", common + " token 1111", near_key="lineage-template")
        representative["_dup_chats"] = 2
        representative["_dup_sessions"] = ["x", "y"]
        sibling = _row(
            "b", common + " token 2222", near_key="lineage-template")
        kept = display_policy.collapse_display_rows(
            [representative, sibling])
        self.assertEqual(kept, [representative])
        self.assertEqual(representative["_dup_chats"], 3)
        self.assertEqual(
            representative["_dup_sessions"], ["b", "x", "y"])

    def test_lineage_fold_carries_the_hidden_rows_exact_fold_count(self) -> None:
        common = (
            "Fixture dispatch asks a sibling to inspect the same production "
            "incident and return the cause, remedy, and proof boundary."
        )
        representative = _row(
            "a", common + " token 1111", near_key="lineage-template")
        representative["_dup_chats"] = 2
        representative["_dup_sessions"] = ["x", "y"]
        sibling = _row(
            "b", common + " token 2222", near_key="lineage-template")
        sibling["_dup_chats"] = 4
        sibling["_dup_sessions"] = ["c", "d", "e", "f"]
        kept = display_policy.collapse_display_rows(
            [representative, sibling])
        self.assertEqual(kept, [representative])
        # Two already hidden under the representative, plus the sibling and
        # the sibling's four upstream exact copies.
        self.assertEqual(representative["_dup_chats"], 7)

    def test_scalar_upstream_count_refuses_an_unprovable_near_fold(self) -> None:
        text = (
            "Fixture dispatch asks a sibling to inspect the same production "
            "incident and return the cause, remedy, and proof boundary."
        )
        representative = _row(
            "a", text + " token 1111", near_key="lineage-template")
        representative["_dup_chats"] = 1
        sibling = _row(
            "b", text + " token 2222", near_key="lineage-template")
        self.assertEqual(
            display_policy.collapse_display_rows(
                [representative, sibling]),
            [representative, sibling],
        )
        self.assertEqual(representative["_dup_chats"], 1)

    def test_malformed_upstream_counts_refuse_near_fold(self) -> None:
        text = (
            "Fixture dispatch asks a sibling to inspect the same production "
            "incident and return the cause, remedy, and proof boundary."
        )
        for value in (True, "1", -1):
            with self.subTest(value=value):
                representative = _row(
                    "a", text + " token 1111",
                    near_key="lineage-template")
                representative["_dup_chats"] = value
                sibling = _row(
                    "b", text + " token 2222",
                    near_key="lineage-template")
                self.assertEqual(
                    display_policy.collapse_display_rows(
                        [representative, sibling]),
                    [representative, sibling],
                )

    def test_overlapping_exact_lineages_use_set_union_not_arithmetic(
            self) -> None:
        text = (
            "Fixture dispatch asks a sibling to inspect the same production "
            "incident and return the cause, remedy, and proof boundary."
        )
        representative = _row(
            "a", text + " token 1111", near_key="lineage-template")
        representative["_dup_chats"] = 1
        representative["_dup_sessions"] = ["b"]
        sibling = _row(
            "b", text + " token 2222", near_key="lineage-template")
        sibling["_dup_chats"] = 1
        sibling["_dup_sessions"] = ["c"]
        kept = display_policy.collapse_display_rows(
            [representative, sibling])
        self.assertEqual(kept, [representative])
        self.assertEqual(representative["_dup_sessions"], ["b", "c"])
        self.assertEqual(representative["_dup_chats"], 2)

    def test_similarity_work_has_a_hard_per_row_candidate_cap(self) -> None:
        text = (
            "Fixture machinery preserves this deliberately distinct incident "
            "record while exercising the bounded near duplicate matcher. "
        )
        rows = [
            _row(
                str(index), text + f"unique payload {index:04d}",
                near_key="candidate-cap-template",
            )
            for index in range(1_000)
        ]
        calls = 0
        fixed_profile = display_policy._text_profile(text)

        def no_match(_left, _right):
            nonlocal calls
            calls += 1
            return False

        with mock.patch.object(
                display_policy, "_text_profile",
                return_value=fixed_profile), mock.patch.object(
                    display_policy, "_profile_similarity",
                    side_effect=no_match):
            self.assertEqual(
                display_policy.collapse_display_rows(rows), rows)
        self.assertLessEqual(
            calls,
            display_policy._NEAR_CANDIDATE_LIMIT * len(rows),
        )

    def test_near_pair_after_distinct_pressure_still_folds(self) -> None:
        text = (
            "Fixture machinery preserves this deliberately distinct incident "
            "record while exercising the bounded near duplicate matcher. "
        )
        rows = [
            _row(
                str(index), chr(65 + index) * 120,
                near_key="pressure-template",
            )
            for index in range(20)
        ]
        pair = [
            _row("pair-a", text + "worker token 1111",
                 near_key="pressure-template"),
            _row("pair-b", text + "worker token 2222",
                 near_key="pressure-template"),
        ]
        kept = display_policy.collapse_display_rows([*rows, *pair])
        self.assertEqual(kept[:-1], rows)
        self.assertIs(kept[-1], pair[0])
        self.assertEqual(pair[0]["_dup_sessions"], ["pair-b"])

    def test_adjacent_near_pair_survives_a_minhash_band_miss(self) -> None:
        rows = [
            _row("a", ("idqihvl " * 20) + "uqjmmdk",
                 near_key="band-miss-template"),
            _row("b", ("idqihvl " * 20) + "ymliaeo",
                 near_key="band-miss-template"),
        ]
        left = display_policy._text_profile(rows[0]["snippet"])
        right = display_policy._text_profile(rows[1]["snippet"])
        self.assertFalse(set(left.bands) & set(right.bands))
        self.assertTrue(display_policy._profile_similarity(left, right))
        self.assertEqual(
            display_policy.collapse_display_rows(rows),
            [rows[0]],
        )

    def test_band_collisions_cannot_crowd_out_the_adjacent_peer(self) -> None:
        distractors = [
            _row(
                f"d{index}",
                (token + " ") * 20 + "ymliaeo",
                near_key="collision-template",
            )
            for index, token in enumerate(
                ("twqnrlt", "fcciaty", "lbsdxni", "mjmmyxy")
            )
        ]
        pair = [
            _row("a", ("idqihvl " * 20) + "uqjmmdk",
                 near_key="collision-template"),
            _row("b", ("idqihvl " * 20) + "ymliaeo",
                 near_key="collision-template"),
        ]
        self.assertTrue(display_policy._profile_similarity(
            display_policy._text_profile(pair[0]["snippet"]),
            display_policy._text_profile(pair[1]["snippet"]),
        ))
        kept = display_policy.collapse_display_rows(
            [*distractors, *pair])
        self.assertEqual(kept, [*distractors, pair[0]])

    def test_repeated_scaffolding_cannot_hide_distinct_payloads(self) -> None:
        rows = [
            _row(
                "a",
                ("status ok " * 35) + "disk full delete temp files",
                near_key="payload-template-a",
            ),
            _row(
                "b",
                ("status ok " * 35)
                + "auth token expired rotate credentials",
                near_key="payload-template-b",
            ),
        ]
        self.assertEqual(
            display_policy.collapse_display_rows(rows),
            rows,
        )

    def test_diverse_repeated_scaffolding_cannot_outvote_payloads(self) -> None:
        scaffold = (
            "worker search phase dispatch status ok elapsed one rows eight "
        )
        rows = [
            _row(
                "a",
                (scaffold * 6) + "ERROR disk full delete temp files",
                near_key="payload-template-a",
            ),
            _row(
                "b",
                (scaffold * 6)
                + "ERROR auth token expired rotate credentials",
                near_key="payload-template-b",
            ),
        ]
        self.assertEqual(
            display_policy.collapse_display_rows(rows),
            rows,
        )

    def test_long_unequal_suffixes_do_not_phase_shift_the_sample(self) -> None:
        common = (
            "Inspect the exact production incident and preserve every "
            "boundary, cause, remedy, provenance marker, and verification "
            "result before returning evidence. "
        ) * 4
        rows = [
            _row("a", common + "Worker token 1.",
                 near_key="long-template"),
            _row("b", common + "Worker credential abcdefgh.",
                 near_key="long-template"),
        ]
        self.assertGreater(len(rows[0]["snippet"]), 512)
        self.assertEqual(
            display_policy.collapse_display_rows(rows),
            [rows[0]],
        )

    def test_pathological_real_similarity_stays_inside_renderer_budget(
            self) -> None:
        rng = random.Random(991)
        low_alphabet = [
            _row(
                f"low-{index}",
                "".join(rng.choice("abcd") for _ in range(512)),
                near_key="low-alphabet-template",
            )
            for index in range(40)
        ]
        repeated_words = [
            _row(
                f"words-{index}",
                ("alpha beta gamma delta " * 21)
                + f"token-{index:08x}",
                near_key="repeated-template",
            )
            for index in range(40)
        ]
        for rows in (low_alphabet, repeated_words):
            with self.subTest(shape=rows[0]["session"].split("-", 1)[0]):
                started = time.perf_counter()
                display_policy.collapse_display_rows(rows)
                self.assertLess(time.perf_counter() - started, 0.150)

    def test_flattened_tool_rows_wait_for_structural_success_fields(self) -> None:
        text = (
            "subagent task result inspected the same production incident and "
            "returned its exact cause, remedy, and proof boundary."
        )
        unknown = [
            _row("a", text + " token 1111", who="tool",
                 near_key="tool-template"),
            _row("b", text + " token 2222", who="tool",
                 near_key="tool-template"),
        ]
        self.assertEqual(
            display_policy.collapse_display_rows(unknown), unknown)

        successful = [
            {**_row("c", text + " token 3333", who="tool",
                    near_key="tool-template"),
             "kind": "tool", "ok": True},
            {**_row("d", text + " token 4444", who="tool",
                    near_key="tool-template"),
             "kind": "tool", "ok": True},
        ]
        self.assertEqual(
            display_policy.collapse_display_rows(successful),
            [successful[0]],
        )

    def test_structural_failure_fields_and_sidechain_phase_stay_visible(self) -> None:
        text = (
            "Subagent completed the same bounded investigation with exact "
            "evidence and a clear proof boundary."
        )
        failed = [
            {**_row("a", text + " token 1111",
                    near_key="failure-template"), "status": "failed"},
            {**_row("b", text + " token 2222",
                    near_key="failure-template"), "status": "failed"},
        ]
        self.assertEqual(
            display_policy.collapse_display_rows(failed), failed)

        spawn = _row(
            "c", text + " token 3333", near_key="phase-template")
        body = _row(
            "d", text + " token 4444", near_key="phase-template")
        body["turn"] = 4
        self.assertEqual(
            display_policy.collapse_display_rows([spawn, body]),
            [spawn, body],
        )


class ToolOutputTests(unittest.TestCase):
    def test_selected_output_preview_is_centered_on_the_proven_match(self) -> None:
        output = (
            "routine preface " * 15
            + "RARE_TOOL_ONLY_E930_X7F91 decisive failure"
            + " routine trailer" * 15
        )
        start = output.index("RARE_TOOL_ONLY_E930_X7F91")
        preview = display_policy.tool_output_preview(
            {"output": output},
            match_span=(start, start + len("RARE_TOOL_ONLY_E930_X7F91")),
        )
        self.assertIn("RARE_TOOL_ONLY_E930_X7F91", preview.text)
        self.assertTrue(preview.text.startswith("…"))
        self.assertTrue(preview.text.endswith("…"))
        self.assertLessEqual(len(preview.text), 160)
        self.assertEqual(preview.source_bytes, len(output.encode("utf-8")))
        self.assertTrue(preview.truncated)

    def test_first_meaningful_output_line_precedes_byte_accounting(self) -> None:
        prefix = (
            "\n\n   \nTo github.com:private/agrep.git\n"
            "   6fbeae9..abc1234  master -> master\n"
        )
        excerpt = prefix + ("x" * (800 - len(prefix))) + "…"
        event = {
            "output": excerpt,
            "output_chars": 900,
            "output_bytes": 900,
            "output_truncated": True,
        }
        preview = display_policy.tool_output_preview(event)
        self.assertEqual(preview.text, "To github.com:private/agrep.git")
        self.assertEqual(preview.source_bytes, 900)
        self.assertEqual(preview.source_chars, 900)
        self.assertTrue(preview.truncated)

    def test_empty_output_has_no_preview_but_keeps_source_count(self) -> None:
        preview = display_policy.tool_output_preview(
            {"output": "\n \t", "output_chars": 33})
        self.assertEqual(preview.text, "")
        self.assertEqual(preview.source_chars, 33)
        self.assertIsNone(preview.source_bytes)

    def test_whitespace_normalization_alone_is_not_claimed_as_truncation(self) -> None:
        preview = display_policy.tool_output_preview(
            {"output": "master   ->   master"})
        self.assertEqual(preview.text, "master -> master")
        self.assertEqual(preview.source_bytes, 20)
        self.assertFalse(preview.truncated)

    def test_truncated_unicode_uses_original_source_bytes(self) -> None:
        excerpt = ("é" * 800) + "…"
        preview = display_policy.tool_output_preview({
            "output": excerpt,
            "output_chars": 900,
            "output_bytes": 1800,
            "output_truncated": True,
        }, max_chars=20)
        self.assertEqual(preview.source_bytes, 1800)
        self.assertNotEqual(preview.source_bytes, len(excerpt.encode("utf-8")))
        self.assertTrue(preview.truncated)

    def test_legacy_truncated_excerpt_does_not_invent_source_bytes(self) -> None:
        preview = display_policy.tool_output_preview({
            "output": ("é" * 800) + "…",
            "output_chars": 900,
            "output_truncated": True,
        })
        self.assertIsNone(preview.source_bytes)

    def test_stale_small_counts_never_undercount_the_stored_preview(self) -> None:
        preview = display_policy.tool_output_preview({
            "output": "abcdef",
            "output_chars": 1,
            "output_bytes": 0,
        })
        self.assertEqual(preview.source_chars, 6)
        self.assertEqual(preview.source_bytes, 6)
        self.assertFalse(preview.truncated)

    def test_malformed_counts_fail_closed_without_raising(self) -> None:
        for value in (-1, True, 1.5, float("inf"), "900"):
            with self.subTest(value=value):
                preview = display_policy.tool_output_preview({
                    "output": "é",
                    "output_chars": value,
                    "output_bytes": value,
                })
                self.assertEqual(preview.source_chars, 1)
                self.assertEqual(preview.source_bytes, 2)
                self.assertFalse(preview.truncated)

    def test_invalid_byte_count_stays_unknown_when_excerpt_is_truncated(
            self) -> None:
        preview = display_policy.tool_output_preview({
            "output": "partial",
            "output_chars": 90,
            "output_bytes": 0,
            "output_truncated": True,
        })
        self.assertEqual(preview.source_chars, 90)
        self.assertIsNone(preview.source_bytes)
        self.assertTrue(preview.truncated)

    def test_declared_bytes_cannot_undercount_declared_source_chars(
            self) -> None:
        for byte_count in (7, 50):
            with self.subTest(byte_count=byte_count):
                preview = display_policy.tool_output_preview({
                    "output": "partial",
                    "output_chars": 90,
                    "output_bytes": byte_count,
                })
                self.assertEqual(preview.source_chars, 90)
                self.assertIsNone(preview.source_bytes)
                self.assertTrue(preview.truncated)

    def test_declared_bytes_obey_utf8_and_retained_excerpt_bounds(
            self) -> None:
        too_small = display_policy.tool_output_preview({
            "output": "é",
            "output_chars": 10,
            "output_bytes": 10,
        })
        self.assertIsNone(too_small.source_bytes)
        self.assertTrue(too_small.truncated)

        same_chars = display_policy.tool_output_preview({
            "output": "abc",
            "output_chars": 3,
            "output_bytes": 100,
        })
        self.assertEqual(same_chars.source_bytes, 3)
        self.assertFalse(same_chars.truncated)

        empty = display_policy.tool_output_preview({
            "output": "",
            "output_chars": 0,
            "output_bytes": 7,
        })
        self.assertEqual(empty.source_bytes, 0)
        self.assertFalse(empty.truncated)

        one_replaced_character = display_policy.tool_output_preview({
            "output": ("x" * 800) + "…",
            "output_chars": 801,
            "output_bytes": 801,
            "output_truncated": True,
        })
        self.assertEqual(one_replaced_character.source_bytes, 801)
        self.assertTrue(one_replaced_character.truncated)

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
                preview = display_policy.tool_output_preview(event)
                self.assertIsNone(preview.source_bytes)
                self.assertTrue(preview.truncated)

    def test_unpaired_unicode_keeps_source_bytes_unknown_without_raising(
            self) -> None:
        for output in ("\ud800", "ok\udcfftail"):
            with self.subTest(output=repr(output)):
                preview = display_policy.tool_output_preview({
                    "output": output,
                    "output_chars": len(output),
                    "output_bytes": len(output),
                })
                self.assertIsNone(preview.source_bytes)
                self.assertEqual(preview.text, "")
                self.assertTrue(preview.truncated)

    def test_non_string_output_fails_closed_without_stringification(
            self) -> None:
        for output in (10 ** 5000, True, {"forged": "output"}):
            with self.subTest(output=type(output).__name__):
                preview = display_policy.tool_output_preview({
                    "output": output,
                    "output_chars": 10,
                    "output_bytes": 10,
                })
                self.assertEqual(
                    preview,
                    display_policy.ToolOutputPreview("", None, 0, False),
                )

    def test_malformed_preview_caps_use_a_bounded_safe_default(self) -> None:
        output = "x" * 5000
        for cap in (None, float("inf"), float("nan"), "bogus",
                    True, 2.9, "3"):
            with self.subTest(cap=cap):
                preview = display_policy.tool_output_preview(
                    {"output": output}, max_chars=cap)
                self.assertEqual(len(preview.text), 160)
                self.assertTrue(preview.truncated)
        self.assertEqual(
            len(display_policy.tool_output_preview(
                {"output": output}, max_chars=10 ** 5000).text),
            display_policy._TOOL_PREVIEW_HARD_MAX,
        )


class PayloadSnippetTests(unittest.TestCase):
    def test_match_including_opening_quote_keeps_valid_local_span(self) -> None:
        text = '{"error":"needle survives"}'
        start = text.index('"needle')
        snippet = display_policy.payload_snip_at(
            text, start, start + len('"needle'))
        self.assertIn('"needle survives', snippet)
        self.assertNotIn("error", snippet)

    def test_json_value_drops_scaffolding_before_payload(self) -> None:
        payload = "Error: No space left on device while recording rollout items"
        text = '{"metadata":"' + "x" * 220 + '","last_err":"' + payload + '"}'
        start = text.index("No space")
        snippet = display_policy.payload_snip_at(
            text, start, start + len("No space"), pad=18)
        self.assertIn("No space left on device", snippet)
        self.assertNotIn("metadata", snippet)
        self.assertNotIn("last_err", snippet)
        self.assertTrue(snippet.startswith("…"))
        self.assertTrue(snippet.endswith("…"))

    def test_payload_window_shifts_to_keep_post_match_evidence(self) -> None:
        payload = "No space left on device; detach image, chmod tree, free 20GB"
        text = '{"error":"' + payload + '"}'
        start = text.index("No space")
        snippet = display_policy.payload_snip_at(
            text, start, start + len("No space"), pad=24)
        self.assertIn("detach image", snippet)
        self.assertNotIn('{"error"', snippet)

    def test_short_discriminator_match_keeps_richer_following_payload(self) -> None:
        payload = "Error: No space left on device while recording rollout items"
        text = (
            '{"kind":"ENOSPC","metadata":"' + "x" * 300
            + '","last_err":"' + payload + '"}'
        )
        start = text.index("ENOSPC")
        snippet = display_policy.payload_snip_at(
            text, start, start + len("ENOSPC"), pad=48)
        self.assertIn("ENOSPC", snippet)
        self.assertIn("No space left on device", snippet)
        self.assertNotIn("metadata", snippet)
        self.assertNotIn("last_err", snippet)
        self.assertNotIn("x" * 20, snippet)

    def test_terminal_quoted_fallback_skips_payload_leading_whitespace(
            self) -> None:
        text = (
            '{"kind":"EFAIL","payload":"'
            + (" " * 220)
            + 'DECISIVE_REMEDY restart child"}'
        )
        start = text.index("EFAIL")
        snippet = display_policy.payload_snip_at(
            text, start, start + len("EFAIL"))
        self.assertIn("EFAIL", snippet)
        self.assertIn("DECISIVE_REMEDY", snippet)
        self.assertNotIn("payload", snippet)

    def test_verbose_metadata_does_not_outrank_terminal_error_payload(
            self) -> None:
        metadata = (
            "capture host platform package revision timestamps feature flags "
            "transport retries and every other verbose diagnostic attribute"
        )
        payload = "Error: No space left on device"
        text = (
            '{"kind":"ENOSPC","metadata":"' + metadata
            + '","last_err":"' + payload + '"}'
        )
        start = text.index("ENOSPC")
        snippet = display_policy.payload_snip_at(
            text, start, start + len("ENOSPC"), pad=48)
        self.assertIn("ENOSPC", snippet)
        self.assertIn("No space left on device", snippet)
        self.assertNotIn("capture host platform", snippet)

    def test_explicit_structural_payload_span_beats_trailing_metadata(
            self) -> None:
        payload = "Error: permission denied while opening the audit book"
        trailing = "verbose trailing metadata words that are not the payload"
        text = (
            '{"kind":"EACCES","last_err":"' + payload
            + '","metadata":"' + trailing + '"}'
        )
        match_start = text.index("EACCES")
        payload_start = text.index(payload)
        snippet = display_policy.payload_snip_at(
            text,
            match_start,
            match_start + len("EACCES"),
            pad=48,
            payload_bounds=(payload_start, payload_start + len(payload)),
        )
        self.assertIn("permission denied", snippet)
        self.assertNotIn("verbose trailing metadata", snippet)

    def test_explicit_payload_span_does_not_parse_unrelated_quotes(self) -> None:
        text = 'Read: {"note":"needle"}\nexact output'
        start = text.index("needle")
        output = text.index("exact output")
        with mock.patch.object(
                display_policy, "_quoted_value_bounds",
                side_effect=AssertionError("quoted scan ran")):
            snippet = display_policy.payload_snip_at(
                text, start, start + len("needle"),
                payload_bounds=(output, len(text)))
        self.assertEqual(snippet, "…needle · exact output")

    def test_explicit_payload_span_is_used_when_match_is_a_json_key(
            self) -> None:
        payload = "Error: No space left on device"
        text = (
            '{"kind_name":"ENOSPC","metadata":"' + "x" * 220
            + '","last_err":"' + payload + '"}'
        )
        match_start = text.index("kind_name")
        payload_start = text.index(payload)
        snippet = display_policy.payload_snip_at(
            text,
            match_start,
            match_start + len("kind_name"),
            pad=36,
            payload_bounds=(payload_start, payload_start + len(payload)),
        )
        self.assertIn("kind_name", snippet)
        self.assertIn("No space left on device", snippet)
        self.assertNotIn("x" * 20, snippet)

    def test_explicit_payload_span_is_used_for_a_long_discriminator(
            self) -> None:
        discriminator = "E" * 180
        payload = "Error: source evidence changed during audit"
        text = (
            '{"kind":"' + discriminator
            + '","last_err":"' + payload + '"}'
        )
        match_start = text.index(discriminator)
        payload_start = text.index(payload)
        snippet = display_policy.payload_snip_at(
            text,
            match_start,
            match_start + 8,
            pad=32,
            payload_bounds=(payload_start, payload_start + len(payload)),
        )
        self.assertIn("EEEEEEEE", snippet)
        self.assertIn("source evidence changed", snippet)

    def test_explicit_unquoted_tool_output_span_beats_long_input(
            self) -> None:
        payload = "Error: decisive child-process failure"
        text = "Bash: " + ("x" * 220) + "\n" + payload
        match_start = text.index("Bash")
        payload_start = text.index(payload)
        snippet = display_policy.payload_snip_at(
            text,
            match_start,
            match_start + len("Bash"),
            pad=36,
            payload_bounds=(payload_start, payload_start + len(payload)),
        )
        self.assertIn("Bash", snippet)
        self.assertIn(payload, snippet)
        self.assertNotIn("x" * 20, snippet)

    def test_match_inside_unquoted_output_drops_long_tool_input(self) -> None:
        payload = (
            "prelude " + ("y" * 120)
            + " DECISIVE_REMEDY rebuild exact artifact"
        )
        text = "Bash: " + ("x" * 300) + "\n" + payload
        payload_start = text.index(payload)
        match_start = text.index("DECISIVE_REMEDY")
        snippet = display_policy.payload_snip_at(
            text,
            match_start,
            match_start + len("DECISIVE_REMEDY"),
            pad=36,
            payload_bounds=(payload_start, len(text)),
        )
        self.assertIn("DECISIVE_REMEDY", snippet)
        self.assertIn("rebuild exact artifact", snippet)
        self.assertNotIn("Bash", snippet)
        self.assertNotIn("x" * 20, snippet)

    def test_explicit_unquoted_span_skips_leading_blank_output_lines(
            self) -> None:
        payload = ("\n   " * 220) + "\nDECISIVE_REMEDY: restart child"
        text = "MATCH: long tool input\n" + payload
        match_start = text.index("MATCH")
        payload_start = text.index("\n", match_start) + 1
        snippet = display_policy.payload_snip_at(
            text,
            match_start,
            match_start + len("MATCH"),
            payload_bounds=(payload_start, len(text)),
        )
        self.assertIn("MATCH", snippet)
        self.assertIn("DECISIVE_REMEDY", snippet)

    def test_match_on_escape_keeps_the_complete_quoted_payload(
            self) -> None:
        payload = (
            '\\"needle marks the failure; retain the rest of this payload '
            "until DECISIVE_REMEDY: rebuild the exact artifact"
        )
        text = '{"metadata":"' + ("x" * 300) + '","payload":"' + payload + '"}'
        match_start = text.index('\\"needle')
        snippet = display_policy.payload_snip_at(
            text,
            match_start,
            match_start + len('\\"needle'),
        )
        self.assertIn("needle", snippet)
        self.assertIn("DECISIVE_REMEDY", snippet)
        self.assertNotIn("metadata", snippet)
        self.assertNotIn("x" * 20, snippet)

    def test_direct_quoted_match_backfills_discarded_leading_whitespace(
            self) -> None:
        payload = (
            (" " * 160)
            + "MATCH "
            + ("y" * 95)
            + " DECISIVE_REMEDY rotate credentials"
        )
        text = (
            '{"metadata":"' + ("x" * 300)
            + '","payload":"' + payload + '"}'
        )
        match_start = text.index("MATCH")
        snippet = display_policy.payload_snip_at(
            text, match_start, match_start + len("MATCH"))
        self.assertIn("MATCH", snippet)
        self.assertIn("DECISIVE_REMEDY", snippet)
        self.assertNotIn("metadata", snippet)
        self.assertNotIn("x" * 20, snippet)

    def test_explicit_payload_span_must_be_nonempty_and_follow_match(
            self) -> None:
        text = "tool: input\noutput"
        match_start = text.index("tool")
        output_start = text.index("output")
        for bounds in (
                (match_start + 1, output_start),
                (output_start, output_start),
                [output_start, len(text)],
                (output_start, len(text) + 1)):
            with self.subTest(bounds=bounds):
                with self.assertRaises(ValueError):
                    display_policy.payload_snip_at(
                        text,
                        match_start,
                        match_start + len("tool"),
                        payload_bounds=bounds,
                    )

    def test_plain_prose_keeps_the_normal_centered_window(self) -> None:
        text = "a" * 40 + "needle" + "b" * 40
        self.assertEqual(
            display_policy.payload_snip_at(text, 40, 46, pad=10),
            "…" + "a" * 10 + "needle" + "b" * 10 + "…",
        )


class SemanticEdgeTests(unittest.TestCase):
    def test_warmup_and_partial_coverage_are_explicit(self) -> None:
        self.assertEqual(
            display_policy.semantic_warming_line(),
            "semantic warming — keyword results below",
        )
        self.assertEqual(
            display_policy.semantic_warming_line(7.6),
            "semantic warming (~8s) — keyword results below",
        )
        self.assertEqual(
            display_policy.semantic_warming_line(float("nan")),
            "semantic warming — keyword results below",
        )
        self.assertEqual(
            display_policy.semantic_warming_line(10 ** 1000),
            "semantic warming — keyword results below",
        )
        self.assertEqual(
            display_policy.semantic_warming_line(-8),
            "semantic warming — keyword results below",
        )
        self.assertEqual(
            display_policy.semantic_warming_line(0),
            "semantic warming — keyword results below",
        )
        self.assertEqual(
            display_policy.semantic_warming_line(True),
            "semantic warming — keyword results below",
        )
        self.assertEqual(
            display_policy.semantic_warming_line(0.4),
            "semantic warming (~1s) — keyword results below",
        )
        coverage = {
            "indexed": 25, "total": 100, "pending": 75,
            "complete": False,
        }
        self.assertEqual(
            display_policy.semantic_coverage_line(coverage),
            "semantic: searched 25/100 embedded rows (25%)",
        )
        self.assertIsNone(display_policy.semantic_coverage_line(
            {"indexed": 100, "total": 100, "pending": 0, "complete": True}))
        self.assertEqual(
            display_policy.semantic_coverage_line(
                {"indexed": 99, "total": 100, "pending": 1,
                 "complete": False}),
            "semantic: searched 99/100 embedded rows (99%)",
        )
        self.assertEqual(
            display_policy.semantic_coverage_line(
                {"indexed": 999, "total": 1000, "pending": 1,
                 "complete": False}),
            "semantic: searched 999/1000 embedded rows (99%)",
        )

    def test_invalid_semantic_coverage_fails_closed_without_raising(self) -> None:
        unavailable = (
            "semantic: embedding coverage unavailable; "
            "searched scope is not verified"
        )
        for coverage in (
                None,
                {},
                {"indexed": 25, "total": 10, "complete": False},
                {"indexed": "25", "total": 100, "complete": False},
                {"indexed": 25, "total": None, "complete": False},
                {"indexed": 25, "total": 100, "complete": "yes"},
                {"indexed": 25, "total": 25, "complete": False},
                {"indexed": 0, "total": 0, "complete": False},
                {"indexed": 3, "total": 3, "pending": 7,
                 "complete": True},
                {"indexed": 3, "total": 10, "pending": 0,
                 "complete": False},
                {"indexed": 3, "total": 10, "pending": True,
                 "complete": False},
                {"indexed": 3, "total": 10, "pending": 7,
                 "complete": True},
                {"indexed": 10 ** 5000, "total": 10 ** 5000 + 1,
                 "pending": 1, "complete": False}):
            with self.subTest(coverage=coverage):
                self.assertEqual(
                    display_policy.semantic_coverage_line(coverage),
                    unavailable,
                )
                self.assertEqual(
                    display_policy.semantic_empty_line(coverage),
                    "semantic: no match; embedding coverage is unavailable",
                )

    def test_empty_semantic_result_distinguishes_unembedded_rows(self) -> None:
        self.assertEqual(
            display_policy.semantic_empty_line(
                {"indexed": 25, "total": 100, "pending": 75,
                 "complete": False}),
            "semantic: no match in the 25 embedded rows searched; "
            "75 rows are not embedded yet",
        )
        self.assertEqual(
            display_policy.semantic_empty_line(
                {"indexed": 100, "total": 100, "pending": 0,
                 "complete": True}),
            "semantic: no match among 100 embedded rows",
        )

    def test_keyword_miss_names_corpus_size(self) -> None:
        self.assertEqual(
            display_policy.keyword_empty_line(17_352),
            "keyword: 0 matching rows across 17,352 indexed messages",
        )
        for invalid in (None, -1, True, 1.5, "12"):
            with self.subTest(invalid=invalid):
                self.assertEqual(
                    display_policy.keyword_empty_line(invalid),
                    "keyword: no match; indexed corpus size is unavailable",
                )
        self.assertEqual(
            display_policy.keyword_empty_line(10 ** 5000),
            "keyword: no match; indexed corpus size is unavailable",
        )

    def test_every_matcher_owns_a_zero_line(self) -> None:
        # E3: -w/-E stated their zero on no channel unless stderr was a tty
        for mode, label in (("word", "word"), ("regex", "regex")):
            with self.subTest(mode=mode):
                line = display_policy.matcher_empty_line(mode, 17_352)
                self.assertEqual(
                    line,
                    f"{label}: 0 matching rows across 17,352 indexed messages"
                    " - `-s` runs semantic search")
                self.assertIn(
                    "`-s` runs semantic search",
                    display_policy.matcher_empty_line(mode, None))
        self.assertEqual(display_policy.matcher_empty_line("keyword", 17_352),
                         display_policy.keyword_empty_line(17_352))
        self.assertNotIn("`-s`", display_policy.keyword_empty_line(17_352))

    def test_probe_miss_is_not_silent_and_pointer_is_hedged(self) -> None:
        miss = display_policy.probe_miss_line(
            "keyword", corpus_sessions=143, semantic_warming=True)
        self.assertIn("no confident past-context pointer", miss)
        self.assertIn("searched 143 past session(s)", miss)
        self.assertIn("semantic model warming", miss)
        row = {"who": "subagent", "_meta_row": True}
        label = display_policy.probe_pointer_label(
            row, semantic=True, weak=False)
        self.assertEqual(
            label,
            "top candidate (semantic evidence; provenance: "
            "sidechain/subagent, ~meta)",
        )
        self.assertNotIn("semantic match", label)
        self.assertEqual(
            display_policy.probe_pointer_label(
                {"who": "user"}, semantic=False, weak=True),
            "top candidate (weak prose evidence; provenance: lived/user)",
        )
        self.assertEqual(
            display_policy.probe_pointer_label(
                {}, semantic=False, weak=False),
            "top candidate (prose evidence; provenance: unknown/unknown)",
        )
        self.assertEqual(
            display_policy.probe_pointer_label(
                {"who": "mystery"}, semantic=True, weak=True),
            "top candidate (weak semantic evidence; "
            "provenance: unknown/mystery)",
        )
        self.assertEqual(
            display_policy.probe_pointer_label(
                {"who": "tool", "kind": "subagent_result", "ok": True},
                semantic=True),
            "top candidate (semantic evidence; provenance: "
            "sidechain/subagent_result)",
        )
        self.assertEqual(
            display_policy.probe_pointer_label(
                {"who": "user", "event_kind": "mystery", "kind": "tool"},
                semantic=True),
            "top candidate (semantic evidence; provenance: tool-output/tool)",
        )
        self.assertEqual(
            display_policy.probe_pointer_label(
                {"who": "user"}, semantic="false"),
            "top candidate (unverified evidence; provenance: lived/user)",
        )
        self.assertEqual(
            display_policy.probe_pointer_label(
                {"who": "user"}, semantic=True, weak="true"),
            "top candidate (weak semantic evidence; provenance: lived/user)",
        )
        self.assertEqual(
            display_policy.probe_pointer_label(
                {"who": "user", "_meta_row": "false"},
                semantic=False),
            "top candidate (prose evidence; provenance: lived/user)",
        )
        self.assertEqual(
            display_policy.probe_pointer_label(
                {"who": "user\nforged"}, semantic=False),
            "top candidate (prose evidence; provenance: unknown/unknown)",
        )
        self.assertEqual(
            display_policy.probe_miss_line(
                "keyword\nFORGED", corpus_sessions=143),
            "recall: no confident past-context pointer "
            "(unknown engine; searched 143 past session(s))",
        )
        huge = 10 ** 5000
        self.assertEqual(
            display_policy.probe_pointer_label(
                {"who": huge}, semantic=False),
            "top candidate (prose evidence; provenance: unknown/unknown)",
        )
        self.assertLess(
            len(display_policy.probe_pointer_label(
                {"who": "x" * 1_000_000}, semantic=False)),
            100,
        )
        self.assertEqual(
            display_policy.probe_miss_line(
                "x" * 1_000_000, corpus_sessions=huge),
            "recall: no confident past-context pointer "
            "(unknown engine; corpus session count unavailable)",
        )
        for invalid in (None, -1, True, 1.5, "12"):
            with self.subTest(invalid=invalid):
                self.assertEqual(
                    display_policy.probe_miss_line(
                        "corpusdb", corpus_sessions=invalid),
                    "recall: no confident past-context pointer "
                    "(corpusdb; corpus session count unavailable)",
                )


if __name__ == "__main__":
    unittest.main()
