"""The explorer is authenticated, read-only, and inert to indexed content."""

from __future__ import annotations

import http.client
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest import mock
from urllib.parse import quote

from _test_support import isolate_data_dir

isolate_data_dir()
import explore  # noqa: E402
import search  # noqa: E402
import server  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]


class ExplorerSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = server.ExplorerServer(0)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01},
            daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method: str, path: str, *,
                headers: dict[str, str] | None = None,
                body: bytes | None = None) -> tuple[int, dict, bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        meta = {name.lower(): value for name, value in response.getheaders()}
        status = response.status
        connection.close()
        return status, meta, payload

    def enroll(self) -> str:
        status, headers, _body = self.request(
            "POST", "/auth", body=b"", headers={
                "Content-Length": "0",
                "Origin": f"http://127.0.0.1:{self.port}",
                "X-Agrep-Bootstrap": self.server.bootstrap_token,
            })
        self.assertEqual(status, 204)
        return headers["set-cookie"].split(";", 1)[0]

    def test_shell_has_a_nonce_csp_and_no_indexed_html_sink(self) -> None:
        token = self.server.bootstrap_token
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertEqual(headers["connection"], "close")
        self.assertIn("default-src 'none'", headers["content-security-policy"])
        self.assertIn("script-src 'nonce-", headers["content-security-policy"])
        text = body.decode("utf-8")
        self.assertNotIn(token, text)
        self.assertNotIn(".innerHTML", text)
        self.assertNotIn("insertAdjacentHTML", text)
        self.assertNotIn("document.write", text)
        self.assertNotRegex(text, r"\son[a-z]+\s*=")

    def test_data_requires_cookie_and_bootstrap_is_one_use(self) -> None:
        status, _headers, body = self.request("GET", "/api/status")
        self.assertEqual(status, 401)
        self.assertIn("fresh explorer link", body.decode("utf-8"))

        cookie = self.enroll()
        status, _headers, body = self.request(
            "GET", "/api/status", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("indexed", json.loads(body))

        status, _headers, _body = self.request(
            "POST", "/auth", body=b"", headers={
                "Content-Length": "0",
                "Origin": f"http://127.0.0.1:{self.port}",
                "X-Agrep-Bootstrap": self.server.auth_token,
            })
        self.assertEqual(status, 403)

    def test_host_and_origin_checks_block_rebinding(self) -> None:
        status, _headers, _body = self.request(
            "GET", "/", headers={"Host": "attacker.example"})
        self.assertEqual(status, 403)
        status, _headers, _body = self.request(
            "POST", "/auth", body=b"", headers={
                "Content-Length": "0", "Origin": "https://attacker.example",
                "X-Agrep-Bootstrap": self.server.bootstrap_token,
            })
        self.assertEqual(status, 403)

    def test_client_disconnect_during_send_stays_silent(self) -> None:
        for failure_at in ("headers", "body"):
            for failure in (
                    BrokenPipeError(), ConnectionResetError(),
                    ConnectionAbortedError()):
                with self.subTest(
                        failure_at=failure_at,
                        failure=type(failure).__name__):
                    handler = object.__new__(server.Handler)
                    handler.send_response = mock.Mock()
                    handler.send_header = mock.Mock()
                    handler._headers = mock.Mock()
                    handler.end_headers = mock.Mock()
                    handler.wfile = mock.Mock()
                    target = (handler.end_headers if failure_at == "headers"
                              else handler.wfile.write)
                    target.side_effect = failure
                    handler._send(200, b"body", "text/plain")
                    self.assertTrue(handler.close_connection)
                    self.assertEqual(
                        handler.wfile.write.call_count,
                        0 if failure_at == "headers" else 1)

    def test_mutation_routes_do_not_exist(self) -> None:
        cookie = self.enroll()
        status, _headers, body = self.request(
            "POST", "/api/reindex", body=b"", headers={
                "Content-Length": "0", "Cookie": cookie,
                "Origin": f"http://127.0.0.1:{self.port}",
            })
        self.assertEqual(status, 405)
        self.assertIn("read-only", body.decode("utf-8"))

    def test_search_keeps_hostile_transcript_text_in_json(self) -> None:
        cookie = self.enroll()
        payload = 'x</button><img src=x onerror="steal()">'
        result = {
            "engine": "fixture", "total": 1, "totals_exact": True,
            "hits": [{
                "session": "session", "turn": 4, "agent": "codex",
                "project": "fixture", "who": "user", "snippet": payload,
            }],
        }
        with mock.patch.object(search, "run_query", return_value=result), \
                mock.patch.object(search, "public_rows", side_effect=lambda rows: rows):
            status, headers, body = self.request(
                "GET", "/api/search?q=needle", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        self.assertEqual(json.loads(body)["hits"][0]["snippet"], payload)

    def test_search_discloses_a_pending_tool_lane(self) -> None:
        cookie = self.enroll()
        result = {
            "engine": "explore", "total": 0, "totals_exact": False,
            "tools_excluded": True, "hits": [],
        }
        with mock.patch.object(search, "run_query", return_value=result):
            status, _headers, body = self.request(
                "GET", "/api/search?q=needle", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["tools_excluded"], {"reason": "tools-pending"})
        self.assertFalse(payload["totals_exact"])

    def test_context_drops_tool_output_and_caps_visible_payloads(self) -> None:
        cookie = self.enroll()
        raw = {
            "session": "session", "agent": "codex", "project": "fixture",
            "concept": "", "title": "", "n_msgs": 1,
            "first_turn": 1, "last_turn": 1, "center": 1,
            "turns": [{
                "turn": 1, "ts": 0, "who": "user",
                "text": "t" * (server.MAX_TURN_CHARS + 1), "reply": "ok",
            }],
            "events": [{
                "turn": 1, "ts": 0, "kind": "tool", "name": "Read",
                "input": "i" * (server.MAX_TOOL_INPUT_CHARS + 1),
                "output": "secret output", "ok": True,
            }],
        }
        with mock.patch.object(explore, "get_window", return_value=raw):
            status, _headers, body = self.request(
                "GET", "/api/around?session=session&turn=1",
                headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        window = json.loads(body)
        self.assertNotIn("output", window["events"][0])
        self.assertTrue(window["events"][0]["input_truncated"])
        self.assertTrue(window["turns"][0]["text_truncated"])

    def test_context_has_aggregate_turn_event_and_character_caps(self) -> None:
        raw = {
            "turns": [
                {"turn": index, "text": "t" * 20_000, "reply": "r" * 20_000}
                for index in range(server.MAX_WINDOW_TURNS + 8)
            ],
            "events": [
                {"turn": index, "input": "i" * 2_000}
                for index in range(server.MAX_WINDOW_EVENTS + 8)
            ],
        }
        window = server._public_window(raw)
        self.assertTrue(window["truncated"])
        self.assertLessEqual(len(window["turns"]), server.MAX_WINDOW_TURNS)
        self.assertLessEqual(len(window["events"]), server.MAX_WINDOW_EVENTS)
        self.assertLessEqual(
            sum(len(row["text"]) + len(row["reply"])
                for row in window["turns"]),
            server.MAX_WINDOW_TURN_CHARS)
        self.assertLessEqual(
            sum(len(row["input"]) for row in window["events"]),
            server.MAX_WINDOW_EVENT_CHARS)
        self.assertGreater(window["omitted_turns"], 0)
        self.assertGreater(window["omitted_events"], 0)

    def test_search_rejects_invalid_mode_and_bounds_row_count(self) -> None:
        cookie = self.enroll()
        status, _headers, _body = self.request(
            "GET", "/api/search?q=x&mode=magic", headers={"Cookie": cookie})
        self.assertEqual(status, 400)
        status, _headers, body = self.request(
            "GET", "/api/search?q=x&sort=oldest", headers={"Cookie": cookie})
        self.assertEqual(status, 400)
        self.assertIn("sort must be score or time", body.decode("utf-8"))
        status, _headers, body = self.request(
            "GET", "/api/search?q=x&mode=semantic&sort=time",
            headers={"Cookie": cookie})
        self.assertEqual(status, 400)
        self.assertIn("meaning search is relevance-ordered", body.decode("utf-8"))
        with mock.patch.object(search, "run_query", return_value={
                "engine": "fixture", "total": 0, "hits": []}) as run:
            status, _headers, _body = self.request(
                "GET", f"/api/search?q={quote('needle')}&n=999999&sort=time",
                headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertEqual(run.call_args.kwargs["limit"], server.MAX_SEARCH_ROWS)
        self.assertEqual(run.call_args.kwargs["sort"], "time")
        self.assertEqual(json.loads(_body)["sort"], "time")

    def test_search_does_not_retry_stable_snapshot_damage(self) -> None:
        cookie = self.enroll()
        with mock.patch.object(
                search, "run_query",
                side_effect=search.NativeEventScanError("damaged")) as run:
            status, _headers, body = self.request(
                "GET", "/api/search?q=needle", headers={"Cookie": cookie})
        self.assertEqual(status, 503, body.decode("utf-8"))
        run.assert_called_once()

    def test_meaning_search_is_bounded_and_never_downloads_a_model(self) -> None:
        cookie = self.enroll()
        with mock.patch.object(search, "run_query", return_value={
                "engine": "semantic:fixture", "mode": "semantic",
                "total": 0, "hits": [], "partial": True,
                "semantic_coverage": {
                    "indexed": 4, "total": 10, "pending": 6,
                    "complete": False,
                },
                "semantic_status": {"state": "ready", "complete": False},
                "fallback_recommended": True, "truncated": True,
        }) as run:
            status, _headers, body = self.request(
                "GET", "/api/search?q=natural+language&mode=semantic",
                headers={"Cookie": cookie})
        self.assertEqual(status, 200, body.decode("utf-8"))
        self.assertEqual(run.call_args.kwargs["semantic_timeout_s"], 5.0)
        self.assertTrue(run.call_args.kwargs["semantic_process_guard"])
        self.assertFalse(run.call_args.kwargs["allow_model_download"])
        semantic = json.loads(body)["semantic"]
        self.assertEqual(semantic["state"], "ready")
        self.assertEqual(semantic["indexed"], 4)
        self.assertEqual(semantic["total"], 10)
        self.assertTrue(semantic["partial"])
        self.assertTrue(semantic["fallback_recommended"])

    def test_slow_meaning_does_not_block_status_or_board(self) -> None:
        cookie = self.enroll()
        started = threading.Event()
        release = threading.Event()
        request_result = []

        def meaning(*_args, **_kwargs):
            started.set()
            release.wait(2)
            return {
                "engine": "semantic:fixture", "mode": "semantic",
                "total": 0, "hits": [],
            }

        def request_meaning():
            request_result.append(self.request(
                "GET", "/api/search?q=wait&mode=semantic",
                headers={"Cookie": cookie}))

        with mock.patch.object(search, "run_query", side_effect=meaning):
            thread = threading.Thread(target=request_meaning)
            thread.start()
            self.assertTrue(started.wait(1))
            before = time.monotonic()
            status, _headers, _body = self.request(
                "GET", "/api/status", headers={"Cookie": cookie})
            elapsed = time.monotonic() - before
            release.set()
            thread.join(timeout=2)
        self.assertEqual(status, 200)
        self.assertLess(elapsed, 0.5)
        self.assertEqual(request_result[0][0], 200)

    def test_board_prefers_resident_and_projects_a_bounded_safe_snapshot(self) -> None:
        cookie = self.enroll()
        token = "gAAAAAB" + "x" * 96 + "=="
        capability = "urlsafe-bootstrap-token"
        snapshot = {
            "now": 1234, "window_s": 90,
            "_agrep_live_ipc": {
                "published_at_ms": 1200, "recent_trimmed": True,
                "recent_events_omitted": 7,
            },
            "sessions": [{
                "agent": "codex", "session": "child", "parent": "root",
                "sub": True, "working": True, "last_ts": 1100,
                "state": "tool Read", "recent": [{
                    "type": "tool", "ts": float("nan"), "tn": 4,
                    "name": "Read", "text": "opaque " + token,
                    "input": (
                        'agrep ui http://127.0.0.1:8732/#bootstrap='
                        + capability + ' --project agrep '
                        + 'token/count fixture-id --token-count 2048 '
                        + '-H "X-Agrep-Bootstrap: header-token" '
                        + '-H "Authorization: Bearer bearer.token.value" '
                        + '--api-key api-key-value '
                        + '{"task":"probe","message":"' + token + '"}'
                        + "y" * 1000),
                    "output": "must not cross the API", "ok": True,
                }],
            }, {
                "agent": "codex", "session": "root", "working": False,
                "last_ts": 1000, "state": "waiting",
                "last_text": "Readable task title " + token,
                "queued_text": "queued " + token, "recent": [{
                    "type": "tool", "ts": 801 + index, "name": "Read",
                } for index in range(server.MAX_BOARD_EVENTS + 1)],
            }, {
                "agent": "claude", "session": "orphan", "parent": "gone",
                "sub": True, "working": False, "last_ts": 900,
                "recent": [],
            }],
        }
        with mock.patch(
                "indexd_runtime.resident_indexd_live_snapshot",
                return_value=snapshot), \
                mock.patch("hookless.live.watcher") as watcher:
            status, _headers, body = self.request(
                "GET", "/api/board", headers={"Cookie": cookie})
        self.assertEqual(status, 200, body.decode("utf-8"))
        board = json.loads(body)
        self.assertEqual(board["source"], "resident")
        self.assertEqual(board["working"], 1)
        self.assertEqual(board["side"], 2)
        self.assertEqual(board["hidden_orphan_sides"], 1)
        self.assertEqual(
            [row["session"] for row in board["sessions"]],
            ["root", "child"])
        self.assertEqual(board["sessions"][0]["side_total"], 1)
        self.assertEqual(board["sessions"][0]["side_working"], 1)
        root = board["sessions"][0]
        self.assertEqual(
            root["title"], "Readable task title [encrypted payload]")
        self.assertEqual(root["queued_text"], "queued [encrypted payload]")
        self.assertEqual(
            len(board["sessions"][0]["recent"]), server.MAX_BOARD_EVENTS)
        event = board["sessions"][1]["recent"][0]
        self.assertNotIn("output", event)
        self.assertEqual(len(event["input"]), server.MAX_BOARD_TOOL_CHARS)
        self.assertIn("--project agrep", event["input"])
        self.assertIn("token/count fixture-id --token-count 2048", event["input"])
        self.assertIn('"task":"probe"', event["input"])
        self.assertIn("[encrypted payload]", event["input"])
        self.assertNotIn("gAAAAAB", event["input"])
        for secret in (
                capability, "header-token", "bearer.token.value",
                "api-key-value"):
            self.assertNotIn(secret, event["input"])
        self.assertGreaterEqual(event["input"].count("[redacted]"), 4)
        self.assertEqual(event["text"], "opaque [encrypted payload]")
        self.assertEqual(event["ts"], 0)
        self.assertTrue(board["recent_trimmed"])
        watcher.assert_not_called()

    def test_browser_board_uses_family_activity_and_collapses_done_sides(self) -> None:
        rows = [{
            "agent": "claude", "session": "old-family", "working": False,
            "active": True, "last_ts": 100, "recent": [],
        }, {
            "agent": "claude", "session": "child-done", "parent": "old-family",
            "sub": True, "working": False, "active": True,
            "last_ts": 900, "recent": [],
        }, {
            "agent": "claude", "session": "other", "working": False,
            "active": True, "last_ts": 800, "recent": [],
        }]
        ordered = server._board_order([
            server._public_board_session(row) for row in rows])
        self.assertEqual([row["session"] for row in ordered],
                         ["old-family", "other"])
        self.assertEqual(ordered[0]["family_last_ts"], 900)
        self.assertEqual(ordered[0]["side_total"], 1)
        self.assertEqual(ordered[0]["side_working"], 0)

    def test_browser_board_counts_the_same_classified_session_population(self) -> None:
        snapshot = {
            "booting": False, "now": 1_000, "window_s": 90,
            "degraded_sources": [], "last_err": "",
            "sessions": [{
                "agent": "claude", "session": "root", "working": False,
                "active": True, "last_ts": 100, "recent": [],
            }, {
                "agent": "claude", "session": "child", "parent": "root",
                "sub": False, "working": True, "active": True,
                "last_ts": 200, "recent": [],
            }],
        }
        payload = server._public_board(snapshot, "fixture")
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["roots"], 1)
        self.assertEqual(payload["side"], 1)
        self.assertEqual(payload["hidden_orphan_sides"], 0)
        self.assertEqual(payload["working"], 1)
        self.assertEqual(
            [(row["session"], row["sub"]) for row in payload["sessions"]],
            [("root", False), ("child", True)])
        self.assertFalse(payload["sessions"][0]["working"])
        self.assertTrue(payload["sessions"][0]["family_working"])

    def test_board_fallback_watcher_starts_once_and_reports_scanning(self) -> None:
        cookie = self.enroll()
        watcher = mock.Mock()
        watcher.snapshot.return_value = {
            "booting": True, "now": 1234, "sessions": [],
        }
        with mock.patch(
                "indexd_runtime.resident_indexd_live_snapshot",
                return_value=None), \
                mock.patch("hookless.live.watcher", return_value=watcher) as make:
            for _ in range(2):
                status, _headers, body = self.request(
                    "GET", "/api/board", headers={"Cookie": cookie})
                self.assertEqual(status, 200, body.decode("utf-8"))
                self.assertEqual(json.loads(body)["status"], "scanning")
        make.assert_called_once_with()
        watcher.wait_boot.assert_called_once_with(0.18)
        self.assertEqual(watcher.snapshot.call_count, 2)

    def test_recent_chats_hide_sidechains_and_bound_enrichment(self) -> None:
        cookie = self.enroll()
        hostile = 'x</button><img src=x onerror="steal()">'
        with mock.patch.object(explore, "list_chats", return_value={
                "total": 1,
                "chats": [{
                    "session": "session", "agent": "codex",
                    "project": "fixture", "n_msgs": 12,
                    "first_ts": 1, "last_ts": 2,
                    "first_text": hostile, "title": hostile,
                    "summary": "s" * 2000, "concept": "topic",
                    "side": False,
                }],
        }) as listed:
            status, headers, body = self.request(
                "GET", "/api/chats?n=999999", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        payload = json.loads(body)
        self.assertEqual(payload["chats"][0]["title"], hostile)
        self.assertTrue(payload["chats"][0]["summary_truncated"])
        listed.assert_called_once_with(
            server.MAX_RECENT_CHATS, include_side=False)

    def test_server_is_ipv4_loopback_only(self) -> None:
        self.assertEqual(self.server.server_address[0], "127.0.0.1")


class ExplorerPackagingTests(unittest.TestCase):
    def test_recent_history_refetches_without_claiming_live_activity(self) -> None:
        app = (ROOT / "web" / "app.html").read_text(encoding="utf-8")
        self.assertIn('aria-pressed="true">recent history</button>', app)
        self.assertIn('<div class="board-rail-label">live activity · read only</div>', app)
        self.assertIn("async function refreshRecent(options = {})", app)
        self.assertIn('window.addEventListener("focus", refreshRecentOnActivation)', app)
        self.assertIn('window.addEventListener("pageshow", refreshRecentOnActivation)', app)
        self.assertIn('else refreshRecentOnActivation();', app)
        self.assertIn('refreshRecent({ force: true, announce: true });', app)
        self.assertIn("const epoch = ++state.recentEpoch;", app)
        self.assertIn("state.recentController?.abort();", app)
        self.assertIn("if (epoch !== state.recentEpoch", app)
        self.assertIn("if (state.recentController === controller)", app)

    def test_board_copy_names_the_live_window_not_indexed_recency(self) -> None:
        app = (ROOT / "web" / "app.html").read_text(encoding="utf-8")
        self.assertIn('"main sessions in window"', app)
        self.assertIn(
            "Main sessions retained in the live window and their side agents.", app)
        self.assertIn("main in window", app)
        self.assertIn("No sessions are retained in the current live window.", app)
        self.assertNotIn(
            '["", Number(payload.roots || 0), "recent chats"]', app)
        self.assertNotIn("Active main chats and their side agents.", app)
        self.assertIn('const updated = session.sub ? session.last_ts', app)
        self.assertIn('`${sideTotal} side agent${sideTotal === 1 ? "" : "s"}`', app)

    def test_search_exposes_only_score_and_newest_sort_controls(self) -> None:
        app = (ROOT / "web" / "app.html").read_text(encoding="utf-8")
        self.assertIn('data-sort="score" aria-pressed="true">score</button>', app)
        self.assertIn('data-sort="time" aria-pressed="false">newest</button>', app)
        self.assertIn('sort: state.sort,', app)
        self.assertIn(
            'sortSwitch.hidden = view !== "search" || state.mode === "semantic";', app)
        self.assertIn('if (state.mode === "semantic") state.sort = "score";', app)

    def test_phone_history_has_exclusive_list_and_detail_states(self) -> None:
        app = (ROOT / "web" / "app.html").read_text(encoding="utf-8")
        self.assertIn(
            'body[data-surface="history"] .reader { display: none; }', app)
        self.assertIn(
            'body[data-surface="history"].history-detail .rail { display: none; }',
            app)
        self.assertIn('"quiet-button history-back", "Back to results"', app)
        self.assertIn('function showHistoryList()', app)
        self.assertIn('`${sideWorking} side working`', app)

    def test_history_lists_offer_bounded_depth_without_false_totals(self) -> None:
        app = (ROOT / "web" / "app.html").read_text(encoding="utf-8")
        self.assertIn('const CHAT_MAX_ROWS = 200;', app)
        self.assertIn('"Show more recent chats"', app)
        self.assertIn('const SEARCH_MAX_ROWS = 100;', app)
        self.assertIn('"Show more search results"', app)
        self.assertIn('function searchCount(payload)', app)
        self.assertIn('no returned hits · total unknown', app)
        self.assertIn('No prose hits; tool output is still indexing.', app)
        self.assertNotIn('`${total.toLocaleString()}${suffix}', app)

    def test_history_reader_never_keeps_stale_content_behind_a_new_state(self) -> None:
        app = (ROOT / "web" / "app.html").read_text(encoding="utf-8")
        self.assertIn('function showRecentLanding()', app)
        self.assertIn('showReaderNotice("Nothing to open.", message);', app)
        self.assertIn('showReaderNotice("Search unavailable.", error.message, true);', app)
        self.assertIn('Meaning search is unavailable for this query. Try Grep.', app)

    def test_history_context_has_bounded_earlier_and_later_navigation(self) -> None:
        app = (ROOT / "web" / "app.html").read_text(encoding="utf-8")
        self.assertIn('addPage("Earlier turns"', app)
        self.assertIn('addPage("Later turns"', app)
        self.assertIn('async function loadHistoryWindow(', app)
        self.assertIn('radius: String(radius)', app)
        self.assertIn('reader.scrollTop = 0;', app)

    def test_runtime_manifest_carries_server_and_app(self) -> None:
        manifest = json.loads(
            (ROOT / "py" / "runtime_manifest.json").read_text(encoding="utf-8"))
        sources = {item["source"] for item in manifest["files"]}
        self.assertIn("py/server.py", sources)
        self.assertIn("web/app.html", sources)


if __name__ == "__main__":
    unittest.main()
