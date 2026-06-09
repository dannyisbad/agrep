"""tilt server: the interactive explorer. Serves the app + read-only endpoints over
your indexed chat history, plus the LLM ask agent. Warms the embedder/reranker once at
startup so /ask is fast (no per-call model reload).

  GET  /                 -> web/app.html
  GET  /data             -> report.build_data() (rankings, timeline, vibe-traces) [dashboard view]
  GET  /chats            -> [{session,agent,project,n_msgs,summary,tags,has_vibe,...}] [the organizer list]
  GET  /concepts         -> top concept threads (grouping chips)
  GET  /concept?id=N     -> one concept thread + every chat inside it (full drilldown)
  GET  /search?q=&level= -> semantic search + rerank; level=chat (default) | message
  GET  /chat?session=ID  -> one chat: summary + per-turn transcript w/ affect + vibe arc
  GET  /vibe?session=ID  -> the on-demand vibe-trace arc JSON (or 404)
  POST /ask              -> {question} -> ask.ask() -> {answer, steps:[{tool,args,result}]}

The /chats, /concepts, /concept, /chat, /vibe endpoints are pure file reads (no GPU, no
LLM): the explorer is instant. /search and /ask touch the warmed models.

Usage: python server.py [--port 8732]   (needs `ollama serve` + gemma4 pulled for /ask)
"""

from __future__ import annotations

import argparse
import gzip
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import common
import report
import explore
import ask

# Serve the app shell from memory, re-reading only when the file changes on disk.
_HTML: dict = {"mtime": -1.0, "body": ""}


def _app_html() -> str:
    p = common.PY_DIR.parent / "web" / "app.html"
    m = p.stat().st_mtime
    if _HTML["mtime"] != m:
        _HTML["body"], _HTML["mtime"] = p.read_text(encoding="utf-8"), m
    return _HTML["body"]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive: reuse the connection across endpoint calls

    def _send(self, code, body, ctype="application/json"):
        b = body.encode("utf-8") if isinstance(body, str) else body
        gz = ""
        # gzip when the client accepts it and the payload is worth compressing
        # (/chats + /data are big JSON; HTML compresses ~4x). Tiny bodies skip it.
        if len(b) > 700 and "gzip" in (self.headers.get("Accept-Encoding", "") or ""):
            b = gzip.compress(b, compresslevel=6)
            gz = "gzip"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if gz:
            self.send_header("Content-Encoding", gz)
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)
        if path in ("/", "/index.html"):
            self._send(200, _app_html(), "text/html; charset=utf-8")
        elif path == "/data":
            self._json(report.build_data)
        elif path == "/chats":
            self._json(explore.list_chats)
        elif path == "/concepts":
            self._json(explore.list_concepts)
        elif path == "/concept":
            cid = (qs.get("id") or [""])[0]
            if not cid:
                self._send(400, json.dumps({"error": "missing id"}))
                return
            self._json(lambda: explore.chats_in_concept(int(cid)))
        elif path == "/search":
            query = (qs.get("q") or [""])[0].strip()
            if not query:
                self._send(400, json.dumps({"error": "missing q"}))
                return
            level = (qs.get("level") or ["chat"])[0].lower()
            try:
                k = int((qs.get("k") or ["10"])[0])
            except ValueError:
                k = 10
            # A quoted query ("...") OR mode=keyword => exact substring search over the real
            # message/reply text. Otherwise the warmed semantic search (bi-encoder + reranker).
            mode = (qs.get("mode") or [""])[0]
            quoted = len(query) >= 2 and query[0] == '"' and query[-1] == '"'
            keyword = mode == "keyword" or quoted
            term = query[1:-1].strip() if quoted else query
            try:
                if keyword:
                    kr = explore.keyword_search(term, k=300)
                    self._send(200, json.dumps({"query": term, "mode": "keyword",
                                                "results": kr["hits"], "total": kr["total"],
                                                "chats": kr["chats"]}))
                else:
                    if level == "message":
                        results = json.loads(ask.tool_search_messages(query, k=k))
                    else:
                        level = "chat"
                        results = json.loads(ask.tool_search_chats(query, k=k))
                    self._send(200, json.dumps({"query": query, "mode": "semantic", "level": level, "results": results}))
            except Exception as e:  # noqa: BLE001
                self._send(500, json.dumps({"error": str(e), "query": query}))
        elif path == "/chat":
            sess = (qs.get("session") or [""])[0]
            if not sess:
                self._send(400, json.dumps({"error": "missing session"}))
                return
            self._json(lambda: explore.get_chat(sess))
        elif path == "/vibe":
            sess = (qs.get("session") or [""])[0]
            v = explore.get_vibe(sess) if sess else None
            if v is None:
                self._send(404, json.dumps({"error": "no vibe trace"}))
            else:
                self._send(200, json.dumps(v))
        else:
            self._send(404, "{}")

    def _json(self, fn):
        try:
            self._send(200, json.dumps(fn()))
        except Exception as e:  # noqa: BLE001
            self._send(500, json.dumps({"error": str(e)}))

    def do_POST(self):
        if self.path == "/ask":
            n = int(self.headers.get("Content-Length", 0) or 0)
            try:
                q = json.loads(self.rfile.read(n) or b"{}").get("question", "")
            except Exception:  # noqa: BLE001
                q = ""
            if not q.strip():
                self._send(400, json.dumps({"error": "empty question"}))
                return
            try:
                out = ask.ask(q)
            except Exception as e:  # noqa: BLE001
                out = {"answer": f"error: {e}", "steps": []}
            self._send(200, json.dumps(out))
        else:
            self._send(404, "{}")

    def log_message(self, *a):  # quiet
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8732)
    ap.add_argument("--no-warm", action="store_true", help="skip pre-warming models (dashboard-only)")
    args = ap.parse_args()
    # Parse the read tables once at boot so every /chat is a dict lookup, not a 50 MB scan.
    # Cheap (one pass) and GPU-independent, so warm them even with --no-warm.
    try:
        common.log("warming read caches (messages / affect / replies) ...")
        explore.warm_caches()
        common.log("read caches warm.")
    except Exception as e:  # noqa: BLE001
        common.log(f"read-cache warm failed (endpoints lazy-load): {e}")
    if not args.no_warm:
        common.log("warming embedder + reranker ...")
        try:
            ask._embedder()
            ask._reranker()
            common.log("warm.")
        except Exception as e:  # noqa: BLE001
            common.log(f"warm failed (queries will lazy-load): {e}")
    common.log(f"tilt server -> http://localhost:{args.port}")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
