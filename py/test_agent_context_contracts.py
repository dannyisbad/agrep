"""Contracts for agent detection, caller identity, and freshness disclosure."""

from __future__ import annotations

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
                        session_context, "calling_session", return_value="child"):
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
                        session_context, "calling_session", return_value="root"):
                    policy = common.calling_self_exclusion()
            db.close()
        self.assertIsNotNone(policy)
        self.assertEqual(policy.boundary, 11)
        self.assertFalse(policy.excludes("root", 10))
        self.assertTrue(policy.excludes("root", 11))
        self.assertTrue(policy.labels("root", 10))
        self.assertFalse(policy.excludes("child", 99))
        self.assertFalse(policy.labels("child", 99))

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
                        session_context, "calling_session", return_value="root"):
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
            True, 7, frozenset({"custom-side"}))
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
        self.assertEqual(malformed_policy.query_filters(), {})
        # Related children and sidechains are ordinary history. Family shape
        # alone is not evidence that their independently numbered turns are in
        # the caller's current context window.
        self.assertFalse(policy.excludes("agent-a1b2c3", 0))
        self.assertFalse(policy.excludes("agent-a1b2c3", 99))
        self.assertFalse(policy.labels("agent-a1b2c3", 0))
        self.assertFalse(policy.excludes("child", 99))
        self.assertFalse(policy.labels("child", 99))
        self.assertFalse(policy.excludes("agent-foreign", 0))
        self.assertFalse(policy.labels("agent-foreign", 0))

    def test_auto_policy_requires_a_proven_boundary_but_forced_is_structural(
            self) -> None:
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
                session_context, "calling_session", return_value="child"), \
                mock.patch.object(
                    session_context, "_indexed_calling_family_state",
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
