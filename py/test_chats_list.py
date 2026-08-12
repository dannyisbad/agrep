"""`agrep chats` - find chats by identity or indexed conversation content.

Bare listing is newest-first; patterned lookup ranks content hits and opens the
matching turn. Each row's @prefix pastes into around/--chat. Side chats stay
hidden unless --side or the user names their id directly.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "py"))

from _test_support import isolate_data_dir, publish_derived_generation  # noqa: E402

isolate_data_dir()
import around  # noqa: E402
import cli  # noqa: E402
import common  # noqa: E402
import corpusdb  # noqa: E402
import explore  # noqa: E402
import indexd_runtime  # noqa: E402
import livetui  # noqa: E402
import search  # noqa: E402

SESSIONS = (
    {"session": "0199aaaa-1111-7000-8000-000000000001", "agent": "codex",
     "project": "/home/u/webapp", "n": 12, "first_ts": 1000,
     "last_ts": 9000, "first_text": "fix the login race condition",
     "parent": ""},
    {"session": "0199bbbb-2222-7000-8000-000000000002", "agent": "claude",
     "project": "/home/u/apartment-hunt", "n": 4, "first_ts": 2000,
     "last_ts": 8000, "first_text": "find me a two bedroom in sf",
     "parent": ""},
    {"session": "agent-a0000000000000001", "agent": "claude",
     "project": "/home/u/webapp", "n": 2, "first_ts": 3000,
     "last_ts": 7000, "first_text": "spawned side task",
     "parent": "0199aaaa-1111-7000-8000-000000000001"},
)


MESSAGES = tuple(
    {"session": "0199aaaa-1111-7000-8000-000000000001", "turn": turn,
     "who": "user", "text": f"turn {turn} about the login race",
     "reply": f"answer {turn}", "agent": "codex", "project": "/home/u/webapp",
     "ts": 1000 + turn}
    for turn in range(6))


def _write_fixture() -> None:
    with (common.DATA_DIR / "sessions.jsonl").open(
            "w", encoding="utf-8") as f:
        for row in SESSIONS:
            f.write(json.dumps(row) + "\n")
    common.MESSAGES_PATH.touch()
    explore._freshen()


def _write_paste_fixture() -> None:
    """The handle round-trip needs real turns to open, where the other
    fixtures deliberately serve an empty messages.jsonl."""
    publish_derived_generation(
        common.DATA_DIR, list(MESSAGES), common, corpusdb,
        signature="chats-handle-paste")
    explore._freshen()


def _run(argv):
    stdout, stderr = io.StringIO(), io.StringIO()
    with mock.patch.object(search.indexd_runtime, "ensure_index",
                           return_value=True), \
            contextlib.redirect_stdout(stdout), \
            contextlib.redirect_stderr(stderr):
        rc = search.chats_main(argv)
    return rc, stdout.getvalue(), stderr.getvalue()


class ChatsListTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # The isolated data dir is shared by every module in one discovery
        # process: restore whatever was published there when this suite ends.
        cls._prior = {}
        for name in ("sessions.jsonl", "messages.jsonl"):
            path = common.DATA_DIR / name
            cls._prior[name] = path.read_bytes() if path.exists() else None
        _write_fixture()

    @classmethod
    def tearDownClass(cls) -> None:
        for name, body in cls._prior.items():
            path = common.DATA_DIR / name
            if body is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(body)
        explore._freshen()

    def test_bare_listing_is_newest_first_without_side_chats(self) -> None:
        rc, out, err = _run([])
        self.assertEqual(rc, 0)
        lines = out.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("@0199aaaa", lines[0])  # last_ts 9000 first
        self.assertIn("@0199bbbb", lines[1])
        self.assertNotIn("side task", out)
        self.assertIn("showing 2 of 2 matching chats", err)
        self.assertIn("--side", err)

    def test_explorer_recent_chats_share_the_main_chat_policy(self) -> None:
        payload = explore.list_chats(10)
        self.assertEqual(payload["total"], 2)
        self.assertEqual(
            [row["session"] for row in payload["chats"]],
            [SESSIONS[0]["session"], SESSIONS[1]["session"]])
        with_side = explore.list_chats(10, include_side=True)
        self.assertEqual(with_side["total"], 3)
        self.assertTrue(any(row["side"] for row in with_side["chats"]))
        self.assertTrue(explore._indexed_chat_is_side({
            "session": "uuid-with-missing-parent",
            "first_text": "[subagent task] legacy cache row",
        }))

    def test_side_flag_includes_and_labels_side_chats(self) -> None:
        rc, out, _err = _run(["--side"])
        self.assertEqual(rc, 0)
        self.assertIn("side task", out)
        self.assertIn("[side chat]", out)

    def test_cli_uses_the_shared_legacy_side_classifier(self) -> None:
        legacy = {
            "session": "legacy-child", "agent": "claude",
            "project": "/work", "n": 1, "first_ts": 1, "last_ts": 10_000,
            "first_text": "[subagent task] inspect the cache", "parent": "",
        }
        indexed = {row["session"]: row for row in SESSIONS}
        with mock.patch.object(
                explore, "_session_index",
                return_value={**indexed, legacy["session"]: legacy}):
            rc, out, err = _run([])
            self.assertEqual(rc, 0)
            self.assertNotIn("legacy-child", out)
            self.assertIn("side chats hidden", err)
            rc, out, _err = _run(["--side"])
            self.assertEqual(rc, 0)
            self.assertIn("legacy-child", out)
            self.assertIn("[side chat]", out)

    def test_direct_side_chat_id_is_not_hidden(self) -> None:
        for target in ("agent-a0", "@agent-a0"):
            with self.subTest(target=target):
                rc, out, err = _run([target])
                self.assertEqual(rc, 0)
                self.assertEqual(len(out.splitlines()), 1)
                self.assertIn("side task", out)
                self.assertIn("[side chat]", out)
                self.assertNotIn("side chats hidden", err)

    def test_bare_larger_page_command_has_no_dangling_separator(self) -> None:
        _rc, _out, err = _run(["-n", "1"])
        larger = err.split("larger page: ", 1)[1]
        self.assertFalse(larger.rstrip().endswith("--"))

    def test_larger_page_preserves_no_auto_intent(self) -> None:
        _rc, _out, err = _run(["-n", "1", "--no-auto"])
        larger = err.split("larger page: ", 1)[1]
        self.assertIn("--no-auto", larger)

    def test_pattern_matches_project_opening_line_and_id_prefix(self) -> None:
        for pattern, expected in (
                ("apartment", "@0199bbbb"),      # project directory
                ("login race", "@0199aaaa"),     # opening message
                ("0199bbbb", "@0199bbbb"),       # session id prefix
        ):
            rc, out, _err = _run([pattern])
            self.assertEqual(rc, 0, pattern)
            self.assertEqual(len(out.splitlines()), 1, pattern)
            self.assertIn(expected, out, pattern)

    def test_agent_filter_and_row_cap(self) -> None:
        rc, out, _err = _run(["--agent", "codex"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(out.splitlines()), 1)
        rc, out, _err = _run(["-n", "1"])
        self.assertEqual(len(out.splitlines()), 1)
        self.assertIn("@0199aaaa", out)

    def test_rows_carry_age_and_turn_count(self) -> None:
        _rc, out, _err = _run(["webapp"])
        self.assertIn("12t", out)

    def test_proven_no_match_exits_1_with_the_count_line(self) -> None:
        with mock.patch.object(
                search, "_chat_content_heads", return_value=({}, True)), \
                mock.patch.object(
                    indexd_runtime, "freshness_story",
                    return_value=search.surface.FreshnessStory("current")):
            rc, out, err = _run(["zz-nothing-matches"])
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("showing 0 of 0 matching chats", err)

    def test_json_rows_are_machine_shaped(self) -> None:
        rc, out, err = _run(["--json", "--side"])
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        lines = [json.loads(line) for line in out.splitlines()]
        # OUTPUT_CONTRACTS: chats JSON uses the one-envelope page shape - the
        # leading agrep-meta record carries page state once, rows follow.
        self.assertEqual(lines[0]["kind"], "agrep-meta")
        rows = lines[1:]
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["session"],
                         "0199aaaa-1111-7000-8000-000000000001")
        self.assertTrue(any(row["side"] for row in rows))
        # freshness is page state: it lives once in the envelope, never per row
        self.assertIn("freshness", lines[0])
        for row in rows:
            for key in ("session", "agent", "project", "turns", "last_ts",
                        "first_text"):
                self.assertIn(key, row)
            self.assertNotIn("freshness", row)

    def test_no_auto_json_miss_is_unverified_and_quiet(self) -> None:
        story = search.surface.FreshnessStory(
            "unverified", code="freshness-unchecked",
            detail=indexd_runtime.NO_AUTO_REFRESH_REASON)
        unchecked = {
            "state": "unchecked", "failing": False,
            "checked": False, "may_be_stale": True,
        }
        with mock.patch.object(
                indexd_runtime, "freshness_story", return_value=story), \
                mock.patch.object(
                    indexd_runtime, "machine_freshness",
                    return_value=unchecked):
            rc, out, err = _run([
                "zz-nothing-matches", "--json", "--no-auto"])
        self.assertEqual(rc, 2)
        self.assertEqual(err, "")
        payload = json.loads(out)
        self.assertEqual(payload["kind"], "agrep-meta")
        self.assertFalse(payload["freshness"]["checked"])

    def test_no_auto_miss_is_unverified_in_a_fresh_process(self) -> None:
        rows = [{
            "session": "11111111-1111-4111-8111-111111111111",
            "turn": 0, "who": "user", "text": "fixture chat alpha",
            "agent": "codex", "project": "fixture", "ts": 1000,
        }]
        with tempfile.TemporaryDirectory(prefix="agrep-chats-process-") as raw:
            root = Path(raw)
            data = root / "data"
            home = root / "home"
            home.mkdir()
            publish_derived_generation(
                data, rows, common, corpusdb, signature="chats-process")
            env = os.environ.copy()
            env.update({
                "AGREP_DATA_DIR": str(data),
                "AGREP_DATA_DIR_SOURCE": "test",
                "AGREP_HOME": str(home),
                "AGREP_NO_DAEMON": "1",
                "AGREP_NO_FETCH": "1",
                "NO_COLOR": "1",
                "TERM": "dumb",
            })
            env.pop("AGREP_PROFILE", None)

            def run(*args: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [sys.executable, str(ROOT / "cli.py"), *args],
                    env=env, capture_output=True, text=True, timeout=10,
                    check=False)

            human = run("chats", "definite-miss", "--no-auto")
            self.assertEqual(human.returncode, 2, human.stderr)
            self.assertEqual(human.stdout, "")
            self.assertIn("showing 0 of 0 matching chats", human.stderr)
            self.assertEqual(human.stderr.count("history may be stale:"), 1)

            machine = run(
                "chats", "definite-miss", "--json", "--no-auto")
            self.assertEqual(machine.returncode, 2, machine.stderr)
            self.assertEqual(machine.stderr, "")
            payload = json.loads(machine.stdout)
            self.assertEqual(payload["kind"], "agrep-meta")
            self.assertFalse(payload["freshness"]["checked"])

            self.assertEqual(
                run("chats", "alpha", "--no-auto").returncode, 0)
            positive_json = run(
                "chats", "alpha", "--json", "--no-auto")
            self.assertEqual(positive_json.returncode, 0)
            self.assertEqual(positive_json.stderr, "")


class ChatsDamagedAggregateTests(unittest.TestCase):
    """sessions.jsonl is derived and rebuildable: damage is agrep's own repair
    task. chats serves what parses, marks its totals a floor,
    kicks the rebuild silently, and never crashes or lies a confident zero."""

    def setUp(self) -> None:
        self._prior = {}
        for name in ("sessions.jsonl", "messages.jsonl"):
            path = common.DATA_DIR / name
            self._prior[name] = path.read_bytes() if path.exists() else None

    def tearDown(self) -> None:
        for name, body in self._prior.items():
            path = common.DATA_DIR / name
            if body is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(body)
        explore._freshen()

    def _run_kicked(self, argv):
        kick = indexd_runtime.RepairKick(True, "")
        with mock.patch.object(
                indexd_runtime, "kick_background_repair",
                return_value=kick) as kicked:
            rc, out, err = _run(argv)
        return rc, out, err, kicked

    def test_a_corrupt_line_is_skipped_served_around_and_healing(self) -> None:
        with (common.DATA_DIR / "sessions.jsonl").open(
                "w", encoding="utf-8") as f:
            for row in SESSIONS:
                f.write(json.dumps(row) + "\n")
            f.write("{torn mid-write\n")
        common.MESSAGES_PATH.touch()
        explore._freshen()
        rc, out, err, kicked = self._run_kicked([])
        self.assertEqual(rc, 0)
        self.assertEqual(len(out.splitlines()), 2)
        self.assertIn("showing 2 of 2+ matching chats", err)
        self.assertNotIn("Traceback", err)
        self.assertNotIn("doctor", err)
        kicked.assert_called()

    def test_corrupt_lines_mark_machine_totals_a_floor(self) -> None:
        with (common.DATA_DIR / "sessions.jsonl").open(
                "w", encoding="utf-8") as f:
            f.write(json.dumps(SESSIONS[0]) + "\n")
            f.write("\x00garbage\n")
        common.MESSAGES_PATH.touch()
        explore._freshen()
        rc, out, _err, _kicked = self._run_kicked(["--json"])
        self.assertEqual(rc, 0)
        lines = [json.loads(line) for line in out.splitlines()]
        self.assertEqual(lines[0]["kind"], "agrep-meta")
        self.assertEqual(lines[0]["completeness"]["total_basis"], "floor")
        self.assertEqual(len(lines[1:]), 1)

    def test_a_truncated_aggregate_serves_the_corpus_not_a_zero(self) -> None:
        (common.DATA_DIR / "sessions.jsonl").write_text("", encoding="utf-8")
        message = {
            "id": "codex:0199cccc-3333-7000-8000-000000000003:1",
            "session": "0199cccc-3333-7000-8000-000000000003",
            "agent": "codex", "project": "/home/u/webapp", "ts": 5000,
            "turn": 1, "text": "the corpus is fine", "who": "user",
        }
        common.MESSAGES_PATH.write_text(
            json.dumps(message) + "\n", encoding="utf-8")
        explore._freshen()
        rc, out, err, kicked = self._run_kicked([])
        self.assertEqual(rc, 0)
        self.assertIn("@0199cccc", out)
        self.assertIn("showing 1 of 1 matching chat", err)
        kicked.assert_called()

    def test_schema_mutant_rows_are_skipped_like_torn_bytes(self) -> None:
        # valid JSON, wrong shape: none may crash the reader; all count skipped
        mutants = (
            '{"session": []}',
            '{"session": {}}',
            '{"session": null}',
            '{"session": 42}',
            '{"session": true}',
            '{"agent": "codex"}',
            '["not", "a", "dict"]',
            '"just a string"',
        )
        with (common.DATA_DIR / "sessions.jsonl").open(
                "w", encoding="utf-8") as f:
            for row in SESSIONS:
                f.write(json.dumps(row) + "\n")
            for mutant in mutants:
                f.write(mutant + "\n")
        common.MESSAGES_PATH.touch()
        explore._freshen()
        rows, skipped = explore._session_index_read()
        self.assertEqual(len(rows), len(SESSIONS))
        self.assertEqual(skipped, len(mutants))
        rc, out, err, kicked = self._run_kicked([])
        self.assertEqual(rc, 0)
        self.assertEqual(len(out.splitlines()), 2)
        self.assertIn("showing 2 of 2+ matching chats", err)
        self.assertNotIn("Traceback", err)
        kicked.assert_called()

    def test_field_mutant_rows_are_skipped_like_torn_bytes(self) -> None:
        # valid JSON, right shape, wrong field types: "last_ts":"yesterday"
        # crashed resume's sort and "n":"lots" crashed chats' int() until the
        # row was hand-removed; both are the torn-byte damage class
        mutants = (
            '{"session":"99999999-9999-4999-8999-999999999991","agent":"claude",'
            '"project":"work","n":2,"first_ts":1,"last_ts":"yesterday",'
            '"first_text":"mutant"}',
            '{"session":"99999999-9999-4999-8999-999999999992","agent":"claude",'
            '"project":"work","n":"lots","first_ts":1,"last_ts":2,'
            '"first_text":"mutant"}',
        )
        with (common.DATA_DIR / "sessions.jsonl").open(
                "w", encoding="utf-8") as f:
            for row in SESSIONS:
                f.write(json.dumps(row) + "\n")
            for mutant in mutants:
                f.write(mutant + "\n")
        common.MESSAGES_PATH.touch()
        explore._freshen()
        rows, skipped = explore._session_index_read()
        self.assertEqual(len(rows), len(SESSIONS))
        self.assertEqual(skipped, len(mutants))
        rc, out, err, kicked = self._run_kicked([])
        self.assertEqual(rc, 0)
        self.assertEqual(len(out.splitlines()), 2)
        self.assertIn("showing 2 of 2+ matching chats", err)
        self.assertNotIn("Traceback", err)
        kicked.assert_called()

    def test_a_field_mutant_never_crashes_resume_listing(self) -> None:
        import resume
        with (common.DATA_DIR / "sessions.jsonl").open(
                "w", encoding="utf-8") as f:
            for row in SESSIONS:
                f.write(json.dumps(row) + "\n")
            f.write('{"session":"99999999-9999-4999-8999-999999999991",'
                    '"agent":"claude","project":"work","n":2,"first_ts":1,'
                    '"last_ts":"yesterday","first_text":"mutant"}\n')
        common.MESSAGES_PATH.touch()
        explore._freshen()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(indexd_runtime, "kick_background_repair",
                               return_value=indexd_runtime.RepairKick(True, "")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = resume.main(["-l"])
        self.assertEqual(rc, 0, err.getvalue())
        self.assertEqual(len(out.getvalue().splitlines()), len(SESSIONS))
        self.assertNotIn("Traceback", err.getvalue())

    def test_a_reply_digest_mutant_never_poisons_reply_expansion(self) -> None:
        # one appended row with an invalid content_digest raised CompactError
        # inside the lru-cached reader, killing around AND recall corpus-wide;
        # it must be skipped, keep the earlier good row, and kick the rebuild
        replies = common.DATA_DIR / "replies.jsonl"
        prior = replies.read_bytes() if replies.exists() else None
        self.addCleanup(lambda: (replies.write_bytes(prior) if prior is not None
                                 else replies.unlink(missing_ok=True)))
        good_id = "codex:0199aaaa-1111-7000-8000-000000000001:3"
        with replies.open("w", encoding="utf-8") as f:
            f.write(json.dumps({"id": good_id, "reply": "the real reply",
                                "reply_chars": 14,
                                "reply_truncated": False}) + "\n")
            f.write(json.dumps({"id": good_id, "reply": "shadow",
                                "content_digest": "NOTHEX", "reply_chars": 6,
                                "reply_truncated": False}) + "\n")
        explore._freshen()
        kick = indexd_runtime.RepairKick(True, "")
        with mock.patch.object(indexd_runtime, "kick_background_repair",
                               return_value=kick) as kicked:
            records = explore._reply_records_by_id()
        self.assertEqual(records[good_id]["reply"], "the real reply")
        kicked.assert_called()
        explore._reply_records_by_id.cache_clear()

    def test_unproved_empty_files_never_claim_an_exact_zero(self) -> None:
        (common.DATA_DIR / "sessions.jsonl").write_text("", encoding="utf-8")
        common.MESSAGES_PATH.write_text("", encoding="utf-8")
        explore._freshen()
        rc, out, err, kicked = self._run_kicked([])
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("showing 0 of 0 matching chats", err)
        kicked.assert_not_called()


class ChatsDispatchTests(unittest.TestCase):
    def _dispatch(self, argv):
        fired = []
        handlers = {name: (lambda n: lambda a: fired.append(
            (n, list(a.rest or []))) or 0)(name)
            for name in ("cmd_search", "cmd_chats")}
        with mock.patch.multiple(cli, **handlers), \
                mock.patch.object(sys, "argv", ["agrep", *argv]):
            cli._main()
        return fired[0]

    def test_chats_verb_dispatches_with_its_pattern(self) -> None:
        self.assertEqual(self._dispatch(["chats", "webapp"]),
                         ("cmd_chats", ["webapp"]))

    def test_search_verb_still_greps_the_word_chats(self) -> None:
        self.assertEqual(self._dispatch(["search", "chats"]),
                         ("cmd_search", ["chats"]))


class ChatsHandlePasteTests(unittest.TestCase):
    """E2: the epilogue's own instruction, executed rather than asserted on."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._prior = {}
        generated = (
            *corpusdb._DERIVED_PROOF_NAMES,
            ".derived_generation.json", ".ingest.sig", "settings.json",
        )
        for name in generated:
            path = common.DATA_DIR / name
            cls._prior[name] = path.read_bytes() if path.exists() else None
        _write_paste_fixture()

    @classmethod
    def tearDownClass(cls) -> None:
        for name, body in cls._prior.items():
            path = common.DATA_DIR / name
            if body is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(body)
        explore._freshen()

    def _printed_handle(self) -> str:
        rc, out, _err = _run(["login race"])
        self.assertEqual(rc, 0)
        handle = out.split()[0]
        self.assertTrue(handle.startswith("@"), handle)
        return handle

    def test_the_epilogue_still_documents_the_paths_executed_here(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), \
                self.assertRaises(SystemExit):
            search.chats_main(["--help"])
        epilogue = stdout.getvalue()
        self.assertIn("@session", epilogue)
        self.assertIn("latest indexed turn", epilogue)
        self.assertIn("--chat", epilogue)

    def test_the_documented_around_invocation_runs(self) -> None:
        handle = self._printed_handle()
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(around.indexd_runtime, "ensure_index",
                               return_value=True), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            rc = around.main([handle, "3"])
        self.assertEqual(rc, 0, stderr.getvalue())
        self.assertIn("turn 3 about the login race", stdout.getvalue())
        self.assertNotIn("already includes its turn", stderr.getvalue())

    def test_the_documented_chat_filter_accepts_the_same_handle(self) -> None:
        handle = self._printed_handle()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            resolved = search._resolve_chat(handle)
        self.assertEqual(resolved, SESSIONS[0]["session"], stderr.getvalue())

    def test_rows_offer_a_digest_bound_latest_handle(self) -> None:
        class LatestDb:
            def execute(self, _query, _sessions):
                return [(SESSIONS[0]["session"], 5,
                         "turn 5 about the login race", None)]

            def close(self) -> None:
                pass

        with mock.patch.object(corpusdb, "connect", return_value=LatestDb()):
            rc, out, err = _run(["login race", "--json"])
            self.assertEqual(rc, 0, err)
            row = json.loads(out.splitlines()[1])
            self.assertEqual(row["session"], SESSIONS[0]["session"])
            self.assertEqual(row["last_turn"], 5)
            self.assertTrue(row["session_handle"].startswith("@0199aaaa"))
            self.assertNotIn("session_handle_unavailable_reason", row)
            self.assertRegex(
                row["latest_handle"], r"^@0199aaaa[^:]*:5\.[0-9a-f]{4}$")

            with mock.patch.object(search.console, "WIN", True):
                followup = search.console.shell_command(
                    "agrep", "around", row["latest_handle"], fallback="")
                self.assertTrue(followup)
                rc, human, _err = _run(["login race"])
            self.assertEqual(rc, 0)
            self.assertIn(followup, human)
        opened, open_err = io.StringIO(), io.StringIO()
        with mock.patch.object(around.indexd_runtime, "ensure_index",
                               return_value=True), \
                contextlib.redirect_stdout(opened), \
                contextlib.redirect_stderr(open_err):
            open_rc = around.main([row["latest_handle"]])
        self.assertEqual(open_rc, 0, open_err.getvalue())
        self.assertIn("turn 5 about the login race", opened.getvalue())

    def test_topic_lookup_finds_a_chat_with_an_unrelated_opener(self) -> None:
        generic = {**SESSIONS[0], "first_text": "ok sounds good"}
        with mock.patch.object(
                explore, "_session_index",
                return_value={generic["session"]: generic}), \
                mock.patch.object(explore, "_session_concept", return_value={}):
            rc, out, err = _run(["login race", "--json"])
        self.assertEqual(rc, 0, err)
        row = json.loads(out.splitlines()[1])
        self.assertEqual(row["match_source"], "content")
        self.assertIn("login race", row["match_text"])
        self.assertRegex(
            row["match_handle"], r"^@0199aaaa[^:]*:\d+\.[0-9a-f]{4}$")

    def test_topic_lookup_without_recap_does_not_invent_exclusion(
            self) -> None:
        current = SESSIONS[0]["session"]
        text = "the current chat asks about the demand letter rule"
        hit = {
            "session": current, "turn": 2, "who": "user", "text": text,
            "snippet": text, "content_digest": search.compact.content_digest(text),
        }
        result = {
            "hits": [hit], "chats": 1, "totals_exact": True,
            "truncated": False, "index_missing": False, "tools_excluded": False,
        }
        with mock.patch.object(
                search.common, "in_agent_context", return_value=True), \
                mock.patch.object(
                    search.common, "calling_self_exclusion", return_value=None), \
                mock.patch.object(
                    explore, "_session_index",
                    return_value={row["session"]: row for row in SESSIONS}), \
                mock.patch.object(explore, "_session_concept", return_value={}), \
                mock.patch.object(search, "run_query", return_value=result) as query:
            rc, out, err = _run(["demand", "letter"])
        self.assertEqual(rc, 0, err)
        self.assertIn("@0199aaaa", out)
        filters = query.call_args.kwargs
        self.assertNotIn("exclude_session", filters)
        self.assertNotIn("exclude_session_from_turn", filters)

    def test_topic_candidate_page_cannot_be_consumed_by_hidden_side_chats(
            self) -> None:
        side_hits = [
            {"session": f"agent-side-{index}", "turn": 1}
            for index in range(80)
        ]
        ordinary = {"session": SESSIONS[1]["session"], "turn": 2}
        ranked = [*side_hits, ordinary]

        def query(_text, **kwargs):
            excluded = set(kwargs["_exclude_sessions"])
            visible = [
                hit for hit in ranked if hit["session"] not in excluded
            ]
            limit = kwargs["limit"]
            return {
                "hits": visible[:limit], "chats": len(visible),
                "totals_exact": limit >= len(visible),
            }

        with mock.patch.object(search, "run_query", side_effect=query) as run:
            heads, exact = search._chat_content_heads(
                "needle", agent=None, requested=20,
                hidden_sessions=frozenset(
                    hit["session"] for hit in side_hits))
        self.assertTrue(exact)
        self.assertIn(SESSIONS[1]["session"], heads)
        self.assertEqual(run.call_args.kwargs["session_limit"], 40)
        self.assertEqual(
            set(run.call_args.kwargs["_exclude_sessions"]),
            {hit["session"] for hit in side_hits})
        run.assert_called_once()

    def test_topic_candidate_page_does_not_precharge_every_hidden_chat(
            self) -> None:
        ordinary = [
            {"session": f"ordinary-{index}", "turn": 1}
            for index in range(20)
        ]
        side_hits = [
            {"session": f"agent-side-{index}", "turn": 1}
            for index in range(80)
        ]
        ranked = [*ordinary, *side_hits]

        def query(_text, **kwargs):
            excluded = set(kwargs["_exclude_sessions"])
            visible = [
                hit for hit in ranked if hit["session"] not in excluded
            ]
            return {
                "hits": visible[:kwargs["limit"]], "chats": len(visible),
                "totals_exact": False,
            }

        with mock.patch.object(search, "run_query", side_effect=query) as run:
            heads, exact = search._chat_content_heads(
                "needle", agent=None, requested=20,
                hidden_sessions=frozenset(
                    hit["session"] for hit in side_hits))
        self.assertFalse(exact)
        self.assertTrue(all(hit["session"] in heads for hit in ordinary))
        self.assertEqual(run.call_args.kwargs["session_limit"], 40)
        run.assert_called_once()
        self.assertEqual(
            set(run.call_args.kwargs["_exclude_sessions"]),
            {hit["session"] for hit in side_hits})

    def test_topic_lookup_with_unavailable_caller_fails_open_silently(
            self) -> None:
        text = "turn 2 about the login race"
        hit = {
            "session": SESSIONS[0]["session"], "turn": 2, "text": text,
            "content_digest": search.compact.content_digest(text),
        }
        result = {"hits": [hit], "chats": 1, "totals_exact": True}
        with mock.patch.object(
                search.common, "in_agent_context", return_value=True), \
                mock.patch.object(
                    search.common, "calling_self_exclusion", return_value=None), \
                mock.patch.object(search, "run_query", return_value=result):
            rc, out, err = _run(["login", "race"])
        self.assertEqual(rc, 0, err)
        self.assertIn("@0199aaaa", out)
        self.assertNotIn("self-exclusion", err)

    def test_unrepresentable_session_never_advertises_a_false_handle(self) -> None:
        hostile = {
            **SESSIONS[0], "session": "evil:session",
            "first_text": "invalid public handle fixture",
        }
        with mock.patch.object(
                explore, "_session_index",
                return_value={hostile["session"]: hostile}), \
                mock.patch.object(explore, "_session_concept", return_value={}):
            rc, out, err = _run([
                "invalid public handle fixture", "--json"])
            self.assertEqual(rc, 0, err)
            row = json.loads(out.splitlines()[1])
            self.assertIsNone(row["session_handle"])
            self.assertIn(
                "outside the public handle grammar",
                row["session_handle_unavailable_reason"])
            rc, human, _err = _run(["invalid public handle fixture"])
        self.assertEqual(rc, 0)
        self.assertIn("session=evil:session", human)
        self.assertNotIn("@evil:session", human)

    def test_db_unavailable_enrichment_never_materializes_the_corpus(self) -> None:
        for outcome in (None, OSError("read failed")):
            with self.subTest(outcome=type(outcome).__name__), \
                    mock.patch.object(
                        corpusdb, "connect", return_value=outcome
                        if outcome is None else mock.DEFAULT,
                        side_effect=outcome if isinstance(outcome, OSError) else None), \
                    mock.patch.object(
                        explore, "_messages_by_session",
                        side_effect=AssertionError("unbounded fallback")):
                self.assertEqual(search._chat_latest_claims([
                    SESSIONS[0]["session"]]), {})

    def test_a_full_result_handle_still_refuses_a_second_turn(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), \
                self.assertRaises(SystemExit) as caught:
            around._parse_target("@0199aaaa:3", "4", False)
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("already includes its turn", stderr.getvalue())

    def test_a_bare_handle_opens_the_latest_indexed_turn(self) -> None:
        handle = self._printed_handle()
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(around.indexd_runtime, "ensure_index",
                               return_value=True), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            rc = around.main([handle])
        self.assertEqual(rc, 0, stderr.getvalue())
        self.assertIn("turn 5 about the login race", stdout.getvalue())

    def test_board_handle_opens_the_live_chat_latest_turn(self) -> None:
        session = SESSIONS[0]["session"]
        snapshot = {
            "booting": False, "degraded_sources": [], "last_err": "",
            "now": 10_000, "window_s": 90,
            "sessions": [{
                "active": True, "agent": "codex", "last_ts": 9_000,
                "parent": None, "project": "/home/u/webapp", "recent": [],
                "session": session, "state": "done", "sub": False,
                "title": "fix the login race condition", "working": False,
            }],
        }
        watcher = mock.Mock()
        watcher.snapshot.return_value = snapshot
        board_out, board_err = io.StringIO(), io.StringIO()
        with mock.patch.object(
                livetui.indexd_runtime, "resident_indexd_live_snapshot",
                return_value=None), \
                mock.patch.object(livetui.live, "watcher", return_value=watcher), \
                mock.patch.object(livetui, "_enable_ansi", return_value=False), \
                contextlib.redirect_stdout(board_out), \
                contextlib.redirect_stderr(board_err):
            board_rc = livetui.main([
                "--once", "--json", "--session", session])
        self.assertEqual(board_rc, 0, board_err.getvalue())
        handle = json.loads(board_out.getvalue())["sessions"][0]["handle"]
        opened, open_err = io.StringIO(), io.StringIO()
        with mock.patch.object(around.indexd_runtime, "ensure_index",
                               return_value=True), \
                contextlib.redirect_stdout(opened), \
                contextlib.redirect_stderr(open_err):
            open_rc = around.main([handle])
        self.assertEqual(open_rc, 0, open_err.getvalue())
        self.assertIn("turn 5 about the login race", opened.getvalue())


if __name__ == "__main__":
    unittest.main()
