"""Read-only loopback explorer for agrep's published history index."""

from __future__ import annotations

import argparse
from contextlib import nullcontext
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import secrets
import sys
import threading
import time
import webbrowser
from urllib.parse import parse_qs, urlparse

import common
import surface_policy as surface


MAX_QUERY_BYTES = 4096
MAX_SEARCH_ROWS = 100
MAX_RECENT_CHATS = 200
MAX_BOARD_SESSIONS = 160
MAX_BOARD_EVENTS = 12
MAX_WINDOW_TURNS = 25
MAX_WINDOW_EVENTS = 96
MAX_TURN_CHARS = 32_000
MAX_TOOL_INPUT_CHARS = 8_000
MAX_WINDOW_TURN_CHARS = 192_000
MAX_WINDOW_EVENT_CHARS = 48_000
MAX_BOARD_MESSAGE_CHARS = 4_000
MAX_BOARD_TOOL_CHARS = 400
COOKIE_TTL_S = 12 * 60 * 60
APP_PATH = common.REPO_ROOT / "web" / "app.html"


def _cookie_name(port: int) -> str:
    import hashlib

    scope = f"{common.DATA_DIR.resolve()}\0{port}".encode(
        "utf-8", "surrogatepass")
    return "agrep_ui_" + hashlib.sha256(scope).hexdigest()[:12]


def _bounded_text(value: object, limit: int) -> tuple[str, bool]:
    text = str(value or "")
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _bounded_board_text(value: object, limit: int) -> tuple[str, bool]:
    text = surface.redact_live_secrets(value)
    return _bounded_text(text, limit)


def _nonnegative_int(value: object, default: int = 0) -> int:
    if type(value) not in (int, float):
        return default
    if isinstance(value, float) and not math.isfinite(value):
        return default
    try:
        return max(0, int(value))
    except (OverflowError, ValueError):
        return default


def _public_window(raw: dict) -> dict:
    raw_turns = raw.get("turns")
    raw_turns = raw_turns if isinstance(raw_turns, list) else []
    turns = []
    turn_chars = MAX_WINDOW_TURN_CHARS
    for source in raw_turns[:MAX_WINDOW_TURNS]:
        if not isinstance(source, dict) or turn_chars <= 0:
            break
        text_limit = min(MAX_TURN_CHARS, turn_chars)
        text, text_cut = _bounded_text(source.get("text"), text_limit)
        turn_chars -= len(text)
        reply_limit = min(MAX_TURN_CHARS, turn_chars)
        reply, reply_cut = _bounded_text(source.get("reply"), reply_limit)
        turn_chars -= len(reply)
        turns.append({
            "turn": source.get("turn"), "ts": source.get("ts"),
            "who": source.get("who"), "text": text, "reply": reply,
            "text_truncated": text_cut,
            "reply_truncated": reply_cut or bool(source.get("reply_truncated")),
        })
    raw_events = raw.get("events")
    raw_events = raw_events if isinstance(raw_events, list) else []
    events = []
    event_chars = MAX_WINDOW_EVENT_CHARS
    for source in raw_events[:MAX_WINDOW_EVENTS]:
        if not isinstance(source, dict) or event_chars <= 0:
            break
        input_limit = min(MAX_TOOL_INPUT_CHARS, event_chars)
        tool_input, cut = _bounded_text(
            source.get("input"), input_limit)
        event_chars -= len(tool_input)
        events.append({
            "turn": source.get("turn"), "ts": source.get("ts"),
            "kind": source.get("kind"), "name": source.get("name"),
            "input": tool_input, "input_truncated": cut,
            "ok": source.get("ok"),
        })
    return {
        key: raw.get(key) for key in (
            "session", "agent", "project", "concept", "title", "n_msgs",
            "first_turn", "last_turn", "center")
    } | {
        "turns": turns, "events": events,
        "truncated": (
            len(turns) < len(raw_turns) or len(events) < len(raw_events)
            or turn_chars <= 0 or event_chars <= 0),
        "omitted_turns": max(0, len(raw_turns) - len(turns)),
        "omitted_events": max(0, len(raw_events) - len(events)),
    }


def _public_chat(raw: dict) -> dict:
    session, _ = _bounded_text(raw.get("session"), MAX_QUERY_BYTES)
    agent, _ = _bounded_text(raw.get("agent"), 128)
    project, _ = _bounded_text(raw.get("project"), 1024)
    title, _ = _bounded_text(raw.get("title"), 256)
    concept, _ = _bounded_text(raw.get("concept"), 256)
    summary, summary_cut = _bounded_text(raw.get("summary"), 1200)
    first_text, first_text_cut = _bounded_text(raw.get("first_text"), 500)
    return {
        "session": session, "agent": agent, "project": project,
        "title": title, "concept": concept, "summary": summary,
        "summary_truncated": summary_cut,
        "first_text": first_text, "first_text_truncated": first_text_cut,
        "n_msgs": _nonnegative_int(raw.get("n_msgs")),
        "first_ts": _nonnegative_int(raw.get("first_ts")),
        "last_ts": _nonnegative_int(raw.get("last_ts")),
        "side": bool(raw.get("side")),
    }


def _public_semantic_state(result: dict) -> dict | None:
    if result.get("mode") != "semantic":
        return None
    coverage = result.get("semantic_coverage")
    coverage = coverage if isinstance(coverage, dict) else {}
    status = result.get("semantic_status")
    status = status if isinstance(status, dict) else {}
    state, _ = _bounded_text(status.get("state") or "unknown", 32)
    return {
        "state": state,
        "complete": bool(status.get("complete") or coverage.get("complete")),
        "partial": bool(result.get("partial") or status.get("partial")),
        "indexed": _nonnegative_int(coverage.get("indexed")),
        "total": _nonnegative_int(coverage.get("total")),
        "pending": _nonnegative_int(coverage.get("pending")),
        "fallback_recommended": bool(result.get("fallback_recommended")),
        "truncated": bool(result.get("truncated")),
    }


def _public_board_event(raw: dict) -> dict:
    kind, _ = _bounded_text(raw.get("type"), 32)
    text_source = raw.get("text")
    if kind == "subagent_result" and not text_source:
        text_source = raw.get("output")
    text, text_cut = _bounded_board_text(text_source, MAX_BOARD_MESSAGE_CHARS)
    tool_input, input_cut = _bounded_board_text(
        raw.get("input"), MAX_BOARD_TOOL_CHARS)
    name, _ = _bounded_text(raw.get("name"), 128)
    event = {
        "type": kind, "ts": _nonnegative_int(raw.get("ts")),
        "turn": _nonnegative_int(raw.get("tn")),
        "name": name, "text": text, "text_truncated": text_cut,
        "input": tool_input, "input_truncated": input_cut,
    }
    if type(raw.get("ok")) is bool:
        event["ok"] = raw["ok"]
    if type(raw.get("dur")) is int and raw["dur"] >= 0:
        event["duration_ms"] = raw["dur"]
    return event


def _public_board_session(raw: dict) -> dict:
    session, _ = _bounded_text(raw.get("session"), 1024)
    agent, _ = _bounded_text(raw.get("agent"), 128)
    project, _ = _bounded_text(raw.get("project"), 512)
    recent = raw.get("recent") if isinstance(raw.get("recent"), list) else []
    title_source = raw.get("title") or raw.get("last_text")
    if not title_source:
        title_source = next((
            event.get("text") for event in reversed(recent)
            if isinstance(event, dict)
            and event.get("type") in {"user", "reply"}
            and str(event.get("text") or "").strip()
        ), "")
    title, title_cut = _bounded_board_text(title_source, 1000)
    model, _ = _bounded_text(raw.get("model"), 256)
    state, _ = _bounded_text(raw.get("state"), 256)
    queued_text, queued_cut = _bounded_board_text(raw.get("queued_text"), 1000)
    parent = raw.get("parent")
    parent, _ = _bounded_text(parent, 1024) if parent else ("", False)
    return {
        "agent": agent, "session": session, "project": project,
        "title": title, "title_truncated": title_cut, "model": model,
        "state": state, "working": bool(raw.get("working")),
        "active": bool(raw.get("active")), "sub": bool(raw.get("sub")),
        "parent": parent or None,
        "last_ts": _nonnegative_int(raw.get("last_ts")),
        "state_ts": _nonnegative_int(raw.get("state_ts")),
        "queued": _nonnegative_int(raw.get("queued")),
        "queued_text": queued_text, "queued_text_truncated": queued_cut,
        "recent": [
            _public_board_event(event)
            for event in recent[-MAX_BOARD_EVENTS:] if isinstance(event, dict)
        ],
    }


def _board_order(rows: list[dict]) -> list[dict]:
    import livetui

    identities = {(row["agent"], row["session"]) for row in rows}
    ordered = livetui._visible(
        {"sessions": rows}, "all", side_mode="collapse", sort="working")
    for row in ordered:
        row["orphan"] = bool(
            row.get("sub") and row.get("parent")
            and (row["agent"], row["parent"]) not in identities)
        row["family_last_ts"] = int(row.get("_family_last_ts") or 0)
        row["family_active"] = bool(row.get("_family_active"))
        row["family_working"] = bool(row.get("_family_working"))
        row["side_total"] = int(row.get("_side_count") or 0)
        row["side_working"] = int(row.get("_side_working") or 0)
        for key in tuple(row):
            if key.startswith("_"):
                row.pop(key)
    return ordered


def _public_board(snapshot: dict, source: str) -> dict:
    import livetui

    raw_rows = snapshot.get("sessions")
    rows = [
        _public_board_session(row) for row in raw_rows or []
        if isinstance(row, dict)
    ] if isinstance(raw_rows, list) else []
    classified = livetui._family_rows(rows)
    ordered = _board_order(classified)
    visible_ids = {
        (str(row.get("agent") or ""), str(row.get("session") or ""))
        for row in ordered
    }
    visible_root_families = {
        (str(row.get("agent") or ""), str(row.get("session") or ""))
        for row in ordered if not row.get("sub")
    }
    hidden_orphan_sides = sum(
        bool(row.get("sub"))
        and (str(row.get("agent") or ""), str(row.get("session") or ""))
        not in visible_ids
        and row.get("_family_key") not in visible_root_families
        for row in classified
    )
    degraded = snapshot.get("degraded_sources")
    degraded_agents = sorted({
        str(item.get("agent") or "agent")[:128]
        for item in degraded or [] if isinstance(item, dict)
    }) if isinstance(degraded, list) else []
    metadata = snapshot.get("_agrep_live_ipc")
    metadata = metadata if isinstance(metadata, dict) else {}
    booting = bool(snapshot.get("booting"))
    warning = bool(snapshot.get("last_err"))
    status = "scanning" if booting else (
        "degraded" if degraded_agents or warning else "ready")
    updated_at = metadata.get("published_at_ms") or snapshot.get("now")
    updated_at = _nonnegative_int(updated_at, int(time.time() * 1000))
    window_s = snapshot.get("window_s")
    window_s = _nonnegative_int(window_s, 90)
    return {
        "status": status, "source": source,
        "updated_at": updated_at,
        "window_s": max(0, min(3600, window_s)),
        "total": len(classified),
        "working": sum(row["working"] for row in classified),
        "roots": sum(not row["sub"] for row in classified),
        "side": sum(row["sub"] for row in classified),
        "hidden_orphan_sides": hidden_orphan_sides,
        "truncated": len(ordered) > MAX_BOARD_SESSIONS,
        "recent_trimmed": bool(metadata.get("recent_trimmed")),
        "recent_events_omitted": max(
            0, _nonnegative_int(metadata.get("recent_events_omitted"))),
        "degraded_agents": degraded_agents,
        "watcher_warning": warning,
        "sessions": ordered[:MAX_BOARD_SESSIONS],
    }


class ExplorerServer(ThreadingHTTPServer):
    """Concurrent loopback server with one-use browser enrollment."""

    allow_reuse_address = False
    daemon_threads = True
    block_on_close = True

    def __init__(self, port: int = 0):
        self.auth_token = secrets.token_urlsafe(32)
        self.bootstrap_token = secrets.token_urlsafe(24)
        self.live_watcher = None
        self._bootstrap_lock = threading.Lock()
        self._board_lock = threading.Lock()
        self._reader_lock = threading.Lock()
        super().__init__(("127.0.0.1", port), Handler)

    def consume_bootstrap(self, supplied: str) -> bool:
        with self._bootstrap_lock:
            expected = self.bootstrap_token
            if not expected or not secrets.compare_digest(supplied, expected):
                return False
            self.bootstrap_token = ""
            return True

    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(10.0)
        return sock, address

    @property
    def explorer_url(self) -> str:
        port = int(self.server_address[1])
        return f"http://127.0.0.1:{port}/#bootstrap={self.bootstrap_token}"

    def board_snapshot(self) -> tuple[dict, str]:
        import indexd_runtime

        resident = indexd_runtime.resident_indexd_live_snapshot()
        if resident is not None:
            return resident, "resident"
        with self._board_lock:
            if self.live_watcher is None:
                from hookless import live

                self.live_watcher = live.watcher()
                self.live_watcher.wait_boot(0.18)
            watcher = self.live_watcher
        return watcher.snapshot(), "foreground"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "agrep"
    sys_version = ""

    def log_message(self, fmt: str, *args) -> None:
        if common.DEBUG:
            common.log("explorer: " + (fmt % args))

    def _expected_hosts(self) -> set[str]:
        port = int(self.server.server_address[1])
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _valid_host(self) -> bool:
        return (self.headers.get("Host") or "").strip().lower() \
            in self._expected_hosts()

    def _valid_origin(self) -> bool:
        origin = (self.headers.get("Origin") or "").strip().lower()
        return origin in {f"http://{host}" for host in self._expected_hosts()}

    def _authenticated(self) -> bool:
        try:
            jar = SimpleCookie()
            jar.load(self.headers.get("Cookie") or "")
            name = _cookie_name(int(self.server.server_address[1]))
            value = jar.get(name)
            return bool(value and secrets.compare_digest(
                value.value, self.server.auth_token))
        except (KeyError, TypeError):
            return False

    def _headers(self, *, nonce: str | None = None) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Cache-Control", "no-store")
        if nonce:
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; "
                f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                "connect-src 'self'; img-src 'self' data:; "
                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
                "object-src 'none'")

    def _send(self, code: int, body: bytes, content_type: str, *,
              nonce: str | None = None,
              headers: dict[str, str] | None = None) -> None:
        self.close_connection = True
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Connection", "close")
            self._headers(nonce=nonce)
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _error(self, code: int, message: str) -> None:
        self._json(code, {"error": message})

    def _query(self) -> tuple[str, dict[str, list[str]]]:
        parsed = urlparse(self.path)
        return parsed.path, parse_qs(parsed.query, keep_blank_values=True)

    @staticmethod
    def _one(params: dict[str, list[str]], name: str,
             default: str = "") -> str:
        values = params.get(name)
        return values[0] if values else default

    def _serve_app(self) -> None:
        try:
            html = APP_PATH.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            self._error(500, "explorer assets are unavailable")
            return
        nonce = secrets.token_urlsafe(18)
        html = html.replace("<style>", f'<style nonce="{nonce}">', 1)
        html = html.replace("<script>", f'<script nonce="{nonce}">', 1)
        self._send(
            200, html.encode("utf-8"), "text/html; charset=utf-8", nonce=nonce)

    def _serve_status(self) -> None:
        try:
            summary = common.index_summary() or {}
        except (OSError, ValueError, TypeError):
            summary = {}
        self._json(200, {
            "messages": int(summary.get("messages") or 0),
            "sessions": int(summary.get("sessions") or 0),
            "indexed": common.MESSAGES_PATH.is_file(),
        })

    def _serve_search(self, params: dict[str, list[str]]) -> None:
        query = self._one(params, "q").strip()
        if not query:
            self._error(400, "enter a search query")
            return
        if len(query.encode("utf-8")) > MAX_QUERY_BYTES:
            self._error(413, "search query is too large")
            return
        mode = self._one(params, "mode", "keyword")
        if mode not in {"keyword", "semantic"}:
            self._error(400, "mode must be keyword or semantic")
            return
        sort = self._one(params, "sort", "score")
        if sort not in {"score", "time"}:
            self._error(400, "sort must be score or time")
            return
        if mode == "semantic" and sort != "score":
            self._error(400, "meaning search is relevance-ordered; use sort=score")
            return
        try:
            limit = min(MAX_SEARCH_ROWS, max(1, int(self._one(params, "n", "40"))))
        except ValueError:
            self._error(400, "n must be an integer")
            return
        try:
            import search

            result = None
            lane_lock = (
                nullcontext() if mode == "semantic"
                else self.server._reader_lock)
            with lane_lock:
                result = search.run_query(
                    query, mode=mode, limit=limit, sort=sort,
                    allow_model_download=False, semantic_timeout_s=5.0,
                    semantic_process_guard=mode == "semantic")
            if result is None:
                self._error(503, "search index is unavailable")
                return
            rows = search.public_rows(result.get("hits") or [])
            response = {
                "query": query, "mode": mode, "sort": sort,
                "engine": result.get("engine"),
                "total": _nonnegative_int(result.get("total")),
                "totals_exact": bool(result.get("totals_exact", True)),
                "hits": rows[:limit],
            }
            if result.get("tools_excluded"):
                response["tools_excluded"] = {
                    "reason": surface.TOOLS_PENDING_ERROR_CODE}
            semantic = _public_semantic_state(result)
            if semantic is not None:
                response["semantic"] = semantic
            self._json(200, response)
        except Exception as exc:  # noqa: BLE001 - the browser gets no local paths
            common.dbg(f"explorer search failed ({exc})", "!")
            self._error(503, "search could not read a complete index snapshot")

    def _serve_chats(self, params: dict[str, list[str]]) -> None:
        try:
            limit = min(
                MAX_RECENT_CHATS,
                max(1, int(self._one(params, "n", "80"))))
        except ValueError:
            self._error(400, "n must be an integer")
            return
        side = self._one(params, "side", "0")
        if side not in {"0", "1"}:
            self._error(400, "side must be 0 or 1")
            return
        try:
            import explore

            with self.server._reader_lock:
                payload = explore.list_chats(limit, include_side=side == "1")
            self._json(200, {
                "total": _nonnegative_int(payload.get("total")),
                "totals_exact": bool(payload.get("totals_exact", True)),
                "chats": [
                    _public_chat(row) for row in payload.get("chats") or []
                ],
            })
        except Exception as exc:  # noqa: BLE001 - the browser gets no local paths
            common.dbg(f"explorer chat list failed ({exc})", "!")
            self._error(503, "recent chats could not read a complete index snapshot")

    def _serve_around(self, params: dict[str, list[str]]) -> None:
        session = self._one(params, "session").strip()
        if not session or len(session.encode("utf-8")) > MAX_QUERY_BYTES:
            self._error(400, "session is required")
            return
        try:
            turn = int(self._one(params, "turn", "0"))
            radius = min(12, max(0, int(self._one(params, "radius", "4"))))
        except ValueError:
            self._error(400, "turn and radius must be integers")
            return
        try:
            import explore

            with self.server._reader_lock:
                window = explore.get_window(session, turn, radius)
        except Exception as exc:  # noqa: BLE001 - the browser gets no local paths
            common.dbg(f"explorer context failed ({exc})", "!")
            self._error(503, "context could not read a complete index snapshot")
            return
        if "error" in window:
            self._error(404, "chat is no longer indexed")
            return
        self._json(200, _public_window(window))

    def _serve_board(self) -> None:
        try:
            snapshot, source = self.server.board_snapshot()
            self._json(200, _public_board(snapshot, source))
        except Exception as exc:  # noqa: BLE001 - the browser gets no local paths
            common.dbg(f"explorer board failed ({exc})", "!")
            self._error(503, "live board is unavailable; run `agrep board --once`")

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if not self._valid_host():
            self._error(403, "invalid host")
            return
        path, params = self._query()
        if path == "/":
            self._serve_app()
            return
        if not self._authenticated():
            self._error(401, "open a fresh explorer link with `agrep ui`")
            return
        if path == "/api/status":
            self._serve_status()
        elif path == "/api/chats":
            self._serve_chats(params)
        elif path == "/api/search":
            self._serve_search(params)
        elif path == "/api/around":
            self._serve_around(params)
        elif path == "/api/board":
            self._serve_board()
        else:
            self._error(404, "not found")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if not self._valid_host() or not self._valid_origin():
            self._error(403, "invalid browser origin")
            return
        path, _params = self._query()
        if path != "/auth":
            self._error(405, "the explorer is read-only")
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self.close_connection = True
            self._error(400, "invalid request length")
            return
        if length != 0:
            self.close_connection = True
            self._error(413, "authentication requests have no body")
            return
        supplied = self.headers.get("X-Agrep-Bootstrap") or ""
        if not self.server.consume_bootstrap(supplied):
            self._error(403, "explorer link is invalid or already used")
            return
        port = int(self.server.server_address[1])
        cookie = (
            f"{_cookie_name(port)}={self.server.auth_token}; Path=/; "
            f"Max-Age={COOKIE_TTL_S}; HttpOnly; SameSite=Strict")
        self._send(204, b"", "text/plain; charset=utf-8",
                   headers={"Set-Cookie": cookie})


def main(argv: list[str] | None = None, *, open_browser: bool = False) -> int:
    verb = "ui" if open_browser else "serve"
    parser = surface.ArgumentParser(
        prog=f"agrep {verb}",
        description=(
            "open agrep's private read-only history explorer with live Board"
            if open_browser else
            "serve agrep's private read-only history explorer with live Board "
            "on loopback"),
        allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            f"  agrep {verb}               use an available loopback port\n"
            f"  agrep {verb} --port 8732   request a specific loopback port\n\n"
            "The server binds only to 127.0.0.1, requires a one-use browser "
            "link, and has no mutation endpoints.\n"
            "Press Ctrl-C to stop it.\n\n"
            "exit: 0 stopped normally, 2 invalid arguments or bind failure."
        ))
    parser.add_argument(
        "--port", type=int, default=0,
        help="loopback port (default: an available random port)")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65_535:
        parser.error("--port must be between 0 and 65535")
    try:
        server = ExplorerServer(args.port)
    except OSError as exc:
        common.log(f"explorer could not bind to loopback: {exc}")
        return 2
    url = server.explorer_url
    print(f"agrep explorer: {url}", flush=True)
    if open_browser and not webbrowser.open(url):
        common.log("browser did not open automatically; paste the explorer URL")
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
