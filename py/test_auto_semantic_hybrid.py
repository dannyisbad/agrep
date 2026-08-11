"""Contract tests for the compact profile's automatic semantic assist."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from _test_support import isolate_data_dir


isolate_data_dir()
import compact  # noqa: E402
import corpusdb  # noqa: E402
import display_policy  # noqa: E402
import explore  # noqa: E402
import recall  # noqa: E402
import search  # noqa: E402
import semworker  # noqa: E402
import session_context  # noqa: E402
import surface_policy as surface  # noqa: E402


class _TtyBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


def _hit(session: str, turn: int, snippet: str, *, semantic: bool = False) -> dict:
    row = {
        "session": session,
        "turn": turn,
        "ts": 1_750_000_000_000 + turn,
        "who": "user",
        "agent": "codex",
        "project": "agrep",
        "content_digest": compact.content_digest(snippet),
        "snippet": snippet,
        "score": 10.0 - turn / 100,
    }
    if semantic:
        row.update({"sem_score": 0.9 - turn / 1000, "lane": "semantic"})
    return row


def _result(hits: list[dict], *, semantic: bool = False, **extra) -> dict:
    result = {
        "hits": [dict(hit) for hit in hits],
        "total": len(hits),
        "chats": len({hit["session"] for hit in hits}),
        "tool_hits": 0,
        "engine": "semantic:hybrid" if semantic else "corpusdb",
        "mode": "semantic" if semantic else "keyword",
        "totals_exact": True,
    }
    if semantic:
        result.update({
            "fallback_recommended": False,
            "semantic_status": {"state": "ready", "complete": True},
            "semantic_coverage": {
                "indexed": 20, "total": 20, "pending": 0, "complete": True},
        })
    result.update(extra)
    return result


def _search_json(stdout: str) -> tuple[dict, list[dict]]:
    """Split search's one run envelope from its row-level JSONL evidence."""
    lines = [json.loads(line) for line in stdout.splitlines() if line]
    if not lines or lines[0].get("kind") != "agrep-meta":
        raise AssertionError("search JSON must start with an agrep-meta envelope")
    return lines[0], lines[1:]


def _excluded_keys(count: int) -> set[tuple]:
    return {("root", turn, "user", f"hidden-{turn}")
            for turn in range(max(0, count))}


def _self_only_database(path: Path) -> None:
    db = sqlite3.connect(path)
    db.executescript(corpusdb._SCHEMA_SQL)
    db.execute(
        corpusdb._INS,
        ("root", 9, 1_750_000_000_009, "codex", "agrep", "", "", "",
         "user", "needle current echo"),
    )
    db.execute("INSERT INTO session_family VALUES('root', 'root', 0)")
    db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
    db.execute(
        "INSERT INTO msgs_prose_fts(rowid, text) "
        "SELECT id, text FROM msgs WHERE who <> 'tool'")
    db.commit()
    db.close()


def _family_database(
        path: Path, *, caller: str = "root", sidechain_count: int = 0,
) -> None:
    db = sqlite3.connect(path)
    db.executescript(corpusdb._SCHEMA_SQL)
    rows = [
        ("root", 9, "needle current echo"),
        ("child", 4, "needle useful side chat"),
        ("other", 3, "needle independent history"),
    ]
    if caller != "root":
        rows.append((caller, 10, "needle delegated caller echo"))
    rows.extend(
        (f"agent-echo-{index}", index, f"needle delegated echo {index}")
        for index in range(sidechain_count))
    for session, turn, text in rows:
        db.execute(
            corpusdb._INS,
            (session, turn, 1_750_000_000_000 + turn, "codex", "agrep",
             "", "", "", "user", text),
        )
    family_rows = [
        ("root", "root", 0), ("child", "root", 0), ("other", "other", 0),
    ]
    if caller != "root":
        family_rows.append((caller, "root", 1))
    family_rows.extend(
        (f"agent-echo-{index}", "root", 1)
        for index in range(sidechain_count))
    db.executemany("INSERT INTO session_family VALUES(?, ?, ?)", family_rows)
    db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
    db.execute(
        "INSERT INTO msgs_prose_fts(rowid, text) "
        "SELECT id, text FROM msgs WHERE who <> 'tool'")
    db.commit()
    db.close()


class AutoSemanticHybridTests(unittest.TestCase):
    def _run(self, argv: list[str], run_query, *, compact_profile: bool = True,
             current_session: str | None = None, tty_stderr: bool = False,
             family_parents: dict[str, str] | None = None,
             family_sides: frozenset[str] = frozenset(),
             family_resolved: bool = True,
             recap_turn: int | None = None,
             agent_context: bool | None = None,
             excluded_match_count: int | None = 0,
             semantic_runtime_installed: bool = True,
             extra_env: dict[str, str] | None = None):
        env = {
            "AGREP_PROFILE": "compact" if compact_profile else "",
            # Keep caller resolution independent of whichever agent runs this
            # test process; individual cases opt into an identity explicitly.
            "CODEX_THREAD_ID": current_session or "",
            "CLAUDE_CODE_SESSION_ID": "",
            "CLAUDECODE": "",
            "CLAUDE_CODE": "",
            "CLAUDE_CODE_ENTRYPOINT": "",
            **(extra_env or {}),
        }
        stdout = _TtyBuffer()
        stderr = _TtyBuffer() if tty_stderr else io.StringIO()
        parents = family_parents or {}
        family = None
        if current_session:
            root = search.common.family_root(current_session, parents)
            candidates = {current_session, root, *parents, *parents.values()}
            members = frozenset(
                session for session in candidates
                if search.common.family_root(session, parents) == root
            )
            family = search.common.CallingFamily(
                current_session, root, members, family_resolved, recap_turn,
                family_sides)
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.dict(os.environ, env), \
                mock.patch.object(search.common, "DATA_DIR", Path(td)), \
                mock.patch.object(search.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(search.common, "transcript_generation",
                                  return_value={"generation": 7}), \
                mock.patch.object(search.common, "in_agent_context",
                                  return_value=(current_session is not None
                                                if agent_context is None
                                                else agent_context)), \
                mock.patch.object(session_context, "calling_family",
                                  return_value=family), \
                mock.patch.object(
                    search, "_self_exclusion_match_keys",
                    return_value=(None if excluded_match_count is None else
                                  _excluded_keys(excluded_match_count))), \
                mock.patch.object(
                    search, "_semantic_runtime_installed",
                    return_value=semantic_runtime_installed), \
                mock.patch.object(search, "run_query", side_effect=run_query), \
                mock.patch.object(search, "_resolve_chat",
                                  side_effect=lambda session: session), \
                mock.patch.object(search, "_stream_first_run", return_value=None), \
                mock.patch.object(explore, "_session_index", return_value={}), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            rc = search.main(argv)
        return rc, stdout.getvalue(), stderr.getvalue()

    def test_no_recap_fails_open_without_excluding_or_nagging(self) -> None:
        keyword = [
            _hit("root", 1, "needle in root"),
            _hit("child", 2, "needle in child"),
            _hit("grandchild", 3, "needle in grandchild"),
            _hit("other", 4, "needle in other"),
        ]

        seen = {}

        def run_query(_query, *, mode="keyword", **kwargs):
            self.assertEqual(mode, "keyword")
            seen.update(kwargs)
            return _result(keyword)

        rc, stdout, stderr = self._run(
            ["needle", "--lexical"],
            run_query,
            current_session="root",
            family_parents={"child": "root", "grandchild": "child"},
        )
        self.assertEqual(rc, 0)
        self.assertNotIn("exclude_session", seen)
        self.assertIn("needle in root", stdout)
        self.assertIn("needle in child", stdout)
        self.assertIn("needle in grandchild", stdout)
        self.assertIn("needle in other", stdout)
        self.assertNotIn("excluded", stderr)
        self.assertNotIn("--self", stderr)

    def test_self_exclusion_reaches_query_before_top_k(self) -> None:
        current = [_hit("root", index, "needle current")
                   for index in range(40)]
        other = [_hit(f"other-{index}", index + 100, "needle other")
                 for index in range(40)]
        seen_kwargs = None

        def run_query(_query, *, mode="keyword", **kwargs):
            nonlocal seen_kwargs
            seen_kwargs = kwargs
            return _result([*current, *other])

        rc, stdout, stderr = self._run(
            ["needle", "--lexical", "--classic", "--color", "never"],
            run_query,
            current_session="root",
            recap_turn=0,
            excluded_match_count=40,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(seen_kwargs["limit"], 40)
        self.assertEqual(seen_kwargs["exclude_session"], "root")
        self.assertEqual(len(stdout.splitlines()), 40)
        self.assertNotIn("needle current", stdout)
        self.assertEqual(stdout.count("needle other"), 40)
        self.assertIn("excluded 40 hits", stderr)

    def test_no_recap_never_applies_an_engine_filter(
            self) -> None:
        def run_query(_query, *, mode="keyword", **kwargs):
            self.assertNotIn("exclude_session", kwargs)
            return _result([_hit("other", 3, "needle independent")])

        rc, stdout, stderr = self._run(
            ["needle", "--lexical"], run_query, current_session="root")
        self.assertEqual(rc, 0)
        self.assertIn("needle independent", stdout)
        self.assertNotIn("excluded", stderr)
        self.assertNotIn("--self", stderr)

    def test_conflicting_identity_fails_open_silently_unless_forced(self) -> None:
        seen = {}

        def run_query(_query, *, mode="keyword", **kwargs):
            seen.update(kwargs)
            return _result([_hit("live-claude", 3, "needle current")])

        env = {
            "CODEX_THREAD_ID": "stale-codex",
            "CLAUDE_CODE_SESSION_ID": "live-claude",
            "CLAUDECODE": "1",
        }
        rc, stdout, stderr = self._run(
            ["needle", "--lexical"], run_query,
            agent_context=True, extra_env=env)
        self.assertEqual(rc, 0)
        self.assertNotIn("exclude_session", seen)
        self.assertIn("needle current", stdout)
        self.assertNotIn("excluded", stderr)
        self.assertNotIn("--self", stderr)
        self.assertNotIn("identities conflict", stderr)

        rc, forced_stdout, forced_stderr = self._run(
            ["needle", "--lexical", "--no-self"], run_query,
            agent_context=True, extra_env=env)
        self.assertEqual(rc, 0)
        self.assertIn("needle current", forced_stdout)
        self.assertIn("--no-self was not applied", forced_stderr)
        self.assertIn("identities conflict", forced_stderr)

        rc, stdout, _ = self._run(
            ["needle", "--json", "--lexical", "--no-self"], run_query,
            agent_context=True, extra_env=env)
        self.assertEqual(rc, 0)
        head, rows = _search_json(stdout)
        self.assertEqual(
            head["self_exclusion"],
            {"active": False, "reason": "identity-conflict"})
        self.assertEqual([row["session"] for row in rows], ["live-claude"])

    def test_recap_window_excludes_inclusive_echo_and_labels_older_family(self) -> None:
        seen = {}
        hits = [
            _hit("root", 2, "needle before recap"),
            _hit("root", 7, "needle recap echo"),
            _hit("root", 9, "needle current echo"),
            _hit("child", 20, "needle side evidence"),
            _hit("other", 30, "needle independent"),
        ]

        def run_query(_query, *, mode="keyword", **kwargs):
            seen.update(kwargs)
            return _result(hits)

        rc, stdout, stderr = self._run(
            ["needle", "--lexical"], run_query,
            current_session="root", family_parents={"child": "root"},
            recap_turn=7, excluded_match_count=2)
        self.assertEqual(rc, 0)
        self.assertEqual(seen["exclude_session"], "root")
        self.assertEqual(seen["exclude_session_from_turn"], 7)
        self.assertIn("~self needle before recap", stdout)
        self.assertIn("needle side evidence", stdout)
        self.assertNotIn("~self needle side evidence", stdout)
        self.assertIn("needle independent", stdout)
        self.assertNotIn("recap echo", stdout)
        self.assertNotIn("current echo", stdout)
        # F1: one short owned line, not the mechanism lecture
        self.assertEqual(stderr.count(
            "excluded 2 hits from the current window"), 1)
        self.assertNotIn("--self", stderr)
        self.assertNotIn("family sidechains stay hidden", stderr)

    def test_recap_window_keeps_same_family_sidechains_visible(self) -> None:
        hits = [
            _hit("root", 2, "needle before recap"),
            _hit("custom-side", 0, "needle delegated echo"),
            _hit("child", 20, "needle side evidence"),
            _hit("agent-name-only", 0, "needle prefix-only history"),
            _hit("foreign-side", 0, "needle independent sidechain"),
        ]

        def run_query(_query, *, mode="keyword", **_kwargs):
            return _result(hits)

        rc, stdout, stderr = self._run(
            ["needle", "--lexical"], run_query,
            current_session="root",
            family_parents={
                "child": "root", "custom-side": "root",
                "agent-name-only": "root",
            },
            family_sides=frozenset({"custom-side"}),
            recap_turn=7)
        self.assertEqual(rc, 0)
        self.assertIn("~self needle before recap", stdout)
        self.assertIn("needle delegated echo", stdout)
        self.assertIn("needle side evidence", stdout)
        self.assertIn("needle independent sidechain", stdout)
        self.assertNotIn("~self needle side evidence", stdout)
        self.assertNotIn("excluded", stderr)
        self.assertNotIn("--self", stderr)

    def test_display_lane_sinks_verbatim_query_echoes(self) -> None:
        query = "why does the deployment keep retrying after compaction"
        keyword = [
            _hit("echoer", 1, "ECHOROW delegated brief"),
            _hit("fixer", 2, "EVIDENCEROW compaction held the lock"),
        ]

        def run_query(_query, *, mode="keyword", **_kwargs):
            if mode == "semantic":
                return _result([], semantic=True)
            return _result(keyword)

        with tempfile.TemporaryDirectory() as td:
            database = Path(td) / "corpus.db"
            db = sqlite3.connect(database)
            db.execute(
                "CREATE TABLE msgs(session TEXT, turn INTEGER, "
                "who TEXT, text TEXT)")
            db.executemany(
                "INSERT INTO msgs VALUES(?, ?, 'user', ?)",
                (("echoer", 1, "subtask brief: why does the deployment keep "
                               "retrying after compaction - report back"),
                 ("fixer", 2, "the deployment kept retrying because "
                              "compaction held the sqlite lock")))
            db.commit()
            db.close()

            def connect(**_kwargs):
                return sqlite3.connect(database)

            with mock.patch.object(corpusdb, "connect", side_effect=connect):
                rc, stdout, _stderr = self._run([query], run_query)
        self.assertEqual(rc, 0)
        # the echo row outranks on score; the display lane must sink it
        self.assertIn("ECHOROW", stdout)
        self.assertLess(stdout.index("EVIDENCEROW"), stdout.index("ECHOROW"))

    def test_window_labels_survive_keyword_semantic_fusion(self) -> None:
        query = "why did deployment keep retrying after compaction"
        keyword = [_hit("root", 3, "deployment kept retrying before compaction")]
        meaning = [
            _hit("root", 8, "current semantic echo", semantic=True),
            _hit("child", 30, "side agent found the retry cause", semantic=True),
            _hit("other", 31, "independent meaning", semantic=True),
        ]

        def run_query(_query, *, mode="keyword", **kwargs):
            self.assertEqual(kwargs["exclude_session_from_turn"], 8)
            return (_result(meaning, semantic=True)
                    if mode == "semantic" else _result(keyword))

        rc, stdout, _stderr = self._run(
            [query], run_query, current_session="root",
            family_parents={"child": "root"}, recap_turn=8,
            excluded_match_count=1)
        self.assertEqual(rc, 0)
        self.assertNotIn("current semantic echo", stdout)
        self.assertIn("~self deployment kept retrying", stdout)
        self.assertRegex(
            stdout, r"~semantic .*side agent found the retry cause")
        self.assertNotRegex(
            stdout, r"~self ~semantic .*side agent found the retry cause")

    def test_chat_filter_waives_but_json_keeps_window_policy(self) -> None:
        calls = []
        current = _hit("root", 12, "needle current echo")
        other = _hit("other", 3, "needle independent")

        def run_query(_query, *, mode="keyword", **kwargs):
            calls.append(kwargs)
            return _result([current] if kwargs.get("chat") else [current, other])

        rc, stdout, _stderr = self._run(
            ["needle", "--chat", "root", "--lexical"],
            run_query, current_session="root", recap_turn=7)
        self.assertEqual(rc, 0)
        self.assertNotIn("exclude_session", calls[-1])
        self.assertIn("needle current echo", stdout)
        self.assertNotIn("~self", stdout)

        rc, stdout, _stderr = self._run(
            ["needle", "--json", "--lexical"],
            run_query, current_session="root", recap_turn=7,
            excluded_match_count=1)
        self.assertEqual(rc, 0)
        self.assertEqual(calls[-1]["exclude_session"], "root")
        self.assertEqual(calls[-1]["exclude_session_from_turn"], 7)
        head, rows = _search_json(stdout)
        self.assertEqual(head["self_exclusion"]["excluded_hits"], 1)
        self.assertEqual([row["session"] for row in rows], ["other"])
        self.assertNotIn("engine", rows[0])
        self.assertNotIn("semantic", rows[0])
        self.assertNotIn("_self", stdout)

    def test_window_policy_is_silent_when_it_excludes_no_match(self) -> None:
        def run_query(_query, *, mode="keyword", **_kwargs):
            return _result([_hit("other", 30, "needle independent")])

        rc, stdout, stderr = self._run(
            ["needle", "--lexical"], run_query,
            current_session="root", recap_turn=7)
        self.assertEqual(rc, 0)
        self.assertNotIn("excluded", stderr)
        self.assertNotIn("--self", stderr)
        self.assertNotIn("~self", stdout)

    def test_engine_filtered_empty_page_discloses_each_active_self_policy(
            self) -> None:
        raw = [_hit("root", 9, "needle current echo")]
        cases = (
            ([], 7, 1, 1, "excluded 1 hit from the current window"),
            ([], None, 0, 0, None),
            (["--no-self"], 7, 1, 1,
             "excluded 1 hit from this session family"),
        )
        for extra, recap_turn, excluded_count, expected_rc, expected in cases:
            with self.subTest(extra=extra, recap_turn=recap_turn):
                captured = {}

                def run_query(_query, *, mode="keyword", **kwargs):
                    captured.update(kwargs)
                    boundary = kwargs.get("exclude_session_from_turn")
                    kept = [
                        hit for hit in raw
                        if not (
                            hit["session"] == kwargs.get("exclude_session")
                            and (boundary is None or hit["turn"] >= boundary)
                        )
                    ]
                    return _result(kept)

                rc, stdout, stderr = self._run(
                    ["needle", "--lexical", *extra], run_query,
                    current_session="root", recap_turn=recap_turn,
                    excluded_match_count=excluded_count)
                self.assertEqual(rc, expected_rc)
                if recap_turn is None and not extra:
                    self.assertNotIn("exclude_session", captured)
                    self.assertIn("needle current echo", stdout)
                    self.assertNotIn("excluded", stderr)
                else:
                    self.assertEqual(stdout, "")
                    self.assertEqual(captured["exclude_session"], "root")
                    self.assertEqual(stderr.count(expected), 1)
                    self.assertNotIn("--self", stderr)

    def test_empty_page_omits_scope_notice_when_caller_has_no_indexed_rows(
            self) -> None:
        rc, stdout, stderr = self._run(
            ["needle", "--lexical"], lambda *_args, **_kwargs: _result([]),
            current_session="absent", recap_turn=0, excluded_match_count=0)
        self.assertEqual((rc, stdout), (1, ""))
        self.assertNotIn("--self to include", stderr)

    def test_real_keyword_engine_empty_page_discloses_window_filter(self) -> None:
        family = search.common.CallingFamily(
            "root", "root", frozenset({"root"}), True, 7)
        stdout, stderr = _TtyBuffer(), io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            database = root / "corpus.db"
            _self_only_database(database)

            def connect(**_kwargs):
                return sqlite3.connect(database)

            with mock.patch.dict(os.environ, {"AGREP_PROFILE": "compact"}), \
                    mock.patch.object(search.common, "DATA_DIR", root), \
                    mock.patch.object(
                        search.indexd_runtime, "ensure_index",
                        return_value=True), \
                    mock.patch.object(
                        search.indexd_runtime, "agent_freshness_notice",
                        return_value=""), \
                    mock.patch.object(
                        search.common, "transcript_generation",
                        return_value={"generation": 7}), \
                    mock.patch.object(
                        search.common, "in_agent_context",
                        return_value=True), \
                    mock.patch.object(
                        session_context, "calling_family",
                        return_value=family), \
                    mock.patch.object(
                        search.common, "indexed_self_exclusion_has_rows",
                        return_value=True), \
                    mock.patch.object(
                        corpusdb, "connect", side_effect=connect), \
                    mock.patch.object(
                        search, "_stream_first_run", return_value=None), \
                    mock.patch.object(
                        explore, "_session_index", return_value={}), \
                    contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(stderr):
                rc = search.main(["needle", "--lexical"])
        self.assertEqual(rc, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            stderr.getvalue().count(
                "excluded 1 hit from the current window"), 1)
        self.assertNotIn("--self", stderr.getvalue())

    def test_real_keyword_zero_omits_scope_when_current_chat_does_not_match(
            self) -> None:
        family = search.common.CallingFamily(
            "root", "root", frozenset({"root"}), True, 7)
        stdout, stderr = _TtyBuffer(), io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            database = root / "corpus.db"
            _self_only_database(database)

            def connect(**_kwargs):
                return sqlite3.connect(database)

            with mock.patch.dict(os.environ, {"AGREP_PROFILE": "compact"}), \
                    mock.patch.object(search.common, "DATA_DIR", root), \
                    mock.patch.object(
                        search.indexd_runtime, "ensure_index",
                        return_value=True), \
                    mock.patch.object(
                        search.indexd_runtime, "agent_freshness_notice",
                        return_value=""), \
                    mock.patch.object(
                        search.common, "transcript_generation",
                        return_value={"generation": 7}), \
                    mock.patch.object(
                        search.common, "in_agent_context",
                        return_value=True), \
                    mock.patch.object(
                        session_context, "calling_family",
                        return_value=family), \
                    mock.patch.object(
                        search.common, "indexed_self_exclusion_has_rows",
                        return_value=True), \
                    mock.patch.object(
                        corpusdb, "connect", side_effect=connect), \
                    mock.patch.object(
                        search, "_stream_first_run", return_value=None), \
                    mock.patch.object(
                        explore, "_session_index", return_value={}), \
                    contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(stderr):
                rc = search.main(["absent-term", "--lexical"])
        self.assertEqual(rc, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn("--self to include", stderr.getvalue())
        self.assertIn("no match", stderr.getvalue())

    def _real_family_search(
            self, argv: list[str], *, family_resolved: bool = True,
            caller: str = "root", sidechain_count: int = 0,
            recap_turn: int | None = 7,
    ) -> tuple[int, str, str]:
        sidechains = {
            f"agent-echo-{index}" for index in range(sidechain_count)}
        side_sessions = set(sidechains)
        if caller != "root":
            side_sessions.add(caller)
        members = {"root", "child", caller, *sidechains}
        family = search.common.CallingFamily(
            caller, "root",
            frozenset(members if family_resolved else {caller}),
            family_resolved, recap_turn, frozenset(side_sessions))
        stdout, stderr = _TtyBuffer(), io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            database = root / "corpus.db"
            _family_database(
                database, caller=caller, sidechain_count=sidechain_count)

            def connect(**_kwargs):
                return sqlite3.connect(database)

            with mock.patch.dict(os.environ, {"AGREP_PROFILE": "compact"}), \
                    mock.patch.object(search.common, "DATA_DIR", root), \
                    mock.patch.object(
                        search.indexd_runtime, "ensure_index",
                        return_value=True), \
                    mock.patch.object(
                        search.indexd_runtime, "agent_freshness_notice",
                        return_value=""), \
                    mock.patch.object(
                        search.common, "transcript_generation",
                        return_value={"generation": 7}), \
                    mock.patch.object(
                        search.common, "in_agent_context",
                        return_value=True), \
                    mock.patch.object(
                        session_context, "calling_family",
                        return_value=family), \
                    mock.patch.object(
                        search.common, "indexed_self_exclusion_has_rows",
                        return_value=True), \
                    mock.patch.object(
                        corpusdb, "connect", side_effect=connect), \
                    mock.patch.object(
                        search, "_stream_first_run", return_value=None), \
                    mock.patch.object(
                        explore, "_session_index", return_value={}), \
                    contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(stderr):
                rc = search.main(argv)
        return rc, stdout.getvalue(), stderr.getvalue()

    def test_real_engine_auto_exclusion_keeps_family_side_chats(self) -> None:
        rc, stdout, stderr = self._real_family_search(
            ["needle", "--lexical"])
        self.assertEqual(rc, 0)
        self.assertNotIn("needle current echo", stdout)
        self.assertIn("needle useful side chat", stdout)
        self.assertIn("needle independent history", stdout)
        self.assertNotIn("--self to include", stderr)

    def test_delegated_window_prefilter_keeps_side_chats(self) -> None:
        rc, stdout, stderr = self._real_family_search(
            ["delegated", "--lexical", "-n", "1"],
            caller="agent-caller", sidechain_count=8, recap_turn=0)
        self.assertEqual(rc, 0)
        self.assertNotIn("caller echo", stdout)
        self.assertIn("delegated echo", stdout)
        self.assertNotIn("invalid", stderr)

    def test_family_publication_move_fails_open_without_exclusion(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            database = root / "corpus.db"
            _family_database(database)
            db = sqlite3.connect(database)
            db.execute("INSERT INTO meta VALUES('family_stamp', 'before')")
            db.commit()
            db.close()
            with mock.patch.object(session_context, "DATA_DIR", root), \
                    mock.patch.object(
                        session_context, "calling_session", return_value="root"), \
                    mock.patch.object(
                        session_context, "session_family_source_stamp",
                        side_effect=("before", "after")):
                policy = session_context.calling_self_exclusion()
        self.assertIsNone(policy)

        rc, stdout, stderr = self._real_family_search(
            ["needle", "--lexical"], family_resolved=False)
        self.assertEqual(rc, 0)
        self.assertIn("needle current echo", stdout)
        self.assertIn("needle useful side chat", stdout)
        self.assertIn("needle independent history", stdout)
        self.assertNotIn("excluded", stderr)

    def test_real_engine_discloses_a_hidden_current_chat_match(self) -> None:
        rc, stdout, stderr = self._real_family_search(
            ["current", "--lexical"])
        self.assertEqual((rc, stdout), (1, ""))
        self.assertIn("excluded 1 hit from the current window", stderr)

    def test_real_engine_explicit_no_self_excludes_the_family(self) -> None:
        rc, stdout, _stderr = self._real_family_search(
            ["needle", "--lexical", "--no-self"])
        self.assertEqual(rc, 0)
        self.assertNotIn("needle current echo", stdout)
        self.assertNotIn("needle useful side chat", stdout)
        self.assertIn("needle independent history", stdout)

    def test_real_engine_machine_contract_changes_only_for_no_self(self) -> None:
        rc, stdout, stderr = self._real_family_search(
            ["needle", "--json", "--lexical"])
        self.assertEqual((rc, stderr), (0, ""))
        rows = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual(
            {row["session"] for row in rows[1:]},
            {"child", "other"})
        self.assertEqual(rows[0]["kind"], "agrep-meta")

        rc, stdout, stderr = self._real_family_search(
            ["needle", "--json", "--lexical", "--no-self"])
        self.assertEqual((rc, stderr), (0, ""))
        rows = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual([row["session"] for row in rows[1:]], ["other"])
        self.assertTrue(rows[0]["self_exclusion"]["active"])

    def test_no_self_forces_conservative_family_exclusion_after_recap(self) -> None:
        calls = []
        hits = [
            _hit("root", 2, "needle old caller"),
            _hit("child", 20, "needle side evidence"),
            _hit("other", 30, "needle independent"),
        ]

        def run_query(_query, *, mode="keyword", **kwargs):
            calls.append(kwargs)
            return _result(hits)

        rc, stdout, _stderr = self._run(
            ["needle", "--lexical", "--no-self"], run_query,
            current_session="root", family_parents={"child": "root"},
            recap_turn=7, excluded_match_count=2)
        self.assertEqual(rc, 0)
        self.assertEqual(calls[-1]["exclude_session"], "root")
        self.assertNotIn("exclude_session_from_turn", calls[-1])
        self.assertNotIn("old caller", stdout)
        self.assertNotIn("side evidence", stdout)
        self.assertIn("needle independent", stdout)

    def test_unresolved_family_fails_open_before_top_k(self) -> None:
        seen_kwargs = None
        other = [_hit(f"other-{index}", index, "needle other")
                 for index in range(40)]

        def run_query(_query, *, mode="keyword", **kwargs):
            nonlocal seen_kwargs
            seen_kwargs = kwargs
            return _result(other)

        rc, stdout, stderr = self._run(
            ["needle", "--lexical", "--classic", "--color", "never"],
            run_query,
            current_session="current",
            family_resolved=False,
        )
        self.assertEqual(rc, 0)
        self.assertNotIn("exclude_session", seen_kwargs)
        self.assertEqual(len(stdout.splitlines()), 40)
        self.assertEqual(stdout.count("needle other"), 40)
        self.assertNotIn("excluded", stderr)
        self.assertNotIn("--self", stderr)

    def test_failed_identification_fails_open_silently(self) -> None:
        def run_query(_query, *, mode="keyword", **_kwargs):
            return _result([_hit("other", 1, "needle other")])

        rc, stdout, stderr = self._run(
            ["needle", "--lexical"], run_query, agent_context=True)
        self.assertEqual(rc, 0)
        self.assertIn("needle other", stdout)
        self.assertEqual(stderr, "")

        rc, stdout, stderr = self._run(
            ["needle", "--lexical", "--classic", "--color", "never"],
            run_query, agent_context=True)
        self.assertEqual(rc, 0)
        self.assertIn("needle other", stdout)
        self.assertEqual(stderr, "")

        rc, stdout, stderr = self._run(
            ["needle", "--lexical", "--classic", "--color", "never",
             "--no-self"],
            run_query, agent_context=True)
        self.assertEqual(rc, 0)
        self.assertIn("needle other", stdout)
        self.assertIn("--no-self was not applied", stderr)
        self.assertIn("could not identify this session", stderr)

        rc, _stdout, stderr = self._run(
            ["needle", "--lexical", "--classic", "--color", "never"],
            run_query, agent_context=False)
        self.assertEqual(rc, 0)
        self.assertEqual(stderr, "")

    def test_post_filter_reports_only_known_surviving_hits(self) -> None:
        leaked = [_hit("current", index, "needle current")
                  for index in range(5)]
        kept = [_hit(f"other-{index}", index + 10, f"needle other {index}")
                for index in range(2)]

        def run_query(_query, *, mode="keyword", **_kwargs):
            return _result([*leaked, *kept], total=100, tool_hits=25)

        rc, stdout, stderr = self._run(
            ["needle", "--lexical"],
            run_query,
            current_session="current",
            recap_turn=0,
            excluded_match_count=5,
        )
        self.assertEqual(rc, 0)
        self.assertNotIn("needle current", stdout)
        self.assertEqual(stdout.count("needle other"), 2)
        self.assertIn(
            "excluded 5 hits from the current window", stderr)
        self.assertNotIn("matching rows", stderr)
        self.assertNotIn("95", stderr)

    def test_flat_uses_the_same_proven_window_as_porcelain(self) -> None:
        calls = []

        def run_query(_query, *, mode="keyword", **kwargs):
            calls.append(kwargs)
            if kwargs.get("exclude_session") == "root":
                return _result([])
            return _result([_hit("root", 1, "needle current")])

        with mock.patch.object(
                search.indexd_runtime, "freshness_story",
                return_value=surface.FreshnessStory("current")):
            rc, stdout, _stderr = self._run(
                ["needle", "--flat", "--lexical", "--color", "never"],
                run_query, current_session="root", recap_turn=1,
                excluded_match_count=1)
        self.assertEqual(rc, 1)
        self.assertEqual(calls[-1]["exclude_session"], "root")
        self.assertEqual(calls[-1]["exclude_session_from_turn"], 1)
        self.assertEqual(stdout, "")

        with mock.patch.object(
                search.indexd_runtime, "freshness_story",
                return_value=surface.FreshnessStory("current")):
            rc, _stdout, _stderr = self._run(
                ["needle", "--flat", "--lexical", "--no-self",
                 "--color", "never"],
                run_query, current_session="root", excluded_match_count=1)
        self.assertEqual(rc, 1)
        self.assertEqual(calls[-1]["exclude_session"], "root")
        self.assertNotIn("exclude_session_from_turn", calls[-1])

    def test_count_uses_the_same_proven_window_as_porcelain(self) -> None:
        calls = []

        def run_query(_query, *, mode="keyword", **kwargs):
            calls.append(kwargs)
            return _result([_hit("other", 1, "needle independent")], total=1)

        rc, stdout, stderr = self._run(
            ["needle", "-c", "--lexical"], run_query,
            current_session="root", recap_turn=4,
            excluded_match_count=2)
        self.assertEqual(rc, 0)
        self.assertEqual(stdout, "1\n")
        self.assertEqual(calls[-1]["exclude_session"], "root")
        self.assertEqual(calls[-1]["exclude_session_from_turn"], 4)
        self.assertEqual(
            stderr.count("excluded 2 hits from the current window"), 1)
        self.assertNotIn("--self", stderr)

    def test_agent_search_discloses_persistent_freshness_failure(self) -> None:
        keyword = [_hit("other", 1, "needle")]

        def run_query(_query, *, mode="keyword", **_kwargs):
            return _result(keyword)

        with mock.patch.object(
                search.indexd_runtime,
                "agent_freshness_notice",
                return_value="history may be stale - run `agrep doctor`",
        ):
            rc, _stdout, stderr = self._run(
                ["needle", "--lexical"], run_query, current_session="current")
        self.assertEqual(rc, 0)
        self.assertEqual(stderr.count("history may be stale"), 1)

    def test_phrase_keyword_hits_do_not_suppress_compact_semantic_assist(self) -> None:
        query = "malicious markup can steal the local archive"
        keyword = [_hit(f"keyword-{index}", index, f"{query} clue {index}")
                   for index in range(3)]
        meaning = [_hit(f"meaning-{index}", index + 10,
                        f"stored browser injection evidence {index}", semantic=True)
                   for index in range(5)]
        calls = []

        def run_query(_query, *, mode="keyword", **_kwargs):
            calls.append(mode)
            return _result(meaning, semantic=True) if mode == "semantic" \
                else _result(keyword)

        rc, stdout, stderr = self._run([query], run_query)
        lines = stdout.splitlines()
        self.assertEqual(rc, 0)
        self.assertEqual(calls.count("keyword"), 1)
        self.assertEqual(calls.count("semantic"), 1)
        self.assertTrue(lines[0].endswith("clue 0"))
        self.assertEqual(sum("~semantic" in line for line in lines), 1)
        self.assertIn("stored browser injection evidence", stdout)

    def test_sparse_lexical_page_adds_at_most_three_labeled_meaning_rows(self) -> None:
        query = "why did history refresh rebuild unchanged conversations"
        keyword = [_hit("keyword", 1, "history refresh rebuild unchanged conversations")]
        keyword[0]["matched"] = "all-terms"
        meaning = [_hit(f"meaning-{index}", index + 10,
                        f"cache invalidation evidence {index} " + "x" * 500,
                        semantic=True)
                   for index in range(12)]

        def run_query(_query, *, mode="keyword", **_kwargs):
            return _result(meaning, semantic=True) if mode == "semantic" \
                else _result(keyword)

        rc, stdout, stderr = self._run([query], run_query)
        lines = stdout.splitlines()
        semantic_lines = [line for line in lines if "~semantic" in line]
        self.assertEqual(rc, 0)
        self.assertEqual(len(semantic_lines), 3)
        self.assertLessEqual(len(lines), compact.MAX_PAGE_HITS)
        self.assertLessEqual(sum(compact.visible_bytes(line) + 1 for line in lines),
                             compact.DEFAULT_BYTE_BUDGET)

    def test_top_semantic_duplicate_does_not_backfill_unrelated_rows(self) -> None:
        query = "find the earlier release failure explanation"
        keyword = [_hit("shared-chat", 4, "release failure explanation")]
        meaning = [
            _hit("shared-chat", 4, "different text at the same turn", semantic=True),
            _hit("other-chat", 7, "  RELEASE   failure explanation  ", semantic=True),
            _hit("shared-chat", 8, "the actual signing failure", semantic=True),
            _hit("third-chat", 9, "the checksum upload failed", semantic=True),
            _hit("fourth-chat", 10, "the wheel publisher rejected it", semantic=True),
        ]

        def run_query(_query, *, mode="keyword", **_kwargs):
            return _result(meaning, semantic=True) if mode == "semantic" \
                else _result(keyword)

        rc, stdout, _stderr = self._run([query], run_query)
        self.assertEqual(rc, 0)
        self.assertNotIn("different text at the same turn", stdout)
        self.assertNotIn("RELEASE   failure explanation", stdout)
        self.assertNotIn("the actual signing failure", stdout)
        self.assertEqual(stdout.count("~semantic"), 0)

    def test_hidden_keyword_family_corroborates_without_suppressing_meaning(self) -> None:
        lexical = [_hit(f"weak-{index}", index, "keyword evidence")
                   for index in range(4)]
        for hit in lexical:
            hit["matched"] = "all-terms"
        meaning = [_hit("weak-3", 9, "corroborated answer", semantic=True)]
        merged = search._merge_auto_semantic_hits(
            lexical, meaning, 3, family_diverse=False)
        self.assertEqual([hit["session"] for hit in merged],
                         ["weak-3", "weak-0", "weak-1"])

    def test_weak_visible_duplicate_does_not_suppress_a_strong_meaning_lead(
            self) -> None:
        lexical = [_hit("shared-chat", 4, "scattered cousin"),
                   _hit("other", 5, "keyword evidence")]
        for hit in lexical:
            hit["matched"] = "all-terms"
        meaning = [_hit("shared-chat", 9, "the verdict row", semantic=True)]
        merged = search._merge_auto_semantic_hits(
            lexical, meaning, 3, family_diverse=False)
        self.assertEqual(merged[0]["session"], "shared-chat")
        self.assertEqual(merged[0].get("lane"), "semantic")
        self.assertEqual([hit["session"] for hit in merged],
                         ["shared-chat", "other"])

    def test_weak_lexical_twin_yields_its_row_to_the_semantic_copy(self) -> None:
        lexical = [_hit("scatter", 4, "weak evidence"),
                   _hit("answer-chat", 30, "the exact verdict row"),
                   _hit("other", 5, "weak evidence")]
        for hit in lexical:
            hit["matched"] = "content-terms"
        meaning = [_hit("answer-chat", 30, "the exact verdict row", semantic=True)]
        merged = search._merge_auto_semantic_hits(
            lexical, meaning, 3, family_diverse=False)
        self.assertEqual(merged[0]["session"], "answer-chat")
        self.assertEqual(merged[0].get("lane"), "semantic")
        # shown once: the weak keyword copy yielded, not duplicated
        self.assertEqual(
            [hit["session"] for hit in merged if hit["session"] == "answer-chat"],
            ["answer-chat"])

    def test_later_hidden_agreement_can_rescue_visible_top_duplicate(self) -> None:
        lexical = [_hit("visible", 1, "keyword evidence"),
                   _hit("other", 2, "keyword evidence"),
                   _hit("third", 3, "keyword evidence"),
                   _hit("hidden-answer", 4, "keyword evidence")]
        for hit in lexical:
            hit["matched"] = "all-terms"
        meaning = [
            _hit("visible", 8, "same visible family", semantic=True),
            _hit("meaning-two", 9, "second meaning", semantic=True),
            _hit("hidden-answer", 10, "buried answer", semantic=True),
        ]
        merged = search._merge_auto_semantic_hits(
            lexical, meaning, 3, family_diverse=False)
        self.assertEqual([hit["session"] for hit in merged],
                         ["visible", "meaning-two", "hidden-answer"])

    def test_later_cross_lane_agreement_exposes_semantic_prefix_only(self) -> None:
        lexical = [_hit("keyword", 1, "keyword evidence"),
                   _hit("corroborated", 2, "keyword evidence")]
        for hit in lexical:
            hit["matched"] = "all-terms"
        meaning = [
            _hit("meaning-one", 8, "first meaning", semantic=True),
            _hit("meaning-two", 9, "second meaning", semantic=True),
            _hit("corroborated", 10, "agreed answer", semantic=True),
            _hit("noise", 11, "lower unrelated result", semantic=True),
        ]
        merged = search._merge_auto_semantic_hits(
            lexical, meaning, 3, family_diverse=False)
        self.assertEqual([hit["session"] for hit in merged],
                         ["meaning-one", "meaning-two", "corroborated"])
        self.assertNotIn("noise", [hit["session"] for hit in merged])

    def test_unavailable_semantics_preserve_keyword_output_and_exit(self) -> None:
        query = "why did the deployment keep retrying"
        keyword = [_hit("keyword-one", 1, "deployment kept retrying"),
                   _hit("keyword-two", 2, "why deployment retries")]
        calls = []

        def run_query(_query, *, mode="keyword", **_kwargs):
            calls.append(mode)
            return None if mode == "semantic" else _result(keyword)

        rc, stdout, stderr = self._run([query], run_query)
        self.assertEqual(rc, 0)
        self.assertEqual(calls.count("semantic"), 1)
        self.assertEqual(len(stdout.splitlines()), 2)
        self.assertIn("deployment kept retrying", stdout)
        self.assertNotIn("~semantic", stdout)
        self.assertEqual(
            stderr.count(surface.SEMANTIC_LANE_POLICY.keyword_only), 1)

    def test_stuck_semantic_lane_keeps_keyword_exit_within_deadline(self) -> None:
        query = "why did the deployment keep retrying"
        keyword = [_hit("keyword", 1, "deployment kept retrying")]
        entered = threading.Event()
        release = threading.Event()

        def run_query(_query, *, mode="keyword", **_kwargs):
            if mode == "semantic":
                entered.set()
                release.wait(1.0)
                return _result(
                    [_hit("meaning", 2, "late semantic evidence", semantic=True)],
                    semantic=True)
            return _result(keyword)

        started = time.monotonic()
        try:
            with mock.patch.object(search, "_AUTO_SEMANTIC_TIMEOUT_S", 0.02), \
                    mock.patch.object(search, "_semantic_runtime_installed",
                                      return_value=True):
                rc, stdout, stderr = self._run([query], run_query)
        finally:
            release.set()
        self.assertTrue(entered.is_set())
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(rc, 0)
        self.assertIn("deployment kept retrying", stdout)
        self.assertNotIn("late semantic evidence", stdout)
        self.assertEqual(
            stderr.count(surface.SEMANTIC_LANE_POLICY.keyword_only), 1)

    def test_semantic_thread_start_failure_keeps_keyword_output(self) -> None:
        query = "why did the deployment keep retrying"
        keyword = [_hit("keyword", 1, "deployment kept retrying")]
        calls = []

        def run_query(_query, *, mode="keyword", **_kwargs):
            calls.append(mode)
            return _result(keyword)

        with mock.patch.object(
                search, "_semantic_runtime_installed", return_value=True), \
                mock.patch.object(
                    search, "_start_semantic_query",
                    side_effect=RuntimeError("thread resources exhausted")):
            rc, stdout, stderr = self._run([query], run_query)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["keyword"])
        self.assertIn("deployment kept retrying", stdout)
        self.assertNotIn("Traceback", stderr)

    def test_core_only_and_complete_semantic_miss_need_no_failure_notice(self) -> None:
        query = "why did the deployment keep retrying"
        keyword = [_hit("keyword", 1, "deployment kept retrying")]
        calls = []

        def run_query(_query, *, mode="keyword", **_kwargs):
            calls.append(mode)
            return _result([], semantic=True) if mode == "semantic" else _result(keyword)

        rc, _stdout, stderr = self._run(
            [query], run_query, semantic_runtime_installed=False)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["keyword"])
        self.assertNotIn("meaning unavailable", stderr)

        calls.clear()
        rc, _stdout, stderr = self._run(
            [query], run_query, semantic_runtime_installed=True)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["semantic", "keyword"])
        self.assertNotIn("meaning unavailable", stderr)

    def test_compact_complete_semantic_miss_discloses_the_searched_scope(self) -> None:
        query = "why did the deployment keep retrying"
        coverage = {"indexed": 20, "total": 20, "pending": 0,
                    "complete": True}

        def run_query(_query, *, mode="keyword", **_kwargs):
            if mode == "semantic":
                return _result([], semantic=True, semantic_coverage=coverage)
            return _result([])

        with mock.patch.object(search, "_semantic_runtime_installed", return_value=True):
            rc, _stdout, stderr = self._run([query], run_query)
        self.assertEqual(rc, 1)
        self.assertIn(display_policy.semantic_empty_line(coverage), stderr)

    def test_partial_auto_semantic_miss_is_unverified(self) -> None:
        query = "why did the deployment keep retrying"
        coverage = {"indexed": 4, "total": 20, "pending": 16,
                    "complete": False}

        def run_query(_query, *, mode="keyword", **_kwargs):
            if mode == "semantic":
                return _result(
                    [], semantic=True, totals_exact=False, partial=True,
                    fallback_recommended=True,
                    semantic_status={
                        "state": "no-confident-match", "complete": False,
                        "fallback_recommended": True},
                    semantic_coverage=coverage)
            return _result([])

        variants = (
            ([query], True),
            ([query, "--hybrid", "--classic", "--color", "never"], False),
        )
        for argv, compact_profile in variants:
            with self.subTest(argv=argv), mock.patch.object(
                    search, "_semantic_runtime_installed", return_value=True):
                rc, _stdout, stderr = self._run(
                    argv, run_query, compact_profile=compact_profile)
            self.assertEqual(rc, 2)
            expected = (
                surface.semantic_coverage_notice(coverage)
                if compact_profile else "a floor, not the total")
            self.assertIn(expected, stderr)


    def test_total_miss_with_proven_scope_renders_one_confident_zero(self) -> None:
        query = "why did the deployment keep retrying"

        def run_query(_query, *, mode="keyword", **_kwargs):
            return _result([], semantic=True) if mode == "semantic" else _result([])

        with mock.patch.object(search, "_semantic_runtime_installed",
                               return_value=True), \
                mock.patch.object(search, "_indexed_corpus_counts",
                                  return_value={"sessions": 4912,
                                                "messages": 17_352}), \
                mock.patch.object(
                    search.indexd_runtime, "freshness_story",
                    return_value=surface.FreshnessStory("current")):
            rc, _stdout, stderr = self._run([query], run_query)
        self.assertEqual(rc, 1)
        # the exact confident wording: what the zero proved, once
        self.assertIn(
            "no match across 4,912 sessions - keyword + meaning, "
            "index current", stderr)
        # never beside the old stack it replaces
        self.assertNotIn("keyword: 0 matching rows", stderr)
        self.assertNotIn("semantic: no match among", stderr)
        self.assertNotIn(surface.SEMANTIC_LANE_POLICY.keyword_only, stderr)

    def test_lane_down_total_miss_renders_one_hedged_zero(self) -> None:
        query = "why did the deployment keep retrying"

        def run_query(_query, *, mode="keyword", **_kwargs):
            return None if mode == "semantic" else _result([])

        with mock.patch.object(search, "_semantic_runtime_installed",
                               return_value=True), \
                mock.patch.object(search, "_indexed_corpus_counts",
                                  return_value={"sessions": 12,
                                                "messages": 300}), \
                mock.patch.object(
                    search.indexd_runtime, "freshness_story",
                    return_value=surface.FreshnessStory("current")):
            rc, _stdout, stderr = self._run([query], run_query)
        self.assertEqual(rc, 1)
        self.assertIn(
            f"no match across 12 sessions - "
            f"{surface.SEMANTIC_LANE_POLICY.keyword_only}", stderr)
        self.assertNotIn("keyword: 0 matching rows", stderr)
        # the lever appears once - inside the zero, not as its own line too
        self.assertEqual(
            stderr.count(surface.SEMANTIC_LANE_POLICY.keyword_only), 1)

    def test_stale_index_total_miss_hedges_once_through_said_once(self) -> None:
        query = "why did the deployment keep retrying"
        behind = surface.FreshnessStory(
            "behind", behind_s=7200.0, changed_stores=2)
        story_line = surface.freshness_story_line(behind)

        def run_query(_query, *, mode="keyword", **_kwargs):
            return _result([], semantic=True) if mode == "semantic" else _result([])

        with mock.patch.object(search, "_semantic_runtime_installed",
                               return_value=True), \
                mock.patch.object(search, "_indexed_corpus_counts",
                                  return_value={"sessions": 12,
                                                "messages": 300}), \
                mock.patch.object(search.indexd_runtime, "freshness_story",
                                  return_value=behind), \
                mock.patch.object(search.indexd_runtime,
                                  "agent_freshness_notice",
                                  return_value=story_line):
            rc, _stdout, stderr = self._run([query], run_query)
        self.assertEqual(rc, 2)
        self.assertIn(f"no match across 12 sessions - {story_line}", stderr)
        # the freshness story is told once: inside the zero, never stacked
        self.assertEqual(stderr.count(story_line), 1)
        self.assertNotIn("index current", stderr)

    def test_a_narrowing_filter_forfeits_the_corpus_scope_claim(self) -> None:
        query = "why did the deployment keep retrying"

        def run_query(_query, *, mode="keyword", **_kwargs):
            return _result([], semantic=True) if mode == "semantic" else _result([])

        with mock.patch.object(search, "_semantic_runtime_installed",
                               return_value=True), \
                mock.patch.object(search, "_indexed_corpus_counts",
                                  return_value={"sessions": 12,
                                                "messages": 300}), \
                mock.patch.object(
                    search.indexd_runtime, "freshness_story",
                    return_value=surface.FreshnessStory("current")):
            rc, _stdout, stderr = self._run(
                [query, "--since", "7d"], run_query)
        self.assertEqual(rc, 1)
        # a filtered zero cannot claim the whole corpus was searched
        self.assertNotIn("no match across", stderr)
        self.assertIn("keyword: 0 matching rows", stderr)

    def test_tools_excluded_zero_forfeits_the_corpus_claim(self) -> None:
        query = "why did the deployment keep retrying"

        def run_query(_query, *, mode="keyword", **_kwargs):
            if mode == "semantic":
                return _result([], semantic=True)
            return _result([], tools_excluded=True, totals_exact=False)

        with mock.patch.object(search, "_semantic_runtime_installed",
                               return_value=True), \
                mock.patch.object(search, "_indexed_corpus_counts",
                                  return_value={"sessions": 12,
                                                "messages": 300}), \
                mock.patch.object(
                    search.indexd_runtime, "freshness_story",
                    return_value=surface.FreshnessStory("current")):
            rc, _stdout, stderr = self._run([query], run_query)
        self.assertEqual(rc, 2)
        # a lane that dropped tool rows searched a narrowed corpus: the zero
        # must not claim proof over the full indexed count beside it
        self.assertNotIn("no match across", stderr)
        self.assertNotIn("matching rows across", stderr)
        self.assertIn("tool output isn't indexed yet", stderr)

    def test_tool_only_zero_names_the_pending_lane_not_a_confident_zero(
            self) -> None:
        query = "cargo build failure"

        def run_query(_query, *, mode="keyword", **_kwargs):
            return _result([], engine="none", tools_excluded=True,
                           totals_exact=False)

        with mock.patch.object(search, "_indexed_corpus_counts",
                               return_value={"sessions": 12,
                                             "messages": 1680}):
            rc, _stdout, stderr = self._run(
                [query, "--who", "tool"], run_query)
        self.assertEqual(rc, 2)
        self.assertIn(surface.TOOL_QUERY_PENDING_LINE, stderr)
        self.assertNotIn("matching rows across", stderr)
        self.assertNotIn("no search index exists yet", stderr)

    def test_self_excluded_zero_forfeits_the_corpus_claim(self) -> None:
        # the automatic family exclusion narrows the corpus exactly like a
        # named filter: N would over-count, so the proof-shaped zero forfeits
        query = "why did the deployment keep retrying"

        def run_query(_query, *, mode="keyword", **_kwargs):
            return _result([], semantic=True) if mode == "semantic" else _result([])

        with mock.patch.object(search, "_semantic_runtime_installed",
                               return_value=True), \
                mock.patch.object(search, "_indexed_corpus_counts",
                                  return_value={"sessions": 12,
                                                "messages": 300}), \
                mock.patch.object(
                    search.indexd_runtime, "freshness_story",
                    return_value=surface.FreshnessStory("current")):
            rc, _stdout, stderr = self._run(
            [query], run_query, current_session="caller-session-0042",
            recap_turn=0)
        self.assertEqual(rc, 1)
        self.assertNotIn("no match across", stderr)

    def test_compact_hybrid_discloses_partial_semantic_coverage(self) -> None:
        query = "why did the deployment keep retrying"
        keyword = [_hit("keyword", 1, "deployment kept retrying")]
        meaning = [_hit("meaning", 2, "semantic evidence", semantic=True)]

        def run_query(_query, *, mode="keyword", **_kwargs):
            if mode == "semantic":
                return _result(
                    meaning, semantic=True,
                    semantic_coverage={
                        "indexed": 4, "total": 20, "pending": 16,
                        "complete": False})
            return _result(keyword)

        rc, stdout, stderr = self._run([query], run_query)
        self.assertEqual(rc, 0)
        self.assertIn("~semantic", stdout)
        self.assertIn("semantic coverage is partial: 4/20", stderr)
        self.assertIn("2+ matches (floor)", stderr)
        self.assertNotIn("more:", stderr)

    def test_partial_accelerator_is_disclosed_even_when_base_is_complete(self) -> None:
        # ask.py flags the run partial when the q8 prefix is short; the one
        # owned notice must carry that fact instead of claiming completeness
        complete = {
            "indexed": 20, "total": 20, "pending": 0, "complete": True}
        self.assertIsNone(surface.semantic_coverage_notice(
            complete, {
                "indexed": 20, "total": 20, "pending": 0, "complete": True}))
        self.assertIsNone(surface.semantic_coverage_notice(complete, None))
        short_prefix = surface.semantic_coverage_notice(
            complete, {
                "indexed": 12, "total": 20, "pending": 8, "complete": False})
        self.assertEqual(
            short_prefix,
            "semantic coverage is partial: 12 searched / 20 embedded / "
            "20 source rows")
        equal_counts = surface.semantic_coverage_notice(
            complete, {
                "indexed": 20, "total": 24, "pending": 4, "complete": False})
        self.assertIn("semantic coverage is partial", equal_counts)
        self.assertIn("20/24 accelerated rows searched", equal_counts)

    def test_compact_hybrid_discloses_partial_accelerator_coverage(self) -> None:
        query = "why did the deployment keep retrying"
        keyword = [_hit("keyword", 1, "deployment kept retrying")]
        meaning = [_hit("meaning", 2, "semantic evidence", semantic=True)]

        def run_query(_query, *, mode="keyword", **_kwargs):
            if mode == "semantic":
                return _result(
                    meaning, semantic=True,
                    semantic_coverage={
                        "indexed": 20, "total": 20, "pending": 0,
                        "complete": True},
                    semantic_accelerator_coverage={
                        "indexed": 12, "total": 20, "pending": 8,
                        "complete": False})
            return _result(keyword)

        rc, _stdout, stderr = self._run([query], run_query)
        self.assertEqual(rc, 0)
        self.assertIn(
            "semantic coverage is partial: 12 searched / 20 embedded / "
            "20 source rows", stderr)

    def test_explicit_semantic_miss_discloses_partial_coverage_when_piped(self) -> None:
        query = "agent got stuck retrying the same command"
        coverage = {"indexed": 4, "total": 20, "pending": 16,
                    "complete": False}

        def run_query(_query, *, mode="keyword", **_kwargs):
            if mode == "keyword":  # the miss page's prose-coverage count
                return _result([])
            return _result(
                [], semantic=True, totals_exact=False, partial=True,
                fallback_recommended=True,
                semantic_status={
                    "state": "no-confident-match", "complete": False,
                    "fallback_recommended": True},
                semantic_coverage=coverage)

        rc, stdout, stderr = self._run(
            [query, "-s", "--classic", "--color", "never"],
            run_query, compact_profile=False, tty_stderr=False)

        self.assertEqual(rc, 2)
        self.assertEqual(stdout, "")
        self.assertIn(display_policy.semantic_coverage_line(coverage), stderr)
        self.assertIn(display_policy.semantic_empty_line(coverage), stderr)

    def test_auto_semantic_partial_empty_is_unverified_and_disclosed(self) -> None:
        query = "why did deployment retry fail"
        coverage = {
            "indexed": 4, "total": 20, "pending": 16, "complete": False}

        def run_query(_query, *, mode="keyword", **_kwargs):
            if mode == "semantic":
                return _result(
                    [], semantic=True, semantic_coverage=coverage,
                    partial=True,
                    semantic_status={
                        "state": "no-confident-match", "complete": False,
                        "fallback_recommended": False})
            return _result([])

        rc, stdout, stderr = self._run([query], run_query)
        self.assertEqual(rc, 2)
        self.assertEqual(stdout, "")
        self.assertIn(display_policy.semantic_coverage_line(coverage), stderr)
        self.assertIn(display_policy.semantic_empty_line(coverage), stderr)
        self.assertNotIn("1+ matching rows", stderr)
        self.assertIn("0+ matches (floor)", stderr)
        self.assertNotIn("-c exact", stderr)

    def test_freshness_stamp_only_describes_incomplete_coverage(self) -> None:
        import semantic
        query = "why did the deployment keep retrying"
        meaning = [_hit("meaning", 10, "semantic evidence", semantic=True)]

        def complete(_query, *, mode="keyword", **_kwargs):
            self.assertEqual(mode, "semantic")
            return _result(meaning, semantic=True)

        with mock.patch.object(semantic, "read_embed_state", return_value={
                "finished_at": 1}) as read_state:
            rc, _stdout, stderr = self._run(
                [query, "-s", "--color", "never"], complete,
                compact_profile=False, tty_stderr=True)
        self.assertEqual(rc, 0)
        self.assertNotIn("current to", stderr)
        read_state.assert_not_called()

        def partial(_query, *, mode="keyword", **_kwargs):
            self.assertEqual(mode, "semantic")
            return _result(
                meaning, semantic=True,
                semantic_coverage={
                    "indexed": 10, "total": 20, "pending": 10,
                    "complete": False})

        with mock.patch.object(semantic, "read_embed_state", return_value={
                "finished_at": 1}) as read_state:
            rc, _stdout, stderr = self._run(
                [query, "-s", "--color", "never"], partial,
                compact_profile=False, tty_stderr=True)
        self.assertEqual(rc, 0)
        self.assertIn("current to", stderr)
        read_state.assert_called_once_with()

    def test_self_exclusion_refills_the_semantic_assist_quota(self) -> None:
        query = "why did deployment keep retrying"
        keyword = [_hit("keyword", 1, "deployment retry evidence")]
        keyword[0]["matched"] = "all-terms"
        meaning = [_hit("current", 10, "current session echo", semantic=True)]
        meaning.extend(_hit(f"meaning-{index}", index + 20,
                            f"independent meaning {index}", semantic=True)
                       for index in range(4))

        def run_query(_query, *, mode="keyword", **_kwargs):
            return _result(meaning, semantic=True) if mode == "semantic" \
                else _result(keyword)

        rc, stdout, _stderr = self._run(
            [query], run_query, current_session="current", recap_turn=10,
            excluded_match_count=1)
        self.assertEqual(rc, 0)
        self.assertNotIn("current session echo", stdout)
        self.assertEqual(stdout.count("~semantic"), 3)
        self.assertEqual(len(stdout.splitlines()), 4)

    def test_tty_semantic_assist_invalidates_keyword_only_exclusion_count(
            self) -> None:
        query = "why deployment retry failed"
        keyword = [
            _hit("current", 10, "current keyword echo"),
            _hit("independent", 2, "independent keyword evidence"),
        ]
        for hit in keyword:
            hit["matched"] = "all-terms"
        meaning = [
            _hit("current", 11, "current semantic echo", semantic=True),
            _hit("meaning", 20, "independent semantic evidence", semantic=True),
        ]

        def run_query(_query, *, mode="keyword", **_kwargs):
            if mode == "semantic":
                return _result(meaning, semantic=True)
            return _result(keyword, terms_fallback=True)

        rc, _stdout, stderr = self._run(
            [query], run_query, compact_profile=False, tty_stderr=True,
            current_session="current", recap_turn=10,
            excluded_match_count=1)
        self.assertEqual(rc, 0)
        self.assertNotIn("excluded 1 hit", stderr)

    def test_hidden_only_auto_semantic_lane_keeps_exclusion_count_unknown(
            self) -> None:
        query = "why deployment retry failed"

        def run_query(_query, *, mode="keyword", **_kwargs):
            if mode == "semantic":
                return _result([
                    _hit("current", 11, "hidden meaning", semantic=True)],
                    semantic=True)
            return _result([
                _hit("independent", 2, "visible keyword evidence")])

        rc, stdout, stderr = self._run(
            [query], run_query,
            current_session="current", recap_turn=10,
            excluded_match_count=1)
        self.assertEqual(rc, 0)
        self.assertIn("visible keyword evidence", stdout)
        self.assertNotIn("hidden meaning", stdout)
        self.assertNotIn("excluded 1 hit", stderr)

    def test_hidden_only_tty_semantic_fallback_keeps_count_unknown(self) -> None:
        query = "why deployment retry failed"

        def run_query(_query, *, mode="keyword", **_kwargs):
            if mode == "semantic":
                return _result([
                    _hit("current", 11, "hidden meaning", semantic=True)],
                    semantic=True)
            return _result([])

        rc, _stdout, stderr = self._run(
            [query, "--classic", "--color", "never"], run_query,
            compact_profile=False, tty_stderr=True,
            current_session="current", recap_turn=10,
            excluded_match_count=1)
        self.assertEqual(rc, 2)
        self.assertNotIn("excluded 1 hit", stderr)

    def test_truncated_summary_keeps_the_exact_total_and_a_copyable_deeper(self) -> None:
        hits = [_hit(f"chat-{index}", index, "deploy retry evidence")
                for index in range(5)]
        for hit in hits:
            hit["matched"] = "all-terms"

        def run_query(_query, *, mode="keyword", **_kwargs):
            return _result(hits, total=237, truncated=True)

        with mock.patch.object(
                compact, "_save_snapshot",
                wraps=compact._save_snapshot) as saved:
            rc, _stdout, stderr = self._run(
                ["deploy retry", "--lexical"], run_query)
        self.assertEqual(rc, 0)
        self.assertRegex(
            stderr,
            r"^237 matches · broader rerun \(may repeat\): agrep --deeper "
            r"m\.[A-Za-z0-9_-]{8}\n$")
        self.assertEqual(
            saved.call_args.kwargs["deeper_argv"],
            ("agrep", "--lexical", "--classic", "-n", "80",
             "--", "deploy retry"))

    def test_empty_meaning_page_says_what_the_prose_lane_knows(self) -> None:
        def run_query(_query, *, mode="keyword", **_kwargs):
            if mode == "semantic":
                return _result([], semantic=True, total=0)
            return _result([_hit("prose-0", 1, "wedged rows exist")], total=24)

        rc, _stdout, stderr = self._run(["wedged", "-s"], run_query)
        self.assertEqual(rc, 1)
        self.assertIn("no confident meaning match; 24 prose match(es) exist",
                      stderr)
        self.assertIn("--lexical", stderr)

    def test_empty_meaning_page_with_no_prose_says_so(self) -> None:
        def run_query(_query, *, mode="keyword", **_kwargs):
            return _result([], semantic=(mode == "semantic"), total=0)

        rc, _stdout, stderr = self._run(["qzxvplmbrt", "-s"], run_query)
        self.assertEqual(rc, 1)
        self.assertIn("no prose match either", stderr)

    def test_compact_weak_meaning_uses_row_markers_without_a_lecture(self) -> None:
        hits = [_hit(f"meaning-{index}", index, f"unrelated result {index}",
                     semantic=True) for index in range(3)]
        for hit in hits:
            hit["sem_score"] = 0.1

        def run_query(_query, *, mode="keyword", **_kwargs):
            self.assertEqual(mode, "semantic")
            return _result(hits, semantic=True)

        rc, stdout, stderr = self._run(["qzxvplmbrt", "-s"], run_query)
        self.assertEqual(rc, 0)
        self.assertEqual(len(stdout.splitlines()), 3)
        self.assertEqual(stdout.count("~semantic-weak"), 3)
        self.assertEqual(stderr, "")

    def test_compact_weak_meaning_page_keeps_terse_completeness(self) -> None:
        selected = [_hit(f"meaning-{index}", index, f"unrelated result {index}",
                         semantic=True) for index in range(40)]
        for hit in selected:
            hit["sem_score"] = 0.1
        unseen_strong = _hit("strong-beyond-top-40", 41, "strong result",
                             semantic=True)
        unseen_strong["sem_score"] = 0.99
        all_rows = [*selected, unseen_strong]

        def run_query(_query, *, mode="keyword", **_kwargs):
            self.assertEqual(mode, "semantic")
            return _result(
                selected, semantic=True, total=len(all_rows), truncated=True)

        rc, stdout, stderr = self._run(
            ["qzxvplmbrt", "-s", "--sort=time"], run_query)
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(len(stdout.splitlines()), compact.MIN_PAGE_HITS)
        self.assertTrue(all("~semantic-weak" in line
                            for line in stdout.splitlines()))
        self.assertRegex(
            stderr,
            r"^41 matches · more: agrep --more m\.[A-Za-z0-9_-]{8}\n$")
        self.assertNotIn("matching rows", stderr)

    def test_compact_continuation_does_not_add_weak_meaning_narration(self) -> None:
        hits = [_hit(f"strong-{index}", index, "strong result",
                     semantic=True) for index in range(4)]
        hits.extend(_hit(f"weak-{index}", index + 10, "weak result",
                         semantic=True) for index in range(8))
        for hit in hits[:4]:
            hit["sem_score"] = 0.9
        for hit in hits[4:]:
            hit["sem_score"] = 0.1

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            page = compact.start_compact(
                hits, lambda hit: hit["snippet"], {"generation": 7},
                search._RANKING_VERSION, data_dir=root, query="qzxvplmbrt",
                exact_total=len(hits))
            first_stderr = io.StringIO()
            with contextlib.redirect_stderr(first_stderr):
                search._compact_summary(page)
            self.assertNotIn("weak meaning match", first_stderr.getvalue())

            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.object(search.common, "DATA_DIR", root), \
                    mock.patch.object(
                        search.common, "transcript_generation",
                        return_value={"generation": 7}), \
                    contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(stderr):
                rc = search.main(["--more", page.handle])

        self.assertEqual(rc, 0)
        self.assertEqual(len(stdout.getvalue().splitlines()), 8)
        self.assertEqual(stderr.getvalue(), "")

    def test_compact_weak_partial_pages_keep_terse_completeness(self) -> None:
        hits = [_hit(f"partial-{index}", index, "partial result")
                for index in range(20)]
        for hit in hits:
            hit.update({"matched": "content-terms", "coverage": 0.2})

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            page = compact.start_compact(
                hits, lambda hit: hit["snippet"], {"generation": 7},
                search._RANKING_VERSION, data_dir=root, query="wordy query",
                exact_total=len(hits))
            first_stderr = io.StringIO()
            with contextlib.redirect_stderr(first_stderr):
                search._compact_summary(page)

            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.object(search.common, "DATA_DIR", root), \
                    mock.patch.object(
                        search.common, "transcript_generation",
                        return_value={"generation": 7}), \
                    contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(stderr):
                rc = search.main(["--more", page.handle])

        self.assertEqual(rc, 0)
        self.assertRegex(
            first_stderr.getvalue(),
            r"^20 matches · more: agrep --more m\.[A-Za-z0-9_-]{8}\n$")
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn("best covers", first_stderr.getvalue())
        self.assertNotIn("best covers", stderr.getvalue())
        self.assertEqual(len(stdout.getvalue().splitlines()), 4)

    def test_deeper_command_preserves_matcher_filters_and_sort(self) -> None:
        hits = [_hit(f"chat-{index}", index, "peakDetect evidence")
                for index in range(5)]

        def run_query(_query, *, mode="keyword", **_kwargs):
            self.assertEqual(mode, "word")
            return _result(hits, total=91, truncated=True)

        with mock.patch.object(
                compact, "_save_snapshot",
                wraps=compact._save_snapshot) as saved:
            rc, _stdout, stderr = self._run([
                "peakDetect", "-w", "--agent", "codex", "--project", "agrep",
                "--model", "gpt-5", "--soft", "--who", "agent",
                "--chat", "session-prefix", "--since", "7d", "--until", "1d",
                "--sort", "time", "--self", "--no-auto", "--color", "never",
            ], run_query)
        self.assertEqual(rc, 0)
        self.assertRegex(
            stderr,
            r"^91 matches · broader rerun \(may repeat\): agrep --deeper "
            r"m\.[A-Za-z0-9_-]{8}\n$")
        self.assertEqual(
            saved.call_args.kwargs["deeper_argv"],
            (
                "agrep", "-w", "--agent=codex", "--project=agrep",
                "--model=gpt-5", "--who=agent", "--chat=session-prefix",
                "--since=7d", "--until=1d", "--soft", "--sort=time",
                "--self", "--no-auto", "--color=never", "--classic",
                "-n", "80", "--", "peakDetect",
            ))

    def test_deeper_replay_keeps_compact_budget_and_repeats_leader(self) -> None:
        hits = [
            _hit(f"chat-{index}", index,
                 f"needle evidence {index} " + "x" * 420)
            for index in range(80)
        ]

        def run_query(_query, *, mode="keyword", **_kwargs):
            self.assertEqual(mode, "keyword")
            return _result(hits, total=25001, truncated=True)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            handle = compact.save_snapshot(
                [], "generation", search._RANKING_VERSION, data_dir=root,
                corpus_more=True, query="needle",
                deeper_argv=(
                    "agrep", "--lexical", "--classic", "-n", "80", "--",
                    "needle",
                ))
            stdout, stderr = _TtyBuffer(), io.StringIO()
            with mock.patch.dict(os.environ, {"AGREP_PROFILE": "classic"}), \
                    mock.patch.object(search.common, "DATA_DIR", root), \
                    mock.patch.object(
                        search.indexd_runtime, "ensure_index", return_value=True), \
                    mock.patch.object(
                        search.common, "transcript_generation",
                        return_value={"generation": 7}), \
                    mock.patch.object(
                        search.common, "in_agent_context", return_value=False), \
                    mock.patch.object(search, "run_query", side_effect=run_query), \
                    mock.patch.object(
                        search, "_stream_first_run", return_value=None), \
                    mock.patch.object(
                        explore, "_session_index", return_value={}), \
                    contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(stderr):
                rc = search.main(["--deeper", handle])

        lines = stdout.getvalue().splitlines()
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(len(lines), compact.MIN_PAGE_HITS)
        self.assertLessEqual(len(lines), compact.MAX_PAGE_HITS)
        self.assertLessEqual(
            sum(compact.visible_bytes(line) + 1 for line in lines),
            compact.DEFAULT_BYTE_BUDGET)
        self.assertTrue(lines[0].startswith("@chat-0:"))
        self.assertTrue(all(line.startswith("@") for line in lines))
        self.assertRegex(stderr.getvalue(), r"agrep --more m\.[A-Za-z0-9_-]{8}")

    def test_hybrid_deeper_handle_replays_both_evidence_lanes(self) -> None:
        keyword = [_hit("keyword", 1, "deployment retry evidence")]
        keyword[0]["matched"] = "all-terms"
        meaning = [_hit(f"meaning-{index}", index + 10,
                        f"meaning evidence {index}", semantic=True)
                   for index in range(3)]

        def run_query(_query, *, mode="keyword", **_kwargs):
            if mode == "semantic":
                return _result(meaning, semantic=True, truncated=True)
            return _result(keyword)

        rc, _stdout, stderr = self._run(
            ["why did deployment keep retrying"], run_query)
        self.assertEqual(rc, 0)
        handle = re.search(r"agrep --deeper (m\.[A-Za-z0-9_-]{8})", stderr)
        self.assertIsNotNone(handle)
        # the keyword-lane total must not label a hybrid page as exact
        self.assertNotIn("1 known", stderr)

        modes = []

        def replay(_query, *, mode="keyword", **_kwargs):
            modes.append(mode)
            return (_result(meaning, semantic=True)
                    if mode == "semantic" else _result(keyword))

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            deeper = compact.save_snapshot(
                [], "generation", search._RANKING_VERSION, data_dir=root,
                corpus_more=True, query="why did deployment keep retrying",
                deeper_argv=(
                    "agrep", "--hybrid", "--classic", "-n", "80", "--",
                    "why did deployment keep retrying",
                ))
            stdout, replay_stderr = _TtyBuffer(), io.StringIO()
            with mock.patch.dict(os.environ, {"AGREP_PROFILE": ""}), \
                    mock.patch.object(search.common, "DATA_DIR", root), \
                    mock.patch.object(
                        search.indexd_runtime, "ensure_index", return_value=True), \
                    mock.patch.object(
                        search.common, "in_agent_context", return_value=False), \
                    mock.patch.object(
                        search, "_semantic_runtime_installed", return_value=True), \
                    mock.patch.object(search, "run_query", side_effect=replay), \
                    mock.patch.object(search, "_stream_first_run", return_value=None), \
                    mock.patch.object(explore, "_session_index", return_value={}), \
                    contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(replay_stderr):
                replay_rc = search.main(["--deeper", deeper])
        self.assertEqual(replay_rc, 0)
        self.assertCountEqual(modes, ["keyword", "semantic"])
        # the all-terms row now highlights its own terms on a tty (snippet
        # invariant, goal-10 item 1) - the text survives under the marks
        replay_plain = re.sub(r"\x1b\[[0-9;]*m", "", stdout.getvalue())
        self.assertIn("deployment retry evidence", replay_plain)
        self.assertIn("meaning evidence", replay_plain)

    def test_hybrid_summary_never_invents_an_unseen_hit(self) -> None:
        query = "why did deployment keep retrying"
        keyword = [_hit("keyword", 1, "deployment retry evidence")]
        keyword[0]["matched"] = "all-terms"
        meaning = [_hit(f"meaning-{index}", index + 10,
                        f"meaning evidence {index}", semantic=True)
                   for index in range(3)]

        def run_query(_query, *, mode="keyword", **_kwargs):
            return _result(meaning, semantic=True) if mode == "semantic" \
                else _result(keyword)

        rc, stdout, stderr = self._run([query], run_query)
        self.assertEqual(rc, 0)
        self.assertEqual(len(stdout.splitlines()), 4)
        self.assertNotIn("more=no", stderr)
        self.assertEqual(stderr, "")
        self.assertNotIn("5+ matching rows", stderr)

    def test_same_chat_semantic_turn_does_not_duplicate_lexical_family(self) -> None:
        query = "find the earlier release failure explanation"
        keyword = [_hit("shared", 1, "literal release failure")]
        keyword.extend(_hit(f"keyword-{index}", index,
                            f"literal evidence {index}")
                       for index in range(2, 25))
        meaning = [_hit("shared", 99, "the actual signing failure", semantic=True)]

        def run_query(_query, *, mode="keyword", **_kwargs):
            return _result(meaning, semantic=True) if mode == "semantic" \
                else _result(keyword)

        rc, stdout, _stderr = self._run([query], run_query)
        lines = stdout.splitlines()
        self.assertEqual(rc, 0)
        self.assertEqual(sum("~semantic" in line for line in lines), 0)
        self.assertNotIn("the actual signing failure", stdout)

    def test_exact_query_shapes_never_invoke_automatic_semantics(self) -> None:
        cases = (
            (["REPLY_CAP"], "keyword"),
            (["REPLY_CAP EOF_ERROR"], "keyword"),
            (["py/search.py"], "keyword"),
            (["foo.rs bar.py"], "keyword"),
            (["deadbeefdeadbeef"], "keyword"),
            (["deployment", "-w"], "word"),
            (["deploy.*retry", "-E"], "regex"),
            (["why did deployment keep retrying", "--lexical"], "keyword"),
        )
        for argv, expected_mode in cases:
            with self.subTest(argv=argv):
                calls = []

                def run_query(query, *, mode="keyword", **_kwargs):
                    calls.append(mode)
                    return _result([_hit("literal", 1, f"literal {query}")])

                rc, _stdout, _stderr = self._run(argv, run_query)
                self.assertEqual(rc, 0)
                self.assertEqual(calls, [expected_mode])

    def test_tool_only_default_search_never_starts_semantics(self) -> None:
        query = "why did the semantic worker fail"
        calls = []

        def run_query(_query, *, mode="keyword", **_kwargs):
            calls.append(mode)
            return _result([_hit("tool", 1, "semantic worker failed")])

        rc, stdout, stderr = self._run([query, "--who", "tool"], run_query)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["keyword"])
        self.assertIn("semantic worker failed", stdout)
        self.assertNotIn("meaning unavailable", stderr)

    def test_classifier_rejects_identifier_bags_but_keeps_mixed_prose(self) -> None:
        rejected = (
            "REPLY_CAP EOF_ERROR?",
            "foo.rs bar.py?",
            "0xdeadbeef 0xcafebabe?",
            "REPLY_CAP and EOF_ERROR",
            "API HTTP",
        )
        admitted = (
            "why does REPLY_CAP keep growing",
            "OOM crash",
            "DB corruption",
            "API timeout",
        )
        self.assertTrue(all(not search._auto_semantic_query(q) for q in rejected))
        self.assertTrue(all(search._auto_semantic_query(q) for q in admitted))

        # single identifier-shaped tokens are exact lookups: filenames with
        # any extension, bare domains, and semver tags stay keyword-only
        for token in ("server.yaml", "main.cpp", "Program.cs",
                      "example.com", "v1.2.3", "v2.10.0-rc.1"):
            with self.subTest(token=token):
                policy = search.semantic_query_policy(token)
                self.assertFalse(policy["eligible"])
                self.assertEqual(policy["reason"], "identifier-query")
        for token in ("deadlock", "retrying"):
            with self.subTest(token=token):
                self.assertTrue(search.semantic_query_policy(token)["eligible"])
        identifier_bags = (
            "server.yaml main.cpp",
            "Program.cs example.com",
            "example.com v1.2.3",
        )
        self.assertTrue(all(not search._auto_semantic_query(q)
                            for q in identifier_bags))
        self.assertTrue(
            search._auto_semantic_query("why is server.yaml failing to parse"))

        for query in rejected:
            calls = []

            def run_query(_query, *, mode="keyword", **_kwargs):
                calls.append(mode)
                return _result([])

            with self.subTest(query=query):
                rc, _stdout, _stderr = self._run([query], run_query)
                self.assertEqual(rc, 1)
                self.assertEqual(calls, ["keyword"])

    def test_embeddings_off_disables_every_automatic_semantic_lane(self) -> None:
        query = "why does deployment keep retrying"
        with mock.patch.object(search.common, "setting", return_value="off"):
            self.assertFalse(search._auto_semantic_query(query))

            calls = []

            def run_query(_query, *, mode="keyword", **_kwargs):
                calls.append(mode)
                return _result([])

            rc, _stdout, _stderr = self._run([query], run_query)
        self.assertEqual(rc, 1)
        self.assertEqual(calls, ["keyword"])

    def test_classic_and_machine_surfaces_do_not_enable_hybrid(self) -> None:
        cases = (
            (["natural language recall query", "--classic", "--color", "never"],
             "classic"),
            (["natural language recall query", "--flat", "--color", "never"],
             "flat"),
            (["natural language recall query", "--json"], "json"),
            (["natural language recall query", "-c"], "count"),
            (["natural language recall query", "--count-by-tier"], "tiers"),
            (["natural language recall query", "-l", "--color", "never"],
             "chats"),
        )
        for argv, surface in cases:
            with self.subTest(surface=surface):
                calls = []

                def run_query(_query, *, mode="keyword", **_kwargs):
                    calls.append(mode)
                    hit = _hit("keyword", 1, "natural language recall query")
                    if surface == "tiers":
                        hit["_boundary_class"] = "aligned"
                    return _result([hit])

                rc, stdout, _stderr = self._run(argv, run_query)
                self.assertEqual(rc, 0)
                self.assertEqual(calls, ["keyword"])
                if surface == "json":
                    head, rows = _search_json(stdout)
                    self.assertEqual(head["engine"], "corpusdb")
                    self.assertEqual([row["session"] for row in rows],
                                     ["keyword"])
                    self.assertNotIn("engine", rows[0])
                    self.assertNotIn("semantic", rows[0])
                    self.assertNotIn("freshness", rows[0])
                    self.assertNotIn("completeness", rows[0])
                elif surface == "count":
                    self.assertEqual(stdout, "1\n")
                elif surface == "tiers":
                    self.assertEqual(
                        stdout,
                        "phrase_aligned=1 phrase_partial=0 "
                        "phrase_interior=0 all_terms=0 total=1\n")
                else:
                    self.assertIn("keyword", stdout)

    def test_default_semantic_call_keeps_the_legacy_worker_signature(self) -> None:
        calls = []

        def worker(_query, *, level, k, filters):
            calls.append((level, k, filters))
            return {
                "results": [], "truncated": False, "score_kind": "cosine",
                "semantic_coverage": {
                    "indexed": 1, "total": 1, "pending": 0,
                    "complete": True},
                "partial": False,
            }

        with mock.patch.object(semworker, "resident_status",
                               return_value={"running": True}), \
                mock.patch.object(semworker, "search_worker", side_effect=worker):
            result = search._semantic_local("deployment retry loop", 3)
        self.assertIsNotNone(result)
        self.assertEqual(len(calls), 1)

    def test_inprocess_fallback_holds_one_owner_through_resource_release(self) -> None:
        import semantic

        owner = mock.Mock()
        order = []
        payload = {
            "results": [], "truncated": False, "score_kind": "cosine",
            "semantic_coverage": {
                "indexed": 1, "total": 1, "pending": 0, "complete": True},
            "partial": False,
        }
        with mock.patch.object(
                semworker, "resident_status", return_value={"running": False}), \
                mock.patch.object(
                    semworker, "search_worker", return_value=None), \
                mock.patch.object(
                    semworker, "_worker_query_disabled", return_value=False), \
                mock.patch.object(
                    semworker, "_data_dir_readonly", return_value=False), \
                mock.patch.object(
                    semworker, "acquire_inprocess_owner",
                    side_effect=lambda: owner), \
                mock.patch.object(
                    semworker, "verify_inprocess_owner",
                    side_effect=lambda _owner: order.append("verify")), \
                mock.patch.object(
                    semantic, "search",
                    side_effect=lambda *_args, **_kwargs:
                    order.append("search") or payload), \
                mock.patch.object(
                    semantic, "release",
                    side_effect=lambda: order.append("release") or True), \
                mock.patch.object(
                    semworker, "finish_inprocess_owner",
                    side_effect=lambda *_args, **_kwargs:
                    order.append("finish")) as finish:
            result = search._semantic_local("deployment retry loop", 3)
        self.assertIsNotNone(result)
        self.assertEqual(
            order, ["verify", "search", "verify", "release", "finish"])
        finish.assert_called_once_with(owner, resources_released=True)

    def test_inprocess_fallback_never_bypasses_a_protected_owner(self) -> None:
        import semantic

        messages = []
        with mock.patch.object(
                semworker, "resident_status",
                return_value={
                    "running": False, "protected": True, "inprocess": True,
                }), \
                mock.patch.object(
                    semworker, "search_worker", return_value=None), \
                mock.patch.object(
                    semworker, "_worker_query_disabled", return_value=False), \
                mock.patch.object(
                    semworker, "_data_dir_readonly", return_value=False), \
                mock.patch.object(
                    semworker, "acquire_inprocess_owner",
                    return_value=None), \
                mock.patch.object(semantic, "search") as local, \
                mock.patch.object(
                    search.common, "log", side_effect=messages.append):
            result = search._semantic_local("deployment retry loop", 3)
        self.assertTrue(result["fallback_recommended"])
        self.assertIn(
            "semantic ownership", result["semantic_status"]["reason"])
        local.assert_not_called()
        self.assertFalse(any(
            "starting the semantic worker" in message
            or "fetching the semantic model" in message
            for message in messages))

    def test_semantic_runtime_failure_preserves_update_reason(self) -> None:
        stderr = io.StringIO()
        with mock.patch.object(
                semworker, "resident_status", return_value={"running": True}), \
                mock.patch.object(
                    semworker, "search_worker",
                    side_effect=semworker.ResidentSemanticUnavailable(
                        surface.SEMANTIC_INDEX_UPDATE_REASON)), \
                contextlib.redirect_stderr(stderr):
            result = search._semantic_local("deployment retry loop", 3)
        self.assertEqual(stderr.getvalue(), "")
        self.assertTrue(result["fallback_recommended"])
        self.assertEqual(
            result["semantic_status"]["reason"],
            surface.SEMANTIC_INDEX_UPDATE_REASON)

    def test_explicit_semantic_update_failure_is_one_line(self) -> None:
        unavailable = _result(
            [], semantic=True, fallback_recommended=True,
            semantic_status={
                "state": "unavailable", "complete": False,
                "fallback_recommended": True,
                "reason": surface.SEMANTIC_INDEX_UPDATE_REASON,
            })

        def run_query(_query, *, mode="keyword", **_kwargs):
            self.assertEqual(mode, "semantic")
            return unavailable

        rc, stdout, stderr = self._run(
            ["deployment retry loop", "-s"], run_query,
            compact_profile=False)
        self.assertEqual(rc, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr,
            "semantic search unavailable: index update in progress; "
            "retry shortly\n")

        rc, stdout, stderr = self._run(
            ["deployment retry loop", "-s", "--json"], run_query,
            compact_profile=False)
        self.assertEqual(rc, 2)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertEqual(payload["error"], {"code": "semantic-unavailable"})
        self.assertEqual(payload["semantic"], unavailable["semantic_status"])

    def test_automatic_semantic_call_passes_its_bounded_deadlines(self) -> None:
        kwargs = {}

        def worker(_query, **values):
            kwargs.update(values)
            return {
                "results": [], "truncated": False, "score_kind": "cosine",
                "semantic_coverage": {
                    "indexed": 1, "total": 1, "pending": 0,
                    "complete": True},
                "partial": False,
            }

        with mock.patch.object(semworker, "resident_status",
                               return_value={"running": True}), \
                mock.patch.object(semworker, "search_worker", side_effect=worker):
            result = search._semantic_local(
                "deployment retry loop", 3,
                timeout_s=search._AUTO_SEMANTIC_TIMEOUT_S)
        self.assertIsNotNone(result)
        self.assertGreater(kwargs["timeout_s"], 0)
        self.assertLessEqual(kwargs["timeout_s"], search._AUTO_SEMANTIC_TIMEOUT_S)
        self.assertEqual(kwargs["start_timeout_s"], search._AUTO_SEMANTIC_START_S)

    def test_automatic_semantic_deadline_preserves_agent_exit_bound(self) -> None:
        expected_timeout = 1.25 if search.common.WIN else 0.75
        expected_start = 0.50 if search.common.WIN else 0.35
        self.assertEqual(search._AUTO_SEMANTIC_TIMEOUT_S, expected_timeout)
        self.assertEqual(search._AUTO_SEMANTIC_START_S, expected_start)

    def test_guarded_explicit_semantic_returns_the_child_result(self) -> None:
        result = {"engine": "semantic:hybrid", "hits": []}
        process = mock.Mock()
        process.pid = 41
        process.returncode = 0
        process.communicate.return_value = (
            json.dumps(result).encode("utf-8"), b"")
        process.stdin = mock.Mock()
        process.stdout = mock.Mock()
        process.stderr = mock.Mock()
        with mock.patch.object(
                search, "_guarded_semantic_query", return_value=result) as guarded:
            search.run_query(
                "deployment retry loop", mode="semantic", limit=7,
                semantic_timeout_s=5.0, semantic_process_guard=True)
        spec = guarded.call_args.args[0]

        with mock.patch.object(search.subprocess, "Popen", return_value=process) as popen, \
                mock.patch.object(
                    search.common, "process_start_identity",
                    return_value="birth"), \
                mock.patch.object(
                    search, "_stop_semantic_subprocess",
                    return_value=True) as stop:
            self.assertEqual(search._guarded_semantic_query(spec), result)

        command, = popen.call_args.args
        self.assertEqual(command, [
            search.sys.executable, os.path.abspath(search.__file__),
            search._SEMANTIC_CHILD_ARG,
        ])
        self.assertEqual(
            popen.call_args.kwargs["env"]["AGREP_DATA_READONLY"],
            os.fspath(search.common.DATA_DIR))
        self.assertEqual(
            json.loads(process.communicate.call_args.kwargs["input"]),
            {"q": "deployment retry loop", "limit": 7})
        self.assertEqual(stop.call_args.args[:2], (process, "birth"))
        if search.common.WIN:
            self.assertIn("creationflags", popen.call_args.kwargs)
        else:
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
        process.stdin.close.assert_called_once_with()
        process.stdout.close.assert_called_once_with()
        process.stderr.close.assert_called_once_with()

    def test_guarded_explicit_semantic_child_is_read_only_and_tree_bound(self) -> None:
        result = {"engine": "semantic:hybrid", "hits": []}
        request = io.BytesIO(json.dumps({
            "q": "deployment retry loop", "limit": 3,
        }).encode("utf-8"))
        response = io.BytesIO()
        stdin = mock.Mock(buffer=request)
        stdout = mock.Mock(buffer=response)
        with mock.patch.object(search.sys, "stdin", stdin), \
                mock.patch.object(search.sys, "stdout", stdout), \
                mock.patch.object(search.common, "data_dir_readonly", return_value=True), \
                mock.patch.object(
                    search.common, "bind_descendants_to_process_lifetime",
                    return_value=True) as bind, \
                mock.patch.object(
                    search, "run_query", return_value=result) as run:
            self.assertEqual(search._explorer_semantic_child_main(), 0)

        bind.assert_called_once_with()
        run.assert_called_once_with(
            "deployment retry loop", mode="semantic", limit=3,
            allow_model_download=False)
        self.assertEqual(json.loads(response.getvalue()), result)

    def test_guarded_explicit_semantic_timeout_stops_the_child(self) -> None:
        process = mock.Mock()
        process.pid = 41
        process.communicate.side_effect = subprocess.TimeoutExpired(
            "semantic", 0.05)
        process.stdin = mock.Mock()
        process.stdout = mock.Mock()
        process.stderr = mock.Mock()
        with mock.patch.object(search.subprocess, "Popen", return_value=process), \
                mock.patch.object(
                    search.common, "process_start_identity",
                    return_value="birth"), \
                mock.patch.object(
                    search, "_stop_semantic_subprocess",
                    return_value=True) as stop, \
                self.assertRaises(search.SemanticQueryTimeoutError):
            search.run_query(
                "deployment retry loop", mode="semantic",
                semantic_timeout_s=0.05, semantic_process_guard=True)

        self.assertEqual(stop.call_args.args[:2], (process, "birth"))
        self.assertGreaterEqual(stop.call_args.args[2], 0.0)

    def test_guarded_explicit_semantic_timeout_drains_the_bound_tree(self) -> None:
        process = mock.Mock()
        process.pid = 41
        process.poll.return_value = None
        with mock.patch.object(
                search.common, "terminate_exact_process_tree",
                return_value=True) as terminate:
            self.assertTrue(search._stop_semantic_subprocess(
                process, "birth", 1.0))

        terminate.assert_called_once()
        self.assertEqual(terminate.call_args.args, (41, "birth"))
        self.assertLessEqual(terminate.call_args.kwargs["wait_s"], 1.0)
        self.assertTrue(terminate.call_args.kwargs["require_bound_tree"])
        self.assertEqual(terminate.call_args.kwargs["term_grace_s"], 0.1)
        process.kill.assert_not_called()

    def test_exited_semantic_root_still_requires_tree_proof(self) -> None:
        process = mock.Mock(pid=41)
        with mock.patch.object(
                search.common, "terminate_exact_process_tree",
                return_value=False) as terminate, \
                mock.patch.object(search.common, "WIN", True):
            self.assertFalse(search._stop_semantic_subprocess(
                process, "birth", 1.0))
        terminate.assert_called_once()
        process.kill.assert_not_called()

    def test_guarded_explicit_semantic_reports_an_undrained_tree(self) -> None:
        process = mock.Mock()
        process.pid = 41
        process.communicate.side_effect = subprocess.TimeoutExpired(
            "semantic", 0.05)
        process.stdin = mock.Mock()
        process.stdout = mock.Mock()
        process.stderr = mock.Mock()
        with mock.patch.object(search.subprocess, "Popen", return_value=process), \
                mock.patch.object(
                    search.common, "process_start_identity",
                    return_value="birth"), \
                mock.patch.object(
                    search, "_stop_semantic_subprocess",
                    return_value=False), \
                self.assertRaisesRegex(
                    search.SemanticQueryWorkerError, "could not be drained"):
            search.run_query(
                "deployment retry loop", mode="semantic",
                semantic_timeout_s=0.05, semantic_process_guard=True)


class RecallHybridTests(unittest.TestCase):
    @staticmethod
    def _keyword(session: str, *, fallback: bool = False,
                 evidence: float = 0.87) -> dict:
        # _evidence is the lexical half of the score search records on every
        # ranked row; the default is a lived bag-of-words match's band.
        hit = _hit(session, 3, "keyword evidence")
        hit["matched"] = "all-terms" if fallback else "phrase"
        hit["_evidence"] = evidence
        return hit

    @staticmethod
    def _window(requests):
        return [{
            "session": session, "center": turn,
            "first_turn": turn, "last_turn": turn,
            "agent": "codex", "project": "agrep",
            "turns": [{"turn": turn, "ts": 1, "who": "user",
                       "text": "evidence", "reply": ""}],
            "events": [],
        } for session, turn, _context in requests]

    def test_recall_can_inject_a_quality_gate_timeout(self) -> None:
        captured = {}
        pending = object()

        def start(query, kwargs):
            captured.update({"query": query, **kwargs})
            return pending

        keyword = _result([self._keyword("literal")], phrase_chats=1)
        meaning = _result(
            [_hit("meaning", 7, "semantic rescue", semantic=True)], semantic=True)
        with mock.patch.object(recall.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(recall.common, "in_agent_context", return_value=False), \
                mock.patch.object(search, "_semantic_runtime_installed",
                                  return_value=True), \
                mock.patch.object(search, "_start_semantic_query", side_effect=start), \
                mock.patch.object(search, "_finish_semantic_query",
                                  return_value=meaning), \
                mock.patch.object(search, "run_query", return_value=keyword), \
                mock.patch.object(explore, "get_windows", side_effect=self._window), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = recall.main([
                "deployment retry loop", "--hits", "2", "--json",
                "--budget", "4000"], auto_semantic_timeout_s=2.5)
        self.assertEqual(rc, 0)
        self.assertEqual(captured["query"], "deployment retry loop")
        self.assertEqual(captured["semantic_timeout_s"], 2.5)

    def test_recall_renders_the_shared_accelerator_coverage_notice(self) -> None:
        # base index complete, q8 prefix short: recall's page must disclose
        # through the same owned notice search renders, never stay silent
        def run_query(_query, *, mode="keyword", **_kwargs):
            return _result(
                [_hit("meaning", 7, "semantic evidence", semantic=True)],
                semantic=True,
                semantic_coverage={
                    "indexed": 20, "total": 20, "pending": 0,
                    "complete": True},
                semantic_accelerator_coverage={
                    "indexed": 12, "total": 20, "pending": 8,
                    "complete": False})

        stderr = io.StringIO()
        with mock.patch.object(recall.indexd_runtime, "ensure_index",
                               return_value=True), \
                mock.patch.object(recall.common, "in_agent_context",
                                  return_value=False), \
                mock.patch.object(search, "run_query", side_effect=run_query), \
                mock.patch.object(explore, "get_windows",
                                  side_effect=self._window), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(stderr):
            rc = recall.main(["deployment retry loop", "-s", "--budget", "0"])
        self.assertEqual(rc, 0)
        self.assertIn(surface.semantic_coverage_notice(
            {"indexed": 20, "total": 20, "pending": 0, "complete": True},
            {"indexed": 12, "total": 20, "pending": 8, "complete": False}),
            stderr.getvalue())

    def test_probe_prefers_a_confident_semantic_rescue_over_weak_lexical(self) -> None:
        calls = []

        def run_query(_query, *, mode="keyword", **kwargs):
            calls.append((mode, kwargs.get("who")))
            if mode == "semantic":
                return _result(
                    [_hit("meaning", 7, "semantic rescue", semantic=True)],
                    semantic=True)
            if kwargs.get("who") == "tool":
                return _result([], phrase_chats=0)
            return _result([self._keyword("weak", fallback=True)],
                           phrase_chats=0, terms_fallback=True)

        stdout = io.StringIO()
        with mock.patch.object(recall.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(recall.common, "in_agent_context", return_value=False), \
                mock.patch.object(search, "_semantic_runtime_installed",
                                  return_value=True), \
                mock.patch.object(search, "run_query", side_effect=run_query), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = recall.main(["deployment retry loop", "--probe"])
        self.assertEqual(rc, 0)
        # a weak-only page is not "fill": the tool lane runs before the rescue
        self.assertEqual(calls, [("keyword", None), ("keyword", "tool"),
                                 ("semantic", None)])
        self.assertIn(
            display_policy.probe_pointer_label(
                {"who": "user"}, semantic=True, weak=False),
            stdout.getvalue())
        self.assertIn("@meaning:7", stdout.getvalue())

    def test_probe_keyword_only_all_terms_hit_is_a_confident_pointer(self) -> None:
        # panel 2: plain keyword search answered first try, yet the probe
        # reported a miss because its gate expected semantic confirmation
        def run_query(_query, *, mode="keyword", **kwargs):
            if mode == "semantic":
                return None  # the meaning lane is down
            if kwargs.get("who") == "tool":
                return _result([], phrase_chats=0)
            return _result([self._keyword("past", fallback=True)],
                           phrase_chats=0)

        stdout = io.StringIO()
        with mock.patch.object(recall.indexd_runtime, "ensure_index",
                               return_value=True), \
                mock.patch.object(recall.common, "in_agent_context",
                                  return_value=False), \
                mock.patch.object(search, "_semantic_runtime_installed",
                                  return_value=True), \
                mock.patch.object(search, "run_query", side_effect=run_query), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = recall.main(["deployment retry loop", "--probe"])
        self.assertEqual(rc, 0)
        self.assertNotIn("no confident past-context pointer",
                         stdout.getvalue())
        self.assertIn("@past:3", stdout.getvalue())
        self.assertIn("hold", stdout.getvalue())  # "holds every query term"

    def test_probe_all_terms_stays_weak_when_the_meaning_lane_served(self) -> None:
        weak_semantic = _hit("meaning", 7, "vaguely related", semantic=True)
        weak_semantic["sem_score"] = recall.PROBE_MIN_SEM - 0.05
        line = recall._probe_line(
            ["needle"],
            [self._keyword("weak", fallback=True), weak_semantic],
            "corpusdb", total_sessions=0,
            session_index=("weak", "meaning"))
        self.assertIsNone(line)

    def test_probe_misses_when_the_only_row_is_substring_scatter(self) -> None:
        # the bag-of-words lane matches substrings, so unrelated prose can hold
        # every query token; below the floor the honest answer is "no"
        floor = search.SCATTER_MIN_EVIDENCE
        scatter = self._keyword("scatter", fallback=True, evidence=floor - 0.01)
        self.assertIsNone(recall._probe_line(
            ["needle"], [scatter], "corpusdb", total_sessions=0,
            session_index=("scatter",)))
        lived = self._keyword("lived", fallback=True, evidence=floor)
        self.assertIsNotNone(recall._probe_line(
            ["needle"], [lived], "corpusdb", total_sessions=0,
            session_index=("lived",)))

    def test_probe_cannot_judge_an_unscored_scatter_row(self) -> None:
        unscored = self._keyword("unscored", fallback=True)
        unscored.pop("_evidence")
        self.assertIsNone(recall._probe_line(
            ["needle"], [unscored], "corpusdb", total_sessions=0,
            session_index=("unscored",)))

    def test_probe_keeps_same_family_sidechat_outside_the_caller_window(self) -> None:
        family = recall.common.CallingFamily(
            "root", "root", frozenset({"root", "child"}), resolved=True,
            recap_turn=9)
        labeled = self._keyword("child")
        labeled["turn"] = 2  # inside the family, before the recap boundary

        def run_query(_query, *, mode="keyword", **kwargs):
            if mode == "semantic":
                return None
            return _result([labeled], phrase_chats=1)

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(recall.indexd_runtime, "ensure_index",
                               return_value=True), \
                mock.patch.object(recall.common, "in_agent_context",
                                  return_value=True), \
                mock.patch.object(session_context, "calling_family",
                                  return_value=family), \
                mock.patch.object(recall.common, "index_summary",
                                  return_value={"sessions": 4}), \
                mock.patch.object(search, "_self_exclusion_match_keys",
                                  return_value=set()), \
                mock.patch.object(search, "run_query", side_effect=run_query), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            rc = recall.main(["needle", "--probe"])
        self.assertEqual(rc, 0)
        self.assertIn("child", stdout.getvalue())
        self.assertNotIn("excluded", stderr.getvalue())
        self.assertNotIn("--self", stderr.getvalue())

    def test_probe_miss_emits_owned_line_and_warns_when_history_is_stale(self) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(recall.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(recall.common, "in_agent_context", return_value=True), \
                mock.patch.object(session_context, "calling_family", return_value=None), \
                mock.patch.object(
                    recall.indexd_runtime,
                    "agent_freshness_notice",
                    return_value="history may be stale - run `agrep doctor`",
                ), \
                mock.patch.object(recall.common, "index_summary",
                                  return_value={"sessions": 4, "messages": 40}), \
                mock.patch.object(search, "run_query", return_value=_result([])), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            rc = recall.main(["needle", "--probe", "--lexical"])
        self.assertEqual(rc, 1)
        # rc 1 still means no confident pointer, but never means no output
        self.assertEqual(
            stdout.getvalue().strip(),
            display_policy.probe_miss_line("corpusdb", corpus_sessions=4))
        self.assertIn("history may be stale", stderr.getvalue())

    def test_probe_applies_a_proven_current_window_before_session_top_k(self) -> None:
        family = recall.common.CallingFamily(
            "root", "root", frozenset({"root", "child"}), True, 3)
        captured = {}

        def run_query(_query, *, mode="keyword", **kwargs):
            captured.update(kwargs)
            return _result([self._keyword("past")], phrase_chats=1)

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(recall.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(recall.common, "in_agent_context", return_value=True), \
                mock.patch.object(session_context, "calling_family",
                                  return_value=family), \
                mock.patch.object(search, "_self_exclusion_match_keys",
                                  return_value=set()), \
                mock.patch.object(search, "run_query", side_effect=run_query), \
                mock.patch.object(explore, "_session_index",
                                  return_value={"root": {}, "child": {}, "past": {}}), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            rc = recall.main(["needle", "--probe", "--lexical"])
        self.assertEqual(rc, 0)
        # The engine excludes the caller before the bounded provenance
        # lookahead; the rendered probe still selects one pointer.
        self.assertEqual(captured["session_limit"], 8)
        self.assertEqual(captured["exclude_session"], "root")
        self.assertEqual(captured["exclude_session_from_turn"], 3)
        self.assertIn("@past:3", stdout.getvalue())
        self.assertNotIn("excluded", stderr.getvalue())
        self.assertNotIn("--self", stderr.getvalue())

    def test_recall_preexcludes_only_a_proven_scope_before_family_heads(
            self) -> None:
        cases = (
            ("window", 7, (), (), "ordinary-child"),
            ("no-recap", None, (), (), "ordinary-child"),
            ("forced", None, ("--no-self",), (), "other"),
        )
        for name, recap_turn, extra, excluded, candidate_session in cases:
            family = recall.common.CallingFamily(
                "root", "root",
                frozenset({"root", "ordinary-child", "agent-echo"}),
                resolved=True, recap_turn=recap_turn)
            captured = []

            def candidates(spec):
                captured.append(spec)
                return search.LaneResult(
                    [self._keyword(candidate_session)], "fixture",
                    pre_ranked=True)

            stdout = io.StringIO()
            with self.subTest(policy=name), \
                    mock.patch.object(
                        recall.indexd_runtime, "ensure_index",
                        return_value=True), \
                    mock.patch.object(
                        recall.common, "in_agent_context", return_value=True), \
                    mock.patch.object(
                        session_context, "calling_family",
                        return_value=family), \
                    mock.patch.object(
                        recall.common, "indexed_self_exclusion_has_rows",
                        return_value=True), \
                    mock.patch.object(
                        search, "_keyword_candidates",
                        side_effect=candidates), \
                    mock.patch.object(
                        search, "_family_roots_for_hits",
                        side_effect=lambda hits: {
                            hit["session"]: (
                                "root" if hit["session"] == "ordinary-child"
                                else hit["session"])
                            for hit in hits}), \
                    mock.patch.object(
                        explore, "get_windows", side_effect=self._window), \
                    mock.patch.object(
                        recall, "_expand",
                        side_effect=lambda pairs, *_args: pairs), \
                    mock.patch.object(
                        recall.common, "indexed_session_prefix_candidates",
                        return_value=("root", "ordinary-child",
                                      "agent-echo", "other")), \
                    mock.patch.object(
                        recall.indexd_runtime, "agent_freshness_notice",
                        return_value=None), \
                    contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = recall.main([
                    "needle", "--lexical", "--hits", "1", "--budget", "0",
                    "--no-auto", "--color", "never", *extra])

            self.assertEqual(rc, 0)
            self.assertGreaterEqual(len(captured), 1)
            spec = captured[0]
            self.assertEqual(spec.excluded_sessions, excluded)
            if name == "no-recap":
                self.assertIsNone(spec.exclude_session)
                self.assertIsNone(spec.exclude_session_from_turn)
            else:
                self.assertEqual(spec.exclude_session, "root")
                self.assertEqual(spec.exclude_session_from_turn, recap_turn)
            self.assertIn("evidence", stdout.getvalue())

    def test_recall_window_labels_old_and_family_hits_and_excludes_echo(self) -> None:
        family = recall.common.CallingFamily(
            "root", "root", frozenset({"root", "child"}), True, 5)
        captured = {}
        hits = [
            _hit("root", 3, "old caller evidence"),
            _hit("root", 5, "recap echo"),
            _hit("child", 9, "side evidence"),
            _hit("other", 11, "independent evidence"),
        ]

        def run_query(_query, *, mode="keyword", **kwargs):
            captured.update(kwargs)
            return _result(hits, phrase_chats=4)

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(recall.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(recall.common, "in_agent_context", return_value=True), \
                mock.patch.object(session_context, "calling_family",
                                  return_value=family), \
                mock.patch.object(search, "_self_exclusion_match_keys",
                                  return_value=_excluded_keys(1)), \
                mock.patch.object(search, "run_query", side_effect=run_query), \
                mock.patch.object(explore, "get_windows",
                                  side_effect=self._window), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            rc = recall.main(["needle", "--lexical", "--hits", "4"])
        self.assertEqual(rc, 0)
        self.assertEqual(captured["exclude_session_from_turn"], 5)
        self.assertNotIn("recap echo", stdout.getvalue())
        self.assertEqual(stdout.getvalue().count("~self"), 1)
        self.assertIn("@child:9", stdout.getvalue())
        # F1: one short owned line, not the mechanism lecture
        self.assertEqual(
            stderr.getvalue().count(
                "excluded 1 hit from the current window"), 1)
        self.assertNotIn("--self", stderr.getvalue())

    def test_recall_engine_filtered_empty_page_discloses_each_self_policy(
            self) -> None:
        raw = [self._keyword("root")]
        cases = (
            ([], 3, 1, 1, "excluded 1 hit from the current window"),
            # An unresolved automatic boundary fails open and stays silent.
            ([], None, 0, 0, None),
            (["--no-self"], 3, 1, 1,
             "excluded 1 hit from this session family"),
        )
        for extra, recap_turn, excluded_count, expected_rc, expected in cases:
            with self.subTest(extra=extra, recap_turn=recap_turn):
                family = recall.common.CallingFamily(
                    "root", "root", frozenset({"root"}), True, recap_turn)
                captured = {}

                def run_query(_query, *, mode="keyword", **kwargs):
                    captured.update(kwargs)
                    boundary = kwargs.get("exclude_session_from_turn")
                    kept = [
                        hit for hit in raw
                        if not (
                            hit["session"] == kwargs.get("exclude_session")
                            and (boundary is None or hit["turn"] >= boundary)
                        )
                    ]
                    return _result(kept, phrase_chats=len(kept))

                stdout, stderr = io.StringIO(), io.StringIO()
                with mock.patch.object(
                        recall.indexd_runtime, "ensure_index",
                        return_value=True), \
                        mock.patch.object(
                            recall.indexd_runtime, "agent_freshness_notice",
                            return_value=""), \
                        mock.patch.object(
                            recall.common, "in_agent_context",
                            return_value=True), \
                        mock.patch.object(
                            session_context, "calling_family",
                            return_value=family), \
                        mock.patch.object(
                            search, "_self_exclusion_match_keys",
                            side_effect=lambda _q, _mode, query_kwargs,
                            _policy, **_kwargs: _excluded_keys(
                                excluded_count
                                if query_kwargs.get("include_tools") is False
                                else 0)), \
                        mock.patch.object(
                            search, "run_query", side_effect=run_query), \
                        mock.patch.object(
                            explore, "get_windows", side_effect=self._window), \
                        mock.patch.object(
                            explore, "_session_index", return_value={}), \
                        contextlib.redirect_stdout(stdout), \
                        contextlib.redirect_stderr(stderr):
                    rc = recall.main([
                        "needle", "--lexical", "--hits", "1", *extra])
                self.assertEqual(
                    rc, expected_rc,
                    (stdout.getvalue(), stderr.getvalue(), captured))
                if excluded_count:
                    self.assertEqual(stdout.getvalue(), "")
                    self.assertEqual(captured["exclude_session"], "root")
                    self.assertEqual(stderr.getvalue().count(expected), 1)
                else:
                    self.assertIn("@root:3", stdout.getvalue())
                    self.assertNotIn("exclude_session", captured)
                    self.assertEqual(stderr.getvalue(), "")
                self.assertNotIn("--self", stderr.getvalue())

    def test_recall_real_keyword_engine_discloses_empty_window_filter(
            self) -> None:
        family = recall.common.CallingFamily(
            "root", "root", frozenset({"root"}), True, 7)
        stdout, stderr = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            database = Path(td) / "corpus.db"
            _self_only_database(database)

            def connect(**_kwargs):
                return sqlite3.connect(database)

            with mock.patch.object(
                    recall.indexd_runtime, "ensure_index",
                    return_value=True), \
                    mock.patch.object(
                        recall.indexd_runtime, "agent_freshness_notice",
                        return_value=""), \
                    mock.patch.object(
                        recall.common, "in_agent_context",
                        return_value=True), \
                    mock.patch.object(
                        session_context, "calling_family",
                        return_value=family), \
                    mock.patch.object(
                        recall.common, "indexed_self_exclusion_has_rows",
                        return_value=True), \
                    mock.patch.object(
                        corpusdb, "connect", side_effect=connect), \
                    mock.patch.object(
                        explore, "_session_index", return_value={}), \
                    contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(stderr):
                rc = recall.main([
                    "needle", "--lexical", "--hits", "1"])
        self.assertEqual(rc, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            stderr.getvalue().count(
                "excluded 1 hit from the current window"), 1)
        self.assertNotIn("--self", stderr.getvalue())

    def test_recall_window_policy_is_silent_on_independent_page(
            self) -> None:
        family = recall.common.CallingFamily(
            "root", "root", frozenset({"root"}), True, 3)
        hit = self._keyword("other")
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(
                recall.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(
                    recall.indexd_runtime, "agent_freshness_notice",
                    return_value=""), \
                mock.patch.object(
                    recall.common, "in_agent_context", return_value=True), \
                mock.patch.object(
                    session_context, "calling_family", return_value=family), \
                mock.patch.object(
                    search, "_self_exclusion_match_keys", return_value=set()), \
                mock.patch.object(
                    search, "run_query",
                    return_value=_result([hit], phrase_chats=1)), \
                mock.patch.object(
                    explore, "get_windows", side_effect=self._window), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            rc = recall.main(["needle", "--lexical", "--hits", "1"])
        self.assertEqual(rc, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn("~self", stdout.getvalue())

    def test_hidden_recall_tool_event_does_not_corrupt_self_exclusion_state(
            self) -> None:
        family = recall.common.CallingFamily(
            "root", "root", frozenset({"root"}), True, 3)
        hit = self._keyword("other")

        def window(requests):
            rendered = self._window(requests)
            rendered[0]["events"] = [{
                "turn": 3, "ts": 1, "kind": "tool", "name": "shell",
                "input": "printf needle", "output": "needle", "ok": True,
                "output_chars": 6,
            }]
            return rendered

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(
                recall.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(
                    recall.indexd_runtime, "agent_freshness_notice",
                    return_value=""), \
                mock.patch.object(
                    recall.common, "in_agent_context", return_value=True), \
                mock.patch.object(
                    session_context, "calling_family", return_value=family), \
                mock.patch.object(
                    search, "run_query",
                    return_value=_result([hit], phrase_chats=1)), \
                mock.patch.object(
                    explore, "get_windows", side_effect=window), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            rc = recall.main(["needle", "--lexical", "--hits", "1"])
        self.assertEqual(rc, 0)
        self.assertNotIn("shell", stdout.getvalue())
        self.assertIn(
            "[+1 tool calls - agrep around other 3 -C 0 --tool-output 200]",
            stdout.getvalue())
        self.assertNotIn("--full", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")

    def test_recall_window_trims_in_window_context_from_old_hit(self) -> None:
        family = recall.common.CallingFamily(
            "root", "root", frozenset({"root"}), True, 5)
        hit = _hit("root", 3, "old caller evidence")

        def window(_requests):
            return [{
                "session": "root", "center": 3,
                "first_turn": 2, "last_turn": 6,
                "agent": "codex", "project": "agrep",
                "turns": [
                    {"turn": turn, "ts": turn, "who": "user",
                     "text": f"turn-{turn}", "reply": ""}
                    for turn in (2, 3, 5, 6)
                ],
                "events": [
                    {"turn": 5, "kind": "tool", "name": "echo",
                     "input": "current", "ok": True, "ts": 5,
                     "output_chars": 0}
                ],
            }]

        stdout = io.StringIO()
        with mock.patch.object(recall.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(recall.common, "in_agent_context", return_value=True), \
                mock.patch.object(session_context, "calling_family",
                                  return_value=family), \
                mock.patch.object(search, "run_query",
                                  return_value=_result([hit], phrase_chats=1)), \
                mock.patch.object(explore, "get_windows", side_effect=window), \
                mock.patch.object(recall, "_expand", side_effect=lambda pairs, *_: pairs), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = recall.main(["needle", "--lexical", "--hits", "1"])
        self.assertEqual(rc, 0)
        self.assertIn("turn-3", stdout.getvalue())
        self.assertNotIn("turn-5", stdout.getvalue())
        self.assertNotIn("turn-6", stdout.getvalue())
        self.assertNotIn("current", stdout.getvalue())

    def test_recall_own_chat_overrides_window_exclusion(self) -> None:
        family = recall.common.CallingFamily(
            "root", "root", frozenset({"root"}), True, 5)
        captured = {}
        hit = _hit("root", 8, "current evidence")

        def run_query(_query, *, mode="keyword", **kwargs):
            captured.update(kwargs)
            return _result([hit], phrase_chats=1)

        stdout = io.StringIO()
        with mock.patch.object(recall.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(recall.common, "in_agent_context", return_value=True), \
                mock.patch.object(session_context, "calling_family",
                                  return_value=family), \
                mock.patch.object(search, "_resolve_chat", return_value="root"), \
                mock.patch.object(search, "run_query", side_effect=run_query), \
                mock.patch.object(explore, "get_windows",
                                  side_effect=self._window), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = recall.main([
                "needle", "--chat", "root", "--lexical", "--hits", "1"])
        self.assertEqual(rc, 0)
        self.assertNotIn("exclude_session", captured)
        self.assertIn("@root:8", stdout.getvalue())
        self.assertNotIn("~self", stdout.getvalue())

    def test_recall_json_uses_the_same_window_policy(self) -> None:
        family = recall.common.CallingFamily(
            "root", "root", frozenset({"root"}), True, 5)
        captured = {}
        current = _hit("root", 8, "current evidence")
        other = _hit("other", 8, "independent evidence")

        def run_query(_query, *, mode="keyword", **kwargs):
            captured.update(kwargs)
            return _result([current, other], phrase_chats=2)

        stdout = io.StringIO()
        with mock.patch.object(recall.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(recall.common, "in_agent_context", return_value=True), \
                mock.patch.object(session_context, "calling_family",
                                  return_value=family), \
                mock.patch.object(search, "_self_exclusion_match_keys",
                                  return_value=_excluded_keys(1)), \
                mock.patch.object(search, "run_query", side_effect=run_query), \
                mock.patch.object(explore, "get_windows",
                                  side_effect=self._window), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = recall.main([
                "needle", "--json", "--lexical", "--hits", "1"])
        self.assertEqual(rc, 0)
        self.assertEqual(captured["exclude_session"], "root")
        self.assertEqual(captured["exclude_session_from_turn"], 5)
        payload = json.loads(stdout.getvalue())
        self.assertEqual([hit["session"] for hit in payload["hits"]], ["other"])
        self.assertNotIn("self", payload["hits"][0])
        self.assertEqual(payload["self_exclusion"]["excluded_hits"], 1)

    def test_explicit_semantic_unavailable_never_falls_back_to_keyword(self) -> None:
        calls = []

        def run_query(_query, *, mode="keyword", **_kwargs):
            calls.append(mode)
            return None

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(recall.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(search, "run_query", side_effect=run_query), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            rc = recall.main(["deployment retry loop", "-s"])
        self.assertEqual(rc, 2)
        self.assertEqual(calls, ["semantic"])
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("semantic search unavailable", stderr.getvalue())

    def test_recall_semantic_update_failure_is_one_line(self) -> None:
        unavailable = _result(
            [], semantic=True, fallback_recommended=True,
            semantic_status={
                "state": "unavailable", "complete": False,
                "fallback_recommended": True,
                "reason": surface.SEMANTIC_INDEX_UPDATE_REASON,
            })
        calls = []

        def run_query(_query, *, mode="keyword", **_kwargs):
            calls.append(mode)
            return unavailable

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(
                recall.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(search, "run_query", side_effect=run_query), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            rc = recall.main(["deployment retry loop", "-s"])
        self.assertEqual(rc, 2)
        self.assertEqual(calls, ["semantic"])
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            stderr.getvalue(),
            "semantic search unavailable: index update in progress; "
            "retry shortly\n")

    def _json_status(self, argv: list[str], *, finish=None,
                     start_lane: bool = True) -> tuple[dict, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(recall.indexd_runtime, "ensure_index",
                               return_value=True), \
                mock.patch.object(recall.common, "in_agent_context",
                                  return_value=False), \
                mock.patch.object(search, "_semantic_runtime_installed",
                                  return_value=start_lane), \
                mock.patch.object(search, "_start_semantic_query",
                                  return_value=object()), \
                mock.patch.object(search, "_finish_semantic_query",
                                  return_value=finish), \
                mock.patch.object(
                    search, "run_query",
                    side_effect=lambda *a, **k: _result(
                        [self._keyword("literal")], phrase_chats=1)), \
                mock.patch.object(explore, "get_windows",
                                  side_effect=self._window), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            rc = recall.main([*argv, "--json", "--hits", "2",
                              "--budget", "4000"])
        self.assertEqual(rc, 0)
        payload = json.loads(stdout.getvalue())
        return payload["semantic_status"], stderr.getvalue()

    def test_recall_json_separates_searched_empty_from_never_ran(self) -> None:
        status, stderr = self._json_status(
            ["deployment retry loop", "--lexical"])
        self.assertEqual(status, {"state": "not-run", "reason": "--lexical"})
        self.assertNotIn("meaning unavailable", stderr)

        status, stderr = self._json_status(
            ["deployment retry loop"], start_lane=False)
        self.assertEqual(status, {"state": "not-run",
                                  "reason": "runtime-not-installed"})
        self.assertNotIn("meaning unavailable", stderr)

        searched_empty = _result(
            [], semantic=True, fallback_recommended=False,
            semantic_status={"state": "no-confident-match", "complete": True,
                             "fallback_recommended": False})
        status, stderr = self._json_status(
            ["deployment retry loop"], finish=searched_empty)
        self.assertEqual(status["state"], "no-confident-match")
        self.assertNotIn("meaning unavailable", stderr)

        status, stderr = self._json_status(
            ["deployment retry loop"], finish=None)
        self.assertEqual(status, {"state": "unavailable", "complete": False,
                                  "fallback_recommended": True})
        self.assertIn("meaning unavailable; keyword-only", stderr)

    def test_recall_lexical_disables_content_term_recovery(self) -> None:
        calls = []

        def run_query(_query, *, mode="keyword", **kwargs):
            calls.append((mode, kwargs))
            return _result([])

        with mock.patch.object(recall.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(recall.common, "in_agent_context", return_value=False), \
                mock.patch.object(search, "_semantic_runtime_installed", return_value=False), \
                mock.patch.object(search, "run_query", side_effect=run_query), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = recall.main([
                "deployment retry loop", "--lexical", "--json",
                "--hits", "2", "--budget", "4000",
            ])
        self.assertEqual(rc, 1)
        self.assertTrue(calls)
        self.assertTrue(all(
            mode == "keyword" and kwargs["allow_fallback"] is False
            for mode, kwargs in calls))

    def test_probe_lane_down_miss_discloses_keyword_only_evidence(self) -> None:
        def run_query(_query, *, mode="keyword", **_kwargs):
            return None if mode == "semantic" else _result([], phrase_chats=0)

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(recall.indexd_runtime, "ensure_index",
                               return_value=True), \
                mock.patch.object(recall.common, "in_agent_context",
                                  return_value=False), \
                mock.patch.object(recall.common, "index_summary",
                                  return_value={"sessions": 4, "messages": 40}), \
                mock.patch.object(search, "_semantic_runtime_installed",
                                  return_value=True), \
                mock.patch.object(search, "run_query", side_effect=run_query), \
                mock.patch.object(
                    recall.indexd_runtime, "freshness_story",
                    return_value=surface.FreshnessStory("current")), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            rc = recall.main(["deployment retry loop", "--probe"])
        self.assertEqual(rc, 2)
        # a lane-down miss must not read exactly like "no past context";
        # the tail is the one owned lane-down story (F4)
        self.assertEqual(
            stdout.getvalue().strip(),
            display_policy.probe_miss_line("corpusdb", corpus_sessions=4)
            + " - " + surface.SEMANTIC_LANE_POLICY.keyword_only)
        self.assertNotIn("semantic model warming", stdout.getvalue())

    def test_probe_miss_carries_the_same_confident_zero_verdict(self) -> None:
        # parity law: recall's probe miss speaks search's verdict vocabulary
        # through the same policy artifact, never a second wording
        def run_query(_query, *, mode="keyword", **_kwargs):
            if mode == "semantic":
                return _result([], semantic=True)
            return _result([], phrase_chats=0)

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(recall.indexd_runtime, "ensure_index",
                               return_value=True), \
                mock.patch.object(recall.common, "in_agent_context",
                                  return_value=False), \
                mock.patch.object(recall.common, "index_summary",
                                  return_value={"sessions": 4, "messages": 40}), \
                mock.patch.object(
                    recall.indexd_runtime, "freshness_story",
                    return_value=surface.FreshnessStory("current")), \
                mock.patch.object(search, "_semantic_runtime_installed",
                                  return_value=True), \
                mock.patch.object(search, "run_query", side_effect=run_query), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            rc = recall.main(["deployment retry loop", "--probe"])
        self.assertEqual(rc, 1)
        self.assertEqual(
            stdout.getvalue().strip(),
            display_policy.probe_miss_line("corpusdb", corpus_sessions=4)
            + " - " + surface.MISS_CONFIDENT_TAIL)

    def test_probe_stale_miss_carries_the_freshness_lever_once(self) -> None:
        behind = surface.FreshnessStory(
            "behind", behind_s=7200.0, changed_stores=1)
        story_line = surface.freshness_story_line(behind)

        def run_query(_query, *, mode="keyword", **_kwargs):
            if mode == "semantic":
                return _result([], semantic=True)
            return _result([], phrase_chats=0)

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(recall.indexd_runtime, "ensure_index",
                               return_value=True), \
                mock.patch.object(recall.common, "in_agent_context",
                                  return_value=False), \
                mock.patch.object(recall.common, "index_summary",
                                  return_value={"sessions": 4, "messages": 40}), \
                mock.patch.object(recall.indexd_runtime, "freshness_story",
                                  return_value=behind), \
                mock.patch.object(recall.indexd_runtime,
                                  "agent_freshness_notice",
                                  return_value=story_line), \
                mock.patch.object(search, "_semantic_runtime_installed",
                                  return_value=True), \
                mock.patch.object(search, "run_query", side_effect=run_query), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            rc = recall.main(["deployment retry loop", "--probe"])
        self.assertEqual(rc, 2)
        self.assertIn(f" - {story_line}", stdout.getvalue())
        self.assertNotIn(story_line, stderr.getvalue())

    def test_explicit_semantic_probe_unavailable_exits_unverified(self) -> None:
        unavailable = _result(
            [], semantic=True, fallback_recommended=True,
            semantic_status={"state": "unavailable", "complete": False,
                             "fallback_recommended": True})
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(recall.indexd_runtime, "ensure_index",
                               return_value=True), \
                mock.patch.object(recall.common, "in_agent_context",
                                  return_value=False), \
                mock.patch.object(recall.common, "index_summary",
                                  return_value={"sessions": 4, "messages": 40}), \
                mock.patch.object(search, "run_query",
                                  return_value=unavailable), \
                mock.patch.object(
                    recall.indexd_runtime, "freshness_story",
                    return_value=surface.FreshnessStory("current")), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            rc = recall.main([
                "deployment retry loop", "--probe", "--semantic"])
        self.assertEqual(rc, 2)
        self.assertIn("semantic search unavailable", stderr.getvalue())
        self.assertIn("semantic:unavailable", stdout.getvalue())

    def test_default_recall_lane_down_miss_is_unverified_on_both_surfaces(
            self) -> None:
        empty = _result([], phrase_chats=0)
        for machine in (False, True):
            with self.subTest(machine=machine):
                stdout, stderr = io.StringIO(), io.StringIO()
                argv = ["deployment retry loop", "--budget", "4000"]
                if machine:
                    argv.append("--json")
                with mock.patch.object(
                        recall.indexd_runtime, "ensure_index",
                        return_value=True), \
                        mock.patch.object(
                            recall.common, "in_agent_context",
                            return_value=False), \
                        mock.patch.object(
                            recall.common, "index_summary",
                            return_value={"sessions": 4, "messages": 40}), \
                        mock.patch.object(
                            search, "_semantic_runtime_installed",
                            return_value=True), \
                        mock.patch.object(
                            search, "_start_semantic_query",
                            return_value=object()), \
                        mock.patch.object(
                            search, "_finish_semantic_query",
                            return_value=None), \
                        mock.patch.object(search, "run_query",
                                          return_value=empty), \
                        mock.patch.object(
                            recall.indexd_runtime, "freshness_story",
                            return_value=surface.FreshnessStory("current")), \
                        contextlib.redirect_stdout(stdout), \
                        contextlib.redirect_stderr(stderr):
                    rc = recall.main(argv)
                self.assertEqual(rc, 2)
                self.assertIn("meaning unavailable", stderr.getvalue())
                if machine:
                    payload = json.loads(stdout.getvalue())
                    self.assertEqual(
                        payload["semantic_status"]["state"], "unavailable")

    def test_partial_semantic_empty_is_unverified_in_search_and_recall_json(
            self) -> None:
        coverage = {
            "indexed": 7, "total": 11, "pending": 4, "complete": False}
        partial = _result(
            [], semantic=True, semantic_coverage=coverage, partial=True,
            semantic_status={"state": "no-confident-match",
                             "complete": False,
                             "fallback_recommended": False})

        rc, stdout, _stderr = AutoSemanticHybridTests()._run(
            ["deployment retry loop", "-s", "--json"],
            lambda *_args, **_kwargs: partial,
            compact_profile=False)
        self.assertEqual(rc, 2)
        head, rows = _search_json(stdout)
        self.assertEqual(rows, [])
        self.assertEqual(head["completeness"]["total_basis"], "floor")
        self.assertTrue(head["completeness"]["truncated"])

        output = io.StringIO()
        with mock.patch.object(recall.indexd_runtime, "ensure_index",
                               return_value=True), \
                mock.patch.object(recall.common, "in_agent_context",
                                  return_value=False), \
                mock.patch.object(search, "run_query", return_value=partial), \
                mock.patch.object(
                    recall.indexd_runtime, "freshness_story",
                    return_value=surface.FreshnessStory("current")), \
                contextlib.redirect_stdout(output), \
                contextlib.redirect_stderr(io.StringIO()):
            recall_rc = recall.main([
                "deployment retry loop", "-s", "--json", "--budget", "4000"])
        self.assertEqual(recall_rc, 2)
        self.assertFalse(json.loads(output.getvalue())["semantic_status"]["complete"])

    def test_semantic_tool_filter_is_rejected_in_search_and_recall(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(
                recall.main(["semantic worker failed", "-s", "--who", "tool"]), 2)
        self.assertIn("tool rows are not embedded", stderr.getvalue())
        with contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as stopped:
            search.main(["semantic worker failed", "-s", "--who", "tool"])
        self.assertEqual(stopped.exception.code, 2)

    def test_explicit_search_semantic_exit_contract_is_zero_one_two(self) -> None:
        query = "why did deployment keep retrying"
        cases = (
            (_result([_hit("meaning", 1, "answer", semantic=True)],
                     semantic=True), 0, ["semantic"]),
            # the empty page's prose-coverage count is a deliberate second call
            (_result([], semantic=True), 1, ["semantic", "keyword"]),
            (_result([], semantic=True, fallback_recommended=True,
                     semantic_status={"state": "unavailable",
                                      "fallback_recommended": True}),
             2, ["semantic"]),
            (_result([], semantic=True, fallback_recommended=True,
                     semantic_status={"state": "query-rejected",
                                      "fallback_recommended": True}),
             2, ["semantic"]),
        )
        for result, expected, lanes in cases:
            calls = []

            def run_query(_query, *, mode="keyword", **_kwargs):
                calls.append(mode)
                return result

            with self.subTest(expected=expected):
                stderr = io.StringIO()
                with mock.patch.object(search.indexd_runtime, "ensure_index", return_value=True), \
                        mock.patch.object(search, "run_query", side_effect=run_query), \
                        contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(stderr):
                    rc = search.main([query, "-s", "--color", "never"])
                self.assertEqual(rc, expected)
                self.assertEqual(calls, lanes)
                warming = display_policy.semantic_warming_line()
                self.assertNotIn(warming, stderr.getvalue())

    def test_explicit_semantic_unavailable_agrees_across_surfaces(self) -> None:
        query = "why did deployment keep retrying"
        unavailable = _result(
            [], semantic=True, fallback_recommended=True,
            semantic_status={
                "state": "unavailable", "complete": False,
                "fallback_recommended": True,
                "reason": "embeddings=off disables semantic search",
            })
        variants = (
            ("short", [query, "-s"]),
            ("long", [query, "--semantic"]),
            ("strict", [query, "-s", "--strict-semantic"]),
        )
        for label, argv in variants:
            for machine in (False, True):
                calls = []

                def run_query(_query, *, mode="keyword", **_kwargs):
                    calls.append(mode)
                    return unavailable

                with self.subTest(label=label, machine=machine), \
                        mock.patch.object(search.common, "setting",
                                          return_value="off"):
                    rc, stdout, stderr = AutoSemanticHybridTests()._run(
                        [*argv, *(["--json"] if machine else [])], run_query,
                        compact_profile=False)
                self.assertEqual(rc, 2)
                self.assertEqual(calls, ["semantic"])
                self.assertNotIn(display_policy.semantic_warming_line(), stderr)
                if machine:
                    payload = json.loads(stdout)
                    self.assertEqual(payload["error"]["code"],
                                     "semantic-unavailable")
                    self.assertEqual(payload["semantic"],
                                     unavailable["semantic_status"])
                    self.assertEqual(payload["hits"], [])
                else:
                    self.assertEqual(stdout, "")
                    self.assertIn("semantic search unavailable", stderr)
                    self.assertIn("embeddings=off", stderr)

    def test_explicit_semantic_runtime_failure_never_runs_keyword(self) -> None:
        for machine in (False, True):
            calls = []

            def run_query(_query, *, mode="keyword", **_kwargs):
                calls.append(mode)
                return None

            rc, stdout, stderr = AutoSemanticHybridTests()._run(
                ["deployment retry loop", "-s",
                 *(["--json"] if machine else [])], run_query,
                compact_profile=False)
            self.assertEqual(rc, 2)
            self.assertEqual(calls, ["semantic"])
            self.assertNotIn(display_policy.semantic_warming_line(), stderr)
            if machine:
                payload = json.loads(stdout)
                self.assertEqual(payload["error"]["code"],
                                 "semantic-unavailable")
                self.assertEqual(payload["semantic"]["state"], "unavailable")
            else:
                self.assertEqual(stdout, "")
                self.assertIn("semantic search unavailable", stderr)

    def test_tool_only_default_recall_never_starts_semantics(self) -> None:
        calls = []

        def run_query(_query, *, mode="keyword", **_kwargs):
            calls.append(mode)
            return _result([self._keyword("tool-evidence")])

        stdout = io.StringIO()
        with mock.patch.object(recall.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(search, "_semantic_runtime_installed",
                                  return_value=True), \
                mock.patch.object(search, "run_query", side_effect=run_query), \
                mock.patch.object(explore, "get_windows", side_effect=self._window), \
                mock.patch.object(explore, "_session_index", return_value={}), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = recall.main([
                "why did the semantic worker fail", "--who", "tool",
                "--json", "--budget", "0"])
        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["keyword"])

    def test_recall_head_labels_weak_cosine_without_keyword_score(self) -> None:
        hit = _hit("meaning", 7, "semantic rescue", semantic=True)
        hit["sem_score"] = 0.8256
        hit["score"] = 0.4851
        stdout = io.StringIO()
        with mock.patch.object(recall.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(recall.common, "in_agent_context", return_value=False), \
                mock.patch.object(search, "run_query",
                                  return_value=_result([hit], semantic=True)), \
                mock.patch.object(explore, "get_windows", side_effect=self._window), \
                mock.patch.object(explore, "_session_index",
                                  return_value={"meaning": {}}), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = recall.main(["deployment retry loop", "-s", "--budget", "0"])
        self.assertEqual(rc, 0)
        head = stdout.getvalue().splitlines()[0]
        self.assertIn("cosine 0.8256", head)
        self.assertIn("weak meaning match", head)
        self.assertNotIn("score 0.4851", head)

    def test_one_hit_recall_does_not_claim_an_invisible_semantic_lane(self) -> None:
        calls = []

        def run_query(_query, *, mode="keyword", **_kwargs):
            calls.append(mode)
            return _result([self._keyword("literal")], phrase_chats=1)

        stdout = io.StringIO()
        with mock.patch.object(recall.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(recall.common, "in_agent_context", return_value=False), \
                mock.patch.object(search, "_semantic_runtime_installed",
                                  return_value=True), \
                mock.patch.object(search, "run_query", side_effect=run_query), \
                mock.patch.object(explore, "get_windows", side_effect=self._window), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = recall.main([
                "deployment retry loop", "--hits", "1", "--json", "--budget", "0"])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["keyword"])
        self.assertNotIn("hybrid", payload)
        self.assertNotIn("semantic", payload["engine"])
        self.assertEqual(payload["hits"][0]["session"], "literal")

    def test_semantic_reservation_preserves_global_session_uniqueness(self) -> None:
        selected = [
            {**self._keyword("session-a"), "_recall_query": 0},
            {**self._keyword("session-b"), "_recall_query": 1},
        ]
        ranked = [*selected,
                  {**_hit("session-a", 9, "duplicate session", semantic=True),
                   "_recall_query": 1},
                  {**_hit("session-c", 9, "independent session", semantic=True),
                   "_recall_query": 1}]
        reserved = recall._reserve_semantic_hits(
            selected, ranked, 3, 2, family_diverse=False)
        self.assertEqual([hit["session"] for hit in reserved],
                         ["session-a", "session-b", "session-c"])

    def test_semantic_reservation_does_not_replace_same_family_lexical_evidence(self) -> None:
        selected = [
            {**self._keyword("child-a"), "_recall_query": 0},
            {**self._keyword("unrelated"), "_recall_query": 0},
        ]
        ranked = [*selected,
                  {**_hit("child-b", 9, "better family turn", semantic=True),
                   "_recall_query": 0}]
        with mock.patch.object(
                recall.common, "indexed_family_roots",
                return_value={
                    "child-a": "root", "child-b": "root",
                    "unrelated": "unrelated",
                }):
            reserved = recall._reserve_semantic_hits(
                selected, ranked, 3, 1, family_diverse=True)
        self.assertEqual([hit["session"] for hit in reserved],
                         ["child-a", "unrelated"])

    def test_floored_pack_target_keeps_the_meaning_lane(self) -> None:
        # --hits below the query count used to zero the lane along with q2's
        # slot; the floor restores one slot per query AND leaves the meaning
        # lane able to serve a query whose keyword evidence is weak scatter
        started = []

        def start(query, _kwargs):
            started.append(query)
            return ("pending", query)

        def finish(_pending):
            return _result(
                [_hit("meaning", 7, "semantic rescue", semantic=True)],
                semantic=True)

        def run_query(q, *, mode="keyword", **_kwargs):
            session = "weak-a" if q == "deployment retry loop" else "weak-b"
            return _result([self._keyword(session, fallback=True)],
                           phrase_chats=0)

        stdout = io.StringIO()
        with mock.patch.object(recall.indexd_runtime, "ensure_index", return_value=True), \
                mock.patch.object(recall.common, "in_agent_context",
                                  return_value=False), \
                mock.patch.object(search, "_semantic_runtime_installed",
                                  return_value=True), \
                mock.patch.object(search, "_start_semantic_query", side_effect=start), \
                mock.patch.object(search, "_finish_semantic_query",
                                  side_effect=finish), \
                mock.patch.object(search, "run_query", side_effect=run_query), \
                mock.patch.object(explore, "get_windows", side_effect=self._window), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = recall.main(
                ["deployment retry loop", "database lock contention",
                 "--hits", "1", "--json", "--budget", "0"], prog="pack")
        self.assertEqual(rc, 0)
        self.assertEqual(started, ["deployment retry loop",
                                   "database lock contention"])
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload.get("hybrid"))
        sessions = {hit["session"] for hit in payload["hits"]}
        self.assertEqual(sessions, {"meaning", "weak-b"})

    def test_dropped_semantic_rows_reach_search_text_and_json(self) -> None:
        integrity = {"state": "rows-dropped", "dropped": 2,
                     "reason": "text proof failed",
                     "repair": "full-rebuild", "repair_persistent": True}

        def run_query(_query, *, mode="keyword", **_kwargs):
            return _result([_hit("other", 4, "needle", semantic=True)],
                           semantic=True, semantic_integrity=integrity)

        rc, _out, err = AutoSemanticHybridTests()._run(
            ["needle", "-s"], run_query, compact_profile=False)
        self.assertEqual(rc, 0)
        self.assertIn("2 rows dropped", err)
        self.assertIn("full rebuild is running", err)

        rc, out, _err = AutoSemanticHybridTests()._run(
            ["needle", "-s", "--json"], run_query, compact_profile=False)
        self.assertEqual(rc, 0)
        head, rows = _search_json(out)
        self.assertEqual(head["semantic_integrity"]["dropped"], 2)
        self.assertEqual(len(rows), 1)
        for field in ("semantic_integrity", "semantic", "engine", "run"):
            self.assertNotIn(field, rows[0])

    def test_search_json_moves_real_semantic_run_fields_to_envelope(self) -> None:
        coverage = {
            "indexed": 7, "total": 9, "pending": 2, "complete": False}
        accelerator = {
            "indexed": 5, "total": 9, "pending": 4, "complete": False}
        hit = _hit("other", 4, "meaning", semantic=True)
        hit.update({
            "semantic_coverage": coverage,
            "semantic_accelerator_coverage": accelerator,
            "semantic_partial": True,
        })

        def run_query(_query, *, mode="keyword", **_kwargs):
            return _result(
                [hit], semantic=True, semantic_coverage=coverage,
                semantic_accelerator_coverage=accelerator, partial=True)

        rc, out, _err = AutoSemanticHybridTests()._run(
            ["meaning", "-s", "--json"], run_query,
            compact_profile=False)
        self.assertEqual(rc, 0)
        head, rows = _search_json(out)
        self.assertEqual(head["semantic_coverage"], coverage)
        self.assertEqual(head["semantic_accelerator_coverage"], accelerator)
        self.assertTrue(head["semantic_partial"])
        self.assertEqual(len(rows), 1)
        for field in (
                "semantic_coverage", "semantic_accelerator_coverage",
                "semantic_partial", "semantic", "engine"):
            self.assertNotIn(field, rows[0])

    def test_rejected_generation_reaches_semantic_failure_surfaces(self) -> None:
        integrity = {
            "state": "generation-rejected", "dropped": 0,
            "reason": "active semantic artifact digest mismatch",
            "repair": "full-rebuild-requested", "repair_persistent": True,
        }
        status = {
            "state": "generation-rejected", "complete": False,
            "fallback_recommended": True,
        }

        def run_query(_query, *, mode="keyword", **_kwargs):
            return _result(
                [], semantic=True, fallback_recommended=True,
                semantic_status=status, semantic_integrity=integrity)

        rc, out, _err = AutoSemanticHybridTests()._run(
            ["needle", "-s", "--json"], run_query, compact_profile=False)
        self.assertEqual(rc, 2)
        payload = json.loads(out)
        self.assertEqual(payload["semantic"]["state"], "generation-rejected")
        self.assertEqual(payload["semantic_integrity"], integrity)

        rc, _out, err = AutoSemanticHybridTests()._run(
            ["needle", "-s"], run_query, compact_profile=False)
        self.assertEqual(rc, 2)
        self.assertIn("active generation rejected", err)
        self.assertIn("full rebuild requested", err)

    def test_semantic_local_classifies_integrity_rejection(self) -> None:
        integrity = {
            "state": "generation-rejected", "dropped": 0,
            "reason": "active semantic artifact digest mismatch",
            "repair": "full-rebuild-requested", "repair_persistent": True,
        }
        payload = {
            "results": [], "semantic_unavailable": True,
            "semantic_coverage": {
                "indexed": 2, "total": 2, "pending": 0, "complete": True},
            "partial": True, "score_kind": "unavailable",
            "semantic_integrity": integrity,
        }
        with mock.patch.object(
                semworker, "resident_status", return_value={"running": True}), \
                mock.patch.object(
                    semworker, "search_worker", return_value=payload):
            result = search._semantic_local("needle", 10)
        self.assertIsNotNone(result)
        self.assertEqual(
            result["semantic_status"]["state"], "generation-rejected")
        self.assertTrue(result["fallback_recommended"])
        self.assertEqual(result["semantic_integrity"], integrity)

    def test_query_seam_preserves_semantic_integrity(self) -> None:
        integrity = {
            "state": "generation-rejected", "dropped": 0,
            "repair": "full-rebuild-requested", "repair_persistent": True,
        }
        local = {
            "hits": [], "total": 0, "chats": 0, "truncated": False,
            "semantic_coverage": {
                "indexed": 2, "total": 2, "pending": 0, "complete": True},
            "semantic_accelerator_coverage": None, "partial": True,
            "score_kind": "unavailable",
            "semantic_status": {
                "state": "generation-rejected", "complete": False,
                "fallback_recommended": True},
            "fallback_recommended": True, "semantic_integrity": integrity,
        }
        with mock.patch.object(search, "_semantic_local", return_value=local):
            result = search.run_query("needle", mode="semantic")
        self.assertEqual(result["semantic_integrity"], integrity)

    def test_recall_preserves_generation_rejection(self) -> None:
        integrity = {
            "state": "generation-rejected", "dropped": 0,
            "reason": "active semantic artifact digest mismatch",
            "repair": "full-rebuild-requested", "repair_persistent": True,
        }
        rejected = _result(
            [], semantic=True, fallback_recommended=True,
            semantic_status={
                "state": "generation-rejected", "complete": False,
                "fallback_recommended": True},
            semantic_integrity=integrity, partial=True)

        def invoke(extra):
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.object(
                    recall.indexd_runtime, "ensure_index", return_value=True), \
                    mock.patch.object(
                        recall.common, "in_agent_context", return_value=False), \
                    mock.patch.object(
                        search, "run_query", return_value=rejected), \
                    contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(stderr):
                rc = recall.main(["needle", "-s", *extra])
            return rc, stdout.getvalue(), stderr.getvalue()

        rc, out, _err = invoke(["--json", "--budget", "0"])
        self.assertEqual(rc, 2)
        payload = json.loads(out)
        self.assertEqual(payload["semantic_integrity"], integrity)

        rc, _out, err = invoke([])
        self.assertEqual(rc, 2)
        self.assertIn("active generation rejected", err)

    def test_intact_semantic_page_says_nothing_about_integrity(self) -> None:
        def run_query(_query, *, mode="keyword", **_kwargs):
            return _result([_hit("other", 4, "needle", semantic=True)],
                           semantic=True)

        rc, out, err = AutoSemanticHybridTests()._run(
            ["needle", "-s", "--json"], run_query, compact_profile=False)
        self.assertEqual(rc, 0)
        self.assertNotIn("integrity", err)
        head, rows = _search_json(out)
        self.assertNotIn("semantic_integrity", head)
        self.assertEqual(len(rows), 1)
        for field in ("semantic_integrity", "semantic", "engine", "run"):
            self.assertNotIn(field, rows[0])

    def test_weak_single_query_surfaces_semantic_ranks_one_through_three(self) -> None:
        lexical = [{**self._keyword(f"weak-{index}", fallback=True),
                    "_recall_query": 0} for index in range(3)]
        meaning = [{**_hit(f"meaning-{index}", index + 10,
                           f"semantic rank {index + 1}", semantic=True),
                    "_recall_query": 0, "lane": "semantic"}
                   for index in range(3)]
        selected = recall._reserve_semantic_hits(
            lexical, [*lexical, *meaning], 3, 1, family_diverse=False)
        self.assertEqual([hit["session"] for hit in selected],
                         ["meaning-0", "meaning-1", "meaning-2"])


class ProbeOrderIndependenceTests(unittest.TestCase):
    """A multi-query probe judges each query's best candidate: one weak
    query in slot 0 must not eclipse another query's exact-phrase hit into
    a false miss - reversing the argument order flipped rc 1/0."""

    WEAK_Q = "gamma delta"
    STRONG_Q = "quokka fence deadlock"

    def _run_query(self, query, *, mode="keyword", **kwargs):
        if kwargs.get("who") == "tool":
            return _result([], phrase_chats=0)
        if query == self.STRONG_Q:
            strong = _hit("strongsess", 3, "quokka fence deadlock evidence")
            strong["matched"] = "phrase"
            return _result([strong], phrase_chats=1)
        weak = _hit("weakling", 5, "gamma ray delta wing")
        weak["matched"] = "all-terms"
        weak["_evidence"] = search.SCATTER_MIN_EVIDENCE - 0.2
        return _result([weak], phrase_chats=0)

    def _probe(self, queries: list[str]) -> tuple[int, str]:
        stdout = io.StringIO()
        with mock.patch.object(recall.indexd_runtime, "ensure_index",
                               return_value=True), \
                mock.patch.object(recall.common, "in_agent_context",
                                  return_value=False), \
                mock.patch.object(search, "run_query",
                                  side_effect=self._run_query), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = recall.main([*queries, "--probe", "--lexical"], prog="pack")
        return rc, stdout.getvalue()

    def test_both_orders_serve_the_exact_phrase_pointer(self) -> None:
        for queries in ((self.WEAK_Q, self.STRONG_Q),
                        (self.STRONG_Q, self.WEAK_Q)):
            with self.subTest(order=queries):
                rc, out = self._probe(list(queries))
                self.assertEqual(rc, 0, out)
                self.assertIn("@strongsess:3", out)
                self.assertNotIn("no confident past-context pointer", out)

    def test_two_weak_queries_still_miss_in_both_orders(self) -> None:
        other_weak = "epsilon zeta"
        for queries in ([self.WEAK_Q, other_weak],
                        [other_weak, self.WEAK_Q]):
            with self.subTest(order=queries):
                rc, out = self._probe(queries)
                self.assertEqual(rc, 1, out)
                self.assertIn("no confident past-context pointer", out)


if __name__ == "__main__":
    unittest.main()
