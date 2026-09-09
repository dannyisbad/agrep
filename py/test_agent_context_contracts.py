"""Contracts for agent detection, caller identity, and freshness disclosure."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from _test_support import isolate_data_dir


isolate_data_dir()
import common  # noqa: E402
import indexd_runtime  # noqa: E402
import compact  # noqa: E402
import session_context  # noqa: E402


def _publish_family_meta(root: Path, rows: list[dict],
                         signature: str = "6:fixture") -> None:
    rows = sorted(rows, key=lambda row: str(row["session"]))
    (root / "sessions.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    (root / ".ingest.sig").write_text(signature + "\n", encoding="utf-8")
    pairs = [
        (str(row["session"]), str(row.get("parent") or ""))
        for row in rows
    ]
    (root / common.SESSION_FAMILY_META_FILE).write_text(
        json.dumps({
            "version": common.SESSION_FAMILY_INDEX_VERSION,
            "algorithm": common.SESSION_FAMILY_DIGEST_ALGORITHM,
            "ingest_signature": signature,
            "count": len(rows),
            "digest": common.session_family_digest(sorted(pairs)),
        }),
        encoding="utf-8",
    )


class AgentContextContracts(unittest.TestCase):
    def test_importing_selftest_does_not_mutate_environment(self) -> None:
        env = {
            **os.environ,
            "AGREP_PROFILE": "compact",
            "CODEX_THREAD_ID": "sentinel-thread",
            "AGREP_RS_BIN": "sentinel-rs",
        }
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import os; before=dict(os.environ); import selftest; "
                "assert dict(os.environ) == before",
            ],
            cwd=Path(__file__).parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_agent_context_uses_the_supplied_environment(self) -> None:
        self.assertTrue(common.in_agent_context({"CODEX_THREAD_ID": "thread"}))
        self.assertTrue(common.in_agent_context({"CLAUDECODE": "1"}))
        self.assertTrue(common.in_agent_context({
            "CLAUDE_CODE_SESSION_ID": "direct-session",
        }))
        self.assertTrue(common.in_agent_context({
            "AGREP_PI_SESSION_ID": "pi-session",
        }))
        self.assertFalse(common.in_agent_context({"TERM": "xterm-256color"}))

    def test_codex_session_identity_is_direct(self) -> None:
        with mock.patch.dict(
                os.environ,
                {"CODEX_THREAD_ID": "direct-thread"},
                clear=True):
            self.assertEqual(common.calling_session(), "direct-thread")


    def test_pi_and_omp_session_identity_is_direct(self) -> None:
        identity = common.calling_identity({
            "AGREP_PI_SESSION_ID": "pi-session",
        })
        self.assertEqual(identity.session, "pi-session")
        self.assertEqual(identity.reason, "pi")

    def test_direct_identity_outranks_presence_only_fingerprints(self) -> None:
        cases = (
            ("codex", {
                "CODEX_THREAD_ID": "codex-session",
                "CLAUDECODE": "1",
            }),
            ("pi", {
                "AGREP_PI_SESSION_ID": "pi-session",
                "CLAUDECODE": "1",
            }),
        )
        for agent, env in cases:
            with self.subTest(agent=agent):
                identity = common.calling_identity(env)
                self.assertEqual(identity.session, f"{agent}-session")
                self.assertEqual(identity.reason, agent)

    def test_conflicting_direct_agent_identities_fail_open(self) -> None:
        cases = (
            {
                "CODEX_THREAD_ID": "stale-codex-thread",
                "CLAUDE_CODE_SESSION_ID": "live-claude-session",
                "CLAUDECODE": "1",
            },
            {
                "AGREP_PI_SESSION_ID": "pi-session",
                "CODEX_THREAD_ID": "codex-session",
            },
        )
        for env in cases:
            with self.subTest(env=env), \
                    mock.patch.dict(os.environ, env, clear=True):
                identity = common.calling_identity()
                self.assertIsNone(identity.session)
                self.assertEqual(identity.reason, "identity-conflict")
                self.assertIsNone(common.calling_session())

    def test_claude_session_identity_is_direct(self) -> None:
        with mock.patch.dict(
                os.environ,
                {"CLAUDE_CODE_SESSION_ID": "env-session", "CLAUDECODE": "1"},
                clear=True), \
                mock.patch.object(
                    session_context.os, "scandir",
                    side_effect=AssertionError("mtime discovery ran")):
            self.assertEqual(common.calling_session(), "env-session")

    def test_presence_only_claude_fingerprint_never_guesses_from_mtime(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}, clear=True), \
                mock.patch.object(
                    session_context.os, "scandir",
                    side_effect=AssertionError("mtime discovery ran")):
            identity = common.calling_identity()
            self.assertIsNone(identity.session)
            self.assertEqual(identity.reason, "caller-unresolved")
            self.assertIsNone(common.calling_session())

    def _publication_dir(self, stack, records: dict[int, object]) -> Path:
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        root.chmod(0o700)
        for pid, record in records.items():
            (root / f"{pid}.json").write_text(
                record if isinstance(record, str) else json.dumps(record),
                encoding="utf-8")
        stack.enter_context(mock.patch.object(
            session_context, "CALLER_PUBLICATION_DIR", root))
        return root

    def test_published_ancestor_names_the_caller_when_no_env_does(self) -> None:
        # omp's tool shell: presence key only, no identity var, but the agent
        # process three levels up published its sessions
        chain = {4000: 3000, 3000: 2000, 2000: 1000, 1000: 1}
        with contextlib.ExitStack() as stack:
            self._publication_dir(stack, {
                2000: {"version": 1, "pid": 2000,
                       "sessions": ["omp-root-session"],
                       "cwd": "/work", "updated": int(time.time() * 1000)},
            })
            stack.enter_context(mock.patch.dict(
                os.environ, {"CLAUDECODE": "1"}, clear=True))
            stack.enter_context(mock.patch.object(os, "getppid", return_value=4000))
            stack.enter_context(mock.patch.object(
                session_context.hookless_proc, "parent_pid",
                side_effect=lambda pid: chain.get(pid)))
            stack.enter_context(mock.patch.object(
                session_context.hookless_proc, "process_start_time",
                return_value=None))
            stack.enter_context(mock.patch.object(
                session_context.os, "scandir",
                side_effect=AssertionError("mtime discovery ran")))
            identity = common.calling_identity()
            self.assertEqual(identity.session, "omp-root-session")
            self.assertEqual(identity.reason, "pi-process")
            self.assertTrue(common.in_agent_context())
            # a direct export still outranks the publication
            with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "direct"}):
                self.assertEqual(common.calling_identity().reason, "codex")
            # a supplied environment never consults the process tree
            self.assertEqual(
                common.calling_identity({"CLAUDECODE": "1"}).reason,
                "caller-unresolved")

    def test_publication_without_an_agent_env_still_counts_as_agent_context(
            self) -> None:
        with contextlib.ExitStack() as stack:
            self._publication_dir(stack, {
                77: {"pid": 77, "sessions": ["s"], "updated": int(time.time() * 1000)},
            })
            stack.enter_context(mock.patch.dict(os.environ, {}, clear=True))
            stack.enter_context(mock.patch.object(os, "getppid", return_value=77))
            stack.enter_context(mock.patch.object(
                session_context.hookless_proc, "process_start_time",
                return_value=None))
            self.assertTrue(common.in_agent_context())
            self.assertEqual(common.calling_session(), "s")

    def test_publication_is_refused_for_a_recycled_or_foreign_pid(self) -> None:
        now_ms = int(time.time() * 1000)
        cases = {
            # exact birth identity disagrees with the live process
            "start-mismatch": (
                {"pid": 500, "sessions": ["s"], "start": "proc_1",
                 "updated": now_ms},
                {"process_start_identity": "proc_2"}),
            # no birth identity, record older than the live process
            "older-than-process": (
                {"pid": 500, "sessions": ["s"], "updated": now_ms - 60_000},
                {"process_start_time": time.time()}),
            # record names another pid
            "wrong-pid": (
                {"pid": 501, "sessions": ["s"], "updated": now_ms}, {}),
            "no-sessions": ({"pid": 500, "sessions": [], "updated": now_ms}, {}),
            "unsafe-session": (
                {"pid": 500, "sessions": ["s;rm -rf /"], "updated": now_ms}, {}),
            "no-timestamp": ({"pid": 500, "sessions": ["s"]}, {}),
            "not-json": ("{not json", {}),
        }
        for label, (record, probes) in cases.items():
            with self.subTest(label=label), contextlib.ExitStack() as stack:
                self._publication_dir(stack, {500: record})
                stack.enter_context(mock.patch.object(
                    session_context.hookless_proc, "process_start_identity",
                    return_value=probes.get("process_start_identity")))
                stack.enter_context(mock.patch.object(
                    session_context.hookless_proc, "process_start_time",
                    return_value=probes.get("process_start_time")))
                self.assertIsNone(session_context.read_caller_publication(500))
        with contextlib.ExitStack() as stack:
            self._publication_dir(stack, {
                500: {"pid": 500, "sessions": ["s"], "start": "proc_1",
                      "updated": now_ms}})
            stack.enter_context(mock.patch.object(
                session_context.hookless_proc, "process_start_identity",
                return_value="proc_1"))
            self.assertEqual(
                session_context.read_caller_publication(500).sessions, ("s",))

    def test_multi_session_publication_picks_the_indexed_root(self) -> None:
        # advisor + scouts + root all published from one omp process
        sessions = ["scout-b", "advisor", "root-session", "scout-a"]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _publish_family_meta(root, [
                {"session": "root-session"},
                {"session": "advisor", "parent": "root-session"},
                {"session": "scout-a", "parent": "root-session"},
                {"session": "scout-b", "parent": "root-session"},
            ])
            db = sqlite3.connect(root / "corpus.db")
            db.executescript("""
                CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE session_family(
                    session TEXT PRIMARY KEY, root TEXT NOT NULL,
                    side INTEGER NOT NULL CHECK(side IN (0, 1))
                ) WITHOUT ROWID;
                CREATE TABLE msgs(session TEXT, turn INTEGER, who TEXT);
            """)
            with mock.patch.object(session_context, "DATA_DIR", root), \
                    contextlib.ExitStack() as stack:
                db.execute(
                    "INSERT INTO meta VALUES('family_stamp', ?)",
                    (common.session_family_source_stamp(),),
                )
                db.executemany(
                    "INSERT INTO session_family VALUES(?, ?, ?)",
                    (("root-session", "root-session", 0),
                     ("advisor", "root-session", 1),
                     ("scout-a", "root-session", 1),
                     ("scout-b", "root-session", 1)),
                )
                db.commit()
                self._publication_dir(stack, {
                    9: {"pid": 9, "sessions": sessions,
                        "updated": int(time.time() * 1000)}})
                stack.enter_context(mock.patch.dict(os.environ, {}, clear=True))
                stack.enter_context(mock.patch.object(os, "getppid", return_value=9))
                stack.enter_context(mock.patch.object(
                    session_context.hookless_proc, "process_start_time",
                    return_value=None))
                identity = common.calling_identity()
                family = common.calling_family()
                policy = common.calling_self_exclusion()
            db.close()
        self.assertEqual(identity.session, "root-session")
        self.assertEqual(family.source, "pi-process")
        self.assertEqual(
            family.members,
            frozenset({"root-session", "advisor", "scout-a", "scout-b"}))
        self.assertEqual(
            family.side_members, frozenset({"advisor", "scout-a", "scout-b"}))
        # every published peer is live in the caller's process: current context
        self.assertEqual(
            family.window_members, frozenset({"advisor", "scout-a", "scout-b"}))
        self.assertEqual(policy.boundary, 0)
        self.assertTrue(policy.excludes("root-session", 3))
        self.assertTrue(policy.excludes("scout-a", 3))
        self.assertFalse(policy.labels("scout-a", 3))
        self.assertEqual(
            policy.query_filters()["_exclude_sessions"],
            ("advisor", "scout-a", "scout-b"))
        # not indexed yet: the first published session stands in
        with mock.patch.object(
                session_context, "_open_session_family_index",
                return_value=None):
            self.assertEqual(
                session_context._published_caller_session(tuple(sessions)),
                "scout-b")

    def test_stamp_behind_index_still_serves_display_lookups(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _publish_family_meta(root, [
                {"session": "root"},
                {"session": "child", "parent": "root"},
            ])
            db = sqlite3.connect(root / "corpus.db")
            db.executescript("""
                CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE session_family(
                    session TEXT PRIMARY KEY, root TEXT NOT NULL,
                    side INTEGER NOT NULL CHECK(side IN (0, 1))
                ) WITHOUT ROWID;
                CREATE TABLE msgs(session TEXT, turn INTEGER, who TEXT);
            """)
            db.execute("INSERT INTO meta VALUES('family_stamp', 'published')")
            db.executemany(
                "INSERT INTO session_family VALUES(?, ?, ?)",
                (("root", "root", 0), ("child", "root", 1)))
            db.commit()
            db.close()
            with mock.patch.object(session_context, "DATA_DIR", root), \
                    mock.patch.object(session_context, "_FAMILY_INDEX_BEHIND", False), \
                    mock.patch.object(
                        session_context, "session_family_source_stamp",
                        return_value="drifted"):
                self.assertIsNone(common.indexed_family_roots(("child",)))
                self.assertFalse(session_context.family_index_behind())
                self.assertEqual(
                    common.indexed_family_roots(("child",), allow_behind=True),
                    {"child": "root"})
                self.assertTrue(session_context.family_index_behind())
                prefixes = common.indexed_session_prefix_candidates(("child",))
                self.assertEqual(prefixes.force_full, frozenset())
                with mock.patch.object(
                        session_context, "calling_identity",
                        return_value=common.CallerIdentity("root", "pi")):
                    family = common.calling_family()
                    with session_context.calling_family_snapshot() as (
                            _identity, snapshot_family, snapshot_db):
                        # the generation-bound reader stays strict
                        self.assertIsNone(snapshot_family)
                        self.assertIsNone(snapshot_db)
        self.assertTrue(family.resolved)
        self.assertEqual(family.members, frozenset({"root", "child"}))

    def test_calling_family_materializes_every_related_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _publish_family_meta(root, [
                {"session": "root"},
                {"session": "child", "parent": "root"},
                {"session": "grandchild", "parent": "child"},
                {"session": "sibling", "parent": "root"},
                {"session": "other"},
                {"session": "other-child", "parent": "other"},
            ])
            db = sqlite3.connect(root / "corpus.db")
            db.executescript("""
                CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE session_family(
                    session TEXT PRIMARY KEY, root TEXT NOT NULL,
                    side INTEGER NOT NULL CHECK(side IN (0, 1))
                ) WITHOUT ROWID;
                CREATE INDEX session_family_root ON session_family(root);
                CREATE TABLE msgs(session TEXT, who TEXT, turn INTEGER);
            """)
            with mock.patch.object(session_context, "DATA_DIR", root):
                db.execute(
                    "INSERT INTO meta VALUES('family_stamp', ?)",
                    (common.session_family_source_stamp(),),
                )
                db.executemany(
                    "INSERT INTO session_family VALUES(?, ?, ?)",
                    (
                        ("root", "root", 0),
                        ("child", "root", 1),
                        ("grandchild", "root", 1),
                        ("sibling", "root", 0),
                        ("other", "other", 0),
                        ("other-child", "other", 1),
                    ),
                )
                db.commit()
                db.executemany(
                    "INSERT INTO msgs VALUES(?, ?, ?)",
                    (("child", "user", 1), ("child", "agent", 1),
                     ("child", "tool", 1)),
                )
                db.commit()
                with mock.patch.object(
                        session_context, "calling_identity",
                        return_value=common.CallerIdentity("child", "codex")):
                    family = common.calling_family()
                self.assertEqual(
                    common.indexed_family_roots(
                        ("child", "other-child", "missing")),
                    {
                        "child": "root", "other-child": "other",
                        "missing": "missing",
                    },
                )
                self.assertEqual(
                    common.indexed_family_metadata(
                        ("child", "sibling", "missing")),
                    {
                        "child": ("root", True),
                        "sibling": ("root", False),
                        "missing": ("missing", False),
                    },
                )
                self.assertEqual(
                    common.indexed_session_matches("grand"),
                    ["grandchild"],
                )
                self.assertEqual(common.indexed_session_prose_count("child"), 1)
                policy = common.SelfExclusion(family, None, "forced")
                self.assertTrue(
                    common.indexed_self_exclusion_has_rows(policy))
                absent = common.CallingFamily(
                    "missing", "missing", frozenset({"missing"}), False)
                self.assertFalse(common.indexed_self_exclusion_has_rows(
                    common.SelfExclusion(absent, None, "forced")))
                prefix_index = common.indexed_session_prefix_candidates(
                    ("grandchild",))
                self.assertIn("grandchild", prefix_index)
                with mock.patch.object(
                        session_context, "SESSION_FAMILY_MAX_MEMBERS", 2):
                    self.assertIsNone(
                        common.indexed_calling_family("child"))
            db.close()
        self.assertIsNotNone(family)
        self.assertEqual(family.root, "root")
        self.assertEqual(
            family.members,
            frozenset({"root", "child", "grandchild", "sibling"}),
        )
        self.assertTrue(family.contains("grandchild"))
        self.assertFalse(family.contains("other-child"))
        # a delegated caller owns its own spawn only; root and sibling stay history
        self.assertEqual(family.descendants, frozenset({"grandchild"}))
        self.assertEqual(family.window_members, frozenset({"grandchild"}))

    def test_retained_schema_14_family_roots_group_siblings(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _publish_family_meta(root, [
                {"session": "root"},
                {"session": "sibling-a", "parent": "root"},
                {"session": "sibling-b", "parent": "root"},
                {"session": "other"},
            ])
            db = sqlite3.connect(root / "corpus.db")
            db.executescript("""
                CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE session_family(
                    session TEXT PRIMARY KEY, root TEXT NOT NULL
                ) WITHOUT ROWID;
                CREATE INDEX session_family_root ON session_family(root);
            """)
            with mock.patch.object(session_context, "DATA_DIR", root):
                db.executemany(
                    "INSERT INTO meta VALUES(?, ?)",
                    (
                        ("schema", "14"),
                        ("family_stamp", common.session_family_source_stamp()),
                    ),
                )
                db.executemany(
                    "INSERT INTO session_family VALUES(?, ?)",
                    (
                        ("root", "root"),
                        ("sibling-a", "root"),
                        ("sibling-b", "root"),
                        ("other", "other"),
                    ),
                )
                db.commit()
                self.assertEqual(
                    common.indexed_family_roots(
                        ("sibling-a", "sibling-b", "other", "missing")),
                    {
                        "sibling-a": "root",
                        "sibling-b": "root",
                        "other": "other",
                        "missing": "missing",
                    },
                )
            db.close()

    def test_family_roots_fail_closed_for_corrupt_family_schema(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _publish_family_meta(root, [
                {"session": "root"},
                {"session": "child", "parent": "root"},
            ])
            db = sqlite3.connect(root / "corpus.db")
            db.executescript("""
                CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE session_family(session TEXT PRIMARY KEY);
            """)
            with mock.patch.object(session_context, "DATA_DIR", root):
                db.execute(
                    "INSERT INTO meta VALUES('family_stamp', ?)",
                    (common.session_family_source_stamp(),),
                )
                db.executemany(
                    "INSERT INTO session_family VALUES(?)",
                    (("root",), ("child",)),
                )
                db.commit()
                self.assertIsNone(
                    common.indexed_family_roots(("root", "child")))
            db.close()

    def test_calling_window_uses_the_last_of_multiple_recaps(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _publish_family_meta(root, [
                {"session": "root"},
                {"session": "child", "parent": "root"},
            ])
            db = sqlite3.connect(root / "corpus.db")
            db.executescript("""
                CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE session_family(
                    session TEXT PRIMARY KEY, root TEXT NOT NULL,
                    side INTEGER NOT NULL CHECK(side IN (0, 1))
                ) WITHOUT ROWID;
                CREATE TABLE msgs(session TEXT, turn INTEGER, who TEXT);
            """)
            with mock.patch.object(session_context, "DATA_DIR", root):
                db.execute(
                    "INSERT INTO meta VALUES('family_stamp', ?)",
                    (common.session_family_source_stamp(),),
                )
                db.executemany(
                    "INSERT INTO session_family VALUES(?, ?, ?)",
                    (("root", "root", 0), ("child", "root", 1)),
                )
                db.executemany(
                    "INSERT INTO msgs VALUES(?, ?, ?)",
                    (
                        ("root", 3, "recap"),
                        ("root", 11, "recap"),
                        ("root", 12, "user"),
                    ),
                )
                db.commit()
                with mock.patch.object(
                        session_context, "calling_identity",
                        return_value=common.CallerIdentity("root", "codex")):
                    policy = common.calling_self_exclusion()
            db.close()
        self.assertIsNotNone(policy)
        self.assertEqual(policy.boundary, 11)
        self.assertFalse(policy.excludes("root", 10))
        self.assertTrue(policy.excludes("root", 11))
        self.assertTrue(policy.labels("root", 10))
        # an unproven spawn time keeps the child visible, marked as the caller's own
        self.assertFalse(policy.excludes("child", 99))
        self.assertTrue(policy.labels("child", 99))

    def test_malformed_recap_turn_cannot_create_a_window(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _publish_family_meta(root, [{"session": "root"}])
            db = sqlite3.connect(root / "corpus.db")
            db.executescript("""
                CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE session_family(
                    session TEXT PRIMARY KEY, root TEXT NOT NULL
                ) WITHOUT ROWID;
                CREATE TABLE msgs(session TEXT, turn INTEGER, who TEXT);
            """)
            with mock.patch.object(session_context, "DATA_DIR", root):
                db.execute(
                    "INSERT INTO meta VALUES('family_stamp', ?)",
                    (common.session_family_source_stamp(),),
                )
                db.execute(
                    "INSERT INTO session_family VALUES('root', 'root')")
                # SQLite preserves this non-integral value as REAL despite the
                # INTEGER affinity.  It must not be truncated into turn 7.
                db.execute(
                    "INSERT INTO msgs VALUES('root', 7.5, 'recap')")
                db.commit()
                with mock.patch.object(
                        session_context, "calling_identity",
                        return_value=common.CallerIdentity("root", "codex")):
                    family = common.calling_family()
                    policy = common.calling_self_exclusion()
            db.close()
        self.assertIsNotNone(family)
        self.assertIsNone(family.recap_turn)
        self.assertIsNone(policy)

    def test_windowed_policy_only_excludes_the_callers_proven_window(self) -> None:
        family = common.CallingFamily(
            "root", "root",
            frozenset({"root", "child", "custom-side", "agent-name-only"}),
            True, 7, frozenset({"custom-side"}),
            descendants=frozenset({"child", "agent-a1b2c3"}),
            window_members=frozenset({"agent-a1b2c3"}))
        policy = common.SelfExclusion(family, 7, "window")
        # Only the caller's proven current window is excluded.
        self.assertFalse(policy.excludes("root", 6))
        self.assertTrue(policy.excludes("root", 7))
        self.assertTrue(policy.labels("root", 6))
        # Missing or malformed turn evidence cannot prove that a caller row is
        # inside the window, so the automatic policy fails open.
        self.assertFalse(policy.excludes("root", None))
        self.assertFalse(policy.excludes("root", "not-a-turn"))
        self.assertFalse(policy.excludes("root", "7"))
        self.assertFalse(policy.excludes("root", 7.0))
        self.assertFalse(policy.excludes("root", True))
        self.assertFalse(policy.labels("root", None))
        self.assertFalse(policy.labels("root", "not-a-turn"))
        self.assertFalse(policy.labels("root", "6"))
        self.assertFalse(policy.labels("root", 6.0))
        self.assertFalse(policy.labels("root", True))
        malformed_policy = common.SelfExclusion(family, "7", "window")
        self.assertFalse(malformed_policy.excludes("root", 9))
        self.assertFalse(malformed_policy.excludes("agent-a1b2c3", 0))
        self.assertFalse(malformed_policy.labels("child", 0))
        self.assertEqual(malformed_policy.query_filters(), {})
        # A descendant spawned inside the window is hidden whole: its turns
        # restart at zero, so no caller boundary can split it.
        self.assertTrue(policy.excludes("agent-a1b2c3", 0))
        self.assertTrue(policy.excludes("agent-a1b2c3", 99))
        self.assertTrue(policy.excludes("agent-a1b2c3", None))
        self.assertFalse(policy.labels("agent-a1b2c3", 0))
        # A descendant from before the recap is recoverable history: kept,
        # marked as the caller's own like its pre-boundary turns.
        self.assertFalse(policy.excludes("child", 99))
        self.assertTrue(policy.labels("child", 99))
        # Siblings, ancestors, and foreign sidechains are ordinary history.
        self.assertFalse(policy.excludes("custom-side", 0))
        self.assertFalse(policy.labels("custom-side", 0))
        self.assertFalse(policy.excludes("agent-foreign", 0))
        self.assertFalse(policy.labels("agent-foreign", 0))
        self.assertEqual(policy.query_filters(), {
            "exclude_session": "root", "exclude_session_from_turn": 7,
            "_exclude_sessions": ("agent-a1b2c3",)})
        merged = policy.apply_filters(
            {"_exclude_sessions": ("hidden-side",), "project": "p"})
        self.assertEqual(merged, {
            "project": "p", "exclude_session": "root",
            "exclude_session_from_turn": 7,
            "_exclude_sessions": ("agent-a1b2c3", "hidden-side")})

    def test_auto_policy_needs_a_recap_state_but_forced_is_structural(
            self) -> None:
        # resolved family, recap rows exist but none is a usable boundary
        family = common.CallingFamily(
            "root", "root", frozenset({"root", "child", "sibling"}),
            True, None)
        with mock.patch.object(
                session_context, "calling_family", return_value=family):
            automatic = common.calling_self_exclusion()
            forced = common.calling_self_exclusion(conservative=True)
        self.assertIsNone(automatic)
        self.assertIsNotNone(forced)
        self.assertEqual(forced.reason, "forced")
        self.assertFalse(forced.windowed)
        self.assertTrue(forced.excludes("root", 1))
        self.assertTrue(forced.excludes("child", 99))
        self.assertTrue(forced.excludes("sibling", None))
        self.assertFalse(forced.excludes("other", 1))
        self.assertEqual(forced.query_filters(), {"exclude_session": "root"})

    def test_never_compacted_caller_is_windowed_from_turn_zero(self) -> None:
        family = common.CallingFamily(
            "root", "root", frozenset({"root", "child"}), True, None,
            frozenset({"child"}), never_compacted=True, source="pi-process",
            descendants=frozenset({"child"}),
            window_members=frozenset({"child"}))
        with mock.patch.object(
                session_context, "calling_family", return_value=family):
            policy = common.calling_self_exclusion()
        self.assertIsNotNone(policy)
        self.assertEqual((policy.boundary, policy.reason), (0, "window"))
        self.assertTrue(policy.excludes("root", 0))
        self.assertTrue(policy.excludes("root", 41))
        self.assertFalse(policy.labels("root", 0))
        self.assertTrue(policy.excludes("child", 0))
        self.assertEqual(
            policy.query_filters(),
            {"exclude_session": "root", "exclude_session_from_turn": 0,
             "_exclude_sessions": ("child",)})
        # an unresolved family never becomes a window, compacted or not
        unresolved = family._replace(resolved=False)
        with mock.patch.object(
                session_context, "calling_family", return_value=unresolved):
            self.assertIsNone(common.calling_self_exclusion())

    def test_indexed_no_recap_rows_resolve_as_never_compacted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _publish_family_meta(root, [
                {"session": "root"},
                {"session": "child", "parent": "root"},
            ])
            db = sqlite3.connect(root / "corpus.db")
            db.executescript("""
                CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE session_family(
                    session TEXT PRIMARY KEY, root TEXT NOT NULL,
                    side INTEGER NOT NULL CHECK(side IN (0, 1))
                ) WITHOUT ROWID;
                CREATE TABLE msgs(session TEXT, turn INTEGER, who TEXT);
            """)
            with mock.patch.object(session_context, "DATA_DIR", root):
                db.execute(
                    "INSERT INTO meta VALUES('family_stamp', ?)",
                    (common.session_family_source_stamp(),),
                )
                db.executemany(
                    "INSERT INTO session_family VALUES(?, ?, ?)",
                    (("root", "root", 0), ("child", "root", 1)),
                )
                db.executemany(
                    "INSERT INTO msgs VALUES(?, ?, ?)",
                    (("root", 0, "user"), ("root", 1, "user"),
                     ("child", 0, "subagent")),
                )
                db.commit()
                with mock.patch.object(
                        session_context, "calling_identity",
                        return_value=common.CallerIdentity("root", "pi")):
                    family = common.calling_family()
                    policy = common.calling_self_exclusion()
                    with session_context.calling_family_snapshot() as (
                            _identity, snapshot_family, snapshot_db):
                        # postcompact keeps reading the same state: no recap
                        # boundary to serve, so it stays a proven absence
                        self.assertIsNotNone(snapshot_db)
                        self.assertTrue(snapshot_family.never_compacted)
                        self.assertIsNone(snapshot_family.recap_turn)
            db.close()
        self.assertTrue(family.resolved)
        self.assertTrue(family.never_compacted)
        self.assertIsNone(family.recap_turn)
        self.assertEqual(family.source, "pi")
        self.assertEqual(family.descendants, frozenset({"child"}))
        self.assertEqual(family.window_members, frozenset({"child"}))
        self.assertEqual(policy.boundary, 0)
        self.assertTrue(policy.excludes("child", 0))

    def test_recap_window_hides_descendants_spawned_after_the_recap(
            self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _publish_family_meta(root, [
                {"session": "root"},
                {"session": "child-old", "parent": "root"},
                {"session": "child-new", "parent": "root"},
                {"session": "child-unstamped", "parent": "root"},
                {"session": "root2"},
                {"session": "child2", "parent": "root2"},
            ])
            db = sqlite3.connect(root / "corpus.db")
            db.executescript("""
                CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE session_family(
                    session TEXT PRIMARY KEY, root TEXT NOT NULL,
                    side INTEGER NOT NULL CHECK(side IN (0, 1))
                ) WITHOUT ROWID;
                CREATE TABLE msgs(session TEXT, turn INTEGER, ts INTEGER, who TEXT);
            """)
            with mock.patch.object(session_context, "DATA_DIR", root):
                db.execute(
                    "INSERT INTO meta VALUES('family_stamp', ?)",
                    (common.session_family_source_stamp(),),
                )
                db.executemany(
                    "INSERT INTO session_family VALUES(?, ?, ?)",
                    (("root", "root", 0), ("child-old", "root", 1),
                     ("child-new", "root", 1), ("child-unstamped", "root", 1),
                     ("root2", "root2", 0), ("child2", "root2", 1)),
                )
                db.executemany(
                    "INSERT INTO msgs VALUES(?, ?, ?, ?)",
                    (("root", 0, 100, "user"), ("root", 6, 900, "agent"),
                     ("root", 7, 1000, "recap"), ("root", 8, 1100, "user"),
                     ("root", None, 2000, "user"),
                     ("child-old", 0, 500, "subagent"),
                     ("child-old", 1, 1200, "tool"),
                     ("child-new", 0, 1500, "subagent"),
                     ("child-unstamped", 0, None, "subagent"),
                     ("peer-old", 0, 200, "subagent"),
                     ("peer-new", 0, 1200, "subagent"),
                     ("root2", 3, None, "recap"),
                     ("child2", 0, 100, "subagent")),
                )
                db.commit()
                with mock.patch.object(
                        session_context, "calling_identity",
                        return_value=common.CallerIdentity("root", "codex")):
                    family = common.calling_family()
                    policy = common.calling_self_exclusion()
                    self.assertTrue(
                        common.indexed_self_exclusion_has_rows(policy))
                    # the notice speaks for exactly the applied set: no
                    # NULL-turn caller row and no retained descendant
                    quiet = common.SelfExclusion(
                        family._replace(window_members=frozenset()), 9,
                        "window")
                    self.assertFalse(
                        common.indexed_self_exclusion_has_rows(quiet))
                    widened = common.SelfExclusion(
                        family._replace(
                            window_members=frozenset({"child-old"})),
                        9, "window")
                    self.assertTrue(
                        common.indexed_self_exclusion_has_rows(widened))
                with mock.patch.object(
                        session_context, "calling_identity",
                        return_value=common.CallerIdentity("root", "pi-process")), \
                        mock.patch.object(
                            session_context, "published_caller",
                            return_value=mock.Mock(sessions=(
                                "root", "child-old", "peer-old", "peer-new"))):
                    published_policy = common.calling_self_exclusion()
                with mock.patch.object(
                        session_context, "calling_identity",
                        return_value=common.CallerIdentity("root2", "codex")):
                    unstamped_recap = common.calling_family()
            db.close()
        self.assertEqual(family.recap_turn, 7)
        self.assertEqual(
            family.descendants,
            frozenset({"child-old", "child-new", "child-unstamped"}))
        self.assertEqual(family.window_members, frozenset({"child-new"}))
        self.assertEqual(policy.boundary, 7)
        self.assertTrue(policy.excludes("child-new", 0))
        self.assertFalse(policy.excludes("child-old", 1))
        self.assertTrue(policy.labels("child-old", 1))
        self.assertFalse(policy.excludes("child-unstamped", 0))
        self.assertTrue(policy.labels("child-unstamped", 0))
        self.assertEqual(policy.query_filters(), {
            "exclude_session": "root", "exclude_session_from_turn": 7,
            "_exclude_sessions": ("child-new",)})
        # a recap without a timestamp proves nothing about spawn order
        self.assertEqual(unstamped_recap.recap_turn, 3)
        self.assertEqual(unstamped_recap.descendants, frozenset({"child2"}))
        self.assertEqual(unstamped_recap.window_members, frozenset())
        self.assertFalse(published_policy.excludes("child-old", 1))
        self.assertFalse(published_policy.excludes("peer-old", 0))
        self.assertTrue(published_policy.labels("peer-old", 0))
        self.assertTrue(published_policy.excludes("peer-new", 0))

    def test_family_lookup_rejects_a_source_move_after_database_open(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "sessions.jsonl").write_text(
                '{"session":"child","parent":"root"}\n',
                encoding="utf-8",
            )
            db = sqlite3.connect(root / "corpus.db")
            db.executescript("""
                CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE session_family(
                    session TEXT PRIMARY KEY, root TEXT NOT NULL,
                    side INTEGER NOT NULL CHECK(side IN (0, 1))
                ) WITHOUT ROWID;
            """)
            db.execute("INSERT INTO meta VALUES('family_stamp', 'before')")
            db.execute("INSERT INTO session_family VALUES('child', 'root', 1)")
            db.commit()
            db.close()
            with mock.patch.object(session_context, "DATA_DIR", root), \
                    mock.patch.object(
                        session_context,
                        "session_family_source_stamp",
                        side_effect=("before", "after"),
                    ):
                self.assertIsNone(common.indexed_calling_family("child"))

    def test_index_summary_rejects_same_signature_census_damage(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _publish_family_meta(root, [
                {"session": "a", "agent": "codex", "n": 3},
                {"session": "b", "agent": "claude", "n": 2},
            ], "5:stable")
            messages = root / "messages.jsonl"
            messages.write_text("{}\n", encoding="utf-8")
            with mock.patch.object(common, "DATA_DIR", root), \
                    mock.patch.object(session_context, "DATA_DIR", root), \
                    mock.patch.object(common, "MESSAGES_PATH", messages):
                self.assertEqual(
                    common.index_summary()["messages"],
                    5,
                )
                damaged = (
                    '{"session":"a","agent":"codex","n":3}\nnot-json\n',
                    '{"session":"a","agent":"codex"}\n'
                    '{"session":"b","agent":"claude","n":2}\n',
                    '{"session":"a","n":3}\n'
                    '{"session":"b","agent":"claude","n":2}\n',
                    '{"session":"a","agent":"codex","n":true}\n'
                    '{"session":"b","agent":"claude","n":2}\n',
                    '{"session":"a","agent":"codex","n":4}\n'
                    '{"session":"b","agent":"claude","n":2}\n',
                )
                for body in damaged:
                    with self.subTest(body=body):
                        (root / "sessions.jsonl").write_text(
                            body, encoding="utf-8")
                        self.assertIsNone(common.index_summary())

    def test_session_prefix_lookup_is_bounded_but_exact_still_wins(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rows = [
                {"session": f"bulk-{index:05d}"}
                for index in range(common.SESSION_PREFIX_MAX_CANDIDATES + 8)
            ]
            _publish_family_meta(root, rows)
            db = sqlite3.connect(root / "corpus.db")
            db.executescript("""
                CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE session_family(
                    session TEXT PRIMARY KEY, root TEXT NOT NULL,
                    side INTEGER NOT NULL CHECK(side IN (0, 1))
                ) WITHOUT ROWID;
            """)
            with mock.patch.object(session_context, "DATA_DIR", root):
                db.execute(
                    "INSERT INTO meta VALUES('family_stamp', ?)",
                    (common.session_family_source_stamp(),),
                )
                db.executemany(
                    "INSERT INTO session_family VALUES(?, ?, ?)",
                    ((row["session"], row["session"], 0) for row in rows),
                )
                db.commit()
                self.assertEqual(
                    common.indexed_session_matches("bulk-00000"),
                    ["bulk-00000"],
                )
                matches = common.indexed_session_matches("bulk-")
                prefix_index = common.indexed_session_prefix_candidates(
                    ("bulk-00000",), prefix_chars=5)
            db.close()
        self.assertIsNotNone(matches)
        self.assertEqual(
            len(matches), common.SESSION_PREFIX_MAX_CANDIDATES + 1)
        self.assertEqual(tuple(prefix_index), ("bulk-00000",))
        self.assertEqual(
            compact.encode_session_target(
                "bulk-00000", prefix_chars=5, session_index=prefix_index),
            "bulk-00000",
        )

    def test_session_prefix_index_fails_to_full_ids_without_the_database(self) -> None:
        target = "abcdef012345"
        with mock.patch.object(
                session_context, "_open_session_family_index",
                return_value=None):
            index = common.indexed_session_prefix_candidates((target,))
        self.assertEqual(tuple(index), (target,))
        self.assertEqual(
            compact.encode_session_target(target, session_index=index), target)

    def test_session_prefix_index_queries_shared_prefix_once(self) -> None:
        class Connection:
            calls = 0

            def execute(self, _sql, _params):
                self.calls += 1
                return (("sem-0000001",), ("sem-0000002",), ("sem-0000003",))

            def close(self):
                return None

        connection = Connection()
        with mock.patch.object(
                session_context, "_open_session_family_index",
                return_value=connection):
            index = common.indexed_session_prefix_candidates(
                ("sem-0000001", "sem-0000002"), prefix_chars=8)
        self.assertEqual(connection.calls, 1)
        self.assertEqual(tuple(index), (
            "sem-0000001", "sem-0000002", "sem-0000003"))
        self.assertEqual(index.force_full, frozenset())

    def test_unresolved_family_does_not_create_an_automatic_policy(self) -> None:
        with mock.patch.object(
                session_context, "calling_identity",
                return_value=common.CallerIdentity("child", "codex")), \
                mock.patch.object(
                    session_context, "_indexed_calling_family_details",
                    return_value=None):
            family = common.calling_family()
            policy = common.calling_self_exclusion()
        self.assertIsNotNone(family)
        self.assertFalse(family.resolved)
        self.assertEqual(family.members, frozenset({"child"}))
        self.assertIsNone(policy)

    def test_freshness_notice_is_universal_actionable_and_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(common, "DATA_DIR", Path(td)):
            indexd_runtime.record_auto_index_health(3, "worker \x1b[31mfailed")
            health = Path(td) / indexd_runtime.AUTO_INDEX_HEALTH
            old = time.time() - 24 * 3600
            os.utime(health, (old, old))

            notice = indexd_runtime.agent_freshness_notice(
                {"CODEX_THREAD_ID": "thread"})
            self.assertIn("history may be stale", notice)
            self.assertIn("3 consecutive", notice)
            # law 7: the notice reports the self-heal, not doctor homework
            self.assertIn("automatic rebuild", notice)
            self.assertNotIn("doctor", notice)
            self.assertNotIn("\x1b", notice)
            human_notice = indexd_runtime.agent_freshness_notice({"TERM": "xterm"})
            self.assertIn("history may be stale", human_notice)
            self.assertIn("3 consecutive", human_notice)

            indexd_runtime.record_auto_index_health(0, "")
            self.assertEqual(indexd_runtime.agent_freshness_notice(
                {"CODEX_THREAD_ID": "thread"}), "")

    def test_freshness_reason_survives_persistence_and_rendering(self) -> None:
        reason = "failure-start-" + "x" * 500 + "-failure-end"
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(common, "DATA_DIR", Path(td)):
            indexd_runtime.record_auto_index_health(3, reason)
            self.assertEqual(indexd_runtime.indexd_failure_state()[1], reason)
            notice = indexd_runtime.agent_freshness_notice({"TERM": "xterm"})
            disclosure = indexd_runtime.machine_freshness(checked=True)
        self.assertIn("failure-start", notice)
        self.assertIn("failure-end", notice)
        self.assertLessEqual(
            len(disclosure["reason"]),
            indexd_runtime._FRESHNESS_RENDER_MAX_CHARS)

    def test_non_object_health_record_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(common, "DATA_DIR", Path(td)):
            (Path(td) / indexd_runtime.AUTO_INDEX_HEALTH).write_text(
                "[]", encoding="utf-8")
            self.assertEqual(indexd_runtime.indexd_failing(), (0, ""))
            failure = indexd_runtime.indexing_failure()
            self.assertEqual(failure.code, "freshness-ledger-unavailable")
            self.assertIn("history may be stale", indexd_runtime.agent_freshness_notice(
                {"CODEX_THREAD_ID": "thread"}))
            disclosure = indexd_runtime.machine_freshness(checked=True)
            self.assertEqual(disclosure["state"], "unknown")
            self.assertFalse(disclosure["checked"])

    def test_nonfinite_health_values_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(common, "DATA_DIR", Path(td)):
            (Path(td) / indexd_runtime.AUTO_INDEX_HEALTH).write_text(
                '{"streak":1e1000,"ts":1e1000}', encoding="utf-8")
            self.assertEqual(indexd_runtime.indexd_failure_state(), (0, "", 0.0))
            self.assertEqual(indexd_runtime.indexd_failing(), (0, ""))
            self.assertEqual(
                indexd_runtime.indexing_failure().code,
                "freshness-ledger-unavailable")

    def test_huge_integer_and_inconsistent_health_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(common, "DATA_DIR", Path(td)):
            path = Path(td) / indexd_runtime.AUTO_INDEX_HEALTH
            records = (
                '{"streak":1,"last_err":"failed","ts":' + "9" * 400 + "}",
                '{"streak":' + "9" * 5000 + ',"last_err":"failed","ts":1}',
                json.dumps({"streak": 3, "last_err": "real failure",
                            "ts": time.time()})[:-1]
                + ',"streak":0,"last_err":""}',
                json.dumps({"streak": 0, "last_err": "fatal disk failure",
                            "ts": time.time()}),
                json.dumps({"streak": 1, "last_err": "", "ts": time.time()}),
            )
            for record in records:
                with self.subTest(record=record[:40]):
                    path.write_text(record, encoding="utf-8")
                    disclosure = indexd_runtime.machine_freshness(checked=True)
                    self.assertEqual(disclosure["state"], "unknown")
                    self.assertFalse(disclosure["checked"])

    def test_escalation_marker_round_trips_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(common, "DATA_DIR", Path(td)):
            indexd_runtime.record_auto_index_health(3, "wedged", escalated=True)
            self.assertTrue(indexd_runtime.auto_index_escalated())
            self.assertEqual(indexd_runtime.indexd_failing(), (3, "wedged"))
            indexd_runtime.record_auto_index_health(0, "")
            self.assertFalse(indexd_runtime.auto_index_escalated())
            path = Path(td) / indexd_runtime.AUTO_INDEX_HEALTH
            for record in (
                json.dumps({"streak": 3, "last_err": "wedged",
                            "ts": time.time(), "escalated": "yes"}),
                json.dumps({"streak": 0, "last_err": "",
                            "ts": time.time(), "escalated": True}),
            ):
                with self.subTest(record=record[:60]):
                    path.write_text(record, encoding="utf-8")
                    self.assertFalse(indexd_runtime.auto_index_escalated())
                    self.assertEqual(indexd_runtime.indexd_failing(), (0, ""))
            path.write_text(
                json.dumps({"streak": 2, "last_err": "old wedge",
                            "ts": time.time()}), encoding="utf-8")
            self.assertFalse(indexd_runtime.auto_index_escalated())
            self.assertEqual(indexd_runtime.indexd_failing(), (2, "old wedge"))

    def test_health_reader_rejects_special_and_oversize_files(self) -> None:
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(common, "DATA_DIR", Path(td)):
            path = Path(td) / indexd_runtime.AUTO_INDEX_HEALTH
            with path.open("wb") as stream:
                stream.truncate(128 * 1024 * 1024)
            self.assertEqual(
                indexd_runtime.machine_freshness(checked=True)["state"],
                "unknown")
            path.unlink()
            if hasattr(os, "mkfifo"):
                os.mkfifo(path)
                started = time.perf_counter()
                disclosure = indexd_runtime.machine_freshness(checked=True)
                self.assertLess(time.perf_counter() - started, 0.5)
                self.assertEqual(disclosure["state"], "unknown")
                path.unlink()
                source = Path(td) / ".source-health.json"
                os.mkfifo(source)
                started = time.perf_counter()
                disclosure = indexd_runtime.machine_freshness(checked=True)
                self.assertLess(time.perf_counter() - started, 0.5)
                self.assertEqual(disclosure["code"], "source-unreadable")

    def test_unreadable_ledger_cannot_hide_behind_a_known_source_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(common, "DATA_DIR", Path(td)):
            root = Path(td)
            (root / indexd_runtime.AUTO_INDEX_HEALTH).write_text(
                "[]", encoding="utf-8")
            (root / ".source-health.json").write_text(json.dumps({
                "code": "source-unreadable",
                "issues": [{"path": "/history", "reason": "denied"}],
            }), encoding="utf-8")
            disclosure = indexd_runtime.machine_freshness(checked=True)
        self.assertEqual(disclosure["state"], "unknown")
        self.assertEqual(disclosure["code"], "freshness-ledger-unavailable")
        self.assertFalse(disclosure["checked"])
        self.assertIn("/history", disclosure["reason"])

    def test_recursive_health_json_is_unknown_on_bounded_python_decoders(self) -> None:
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(common, "DATA_DIR", Path(td)):
            (Path(td) / indexd_runtime.AUTO_INDEX_HEALTH).write_text(
                "{}", encoding="utf-8")
            with mock.patch.object(
                    indexd_runtime.json, "loads",
                    side_effect=RecursionError("decoder limit")):
                disclosure = indexd_runtime.machine_freshness(checked=True)
        self.assertEqual(disclosure["state"], "unknown")
        self.assertFalse(disclosure["checked"])


if __name__ == "__main__":
    unittest.main()
