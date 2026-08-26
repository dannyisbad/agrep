from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from _test_support import isolate_data_dir


isolate_data_dir()

import common  # noqa: E402
import corpusdb  # noqa: E402
import explore  # noqa: E402
import indexd_runtime  # noqa: E402
import search  # noqa: E402
import surface_policy as surface  # noqa: E402


def _row(number: int, text: str) -> dict:
    return {
        "id": f"codex:s{number}:{number}",
        "session": f"s{number}",
        "agent": "codex",
        "project": "repo",
        "turn": number,
        "ts": number,
        "who": "user",
        "text": text,
    }


def _proof_row(path: Path) -> dict:
    identity = corpusdb._proof_file_identity(path)
    if corpusdb._PLATFORM_NAME == "posix":
        token = {"Metadata": corpusdb._unix_change_token(identity[2])}
    elif corpusdb._PLATFORM_NAME == "nt":
        token = {"ContentSha256": list(corpusdb._content_sha256(path, identity))}
    else:
        token = {"Metadata": 0}
    return {
        "name": path.name,
        "len": identity[0],
        "modified_ns": identity[1],
        "change_token": token,
        "edge_hash": corpusdb._edge_hash(path, identity[0]),
    }


def _publish(
        root: Path, rows: list[dict], signature: str, *,
        malformed_tail: str = "", replies: list[dict] | None = None,
        malformed_reply_tail: str = "",
        event_generation: str | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    messages = "".join(
        json.dumps(row, separators=(",", ":")) + "\n" for row in rows)
    (root / "messages.jsonl").write_text(
        messages + malformed_tail, encoding="utf-8")
    reply_body = "".join(
        json.dumps(row, separators=(",", ":")) + "\n"
        for row in (replies or []))
    (root / "replies.jsonl").write_text(
        reply_body + malformed_reply_tail, encoding="utf-8")
    session_rows = sorted(rows, key=lambda row: str(row["session"]))
    (root / "sessions.jsonl").write_text("".join(
        json.dumps({
            "session": row["session"], "agent": row["agent"],
            "project": row["project"], "first_ts": row["ts"],
            "last_ts": row["ts"], "n": 1, "parent": "",
        }, separators=(",", ":")) + "\n" for row in session_rows
    ), encoding="utf-8")
    families = [(str(row["session"]), "") for row in session_rows]
    (root / common.SESSION_FAMILY_META_FILE).write_text(json.dumps({
        "version": common.SESSION_FAMILY_INDEX_VERSION,
        "algorithm": common.SESSION_FAMILY_DIGEST_ALGORITHM,
        "ingest_signature": signature,
        "count": len(families),
        "digest": common.session_family_digest(families),
    }, separators=(",", ":")), encoding="utf-8")
    for name, body in (
            ("boundary_stats.json", b"{}"),
            (".boundary_stats.bin", b"fixture"),
            ("event_stats.json", b"{}")):
        (root / name).write_bytes(body)
    proof = {
        "version": corpusdb._DERIVED_PROOF_VERSION,
        "signature": signature,
        "files": [
            _proof_row(root / name) for name in corpusdb._DERIVED_PROOF_NAMES
        ],
    }
    (root / ".derived_generation.json").write_text(
        json.dumps(proof, separators=(",", ":")), encoding="utf-8")
    (root / ".ingest.sig").write_text(signature + "\n", encoding="utf-8")
    if event_generation is not None:
        events_dir = root / explore.EVENTS_DIR_NAME
        events_dir.mkdir(exist_ok=True)
        (events_dir / common.EVENT_GENERATION_NAME).write_text(
            event_generation, encoding="utf-8")


def _publication_state(root: Path) -> str:
    with mock.patch.object(common, "DATA_DIR", root):
        return str(corpusdb._derived_publication_health()["state"])


class JsonlSnapshotTruthTests(unittest.TestCase):
    def setUp(self) -> None:
        indexd_runtime._clear_freshen_failure()
        self._reset_explore()

    def tearDown(self) -> None:
        indexd_runtime._clear_freshen_failure()
        self._reset_explore()

    @staticmethod
    def _reset_explore() -> None:
        explore._GEN = ("jsonl-snapshot-truth-reset",)
        explore._freshen()

    def _count(
            self, root: Path, *, loader=None, event_loader=None,
            tools: bool = False, argv: list[str] | None = None,
    ) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        explore._GEN = None
        with contextlib.ExitStack() as stack:
            for target, name, value in (
                    (common, "DATA_DIR", root),
                    (common, "MESSAGES_PATH", root / "messages.jsonl"),
                    (common, "INGEST_SIG_PATH", root / ".ingest.sig"),
                    (corpusdb, "DB_PATH", root / "corpus.db"),
                    (corpusdb, "INGEST_SIG_PATH", root / ".ingest.sig"),
                    (corpusdb, "BOUNDARY_STATS_PATH", root / "boundary_stats.json")):
                stack.enter_context(mock.patch.object(target, name, value))
            stack.enter_context(mock.patch.object(
                common, "setting", side_effect=lambda name: (
                    "on" if tools and name == "tools" else "off")))
            stack.enter_context(mock.patch.object(
                common, "in_agent_context", return_value=False))
            stack.enter_context(mock.patch.object(
                common, "ingest_bin", return_value=root / "missing-bin"))
            stack.enter_context(mock.patch.object(
                corpusdb, "connect", return_value=None))
            stack.enter_context(mock.patch.object(
                corpusdb, "_trigram_ok", return_value=True))
            stack.enter_context(mock.patch.object(
                indexd_runtime, "ensure_index", return_value=True))
            stack.enter_context(mock.patch.object(
                indexd_runtime, "search_index_build_pending", return_value=False))
            stack.enter_context(mock.patch.object(
                indexd_runtime, "agent_freshness_notice", return_value=""))
            stack.enter_context(mock.patch.object(
                indexd_runtime, "freshness_story",
                return_value=surface.FreshnessStory("current")))
            stack.enter_context(mock.patch.object(
                search, "_semantic_runtime_installed", return_value=False))
            stack.enter_context(mock.patch.object(
                search, "_jsonl_native_keyword", return_value=None))
            if loader is not None:
                stack.enter_context(mock.patch.object(
                    explore, "_messages_by_session_read", side_effect=loader))
            if event_loader is not None:
                stack.enter_context(mock.patch.object(
                    common, "event_blobs_bulk", side_effect=event_loader))
            stack.enter_context(contextlib.redirect_stdout(stdout))
            stack.enter_context(contextlib.redirect_stderr(stderr))
            rc = search.main(argv or ["needle", "-c", "--lexical"])
        return rc, stdout.getvalue(), stderr.getvalue()

    def _assert_valid_family_generation(self, root: Path) -> None:
        self.assertIsNotNone(common.session_family_source_stamp(root))
        self.assertEqual(_publication_state(root), "ready")

    def test_stable_valid_generation_keeps_exact_fallback_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _publish(root, [
                _row(1, "needle one"), _row(2, "needle two"),
            ], "generation-1")
            self._assert_valid_family_generation(root)
            rc, stdout, stderr = self._count(root)

        self.assertEqual((rc, stdout, stderr), (0, "2\n", ""))

    def test_malformed_committed_jsonl_cannot_claim_an_exact_count(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _publish(
                root, [_row(1, "needle from the valid row")], "generation-1",
                malformed_tail=(
                    '{"id":"codex:s2:2","session":"s2",'
                    '"text":"needle from the damaged row"\n'))
            self._assert_valid_family_generation(root)
            rc, stdout, _stderr = self._count(root)

        self.assertEqual((rc, stdout), (2, ""))

    def test_completed_generation_move_retries_the_new_exact_count(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _publish(root, [_row(1, "needle generation one")], "generation-1")
            self._assert_valid_family_generation(root)
            original_loader = explore._messages_by_session_read.__wrapped__
            moved = False

            def moving_loader():
                nonlocal moved
                loaded = original_loader()
                if not moved:
                    moved = True
                    _publish(root, [
                        _row(1, "needle generation two"),
                        _row(2, "needle added during scan"),
                    ], "generation-2")
                return loaded

            rc, stdout, stderr = self._count(root, loader=moving_loader)
            self._assert_valid_family_generation(root)

        self.assertTrue(moved)
        self.assertEqual((rc, stdout, stderr), (0, "2\n", ""))

    def test_live_owned_generation_move_restarts_the_full_exact_query(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _publish(root, [_row(1, "needle generation one")], "generation-1")
            self._assert_valid_family_generation(root)
            original_loader = explore._messages_by_session_read.__wrapped__
            reads = 0

            def moving_loader():
                nonlocal reads
                loaded = original_loader()
                reads += 1
                if reads == 1:
                    _publish(root, [
                        _row(1, "needle generation two"),
                        _row(2, "needle added during scan"),
                    ], "generation-2")
                return loaded

            with mock.patch.object(
                    corpusdb, "query_publication_active",
                    side_effect=[False, True, False]) as publishing, \
                    mock.patch.object(search.time, "sleep") as sleep:
                rc, stdout, stderr = self._count(
                    root, loader=moving_loader)
            self._assert_valid_family_generation(root)

        self.assertEqual((rc, stdout, stderr), (0, "2\n", ""))
        self.assertEqual(reads, 2)
        self.assertEqual(publishing.call_count, 3)
        sleep.assert_not_called()

    def test_same_signature_body_rewrite_does_not_starve_a_scan(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _publish(root, [_row(1, "needle stable")], "generation-1")
            self._assert_valid_family_generation(root)
            original_loader = explore._messages_by_session_read.__wrapped__
            reads = 0

            def touching_loader():
                nonlocal reads
                rows = original_loader()
                reads += 1
                (root / ".ingest.sig").write_text(
                    "generation-1\n", encoding="utf-8")
                return rows

            rc, stdout, _stderr = self._count(root, loader=touching_loader)

        self.assertEqual(reads, 1)
        self.assertEqual((rc, stdout), (0, "1\n"))

    def test_native_snapshot_rejects_windows_no_usn_interior_damage(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            long_text = "a" * 2_048 + " needle " + "z" * 2_048
            with mock.patch.object(corpusdb, "_PLATFORM_NAME", "nt"):
                _publish(
                    root, [_row(1, long_text)], "generation-1",
                    event_generation="events-1")
            path = root / "messages.jsonl"
            stamp = path.stat()
            body = bytearray(path.read_bytes())
            body[1_024] = ord("b")
            path.write_bytes(body)
            os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))

            with (
                mock.patch.object(common, "DATA_DIR", root),
                mock.patch.object(common, "MESSAGES_PATH", path),
                mock.patch.object(corpusdb, "_PLATFORM_NAME", "nt"),
                mock.patch.object(
                    corpusdb, "_windows_file_state", return_value=(93, None)),
                mock.patch.object(explore, "_kick_derived_repair"),
            ):
                snapshot = explore.native_event_scan_snapshot()

        self.assertIsNone(snapshot)

    def test_malformed_reply_row_also_invalidates_the_exact_count(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _publish(
                root, [_row(1, "needle from the valid row")], "generation-1",
                malformed_reply_tail=(
                    '{"id":"codex:s1:1","reply":"damaged reply"\n'))
            self._assert_valid_family_generation(root)
            rc, stdout, _stderr = self._count(root)

        self.assertEqual((rc, stdout), (2, ""))

    def test_schema_mutant_message_never_silently_omits_or_crashes(self) -> None:
        modes = (
            ["needle", "-c", "--lexical"],
            ["needle", "-w", "-c"],
            ["needle", "-E", "-c"],
        )
        for argv in modes:
            with self.subTest(argv=argv), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                mutant = _row(2, "needle")
                mutant["text"] = 123
                _publish(
                    root, [_row(1, "needle from the valid row"), mutant],
                    "generation-1")
                self._assert_valid_family_generation(root)
                rc, stdout, _stderr = self._count(root, argv=argv)

            self.assertEqual((rc, stdout), (2, ""))

    def test_malformed_tool_event_fails_every_exact_python_lane_closed(self) -> None:
        payload = (
            b'{"kind":"tool","name":"needle valid","ts":1}\n'
            b'{"kind":"tool","name":"needle damaged"\n')

        def events_for(keys, **_kwargs):
            return iter(
                (agent, session, payload) for agent, session in list(keys))

        modes = (
            ["needle", "-c", "--lexical"],
            ["needle", "-w", "-c"],
            ["needle", "-E", "-c"],
        )
        for argv in modes:
            with self.subTest(argv=argv), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                _publish(
                    root, [_row(1, "needle from prose")], "generation-1",
                    event_generation="events-1")
                rc, stdout, _stderr = self._count(
                    root, tools=True, event_loader=events_for, argv=argv)

            self.assertEqual((rc, stdout), (2, ""))

    def test_completed_tool_event_move_retries_the_current_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _publish(
                root, [_row(1, "needle from prose")], "generation-1",
                event_generation="events-1")
            marker = root / explore.EVENTS_DIR_NAME / common.EVENT_GENERATION_NAME
            moved = False

            def moving_events(keys, **_kwargs):
                nonlocal moved
                list(keys)
                if not moved:
                    moved = True
                    marker.write_text("events-2", encoding="utf-8")
                return iter(())

            rc, stdout, stderr = self._count(
                root, tools=True, event_loader=moving_events)

        self.assertTrue(moved)
        self.assertEqual((rc, stdout, stderr), (0, "1\n", ""))

    def test_live_owned_event_publication_finishes_before_the_scan(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _publish(
                root, [_row(1, "needle from prose")], "generation-1",
                event_generation="events-1")
            marker = root / explore.EVENTS_DIR_NAME / common.EVENT_GENERATION_NAME
            lock_path = root / ".index.lock"
            ready = threading.Event()

            def publisher() -> None:
                with common.IndexLock("event-publication", timeout=0.1):
                    marker.write_text("events-2", encoding="utf-8")
                    ready.set()
                    time.sleep(0.05)

            with mock.patch.object(common, "INDEX_LOCK_PATH", lock_path), \
                    mock.patch.object(
                        corpusdb.index_lock, "INDEX_LOCK_PATH", lock_path):
                thread = threading.Thread(target=publisher)
                thread.start()
                self.assertTrue(ready.wait(1.0))
                started = time.monotonic()
                rc, stdout, stderr = self._count(
                    root, tools=True,
                    event_loader=lambda _keys, **_kwargs: iter(()))
                elapsed = time.monotonic() - started
                thread.join(1.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual((rc, stdout, stderr), (0, "1\n", ""))
        self.assertGreaterEqual(elapsed, 0.02)
        self.assertLess(elapsed, 1.0)

    def test_native_prose_lane_rejects_consumed_parser_damage(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _publish(
                root, [_row(1, "alpha beta")], "generation-1",
                malformed_tail='{"session":"damaged","text":"alpha beta"\n')
            self._assert_valid_family_generation(root)
            spec = search.QuerySpec(
                q="alpha beta", mode="keyword", limit=4, sort="score",
                agent=None, project=None, who=None, model=None,
                model_soft=False, chat=None, since_ms=None, until_ms=None,
                exhaustive=False, session_limit=None, include_tools=True,
                exclude_session=None, exclude_session_from_turn=None,
                allow_fallback=True, exact_totals=False,
                family_diverse=False, semantic_timeout_s=None)
            explore._GEN = None
            with (
                mock.patch.object(common, "DATA_DIR", root),
                mock.patch.object(
                    common, "MESSAGES_PATH", root / "messages.jsonl"),
                mock.patch.object(common, "setting", return_value="off"),
                mock.patch.object(
                    explore, "native_event_scan_snapshot",
                    return_value={"generations": ("ingest", "events")}),
                mock.patch.object(
                    explore, "native_event_scan_snapshot_current",
                    return_value=True),
                mock.patch.object(
                    explore, "native_event_owner_census_matches",
                    return_value=True),
                self.assertRaises(search.DirectSnapshotQueryError),
            ):
                search._jsonl_native_keyword(
                    spec, {}, search._prepare_boundary(
                        spec.q, spec.mode, None), 10_000,
                    preflight_ok=True)

    def test_json_surface_reports_a_structured_snapshot_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _publish(
                root, [_row(1, "needle from the valid row")], "generation-1",
                malformed_tail='{"session":"damaged","text":"needle"\n')
            self._assert_valid_family_generation(root)
            rc, stdout, stderr = self._count(
                root, argv=["needle", "--json", "--lexical"])

        payload = json.loads(stdout)
        self.assertEqual(rc, 2)
        self.assertEqual(payload["kind"], "agrep-meta")
        self.assertEqual(
            payload["error"]["code"], "direct-snapshot-unverified")
        self.assertEqual(stderr, "")

    def test_unverified_zero_is_not_a_proven_grep_miss(self) -> None:
        result = {
            "hits": [], "total": 0, "chats": 0, "tool_hits": 0,
            "engine": "corpusdb", "mode": "keyword", "totals_exact": True,
        }
        story = surface.FreshnessStory(
            "unverified", code="search-index-stale",
            detail="the published search snapshot is stale")
        freshness = {
            "state": "degraded", "failing": False, "may_be_stale": True,
            "checked": True, "code": "search-index-stale",
        }
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(common, "MESSAGES_PATH", Path(__file__)),
            mock.patch.object(common, "in_agent_context", return_value=False),
            mock.patch.object(common, "ingest_bin", return_value=Path("missing")),
            mock.patch.object(indexd_runtime, "ensure_index", return_value=True),
            mock.patch.object(
                indexd_runtime, "agent_freshness_notice", return_value=""),
            mock.patch.object(
                indexd_runtime, "freshness_story", return_value=story),
            mock.patch.object(
                indexd_runtime, "machine_freshness", return_value=freshness),
            mock.patch.object(
                corpusdb, "machine_freshness_fields",
                side_effect=lambda value, **_kwargs: {
                    "freshness": value, "corpus_age_s": None}),
            mock.patch.object(
                search, "_semantic_runtime_installed", return_value=False),
            mock.patch.object(
                search, "_indexed_corpus_counts",
                return_value={"sessions": 1, "messages": 1}),
            mock.patch.object(search, "run_query", return_value=result),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            rc = search.main(["never-present", "--json", "--lexical"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual((rc, payload["freshness"]["may_be_stale"]), (2, True))


if __name__ == "__main__":
    unittest.main()
