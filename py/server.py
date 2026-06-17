"""tilt server: the interactive explorer. Serves the app + read-only endpoints over
your indexed chat history. Semantic search lazy-loads the embedder/reranker by
default; pass --warm when the first semantic query must be fast.

  GET  /                 -> web/app.html
  GET  /data             -> report.build_data() (rankings, timeline, vibe-traces) [dashboard view]
  GET  /stats            -> honest corpus totals (all sessions/msgs, summarized subset, vibes)
  GET  /chats            -> [{session,agent,project,n_msgs,title,summary,tags,has_vibe,...}] [the organizer list]
  GET  /concepts         -> top concept threads (grouping chips)
  GET  /concept?id=N     -> one concept thread + every chat inside it (full drilldown)
  GET  /search?q=&level= -> semantic search + rerank; level=chat (default) | message
  GET  /chat?session=ID  -> one chat: summary + per-turn transcript w/ affect + vibe arc
  GET  /vibe?session=ID  -> the on-demand vibe-trace arc JSON (or 404)
  GET  /events?agent=&session=     -> the chat's tool/subagent event stream (capped summaries)
  GET  /event_raw?agent=&session=&call_id= -> ONE event's uncapped payload, from the source store
  GET  /file?p=ABSPATH   -> raw image bytes from disk (whitelisted exts) so live <img> can render pics
  GET  /status           -> index age, tier coverage, semantic state, auto-indexer + watcher health
  POST /reindex          -> force the auto-indexer to rebuild now (the in-app "refresh" button)
  GET  /live/state       -> running-sessions snapshot (passive store tailing, no hooks)
  GET  /live/stream      -> SSE: live events as agents work (EventSource)
  POST /open_native      -> {agent,session} -> resume that session in its CLI, cd'd to its dir

The /chats, /concepts, /concept, /chat, /vibe endpoints are pure file reads (no GPU, no
LLM): the explorer is instant. /search touches the warmed models.

Usage: python server.py [--port 8732]   (no LLM needed; ollama is reindex-only)
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import common
import report
import explore
import rawfetch
import native
import live
import indexer
import setupjobs

# `ask` (the semantic search machinery) pulls numpy/torch/sentence-transformers.
# Imported lazily so the explorer works on a fresh clone with NOTHING but stdlib:
# browse, keyword search, chat detail, events, live view and native resume all run
# before any ML dependency is installed. Semantic search lights up when it loads.
_ASK = {"mod": None, "err": None, "loading": False}
# wall time of the last semantic query (or model load). The idle reaper unloads the
# models after SEM_IDLE_S without one: a long-lived background server shouldn't hold
# gigabytes of torch commit for a feature used in bursts. The next query reloads.
_SEM_USED = 0.0
SEM_IDLE_S = 15 * 60


def _ask_mod():
    global _SEM_USED
    if _ASK["mod"] is None and _ASK["err"] is None:
        _ASK["loading"] = True
        try:
            import ask as _a  # noqa: PLC0415
            _ASK["mod"] = _a
        except Exception as e:  # noqa: BLE001 -- missing deps => degraded mode
            _ASK["err"] = f"{type(e).__name__}: {e}"
            common.log(f"semantic stack unavailable ({_ASK['err']}); "
                       "keyword search still works.")
        finally:
            _ASK["loading"] = False
    if _ASK["mod"] is not None:
        _SEM_USED = time.time()
    return _ASK["mod"]


def _sem_deps_present() -> bool:
    """Are the smart-tier deps importable, WITHOUT importing them (that's gigabytes)?
    Used by /status so a lazy, not-yet-loaded stack still reports as available."""
    import importlib.util
    try:
        return importlib.util.find_spec("sentence_transformers") is not None
    except Exception:  # noqa: BLE001 -- a broken venv reads as absent
        return False


def _sem_reaper() -> None:
    """Unload the embedder/reranker after SEM_IDLE_S without a semantic query."""
    while True:
        time.sleep(60)
        mod = _ASK["mod"]
        if mod is None or not _SEM_USED:
            continue
        try:
            if mod.models_loaded() and time.time() - _SEM_USED > SEM_IDLE_S:
                mod.release_models()
                common.log("semantic models idle; released "
                           "(next semantic search reloads them)")
        except Exception as e:  # noqa: BLE001 -- the reaper must never kill the server
            common.log(f"semantic idle-release failed: {e}")

def _status() -> dict:
    """One honest health blob: how old the index is, what coverage each optional tier
    has, whether semantic search is actually loaded, and whether the live watcher is
    alive. The UI turns this into a status chip instead of leaving people to wonder
    why search feels different or the rail looks stale."""
    import time as _t
    now = _t.time()

    def age(name):
        try:
            return int(now - (common.DATA_DIR / name).stat().st_mtime)
        except OSError:
            return None
    st = explore.stats()
    w = live.watcher()
    idx = indexer.instance()
    return {
        "index_age_s": age("sessions.jsonl"),
        "messages_age_s": age("messages.jsonl"),
        "n_sessions": st["n_sessions"], "n_msgs": st["n_msgs"],
        "coverage": {
            "summaries": st["n_summarized"],
            "vibes": st["n_vibes"],
            "emotions": (common.DATA_DIR / "emotions.jsonl").exists(),
            "embeddings": (common.DATA_DIR / "embeddings.f32").exists(),
        },
        # semantic: "ready" when the stack is loaded OR importable (it lazy-loads on
        # the first semantic query), "loading" only mid-import, "off" when deps absent
        "semantic": ("off" if _ASK["err"] else
                     "loading" if _ASK["loading"] else
                     "ready" if (_ASK["mod"] or _sem_deps_present()) else "off"),
        # the auto-indexer: phase (idle/indexing/error), last run, whether it can run
        "indexer": idx.status() if idx else {"phase": "off", "available": False},
        "watcher": {"loops": w._n_loops, "tracked": len(w._offsets),
                    "last_err": w._last_err,
                    "active": sum(1 for s in w.sessions.values()
                                  if (now * 1000 - max(s["last_ts"], s.get("live_ts", 0)))
                                  <= 90 * 1000)},
        # what to TELL the user to type: `agrep` from an installed package, the dev
        # form in a checkout. Drives every command string the UI prints.
        "cli": common.cli_name(),
    }


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

    _IMG_CT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
               ".svg": "image/svg+xml", ".avif": "image/avif"}

    def _serve_image(self, p: str):
        """Serve an on-disk image by absolute path -- the live view points <img> here so
        the SSE stream stays light and the picture is full-resolution. Read-only,
        localhost-bound, extension-whitelisted (only image types; never arbitrary files),
        and size-capped. Images already compress, so this skips gzip and lets the browser
        cache them."""
        ext = os.path.splitext(p)[1].lower()
        ct = self._IMG_CT.get(ext)
        if not p or not ct:
            self._send(404, "{}")
            return
        try:
            if os.path.getsize(p) > 40 * 1024 * 1024:
                self._send(413, "{}")
                return
            with open(p, "rb") as f:
                data = f.read()
        except OSError:
            self._send(404, "{}")
            return
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=300")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)
        if path in ("/", "/index.html"):
            self._send(200, _app_html(), "text/html; charset=utf-8")
        elif path == "/data":
            self._json(report.build_data)
        elif path == "/chats":
            self._json(explore.list_chats)
        elif path == "/stats":
            self._json(explore.stats)
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
                ask = None if keyword else _ask_mod()
                if not keyword and ask is None:
                    # semantic stack absent (fresh install / no embeddings yet):
                    # serve the keyword answer instead of an error page.
                    keyword, term = True, query.strip().strip('"')
                if keyword:
                    kr = explore.keyword_search(term, k=300)
                    payload = {"query": term, "mode": "keyword",
                               "results": kr["hits"], "total": kr["total"],
                               "chats": kr["chats"]}
                    if _ASK["err"]:
                        payload["note"] = "semantic search unavailable; showing exact matches"
                    self._send(200, json.dumps(payload))
                else:
                    global _SEM_USED
                    if level == "message":
                        results = json.loads(ask.tool_search_messages(query, k=k))
                    else:
                        level = "chat"
                        results = json.loads(ask.tool_search_chats(query, k=k))
                    _SEM_USED = time.time()  # the idle reaper counts from the last query
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
        elif path == "/events":
            agent = (qs.get("agent") or [""])[0]
            sess = (qs.get("session") or [""])[0]
            if not agent or not sess:
                self._send(400, json.dumps({"error": "missing agent/session"}))
                return
            self._json(lambda: {"agent": agent, "session": sess,
                                "events": explore.get_events(agent, sess)})
        elif path == "/event_raw":
            agent = (qs.get("agent") or [""])[0]
            sess = (qs.get("session") or [""])[0]
            cid = (qs.get("call_id") or [""])[0]
            if not agent or not sess or not cid:
                self._send(400, json.dumps({"error": "missing agent/session/call_id"}))
                return
            self._json(lambda: rawfetch.event_raw(agent, sess, cid))
        elif path == "/file":
            self._serve_image((qs.get("p") or [""])[0])
        elif path == "/status":
            self._json(_status)
        elif path == "/doctor":
            # structured tier checks for the setup panel; ~1-2s (venv module probes),
            # called on demand only
            import doctor
            self._json(doctor.probe)
        elif path == "/setup/state":
            # progress of the running (or last) one-click install job
            self._json(setupjobs.state)
        elif path == "/live/state":
            self._json(lambda: live.watcher().snapshot())
        elif path == "/live/stream":
            self._sse()
        else:
            self._send(404, "{}")

    def _sse(self):
        """Server-sent events: one dedicated connection per client, events pushed as the
        watcher tails the stores. ThreadingHTTPServer gives this handler its own thread,
        so blocking on the subscriber queue is fine. Connection: close keeps the SSE
        socket out of the keep-alive pool; EventSource auto-reconnects."""
        w = live.watcher()
        q = w.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            hello = json.dumps({"type": "hello", "sessions": len(w.sessions)})
            self.wfile.write(f"data: {hello}\n\n".encode())
            self.wfile.flush()
            import queue as _queue
            while True:
                try:
                    ev = q.get(timeout=15)
                    self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                except _queue.Empty:
                    self.wfile.write(b": hb\n\n")  # heartbeat comment
                self.wfile.flush()
        except (BrokenPipeError, ConnectionError, OSError):
            pass
        finally:
            w.unsubscribe(q)
            self.close_connection = True

    def _json(self, fn):
        try:
            self._send(200, json.dumps(fn()))
        except Exception as e:  # noqa: BLE001
            self._send(500, json.dumps({"error": str(e)}))

    def do_POST(self):
        if self.path == "/open_native":
            n = int(self.headers.get("Content-Length", 0) or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:  # noqa: BLE001
                body = {}
            agent = body.get("agent", "")
            sess = body.get("session", "")
            # same_window (default on): resume as a new tab in the current terminal window
            # rather than a fresh window. Absent -> the default; explicit False opts out.
            same_window = bool(body.get("same_window", True))
            self._json(lambda: native.open_session(agent, sess, same_window))
            return
        if self.path == "/reindex":
            # the in-app "refresh" button: force the auto-indexer to run now
            idx = indexer.instance()
            if not idx:
                self._send(503, json.dumps({"ok": False, "error": "indexer not running"}))
                return
            idx.trigger()
            self._send(200, json.dumps({"ok": True, "phase": idx.status()["phase"]}))
            return
        if self.path == "/setup/run":
            # one-click install: {"step": "smart"|"named"} -> background job,
            # progress at GET /setup/state
            n = int(self.headers.get("Content-Length", 0) or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:  # noqa: BLE001
                body = {}
            self._json(lambda: setupjobs.start(str(body.get("step", ""))))
            return
        if self.path == "/setup/restart":
            # relaunch this server under the venv python so freshly installed deps
            # actually load (the failed `import ask` is cached for the process life)
            self._send(200, json.dumps({"ok": True}))
            threading.Thread(target=_restart_self, name="tilt-restart").start()
            return
        self._send(404, "{}")

    def log_message(self, *a):  # quiet
        pass


_SRV: ThreadingHTTPServer | None = None


def _restart_self() -> None:
    """Swap this process for a fresh server under the venv python. Shut the listener
    down first so the port is free, spawn the replacement detached (it must outlive
    us), then exit. The page survives in the browser; its polls fail for a second or
    two and then land on the new process."""
    time.sleep(0.4)  # let the /setup/restart response reach the browser
    py = str(setupjobs.VENV_PY if setupjobs.VENV_PY.exists() else sys.executable)
    argv = [py, str(Path(__file__).resolve()), *sys.argv[1:]]
    common.log(f"setup: restarting server under {py}")
    try:
        if _SRV:
            _SRV.shutdown()
            _SRV.server_close()
    except Exception:  # noqa: BLE001
        pass
    logf = (common.DATA_DIR / "server.log").open("ab")
    kw: dict = {"stdin": subprocess.DEVNULL, "stdout": logf, "stderr": logf}
    if sys.platform == "win32":
        kw["creationflags"] = 0x00000208  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(argv, cwd=str(common.REPO_ROOT), **kw)
    os._exit(0)


def _reap_superseded() -> None:
    """If a previous agrep server is still alive (its terminal died, it didn't),
    retire it: two servers means double the caches and double any loaded models.
    The portfile carries its pid; only kill after its /status answers like ours."""
    portfile = common.DATA_DIR / ".server"
    try:
        info = json.loads(portfile.read_text(encoding="utf-8"))
        port, pid = int(info["port"]), int(info["pid"])
    except (OSError, ValueError, KeyError):
        return
    if pid == os.getpid():
        return
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=2) as r:
            if "semantic" not in json.loads(r.read().decode("utf-8", "replace")):
                return  # something else owns that port now; leave it alone
    except Exception:  # noqa: BLE001 -- nothing listening: stale file, nothing to reap
        return
    try:
        os.kill(pid, 15)
        common.log(f"retired previous server (pid {pid}, port {port})")
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8732)
    ap.add_argument("--warm", action="store_true",
                    help="pre-load the semantic models at startup (default: lazy on first semantic search)")
    ap.add_argument("--no-warm", action="store_true", help=argparse.SUPPRESS)  # pre-lazy-default flag, kept harmless
    ap.add_argument("--no-autoindex", action="store_true",
                    help="don't auto-rebuild the index on new activity (reindex by hand)")
    ap.add_argument("--no-autosmart", action="store_true",
                    help="don't opportunistically refresh affect/summaries while idle")
    args = ap.parse_args()
    _reap_superseded()

    # All warming happens BEHIND the bound port: the rail reads only the small
    # materialized files, so the browser gets its directory immediately while the
    # read tables load in the background. The semantic models are NOT loaded here by
    # default -- they're gigabytes of commit for a burst-used feature, so they load
    # on the first semantic query and the idle reaper releases them after SEM_IDLE_S.
    # --warm restores eager loading for setups where the first-query wait matters.
    def _warm():
        try:
            common.log("warming read caches (messages / affect / replies) ...")
            explore.warm_caches()
            common.log("read caches warm.")
        except Exception as e:  # noqa: BLE001
            common.log(f"read-cache warm failed (endpoints lazy-load): {e}")
        if args.warm:
            common.log("warming embedder + reranker ...")
            try:
                ask = _ask_mod()
                if ask is None:
                    raise RuntimeError(_ASK["err"] or "ask module unavailable")
                ask._embedder()
                ask._reranker()
                common.log("warm.")
            except Exception as e:  # noqa: BLE001
                common.log(f"warm failed (semantic lazy/disabled, rest works): {e}")

    threading.Thread(target=_warm, daemon=True, name="tilt-warm").start()
    threading.Thread(target=_sem_reaper, daemon=True, name="tilt-sem-reaper").start()
    w = live.watcher()  # start tailing the agent stores (passive, hook-free)
    if not args.no_autoindex:
        indexer.start(w, auto_smart=not args.no_autosmart)
    # Drop a portfile so CLI commands (agrep search links, --semantic) find THIS server
    # without being told the port. Best-effort; a stale file is harmless (the CLI probes
    # reachability before trusting it).
    portfile = common.DATA_DIR / ".server"
    try:
        portfile.write_text(json.dumps({"port": args.port, "pid": os.getpid()}),
                            encoding="utf-8")
        import atexit
        atexit.register(lambda: portfile.unlink(missing_ok=True))
    except OSError:
        pass
    common.log(f"tilt server -> http://localhost:{args.port}")
    global _SRV
    _SRV = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    _SRV.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
