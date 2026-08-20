"""Comprehensive self-test for agrep — every shipped feature, one pass/fail matrix.

    python selftest.py        # run all, print matrix
Exits non-zero if any test FAILS (SKIP is allowed when the environment lacks a fixture,
e.g. no live agent session). Run from the repo's py/ dir.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time

from hookless.registry import AGENT_CONTEXT_ENV_KEYS

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
_RS_EXE = "agrep-rs.exe" if os.name == "nt" else "agrep-rs"
_DEV_RS = os.path.join(REPO, "target", "release", _RS_EXE)
_BUNDLED_RS = os.path.join(REPO, "_bin", _RS_EXE)
RS = os.environ.get("AGREP_RS_BIN") or (
    _DEV_RS if os.path.exists(_DEV_RS) else _BUNDLED_RS)
ENV: dict[str, str] = {}

results = []
_DERIVED_FIXTURE_RESTORES = []


def _establish_current_derived_fixture(root, *, bind_common=False):
    """Bind one synthetic publication to this exact Python+Rust writer build."""
    import json
    from pathlib import Path
    import common
    import corpusdb
    import indexd_runtime

    root = Path(root)
    protected = os.environ.get("AGREP_DATA_READONLY")
    if protected:
        try:
            actual = os.path.normcase(os.path.realpath(root))
            boundary = os.path.normcase(os.path.realpath(protected))
            if os.path.commonpath((actual, boundary)) == boundary:
                raise RuntimeError(
                    "selftest derived fixture is inside AGREP_DATA_READONLY")
        except ValueError as exc:
            raise RuntimeError(
                "selftest derived fixture boundary is unverifiable") from exc
    root.mkdir(parents=True, exist_ok=True)
    saved = (
        common.DATA_DIR,
        indexd_runtime.DERIVED_OWNER_PATH,
        indexd_runtime.INGEST_CACHE_PATH,
        corpusdb.DB_PATH,
    )
    if bind_common:
        common.DATA_DIR = root
    indexd_runtime.DERIVED_OWNER_PATH = root / ".derived-owner.json"
    indexd_runtime.INGEST_CACHE_PATH = root / ".ingest_cache.bin"
    corpusdb.DB_PATH = root / "corpus.db"
    build_id = indexd_runtime.derived_writer_build_id(
        common.ingest_bin(), require_binary=True)
    indexd_runtime.DERIVED_OWNER_PATH.write_text(
        json.dumps(
            {"version": 1, "build_id": build_id},
            separators=(",", ":")),
        encoding="utf-8")

    def restore():
        (common_data_dir,
         indexd_runtime.DERIVED_OWNER_PATH,
         indexd_runtime.INGEST_CACHE_PATH,
         corpusdb.DB_PATH) = saved
        if bind_common:
            common.DATA_DIR = common_data_dir

    _DERIVED_FIXTURE_RESTORES.append(restore)
    return build_id


def check(name, fn):
    restore_boundary = len(_DERIVED_FIXTURE_RESTORES)
    try:
        ok, detail = fn()
        results.append((name, ok, detail))
    except Exception as e:  # noqa: BLE001
        results.append((name, "FAIL", f"exception: {e!r}"))
    finally:
        while len(_DERIVED_FIXTURE_RESTORES) > restore_boundary:
            _DERIVED_FIXTURE_RESTORES.pop()()


# Phrase search must bridge punctuation without bridging alphanumeric runs.
def t_matcher():
    import explore
    pat = explore._kw_pattern("No DWARF binary is stripped")
    text = "No DWARF — binary is `stripped` (confirmed"
    bridges = bool(pat.search(text))
    # and it must NOT match across alphanumerics (in-order, gap = punctuation only)
    nope = bool(pat.search("NoXDWARFXbinaryXisXstripped"))
    return ("PASS" if (bridges and not nope) else "FAIL",
            f"bridges punctuation={bridges}, rejects alnum-run={not nope}")


def t_clean_agent_environment():
    keys = (*AGENT_CONTEXT_ENV_KEYS, "AGREP_PROFILE")
    process_clean = all(not os.environ.get(key) for key in keys)
    child_clean = all(not ENV.get(key) for key in keys)
    return ("PASS" if process_clean and child_clean else "FAIL",
            f"process={process_clean} subprocess={child_clean}")


# AGREP_DEBUG traces must remain on stderr.
def t_debug():
    r = subprocess.run([sys.executable, "-c",
                        "import search; search.main(['the','--color','never','-n','1'])"],
                       cwd=HERE, env={**ENV, "AGREP_DEBUG": "1"},
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=60)
    has = "[agrep +" in r.stderr
    return ("PASS" if has else "FAIL", f"trace lines present={has}")


# Snapshots must expose the sticky working flag.
def t_working():
    from hookless import live
    w = live.watcher()
    time.sleep(2)
    snap = w.snapshot()
    sess = snap.get("sessions", [])
    has_key = all("working" in s for s in sess) if sess else True
    return ("PASS" if has_key else "FAIL",
            f"{len(sess)} sessions, all carry 'working'={has_key}")


# Codex live forks must start at the child handoff and survive restart seeding.
def t_codex_subagent_live():
    """Forked Codex tails start at the child handoff, including restart seeding."""
    import json
    import tempfile
    from pathlib import Path
    from hookless.live import LiveWatcher, TAIL_SEED

    def line(outer, payload, ts="2026-01-02T12:00:00.000Z"):
        return json.dumps({"timestamp": ts, "type": outer, "payload": payload},
                          separators=(",", ":"))

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        routed = root / "rollout-child.jsonl"
        child = "01990004-0002-7000-8000-000000000002"
        turn = "01990004-0003-7000-8000-000000000003"
        parent = "01990004-0001-7000-8000-000000000001"
        handoff = ("Message Type: NEW_TASK\nTask name: /root/model_check\n"
                   "Sender: /root\nPayload:\n")
        followup = ("Message Type: MESSAGE\nTask name: /root/model_check\n"
                    "Sender: /root\nPayload:\n")
        rows = [
            line("session_meta", {"id": child, "cwd": "/work/agrep",
                                   "thread_source": "subagent", "parent_thread_id": parent,
                                   "agent_path": "/root/model_check"}),
            line("session_meta", {"id": parent, "cwd": "/work/agrep"}),
            line("response_item", {"type": "message", "role": "user",
                                    "content": [{"type": "input_text",
                                                 "text": "copied parent must not leak"}]}),
            line("event_msg", {"type": "task_started", "turn_id": turn}),
            line("turn_context", {"model": "gpt-test", "turn_id": turn}),
            line("response_item", {"type": "agent_message", "author": "/root",
                                    "recipient": "/root/model_check",
                                    "content": [{"type": "input_text", "text": handoff}],
                                    "internal_chat_message_metadata_passthrough":
                                        {"turn_id": turn}}),
            line("response_item", {"type": "agent_message", "author": "/root",
                                    "recipient": "/root/model_check",
                                    "content": [{"type": "input_text", "text": followup}],
                                    "internal_chat_message_metadata_passthrough":
                                        {"turn_id": turn}}),
            line("response_item", {"type": "reasoning", "summary": "x" * (TAIL_SEED + 64)}),
            line("response_item", {"type": "function_call", "name": "shell",
                                    "arguments": "{\"command\":\"inspect models\"}",
                                    "call_id": "child-call"}),
            line("response_item", {"type": "function_call_output", "call_id": "child-call",
                                    "output": "Granite inspected"}),
            line("response_item", {"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text",
                                                 "text": "Granite child answer"}]}),
        ]
        routed.write_text("\n".join(rows) + "\n", encoding="utf-8")

        w = LiveWatcher()
        head = w._codex_head(str(routed))
        w._codex_meta[str(routed)] = head[:2]
        w._codex_sub[str(routed)] = head[2]
        size = routed.stat().st_size
        boundary = w._codex_seed_subagent(str(routed), size)
        offset = w._codex_tail_offset(str(routed), size, boundary)
        w._offsets[str(routed)] = offset
        for raw in w._read_delta(str(routed), size):
            w._codex_line(str(routed), raw)
        feed = list(w.ring)
        anchors = [e for e in feed if e.get("type") == "user"]
        replies = [e for e in feed if e.get("type") == "reply"]
        tools = [e for e in feed if e.get("type") == "tool"]
        routed_ok = (len(anchors) == 2 and len(replies) == 1 and len(tools) == 1
                     and all(e.get("sub_session") and e.get("parent") == parent for e in feed)
                     and not any("copied parent" in e.get("text", "") for e in feed)
                     and offset > boundary)

        native = root / "rollout-native.jsonl"
        native_child = "01990003-0003-7000-8000-000000000003"
        native_turn = "01990003-0004-7000-8000-000000000004"
        native_rows = [
            line("session_meta", {"id": native_child, "cwd": "/work/demo-proj",
                                   "thread_source": "subagent", "parent_thread_id": parent,
                                   "source": "vscode"}),
            line("event_msg", {"type": "task_started",
                                "turn_id": "01990003-0001-7000-8000-000000000001"}),
            line("event_msg", {"type": "task_started",
                                "turn_id": "01990003-0002-7000-8000-000000000002"}),
            line("response_item", {"type": "message", "role": "user",
                                    "content": [{"type": "input_text", "text": "old parent"}],
                                    "internal_chat_message_metadata_passthrough":
                                        {"turn_id": "01990003-0002-7000-8000-000000000002"}}),
            line("event_msg", {"type": "task_started", "turn_id": native_turn}),
            line("response_item", {"type": "message", "role": "user",
                                    "content": [{"type": "input_text",
                                                 "text": "inspect job ownership"}]}),
            line("response_item", {"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": "native answer"}]}),
        ]
        native.write_text("\n".join(native_rows) + "\n", encoding="utf-8")
        n = LiveWatcher()
        nhead = n._codex_head(str(native))
        n._codex_meta[str(native)] = nhead[:2]
        n._codex_sub[str(native)] = nhead[2]
        n._codex_seed_subagent(str(native), native.stat().st_size)
        native_anchors = [e for e in n.ring if e.get("type") == "user"]
        native_ok = (len(native_anchors) == 1
                     and "inspect job ownership" in native_anchors[0]["text"]
                     and not any("old parent" in e.get("text", "") for e in n.ring))

        delegated = root / "rollout-delegated.jsonl"
        delegated_child = "01990002-0002-7000-8000-000000000002"
        delegated_turn = "01990002-0003-7000-8000-000000000003"
        delegated_rows = [
            line("session_meta", {"id": delegated_child, "cwd": "/work/legacy",
                                   "thread_source": "subagent", "parent_thread_id": parent,
                                   "source": "vscode"}),
            line("event_msg", {"type": "task_started", "turn_id": delegated_turn}),
            line("response_item", {"type": "message", "role": "user",
                                    "content": [{"type": "input_text", "text":
                                        "<codex_delegation><input>review lib -&gt; shell &amp; report"
                                        "</input></codex_delegation>"}]}),
        ]
        delegated.write_text("\n".join(delegated_rows) + "\n", encoding="utf-8")
        d = LiveWatcher()
        dhead = d._codex_head(str(delegated))
        d._codex_meta[str(delegated)] = dhead[:2]
        d._codex_sub[str(delegated)] = dhead[2]
        d._codex_seed_subagent(str(delegated), delegated.stat().st_size)
        delegated_anchors = [e for e in d.ring if e.get("type") == "user"]
        delegated_ok = (len(delegated_anchors) == 1
                        and "review lib -> shell & report" in delegated_anchors[0]["text"])

        partial = root / "rollout-partial.jsonl"
        partial.write_text(rows[0], encoding="utf-8")
        partial_ok = LiveWatcher._codex_head(str(partial)) is None

        guardian = root / "rollout-guardian.jsonl"
        guardian.write_text(line("session_meta", {
            "id": "01990005-0001-7000-8000-000000000001",
            "cwd": "/work/agrep", "thread_source": "subagent",
            "parent_thread_id": parent,
            "source": {"subagent": {"other": "guardian"}},
        }) + "\n", encoding="utf-8")
        guardian_head = LiveWatcher._codex_head(str(guardian))
        guardian_ok = bool(guardian_head and guardian_head[2]
                           and guardian_head[2].get("internal_guardian"))

    ok = routed_ok and native_ok and delegated_ok and partial_ok and guardian_ok
    return ("PASS" if ok else "FAIL",
            f"routed={routed_ok} native={native_ok} delegated={delegated_ok} "
            f"partial-head={partial_ok} guardian={guardian_ok}")


def t_live_cold_parity():
    """Live watcher and cold ingest must tell the same story for the same bytes:
    identical (session, text, who) user rows and identical parent links, driven from
    the rust golden fixtures so the two pipelines can never drift silently."""
    import json
    import tempfile
    from pathlib import Path

    import common
    from hookless.live import LiveWatcher

    fixroot = Path(HERE).parent / "crates" / "agrep-cli" / "tests" / "fixtures"
    if not fixroot.exists():
        return ("SKIP", "no fixture tree (installed package)")
    binp = common.ingest_bin()
    if not (binp and Path(str(binp)).exists()):
        return ("SKIP", "no ingest binary")

    details = []
    for agent in ("claude", "codex"):
        home = fixroot / agent / "home"
        # cold: the real ingest binary over the fixture home, scrubbed env
        with tempfile.TemporaryDirectory() as td:
            env = {k: v for k, v in os.environ.items()
                   if k not in ("USERPROFILE", "HOME", "APPDATA", "XDG_CONFIG_HOME")}
            env["AGREP_HOME"] = str(home)
            env["AGREP_DATA_DIR"] = td
            r = subprocess.run([str(binp), "index", "--agent", agent, "--full"],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", env=env, timeout=120)
            if r.returncode != 0:
                return ("FAIL", f"{agent}: cold ingest rc={r.returncode}")
            cold_rows = set()
            for ln in (Path(td) / "messages.jsonl").read_text(encoding="utf-8").splitlines():
                if ln.strip():
                    o = json.loads(ln)
                    cold_rows.add((o["session"], o["text"], o.get("who", "user")))
            cold_parent = {}
            for ln in (Path(td) / "sessions.jsonl").read_text(encoding="utf-8").splitlines():
                if ln.strip():
                    o = json.loads(ln)
                    if o.get("parent"):
                        cold_parent[o["session"]] = o["parent"]

        # live: the same bytes through the watcher's line handlers, as if the
        # sessions were written while it watched
        w = LiveWatcher()
        if agent == "claude":
            for f in sorted(home.glob(".claude/projects/**/*.jsonl")):
                nested = "subagents" in f.parts
                for ln in f.read_text(encoding="utf-8").splitlines():
                    if ln.strip():
                        w._claude_line(f.name[:-6], ln, nested=nested)
        else:
            for f in sorted(home.glob(".codex/sessions/**/rollout-*.jsonl")):
                path = str(f)
                head = w._codex_head(path)
                if head and head[2] is not None and head[2].get("internal_guardian"):
                    continue
                if head:
                    w._codex_meta[path] = head[:2]
                    w._codex_sub[path] = head[2]
                start = 0
                if head and head[2] is not None:
                    start = w._codex_seed_subagent(path, f.stat().st_size) or 0
                with open(path, "rb") as fh:
                    fh.seek(start)
                    rest = fh.read().decode("utf-8", "replace")
                for ln in rest.splitlines():
                    if ln.strip():
                        w._codex_line(path, ln.rstrip("\r"))
        live_rows = set()
        live_parent = {}
        for ev in w.ring:
            if ev.get("type") == "user":
                live_rows.add((ev["session"], ev["text"], ev.get("who", "user")))
                if ev.get("parent"):
                    live_parent[ev["session"]] = ev["parent"]

        if cold_rows != live_rows:
            diverged = sorted(cold_rows.symmetric_difference(live_rows))
            return ("FAIL", f"{agent}: rows diverge, cold-only="
                            f"{len(cold_rows - live_rows)} live-only="
                            f"{len(live_rows - cold_rows)}, e.g. {diverged[:2]}")
        if cold_parent != live_parent:
            return ("FAIL",
                    f"{agent}: parent links diverge cold={cold_parent} live={live_parent}")
        details.append(f"{agent}={len(cold_rows)} rows/{len(cold_parent)} links")
    return ("PASS", " · ".join(details))


def t_live_state_integrity():
    """Concurrent snapshots are safe and Claude classification needs path context."""
    import json
    import threading

    from hookless.live import LiveWatcher, _noise, _project_of, _ts_ms

    parent = "parent-123"
    child = "child-456"
    side = json.dumps({
        "type": "assistant", "sessionId": parent, "isSidechain": True,
        "timestamp": "2026-01-01T00:00:00Z", "cwd": "/work/agrep",
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "child-call", "name": "Read",
             "input": {"file_path": "/work/agrep/x"}},
        ]},
    })
    w = LiveWatcher()
    w._claude_line(parent, side, nested=False)
    inline_clean = not w.ring and not w.sessions
    w._claude_line(child, side, nested=True)
    child_ok = (len(w.ring) == 1 and w.ring[0]["session"] == child
                and w.ring[0].get("parent") == parent
                and w.sessions[f"claude:{child}"].get("sub") is True
                and f"claude:{parent}" not in w.sessions)

    internal = json.dumps({
        "type": "user", "sessionId": "ordinary", "userType": "internal",
        "timestamp": "2026-01-01T00:00:01Z",
        "message": {"role": "user", "content": "SYSTEM GENERATED INTERNAL TEXT"},
    })
    before = len(w.ring)
    w._claude_line("ordinary", internal)
    internal_filtered = len(w.ring) == before

    errors = []
    start = threading.Event()
    def writer():
        start.wait()
        try:
            for i in range(1500):
                w._emit({"agent": "codex", "session": f"race-{i}", "ts": i + 1,
                         "type": "user", "text": "x"})
        except Exception as exc:  # pragma: no cover - diagnostic capture
            errors.append(exc)
    thread = threading.Thread(target=writer)
    thread.start()
    start.set()
    try:
        while thread.is_alive():
            w.snapshot()
            # Concurrent readers must never observe a mutating dictionary iteration.
            sum(1 for s in w.sessions.values() if s.get("working"))
    except Exception as exc:
        errors.append(exc)
    thread.join()

    headless = LiveWatcher(headless_indexd=True).snapshot()
    diagnostics = (headless.get("watch_mode") == "indexd"
                   and "poll_s" in headless and "work_total_ms" in headless)
    parity_helpers = (not _noise("<repo> keeps failing to build")
                      and _noise("[SYSTEM NOTIFICATION: task completed]")
                      and _ts_ms(None) == 0 and _ts_ms("bad") == 0
                      and _project_of('C:/Users/tester/sample-project"') == "sample-project")
    ok = (inline_clean and child_ok and internal_filtered and not errors
          and diagnostics and parity_helpers)
    return ("PASS" if ok else "FAIL",
            f"inline={inline_clean} child={child_ok} internal={internal_filtered} "
            f"race_errors={errors[:1]} diagnostics={diagnostics} parity={parity_helpers}")


def t_cap():
    import corpusdb
    db = corpusdb.connect(quiet=True, read_only=True)
    if not db:
        return ("SKIP", "no corpus db")
    mx, over = db.execute(
        "SELECT max(length(text)), sum(case when length(text)>1601 then 1 else 0 end) "
        "FROM msgs WHERE who='agent'").fetchone()
    db.close()
    return ("PASS" if (mx and mx > 1601) else "FAIL",
            f"max reply={mx}, replies beyond old cap={over}")


# Process discovery must retain Claude session identifiers.
def t_procscan():
    from hookless import procscan
    procs = procscan.scan_agents()
    claude = [p for p in procs if p["agent"] == "claude" and p["session"]]
    return ("PASS" if claude else "SKIP",
            f"{len(procs)} agent procs, {len(claude)} claude w/ session id")


# Keyword, word, and regex engines must all return a common term.
def t_engines():
    import corpusdb
    db = corpusdb.connect(quiet=True, read_only=True)
    if not db:
        return ("SKIP", "no corpus db")
    kw = corpusdb.keyword(db, "the", 5)["total"]
    wd = corpusdb.word(db, "the", 5)["total"]
    rx = corpusdb.regex(db, "th.", 5)["total"]
    db.close()
    ok = kw > 0 and wd > 0 and rx > 0
    return ("PASS" if ok else "FAIL", f"keyword={kw} word={wd} regex={rx}")


# 8b. Filter pushdown parity: corpusdb SQL WHERE == JSONL scan + Python filtering.
def t_filter_parity():
    import sqlite3
    import corpusdb
    import explore
    import search
    observed_db_stamps = []
    observed_source_stamps = []
    lock_errors = []
    source_moved = False
    for attempt in range(3):
        lock_before = corpusdb._stamp()
        observed_source_stamps.append(lock_before)
        db = corpusdb.connect(quiet=True, read_only=True)
        if not db:
            return ("SKIP", "no corpus db")
        try:
            # Use filters and rows from one immutable SQLite generation.
            row = db.execute(
                "SELECT agent, count(*) c FROM msgs WHERE agent != '' "
                "GROUP BY agent ORDER BY c DESC LIMIT 1").fetchone()
            if not row:
                return ("SKIP", "empty corpus")
            ag = row[0]
            since = db.execute(
                "SELECT ts FROM msgs ORDER BY ts "
                "LIMIT 1 OFFSET (SELECT count(*)/2 FROM msgs)").fetchone()[0]
            flt = {"agent": ag, "who": "user", "since_ms": since}
            db_stamp = corpusdb.generation(db)[1]
            sql = corpusdb.keyword(db, "the", 10_000_000, flt)["hits"]
            prose_sql = corpusdb.keyword(
                db, "the", 10_000_000,
                {"agent": ag, "include_tools": False})["hits"]
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
            lock_after = corpusdb._stamp()
            observed_source_stamps.append(lock_after)
            lock_errors.append(str(exc))
            source_moved = (source_moved
                            or not corpusdb._stamps_equal(
                                lock_before, lock_after))
            if attempt < 2:
                time.sleep(0.05)
            continue
        finally:
            db.close()
        source_before = corpusdb._stamp()
        observed_db_stamps.append(db_stamp)
        observed_source_stamps.append(source_before)
        if not corpusdb._stamps_equal(db_stamp, source_before):
            if attempt < 2:
                time.sleep(0.05)
            continue
        try:
            jsonl = explore.keyword_search("the", 10_000_000)["hits"]
        except RuntimeError as exc:
            source_after = corpusdb._stamp()
            observed_source_stamps.append(source_after)
            reasons = (
                "generation changed",
                "event proof changed during bulk read",
            )
            if any(reason in str(exc) for reason in reasons):
                source_moved = True
            elif (str(exc) == "event store cannot be opened"
                  and not corpusdb._stamps_equal(source_before, source_after)):
                source_moved = True
            else:
                raise
        else:
            source_after = corpusdb._stamp()
            observed_source_stamps.append(source_after)
            if corpusdb._stamps_equal(db_stamp, source_after):
                py = search._filtered(
                    jsonl, ag, None, "user", None, False, None, since, None)
                prose_py = search._filtered(
                    jsonl, ag, None, None, None, False, include_tools=False)
                key = lambda h: (  # noqa: E731
                    h["session"], h["turn"], h["who"], h["snippet"])
                a, b = {key(h) for h in sql}, {key(h) for h in py}
                c, d = {key(h) for h in prose_sql}, {key(h) for h in prose_py}
                ok = (a == b and c == d
                      and all(h["who"] != "tool" for h in prose_sql))
                return ("PASS" if ok else "FAIL",
                        f"filtered diff={len(a ^ b)} prose diff={len(c ^ d)} "
                        f"rows={len(c)}")
            source_moved = source_moved or not corpusdb._stamps_equal(
                source_before, source_after)
        if attempt < 2:
            time.sleep(0.05)
    if (source_moved or len(set(observed_db_stamps)) > 1
            or len(set(observed_source_stamps)) > 1):
        return ("SKIP", "live writer active; parity unmeasurable")
    if lock_errors:
        return ("FAIL", f"stable source database remained locked: "
                f"{lock_errors[-1]}")
    # A live box's index rides a treadmill - one publish behind the newest
    # transcript write - and 150ms cannot tell that from a wedge. Judge only
    # after one bounded convergence window; catching up proves the treadmill.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        time.sleep(0.5)
        db = corpusdb.connect(quiet=True, read_only=True)
        if not db:
            continue
        try:
            caught_up = corpusdb._stamps_equal(
                corpusdb.generation(db)[1], corpusdb._stamp())
        except sqlite3.Error:
            continue
        finally:
            db.close()
        if caught_up:
            return ("SKIP", "index was catching up behind a live source and "
                    "converged; rerun for parity")
    return ("FAIL", "stable source and search-index generations differ "
            "and did not converge within 5s")


# 8b2. JSONL fallback filter pushdown and bounded row construction.
def t_jsonl_streaming_filters():
    """Filtered JSONL fallback never materializes rows it will discard."""
    import sqlite3
    import compact
    import corpusdb
    import explore
    import search

    messages = {
        "s1": [
            {"session": "s1", "id": "i1", "turn": 1, "ts": 100,
             "agent": "codex", "project": "ProjOne", "model": "gpt-5",
             "model_source": "explicit", "who": "user", "text": "alpha then beta"},
            {"session": "s1", "id": "i2", "turn": 2, "ts": 200,
             "agent": "codex", "project": "ProjOne", "model": "gpt-5",
             "who": "recap", "text": "continuation summary alpha beta"},
        ],
        "s2": [
            {"session": "s2", "id": "i3", "turn": 1, "ts": 300,
             "agent": "claude", "project": "Elsewhere", "model": "opus",
             "who": "control", "text": "beta before alpha"},
        ],
        "s3": [
            {"session": "s3", "id": "i4", "turn": 1, "ts": 400,
             "agent": "codex", "project": "ProjOne", "model": "gpt-5",
             "who": "user", "text": "beta only"},
        ],
    }
    replies = {"i1": "agent says alpha and beta", "i3": "unrelated reply"}
    reply_records = {
        key: {"reply": value, "content_digest": compact.content_digest(value),
              "reply_chars": len(value), "reply_truncated": False}
        for key, value in replies.items()
    }
    tool_calls = []

    def event_blobs_bulk(keys, *, full=False, required_literals=()):
        for agent, session in keys:
            tool_calls.append((
                "blob", agent, session, full, tuple(required_literals)))
            yield agent, session, b"fixture"

    def tool_rows_from_payload(
            payload, turns, _required_literals=(), *, strict=False):
        tool_calls.append(("rows", payload, tuple(turns), strict))
        return [{"turn": turns[0][1], "ts": turns[0][0] + 1,
                 "text": "tool saw beta and alpha"}]

    saved = (explore._messages_by_session, explore._reply_records_by_id,
             explore._session_concept, explore.common.event_blobs_bulk,
             explore.common.tool_rows_from_payload, explore._freshen)
    try:
        explore._messages_by_session = lambda: messages
        explore._reply_records_by_id = lambda: reply_records
        explore._session_concept = lambda: {"s1": "one", "s2": "two"}
        explore.common.event_blobs_bulk = event_blobs_bulk
        explore.common.tool_rows_from_payload = tool_rows_from_payload
        explore._freshen = lambda: None

        streamed = list(explore._iter_kw_corpus())
        canonical = all("model_source" in r for r in streamed)

        # Exact content-tier parity includes two easily-missed details: sub-trigram
        # terms count as globally common, and only the strongest three >=3-char
        # anchors contribute candidates. The beta-only row exercises the latter.
        db = sqlite3.connect(":memory:")
        db.executescript(corpusdb._SCHEMA_SQL)
        db.executemany(corpusdb._INS, [
            (r["session"], r["turn"], r["ts"], r["agent"], r["project"],
             r.get("concept", ""), r.get("model", ""), r.get("model_source", ""),
             r["who"], r["text"]) for r in streamed
        ])
        db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
        db.execute("INSERT INTO msgs_prose_fts(rowid, text) "
                   "SELECT id, text FROM msgs WHERE who <> 'tool'")
        content_terms = ["alpha", "beta", "reply", "zzzz", "to"]
        sql_content = corpusdb.content(
            db, " ".join(content_terms), 10_000_000,
            {"include_tools": False})["hits"]
        legacy_content = search._content_scan(
            content_terms, 10_000_000, {"include_tools": False})["hits"]
        db.close()
        content_key = lambda h: (h["session"], h["turn"], h["who"],  # noqa: E731
                                 h["snippet"], h.get("coverage"))
        content_parity = ([content_key(h) for h in sql_content]
                          == [content_key(h) for h in legacy_content]
                          and all(h["session"] != "s3" for h in legacy_content))

        tool_calls.clear()
        prose = list(explore._iter_kw_corpus({"include_tools": False}))
        prose_skips_tools = not tool_calls and all(r["who"] != "tool" for r in prose)

        filtered = list(explore._iter_kw_corpus({
            "agent": "CODE", "project": "proj", "chat": "S1", "who": "user",
            "model": "GPT-5", "since_ms": 90, "until_ms": 150,
        }))
        filter_exact = len(filtered) == 1 and filtered[0]["text"] == "alpha then beta"

        # Tool-only lookup neither loads replies nor opens event files when the
        # empty tool model cannot satisfy an explicit model filter.
        explore._reply_records_by_id = lambda: (_ for _ in ()).throw(
            AssertionError("tool-only search loaded replies"))
        tool_calls.clear()
        tools = list(explore._iter_kw_corpus({"who": "tool", "chat": "s1"}))
        tool_only = (len(tools) == 1 and tools[0]["who"] == "tool"
                     and [call[0] for call in tool_calls] == ["blob", "rows"])
        tool_calls.clear()
        no_model_tools = list(explore._iter_kw_corpus(
            {"who": "tool", "model": "gpt-5"}))
        tool_prefilter = not no_model_tools and not tool_calls

        # Bag-of-words totals stay exhaustive; streaming is not a hidden shortlist.
        explore._reply_records_by_id = lambda: reply_records
        tool_calls.clear()
        terms = search._terms_scan(
            "beta alpha", 10_000_000, {"include_tools": False})
        phrase = explore.keyword_search(
            "alpha beta", 10_000_000, {"include_tools": False})
        engines = (terms["total"] == 4 and phrase["total"] == 1
                   and not tool_calls
                   and all(h["who"] != "tool" for h in terms["hits"]))
    finally:
        (explore._messages_by_session, explore._reply_records_by_id,
         explore._session_concept, explore.common.event_blobs_bulk,
         explore.common.tool_rows_from_payload, explore._freshen) = saved

    ok = (canonical and prose_skips_tools and filter_exact and tool_only
          and tool_prefilter and engines and content_parity)
    return ("PASS" if ok else "FAIL",
            f"canonical={canonical} prose_skip={prose_skips_tools} "
            f"filters={filter_exact} tool_only={tool_only} "
            f"tool_prefilter={tool_prefilter} engines={engines} "
            f"content_parity={content_parity}")


# 8c. ranking: repeated exact terms and recency determine relevance order.
def t_ranking():
    import search
    now = int(time.time() * 1000)
    mk = lambda snip, ts, turn: {"session": "s", "turn": turn, "ts": ts, "who": "user",  # noqa: E731
                                 "agent": "claude", "project": "p", "snippet": snip}
    hits = [mk("a leak showed up once here", now, 1),
            mk("leak after leak after leak", now, 2),
            mk("a leak showed up once here", now - 30 * 86_400_000, 3)]
    order = [h["turn"] for h in search._rank(hits, "leak", "keyword", "score")]
    # The user-age floor keeps human evidence above fresh generated rows at ANY age.
    hits2 = [mk("a leak showed up once here", now, 1),
             {**mk("a leak showed up once here", now, 4), "who": "tool"},
             mk("a leak showed up once here", now - 365 * 86_400_000, 3),
             {**mk("a leak showed up once here", now, 5), "who": "recap"}]
    order2 = [h["turn"] for h in search._rank(hits2, "leak", "keyword", "score")]
    ok = (order[0] == 2 and order.index(1) < order.index(3)
          and order2 == [1, 3, 5, 4])
    return ("PASS" if ok else "FAIL", f"order={order} (want 3x-repeat first, new before old); "
                                      f"generated-demoted={order2} (want user, year-old user, recap, tool)")


def t_snippet_cut_boundary():
    """A window-edge cut is presentation, not evidence: '…form' (cut from
    'transform') must not grade aligned, on either _boundary_score path."""
    import search

    context = search._prepare_boundary("form", "keyword")
    cut = search._boundary_score(
        {"snippet": "…form was fast", "who": "user"}, context)
    truth = context[0].evaluate("transform was fast")
    aligned = search._boundary_score(
        {"snippet": "the form was fast", "who": "user"}, context)
    tail_cut = search._boundary_score(
        {"snippet": "x transform…", "who": "user"}, context)
    locked = search._boundary_score(
        {"snippet": "…form beats transform", "who": "user"}, context)
    all_terms = search._boundary_score(
        {"snippet": "…form was fast", "matched": "all-terms"},
        search._prepare_boundary("form fast", "keyword"))
    checks = {
        "cut-not-aligned": cut.match_class != "aligned" and cut.factor <= truth.factor,
        "truth-shape": cut.match_class == truth.match_class == "partial",
        "real-aligned-kept": aligned.match_class == "aligned" and aligned.factor == 1.0,
        "tail-cut-interior": tail_cut.match_class == "interior",
        "max-occurrence-honest": locked.match_class != "aligned",
        "all-terms-path": all_terms.match_class != "aligned",
    }
    return ("PASS" if all(checks.values()) else "FAIL", f"checks={checks}")


def t_snip_stitch_merge():
    """The stitcher's window-merge branch is contract: overlapping/touching windows
    must fuse without duplicated text, and every engine path must share one renderer."""
    import common
    import corpusdb
    import explore
    import search

    text = "alpha beta gamma delta epsilon"
    far = "alpha " + "x" * 240 + " omega"
    cases = [
        ("overlap-merges-once", (text, [(6, 10), (11, 16)], 4), "…pha beta gamma del…"),
        ("touching-boundary-merges", (text, [(0, 2), (10, 12)], 4), "alpha beta gamma…"),
        ("separated-keeps-order", (text, [(0, 2), (14, 16)], 4), "alpha … … gamma del…"),
        ("triple-chain-keeps-middle", (text, [(0, 3), (4, 8), (9, 14)], 4),
         "alpha beta gamma d…"),
        ("end-edge-no-trailing-ellipsis", (text, [(23, 30)], 4), "…lta epsilon"),
        ("full-cover-no-ellipses", (text, [(0, 30)], 4), text),
        ("empty-spans", (text, [], 4), ""),
        ("zero-width-span", (text, [(8, 8)], 2), "…beta…"),
        ("unsorted-input-sorted", (text, [(14, 16), (0, 2)], 4), "alpha … … gamma del…"),
        ("stitch-collapses-whitespace", ("alpha\n\tbeta  gamma", [(0, 5), (8, 12)], 4),
         "alpha beta gam…"),
        ("default-pad-far-apart", (far, [(0, 5), (247, 252)], 80),
         "alpha " + "x" * 79 + "… …" + "x" * 79 + " omega"),
    ]
    bad = [(name, common.snip_spans(*args), want)
           for name, args, want in cases if common.snip_spans(*args) != want]
    one_impl = (corpusdb._snip_spans is explore._snip_spans is common.snip_spans
                and corpusdb._snip_at is explore._snip_at is common.snip_at
                and search._snip_at is common.snip_at)
    collapse = (common.one_line("  a\n\tb\r\nc  ") == "a b c"
                and common.one_line(42) == "42")
    ok = not bad and one_impl and collapse
    return ("PASS" if ok else "FAIL",
            f"mismatches={bad or 'none'} one-impl={one_impl} one_line={collapse}")


def t_terms_spread_ranking():
    """All-terms rows: clustered terms outrank a row-wide scatter at equal
    recency/speaker; a stitch joint counts as the gap it hides."""
    import corpusdb
    import search

    now = time.time() * 1000
    tight_text = "note alpha every beta shipped " + "pad " * 60
    scattered_text = "alpha " + "pad " * 120 + "beta"
    tight = corpusdb._snip_spans(tight_text, [(5, 10), (17, 21)])
    at = scattered_text.index("beta")
    scattered = corpusdb._snip_spans(scattered_text, [(0, 5), (at, at + 4)])
    mk = lambda snip, session: {"session": session, "turn": 1, "ts": now, "who": "user",  # noqa: E731
                                "snippet": snip, "matched": "all-terms"}
    # scattered's session sorts first, so a proximity-blind tie keeps the old bug visible
    ranked = search._rank([mk(scattered, "a-scattered"), mk(tight, "b-tight")],
                          "alpha beta", "keyword", "score")
    order = [h["session"] for h in ranked]
    scores = {h["session"]: h["score"] for h in ranked}
    ok = (order == ["b-tight", "a-scattered"]
          and scores["b-tight"] > scores["a-scattered"] and "…" in scattered)
    return ("PASS" if ok else "FAIL", f"order={order} scores={scores}")


def t_explicit_mode_age_floor():
    """-w/-E state explicit intent: a year-old exact user hit outranks fresh
    generated rows, and both modes keep bypassing boundary grading."""
    import search

    now = time.time() * 1000
    mk = lambda who, ts, turn: {"session": "s", "turn": turn, "ts": ts, "who": who,  # noqa: E731
                                "snippet": "oom killed the job"}
    orders = {}
    bypassed = True
    for mode in ("word", "regex"):
        hits = [mk("tool", now, 1), mk("user", now - 365 * 86_400_000, 2),
                mk("recap", now, 3)]
        orders[mode] = [h["turn"] for h in search._rank(hits, "oom", mode, "score")]
        bypassed &= all("_boundary_class" not in h for h in hits)
    ok = all(order[0] == 2 for order in orders.values()) and bypassed
    return ("PASS" if ok else "FAIL",
            f"orders={orders} (want year-old exact user first) boundary-bypassed={bypassed}")


def t_boundary_compact_contract():
    import re
    import tempfile
    from pathlib import Path
    import boundary_rank
    import compact
    import corpusdb
    import search

    stats = {"akd": (372, 32)}
    aligned = boundary_rank.prepare_query("akd", stats).evaluate("akd controller")
    interior = boundary_rank.prepare_query("akd", stats).evaluate("peakDetect")
    now_ms = time.time() * 1000
    context = (boundary_rank.prepare_query("akd", stats), re.compile(r"(akd)", re.I))
    aligned_hit = {"snippet": "akd controller", "ts": now_ms, "who": "user"}
    interior_hit = {"snippet": "peakDetect", "ts": now_ms, "who": "user"}
    aligned_score = search._score(
        aligned_hit, search._match_pat("akd", "keyword"), 3, now_ms,
        terms=["akd"], boundary=context)
    interior_score = search._score(
        interior_hit, search._match_pat("akd", "keyword"), 3, now_ms,
        terms=["akd"], boundary=context)
    unicode_hit = {"snippet": "xxİfoo", "ts": now_ms, "who": "user"}
    unicode_context = (boundary_rank.prepare_query("xxi"),
                       re.compile(r"(xxi)", re.I))
    search._score(unicode_hit, search._match_pat("xxi", "keyword"), 3, now_ms,
                  terms=["xxi"], boundary=unicode_context)
    stat_calls = []
    search._load_corpusdb()
    saved_stats = corpusdb.boundary_token_stats
    try:
        corpusdb.boundary_token_stats = lambda _db, tokens: (
            stat_calls.append(tuple(tokens)) or {"akd": (372, 32)})
        prepared_once = search._prepare_boundary("akd akd", "keyword", object())
        for _ in range(8):
            search._score(dict(aligned_hit), search._match_pat("akd", "keyword"),
                          3, now_ms, terms=["akd"], boundary=prepared_once)
        search._prepare_boundary("akd", "word", object())
        search._prepare_boundary("akd", "regex", object())
    finally:
        corpusdb.boundary_token_stats = saved_stats
    # Distinct counts keep every bucket observable; malformed phrase rows fail closed.
    tiers = search._count_tiers([
        {"_boundary_class": "aligned"}, {"_boundary_class": "aligned"},
        {"_boundary_class": "partial"}, {"_boundary_class": "partial"},
        {"_boundary_class": "partial"}, {"_boundary_class": "partial"},
        {"_boundary_class": "interior"}, {"_boundary_class": "interior"},
        {"_boundary_class": "interior"},
        {"matched": "all-terms"}, {"matched": "all-terms"},
        {"matched": "all-terms"}, {"matched": "content-terms"},
        {"matched": "content-terms"},
    ])
    try:
        search._count_tiers([{}])
    except RuntimeError:
        malformed_rejected = True
    else:
        malformed_rejected = False
    hits = [{"session": f"session{i:04}", "turn": i, "score": 1.0,
             "family": "same" if i < 2 else f"family{i}"} for i in range(8)]
    with tempfile.TemporaryDirectory() as td:
        page = compact.start_compact(
            hits, lambda hit: f"@{hit['session']}:0 result", "generation", "rank-v1",
            data_dir=Path(td), byte_budget=100, query="result")
        next_page = compact.continue_compact(
            page.handle, None, "rank-v1", data_dir=Path(td), byte_budget=10_000)
    checks = {
        "observed": aligned.factor == 1.0 and interior.factor == 0.12,
        "multiplicative": aligned_score > interior_score
                          and aligned_hit.get("_boundary_class") == "aligned"
                          and interior_hit.get("_boundary_class") == "interior",
        "unicode": unicode_hit.get("_boundary_factor", 1.0) < 1.0,
        "stats-once": stat_calls == [("akd", "akd")],
        "tiers": tiers == {"phrase_aligned": 2, "phrase_partial": 4,
                           "phrase_interior": 3, "all_terms": 5},
        "malformed-tier": malformed_rejected,
        "page": len(page.lines) == 4 and page.more and bool(next_page.lines),
    }
    return ("PASS" if all(checks.values()) else "FAIL", f"checks={checks}")


def t_more_survives_reingest():
    """--more serves an unchanged corpus, and after an UNRELATED reingest still serves
    the frozen page (rc 0) with a non-fatal staleness note on stderr - a routine
    background ingest must never kill an outstanding handle."""
    import contextlib
    import io
    import tempfile
    from pathlib import Path
    import compact
    import search

    common_mod = search.common
    saved = (common_mod.DATA_DIR, common_mod.transcript_generation)
    records = compact.freeze_records(
        [{"session": "s1", "turn": 1}, {"session": "s2", "turn": 2}],
        lambda hit: f"row {hit['turn']}")
    try:
        with tempfile.TemporaryDirectory() as td:
            common_mod.DATA_DIR = Path(td)
            common_mod.transcript_generation = lambda **kw: {"sig": "a"}
            fresh = compact.save_snapshot(
                records, {"sig": "a"}, search._RANKING_VERSION, query="result")
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                same_rc = search.main(["--more", fresh])
            stale = compact.save_snapshot(
                records, {"sig": "a"}, search._RANKING_VERSION, query="result")
            common_mod.transcript_generation = lambda **kw: {"sig": "b"}
            out2, err2 = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out2), contextlib.redirect_stderr(err2):
                stale_rc = search.main(["--more", stale])
    finally:
        common_mod.DATA_DIR, common_mod.transcript_generation = saved
    ok = (same_rc == 0 and "row 1" in out.getvalue() and "row 2" in out.getvalue()
          and stale_rc == 0
          and "row 1" in out2.getvalue() and "row 2" in out2.getvalue()
          and "newer results" in err2.getvalue())
    return ("PASS" if ok else "FAIL",
            f"same_rc={same_rc} stale_rc={stale_rc} "
            f"stale_err={err2.getvalue().strip()!r}")


def t_public_row_serialization():
    """--json sheds underscore-prefixed ranking internals without mutating the
    rows the renderers still read."""
    import contextlib
    import io
    import json as _json
    import search

    rows = [{"session": "s1", "turn": 1, "who": "tool", "kind": "tool",
             "snippet": "alpha", "score": 2.0, "_boundary_class": "aligned",
             "_boundary_factor": 1.0, "_agrep_row_key": 7}]
    stripped = search.public_rows(rows)
    helper_ok = (stripped[0] == {"session": "s1", "turn": 1, "who": "tool",
                                 "kind": "tool", "snippet": "alpha",
                                 "score": 2.0}
                 and "_boundary_class" in rows[0])

    saved = (search.run_query, search.indexd_runtime.ensure_index)

    def fake_run(q, **kw):
        return {"hits": [dict(row) for row in rows], "total": 1, "chats": 1,
                "tool_hits": 0, "engine": "corpusdb", "mode": "keyword"}

    try:
        search.run_query = fake_run
        search.indexd_runtime.ensure_index = lambda auto=True, **_kw: True
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            cli_rc = search.main(["alpha", "--json", "--color", "never"])
        emitted = [_json.loads(line) for line in out.getvalue().splitlines()]
    finally:
        search.run_query, search.indexd_runtime.ensure_index = saved
    meta = emitted[0] if emitted else {}
    hits = emitted[1:] if emitted else []
    required_fields = {"completeness", "freshness", "filter_coverage",
                       "self_exclusion", "semantic_coverage", "engine", "query"}
    page_fields = required_fields | {"semantic", "semantic_integrity",
                                     "tools_excluded"}
    cli_ok = (cli_rc == 0 and len(hits) == 1
              and meta.get("kind") == "agrep-meta"
              and sum(row.get("kind") == "agrep-meta" for row in emitted) == 1
              and all(not key.startswith("_") for row in emitted for key in row)
              and required_fields <= meta.keys()
              and not page_fields.intersection(hits[0])
              and hits[0]["session"] == "s1" and hits[0]["kind"] == "tool")
    ok = helper_ok and cli_ok
    return ("PASS" if ok else "FAIL",
            f"helper={helper_ok} cli={cli_ok}")


def t_count_matches_display():
    """Plumbing counts raw keyword lanes; porcelain alone may recover content terms."""
    import contextlib
    import io
    import search

    saved = (search.run_query, search.indexd_runtime.ensure_index, search._stream_first_run)
    calls = []
    content_hits = [
        {"session": "s1", "turn": 1, "who": "user", "snippet": "va",
         "matched": "content-terms"},
        {"session": "s2", "turn": 2, "who": "user", "snippet": "vb",
         "matched": "content-terms"},
    ]
    tier_hits = [{"session": "s1", "turn": 1, "who": "user", "snippet": "x",
                  "_boundary_class": "aligned"},
                 {"session": "s2", "turn": 2, "who": "user", "snippet": "y",
                  "matched": "all-terms"},
                 {"session": "s3", "turn": 3, "who": "user", "snippet": "z",
                  "matched": "all-terms"}]

    def fake_run(q, **kw):
        calls.append(kw)
        hits = content_hits if kw.get("allow_fallback", True) else tier_hits
        return {"hits": [dict(h) for h in hits], "total": len(hits),
                "chats": len({h["session"] for h in hits}), "tool_hits": 0,
                "engine": "corpusdb", "mode": "keyword",
                "content_fallback": kw.get("allow_fallback", True)}

    try:
        search.run_query = fake_run
        search.indexd_runtime.ensure_index = lambda auto=True, **_kw: True
        search._stream_first_run = lambda *args, **kw: None
        class TtyBuffer(io.StringIO):
            def isatty(self):
                return True

        disp = TtyBuffer()
        with contextlib.redirect_stdout(disp), contextlib.redirect_stderr(io.StringIO()):
            disp_rc = search.main(["speaker balance drift", "--classic",
                                   "--color", "never"])
        shown = [line for line in disp.getvalue().splitlines() if line.strip()]
        cnt = io.StringIO()
        with contextlib.redirect_stdout(cnt), contextlib.redirect_stderr(io.StringIO()):
            count_rc = search.main(["speaker balance drift", "-c"])
        counted = cnt.getvalue().strip()
        # -c renders -n inert, and a supplied inert option is refused, never
        # dropped: the refusal must name both flags and run no query.
        zero_err = io.StringIO()
        calls_before_refusal = len(calls)
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(zero_err):
            try:
                zero_cap_rc = search.main(
                    ["speaker balance drift", "-c", "-n", "0"])
            except SystemExit as exc:
                zero_cap_rc = exc.code
        tier = io.StringIO()
        with contextlib.redirect_stdout(tier), contextlib.redirect_stderr(io.StringIO()):
            search.main(["speaker balance drift", "--count-by-tier"])
        fields = dict(part.split("=") for part in tier.getvalue().split())
    finally:
        search.run_query, search.indexd_runtime.ensure_index, search._stream_first_run = saved
    tier_names = ("phrase_aligned", "phrase_partial", "phrase_interior", "all_terms")
    ok = (disp_rc == count_rc == 0 and zero_cap_rc == 2
          and "-c" in zero_err.getvalue() and "-n" in zero_err.getvalue()
          and len(calls) == calls_before_refusal + 1
          and len(shown) == 2 and counted == "3"
          and calls[0].get("allow_fallback") is True
          and calls[1].get("allow_fallback") is False
          and calls[1].get("exhaustive") is True
          and calls[2].get("allow_fallback") is False
          and sum(int(fields[name]) for name in tier_names) == int(fields["total"]) == 3)
    return ("PASS" if ok else "FAIL",
            f"shown={len(shown)} counted={counted!r} zero_cap_rc={zero_cap_rc} "
            f"tier_fields={fields} "
            f"fallback_flags={[kw.get('allow_fallback') for kw in calls]}")


def t_probe_prose_session_count():
    """The probe's printed session count follows the phrase gate: scattered
    any-order sessions must not be reported as prose matches."""
    import contextlib
    import io
    import sqlite3
    import common
    import compact
    import corpusdb
    import recall
    import search

    now_ms = int(time.time() * 1000)
    db = sqlite3.connect(":memory:")
    db.executescript(corpusdb._SCHEMA_SQL)
    db.executemany(corpusdb._INS, [
        ("phrase-sess", 1, now_ms, "claude", "p", "", "", "", "user",
         "the akd urlbag deadlock struck again this morning"),
        ("scatter-sess", 2, now_ms, "claude", "p", "", "", "", "user",
         "akd was fine until the urlbag cache warmed and later a deadlock appeared"),
    ])
    db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
    db.execute("INSERT INTO msgs_prose_fts(rowid, text) "
               "SELECT id, text FROM msgs WHERE who <> 'tool'")
    db.commit()
    saved = (corpusdb.connect, search.run_query, recall.indexd_runtime.ensure_index)
    try:
        corpusdb.connect = lambda **kw: db
        res = search.run_query("akd urlbag deadlock", mode="keyword", limit=1,
                               session_limit=1, include_tools=False,
                               family_diverse=True)
        produced = (res["chats"] == 2 and res["phrase_chats"] == 1
                    and res["hits"][0]["session"] == "phrase-sess")

        def fake_run(q, **kw):
            return {"hits": [
                {"session": "phrase-sess", "turn": 1, "ts": 9, "who": "user",
                 "agent": "codex", "project": "p", "score": 5.0,
                 "content_digest": compact.content_digest("exact"),
                 "snippet": "exact"},
                {"session": "scatter-sess", "turn": 2, "ts": 8, "who": "user",
                 "agent": "codex", "project": "p", "score": 1.0,
                 "matched": "all-terms",
                 "content_digest": compact.content_digest("scattered"),
                 "snippet": "scattered"},
            ], "total": 3, "chats": 2, "phrase_chats": 1, "tool_hits": 0,
                "engine": "corpusdb", "mode": "keyword", "terms_augmented": True}

        search.run_query = fake_run
        recall.indexd_runtime.ensure_index = lambda auto=True, **_kw: True
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = recall.main(["akd urlbag deadlock", "--probe"])
        line = out.getvalue()
    finally:
        (corpusdb.connect, search.run_query, recall.indexd_runtime.ensure_index) = saved
    printed = rc == 0 and "1 past session matches" in line
    ok = produced and printed
    return ("PASS" if ok else "FAIL",
            f"run_query chats={res['chats']}/phrase={res.get('phrase_chats')} "
            f"probe_line={line.strip()!r}")


def t_compact_profile_gate():
    """The compact agent profile covers lexical and semantic message rows."""
    import contextlib
    import io
    import re as _re
    import common
    import compact
    import search

    # compact re-snips are 32-pad windows; the canonical snip helpers default to 80,
    # so a dropped explicit pad silently widens rows and shifts page byte budgets.
    prepared = search.boundary_rank.prepare_query("alpha beta")
    phrase_snip = search._compact_snippet(
        {"snippet": "x" * 200 + " alpha beta " + "y" * 200},
        _re.compile("alpha beta"), prepared)
    terms_snip = search._compact_snippet(
        {"snippet": "alpha " + "z" * 200 + " beta", "matched": "all-terms"},
        _re.compile("alpha beta"), prepared)
    snip_pad_ok = (len(phrase_snip) < 120 and len(terms_snip) < 130
                   and "alpha" in phrase_snip and "beta" in terms_snip)

    now_ms = int(time.time() * 1000)
    saved = (compact.profile_enabled, search.run_query, search.indexd_runtime.ensure_index,
             search.common.indexed_session_prefix_candidates,
             search._stream_first_run, os.environ.get("AGREP_PROFILE"))
    profile_on = [False]
    profile_calls = []
    rq_calls = []
    hits = [{"session": "deadbeef-aaaa", "turn": 1, "ts": now_ms, "who": "user",
             "agent": "codex", "project": "p", "score": 2.0,
             "content_digest": compact.content_digest("alpha beta"),
             "snippet": "alpha beta"},
            {"session": "feedface-bbbb", "turn": 4, "ts": now_ms, "who": "agent",
             "agent": "codex", "project": "p", "score": 1.0,
             "content_digest": compact.content_digest("beta gamma"),
             "snippet": "beta gamma"}]

    def fake_profile(*, classic=False, json_mode=False, environ=None):
        profile_calls.append({"classic": classic, "json_mode": json_mode})
        return profile_on[0]

    def fake_prefixes(_sessions):
        return compact.session_prefix_index((
            "cafebabe-cccc", "deadbeef-aaaa", "deadbeef-abcd",
            "feedface-bbbb",
        ))

    def fake_run(q, *, mode="keyword", **kw):
        rq_calls.append((mode, kw.get("limit")))
        if mode == "semantic":
            return {"hits": [{"session": "cafebabe-cccc", "turn": 7, "ts": now_ms,
                              "agent": "codex", "project": "p", "sem_score": 0.9,
                              "score_kind": "cosine",
                              "content_digest": compact.content_digest("meaning row"),
                              "snippet": "meaning row"}],
                    "total": 1, "chats": 1, "engine": "semantic:hybrid",
                    "mode": "semantic", "tool_hits": 0}
        return {"hits": [dict(h) for h in hits], "total": 2, "chats": 2,
                "tool_hits": 0, "engine": "corpusdb", "mode": "keyword"}

    def run(argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = run_main(argv)
        return rc, out.getvalue(), err.getvalue()

    run_main = search.main
    try:
        compact.profile_enabled = fake_profile
        search.run_query = fake_run
        search.indexd_runtime.ensure_index = lambda auto=True, **_kw: True
        search.common.indexed_session_prefix_candidates = fake_prefixes
        search._stream_first_run = lambda *args, **kw: None
        # env says compact, the (patched) gate says no: main must follow the gate
        os.environ["AGREP_PROFILE"] = "compact"
        _, classic_out, classic_err = run(["alpha beta", "--color", "never"])
        profile_on[0] = True
        _, compact_out, compact_err = run(["alpha beta", "--color", "never"])
        _, sem_out, sem_err = run(["alpha beta", "-s", "--color", "never"])
    finally:
        (compact.profile_enabled, search.run_query, search.indexd_runtime.ensure_index,
         search.common.indexed_session_prefix_candidates,
         search._stream_first_run, profile_env) = saved
        if profile_env is None:
            os.environ.pop("AGREP_PROFILE", None)
        else:
            os.environ["AGREP_PROFILE"] = profile_env
    gate_followed = (profile_calls[0] == {"classic": False, "json_mode": False}
                     and not classic_out.startswith("@")
                     and "more=" not in classic_err)
    compact_lines = compact_out.splitlines()
    compact_ok = (compact_lines and compact_lines[0].startswith("@deadbeef-aa:1")
                  and any(line.startswith("@feedface:4") for line in compact_lines)
                  and "~semantic" in compact_out and "more=" not in compact_err
                  and ("keyword", 40) in rq_calls
                  and ("semantic", search._AUTO_SEMANTIC_FETCH) in rq_calls)
    semantic_ok = (sem_out.startswith("@cafebabe:7")
                   and "~semantic" in sem_out
                   and "more=" not in sem_err
                   and ("semantic", 40) in rq_calls)
    ok = gate_followed and compact_ok and semantic_ok and snip_pad_ok
    return ("PASS" if ok else "FAIL",
            f"gate={gate_followed} compact={compact_ok} semantic={semantic_ok} "
            f"snip_pad_32={snip_pad_ok} ({len(phrase_snip)}/{len(terms_snip)}ch) "
            f"first_compact_line={compact_lines[0] if compact_lines else ''!r}")


def t_age_label_vocabulary():
    """One canonical age vocabulary (minutes granularity, year cap) shared by the
    compact renderer and doctor's drift report."""
    import common
    import compact
    import doctor
    import search

    now_ms = time.time() * 1000
    line = search._compact_line(
        {"session": "abcd1234", "turn": 2, "ts": now_ms - 5 * 60_000,
         "agent": "codex", "who": "user", "project": "p",
         "content_digest": compact.content_digest("s"), "snippet": "s"}, ())
    checks = {
        "minutes": common.age_label(now_ms - 5 * 60_000) == "5m",
        "hours": common.age_label(now_ms - 3 * 3_600_000) == "3h",
        "days": common.age_label(now_ms - 2 * 86_400_000) == "2d",
        "year_cap": common.age_label(now_ms - 500 * 86_400_000) == "1y",
        "unknown": common.age_label(None) == "-" and common.age_label(0) == "-",
        "compact_line": " 5m " in line,
    }
    return ("PASS" if all(checks.values()) else "FAIL", f"checks={checks}")


def t_handle_resolution_unified():
    """`around @x:n` and `recall @x:n` resolve through one compact policy, and the
    bare-prefix ambiguity error teaches the shortest disambiguating prefixes."""
    import contextlib
    import io
    import around
    import common
    import explore
    import recall
    import search

    two = ["0199aaaa-bbbb-4000-8000-000000000001",
           "0199aaaa-dddd-4000-8000-000000000002"]
    saved = (explore.resolve_session, explore.get_window, explore.get_windows,
             explore._session_index, around.indexd_runtime.ensure_index,
             common.indexed_session_prefix_candidates)

    def run(fn, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fn(argv)
        return rc, out.getvalue(), err.getvalue()

    try:
        around.indexd_runtime.ensure_index = lambda auto=True, **_kw: True
        common.indexed_session_prefix_candidates = lambda sessions: tuple(two)
        explore.resolve_session = lambda q: [s for s in two if s.startswith(q)]
        explore._session_index = lambda: {session: {} for session in two}
        explore.get_window = lambda sess, center, radius: {
            "session": sess, "agent": "codex", "project": "p", "concept": "",
            "title": "", "center": center, "first_turn": 0, "last_turn": 9,
            "turns": [{"turn": center, "ts": 0, "who": "you", "text": "t",
                       "reply": "r"}], "events": []}
        explore.get_windows = lambda requests: [
            explore.get_window(sess, turn, radius) for sess, turn, radius in requests]
        prefix_rc, _, prefix_err = run(around.main, ["0199aaaa", "5"])
        ambig_a_rc, _, ambig_a_err = run(around.main, ["@0199aaaa:5"])
        ambig_r_rc, _, ambig_r_err = run(recall.main, ["@0199aaaa:5"])
        unique_a_rc, unique_a_out, _ = run(around.main, ["@0199aaaa-b:5"])
        unique_r_rc, unique_r_out, _ = run(recall.main,
                                           ["@0199aaaa-b:5", "--budget", "0"])
        explore.resolve_session = lambda q: []
        gone_a_rc, gone_a_out, gone_a_err = run(
            around.main, ["@0199aaaa:5"])
        gone_r_rc, gone_r_out, gone_r_err = run(
            recall.main, ["@0199aaaa:5"])
    finally:
        (explore.resolve_session, explore.get_window, explore.get_windows,
         explore._session_index, around.indexd_runtime.ensure_index,
         common.indexed_session_prefix_candidates) = saved
    hint_ok = (prefix_rc == 2
               and "add a char: 0199aaaa-b / 0199aaaa-d" in prefix_err)
    # one policy, two reaches: recall refuses a tie it cannot break, around
    # falls back to the candidate list and hint its positional form prints
    ambiguous_same = (ambig_a_rc == ambig_r_rc == 2
                      and "add a char: 0199aaaa-b / 0199aaaa-d" in ambig_a_err
                      and all(session in ambig_a_err for session in two)
                      and "result handle session is ambiguous" in ambig_r_err)
    unique_same = (unique_a_rc == unique_r_rc == 0
                   and "0199aaaa" in unique_a_out
                   # the block head folds the turn into the @session:turn handle
                   and "@0199aaaa-b:5" in unique_r_out)
    missing_same = (
        gone_a_rc == gone_r_rc == 2
        and gone_a_out == gone_r_out == ""
        and gone_a_err == (
            "result handle is stale or its chat was pruned; "
            "rerun the search for a current handle\n")
        and gone_r_err == (
            "no indexed session matches this handle - the handle is stale "
            "or its session was pruned; rerun the search - fresh results "
            "mint current handles\n"))
    ok = hint_ok and ambiguous_same and unique_same and missing_same
    return ("PASS" if ok else "FAIL",
            f"hint={hint_ok} ambiguous={ambiguous_same} unique={unique_same} "
            f"missing={missing_same}")


# Recall/window regressions are pure fixtures: they guard the retrieval contract
# without depending on the machine's corpus or heavyweight semantic models.
def t_session_retrieval_contract():
    import search

    ranked = []
    for session in range(75):
        for turn in range(3):
            ranked.append({"session": f"s{session:03}", "turn": turn,
                           "ts": 1000 - session, "who": "user", "snippet": "needle"})
    heads = search._session_heads(ranked, 60)
    ok = (len(heads) == 60 and len({h["session"] for h in heads}) == 60
          and heads[0]["session"] == "s000"
          and all(h["session_hits"] == 3 for h in heads))
    return ("PASS" if ok else "FAIL",
            f"requested=60 returned={len(heads)} unique={len({h['session'] for h in heads})}")


def t_conversation_family_retrieval():
    """Ranked retrieval keeps the strongest child but not a page of siblings."""
    import tempfile
    from pathlib import Path
    import numpy as np
    import ask
    import common
    import recall
    import search
    import semworker

    parents = {
        "child-a": {"parent": "root"},
        "child-b": {"parent": "root"},
        "grandchild": {"parent": "child-a"},
    }
    ranked = [
        {"session": "child-b", "score": .95},
        {"session": "root", "score": .90},
        {"session": "child-a", "score": .85},
        {"session": "other", "score": .80},
        {"session": "grandchild", "score": .75},
    ]
    heads = search._family_heads(ranked, 3, parents)
    keep = ask._family_representatives(
        [row["session"] for row in ranked],
        np.asarray([row["score"] for row in ranked], dtype=np.float32),
        parents)
    kept_sessions = [ranked[int(i)]["session"] for i in keep]
    saved_index = common.indexed_family_roots
    try:
        common.indexed_family_roots = lambda sessions: {
            session: common.family_root(session, parents)
            for session in sessions
        }
        packed = recall._select([
            {"session": "child-a", "_recall_query": 0},
            {"session": "other", "_recall_query": 0},
            {"session": "child-b", "_recall_query": 1},
        ], 3, 2)
    finally:
        common.indexed_family_roots = saved_index
    protocol_on = semworker._validate_request({
        "query": "q", "level": "hybrid", "k": 3,
        "filters": {"_family_diverse": True},
    })[3].get("_family_diverse") is True
    protocol_rejects = False
    try:
        semworker._validate_request({
            "query": "q", "level": "hybrid", "k": 3,
            "filters": {"_family_diverse": "yes"},
        })
    except ValueError:
        protocol_rejects = True
    derived_fixture = tempfile.TemporaryDirectory()
    _establish_current_derived_fixture(
        Path(derived_fixture.name), bind_common=True)
    saved_ensure, saved_request = semworker._ensure_worker, semworker._worker_request
    legacy_calls = []
    legacy_mismatch = False
    try:
        semworker._ensure_worker = lambda: {"pid": 1}
        def legacy_request(_rec, payload):
            legacy_calls.append(payload)
            if "_family_diverse" in payload.get("filters", {}):
                raise ValueError("invalid semantic filter")
            return {"results": [], "candidate_sessions": 0}
        semworker._worker_request = legacy_request
        try:
            semworker.search_worker(
                "q", level="hybrid", k=3, filters={"_family_diverse": True})
        except semworker.ResidentSemanticProtocolMismatch:
            legacy_mismatch = True
    finally:
        semworker._ensure_worker, semworker._worker_request = saved_ensure, saved_request
        derived_fixture.cleanup()
    legacy_safe = bool(legacy_mismatch and len(legacy_calls) == 1
                       and "_family_diverse" in legacy_calls[0]["filters"])
    ok = ([row["session"] for row in heads] == ["child-b", "other"]
          and kept_sessions == ["child-b", "other"]
          and [row["session"] for row in packed] == ["child-a", "child-b", "other"]
          and protocol_on and protocol_rejects and legacy_safe)
    return ("PASS" if ok else "FAIL",
            f"heads={[row['session'] for row in heads]} semantic={kept_sessions} "
            f"pack={[row['session'] for row in packed]} "
            f"protocol={protocol_on}/{protocol_rejects}/{legacy_safe}")


def t_prose_fts_lane():
    from collections import Counter
    import sqlite3
    import corpusdb

    if not corpusdb._trigram_ok():
        return ("SKIP", "sqlite build lacks FTS5 trigram")
    db = sqlite3.connect(":memory:")
    db.executescript(corpusdb._SCHEMA_SQL)

    def digested(row):
        return (*row, corpusdb.compact.content_digest(row[9]))

    rows = [
        digested(("s", 0, 1, "codex", "p", "", "", "", "user", "needle prose")),
        digested(("s", 0, 2, "codex", "p", "", "", "", "tool", "needle tool")),
    ]
    db.executemany(corpusdb._INS_DIGEST, rows)
    db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
    db.execute("INSERT INTO msgs_prose_fts(rowid, text) "
               "SELECT id, text FROM msgs WHERE who <> 'tool'")
    db.executescript(corpusdb._TRIGGERS_SQL)
    full = corpusdb.keyword(db, "needle", 99)["total"]
    prose = corpusdb.keyword(
        db, "needle", 99, {"include_tools": False})["hits"]
    db.execute("DELETE FROM msgs WHERE who = 'user'")
    after_delete = corpusdb.keyword(
        db, "needle", 99, {"include_tools": False})["total"]
    db.execute(corpusdb._INS_DIGEST, digested(
        ("s2", 1, 3, "codex", "p", "", "", "", "user", "needle new")))
    after_insert = corpusdb.keyword(
        db, "needle", 99, {"include_tools": False})["total"]

    # Incremental reconciliation is a multiset diff: identical tool rows are legal,
    # unchanged rows retain their rowids, and only the excess/changed rows touch FTS.
    stable = digested(
        ("diff", 0, 10, "codex", "p", "", "", "", "user", "stable anchor"))
    obsolete = digested(
        ("diff", 1, 11, "codex", "p", "", "", "", "agent", "obsolete answer"))
    duplicate = digested(
        ("diff", 1, 12, "codex", "p", "", "", "", "tool", "duplicate tool"))
    db.executemany(corpusdb._INS_DIGEST, [stable, obsolete, duplicate, duplicate])
    stable_id = db.execute(
        "SELECT id FROM msgs WHERE session='diff' AND text='stable anchor'").fetchone()[0]
    fresh = digested(
        ("diff", 1, 13, "codex", "p", "", "", "", "agent", "fresh answer"))
    desired = [stable, fresh, duplicate]
    added, removed = corpusdb._apply_session_row_diff(db, "diff", desired)
    final = Counter(tuple(row) for row in db.execute(
        f"SELECT {corpusdb._ROW_COLS} FROM msgs WHERE session='diff'"))
    stable_after = db.execute(
        "SELECT id FROM msgs WHERE session='diff' AND text='stable anchor'").fetchone()[0]
    obsolete_hits = corpusdb.keyword(db, "obsolete", 99)["total"]
    fresh_prose = corpusdb.keyword(
        db, "fresh", 99, {"include_tools": False})["total"]
    duplicate_full = corpusdb.keyword(db, "duplicate", 99)["total"]
    duplicate_prose = corpusdb.keyword(
        db, "duplicate", 99, {"include_tools": False})["total"]
    db.close()
    ok = (full == 2 and len(prose) == 1 and prose[0]["who"] == "user"
          and after_delete == 0 and after_insert == 1
          and (added, removed) == (1, 2) and final == Counter(desired)
          and stable_after == stable_id and obsolete_hits == 0 and fresh_prose == 1
          and duplicate_full == 1 and duplicate_prose == 0)
    return ("PASS" if ok else "FAIL",
            f"full={full} prose={len(prose)} delete={after_delete} insert={after_insert} "
            f"diff=+{added}/-{removed} exact={final == Counter(desired)} "
            f"stable-id={stable_after == stable_id} fts={obsolete_hits}/{fresh_prose}/"
            f"{duplicate_full}/{duplicate_prose}")


def t_star_delta_row_diff():
    import json
    import sqlite3
    import tempfile
    from pathlib import Path
    import corpusdb

    if not corpusdb._trigram_ok():
        return ("SKIP", "sqlite build lacks FTS5 trigram")
    rows = [
        ("s", 0, 1, "codex", "p", "", "", "", "user", "stable needle"),
        ("s", 0, 2, "codex", "p", "", "", "", "tool", "stable tool"),
    ]
    family_stamp = "family:selftest"
    old_stamp = json.dumps([
        [1, 1], [1, 1], [1, 1], [1, 1], [1, 1], [1, 1],
        ["tools", "on"], [1, 1], family_stamp,
    ])
    new_stamp = json.dumps([
        [2, 1], [1, 1], [1, 1], [1, 1], [1, 1], [1, 1],
        ["tools", "on"], [1, 1], family_stamp,
    ])
    seen_scopes, consumed = [], []
    got = None
    build_id = corpusdb.indexd_runtime.derived_writer_build_id(
        require_binary=True)
    saved = (corpusdb.DB_PATH, corpusdb._read_changed,
             corpusdb._read_session_families, corpusdb._scan,
             corpusdb._consume_changed, corpusdb._stamp)
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "corpus.db"
        seed = sqlite3.connect(path)
        seed.executescript(corpusdb._SCHEMA_SQL)
        seed.executemany(corpusdb._INS, rows)
        seed.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
        seed.execute("INSERT INTO msgs_prose_fts(rowid, text) "
                     "SELECT id, text FROM msgs WHERE who <> 'tool'")
        seed.executescript(corpusdb._TRIGGERS_SQL)
        seed.execute("INSERT INTO session_sig VALUES(?, ?)",
                     ("s", corpusdb._session_sig(rows)))
        seed.executemany("INSERT INTO meta VALUES(?, ?)",
                         [("schema", corpusdb._SCHEMA), ("stamp", old_stamp),
                          ("build_id", build_id)])
        seed.executescript("""
            CREATE TABLE audit(kind TEXT);
            CREATE TRIGGER audit_ai AFTER INSERT ON msgs BEGIN
                INSERT INTO audit VALUES('insert');
            END;
            CREATE TRIGGER audit_ad AFTER DELETE ON msgs BEGIN
                INSERT INTO audit VALUES('delete');
            END;
        """)
        stable_id = seed.execute(
            "SELECT id FROM msgs WHERE session='s' AND who='user'").fetchone()[0]
        seed.commit(); seed.close()
        try:
            corpusdb.DB_PATH = path
            corpusdb._read_changed = lambda: "*"
            corpusdb._read_session_families = lambda: corpusdb._SessionFamilySnapshot(
                family_stamp, frozenset({"s"}), {})

            def scan(scope=None):
                seen_scopes.append(scope)
                return {"s": list(rows)}

            corpusdb._scan = scan
            corpusdb._consume_changed = lambda: consumed.append(True)
            corpusdb._stamp = lambda: new_stamp
            got = corpusdb._incremental(new_stamp)
            if got is not None:
                audit = got.execute("SELECT count(*) FROM audit").fetchone()[0]
                stable_after = got.execute(
                    "SELECT id FROM msgs WHERE session='s' AND who='user'").fetchone()[0]
                stamped = got.execute(
                    "SELECT value FROM meta WHERE key='stamp'").fetchone()[0]
                family_rows = got.execute(
                    "SELECT session,root FROM session_family "
                    "ORDER BY session").fetchall()
                stored_family_stamp = got.execute(
                    "SELECT value FROM meta WHERE key='family_stamp'"
                ).fetchone()[0]
                fts = corpusdb.keyword(got, "needle", 9)["total"]
            else:
                audit = stable_after = fts = -1
                stamped = ""
                family_rows, stored_family_stamp = [], ""
        finally:
            if got is not None:
                got.close()
            (corpusdb.DB_PATH, corpusdb._read_changed,
             corpusdb._read_session_families, corpusdb._scan,
             corpusdb._consume_changed, corpusdb._stamp) = saved
    ok = (got is not None and seen_scopes == [None] and audit == 0
          and stable_after == stable_id and stamped == new_stamp
          and family_rows == [("s", "s")]
          and stored_family_stamp == family_stamp
          and consumed == [True] and fts == 1)
    return ("PASS" if ok else "FAIL",
            f"incremental={got is not None} full-scan={seen_scopes == [None]} "
            f"row-churn={audit} stable-id={stable_after == stable_id} "
            f"family={family_rows}/{stored_family_stamp == family_stamp} "
            f"consumed={len(consumed)} fts={fts}")


def t_legacy_stamp_manifest_slot():
    """A missing new manifest must not invalidate an otherwise identical schema-7 DB."""
    import json
    import sqlite3
    import tempfile
    from pathlib import Path
    import corpusdb

    old_six = [[1, 10], [2, 20], [3, 30], [4, 40], [5, 50], ["tools", "on"]]
    new_seven = old_six[:4] + [None] + old_six[4:]
    old_eight = [*new_seven, None]
    current_nine = [*old_eight, None]
    manifest_published = list(new_seven)
    manifest_published[4] = [9, 90]
    old_stamp = json.dumps(old_six)
    new_stamp = json.dumps(new_seven)
    published_stamp = json.dumps(manifest_published)
    got = None
    scans = []
    build_id = corpusdb.indexd_runtime.derived_writer_build_id(
        require_binary=True)
    saved = (corpusdb.DB_PATH, corpusdb._scan)
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "corpus.db"
        seed = sqlite3.connect(path)
        seed.executescript(corpusdb._SCHEMA_SQL)
        seed.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
        seed.executescript(corpusdb._TRIGGERS_SQL)
        seed.executemany("INSERT INTO meta VALUES(?, ?)",
                         [("schema", corpusdb._SCHEMA), ("stamp", old_stamp),
                          ("build_id", build_id)])
        seed.commit(); seed.close()
        try:
            corpusdb.DB_PATH = path
            corpusdb._scan = lambda *_args, **_kwargs: scans.append(True) or {
                "forbidden": []}
            got = corpusdb._valid_db(new_stamp)
            if got is not None:
                meta = dict(got.execute("SELECT key, value FROM meta"))
                trigger_sql = got.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='msgs_au'"
                ).fetchone()[0]
            else:
                meta, trigger_sql = {}, ""
        finally:
            if got is not None:
                got.close()
            corpusdb.DB_PATH, corpusdb._scan = saved
    absent_equal = corpusdb._stamps_equal(old_stamp, new_stamp)
    historical_equal = all(
        corpusdb._stamps_equal(json.dumps(layout), json.dumps(current_nine))
        for layout in (old_six, new_seven, old_eight)
    )
    published_moves = not corpusdb._stamps_equal(old_stamp, published_stamp)
    ok = (got is not None and not scans and absent_equal and historical_equal
          and published_moves
          and meta.get("stamp") == new_stamp
          and meta.get("fts_triggers") == corpusdb._TRIGGER_SCHEMA
          and "UPDATE OF text" in trigger_sql)
    return ("PASS" if ok else "FAIL",
            f"reused={got is not None} scans={len(scans)} absent-equal={absent_equal} "
            f"historical-equal={historical_equal} published-moves={published_moves} "
            f"stamp-migrated={meta.get('stamp') == new_stamp} "
            f"triggers-v{meta.get('fts_triggers', '?')}")


def t_concept_metadata_refresh():
    """Concept publications relabel rows in place and never write either FTS lane."""
    import json
    import sqlite3
    import tempfile
    from pathlib import Path
    import corpusdb

    if not corpusdb._trigram_ok():
        return ("SKIP", "sqlite build lacks FTS5 trigram")

    old_rows = [
        ("s", 0, 1, "codex", "p", "old concept", "", "", "user", "stable needle",
         corpusdb.compact.content_digest("stable needle")),
        ("s", 1, 2, "codex", "p", "old concept", "", "", "tool", "tool anchor",
         corpusdb.compact.content_digest("tool anchor")),
    ]
    new_rows = [tuple("new concept" if i == 5 else value for i, value in enumerate(row))
                for row in old_rows]
    family_stamp = "family:selftest"
    old_stamp = json.dumps([
        [1, 1], [1, 1], [1, 1], [1, 1], [10, 10], [1, 1],
        ["tools", "on"], [1, 1], family_stamp,
    ])
    new_stamp = json.dumps([
        [1, 1], [1, 1], [1, 1], [1, 1], [11, 10], [1, 1],
        ["tools", "on"], [1, 1], family_stamp,
    ])
    got = None
    verify = None
    consumed, scopes = [], []
    build_id = corpusdb.indexd_runtime.derived_writer_build_id(
        require_binary=True)
    saved = (corpusdb.DB_PATH, corpusdb._read_changed,
             corpusdb._read_session_families, corpusdb._scan,
             corpusdb._consume_changed, corpusdb._stamp)
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "corpus.db"
        seed = sqlite3.connect(path)
        seed.executescript(corpusdb._SCHEMA_SQL)
        seed.executemany(corpusdb._INS_DIGEST, old_rows)
        seed.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
        seed.execute("INSERT INTO msgs_prose_fts(rowid, text) "
                     "SELECT id, text FROM msgs WHERE who <> 'tool'")
        seed.executescript(corpusdb._TRIGGERS_SQL)
        # Recreate the pre-migration schema-7 UPDATE triggers and deliberately omit
        # fts_triggers metadata. This proves the migration is in-place, not just that
        # freshly-created fixtures have the right definitions.
        seed.executescript("""
            DROP TRIGGER msgs_au;
            DROP TRIGGER msgs_prose_au_old;
            DROP TRIGGER msgs_prose_au_new;
            CREATE TRIGGER msgs_au AFTER UPDATE ON msgs BEGIN
                INSERT INTO msgs_fts(msgs_fts, rowid, text)
                    VALUES('delete', old.id, old.text);
                INSERT INTO msgs_fts(rowid, text) VALUES(new.id, new.text);
            END;
            CREATE TRIGGER msgs_prose_au_old AFTER UPDATE ON msgs
            WHEN old.who <> 'tool' BEGIN
                INSERT INTO msgs_prose_fts(msgs_prose_fts, rowid, text)
                    VALUES('delete', old.id, old.text);
            END;
            CREATE TRIGGER msgs_prose_au_new AFTER UPDATE ON msgs
            WHEN new.who <> 'tool' BEGIN
                INSERT INTO msgs_prose_fts(rowid, text) VALUES(new.id, new.text);
            END;
        """)
        seed.executemany("INSERT INTO session_sig VALUES(?, ?)",
                         [("s", corpusdb._session_sig(old_rows))])
        seed.executemany("INSERT INTO meta VALUES(?, ?)",
                         [("schema", corpusdb._SCHEMA), ("stamp", old_stamp),
                          ("build_id", build_id)])
        seed.execute("CREATE TABLE fts_audit(lane TEXT, op TEXT, shadow TEXT)")
        # Audit the FTS shadow tables themselves. A delete+insert of identical text can
        # leave search results unchanged, so result parity alone would miss real churn.
        for lane in ("msgs_fts", "msgs_prose_fts"):
            tables = [row[0] for row in seed.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ?",
                (lane + "_%",))]
            for table in tables:
                suffix = table[len(lane) + 1:]
                for op in ("INSERT", "UPDATE", "DELETE"):
                    trigger = f"audit_{lane}_{suffix}_{op.lower()}"
                    seed.execute(
                        f"CREATE TRIGGER {trigger} AFTER {op} ON {table} BEGIN "
                        f"INSERT INTO fts_audit VALUES('{lane}', '{op.lower()}', '{suffix}'); END")
        stable_ids = dict(seed.execute(
            "SELECT who, id FROM msgs WHERE session='s'"))
        stable_id = stable_ids["user"]
        seed.commit(); seed.close()
        try:
            corpusdb.DB_PATH = path
            corpusdb._read_changed = lambda: None
            corpusdb._read_session_families = lambda: corpusdb._SessionFamilySnapshot(
                family_stamp, frozenset({"s"}), {})

            def scan(scope=None):
                scopes.append(scope)
                return {"s": list(new_rows)}

            corpusdb._scan = scan
            corpusdb._consume_changed = lambda: consumed.append(True)
            corpusdb._stamp = lambda: new_stamp
            got = corpusdb._incremental(new_stamp)
            if got is not None:
                concept_audit = got.execute("SELECT count(*) FROM fts_audit").fetchone()[0]
                row = got.execute(
                    "SELECT id, concept FROM msgs WHERE session='s' AND who='user'"
                ).fetchone()
                ids_after = dict(got.execute(
                    "SELECT who, id FROM msgs WHERE session='s'"))
                stored_sig = got.execute(
                    "SELECT sig FROM session_sig WHERE session='s'").fetchone()[0]
                meta = dict(got.execute("SELECT key, value FROM meta"))
                family_rows = got.execute(
                    "SELECT session,root FROM session_family "
                    "ORDER BY session").fetchall()
                trigger_sql = got.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='msgs_au'"
                ).fetchone()[0]
                # Audit triggers must go before the text-update check: user triggers inside
                # FTS5's shadow writes fail on some SQLite builds; parity proves both lanes update.
                audit_triggers = [r[0] for r in got.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'audit_%'")]
                got.close()
                verify = sqlite3.connect(path)
                for name in audit_triggers:
                    verify.execute(f"DROP TRIGGER {name}")
                verify.execute(
                    "UPDATE msgs SET text='fresh needle' WHERE id=?", (stable_id,))
                old_full = corpusdb.keyword(verify, "stable", 9)["total"]
                new_full = corpusdb.keyword(verify, "fresh", 9)["total"]
                new_prose = corpusdb.keyword(
                    verify, "fresh", 9, {"include_tools": False})["total"]
            else:
                concept_audit, row, meta, trigger_sql = -1, (-1, ""), {}, ""
                family_rows = []
                ids_after, stored_sig = {}, ""
                old_full, new_full, new_prose = -1, -1, -1
        finally:
            if verify is not None:
                verify.close()
            if got is not None:
                got.close()
            (corpusdb.DB_PATH, corpusdb._read_changed,
             corpusdb._read_session_families, corpusdb._scan,
             corpusdb._consume_changed, corpusdb._stamp) = saved
    ok = (got is not None and scopes == [None] and consumed == [True]
          and concept_audit == 0 and row == (stable_id, "new concept")
          and ids_after == stable_ids and stored_sig == corpusdb._session_sig(new_rows)
          and meta.get("stamp") == new_stamp
          and meta.get("family_stamp") == family_stamp
          and family_rows == [("s", "s")]
          and meta.get("fts_triggers") == corpusdb._TRIGGER_SCHEMA
          and "UPDATE OF text" in trigger_sql
          and old_full == 0 and new_full == 1 and new_prose == 1)
    return ("PASS" if ok else "FAIL",
            f"incremental={got is not None} scan={scopes} concept-fts-writes={concept_audit} "
            f"stable-ids={ids_after == stable_ids} sig={stored_sig == corpusdb._session_sig(new_rows)} "
            f"concept={row[1]!r} family={family_rows}/"
            f"{meta.get('family_stamp') == family_stamp} "
            f"triggers-v{meta.get('fts_triggers', '?')} "
            f"text-sync(full-old/full-new/prose-new)={old_full}/{new_full}/{new_prose}")


def t_canonical_windows():
    import json
    import tempfile
    from pathlib import Path
    import common
    import explore

    rows = [
        {"turn": 7, "ts": 101, "who": "tool", "text": "wrong tool echo"},
        {"turn": 7, "ts": 100, "who": "user", "text": "real prompt"},
        {"turn": 7, "ts": 102, "who": "agent", "text": "real reply"},
        {"turn": 7, "ts": 103, "who": "tool", "text": "another echo"},
    ]
    merged = explore._merge_transcript_rows(rows)
    canonical = (len(merged) == 1 and merged[0]["text"] == "real prompt"
                 and merged[0]["reply"] == "real reply")

    import events as ev
    old_data, old_events = common.DATA_DIR, ev.EVENTS_DIR
    try:
        with tempfile.TemporaryDirectory() as td:
            common.DATA_DIR = Path(td)
            ev.EVENTS_DIR = common.DATA_DIR / "events"
            path = explore.events_path("codex", "fixture")
            path.parent.mkdir(parents=True)
            events = [
                {"ts": 110, "kind": "tool", "name": "a", "output": "1"},
                {"ts": 150, "kind": "tool", "name": "b", "output": "2"},
                {"ts": 210, "kind": "tool", "name": "c", "output": "3"},
                {"ts": 250, "kind": "tool", "name": "d", "output": "4"},
                {"ts": 310, "kind": "tool", "name": "outside", "output": "5"},
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in events),
                            encoding="utf-8")
            explore._event_checkpoints.cache_clear()
            timeline = [{"turn": 1, "ts": 100}, {"turn": 2, "ts": 200},
                        {"turn": 3, "ts": 300}]
            selected = [{"turn": 1}, {"turn": 2}]
            got = explore._events_for_turns("codex", "fixture", selected, timeline)
            attribution = ([row["turn"] for row in got] == [1, 1, 2, 2]
                           and len({row["name"] for row in got}) == 4)
    finally:
        common.DATA_DIR, ev.EVENTS_DIR = old_data, old_events
        explore._event_checkpoints.cache_clear()
    ok = canonical and attribution
    return ("PASS" if ok else "FAIL",
            f"canonical_prompt={canonical} event_attribution={attribution}")


def t_embedding_memmap_publication():
    """Vector maps stay read-only/coherent and never pin the Windows publish path."""
    import tempfile
    from pathlib import Path
    import numpy as np
    import ask
    import common
    import embed
    import embedding_store

    key = "selftest-mmap-generation"
    saved_win, saved_link, saved_alive, saved_start = (
        embedding_store.WIN, embedding_store.os.link,
        embedding_store.pid_alive, embedding_store.process_start_identity)
    matrix = copied = legacy = pending_map = None
    checks = {}
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f32, ids, meta = (root / "embeddings.f32", root / "embeddings.ids",
                              root / "embeddings.meta")
            first = np.arange(12, dtype=np.float32).reshape(3, 4)
            second = first + 100
            common.write_embeddings(
                ["a", "b", "c"], first, f32, ids, dim=4, model_id="fixture",
                text_hashes=["ha", "hb", "hc"])
            gen1 = common.embedding_commit_identity(meta, f32, ids)

            matrix_ids, matrix = ask._cached(
                key, (f32, ids, meta),
                lambda: common.read_embeddings(f32, ids, dim=4, meta_path=meta))
            alias1 = getattr(matrix, "_agrep_snapshot_path", None)
            checks["mapped"] = (isinstance(matrix, np.memmap)
                                and not matrix.flags.writeable
                                and matrix_ids == ["a", "b", "c"]
                                and np.array_equal(matrix, first)
                                and common.embedding_matrix_identity(matrix)
                                == common.embedding_artifact_identity(meta, f32, ids))
            old_bundle = common.embedding_matrix_identity(matrix)

            # This is the behavior direct np.memmap lacks on Windows: an active
            # reader does not block replacement of the canonical publication path.
            common.write_embeddings(
                ["d", "e", "f"], second, f32, ids, dim=4, model_id="fixture",
                text_hashes=["hd", "he", "hf"])
            gen2 = common.embedding_commit_identity(meta, f32, ids)
            checks["generation_race"] = (
                old_bundle != common.embedding_artifact_identity(meta, f32, ids))
            next_ids, next_matrix = ask._cached(
                key, (f32, ids, meta),
                lambda: common.read_embeddings(f32, ids, dim=4, meta_path=meta))
            old_closed = bool(getattr(getattr(matrix, "_mmap", None), "closed", False))
            checks["replace"] = (gen1 and gen2 and gen1 != gen2 and old_closed
                                 and (not alias1 or not Path(alias1).exists())
                                 and next_ids == ["d", "e", "f"]
                                 and np.array_equal(next_matrix, second))
            matrix = next_matrix

            # Same row count used to make this mixed-pair window look valid.
            mixed = ids.with_suffix(".mixed")
            mixed.write_text("x\ny\nz\n", encoding="utf-8")
            common.replace_with_retry(mixed, ids)
            try:
                bad = common.read_embeddings(f32, ids, dim=4, meta_path=meta, attempts=1)
            except ValueError:
                checks["mixed_rejected"] = True
            else:
                common.close_embedding_matrix(bad[1])
                checks["mixed_rejected"] = False

            # Restore a committed pair, then exercise the no-hardlink fallback.
            common.write_embeddings(
                ["d", "e", "f"], second, f32, ids, dim=4, model_id="fixture",
                text_hashes=["hd", "he", "hf"])
            entry = ask._CACHE.pop(key, None)
            if entry:
                ask._release_cached_value(entry[1])
            matrix = None
            # Simulate Windows publication on every CI host without calling that
            # host's kernel32 just to identify this already-known test process.
            embedding_store.process_start_identity = lambda pid: f"win_test_{pid}"
            embedding_store.WIN = True
            embedding_store.os.link = (
                lambda *_a, **_k: (_ for _ in ()).throw(OSError("no hardlinks")))
            copy_ids, copied = common.read_embeddings(f32, ids, dim=4, meta_path=meta)
            copy_alias = getattr(copied, "_agrep_snapshot_path", None)
            checks["copy_fallback"] = (copy_ids == ["d", "e", "f"]
                                       and not getattr(copied, "_agrep_snapshot_hardlink", True)
                                       and bool(copy_alias) and Path(copy_alias).exists())
            common.close_embedding_matrix(copied)
            copied = None
            checks["copy_cleanup"] = bool(copy_alias) and not Path(copy_alias).exists()

            # Dead-owner aliases are crash debris, not permanent duplicate data.
            embedding_store.os.link = saved_link
            dead = root / f".{f32.name}.agrep-mmap-99999999-dead"
            embedding_store.os.link(f32, dead)
            embedding_store.pid_alive = lambda _pid: False
            common._prune_embedding_snapshots(f32)
            checks["crash_cleanup"] = not dead.exists()

            reused = root / f".{f32.name}.agrep-mmap-12345-win_old-dead"
            embedding_store.os.link(f32, reused)
            embedding_store.pid_alive = lambda _pid: True
            embedding_store.process_start_identity = lambda _pid: "win_new"
            common._prune_embedding_snapshots(f32)
            checks["pid_reuse_cleanup"] = not reused.exists()

            # Pre-commit installations remain readable and upgrade on their next
            # normal writer run; they still get stable-stat + row-count checking.
            legacy_root = root / "legacy"
            legacy_root.mkdir()
            lf32, lids, lmeta = (legacy_root / "embeddings.f32",
                                 legacy_root / "embeddings.ids",
                                 legacy_root / "embeddings.meta")
            first.tofile(lf32)
            lids.write_text("a\nb\nc\n", encoding="utf-8")
            common.write_index_meta(lmeta, 4, "legacy")
            legacy_ids, legacy = common.read_embeddings(
                lf32, lids, dim=4, meta_path=lmeta)
            checks["legacy"] = (legacy_ids == ["a", "b", "c"]
                                and np.array_equal(legacy, first)
                                and common.embedding_commit_identity(lmeta) is None)
            common.close_embedding_matrix(legacy)
            legacy = None
            legacy_matrix_stat = lf32.stat()
            upgraded = common.ensure_embedding_commit(lf32, lids, lmeta)
            upgraded_state = common.embedding_artifact_state(lmeta, lf32, lids)
            after_upgrade_stat = lf32.stat()
            checks["legacy_upgrade"] = (
                upgraded == upgraded_state["commit"]["generation"]
                and legacy_matrix_stat.st_size == after_upgrade_stat.st_size
                and legacy_matrix_stat.st_mtime_ns == after_upgrade_stat.st_mtime_ns
                and legacy_matrix_stat.st_ino == after_upgrade_stat.st_ino)

            pending_ids, pending_map = common.read_embeddings(
                lf32, lids, dim=4, meta_path=lmeta)
            retained = embed._retained_embedding_rows(
                pending_map, list(range(len(pending_ids))), has_pending=True)
            zero_change = embed._retained_embedding_rows(
                pending_map, list(range(len(pending_ids))), has_pending=False)
            pending_zero_copy = retained is pending_map
            zero_copy = zero_change is pending_map
            # Production write_embeddings_parts consumes the mapped part before
            # close; materialize this tiny fixture while the lifetime is valid.
            combined = np.vstack([retained, np.ones((1, 4), dtype=np.float32)])
            common.close_embedding_matrix(pending_map)
            pending_map = None
            checks["pending_mmap_lifetime"] = (
                pending_zero_copy and zero_copy
                and combined.shape == (4, 4)
                and np.array_equal(combined[:3], first))
    finally:
        entry = ask._CACHE.pop(key, None)
        if entry:
            ask._release_cached_value(entry[1])
        for value in (matrix, copied, legacy, pending_map):
            if value is not None:
                common.close_embedding_matrix(value)
        (embedding_store.WIN, embedding_store.os.link,
         embedding_store.pid_alive,
         embedding_store.process_start_identity) = (
            saved_win, saved_link, saved_alive, saved_start)
    ok = bool(checks) and all(checks.values())
    return ("PASS" if ok else "FAIL",
            " ".join(f"{name}={value}" for name, value in checks.items()))


def t_embedder_exact_batch_budget():
    """Batch padding is bounded by actual tokenizer lengths, including Unicode."""
    import numpy as np
    import embedder

    lengths = {"ascii": 5, "emoji": 800, "log": 511,
               "short": 7, "other": 513}

    class Encoding:
        def __init__(self, marker, n):
            self.marker = marker
            self.ids = [marker + 1] * n
            self.attention_mask = [1] * n
            self.type_ids = [0] * n

    class Tokenizer:
        def __init__(self):
            self.calls = []
            self.batch_calls = 0

        def encode(self, text):
            self.calls.append(text)
            return Encoding(list(lengths).index(text), lengths[text])

        def encode_batch(self, texts):
            self.batch_calls += 1
            width = max(lengths[text] for text in texts)
            return [Encoding(list(lengths).index(text), width) for text in texts]

    fake = embedder.Embedder.__new__(embedder.Embedder)
    fake.tok = Tokenizer()
    batches = []

    def run_encoded(encodings):
        lens = [len(row.ids) for row in encodings]
        batches.append(lens)
        return np.asarray([[row.marker, 1.0] for row in encodings],
                          dtype=np.float32)

    fake._run_encoded = run_encoded
    texts = list(lengths)
    got = fake.embed_texts(texts, token_budget=1024)
    bounded = all(
        len(batch) == 1 or len(batch) * max(batch) <= 1024
        for batch in batches)
    ordered = got[:, 0].astype(int).tolist() == list(range(len(texts)))
    once = fake.tok.calls == texts and fake.tok.batch_calls == 0
    ok = bounded and ordered and once and any(max(batch) == 800 for batch in batches)
    return ("PASS" if ok else "FAIL",
            f"batches={batches} bounded={bounded} ordered={ordered} once={once}")


def t_embedding_publish_serialization():
    """Concurrent manual writers can only publish one whole aligned generation."""
    import tempfile
    import threading
    import subprocess
    import sys
    from pathlib import Path
    import numpy as np
    import common

    errors = []
    checks = {}
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        f32, ids, meta = (root / "embeddings.f32", root / "embeddings.ids",
                          root / "embeddings.meta")
        barrier = threading.Barrier(8)

        def writer(generation: int) -> None:
            try:
                barrier.wait(2)
                common.write_embeddings(
                    [f"{generation}:a", f"{generation}:b", f"{generation}:c"],
                    np.full((3, 4), generation, dtype=np.float32),
                    f32, ids, dim=4, model_id=f"fixture-{generation}",
                    text_hashes=[f"h{generation}:a", f"h{generation}:b", f"h{generation}:c"])
            except Exception as exc:  # noqa: BLE001 -- surface every concurrent failure
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        loaded_ids, matrix = common.read_embeddings(f32, ids, dim=4, meta_path=meta)
        try:
            winner = int(loaded_ids[0].split(":", 1)[0])
            hashes = f32.with_suffix(".hashes").read_text(encoding="utf-8").splitlines()
            commit = common.read_embedding_commit(meta, f32, ids)
            checks["aligned"] = (
                loaded_ids == [f"{winner}:a", f"{winner}:b", f"{winner}:c"]
                and np.all(matrix == winner)
                and hashes == [f"h{winner}:a", f"h{winner}:b", f"h{winner}:c"]
                and bool(commit is not None and commit.get("hashes")))
        finally:
            common.close_embedding_matrix(matrix)
        leftovers = list(root.glob("*.tmp")) + list(root.glob(".*.tmp"))
        checks["serialized"] = not errors and all(not thread.is_alive() for thread in threads)
        publish_locks = (
            root / ".embeddings.f32.publish.lock",
            root / ".embeddings.meta.publish.lock",
        )
        checks["cleanup"] = not leftovers and not any(
            path.exists() for path in publish_locks)
        try:
            common.write_embeddings(
                ["dup", "dup"], np.zeros((2, 4), dtype=np.float32),
                f32, ids, dim=4, model_id="bad", text_hashes=["a", "b"])
        except ValueError:
            checks["duplicates_rejected"] = True
        else:
            checks["duplicates_rejected"] = False

        crash_root = root / "crash"
        crash_root.mkdir()
        crash_code = r'''
import os, sys
from pathlib import Path
import numpy as np
sys.path.insert(0, sys.argv[1])
import common
import embedding_store
class CrashLock:
    def __init__(self, *_a, **_k): pass
    def __enter__(self): os._exit(23)
    def __exit__(self, *_a): pass
embedding_store.EmbeddingPublishLock = CrashLock
root = Path(sys.argv[2])
common.write_embeddings(
    ["crash:a", "crash:b"], np.ones((2, 4), dtype=np.float32),
    root / "embeddings.f32", root / "embeddings.ids", dim=4,
    model_id="crash", text_hashes=["ca", "cb"])
'''
        crashed = subprocess.run(
            [sys.executable, "-c", crash_code, str(Path(REPO) / "py"), str(crash_root)],
            cwd=REPO, capture_output=True, timeout=30)
        abandoned = list(crash_root.glob(".*.tmp"))
        common.write_embeddings(
            ["live:a", "live:b"], np.full((2, 4), 9, dtype=np.float32),
            crash_root / "embeddings.f32", crash_root / "embeddings.ids", dim=4,
            model_id="live", text_hashes=["la", "lb"])
        checks["crash_temp_cleanup"] = (
            crashed.returncode == 23 and bool(abandoned)
            and not list(crash_root.glob(".*.tmp")))
    ok = all(checks.values())
    return ("PASS" if ok else "FAIL",
            f"checks={checks} errors={errors[:2]}")


def t_semantic_embedding_coherence():
    """Only exact source + committed vector generations are accepted as current."""
    import hashlib
    import json
    import tempfile
    from pathlib import Path
    import numpy as np
    import embedding_store
    import semantic
    import session_context

    common = semantic.common
    saved_common = (common.DATA_DIR, common.EMBEDDINGS_PATH, common.IDS_PATH)
    saved_embedding_paths = (
        embedding_store.DATA_DIR, embedding_store.EMBEDDINGS_PATH,
        embedding_store.IDS_PATH)
    saved_session_data = session_context.DATA_DIR
    saved_profile = semantic._active_embedding_profile
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            common.DATA_DIR = root
            _establish_current_derived_fixture(root)
            embedding_store.DATA_DIR = root
            session_context.DATA_DIR = root
            common.EMBEDDINGS_PATH = root / "embeddings.f32"
            common.IDS_PATH = root / "embeddings.ids"
            embedding_store.EMBEDDINGS_PATH = common.EMBEDDINGS_PATH
            embedding_store.IDS_PATH = common.IDS_PATH
            semantic._active_embedding_profile = (
                lambda recorded_model=None: (common.EMBED_DIM, "fixture"))

            messages = root / "messages.jsonl"
            replies = root / "replies.jsonl"
            signature = root / ".ingest.sig"
            meta = root / "embeddings.meta"
            source_body = '{"id":"new","text":"new text"}\n'
            changed_body = '{"id":"NEW","text":"new text"}\n'
            messages.write_text(source_body, encoding="utf-8")
            replies.write_text("", encoding="utf-8")
            signature.write_text("1:aaaa\n", encoding="utf-8")
            common.write_embeddings(
                ["old"], np.zeros((1, common.EMBED_DIM), dtype=np.float32),
                embeddings_path=common.EMBEDDINGS_PATH,
                ids_path=common.IDS_PATH,
                dim=common.EMBED_DIM, model_id="fixture", text_hashes=["hash"])

            before = semantic.embedding_coherence()
            # the refresh a real embed.py run performs: publish the new vectors,
            # then stamp the marker against the pre-run source generation
            source = semantic.source_generation()
            common.write_embeddings(
                ["new"], np.zeros((1, common.EMBED_DIM), dtype=np.float32),
                embeddings_path=common.EMBEDDINGS_PATH,
                ids_path=common.IDS_PATH,
                dim=common.EMBED_DIM, model_id="fixture",
                text_hashes=[hashlib.blake2b(
                    b"new text", digest_size=8).hexdigest()])
            semantic.write_generation_marker(source)
            after = semantic.embedding_coherence()

            source_stat = messages.stat()
            messages.write_text(changed_body, encoding="utf-8")
            os.utime(messages, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
            signature.write_text("1:bbbb\n", encoding="utf-8")
            changed = semantic.embedding_coherence()
            messages.write_text(source_body, encoding="utf-8")
            os.utime(messages, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
            signature.write_text("1:aaaa\n", encoding="utf-8")
            restored = semantic.embedding_coherence()

            marker_path = semantic.generation_marker_path()
            marker_raw = marker_path.read_text(encoding="utf-8")
            marker_path.write_text("[]", encoding="utf-8")
            malformed_marker = semantic.embedding_coherence()
            marker_path.write_text(marker_raw, encoding="utf-8")

            real_source = semantic.source_generation
            real_output = semantic.output_generation
            current_source = real_source()
            current_output = real_output()
            moved_source = json.loads(json.dumps(current_source))
            moved_source["ingest_signature"] = "f" * 64
            source_reads = iter((current_source, moved_source))
            try:
                semantic.source_generation = lambda attempts=4: next(source_reads)
                semantic.output_generation = lambda: current_output
                source_race = semantic.embedding_coherence()
            finally:
                semantic.source_generation = real_source
                semantic.output_generation = real_output

            moved_output = json.loads(json.dumps(current_output))
            moved_output["bundle"] += ":moved"
            output_reads = iter((current_output, moved_output))
            try:
                semantic.source_generation = lambda attempts=4: current_source
                semantic.output_generation = lambda: next(output_reads)
                output_race = semantic.embedding_coherence()
            finally:
                semantic.source_generation = real_source
                semantic.output_generation = real_output

            semantic._active_embedding_profile = lambda recorded_model=None: (
                common.EMBED_DIM, "different-profile")
            profile_mismatch = semantic.embedding_coherence()
            semantic._active_embedding_profile = lambda recorded_model=None: (
                common.EMBED_DIM, "fixture")

            corrupt_meta = json.loads(meta.read_text(encoding="utf-8"))
            corrupt_meta["commit"]["ids"]["sha256"] = "0" * 64
            meta.write_text(json.dumps(corrupt_meta), encoding="utf-8")
            corrupt = semantic.embedding_coherence()
    finally:
        (common.DATA_DIR, common.EMBEDDINGS_PATH, common.IDS_PATH) = saved_common
        (embedding_store.DATA_DIR, embedding_store.EMBEDDINGS_PATH,
         embedding_store.IDS_PATH) = saved_embedding_paths
        session_context.DATA_DIR = saved_session_data
        semantic._active_embedding_profile = saved_profile

    ok = (not before["coherent"] and before["state"] == "stale"
          and after["coherent"] and after["basis"] == "generation"
          and not changed["coherent"] and changed["state"] == "stale"
          and restored["coherent"]
          and not malformed_marker["coherent"] and malformed_marker["state"] == "stale"
          and source_race["state"] == "unstable-source"
          and output_race["state"] == "unstable-embeddings"
          and profile_mismatch["state"] == "profile-mismatch"
          and not corrupt["coherent"] and corrupt["state"] == "corrupt-embeddings")
    return ("PASS" if ok else "FAIL",
            f"before={before['state']} after={after['state']}/{after['basis']} "
            f"same-stat-change={changed['state']} restored={restored['state']} "
            f"malformed={malformed_marker['state']} "
            f"races={source_race['state']}/{output_race['state']} "
            f"profile={profile_mismatch['state']} "
            f"corrupt={corrupt['state']}")


def t_semantic_partial_coverage():
    """A newest-first subset is searchable and honest, while changed covered
    rows and malformed coverage are refused until a later pass repairs them."""
    import contextlib
    import hashlib
    import json
    import tempfile
    from pathlib import Path
    import numpy as np
    import ask
    import common
    import embed
    import embedder
    import embedding_store
    import index_lock
    import semantic
    import session_context

    common_names = ("DATA_DIR", "MESSAGES_PATH", "EMBEDDINGS_PATH", "IDS_PATH",
                    "INDEX_LOCK_PATH")
    saved_common = {name: getattr(common, name) for name in common_names}
    saved_embedding_paths = {
        name: getattr(embedding_store, name)
        for name in ("DATA_DIR", "MESSAGES_PATH", "EMBEDDINGS_PATH", "IDS_PATH")
    }
    saved_session_data = session_context.DATA_DIR
    saved_index_lock_path = index_lock.INDEX_LOCK_PATH
    saved_index_lock = common.IndexLock
    saved_read = common.read_embeddings
    saved_get = embedder.get
    saved_ask = (ask._embed_query, ask._guard_embedder,
                 ask.tool_search_messages, ask.invalidate_message_refs)
    saved_refresh = semantic.ensure_fresh_async
    saved_refs_refresh = semantic.ensure_refs_async
    saved_coherence = semantic.embedding_coherence
    saved_profile = semantic._active_embedding_profile
    saved_embed_paths = (embed.REPLIES_PATH, embed.HASHES_PATH)

    def text_hash(text):
        return hashlib.blake2b(text.encode(), digest_size=8).hexdigest()

    checks = {}
    try:
        with contextlib.ExitStack() as stack:
            td = stack.enter_context(tempfile.TemporaryDirectory())
            stack.callback(ask.clear_artifact_cache)
            root = Path(td)
            common.DATA_DIR = root
            _establish_current_derived_fixture(root)
            embedding_store.DATA_DIR = root
            session_context.DATA_DIR = root
            common.MESSAGES_PATH = root / "messages.jsonl"
            common.EMBEDDINGS_PATH = root / "embeddings.f32"
            common.IDS_PATH = root / "embeddings.ids"
            embedding_store.MESSAGES_PATH = common.MESSAGES_PATH
            embedding_store.EMBEDDINGS_PATH = common.EMBEDDINGS_PATH
            embedding_store.IDS_PATH = common.IDS_PATH
            common.INDEX_LOCK_PATH = root / ".index.lock"
            index_lock.INDEX_LOCK_PATH = common.INDEX_LOCK_PATH
            semantic._active_embedding_profile = (
                lambda recorded_model=None: (2, "fixture"))
            embedder.get = lambda download=True, lane=None: object()
            rows = [
                {"id": f"codex:s{i}:0", "agent": "codex", "session": f"s{i}",
                 "project": "p", "model": "m", "who": "user", "turn": 0,
                 "ts": i, "text": f"row {i} semantic text"}
                for i in range(5)
            ]

            def write_source(values):
                common.MESSAGES_PATH.write_text(
                    "".join(json.dumps(row) + "\n" for row in values),
                    encoding="utf-8")

            write_source(rows)
            (root / "replies.jsonl").write_text("", encoding="utf-8")
            (root / ".ingest.sig").write_text("5:test\n", encoding="utf-8")
            covered = rows[-2:]
            ids = [row["id"] for row in covered]
            common.write_embeddings(
                ids, np.asarray([[1.0, 0.0], [0.8, 0.2]], dtype=np.float32),
                embeddings_path=common.EMBEDDINGS_PATH, ids_path=common.IDS_PATH,
                dim=2, model_id="fixture",
                text_hashes=[text_hash(row["text"]) for row in covered])
            semantic.write_generation_marker(
                semantic.source_generation(), indexed_rows=2, total_rows=5)
            partial = semantic.embedding_coherence()
            checks["partial_state"] = (
                partial.get("searchable") and not partial.get("coherent")
                and partial.get("state") == "partial"
                and partial.get("coverage", {}).get("pending") == 3)

            common.read_embeddings = lambda embeddings_path=None, ids_path=None, dim=2, \
                    meta_path=None, **kwargs: saved_read(
                        common.EMBEDDINGS_PATH, common.IDS_PATH, dim, meta_path)
            ask._embed_query = lambda *a, **kw: np.asarray([1.0, 0.0], dtype=np.float32)
            ask._guard_embedder = lambda *a, **kw: None
            ask.clear_artifact_cache()
            ask.prepare_message_refs(partial["coverage"])
            result = json.loads(ask.tool_search_messages("q", 5, envelope=True))
            checks["subset_search"] = (
                {row["session"] for row in result["results"]} == {"s3", "s4"}
                and result.get("partial") is True
                and result.get("semantic_coverage", {}).get("indexed") == 2)

            # Demand is recorded even when this query falls back, allowing the
            # repair publisher to prewarm refs before the next semantic request.
            semantic.semantic_use_path().unlink(missing_ok=True)
            semantic.embedding_coherence = lambda: {
                "coherent": False, "searchable": False, "state": "stale"}
            semantic.ensure_fresh_async = (
                lambda max_new=None, **_kw: {"state": "running"})
            try:
                semantic.search("stale demand", level="message", k=2)
            except semantic.SemanticUnavailable:
                checks["stale_demand_noted"] = semantic.semantic_use_path().exists()
            else:
                checks["stale_demand_noted"] = False

            # A refresh launcher can observe that another publisher already won.
            # In that case the triggering query should use the repaired generation.
            repaired = {
                "coherent": True, "searchable": True, "state": "current",
                "coverage": {"indexed": 2, "total": 2, "pending": 0,
                             "complete": True},
            }
            semantic.ensure_fresh_async = lambda max_new=None, **_kw: {
                "state": "ready", "coherence": repaired}
            ask.tool_search_messages = lambda *a, **kw: json.dumps({
                "results": [], "candidate_sessions": 0, "truncated": False,
                "score_kind": "cosine"})
            checks["ready_repair_serves"] = (
                semantic.search("ready race", level="message", k=2).get("partial")
                is False)

            # Managed search carries coverage and starts only one bounded continuation.
            semantic.embedding_coherence = saved_coherence
            events = []
            ask.tool_search_messages = lambda *a, **kw: (
                events.append("query") or json.dumps({
                    "results": [], "candidate_sessions": 0, "truncated": False,
                    "score_kind": "cosine"}))
            refreshes = []
            semantic.ensure_fresh_async = lambda max_new=None, **_kw: (
                events.append("refresh") or refreshes.append(max_new)
                or {"state": "running"})
            managed = semantic.search("q", level="message", k=2)
            checks["managed_metadata"] = (
                managed.get("partial") is True
                and managed.get("semantic_coverage", {}).get("indexed") == 2
                and refreshes == [semantic.SEMANTIC_REFRESH_MAX_NEW]
                and events == ["query", "refresh"])
            semantic.ensure_fresh_async = (
                lambda max_new=None, **_kw: (_ for _ in ()).throw(
                    OSError("fixture spawn failure")))
            still_served = semantic.search("q", level="message", k=2)
            checks["partial_refresh_failure_serves"] = still_served.get("partial") is True

            invalidated = []
            ask.invalidate_message_refs = lambda: invalidated.append(True)
            ask.tool_search_messages = lambda *a, **kw: (_ for _ in ()).throw(
                ask.CorruptMessageRefs("poisoned candidate"))
            try:
                semantic.search("corrupt refs", level="message", k=2)
            except semantic.SemanticUnavailable:
                checks["corrupt_refs_fall_back"] = (
                    invalidated == [True]
                    and semantic.integrity_rebuild_requested())
            else:
                checks["corrupt_refs_fall_back"] = False
            semantic.clear_integrity_rebuild_request()
            ask.invalidate_message_refs = saved_ask[3]

            refs_started = []
            semantic.ensure_refs_async = lambda: (
                refs_started.append(True) or {"state": "running"})
            ask.tool_search_messages = lambda *a, **kw: (_ for _ in ()).throw(
                ask.MessageRefsUnavailable("refs missing"))
            try:
                semantic.search("missing refs", level="message", k=2)
            except semantic.SemanticUnavailable:
                checks["missing_refs_backgrounded"] = refs_started == [True]
            else:
                checks["missing_refs_backgrounded"] = False
            semantic.ensure_refs_async = saved_refs_refresh

            ask.tool_search_messages = lambda *a, **kw: json.dumps({
                "results": [], "candidate_sessions": 0, "truncated": False,
                "score_kind": "cosine", "partial": False,
                "semantic_coverage": {"indexed": 1, "total": 1,
                                      "pending": 0, "complete": True}})
            exact_payload = semantic.search("q", level="message", k=2)
            checks["validated_metadata_wins"] = (
                exact_payload.get("partial") is False
                and exact_payload.get("semantic_coverage", {}).get("indexed") == 1)
            observations = iter((
                {"coherent": False, "searchable": False,
                 "state": "unstable-embeddings"},
                partial,
            ))
            semantic.embedding_coherence = lambda: next(observations)
            retried = semantic.search(
                "q", level="message", k=2, refresh_if_stale=False)
            checks["transient_publication_retried"] = retried.get("partial") is False
            semantic.embedding_coherence = saved_coherence
            ask.tool_search_messages = saved_ask[2]

            marker = semantic.generation_marker_path()
            bad = json.loads(marker.read_text(encoding="utf-8"))
            bad["coverage"]["pending"] = 99
            marker.write_text(json.dumps(bad), encoding="utf-8")
            checks["bad_counter_refused"] = (
                semantic.embedding_coherence().get("state") == "stale")
            semantic.write_generation_marker(
                semantic.source_generation(), indexed_rows=2, total_rows=5)

            # A source publication during ONNX work may add uncovered rows. The
            # writer proves covered hashes still match and safely rebases the marker.
            embed.REPLIES_PATH = root / "replies.jsonl"
            embed.HASHES_PATH = root / "embeddings.hashes"
            class ForbiddenIndexLock:
                def __init__(self, *_args, **_kwargs):
                    raise AssertionError("refs prewarm must not hold ingest IndexLock")
            common.IndexLock = ForbiddenIndexLock
            old_source = semantic.source_generation()
            added = rows + [{
                "id": "codex:s5:0", "agent": "codex", "session": "s5",
                "project": "p", "model": "m", "who": "user", "turn": 0,
                "ts": 5, "text": "new uncovered row",
            }]
            write_source(added)
            rebased = embed._stamp(
                old_source, indexed_ids=ids,
                expected_hashes={row["id"]: text_hash(row["text"]) for row in covered},
                total_rows=5)
            checks["additive_rebased"] = (
                rebased == {"indexed": 2, "total": 6, "pending": 4}
                and semantic.embedding_coherence().get("state") == "partial")

            rebased_source = semantic.source_generation()
            changed = [dict(row) for row in added]
            changed[-1]["text"] = "changed covered text"
            changed[4]["text"] = "changed covered text"
            write_source(changed)
            refused_rebase = embed._stamp(
                rebased_source, indexed_ids=ids,
                expected_hashes={row["id"]: text_hash(row["text"]) for row in covered},
                total_rows=6)
            ask.clear_artifact_cache()
            try:
                ask.tool_search_messages("q", 5, envelope=True)
            except RuntimeError:
                checks["covered_change_refused"] = True
            else:
                checks["covered_change_refused"] = False
            checks["covered_change_refused"] &= refused_rebase is None

            write_source(rows)
            common.write_embeddings(
                [row["id"] for row in rows],
                np.tile(np.asarray([[1.0, 0.0]], dtype=np.float32), (5, 1)),
                embeddings_path=common.EMBEDDINGS_PATH, ids_path=common.IDS_PATH,
                dim=2, model_id="fixture",
                text_hashes=[text_hash(row["text"]) for row in rows])
            semantic.write_generation_marker(
                semantic.source_generation(), indexed_rows=5, total_rows=5)
            complete = semantic.embedding_coherence()
            checks["converged"] = (
                complete.get("coherent") and complete.get("state") == "current"
                and complete.get("coverage", {}).get("pending") == 0)
    finally:
        ask.clear_artifact_cache()
        common.IndexLock = saved_index_lock
        common.read_embeddings = saved_read
        embedder.get = saved_get
        (ask._embed_query, ask._guard_embedder, ask.tool_search_messages,
         ask.invalidate_message_refs) = saved_ask
        semantic.ensure_fresh_async = saved_refresh
        semantic.ensure_refs_async = saved_refs_refresh
        semantic.embedding_coherence = saved_coherence
        semantic._active_embedding_profile = saved_profile
        embed.REPLIES_PATH, embed.HASHES_PATH = saved_embed_paths
        for name, value in saved_common.items():
            setattr(common, name, value)
        for name, value in saved_embedding_paths.items():
            setattr(embedding_store, name, value)
        session_context.DATA_DIR = saved_session_data
        index_lock.INDEX_LOCK_PATH = saved_index_lock_path

    ok = all(checks.values())
    return ("PASS" if ok else "FAIL", f"checks={checks}")


def t_partial_embedding_accumulation():
    """Bounded passes retain old vectors, add newest pending rows, and converge
    without re-embedding already-covered text."""
    import json
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace
    import numpy as np
    import common
    import corpusdb
    import embed
    import embedder
    import embedding_segments
    import index_lock
    import semantic
    import session_context

    common_names = ("DATA_DIR", "MESSAGES_PATH", "EMBEDDINGS_PATH", "IDS_PATH",
                    "INDEX_LOCK_PATH")
    saved_common = {name: getattr(common, name) for name in common_names}
    saved_index_lock_path = index_lock.INDEX_LOCK_PATH
    saved_session_data = session_context.DATA_DIR
    saved_get = embedder.get
    saved_paths = (embed.REPLIES_PATH, embed.HASHES_PATH)
    corpusdb_names = ("DB_PATH", "BOUNDARY_STATS_PATH", "INGEST_SIG_PATH",
                      "CHANGED_PATH")
    saved_corpusdb = {name: getattr(corpusdb, name) for name in corpusdb_names}
    embedded_texts = []

    class FakeEmbedder:
        @property
        def profile_string(self):
            return embedder.PROFILE_STRING

        def embed_texts(self, texts):
            embedded_texts.extend(texts)
            out = np.zeros((len(texts), embedder.PROFILE["dim"]), dtype=np.float32)
            for i, text in enumerate(texts):
                out[i, 0] = float(text.rsplit(" ", 1)[-1]) + 1.0
                out[i, 1] = 1.0
            return out

    checks = {}
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            common.DATA_DIR = root
            _establish_current_derived_fixture(root)
            session_context.DATA_DIR = root
            common.MESSAGES_PATH = root / "messages.jsonl"
            common.EMBEDDINGS_PATH = root / "embeddings.f32"
            common.IDS_PATH = root / "embeddings.ids"
            common.INDEX_LOCK_PATH = root / ".index.lock"
            index_lock.INDEX_LOCK_PATH = common.INDEX_LOCK_PATH
            embed.REPLIES_PATH = root / "replies.jsonl"
            embed.HASHES_PATH = root / "embeddings.hashes"
            corpusdb.DB_PATH = root / "corpus.db"
            corpusdb.BOUNDARY_STATS_PATH = root / "boundary_stats.json"
            corpusdb.INGEST_SIG_PATH = root / ".ingest.sig"
            corpusdb.CHANGED_PATH = root / ".changed_sessions"
            expected_corpusdb = {
                "DB_PATH": root / "corpus.db",
                "BOUNDARY_STATS_PATH": root / "boundary_stats.json",
                "INGEST_SIG_PATH": root / ".ingest.sig",
                "CHANGED_PATH": root / ".changed_sessions",
            }
            checks["corpusdb_isolated"] = all(
                getattr(corpusdb, name) == path
                for name, path in expected_corpusdb.items())
            if not checks["corpusdb_isolated"]:
                raise RuntimeError("semantic fixture escaped its temporary data directory")
            rows = [
                {"id": f"codex:s{i}:0", "agent": "codex", "session": f"s{i}",
                 "project": "p", "ts": i, "turn": 0, "text": f"semantic row {i}",
                 "who": "user", "model": "m", "model_source": "explicit"}
                for i in range(7)
            ]
            common.MESSAGES_PATH.write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            embed.REPLIES_PATH.write_text("", encoding="utf-8")
            (root / ".ingest.sig").write_text("7:test\n", encoding="utf-8")
            embedder.get = lambda download=True, lane=None: FakeEmbedder()
            args = SimpleNamespace(full=False, smoke=None, max_new=2)
            observed = []
            first_ids = []
            for expected in (2, 4, 6, 7):
                rc = embed._run(args)
                state = semantic.embedding_coherence()
                observed.append(state.get("coverage", {}).get("indexed"))
                manifest = embedding_segments.load_manifest(
                    root / "embeddings.meta")
                ids = [row["mid"]
                       for row in embedding_segments.active_rows(manifest)]
                if expected == 2:
                    first_ids = ids
                checks[f"pass_{expected}"] = (
                    rc == 0 and observed[-1] == expected
                    and state.get("coverage", {}).get("pending") == 7 - expected)
            checks["newest_first"] = first_ids == ["codex:s6:0", "codex:s5:0"]
            checks["embedded_once"] = len(embedded_texts) == 7
            checks["converged"] = semantic.embedding_coherence().get("coherent")
            checks["refs_prewarmed"] = all(
                embedding_segments.artifact_path(
                    manifest, segment["artifacts"]["refs"]).is_file()
                for segment in manifest["segments"])
    finally:
        embedder.get = saved_get
        embed.REPLIES_PATH, embed.HASHES_PATH = saved_paths
        for name, value in saved_corpusdb.items():
            setattr(corpusdb, name, value)
        for name, value in saved_common.items():
            setattr(common, name, value)
        index_lock.INDEX_LOCK_PATH = saved_index_lock_path
        session_context.DATA_DIR = saved_session_data

    ok = all(checks.values())
    return ("PASS" if ok else "FAIL",
            f"coverage={observed} first={first_ids} embedded={len(embedded_texts)} "
            f"checks={checks}")


def t_bounded_semantic_source_plan():
    """The streaming planner matches the old stable newest-first selection while
    retaining text for at most N pending rows."""
    import hashlib
    import json
    import tempfile
    from pathlib import Path
    import numpy as np
    import common
    import embed

    saved = (common.MESSAGES_PATH, embed.REPLIES_PATH)
    saved_generation = embed.semantic.source_generation
    checks = {}
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            common.MESSAGES_PATH = root / "messages.jsonl"
            embed.REPLIES_PATH = root / "replies.jsonl"
            timestamps = [1, 10, 10, 5, 20, 3, 20, 8, 2, 15, 15, 4]
            rows = [{
                "id": f"codex:s{i}:0", "agent": "codex", "session": f"s{i}",
                "project": "p", "ts": ts, "turn": 0, "text": f"row {i}",
                "who": "user", "model": "m", "model_source": "explicit",
            } for i, ts in enumerate(timestamps)]
            common.MESSAGES_PATH.write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            replies = [{"id": "codex:s1:0", "reply": "reply one"},
                       {"id": "codex:s4:0", "reply": "reply four"}]
            embed.REPLIES_PATH.write_text(
                "".join(json.dumps(row) + "\n" for row in replies), encoding="utf-8")

            base = list(common.iter_messages(path=common.MESSAGES_PATH))
            ts_by = {row.id: row.ts for row in base}
            all_rows = base + list(embed.iter_reply_messages(ts_by))
            reference = sorted(all_rows, key=lambda row: row.ts, reverse=True)[:3]
            hashes, selected, count = embed._scan_source(
                rebuild=True, old_hash_by_id=None, max_new=3)
            checks["rebuild_selection"] = (
                [row.id for row in selected] == [row.id for row in reference]
                and count == len(all_rows) and len(hashes) == len(all_rows)
                and len(selected) == 3)

            digest = lambda text: hashlib.blake2b(
                text.encode(), digest_size=8).hexdigest()
            old = {row.id: digest(row.text) for row in all_rows}
            old.pop("codex:s9:0")
            old["codex:s2:0"] = "changed"
            pending_reference = [row for row in all_rows
                                 if old.get(row.id) != digest(row.text)]
            expected = (sorted(pending_reference, key=lambda row: row.ts,
                               reverse=True)[:1])
            hashes2, selected2, count2 = embed._scan_source(
                rebuild=False, old_hash_by_id=old, max_new=1)
            checks["incremental_selection"] = (
                count2 == 2 and [row.id for row in selected2]
                == [row.id for row in expected] and len(selected2) == 1
                and len(hashes2) == len(all_rows))

            hashes3, selected3, count3 = embed._scan_source(
                rebuild=False, old_hash_by_id=old, max_new=8)
            checks["under_cap_source_order"] = (
                count3 == 2 and [row.id for row in selected3]
                == [row.id for row in pending_reference])

            class FakeEmbedder:
                @property
                def profile_string(self):
                    return embedder.PROFILE_STRING

                def __init__(self):
                    self.calls = []

                def embed_texts(self, texts):
                    self.calls.append(len(texts))
                    return np.zeros((len(texts), 2), dtype=np.float32)

            batch_n = embed._BACKGROUND_CHUNK + 7
            fake = FakeEmbedder()
            embed.semantic.source_generation = lambda: {"generation": "moved"}
            parts, done, moved = embed._embed_pending_chunks(
                fake, ["x"] * batch_n, {"generation": "old"}, background=True)
            checks["moving_background_yields"] = (
                moved and done == embed._BACKGROUND_CHUNK and len(parts) == 1
                and len(parts[0]) == embed._BACKGROUND_CHUNK
                and fake.calls == [1])
            fake = FakeEmbedder()
            embed.semantic.source_generation = lambda: (_ for _ in ()).throw(
                RuntimeError("publication window"))
            parts, done, moved = embed._embed_pending_chunks(
                fake, ["x"] * batch_n, {"generation": "old"}, background=True)
            checks["transient_source_yields"] = (
                moved and done == embed._BACKGROUND_CHUNK and len(parts) == 1
                and len(parts[0]) == embed._BACKGROUND_CHUNK
                and fake.calls == [1])
            fake = FakeEmbedder()
            parts, done, moved = embed._embed_pending_chunks(
                fake, ["x"] * batch_n, {"generation": "old"}, background=False)
            checks["foreground_finishes"] = (
                not moved and done == batch_n and len(parts) == 1
                and len(parts[0]) == batch_n and fake.calls == [1])

            class CollidingText(str):
                def __hash__(self):
                    return 1

            class ValueEmbedder:
                def __init__(self):
                    self.calls = []

                def embed_texts(self, texts):
                    self.calls.append(list(texts))
                    return np.asarray([[i + 1, 0] for i in range(len(texts))],
                                      dtype=np.float32)

            fake = ValueEmbedder()
            values = [CollidingText("alpha"), CollidingText("beta"),
                      CollidingText("alpha")]
            parts, done, moved = embed._embed_pending_chunks(
                fake, values, None, background=False)
            checks["dedup_collision_safe_order"] = (
                fake.calls == [["alpha", "beta"]] and done == 3 and not moved
                and np.concatenate(parts)[:, 0].tolist() == [1, 2, 1])
    finally:
        common.MESSAGES_PATH, embed.REPLIES_PATH = saved
        embed.semantic.source_generation = saved_generation

    ok = all(checks.values())
    return ("PASS" if ok else "FAIL", f"checks={checks}")


def t_semantic_post_ingest_rebase():
    """An additive ingest keeps an unchanged vector subset searchable without
    ONNX work; changed covered text is refused, including on the delta fast path."""
    import hashlib
    import json
    import tempfile
    from pathlib import Path
    import numpy as np
    import ask
    import common
    import embed
    import embedder
    import embedding_store
    import semantic
    import session_context

    common_names = ("DATA_DIR", "MESSAGES_PATH", "EMBEDDINGS_PATH", "IDS_PATH")
    saved_common = {name: getattr(common, name) for name in common_names}
    saved_embedding_paths = {
        name: getattr(embedding_store, name)
        for name in ("DATA_DIR", "MESSAGES_PATH", "EMBEDDINGS_PATH", "IDS_PATH")
    }
    saved_session_data = session_context.DATA_DIR
    saved_paths = (embed.REPLIES_PATH, embed.HASHES_PATH)
    saved_rebase_helpers = (embed._delta_rebase_total, embed._full_rebase_total)
    saved_prepare_refs = ask.prepare_message_refs
    checks = {}

    def digest(text):
        return hashlib.blake2b(text.encode(), digest_size=8).hexdigest()

    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            common.DATA_DIR = root
            _establish_current_derived_fixture(root)
            embedding_store.DATA_DIR = root
            session_context.DATA_DIR = root
            common.MESSAGES_PATH = root / "messages.jsonl"
            common.EMBEDDINGS_PATH = root / "embeddings.f32"
            common.IDS_PATH = root / "embeddings.ids"
            embedding_store.MESSAGES_PATH = common.MESSAGES_PATH
            embedding_store.EMBEDDINGS_PATH = common.EMBEDDINGS_PATH
            embedding_store.IDS_PATH = common.IDS_PATH
            embed.REPLIES_PATH = root / "replies.jsonl"
            embed.HASHES_PATH = root / "embeddings.hashes"
            rows = [{
                "id": f"codex:s{i}:0", "agent": "codex", "session": f"s{i}",
                "project": "p", "ts": i, "turn": 0, "text": f"row {i}",
                "who": "user", "model": "m", "model_source": "explicit",
            } for i in range(3)]

            def publish_source(values, sig):
                common.MESSAGES_PATH.write_text(
                    "".join(json.dumps(row) + "\n" for row in values),
                    encoding="utf-8")
                (root / ".ingest.sig").write_text(sig, encoding="utf-8")

            publish_source(rows, "3:a\n")
            embed.REPLIES_PATH.write_text("", encoding="utf-8")
            covered = rows[:2]
            ids = [row["id"] for row in covered]
            common.write_embeddings(
                ids, np.zeros((2, embedder.PROFILE["dim"]), dtype=np.float32),
                embeddings_path=common.EMBEDDINGS_PATH,
                ids_path=common.IDS_PATH, dim=embedder.PROFILE["dim"],
                model_id=embedder.PROFILE_STRING,
                text_hashes=[digest(row["text"]) for row in covered])
            semantic.write_generation_marker(
                semantic.source_generation(), indexed_rows=2, total_rows=3)

            added = rows + [{
                "id": "codex:s3:0", "agent": "codex", "session": "s3",
                "project": "p", "ts": 3, "turn": 0, "text": "row 3",
                "who": "user", "model": "m", "model_source": "explicit",
            }]
            publish_source(added, "4:b\n")
            rebased = embed.rebase_generation_marker()
            state = semantic.embedding_coherence()
            checks["additive_rebased"] = (
                rebased == {"indexed": 2, "total": 4, "pending": 2}
                and state.get("searchable") and state.get("state") == "partial")

            # The delta shortcut is legal only for the exact marker-source ->
            # current-source transition. A lagged marker must use full validation.
            exact_before = semantic.source_generation()
            added2 = added + [{
                "id": "codex:s4:0", "agent": "codex", "session": "s4",
                "project": "p", "ts": 4, "turn": 0, "text": "row 4",
                "who": "user", "model": "m", "model_source": "explicit",
            }]
            publish_source(added2, "5:c\n")
            exact_after = semantic.source_generation()
            embed._delta_rebase_total = lambda *a, **kw: 5
            embed._full_rebase_total = lambda *a, **kw: None
            exact = embed.rebase_generation_marker(
                {"s4"}, expected_previous_source=exact_before,
                expected_current_source=exact_after)
            checks["exact_delta_rebased"] = (
                exact == {"indexed": 2, "total": 5, "pending": 3})

            added3 = added2 + [{
                "id": "codex:s5:0", "agent": "codex", "session": "s5",
                "project": "p", "ts": 5, "turn": 0, "text": "row 5",
                "who": "user", "model": "m", "model_source": "explicit",
            }]
            publish_source(added3, "6:d\n")
            lagged = embed.rebase_generation_marker(
                {"s5"}, expected_previous_source=exact_before,
                expected_current_source=semantic.source_generation())
            checks["lagged_delta_refused"] = (
                lagged is None and semantic.embedding_coherence().get("state") == "stale")
            embed._delta_rebase_total, embed._full_rebase_total = saved_rebase_helpers

            changed = [dict(row) for row in added3]
            changed[0]["text"] = "covered row changed"
            publish_source(changed, "6:e\n")
            checks["covered_change_refused"] = (
                embed.rebase_generation_marker() is None
                and semantic.embedding_coherence().get("state") == "stale")

            delta_rows = [{
                "id": "codex:s0:0", "agent": "codex", "session": "s0",
                "project": "p", "ts": 0, "turn": 0, "text": "u0",
                "who": "user", "model": "m", "model_source": "explicit",
            }, {
                "id": "codex:s1:1", "agent": "codex", "session": "s1",
                "project": "p", "ts": 1, "turn": 1, "text": "u1",
                "who": "user", "model": "m", "model_source": "explicit",
            }]
            publish_source(delta_rows, "2:f\n")
            embed.REPLIES_PATH.write_text(
                json.dumps({"id": "codex:s0:0", "reply": "a0"}) + "\n",
                encoding="utf-8")
            delta_ids = ["codex:s0:0", "codex:s0:0#r", "codex:s1:1"]
            delta_hashes = dict(zip(delta_ids, map(digest, ("u0", "a0", "u1"))))
            checks["delta_valid"] = embed._delta_rebase_total(
                delta_ids, delta_hashes, {"s0"}) == 3
            embed.REPLIES_PATH.write_text(
                json.dumps({"id": "codex:s0:0", "reply": "moved"}) + "\n",
                encoding="utf-8")
            checks["delta_change_refused"] = embed._delta_rebase_total(
                delta_ids, delta_hashes, {"s0"}) is None

            prepared = []
            ask.prepare_message_refs = lambda coverage: (
                prepared.append(dict(coverage))
                or {"rows": coverage["indexed"], "bytes": 0})
            semantic.note_semantic_use()
            embed._prepare_refs_for_publication(
                {"indexed": 2, "total": 4, "pending": 2})
            old = time.time() - semantic.SEMANTIC_REFS_DEMAND_S - 1
            os.utime(semantic.semantic_use_path(), (old, old))
            embed._prepare_refs_for_publication(
                {"indexed": 3, "total": 4, "pending": 1})
            embed._prepare_refs_for_publication(
                {"indexed": 1, "total": 4, "pending": 3}, bootstrap=True)
            checks["demand_aware_refs"] = (
                [row["indexed"] for row in prepared] == [2, 1])
    finally:
        ask.prepare_message_refs = saved_prepare_refs
        embed._delta_rebase_total, embed._full_rebase_total = saved_rebase_helpers
        embed.REPLIES_PATH, embed.HASHES_PATH = saved_paths
        for name, value in saved_common.items():
            setattr(common, name, value)
        for name, value in saved_embedding_paths.items():
            setattr(embedding_store, name, value)
        session_context.DATA_DIR = saved_session_data

    ok = all(checks.values())
    return ("PASS" if ok else "FAIL", f"checks={checks}")


def t_semantic_fresh_async():
    """A stale lane spawns exactly one detached embed.py; a live claim or a fresh
    failure state suppresses respawn (no crash-looping, no stacked embedders)."""
    import json as _json
    import tempfile
    import time as _time
    from pathlib import Path
    import semantic

    common = semantic.common
    import embedder
    saved = (common.DATA_DIR, semantic.embedding_coherence, semantic.hashes_aligned,
             semantic._needs_unverified_bundle_rebuild, semantic.subprocess.Popen,
             semantic.runtime_dependencies_available, embedder.ensure_model)
    spawns = []

    class FakeProc:
        pid = 4242

    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            common.DATA_DIR = root
            _establish_current_derived_fixture(root)
            semantic.embedding_coherence = lambda: {"coherent": False, "state": "stale"}
            semantic.hashes_aligned = lambda: True
            semantic._needs_unverified_bundle_rebuild = lambda: False
            semantic.subprocess.Popen = lambda cmd, **kw: spawns.append(cmd) or FakeProc()
            semantic.runtime_dependencies_available = lambda: True
            # spawn policy is under test, not the model cache: a box that
            # never downloaded the model must see the same decisions
            embedder.ensure_model = lambda download=True: None

            first = semantic.ensure_fresh_async(max_new=100)
            spawned_once = (first["state"] == "running" and len(spawns) == 1
                            and any(str(a).endswith("embed.py") for a in spawns[0])
                            and "--max-new" in spawns[0]
                            and "--background" in spawns[0])

            # a LIVE claim (this pid + its start identity) means an embed pass owns
            # the run: no respawn. The identity is required - a pid-only claim is
            # deliberately treated as legacy/recycled and NOT running.
            semantic.embed_claim_path().write_text(
                _json.dumps({"pid": os.getpid(),
                             "process_start": common.process_start_identity(os.getpid())}),
                encoding="utf-8")
            second = semantic.ensure_fresh_async()
            deduped = second["state"] == "running" and len(spawns) == 1
            semantic.embed_claim_path().unlink()

            # A legacy/unverified bundle requests a clean rebuild without trying
            # to parse the very generation metadata known to be legacy/corrupt.
            semantic._needs_unverified_bundle_rebuild = lambda: True
            legacy = semantic.ensure_fresh_async(max_new=100)
            legacy_rebuild = (legacy["state"] == "running" and len(spawns) == 2
                              and "--full" in spawns[-1])

            # a fresh failure backs off instead of relaunching every query
            semantic.embed_state_path().write_text(
                _json.dumps({"state": "failed", "finished_at": _time.time()}),
                encoding="utf-8")
            third = semantic.ensure_fresh_async()
            cooled = third["state"] == "failed" and len(spawns) == 2

            refs = semantic.ensure_refs_async()
            refs_backgrounded = (
                refs["state"] == "running" and len(spawns) == 3
                and "--refs-only" in spawns[-1]
                and "--background" in spawns[-1])
    finally:
        (common.DATA_DIR, semantic.embedding_coherence, semantic.hashes_aligned,
         semantic._needs_unverified_bundle_rebuild, semantic.subprocess.Popen,
         semantic.runtime_dependencies_available, embedder.ensure_model) = saved

    ok = spawned_once and deduped and legacy_rebuild and cooled and refs_backgrounded
    return ("PASS" if ok else "FAIL",
            f"spawned_once={spawned_once} deduped={deduped} "
            f"legacy_rebuild={legacy_rebuild} cooled={cooled} "
            f"refs={refs_backgrounded} "
            f"spawns={len(spawns)}")


def t_embed_governor():
    """Background embedding yields to low battery / busy CPU before any claim or
    model work; explicit, --full, and refs-only runs never consult the governor;
    AGREP_EMBED_ANYWAY=1 bypasses it; the pmset probe runs at most once."""
    import tempfile
    from pathlib import Path
    import embed

    common = embed.common
    saved = (embed._probe_battery, embed._normalized_load, embed._acquire_claim,
             common.log, sys.argv)
    saved_nice = getattr(os, "nice", None)
    saved_env = {k: os.environ.get(k)
                 for k in ("AGREP_EMBED_ANYWAY", "AGREP_SEM_THREADS")}
    consulted, claimed, logs = [], [], []
    derived_fixture = tempfile.TemporaryDirectory()

    def deferred_notes():
        return [line for line in logs if line.startswith("embedding deferred: ")]

    def scenario(argv, battery, load, anyway=None):
        embed._BATTERY_PROBE.clear()
        consulted.clear()
        claimed.clear()
        logs.clear()
        embed._probe_battery = lambda: consulted.append("batt") or battery
        embed._normalized_load = lambda: consulted.append("load") or load
        if anyway is None:
            os.environ.pop("AGREP_EMBED_ANYWAY", None)
        else:
            os.environ["AGREP_EMBED_ANYWAY"] = anyway
        sys.argv = ["embed.py", *argv]
        return embed.main()

    try:
        _establish_current_derived_fixture(
            Path(derived_fixture.name), bind_common=True)
        # bool(list.append(...)) is False: the stub records the consultation, then refuses
        embed._acquire_claim = lambda: bool(claimed.append(True))
        common.log = logs.append
        if saved_nice is not None:
            os.nice = lambda n: 0  # allowed runs renice; don't demote the test process

        rc = scenario(["--background"], (True, 12), 0.0)
        battery_declines = bool(
            rc == 0 and not claimed
            and deferred_notes() == ["embedding deferred: on battery (12%)"])

        rc = scenario(["--background"], (False, None), 4.0)
        load_declines = bool(
            rc == 0 and not claimed
            and deferred_notes() == ["embedding deferred: load 4.0/core"])

        rc = scenario(["--background"], (False, 90), 0.1)
        ac_allows = bool(rc == 0 and claimed and not deferred_notes())

        rc = scenario(["--background"], (True, 55), 0.1)
        healthy_battery_allows = bool(rc == 0 and claimed and not deferred_notes())

        rc = scenario(["--background"], (True, 5), 9.9, anyway="1")
        bypass_allows = bool(rc == 0 and claimed and not consulted)

        rc = scenario(["--full"], (True, 5), 9.9)
        full_skips = bool(rc == 0 and claimed and not consulted)

        rc = scenario(["--background", "--refs-only"], (True, 5), 9.9)
        refs_skips = bool(rc == 0 and claimed and not consulted)

        probes = []
        embed._BATTERY_PROBE.clear()
        embed._probe_battery = lambda: probes.append(1) or (False, None)
        embed._battery_state()
        embed._battery_state()
        probe_cached = len(probes) == 1
    finally:
        (embed._probe_battery, embed._normalized_load, embed._acquire_claim,
         common.log, sys.argv) = saved
        if saved_nice is not None:
            os.nice = saved_nice
        embed._BATTERY_PROBE.clear()
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        derived_fixture.cleanup()

    ok = (battery_declines and load_declines and ac_allows
          and healthy_battery_allows and bypass_allows and full_skips
          and refs_skips and probe_cached)
    return ("PASS" if ok else "FAIL",
            f"battery={battery_declines} load={load_declines} ac={ac_allows} "
            f"healthy_batt={healthy_battery_allows} bypass={bypass_allows} "
            f"full_skips={full_skips} refs_skips={refs_skips} cached={probe_cached}")


def t_embed_claim_recovery():
    """Embed claims recover crash debris and recycled PIDs, and detached
    inference failures publish a cooldown state instead of crash-looping."""
    import json
    import tempfile
    from pathlib import Path
    import common
    import embed
    import semantic

    saved = (common.DATA_DIR, common.pid_alive, common.process_start_identity,
             embed._run)
    checks = {}
    try:
        with tempfile.TemporaryDirectory() as td:
            common.DATA_DIR = Path(td)
            _establish_current_derived_fixture(common.DATA_DIR)
            common.pid_alive = lambda pid: True
            common.process_start_identity = lambda pid: "actual-start"
            claim = semantic.embed_claim_path()

            claim.write_text("{", encoding="utf-8")
            old = time.time() - embed._MALFORMED_CLAIM_STALE_S - 1
            os.utime(claim, (old, old))
            checks["malformed_reclaimed"] = embed._acquire_claim()
            embed._release_claim()
            checks["exact_release"] = not claim.exists()

            claim.write_text(json.dumps({
                "pid": os.getpid(), "process_start": "actual-start",
                "token": "other",
            }), encoding="utf-8")
            checks["live_kept"] = not embed._acquire_claim()
            claim.unlink()

            claim.write_text(json.dumps({
                "pid": os.getpid(), "process_start": "recycled-start",
                "token": "other",
            }), encoding="utf-8")
            checks["pid_reuse_reclaimed"] = embed._acquire_claim()
            embed._release_claim()

            def fail(_args):
                raise RuntimeError("fixture inference failure")

            embed._run = fail
            rc = embed.main()
            state = json.loads(semantic.embed_state_path().read_text(encoding="utf-8"))
            checks["failure_published"] = (
                rc == 1 and state.get("state") == "failed"
                and "fixture inference failure" in state.get("error", "")
                and not claim.exists())
    finally:
        (common.DATA_DIR, common.pid_alive, common.process_start_identity,
         embed._run) = saved

    ok = all(checks.values())
    return ("PASS" if ok else "FAIL", f"checks={checks}")


def t_semantic_resident_worker():
    """The disposable worker authenticates loopback requests, serializes every
    semantic cache access onto one owner thread, and removes its descriptor."""
    import contextlib
    import os as _os
    import json
    import tempfile
    import threading
    from pathlib import Path
    import common
    import indexd_runtime
    import resources
    import semworker

    saved = (common.DATA_DIR, indexd_runtime.SEARCH_BEAT_PATH, resources.EMBEDDINGS_PATH,
             resources.available_memory_fraction,
             common.bind_descendants_to_process_lifetime)
    calls = []
    thread = None
    server = None
    lifetime = None
    checks = {}

    def fake_search(query, *, level, k, filters):
        calls.append((threading.get_ident(), query, level, k, filters))
        return {"results": [{"session": query}], "score_kind": "cosine",
                "candidate_sessions": 1, "truncated": False}

    try:
        with tempfile.TemporaryDirectory() as td, contextlib.ExitStack() as cleanup:
            root = Path(td)
            common.DATA_DIR = root
            _establish_current_derived_fixture(root)
            indexd_runtime.SEARCH_BEAT_PATH = root / ".search.beat"
            resources.EMBEDDINGS_PATH = root / "embeddings.f32"
            resources.available_memory_fraction = lambda: 0.5
            common.bind_descendants_to_process_lifetime = lambda: True
            lifetime = semworker.acquire_resident_owner()
            if lifetime is None:
                return ("FAIL", "semantic worker owner was not acquired")

            def release_owner():
                nonlocal lifetime
                if lifetime is not None:
                    checks["owner_release"] = lifetime.release(
                        tombstone=True, require_stable_mtime=True)
                    lifetime = None

            cleanup.callback(release_owner)
            try:
                server = semworker.SemanticWorkerServer(
                    search_fn=fake_search, lifetime=lifetime)
            except PermissionError as exc:
                return ("SKIP", f"loopback bind blocked by sandbox: {exc}")

            def stop_server():
                if server is not None:
                    server.request_stop()
                if thread is not None:
                    thread.join(timeout=2)

            cleanup.callback(stop_server)
            thread = threading.Thread(target=server.serve, daemon=True)
            thread.start()
            first = semworker.search_worker(
                "one", level="hybrid", k=3, filters={"agent": "codex"})
            second = semworker.search_worker(
                "two", level="message", k=2, filters={})
            checks["round_trip"] = (
                first["results"][0]["session"] == "one"
                and second["results"][0]["session"] == "two")
            checks["single_owner"] = (
                len(calls) == 2 and len({call[0] for call in calls}) == 1
                and calls[0][0] != threading.get_ident())
            checks["private_descriptor"] = (
                sys.platform == "win32"
                or (_os.stat(semworker.descriptor_path()).st_mode & 0o077) == 0)
            bad = dict(json.loads(server.record))
            bad["token"] = "0" * 64
            try:
                semworker._worker_request(
                    bad, {"query": "bad", "level": "hybrid", "k": 1,
                          "filters": {}})
            except semworker.ResidentSemanticUnavailable:
                checks["auth"] = len(calls) == 2
            else:
                checks["auth"] = False
            def fail_value(*_args, **_kw):
                raise ValueError("internal numeric failure")
            server.search_fn = fail_value
            try:
                semworker._worker_request(
                    json.loads(server.record),
                    {"query": "internal", "level": "hybrid", "k": 1,
                     "filters": {}})
            except semworker.ResidentSemanticUnavailable:
                checks["internal_value_falls_back"] = True
            except ValueError:
                checks["internal_value_falls_back"] = False
            else:
                checks["internal_value_falls_back"] = False
            with resources.EMBEDDINGS_PATH.open("wb") as matrix_file:
                matrix_file.truncate(300 * 1024 ** 2)
            server.requests = 1
            one_off_lease = server._idle_limit()
            server.requests = 2
            repeated_lease = server._idle_limit()
            # 300 MiB matrix = the middle tier; still shorter than the small
            # tier's 600/900, which is the ordering this check defends.
            checks["large_matrix_short_lease"] = (
                one_off_lease == 300.0 and repeated_lease == 600.0)
            server.request_stop()
            thread.join(timeout=2)
            checks["cleanup"] = (
                not thread.is_alive() and not semworker.descriptor_path().exists())
    finally:
        (common.DATA_DIR, indexd_runtime.SEARCH_BEAT_PATH, resources.EMBEDDINGS_PATH,
         resources.available_memory_fraction,
         common.bind_descendants_to_process_lifetime) = saved

    ok = all(checks.values())
    return ("PASS" if ok else "FAIL", f"checks={checks} calls={calls}")


def t_semantic_idle_release():
    """A refs fallback can map artifacts without loading ONNX; idle reap must
    still close that mapping, while model release remains conditional."""
    import ask
    import embedder
    import semantic

    saved = (embedder.model_loaded, ask.clear_artifact_cache, embedder.release,
             semantic._LAST_USE["mono"])
    calls = []
    try:
        ask.clear_artifact_cache = lambda: calls.append("artifacts")
        embedder.release = lambda: calls.append("model")

        embedder.model_loaded = lambda: False
        semantic._LAST_USE["mono"] = time.monotonic() - 10
        artifacts_only = semantic.release_if_idle(1)
        first_calls = list(calls)

        calls.clear()
        embedder.model_loaded = lambda: True
        semantic._LAST_USE["mono"] = time.monotonic() - 10
        with_model = semantic.release_if_idle(1)
        second_calls = list(calls)
    finally:
        (embedder.model_loaded, ask.clear_artifact_cache, embedder.release,
         semantic._LAST_USE["mono"]) = saved

    ok = (artifacts_only and first_calls == ["artifacts"]
          and with_model and second_calls == ["artifacts", "model"])
    return ("PASS" if ok else "FAIL",
            f"artifacts_only={first_calls} with_model={second_calls}")


def t_semworker_claim_recovery():
    """Crash-partial launch/owner files and PID reuse cannot poison residency."""
    import json
    import tempfile
    from pathlib import Path
    import common
    import semworker

    saved = (common.DATA_DIR, common.pid_alive, common.process_start_identity)
    checks = {}
    try:
        with tempfile.TemporaryDirectory() as td:
            common.DATA_DIR = Path(td)
            _establish_current_derived_fixture(common.DATA_DIR)
            start = semworker.start_claim_path()
            start.write_text("{", encoding="utf-8")
            old = time.time() - semworker.START_TIMEOUT_S - 10
            os.utime(start, (old, old))
            claim = semworker._acquire_start_claim()
            checks["starting_reclaimed"] = claim is not None
            if claim is not None:
                semworker._release_start_claim(claim)
            checks["starting_released"] = not start.exists()

            lock = semworker.worker_lock_path()
            lock.write_text("{", encoding="utf-8")
            os.utime(lock, (old, old))
            owner = semworker._acquire_worker_lock()
            checks["lock_reclaimed"] = owner is not None
            if owner is not None:
                owner.release(tombstone=True, require_stable_mtime=True)

            descriptor = semworker.descriptor_path()
            descriptor.write_text(json.dumps({
                "version": semworker.PROTOCOL, "pid": os.getpid(),
                "port": 9, "token": "a" * 64, "process_start": "wrong",
            }), encoding="utf-8")
            checks["pid_reuse_refused"] = (
                semworker._reconcile_descriptor() is None)

            descriptor.write_text(json.dumps({
                "version": semworker.PROTOCOL, "pid": 4242,
                "port": 9, "token": "b" * 64, "process_start": "win_old",
            }), encoding="utf-8")
            common.pid_alive = lambda pid: pid == 4242
            common.process_start_identity = lambda _pid: None
            checks["unverifiable_owner_protected"] = (
                semworker._reconcile_descriptor() is None
                and descriptor.exists())
    finally:
        common.DATA_DIR, common.pid_alive, common.process_start_identity = saved

    ok = all(checks.values())
    return ("PASS" if ok else "FAIL", f"checks={checks}")


def t_recall_escalation():
    """Phrase abundance cannot suppress meaning; --lexical still opts out."""
    import contextlib
    import io
    import json as _json
    import recall
    import search
    import explore

    saved = (search.run_query, explore.get_windows, recall.indexd_runtime.ensure_index)
    calls = []

    def fake_run_query(q, *, mode="keyword", **kw):
        calls.append(mode)
        if mode == "semantic":
            return {"hits": [{"session": "semsemsem1", "agent": "codex",
                              "project": "p", "ts": 5, "turn": 5,
                              "sem_score": 0.9, "score_kind": "cosine",
                              "snippet": "meaning hit"}],
                    "total": 1, "chats": 1, "engine": "semantic:hybrid",
                    "mode": "semantic", "score_kind": "cosine"}
        if kw.get("who") == "tool":
            return {"hits": [], "total": 0, "chats": 0,
                    "engine": "corpusdb", "mode": "keyword"}
        hits = [{"session": f"keykeykey{i}", "agent": "codex", "project": "p",
                 "ts": 9 - i, "turn": i, "score": 4 - i / 10,
                 "matched": "phrase", "snippet": f"plausible phrase {i}"}
                for i in range(1, 4)]
        return {"hits": hits, "total": 3, "chats": 3,
                "engine": "corpusdb", "mode": "keyword"}

    def fake_windows(requests):
        return [{"session": sess, "center": turn, "agent": "codex", "project": "p",
                 "first_turn": turn, "last_turn": turn,
                 "turns": [{"turn": turn, "ts": 1, "who": "user",
                            "text": "t", "reply": "r"}],
                 "events": []} for sess, turn, _ in requests]

    try:
        search.run_query = fake_run_query
        explore.get_windows = fake_windows
        recall.indexd_runtime.ensure_index = lambda auto=True, **_kw: True

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = recall.main(["deployment retry loop",
                              "--json", "--budget", "0"])
        obj = _json.loads(out.getvalue())
        sem_rows = [h for h in obj["hits"] if h.get("lane") == "semantic"]
        key_rows = [h for h in obj["hits"] if h.get("lane") != "semantic"]
        hybrid_ok = (rc == 0 and obj.get("hybrid") is True
                        and "semantic" in obj.get("engine", "")
                        and sem_rows and key_rows
                        and obj["hits"].index(key_rows[0])
                        < obj["hits"].index(sem_rows[0])
                        and "semantic" in calls)

        calls.clear()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            recall.main(["deployment retry loop",
                         "--json", "--budget", "0", "--lexical"])
        lex_obj = _json.loads(out.getvalue())
        lexical_ok = ("semantic" not in calls
                      and not any(h.get("lane") == "semantic"
                                  for h in lex_obj["hits"]))
    finally:
        (search.run_query, explore.get_windows, recall.indexd_runtime.ensure_index) = saved

    ok = hybrid_ok and lexical_ok
    return ("PASS" if ok else "FAIL",
            f"hybrid={hybrid_ok} lexical_opt_out={lexical_ok}")


def t_dense_probe_gate():
    """Dense probes fire above the calibrated cosine floor, labeled as semantic
    matches; junk below the floor stays silent on probes and normal search."""
    import contextlib
    import io
    import compact
    import recall
    import search
    import semworker

    base = {"session": "abcdef123", "agent": "codex", "project": "p",
            "turn": 3, "ts": 1,
            "content_digest": compact.content_digest("useful")}
    strong = {**base, "sem_score": recall.PROBE_MIN_SEM + 0.01, "score_kind": "cosine"}
    weak = {**base, "sem_score": recall.PROBE_MIN_SEM - 0.05, "score_kind": "cosine"}
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        strong_rc = recall._probe(["q"], [strong], "semantic:hybrid")
    strong_text = out.getvalue()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        weak_rc = recall._probe(["q"], [weak], "semantic:hybrid")
    weak_text = out.getvalue()
    saved_worker = semworker.search_worker
    try:
        semworker.search_worker = lambda *args, **kwargs: {
            "results": [
                {**strong, "score": strong["sem_score"], "text": "useful"},
                {**weak, "session": "weak0000", "score": weak["sem_score"],
                 "text": "nearest but unrelated"},
            ],
            "candidate_sessions": 2,
            "score_kind": "cosine",
        }
        direct = search._semantic_local("q", 10, family_diverse=False)
    finally:
        semworker.search_worker = saved_worker
    import display_policy
    # sub-floor evidence never fires a pointer; the miss is disclosed, not silent
    weak_miss = weak_text.strip() == display_policy.probe_miss_line(
        "semantic:hybrid",
        corpus_sessions=(recall.common.index_summary() or {}).get("sessions"))
    ok = (strong_rc == 0 and "semantic" in strong_text and "recall:" in strong_text
          and weak_rc == 1 and weak_miss
          and [hit["session"] for hit in direct["hits"]] == [base["session"]]
          and direct.get("truncated") is False)
    return ("PASS" if ok else "FAIL",
            f"strong_rc={strong_rc} labeled={'semantic' in strong_text} "
            f"weak_rc={weak_rc} weak_miss={weak_miss} "
            f"direct={[hit['session'] for hit in direct['hits']]}")


def t_windows_hook_argv():
    if sys.platform != "win32":
        return ("SKIP", "CommandLineToArgvW is Windows-only")
    import indexer

    command = ('"C:\\Program Files\\Python\\python.exe" '
               '"C:\\Users\\Example User\\Desktop\\sample-app\\tool\\cli.py" enrich --auto')
    got = indexer.split_user_command(command)
    want = [r"C:\Program Files\Python\python.exe",
            r"C:\Users\Example User\Desktop\sample-app\tool\cli.py", "enrich", "--auto"]
    return ("PASS" if got == want else "FAIL", f"argv={got!r}")


def t_managed_hook_registry():
    import indexer

    saved = indexer.common.setting
    values = {
        "post_index": "off",
        "post_index_hooks": {
            "zeta": {"argv": ["python", "z.py"], "timeout_s": 9999},
            "alpha": {"argv": ["python", "a.py"], "timeout_s": "12"},
            "broken": {"argv": "not-a-list"},
        },
    }
    indexer.common.setting = lambda key, default=None: values.get(key, default)
    try:
        got = indexer.configured_post_index_hooks()
    finally:
        indexer.common.setting = saved
    ok = got == [("alpha", ["python", "a.py"], 12),
                 ("zeta", ["python", "z.py"], indexer.HOOK_TIMEOUT_S)]
    return ("PASS" if ok else "FAIL", f"hooks={got!r}")


# 8d. thrash guard — every foreground reader serves a published snapshot without
# rebuilding; daemon/index and true-first-run paths own all derived refreshes.
def t_stale_serve():
    import contextlib
    import corpusdb
    if not corpusdb.DB_PATH.exists():
        return ("SKIP", "no corpus db")
    calls = {"inc": 0, "build": 0}
    tick = [0]

    def moving_stamp():  # every look says "source moved", like live agent writes
        tick[0] += 1
        return f"[[{tick[0]}, 1]]"

    def fake_inc(stamp):
        calls["inc"] += 1
        return corpusdb._open(corpusdb.DB_PATH)

    saved = (corpusdb._stamp, corpusdb.indexd_runtime.freshener_alive,
             corpusdb._live_refresh_lock, corpusdb._incremental,
             corpusdb._build, corpusdb.common.IndexLock)
    corpusdb._stamp = moving_stamp
    corpusdb._incremental = fake_inc
    corpusdb._build = lambda tmp: calls.__setitem__("build", calls["build"] + 1)
    corpusdb.common.IndexLock = (
        lambda name, **_kwargs: contextlib.nullcontext())  # never block the suite
    corpusdb._live_refresh_lock = lambda: False
    # routing test with refreshes mocked to counters: the gate-wide
    # data-dir write guard would mask the branch under test
    readonly_guard = os.environ.pop("AGREP_DATA_READONLY", None)
    try:
        corpusdb.indexd_runtime.freshener_alive = lambda: True
        t0 = time.time()
        stale_db = direct_scan = 0
        for _ in range(20):
            db = corpusdb.connect(quiet=True, allow_stale=True)
            if db is None:
                direct_scan += 1
            else:
                stale_db += 1
                db.close()
        avg_ms = (time.time() - t0) * 1000 / 20
        clean = calls["inc"] == 0 and calls["build"] == 0
        corpusdb.indexd_runtime.freshener_alive = lambda: False
        db = corpusdb.connect(quiet=True, allow_stale=True)
        alone_readonly = calls["inc"] == 0 and calls["build"] == 0
        if db:
            db.close()
        ok = clean and alone_readonly and avg_ms < 200
        return ("PASS" if ok else "FAIL",
                f"20 snapshot-serves db/direct={stale_db}/{direct_scan} "
                f"rebuilds=0:{clean} alone-readonly:{alone_readonly} "
                f"avg={avg_ms:.0f}ms")
    finally:
        if readonly_guard is not None:
            os.environ["AGREP_DATA_READONLY"] = readonly_guard
        (corpusdb._stamp, corpusdb.indexd_runtime.freshener_alive,
         corpusdb._live_refresh_lock, corpusdb._incremental,
         corpusdb._build, corpusdb.common.IndexLock) = saved


# A live rollback-journal writer must send interactive readers to fallback immediately.
def t_stale_writer_nonblocking():
    import sqlite3
    import tempfile
    from pathlib import Path
    import corpusdb

    calls = {"lock": 0}

    def forbidden_lock(*_args, **_kwargs):
        calls["lock"] += 1
        raise AssertionError("interactive stale read entered IndexLock")

    saved = (corpusdb.DB_PATH, corpusdb.common.MESSAGES_PATH, corpusdb._stamp,
             corpusdb._trigram_ok, corpusdb._live_refresh_lock,
             corpusdb.indexd_runtime.freshener_alive,
             corpusdb.common.IndexLock, corpusdb._derived_write_ownership)
    got = None
    committed = None
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        path = root / "corpus.db"
        messages = root / "messages.jsonl"
        messages.write_text("{}\n", encoding="utf-8")
        build_id = corpusdb.indexd_runtime.derived_writer_build_id(
            require_binary=True)
        seed = sqlite3.connect(path)
        seed.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
        seed.executemany("INSERT INTO meta VALUES(?, ?)",
                         [("schema", corpusdb._SCHEMA), ("stamp", "old"),
                          ("build_id", build_id)])
        seed.commit(); seed.close()
        writer = sqlite3.connect(path, timeout=0)
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE meta SET value='pending' WHERE key='stamp'")
        try:
            corpusdb.DB_PATH = path
            corpusdb.common.MESSAGES_PATH = messages
            corpusdb._stamp = lambda: "new"
            corpusdb._trigram_ok = lambda: True
            corpusdb._live_refresh_lock = lambda: False
            corpusdb.indexd_runtime.freshener_alive = lambda: True
            corpusdb.common.IndexLock = forbidden_lock
            corpusdb._derived_write_ownership = (
                lambda **_kwargs: corpusdb._DerivedWriteOwnership("current"))
            started = time.perf_counter()
            got = corpusdb.connect(quiet=True, allow_stale=True)
            elapsed_ms = (time.perf_counter() - started) * 1000
            if got is not None:
                committed = got.execute(
                    "SELECT value FROM meta WHERE key='stamp'").fetchone()
        finally:
            if got is not None:
                got.close()
            writer.rollback(); writer.close()
            (corpusdb.DB_PATH, corpusdb.common.MESSAGES_PATH, corpusdb._stamp,
             corpusdb._trigram_ok, corpusdb._live_refresh_lock,
             corpusdb.indexd_runtime.freshener_alive,
             corpusdb.common.IndexLock,
             corpusdb._derived_write_ownership) = saved
    ok = committed == ("old",) and calls["lock"] == 0 and elapsed_ms < 500
    return ("PASS" if ok else "FAIL",
            f"committed={committed == ('old',)} index-lock-calls={calls['lock']} "
            f"elapsed={elapsed_ms:.1f}ms")


# 8e. bag-of-words fallback: terms superset phrase, corpusdb/JSONL parity, trigger gating
def t_terms_fallback():
    import sqlite3
    import tempfile
    from pathlib import Path
    import corpusdb
    import explore
    import search

    q = "race condition"
    key = lambda h: (h["session"], h["turn"], h["who"])  # noqa: E731
    rows = [
        ("phrase", 1, 100, "codex", "p", "", "", "", "user",
         "the race condition is reproducible"),
        ("spread", 2, 200, "codex", "p", "", "", "", "agent",
         "race remains reproducible after a long retry; condition confirmed"),
        ("reverse", 3, 300, "claude", "p", "", "", "", "user",
         "condition appears only after the race"),
        ("noise", 4, 400, "claude", "p", "", "", "", "user",
         "race without the other token"),
    ]
    scan_rows = [{
        "session": row[0], "turn": row[1], "ts": row[2],
        "agent": row[3], "project": row[4], "concept": row[5],
        "model": row[6], "model_source": row[7], "who": row[8],
        "text": row[9], "low": row[9].lower(),
    } for row in rows]
    saved_connect = corpusdb.connect
    saved_iter = explore._iter_kw_corpus
    phrase, terms, jsonl = set(), set(), set()
    merged = {}
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "corpus.db"
        db = sqlite3.connect(path)
        db.executescript(corpusdb._SCHEMA_SQL)
        db.executemany(corpusdb._INS, rows)
        db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
        db.execute("INSERT INTO msgs_prose_fts(rowid,text) "
                   "SELECT id,text FROM msgs WHERE who <> 'tool'")
        db.commit()
        try:
            phrase = {key(h) for h in corpusdb.keyword(
                db, q, 10_000_000)["hits"]}
            terms = {key(h) for h in corpusdb.terms(
                db, q, 10_000_000)["hits"]}
            explore._iter_kw_corpus = lambda _flt=None: iter(scan_rows)
            jsonl = {key(h) for h in search._terms_scan(
                q, 10_000_000)["hits"]}
            corpusdb.connect = lambda **_kw: sqlite3.connect(path)
            merged = search.run_query(q, mode="keyword", limit=10,
                                      allow_fallback=False)
        finally:
            db.close()
            corpusdb.connect = saved_connect
            explore._iter_kw_corpus = saved_iter

    superset = phrase <= terms
    parity = terms == jsonl
    gate = (search._want_terms_fallback("keyword", "a b") and
            not search._want_terms_fallback("keyword", "solo") and
            not search._want_terms_fallback("regex", "a b"))

    now = int(time.time() * 1000)
    mk = lambda snip, matched, ts: {"session": "s", "turn": 1, "ts": ts, "who": "user",  # noqa: E731
                                    "agent": "a", "project": "p", "snippet": snip,
                                    **({"matched": matched} if matched else {})}
    mixed = [mk("brand new but only scattered terms", "all-terms", now),
             mk("race condition", None, now - 90 * 86_400_000)]  # old exact phrase
    order = search._rank(mixed, "race condition", "keyword", "score")
    phrase_first = order[0].get("matched") != "all-terms"

    merged_by_session = {hit["session"]: hit for hit in merged.get("hits", [])}
    fired = bool(merged.get("terms_augmented"))
    tagged = (
        merged_by_session.get("phrase", {}).get("matched") is None
        and all(merged_by_session.get(session, {}).get("matched") == "all-terms"
                for session in ("spread", "reverse"))
    )

    ok = superset and parity and gate and phrase_first and fired and tagged
    return ("PASS" if ok else "FAIL",
            f"phrase⊆terms={superset} ({len(phrase)}⊆{len(terms)}), corpusdb==JSONL={parity}, "
            f"gating={gate}, phrase-first={phrase_first}, fired={fired} tagged={tagged}")


# 8e2. query-echo: phrase copies planted by past searches must never shadow scattered hits
def t_query_echo():
    """Searching a phrase writes adjacent copies of it into your own transcripts (tool
    rows quoting the command). Those echoes used to cross the old thin-phrase gate and
    permanently disable the terms lane, hiding the scattered row being hunted - the
    'akd URL-bag deadlock' miss. The terms lane is always-on now; prove the target
    surfaces behind any number of echoes, on both the exhaustive and bounded paths."""
    import sqlite3
    import corpusdb
    import search
    now_ms = int(time.time() * 1000)
    db = sqlite3.connect(":memory:")
    db.executescript(corpusdb._SCHEMA_SQL)
    rows = [(f"echo-{i:02}", 0, now_ms, "claude", "p", "", "", "", "tool",
             f'Bash: agrep "akd urlbag deadlock" --json (attempt {i})')
            for i in range(6)]  # 6 adjacent echoes > the old gate of 3
    rows.append(("target", 7, now_ms - 86_400_000, "claude", "p", "", "", "", "agent",
                 "all 64 threads of akd parked in AKURLBag urlBagFromCache waiting "
                 "on a semaphore - a clean deadlock, no crash"))
    # Two tool rows may legitimately share every metadata field. The phrase/terms
    # merge must identify the actual DB row, not copy the phrase snippet onto both.
    rows.extend([
        ("dupe", 9, now_ms - 1, "claude", "p", "", "", "", "tool",
         "akd appears here while urlbag waits elsewhere before deadlock"),
        ("dupe", 9, now_ms - 1, "claude", "p", "", "", "", "tool",
         "akd urlbag deadlock exact phrase"),
    ])
    db.executemany(corpusdb._INS, rows)
    db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
    db.execute("INSERT INTO msgs_prose_fts(rowid, text) "
               "SELECT id, text FROM msgs WHERE who <> 'tool'")
    db.commit()
    q = "akd urlbag deadlock"

    both = corpusdb.keyword_terms(db, q, 10_000_000, None)
    hits = search._augment_phrase_hits(both["phrase"]["hits"], both["terms"]["hits"])
    hits = search._rank(hits, q, "keyword", "score")
    sessions = [h["session"] for h in hits]
    exhaustive_found = "target" in sessions
    target_tagged = all(h.get("matched") == "all-terms"
                        for h in hits if h["session"] == "target")
    duplicate_hits = [h for h in hits if h["session"] == "dupe"]
    duplicate_exact = (
        len(duplicate_hits) == 2
        and sum(h.get("matched") == "all-terms" for h in duplicate_hits) == 1
        and len({h["snippet"] for h in duplicate_hits}) == 2)

    saved_threshold = search._BOUNDED_KEYWORD_MIN_CANDIDATES
    try:
        search._BOUNDED_KEYWORD_MIN_CANDIDATES = 0
        bounded = search._bounded_keyword_sessions(db, q, 10, {}, True)
    finally:
        search._BOUNDED_KEYWORD_MIN_CANDIDATES = saved_threshold
    bounded_found = bounded is not None and any(
        h["session"] == "target" for h in bounded["hits"])
    db.close()
    ok = exhaustive_found and target_tagged and duplicate_exact and bounded_found
    return ("PASS" if ok else "FAIL",
            f"exhaustive={exhaustive_found} tagged={target_tagged} "
            f"duplicate-identity={duplicate_exact} "
            f"bounded={bounded_found} (6 echoes vs old gate of 3)")


# 8f. bounded recall: exact heads, explicit aggregate precision, safe fallback
def t_bounded_keyword_heads():
    """Bounded recall must match exhaustive top sessions, including thin phrases."""
    import random
    import sqlite3
    import corpusdb
    import search

    rng = random.Random(7401)
    vocab = ("alpha beta gamma delta error index lock semantic search recall memory "
             "windows python thread model worker fuzzy candidate history cleanup").split()
    separators = (" ", "-", "_", " / ", "...", "::")
    now_s = 2_000_000_000.0
    now_ms = int(now_s * 1000)
    db = sqlite3.connect(":memory:")
    db.executescript(corpusdb._SCHEMA_SQL)
    rows = [
        ("exact-old", 0, now_ms - 80 * 86_400_000, "codex", "p", "", "", "",
         "user", "unique adjacent alpha beta"),
        ("terms-new", 0, now_ms, "codex", "p", "", "", "",
         "user", "unique alpha scattered far from beta adjacent"),
    ]
    for session_i in range(28):
        session = f"fixture-{session_i:03}"
        for turn in range(rng.randint(4, 16)):
            who = rng.choices(
                ("user", "agent", "tool", "recap", "control"),
                (35, 30, 25, 5, 5))[0]
            words = rng.choices(vocab, k=rng.randint(5, 24))
            if rng.random() < 0.30:
                left, right = rng.sample(vocab, 2)
                at = rng.randrange(len(words))
                words[at:at + 2] = [left + rng.choice(separators) + right]
            rows.append((session, turn,
                         now_ms - rng.randrange(0, 120) * 86_400_000,
                         "codex", "p", "", "", "", who, " ".join(words)))
    db.executemany(corpusdb._INS, rows)
    db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
    db.execute("INSERT INTO msgs_prose_fts(rowid, text) "
               "SELECT id, text FROM msgs WHERE who <> 'tool'")
    db.commit()

    def exhaustive(query, limit, flt):
        if search._want_terms_fallback("keyword", query):
            both = corpusdb.keyword_terms(
                db, query, 10_000_000, flt, position_order=False)
            phrase_hits = both["phrase"]["hits"]
            hits = search._augment_phrase_hits(phrase_hits, both["terms"]["hits"])
            if not phrase_hits:
                for hit in hits:
                    hit["matched"] = "all-terms"
        else:
            hits = corpusdb.keyword(
                db, query, 10_000_000, flt, position_order=False)["hits"]
        search._rank(hits, query, "keyword", "score")
        counts = (len(hits), len({h["session"] for h in hits}),
                  sum(1 for h in hits if h.get("who") == "tool"))
        return search._session_heads(hits, limit), counts

    def shape(hits):
        return [(hit["session"], hit.get("turn"), hit.get("who"),
                 hit.get("matched"), hit.get("score")) for hit in hits]

    saved_threshold = search._BOUNDED_KEYWORD_MIN_CANDIDATES
    real_time = search.time.time
    checks = 0
    pruned = exhausted = math_independent = False
    session_counts = True
    try:
        search._BOUNDED_KEYWORD_MIN_CANDIDATES = 0
        search.time.time = lambda: now_s
        queries = ["alpha", "unique adjacent", "alpha beta", "semantic search",
                   "fuzzy candidate", "history cleanup"]
        queries.extend(" ".join(rng.sample(vocab, rng.choice((1, 2, 3))))
                       for _ in range(12))
        filters = (None, {"include_tools": False}, {"who": "tool"}, {"who": "user"})
        for query in queries:
            for flt in filters:
                for limit in (1, 3, 8):
                    bounded = search._bounded_keyword_sessions(
                        db, query, limit, flt or {}, True)
                    if bounded is None:
                        continue
                    expected, exact_counts = exhaustive(query, limit, flt)
                    if shape(bounded["hits"]) != shape(expected):
                        return ("FAIL", f"parity mismatch q={query!r} filter={flt} "
                                f"limit={limit}: {shape(bounded['hits'])} != {shape(expected)}")
                    counts = (bounded["total"], bounded["chats"],
                              bounded["tool_hits"])
                    if bounded["totals_exact"]:
                        if counts != exact_counts:
                            return ("FAIL", f"exact totals mismatch q={query!r} "
                                    f"filter={flt} limit={limit}: "
                                    f"{counts} != {exact_counts}")
                    elif any(got > want for got, want in zip(counts, exact_counts)):
                        return ("FAIL", f"pruned totals overshoot q={query!r} "
                                f"filter={flt} limit={limit}: "
                                f"{counts} > {exact_counts}")
                    checks += 1
                    pruned |= not bounded["totals_exact"]
                    exhausted |= bounded["totals_exact"]
                    expected_sessions = {
                        hit["session"]: hit.get("session_hits") for hit in expected}
                    session_counts &= all(
                        hit.get("session_hits") == expected_sessions.get(hit["session"])
                        for hit in bounded["hits"])

        # Score ceilings are computed portably in Python; SQLite math availability
        # must not decide whether bounded ranking exists.
        def broken_pow(_base, _power):
            raise ValueError("fixture: no SQL math")
        db.create_function("pow", 2, broken_pow)
        math_independent = search._bounded_keyword_sessions(
            db, "alpha", 3, {"include_tools": False}, True) is not None
    finally:
        search._BOUNDED_KEYWORD_MIN_CANDIDATES = saved_threshold
        search.time.time = real_time
        db.close()
    ok = checks >= 100 and pruned and exhausted and session_counts and math_independent
    return ("PASS" if ok else "FAIL",
            f"parity={checks} pruned={pruned} exhausted={exhausted} "
            f"session-counts={session_counts} math-independent={math_independent}")


def t_bounded_keyword_rows():
    """Broad top-row optimization must preserve exhaustive rank and exact totals."""
    import sqlite3
    import corpusdb
    import search

    if not corpusdb._trigram_ok():
        return ("SKIP", "sqlite build lacks FTS5 trigram")
    now_s = 2_000_000_000.0
    now_ms = int(now_s * 1000)
    speakers = ("user", "agent", "tool", "recap", "control", "subagent")
    rows = []
    for i in range(360):
        speaker = speakers[i % len(speakers)]
        repeats = 1 + (i % 5)
        text = (("the " * repeats) + ("alpha " if i % 3 else "")
                + f"fixture evidence row {i}")
        rows.append((f"s{i % 47:03}", i, now_ms - i * 3_600_000,
                     "codex", "p", "", "", "", speaker, text))
    # FTS5's Unicode fold and Python's lower() are not identical. Kelvin signs fold
    # to ASCII k in both (a real match), while long-s folds to s only in FTS (a
    # candidate false positive). Pin both sides of the exact aggregate shortcut.
    rows.extend([
        ("unicode-k", 900, now_ms, "codex", "p", "", "", "", "user",
         "KKK Unicode compatibility-fold evidence"),
        ("ascii-k", 901, now_ms - 1, "codex", "p", "", "", "", "agent",
         "kkk ASCII evidence"),
        ("unicode-s", 902, now_ms - 2, "codex", "p", "", "", "", "user",
         "ſſſ FTS-only fold candidate"),
        ("ascii-s", 903, now_ms - 3, "codex", "p", "", "", "", "agent",
         "sss ASCII evidence"),
        ("unicode-i", 905, now_ms + 3, "codex", "p", "", "", "", "user",
         "xxİ Python-only dotted-I match"),
        ("ascii-i", 906, now_ms - 4, "codex", "p", "", "", "", "agent",
         "ordinary xxi ASCII evidence"),
        ("", 904, now_ms + 1, "codex", "p", "", "", "", None,
         "the empty-session NULL-speaker evidence"),
    ])
    db = sqlite3.connect(":memory:")
    db.executescript(corpusdb._SCHEMA_SQL)
    db.executemany(corpusdb._INS, rows)
    db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
    db.execute("INSERT INTO msgs_prose_fts(rowid, text) "
               "SELECT id, text FROM msgs WHERE who <> 'tool'")
    db.executescript(corpusdb._TRIGGERS_SQL)
    db.execute("INSERT INTO boundary_stats(token, n, s, q) VALUES('xxi', 20, 2, 2)")

    saved_gate = search._BOUNDED_ROW_MIN_CANDIDATES
    real_time = search.time.time
    checks = 0
    filtered_fallback = missing_index_fallback = False
    try:
        search._BOUNDED_ROW_MIN_CANDIDATES = 0
        search.time.time = lambda: now_s

        def shape(hits):
            return [(h["session"], h["turn"], h["who"], h["score"], h["snippet"])
                    for h in hits]

        for query in ("the", "alpha", "kkk", "sss", "xxi"):
            for flt in ({}, {"include_tools": False}):
                full = corpusdb.keyword(
                    db, query, 10_000_000, flt, position_order=False)
                boundary = search._prepare_boundary(query, "keyword", db)
                search._rank(full["hits"], query, "keyword", "score", boundary=boundary)
                expected_stats = (
                    full["total"], len({h["session"] for h in full["hits"]}),
                    sum(h["who"] == "tool" for h in full["hits"]))
                for limit in (1, 3, 12):
                    fast = search._bounded_single_keyword_rows(
                        db, query, limit, flt)
                    if fast is None:
                        return ("FAIL", f"fast lane unavailable q={query!r} flt={flt}")
                    got_stats = fast["total"], fast["chats"], fast["tool_hits"]
                    if (shape(fast["hits"]) != shape(full["hits"][:limit])
                            or got_stats != expected_stats):
                        return ("FAIL", f"parity q={query!r} flt={flt} limit={limit}: "
                                f"stats={got_stats}/{expected_stats}")
                    checks += 1
        filtered_fallback = search._bounded_single_keyword_rows(
            db, "the", 3, {"who": "user"}) is None
        db.execute("DROP INDEX msgs_who_ts")
        missing_index_fallback = search._bounded_single_keyword_rows(
            db, "the", 3, {}) is None
    finally:
        search._BOUNDED_ROW_MIN_CANDIDATES = saved_gate
        search.time.time = real_time
        db.close()
    ok = checks == 30 and filtered_fallback and missing_index_fallback
    return ("PASS" if ok else "FAIL",
            f"parity={checks} filtered-fallback={filtered_fallback} "
            f"missing-index-fallback={missing_index_fallback}")


def t_recall_full_prose_skips_tools():
    """A structurally full prose lane makes every tool candidate ineligible."""
    import contextlib
    import io
    import recall

    calls = []
    saved = (recall.indexd_runtime.ensure_index, recall.search.run_query, recall._windows)

    def fake_query(_query, **kwargs):
        calls.append(dict(kwargs))
        rows = [] if kwargs.get("who") == "tool" else [
            {"session": f"s{i}", "turn": 0, "ts": 100 - i, "who": "user",
             "agent": "codex", "project": "p", "score": 0.9 - i / 10,
             "snippet": "needle"} for i in range(3)]
        return {"hits": rows, "total": len(rows), "chats": len(rows),
                "engine": "corpusdb", "mode": "keyword", "tool_hits": 0,
                "returned_chats": len(rows), "totals_exact": False}

    try:
        recall.indexd_runtime.ensure_index = lambda auto=True, **_kw: True
        recall.search.run_query = fake_query
        recall._windows = lambda hits, context: []
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = recall.main([
                "needle", "--hits", "3", "--budget", "2048", "--json"])
    finally:
        recall.indexd_runtime.ensure_index, recall.search.run_query, recall._windows = saved
    tool_calls = sum(call.get("who") == "tool" for call in calls)
    opted_in = bool(calls) and calls[0].get("exact_totals") is False
    # The bounded fixture explicitly reports totals_exact=False, so an empty
    # window is an unverified absence (rc 2), not proven no match (rc 1).
    ok = rc == 2 and len(calls) == 1 and tool_calls == 0 and opted_in
    return ("PASS" if ok else "FAIL",
            f"calls={len(calls)} tool-calls={tool_calls} bounded-opt-in={opted_in} rc={rc}")


def _isolated_cli_smoke_env(root) -> dict[str, str]:
    from pathlib import Path
    from _test_support import publish_derived_generation
    import common
    import corpusdb

    data = Path(root)
    row = {
        "id": "codex:selftest-session:0", "agent": "codex",
        "project": "selftest", "session": "selftest-session",
        "ts": 1, "turn": 0, "text": "fixture needle",
        "who": "user", "model": "fixture",
    }
    publish_derived_generation(
        data, [row], common, corpusdb, signature="selftest-cli-generation",
        replies=[{"id": row["id"], "reply": "fixture answer"}],
    )
    return {
        **ENV,
        "AGREP_DATA_DIR": str(data),
        "AGREP_DATA_READONLY": str(data),
        "AGREP_HOME": str(data / "home"),
        "AGREP_NO_DAEMON": "1",
    }


# Both the run subcommand and default search dispatch must remain reachable.
def t_cli():
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        env = _isolated_cli_smoke_env(td)
        r1 = subprocess.run([sys.executable, "cli.py", "run", "--help"],
                            cwd=REPO, env=env, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=30)
        r2 = subprocess.run([
            sys.executable, "cli.py", "search", "fixture", "--color", "never",
            "-n", "1", "--no-auto", "--self"], cwd=REPO, env=env,
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60)
    ok = ("agrep run" in r1.stdout) and (r2.returncode in (0, 1))
    return ("PASS" if ok else "FAIL", f"run-help={'agrep run' in r1.stdout}, search-rc={r2.returncode}")


# Status and doctor JSON each emit exactly one object on stdout.
# Doctor failure states need direct fixtures because ordinary runs rarely reach them.
def t_doctor_states():
    import importlib.util
    import json as _json
    import tempfile
    import time as _time
    from pathlib import Path
    # doctor legitimately kicks a background repair over this fixture; on
    # Windows that child can briefly hold a file open while the context
    # manager deletes, and deleting an open file is PermissionError there.
    with tempfile.TemporaryDirectory(
            ignore_cleanup_errors=(os.name == "nt")) as td:
        fix = Path(td)
        (fix / "sessions.jsonl").write_text(
            '{"session":"s1","agent":"claude","n":3,"last_ts":1784400000000}\n',
            encoding="utf-8")
        (fix / "messages.jsonl").write_text(
            '{"id":"a","agent":"claude","project":"p","session":"s1",'
            '"ts":1784400000000,"turn":1,"text":"hello","who":"user",'
            '"model":"m","model_source":"explicit"}\n', encoding="utf-8")
        (fix / "auto-index-health.json").write_text(_json.dumps(
            {"streak": 3, "last_err": "ingest exited 101: permission denied",
             "ts": _time.time()}), encoding="utf-8")
        (fix / "teach.json").write_text("not json {{{", encoding="utf-8")
        (fix / ".semantic-embed-state.json").write_text(_json.dumps(
            {"state": "failed", "finished_at": _time.time() - 60,
             "reason": "onnx session init failed", "pid": 1}), encoding="utf-8")
        env = {**ENV, "AGREP_DATA_DIR": str(fix)}
        r = subprocess.run([sys.executable, "cli.py", "doctor"], cwd=REPO,
                           env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=90)
        want = ["daemon down after 3+ consecutive failures",
                "permission denied", "state unreadable"]
        if importlib.util.find_spec("onnxruntime") is not None:
            want += ["last build failed: onnx session init failed",
                     "retries automatically"]
        missing = [w for w in want if w not in r.stdout]
        r2 = subprocess.run([sys.executable, "cli.py", "doctor"], cwd=REPO,
                            env={**env, "AGREP_RS_BIN": "/nonexistent/agrep-rs"},
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=90)
        import shutil as _shutil
        # doctor renders the cargo row for the host it is on: a box with
        # cargo says "can build from source"; a box without says where to
        # get it. Assert the host-appropriate string, not the CI one.
        cargo_tail = ("can build from source" if _shutil.which("cargo")
                      else "install from https://rustup.rs")
        for w in ("not built", cargo_tail):
            if w not in r2.stdout:
                missing.append(w)
    ok = not missing and r.returncode == 0 and r2.returncode == 0
    return ("PASS" if ok else "FAIL",
            f"missing={missing!r} rc={r.returncode}/{r2.returncode}")


def t_json_surface():
    import json as _json
    outs = {}
    for verb in ("status", "doctor"):
        r = subprocess.run([sys.executable, "cli.py", verb, "--json"],
                           cwd=REPO, env=ENV, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=90)
        try:
            outs[verb] = _json.loads(r.stdout)
        except Exception as e:  # noqa: BLE001
            return ("FAIL", f"{verb} --json not one object: {e!r}")
    ok = ("index_built" in outs["status"]) and ("tiers" in outs["doctor"])
    return ("PASS" if ok else "FAIL",
            f"status keys={sorted(outs['status'])[:3]}, doctor tiers={outs['doctor'].get('tiers')}")


# Recall JSON emits one object; --no-auto makes a nonsense miss unverified.
def t_recall():
    import json as _json
    import tempfile

    term = "fixture"
    with tempfile.TemporaryDirectory() as td:
        env = _isolated_cli_smoke_env(td)
        r = subprocess.run([
            sys.executable, "cli.py", "recall", term, "--json",
            "--budget", "4000", "--hits", "2", "--no-auto"],
            cwd=REPO, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=90)
        # A generated miss cannot become history merely because this test is discussed.
        import uuid
        nonsense = f"zzqx{uuid.uuid4().hex[:12]}"
        r2 = subprocess.run([
            sys.executable, "cli.py", "recall", nonsense, "--json", "--no-auto"],
            cwd=REPO, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=90)
    try:
        o = _json.loads(r.stdout)
    except Exception as e:  # noqa: BLE001
        return ("FAIL", f"recall --json not one object: {e!r}")
    shape = ("query" in o and "engine" in o and isinstance(o.get("hits"), list))
    windowed = all("window" in h and isinstance(h["window"], list) for h in o["hits"])
    ok = shape and windowed and r2.returncode == 2
    return ("PASS" if ok else "FAIL",
            f"term={term!r} hits={len(o['hits'])} windowed={windowed} nonsense-rc={r2.returncode}")


# Dead lock holders are reclaimed while live holders remain owned.
def t_lock_liveness():
    import common
    import index_lock
    import tempfile
    from pathlib import Path as P
    # a guaranteed-dead pid: spawn a short-lived child, use its pid after it exits
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_pid = proc.pid
    proc.wait()
    time.sleep(0.2)  # let the OS tear the process down
    if common.pid_alive(dead_pid):
        return ("SKIP", f"pid {dead_pid} reused before check (rare) - can't assert dead")
    if not common.pid_alive(os.getpid()):
        return ("FAIL", "own pid reads dead")

    # point IndexLock at a scratch path so this never fights (or disturbs) the real
    # .index.lock a live daemon/search may be holding right now.
    real = common.INDEX_LOCK_PATH
    real_owner = index_lock.INDEX_LOCK_PATH
    with tempfile.TemporaryDirectory() as td:
        lock = P(td) / ".index.lock"
        common.INDEX_LOCK_PATH = lock
        index_lock.INDEX_LOCK_PATH = lock
        try:
            # dead holder -> acquire must be immediate (no waiting out stale_after)
            lock.write_text(f"pid={dead_pid} label=stale time={time.time():.3f}\n",
                            encoding="utf-8")
            t0 = time.time()
            try:
                with common.IndexLock("selftest", timeout=5.0):
                    reclaim_ms = (time.time() - t0) * 1000
            except TimeoutError:
                return ("FAIL", "timed out reclaiming a dead-holder lock")

            # A live owner cannot be stolen solely because the lock is old.
            lock.write_text(f"pid={os.getpid()} label=live time={time.time():.3f}\n",
                            encoding="utf-8")
            old = time.time() - 10 * 3600
            os.utime(lock, (old, old))
            stole = False
            try:
                with common.IndexLock("selftest", timeout=0.5):
                    stole = True
            except TimeoutError:
                pass

            # Pidless legacy locks retain the age-based reclaim fallback.
            lock.write_text("label=ancient no pid\n", encoding="utf-8")
            os.utime(lock, (old, old))
            pidless_stolen = False
            try:
                with common.IndexLock("selftest", timeout=0.5):
                    pidless_stolen = True
            except TimeoutError:
                pass
        finally:
            common.INDEX_LOCK_PATH = real
            index_lock.INDEX_LOCK_PATH = real_owner
    ok = reclaim_ms < 1000 and not stole and pidless_stolen
    return ("PASS" if ok else "FAIL",
            f"dead-reclaim={reclaim_ms:.0f}ms, live-stolen={stole} (want False at 10h>6h), "
            f"pidless-time-stolen={pidless_stolen} (want True)")


# Self-tests require the staged Rust binary.
def t_binary():
    if not os.path.exists(RS):
        return ("FAIL", f"no Rust binary at {RS}")
    return ("PASS", f"binary present ({os.path.getsize(RS)} bytes)")


# BUG (semantic crash) — ask._lines must split JSONL on real newlines only. str.
# splitlines() also breaks on U+2028 (present in real chat text), which split a row
# mid-JSON-string -> JSONDecodeError and took message-level semantic search down.
def t_u2028():
    import json
    import ask
    row = json.dumps({"id": "a:b:0", "text": "before after"}, ensure_ascii=False)
    lines = ask._lines(row + "\n" + json.dumps({"id": "a:b:1", "text": "next"}))
    splits_clean = len(lines) == 2 and json.loads(lines[0])["text"] == "before after"
    return ("PASS" if splits_clean else "FAIL", f"U+2028 row parses whole={splits_clean}")


def t_ask_private_entrypoint():
    """The unsupported direct CLI cannot bypass freshness and model ownership."""
    import ask

    rc = ask.main()
    return ("PASS" if rc == 2 else "FAIL", f"rc={rc}")


def t_semantic_candidate_refs():
    """Semantic metadata stays ordinal-exact without retaining transcript text."""
    import contextlib
    import hashlib
    import json
    import random
    import sqlite3
    import tempfile
    from pathlib import Path as P
    import numpy as np
    import ask
    import common
    import embedding_store
    import explore
    import index_lock
    import session_context

    common_names = ("DATA_DIR", "MESSAGES_PATH", "EMBEDDINGS_PATH", "IDS_PATH",
                    "INDEX_LOCK_PATH")
    saved_common = {name: getattr(common, name) for name in common_names}
    saved_embedding_paths = {
        name: getattr(embedding_store, name)
        for name in ("DATA_DIR", "MESSAGES_PATH", "EMBEDDINGS_PATH", "IDS_PATH")
    }
    saved_session_data = session_context.DATA_DIR
    saved_index_lock_path = index_lock.INDEX_LOCK_PATH
    saved_read = common.read_embeddings
    saved_ask = (ask._embed_query, ask._guard_embedder, ask._MESSAGE_REFS_SCHEMA)
    saved_concepts = explore._session_concept
    detail = "fixture did not run"
    ok = False
    try:
        with contextlib.ExitStack() as stack:
            td = stack.enter_context(tempfile.TemporaryDirectory())
            stack.callback(ask.clear_artifact_cache)
            root = P(td)
            common.DATA_DIR = root
            _establish_current_derived_fixture(root)
            embedding_store.DATA_DIR = root
            session_context.DATA_DIR = root
            common.MESSAGES_PATH = root / "messages.jsonl"
            common.EMBEDDINGS_PATH = root / "embeddings.f32"
            common.IDS_PATH = root / "embeddings.ids"
            embedding_store.MESSAGES_PATH = common.MESSAGES_PATH
            embedding_store.EMBEDDINGS_PATH = common.EMBEDDINGS_PATH
            embedding_store.IDS_PATH = common.IDS_PATH
            common.INDEX_LOCK_PATH = root / ".index.lock"
            index_lock.INDEX_LOCK_PATH = common.INDEX_LOCK_PATH
            rows = [
                {"id": "A:s-one:0", "agent": "Codex", "session": "s-one",
                 "project": "ProjX", "model": "GPT-X", "who": "user",
                 "turn": 0, "ts": 100,
                 "text": "same duplicate opening alpha text α"},
                {"id": "A:s-one:1", "agent": "Codex", "session": "s-one",
                 "project": "ProjX", "model": "GPT-X", "who": "user",
                 "turn": 1, "ts": 101,
                 "text": "same duplicate opening alpha text α"},
                {"id": "B:s-two:0", "agent": "Claude", "session": "s-two",
                 "project": "Other", "model": "Sonnet", "who": "user",
                 "turn": 0, "ts": 200, "text": "beta text"},
            ]
            # Exercise byte offsets, not character offsets: true multibyte UTF-8,
            # CRLF separators, and a final row with no newline.
            source_body = "\r\n".join(
                json.dumps(row, ensure_ascii=False) for row in rows)
            common.MESSAGES_PATH.write_text(source_body, encoding="utf-8")
            (root / "replies.jsonl").write_text(
                json.dumps({"id": "A:s-one:0", "reply": "agent alpha answer"}),
                encoding="utf-8")
            # Deliberately non-source order: the refs DB is an embedding-ordinal
            # contract, not a transcript-order approximation.
            ids = ["A:s-one:0#r", "B:s-two:0", "A:s-one:0", "A:s-one:1"]
            texts = ["agent alpha answer", "beta text", rows[0]["text"], rows[1]["text"]]
            matrix = np.asarray([[.8, .2], [.7, .3], [1, 0], [.95, .05]],
                                dtype=np.float32)
            common.write_embeddings(
                ids, matrix, embeddings_path=common.EMBEDDINGS_PATH,
                ids_path=common.IDS_PATH, dim=2, model_id="fixture",
                text_hashes=[hashlib.blake2b(text.encode(), digest_size=8).hexdigest()
                             for text in texts])
            hashes_path = common.EMBEDDINGS_PATH.with_suffix(".hashes")
            canonical_hash_blob = hashes_path.read_bytes()
            writer_uses_lf = (
                len(canonical_hash_blob) == len(ids) * 17
                and canonical_hash_blob[16::17] == b"\n" * len(ids))
            hashes_path.write_bytes(canonical_hash_blob.replace(b"\n", b"\r\n"))
            legacy_crlf_hashes = (
                ask._read_embedding_hash_blob(len(ids)) == canonical_hash_blob)
            # Restore one fully committed canonical generation after deliberately
            # mutating the hashes artifact for the legacy-reader check.
            common.write_embeddings(
                ids, matrix, embeddings_path=common.EMBEDDINGS_PATH,
                ids_path=common.IDS_PATH, dim=2, model_id="fixture",
                text_hashes=[hashlib.blake2b(text.encode(), digest_size=8).hexdigest()
                             for text in texts])

            def stamp_message_generation():
                state = common.embedding_artifact_state(
                    root / "embeddings.meta", common.EMBEDDINGS_PATH, common.IDS_PATH)
                (root / ".semantic-embeddings-generation.json").write_text(
                    json.dumps({
                        "version": 2,
                        "source": common.transcript_generation(),
                        "output": {"bundle": state["identity"]},
                    }), encoding="utf-8")

            stamp_message_generation()

            # common.read_embeddings has import-time default paths; make the
            # fixture explicit while still exercising its real commit+memmap reader.
            common.read_embeddings = lambda embeddings_path=None, ids_path=None, dim=2, \
                    meta_path=None, **kwargs: saved_read(
                        common.EMBEDDINGS_PATH, common.IDS_PATH, dim, meta_path)
            ask._embed_query = lambda *args, **kwargs: np.asarray([1, 0], dtype=np.float32)
            ask._guard_embedder = lambda *args, **kwargs: None
            explore._session_concept = lambda: {}
            ask.clear_artifact_cache()

            ask._MESSAGE_REFS_SCHEMA = "1"
            _, _, _, _ = ask._message_artifacts(2, root / "embeddings.meta")
            schema1_path = ask._MESSAGE_REFS["path"]
            ask.clear_artifact_cache()
            ask._MESSAGE_REFS_SCHEMA = saved_ask[2]
            loaded_ids, loaded_matrix, refs, _ = ask._message_artifacts(
                2, root / "embeddings.meta")
            schema_migrated = ask._MESSAGE_REFS["path"] != schema1_path
            identity_hash_calls = []
            saved_ids_sha256 = ask._ids_sha256
            try:
                ask._ids_sha256 = lambda values: (
                    identity_hash_calls.append(len(values))
                    or saved_ids_sha256(values))
                ask._message_artifacts(2, root / "embeddings.meta")
                ask._message_artifacts(2, root / "embeddings.meta")
            finally:
                ask._ids_sha256 = saved_ids_sha256
            fast_identity_cached = not identity_hash_calls
            immutable_ids = isinstance(loaded_ids, tuple)
            try:
                loaded_ids[0] = "mutated"
                immutable_ids = False
            except TypeError:
                pass
            all_eligible = refs.eligible(None).tolist()
            exact_filter = refs.eligible({
                "agent": "cod", "project": "PROJ", "chat": "S-ONE",
                "who": "agent", "model": "gpt-x", "since_ms": 100,
                "until_ms": 101,
            }).tolist()
            soft_model = refs.eligible({"model": "gpt", "model_soft": True}).tolist()
            resolved = refs.resolve([2, 0])
            best = refs.best_by_session(np.asarray([.8, .7, 1, .95]), None)
            invalid_scores_refused = []
            for invalid_scores in (
                    np.asarray([.8, np.nan, 1, .95]),
                    np.asarray([.8, .7, 1, .95, .1])):
                try:
                    refs.best_by_session(invalid_scores, None)
                    invalid_scores_refused.append(False)
                except RuntimeError as exc:
                    invalid_scores_refused.append("misaligned" in str(exc))
            metadata = refs.resolve(range(len(ids)))
            duplicate_rejected = False
            try:
                ask._message_refs_identity([ids[0], ids[0]])
            except RuntimeError as exc:
                duplicate_rejected = "not unique" in str(exc)
            rng = random.Random(9182)
            filter_parity = True
            for _ in range(180):
                flt = {
                    "agent": rng.choice((None, "cod", "CLAUDE", "missing")),
                    "project": rng.choice((None, "proj", "OTHER", "missing")),
                    "chat": rng.choice((None, "s-", "S-ONE", "s-two", "x")),
                    "who": rng.choice((None, "user", "agent", "tool")),
                    "model": rng.choice((None, "gpt-x", "GPT", "sonnet", "none")),
                    "model_soft": bool(rng.getrandbits(1)),
                    "since_ms": rng.choice((None, 99, 100, 101, 200, 201)),
                    "until_ms": rng.choice((None, 100, 101, 200, 201)),
                }
                expected = [i for i, row in enumerate(metadata) if ask._matches(row, flt)]
                if refs.eligible(flt).tolist() != expected:
                    filter_parity = False
                    break

            marker_path = root / ".semantic-embeddings-generation.json"
            malformed_marker_refused = []
            for malformed in ([], {"version": 2, "output": []}):
                marker_path.write_text(json.dumps(malformed), encoding="utf-8")
                try:
                    ask._require_current_message_index()
                except RuntimeError:
                    malformed_marker_refused.append(True)
                else:
                    malformed_marker_refused.append(False)
            stamp_message_generation()

            messages = json.loads(ask.tool_search_messages(
                "q", 4, envelope=True))
            grouped = json.loads(ask.tool_search_messages(
                "q", 4, group_session=True, envelope=True))
            filtered = json.loads(ask.tool_search_messages(
                "q", 4, filters={"who": "agent"}, envelope=True))
            hybrid = json.loads(ask.tool_search_hybrid("q", 4))

            hashes_path = common.EMBEDDINGS_PATH.with_suffix(".hashes")
            hashes_backup = root / "embeddings.hashes.saved"
            missing_hashes_rejected = False
            missing_hashes_db = root / "missing-hashes.db"
            os.replace(hashes_path, hashes_backup)
            try:
                ask._create_message_refs_db(missing_hashes_db, ids, "missing-hashes")
            except RuntimeError as exc:
                missing_hashes_rejected = "hashes are missing" in str(exc)
            finally:
                missing_hashes_db.unlink(missing_ok=True)
                os.replace(hashes_backup, hashes_path)

            unembedded_rejected = False
            unembedded_db = root / "unembedded.db"
            common.MESSAGES_PATH.write_text(
                source_body + "\r\n" + json.dumps(
                    {"id": "C:s-three:0", "text": "not embedded"}),
                encoding="utf-8")
            try:
                ask._create_message_refs_db(unembedded_db, ids, "unembedded")
            except RuntimeError as exc:
                unembedded_rejected = "unembedded source rows" in str(exc)
            finally:
                unembedded_db.unlink(missing_ok=True)
                common.MESSAGES_PATH.write_text(source_body, encoding="utf-8")
            stamp_message_generation()

            # Reproduce the same-ID race exactly: vectors are mapped, then a new
            # commit with the same ids lands before refs resolution. The first
            # matrix is rejected and _message_artifacts reloads the new bundle.
            old_db_path = ask._MESSAGE_REFS["path"]
            old_reader = sqlite3.connect(old_db_path)
            old_reader.execute("PRAGMA mmap_size=67108864")
            old_reader_locator = old_reader.execute(
                "SELECT source_kind,byte_offset,byte_length,text_hash "
                "FROM texts WHERE ord=2").fetchone()
            ask.clear_artifact_cache()
            new_rows = [dict(row) for row in rows]
            new_rows[0]["text"] = "new generation text"
            new_source_body = "\r\n".join(
                json.dumps(row, ensure_ascii=False) for row in new_rows)
            new_texts = ["agent alpha answer", "beta text",
                         new_rows[0]["text"], new_rows[1]["text"]]
            new_matrix = np.asarray([[.81, .19], [.71, .29], [.99, .01], [.94, .06]],
                                    dtype=np.float32)
            original_refs = ask._message_refs
            race_matrices = []

            def publish_between(ids_arg, matrix_arg=None, coverage=None,
                                allow_build=True):
                race_matrices.append(matrix_arg)
                if len(race_matrices) == 1:
                    common.MESSAGES_PATH.write_text(new_source_body, encoding="utf-8")
                    common.write_embeddings(
                        ids, new_matrix, embeddings_path=common.EMBEDDINGS_PATH,
                        ids_path=common.IDS_PATH, dim=2, model_id="fixture",
                        text_hashes=[hashlib.blake2b(text.encode(), digest_size=8).hexdigest()
                                     for text in new_texts])
                return original_refs(
                    ids_arg, matrix_arg, coverage=coverage,
                    allow_build=allow_build)

            ask._message_refs = publish_between
            race_guard_refused = False
            try:
                ask._message_artifacts(2, root / "embeddings.meta")
            except RuntimeError as exc:
                race_guard_refused = "stale" in str(exc)
            finally:
                ask._message_refs = original_refs
            publication_guard_refused = False
            try:
                ask._require_current_message_index()
            except RuntimeError as exc:
                publication_guard_refused = "stale" in str(exc)
            stamp_message_generation()
            ask.clear_artifact_cache()
            new_ids, new_loaded_matrix, new_refs, _ = ask._message_artifacts(
                2, root / "embeddings.meta")
            new_db_path = ask._MESSAGE_REFS["path"]
            canonical_identity = common.embedding_artifact_identity(
                root / "embeddings.meta", common.EMBEDDINGS_PATH, common.IDS_PATH)
            race_retried = (
                # Coverage now validates before each refs attempt, so the retry
                # refuses the unstamped publication before invoking _message_refs.
                race_guard_refused and len(race_matrices) == 1
                and common.embedding_matrix_identity(new_loaded_matrix) == canonical_identity)
            stale_refused = False
            try:
                original_refs(loaded_ids, race_matrices[0])
            except ask._EmbeddingBundleMoved:
                stale_refused = True
            immutable_reader = (
                new_db_path != old_db_path
                and old_reader_locator == old_reader.execute(
                    "SELECT source_kind,byte_offset,byte_length,text_hash "
                    "FROM texts WHERE ord=2").fetchone())
            old_reader.close()

            # Any ordinary SQLite mutation, including an interior row that the old
            # edge-only guard missed, dirties the seal and rebuilds from source.
            corrupt_path = new_db_path
            ask.clear_artifact_cache()
            logical = sqlite3.connect(corrupt_path)
            poison = "EVIL locator/hash"
            poison_hash = hashlib.blake2b(
                poison.encode(), digest_size=8).hexdigest()
            logical.execute(
                "UPDATE texts SET byte_offset=byte_offset+1 WHERE ord=1")
            logical.commit()
            logical.close()
            _, _, logical_store, _ = ask._message_artifacts(
                2, root / "embeddings.meta")
            logical_rebuilt = logical_store.resolve([1])[0]["text"] == "beta text"

            # Proof is independent of the in-DB seal: forge hash/metadata/seal together
            # (guards briefly dropped); open is allowed, resolve must reject vs canonical hashes.
            selected_path = ask._MESSAGE_REFS["path"]
            ask.clear_artifact_cache()
            forged = sqlite3.connect(selected_path)
            forged.executescript("""
                DROP TRIGGER refs_dirty_update;
                DROP TRIGGER texts_dirty_update;
            """)
            row = forged.execute(
                "SELECT mid,agent,session,model,who,side,turn,ts "
                "FROM refs WHERE ord=1"
            ).fetchone()
            locator = forged.execute(
                "SELECT source_kind,byte_offset,byte_length FROM texts WHERE ord=1"
            ).fetchone()
            forged_project = "EVIL project"
            forged_seal = ask._ref_row_seal(
                1, row[0], row[1] or "", row[2] or "", forged_project,
                row[3] or "", row[4] or "user", int(row[5]),
                int(row[6] or 0), int(row[7] or 0), poison_hash,
                int(locator[0]), int(locator[1]), int(locator[2]))
            forged.execute(
                "UPDATE texts SET text_hash=? WHERE ord=1", (poison_hash,))
            forged.execute(
                "UPDATE refs SET project=?,row_seal=? WHERE ord=1",
                (forged_project, forged_seal))
            forged.executescript("""
                CREATE TRIGGER refs_dirty_update AFTER UPDATE ON refs BEGIN
                    UPDATE meta SET value='dirty' WHERE key='sealed'; END;
                CREATE TRIGGER texts_dirty_update AFTER UPDATE ON texts BEGIN
                    UPDATE meta SET value='dirty' WHERE key='sealed'; END;
            """)
            forged.commit()
            forged.close()
            _, _, forged_store, _ = ask._message_artifacts(
                2, root / "embeddings.meta")
            selected_refused = False
            try:
                forged_store.resolve([1])
            except ask.CorruptMessageRefs:
                selected_refused = True
            ask.invalidate_message_refs()
            _, _, healed_store, _ = ask._message_artifacts(
                2, root / "embeddings.meta")
            selected_rebuilt = (
                ask._MESSAGE_REFS["path"] != selected_path
                and healed_store.resolve([1])[0]["text"] == "beta text")

            # Dynamic SQLite scalar corruption must normalize to the dedicated
            # corruption exception too; raw ValueError/AttributeError would make
            # the resident worker return repeat 500s without quarantining.
            def malformed_scalar_refused(table, column, value, trigger):
                bad_path = ask._MESSAGE_REFS["path"]
                ask.clear_artifact_cache()
                bad = sqlite3.connect(bad_path)
                bad.execute(f"DROP TRIGGER {trigger}")
                bad.execute(
                    f"UPDATE {table} SET {column}=? WHERE ord=1", (value,))
                bad.executescript(
                    f"CREATE TRIGGER {trigger} AFTER UPDATE ON {table} BEGIN "
                    "UPDATE meta SET value='dirty' WHERE key='sealed'; END;")
                bad.commit()
                bad.close()
                _, _, store, _ = ask._message_artifacts(
                    2, root / "embeddings.meta")
                try:
                    store.resolve([1])
                except ask.CorruptMessageRefs:
                    refused = True
                else:
                    refused = False
                ask.invalidate_message_refs()
                _, _, repaired_store, _ = ask._message_artifacts(
                    2, root / "embeddings.meta")
                return (refused
                        and repaired_store.resolve([1])[0]["text"] == "beta text")

            selected_scalars = all((
                malformed_scalar_refused(
                    "refs", "turn", "evil", "refs_dirty_update"),
                malformed_scalar_refused(
                    "refs", "ts", "evil", "refs_dirty_update"),
                malformed_scalar_refused(
                    "texts", "byte_offset", sqlite3.Binary(b"evil"),
                    "texts_dirty_update"),
            ))

            corrupt_path = ask._MESSAGE_REFS["path"]
            ask.clear_artifact_cache()
            corrupt_path.write_bytes(b"not sqlite")
            orphan_tmp = root / ".embeddings.refs-dead-owner.tmp"
            orphan_tmp.write_bytes(b"partial")
            _, _, rebuilt_store, _ = ask._message_artifacts(
                2, root / "embeddings.meta")
            rebuilt = rebuilt_store.resolve([0])[0]["text"] == "agent alpha answer"
            debris_pruned = not orphan_tmp.exists()

            # Once opened, source locators stay bound to the exact materialized
            # generation. A replacement is refused rather than returning mixed text.
            stable_store = rebuilt_store
            saved_generation_reader = common.transcript_generation
            expected_generation = stable_store.source_generation
            moved_generation = json.loads(json.dumps(expected_generation))
            moved_generation["ingest_signature"] = "moved"
            observations = iter((expected_generation, moved_generation))
            try:
                common.transcript_generation = lambda *args, **kwargs: next(observations)
                try:
                    stable_store.resolve([])
                except ask._EmbeddingBundleMoved:
                    empty_move_refused = True
                else:
                    empty_move_refused = False
                common.transcript_generation = lambda *args, **kwargs: (
                    (_ for _ in ()).throw(ValueError("publication window")))
                try:
                    stable_store.resolve([])
                except ask._EmbeddingBundleMoved:
                    value_error_normalized = True
                else:
                    value_error_normalized = False
            finally:
                common.transcript_generation = saved_generation_reader
            changed = dict(new_rows[0])
            changed["text"] = "uncommitted third generation"
            common.MESSAGES_PATH.write_text(
                json.dumps(changed) + "\n" + "".join(
                    json.dumps(row) + "\n" for row in new_rows[1:]), encoding="utf-8")
            source_move_refused = False
            try:
                stable_store.resolve([2])
            except ask._EmbeddingBundleMoved:
                source_move_refused = True
            ask.clear_artifact_cache()
            mismatch_refused = False
            try:
                ask._message_artifacts(2, root / "embeddings.meta")
            except RuntimeError as exc:
                mismatch_refused = ("does not match embedding hash" in str(exc)
                                    or "stale" in str(exc))
            direct_stale_refused = False
            try:
                ask._require_current_message_index()
            except RuntimeError as exc:
                direct_stale_refused = "stale" in str(exc)

            # Restore a coherent generation so cleanup can verify the artifact.
            common.MESSAGES_PATH.write_text(new_source_body, encoding="utf-8")
            stamp_message_generation()
            ask.clear_artifact_cache()
            _, _, final_store, _ = ask._message_artifacts(
                2, root / "embeddings.meta")
            dead_temp = root / (
                ".embeddings.refs-build-2147483647-win_1-orphan.tmp")
            dead_temp.write_bytes(b"killed builder")
            ask.clear_artifact_cache()
            _, _, final_store, _ = ask._message_artifacts(
                2, root / "embeddings.meta")
            startup_pruned = not dead_temp.exists()
            recycled = root / ".embeddings.refs-build-4242-win_old-recycled.tmp"
            live_temp = root / ".embeddings.refs-build-4242-win_new-live.tmp"
            recycled.write_bytes(b"old owner")
            live_temp.write_bytes(b"live owner")
            saved_liveness = (common.pid_alive, common.process_start_identity)
            try:
                common.pid_alive = lambda pid: pid == 4242
                common.process_start_identity = lambda pid: "win_new" if pid == 4242 else None
                ask._prune_dead_message_ref_temps()
                pid_reuse_pruned = not recycled.exists() and live_temp.exists()
            finally:
                common.pid_alive, common.process_start_identity = saved_liveness
                live_temp.unlink(missing_ok=True)
            db_path = ask._MESSAGE_REFS["path"]
            db = sqlite3.connect(db_path)
            integrity = db.execute("PRAGMA quick_check").fetchone()[0]
            text_rows = db.execute("SELECT count(*) FROM texts").fetchone()[0]
            text_columns = [row[1] for row in db.execute("PRAGMA table_info(texts)")]
            db.close()
            final_id = final_store.resolve([1])[0]["id"]
            ask.clear_artifact_cache()
            pointer_store = ask._message_refs_from_pointer(
                ask._require_current_message_index())
            pointer_fast = bool(
                pointer_store is not None and pointer_store.ids is None
                and pointer_store.resolve([1])[0]["id"] == ids[1])
            ask.clear_artifact_cache()

            ungroup_texts = [row["text"] for row in messages["results"]]
            grouped_sessions = [row["session"] for row in grouped["results"]]
            hybrid_sessions = [row["session"] for row in hybrid["results"]]
            ok = (
                all_eligible == [0, 1, 2, 3]
                and exact_filter == [0] and soft_model == [0, 2, 3]
                and filter_parity and duplicate_rejected and schema_migrated
                and fast_identity_cached and immutable_ids
                and all(invalid_scores_refused)
                and writer_uses_lf and legacy_crlf_hashes
                and all(malformed_marker_refused)
                and missing_hashes_rejected and unembedded_rejected
                and [row["id"] for row in resolved] == ["A:s-one:0", "A:s-one:0#r"]
                and [row["who"] for row in resolved] == ["user", "agent"]
                and best == {"s-one": 2, "s-two": 1}
                and ungroup_texts == [rows[0]["text"], "agent alpha answer", "beta text"]
                and messages["candidate_sessions"] == 4
                and grouped_sessions == ["s-one", "s-two"]
                and filtered["results"][0]["who"] == "agent"
                and hybrid_sessions == ["s-one", "s-two"]
                and race_retried and publication_guard_refused and stale_refused
                and immutable_reader
                and logical_rebuilt and selected_refused and selected_rebuilt
                and selected_scalars
                and rebuilt and debris_pruned
                and startup_pruned and pid_reuse_pruned
                and source_move_refused and empty_move_refused
                and value_error_normalized
                and mismatch_refused and direct_stale_refused
                and integrity == "ok" and text_rows == len(ids)
                and "text" not in text_columns
                and final_id == ids[1] and pointer_fast)
            detail = (f"filter={exact_filter}/parity={filter_parity} "
                      f"dup-reject={duplicate_rejected} schema-current={schema_migrated} "
                      f"hot-id-cache={fast_identity_cached}/{immutable_ids} "
                      f"score-guard={invalid_scores_refused} "
                      f"hash-newline={writer_uses_lf}/{legacy_crlf_hashes} "
                      f"malformed={malformed_marker_refused} "
                      f"coverage={missing_hashes_rejected}/{unembedded_rejected} "
                      f"order={[row['id'] for row in resolved]} "
                      f"ungroup={len(messages['results'])}/4 grouped={grouped_sessions} "
                      f"race={race_retried}/{publication_guard_refused}/{stale_refused} "
                      f"immutable={immutable_reader} "
                      f"rebuild={logical_rebuilt}/{selected_refused}/"
                      f"{selected_rebuilt}/{selected_scalars}/{rebuilt} "
                      f"debris={debris_pruned}/{startup_pruned}/{pid_reuse_pruned} "
                      f"source-move={source_move_refused}/{empty_move_refused}/"
                      f"{value_error_normalized} "
                      f"refused={mismatch_refused}/{direct_stale_refused} "
                      f"pointer={pointer_fast} "
                      f"db={db_path.stat().st_size}B/{text_rows} rows")
    except Exception as exc:  # noqa: BLE001 -- report the exact fixture failure
        detail = f"{type(exc).__name__}: {exc}"
        ok = False
    finally:
        try:
            ask.clear_artifact_cache()
        except Exception:
            pass
        common.read_embeddings = saved_read
        (ask._embed_query, ask._guard_embedder,
         ask._MESSAGE_REFS_SCHEMA) = saved_ask
        explore._session_concept = saved_concepts
        for name, value in saved_common.items():
            setattr(common, name, value)
        for name, value in saved_embedding_paths.items():
            setattr(embedding_store, name, value)
        session_context.DATA_DIR = saved_session_data
        index_lock.INDEX_LOCK_PATH = saved_index_lock_path
    return ("PASS" if ok else "FAIL", detail)


# hookless isolation — the observation layer imports nothing from the product layer.
# The boundary is what lets hookless split into its own package later; this makes
# drift a test failure instead of a discovery at split time.
def t_hookless_boundary():
    import ast
    allowed = set(sys.stdlib_module_names) | {"hookless"}
    bad = []
    hk = os.path.join(HERE, "hookless")
    for fn in sorted(os.listdir(hk)):
        if not fn.endswith(".py"):
            continue
        with open(os.path.join(hk, fn), encoding="utf-8") as source:
            tree = ast.parse(source.read())
        for node in ast.walk(tree):
            roots = []
            if isinstance(node, ast.Import):
                roots = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                roots = [node.module.split(".")[0]]
            bad += [f"{fn}:{node.lineno} {r}" for r in roots if r not in allowed]
    return ("PASS" if not bad else "FAIL",
            "all imports stdlib/hookless" if not bad else f"crossings: {bad}")


def t_comment_hygiene():
    """Enforce mechanical comment and test hygiene: inline comment blocks stay
    within 3 lines, comments avoid rewrite-relative or personal markers, and
    assertions cannot be statically vacuous. Review still owns semantic quality."""
    import re
    import subprocess
    from pathlib import Path
    repo = Path(REPO)
    try:
        tracked = subprocess.run(["git", "ls-files", "*.py", "*.rs"], cwd=repo,
                                 capture_output=True, text=True, encoding="utf-8",
                                 errors="replace").stdout.split()
    except FileNotFoundError:
        return ("SKIP", "git unavailable (hygiene is a repo gate)")
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "*.py", "*.rs"],
        cwd=repo, capture_output=True, text=True,
        encoding="utf-8", errors="replace").stdout.split()
    files = sorted(set(tracked + untracked))
    files = [f for f in files if "fixtures" not in f and not f.startswith("web/")]
    banned = re.compile(
        r"\b(used to \w|the old \w|now we \w|moved from|verbatim-in-spirit"
        r"|previously the)\b"
        r"|\b(?:my|our) (?:machine|computer|laptop|desktop|home|network|corpus|setup)\b",
        re.I)
    vacuous = re.compile(r"\bassert\s+(True|1)\s*(,|$)|assert!\(\s*true\s*\)")
    lazy_re = re.compile(r"""re\.(search|match|fullmatch)\(\s*r?["']\.[*+]["']""")
    violations = []
    for f in files:
        try:
            lines = (repo / f).read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        run_start, run_len = None, 0
        for i, raw in enumerate(lines, 1):
            s = raw.strip()
            if vacuous.search(s):
                violations.append(f"{f}:{i} vacuous assertion")
            if lazy_re.search(s):
                violations.append(f"{f}:{i} match-anything assertion regex")
            is_inline = ((f.endswith(".py") and s.startswith("#") and not s.startswith("#!"))
                         or (f.endswith(".rs") and s.startswith("//")
                             and not s.startswith("///") and not s.startswith("//!")))
            if is_inline and len(raw) > 110:
                # the height cap's loophole: essays rotated into one crammed line
                violations.append(f"{f}:{i} comment line of {len(raw)} chars")
            if is_inline:
                run_start = run_start or i
                run_len += 1
                body = s.lstrip("#/ ")
                if banned.search(body):
                    violations.append(f"{f}:{i} cold-reading marker: {body[:60]}")
            else:
                if run_len > 3:
                    violations.append(f"{f}:{run_start} inline comment block of {run_len} lines")
                run_start, run_len = None, 0
        if run_len > 3:
            violations.append(f"{f}:{run_start} inline comment block of {run_len} lines")
    ok = not violations
    detail = f"{len(files)} files clean" if ok else "; ".join(violations[:6]) + (
        f" (+{len(violations) - 6} more)" if len(violations) > 6 else "")
    return ("PASS" if ok else "FAIL", detail)


def t_sentinel_teardown():
    """The uninstall sentinel's generated script must fulfill its contract when the
    CLI vanishes: strip every taught block, delete block-only files, and
    self-clean. A
    present CLI and a blip-restore during the recheck must both leave everything
    alone. Runs the REAL generated artifact under /bin/sh in a sandbox."""
    import ast
    import json
    import shutil
    import subprocess
    import tempfile
    import time as _time
    from pathlib import Path as P
    import teach
    import hookinstall
    common = teach.common

    if sys.platform == "win32":
        # the sh twins don't run here; at least prove the waiter parses
        ast.parse(teach._SENTINEL_WATCH_PY)
        return ("SKIP", "windows: sh sentinel not runnable (waiter parses ok)")
    ast.parse(teach._SENTINEL_WATCH_PY)

    saved = (teach.HOME, teach.REPO, common.DATA_DIR, teach.SKILL_TARGETS)
    try:
        with tempfile.TemporaryDirectory() as td:
            root = P(td)
            teach.HOME = root / "home"
            teach.REPO = root / "repo"
            common.DATA_DIR = root / "data"
            teach.SKILL_TARGETS = [
                ("cursor", teach.HOME / ".cursor",
                 teach.HOME / ".cursor" / "skills" / "agrep-recall" / "SKILL.md")]
            for d in (
                    teach.HOME,
                    teach.REPO,
                    common.DATA_DIR,
                    teach.HOME / ".codex",
                    teach.HOME / ".pi" / "agent",
                    teach.HOME / ".omp" / "agent",
            ):
                d.mkdir(parents=True)
            cli = teach.REPO / "cli.py"
            cli.write_text("# cli\n", encoding="utf-8")
            hooks_ready = (
                hookinstall.install_codex_hooks(warn=False)
                and hookinstall.install_pi_extensions(warn=False)
            )
            codex_hook = hookinstall.codex_hooks_paths()[0]
            pi_extensions = dict(hookinstall.pi_extension_paths())

            block = teach._block(None)
            keep = teach.HOME / "AGENTS.md"
            keep.write_text("my own notes\n\n" + block, encoding="utf-8")
            bare = teach.HOME / "GEMINI.md"
            bare.write_text(block, encoding="utf-8")
            targets = [keep, bare]

            (common.DATA_DIR / "teach.json").write_text("{}", encoding="utf-8")
            skill = teach.SKILL_TARGETS[0][2]
            skill.parent.mkdir(parents=True)
            skill.write_text(teach._SKILL_FRONT + block + "my custom rule\n",
                             encoding="utf-8")
            addition = skill.parent / "user-note.txt"
            addition.write_text("keep me", encoding="utf-8")
            exact = teach.HOME / ".cursor" / "skills" / "agrep-exact" / "SKILL.md"
            exact.parent.mkdir(parents=True)
            exact.write_text(teach._SKILL_FRONT + block, encoding="utf-8")
            teach.SKILL_TARGETS.append(("cursor-exact", teach.HOME / ".cursor", exact))
            targets.extend((skill, exact))

            plist = teach._plist_path()
            subs = teach._sh_subs(targets) | {
                # NEVER the real label: the sandboxed teardown executes this
                # script, and its bootout would disarm the box's live sentinel
                "@@LABEL@@": "com.agrep.sentinel-selftest",
                "@@PLIST@@": teach._sh_squote(plist),
            }
            tail = (teach._SENTINEL_TAIL_MAC if sys.platform == "darwin"
                    else teach._SENTINEL_TAIL_LINUX)
            if "@@UNIT@@" in tail:
                subs |= {"@@UNIT@@": teach.TASK_NAME, "@@UNITS@@": "''"}
            script = teach._write_sentinel_sh(tail, subs)
            edited_omp = (
                pi_extensions["omp"].read_bytes() + b"\n// user edit\n")
            pi_extensions["omp"].write_bytes(edited_omp)
            plist.parent.mkdir(parents=True, exist_ok=True)
            plist.write_text(teach._LAUNCHD_PLIST.format(
                # the sandbox label, in the PLIST too: `launchctl unload <path>`
                # resolves the label from the file, so a real label here lets
                # the sandboxed script's fallback unload the box's live job
                label="com.agrep.sentinel-selftest", script=script,
                watch="".join(f"<string>{p}</string>"
                              for p in teach._sentinel_watch_paths())),
                encoding="utf-8")
            leftovers = "@@" in script.read_text(encoding="utf-8")
            plist_ok = True
            if shutil.which("plutil"):
                plist_ok = subprocess.run(["plutil", "-lint", str(plist)],
                                          capture_output=True).returncode == 0
            # timing-only patch: the 20s blip window shrinks to 1s
            script.write_text(script.read_text(encoding="utf-8")
                              .replace("sleep 20", "sleep 1"), encoding="utf-8")

            def run():
                return subprocess.run(["/bin/sh", str(script)], capture_output=True,
                                      text=True, encoding="utf-8", errors="replace",
                                      timeout=30).returncode

            rc_present = run()
            present_ok = (
                rc_present == 0
                and block in keep.read_text(encoding="utf-8")
                and bare.exists() and skill.exists()
                and codex_hook.exists()
                and pi_extensions["pi"].exists()
                and pi_extensions["omp"].read_bytes() == edited_omp
            )

            cli.unlink()
            proc = subprocess.Popen(["/bin/sh", str(script)], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            _time.sleep(0.3)
            cli.write_text("# cli\n", encoding="utf-8")
            proc.wait(timeout=30)
            blip_ok = (
                block in keep.read_text(encoding="utf-8")
                and bare.exists()
                and codex_hook.exists()
                and pi_extensions["pi"].exists()
                and pi_extensions["omp"].read_bytes() == edited_omp
            )

            cli.unlink()
            run()
            kept_text = keep.read_text(encoding="utf-8") if keep.exists() else ""
            stripped = (
                teach.MARK_PREFIX not in kept_text
                and "my own notes" in kept_text
                and not bare.exists()
                and teach.MARK_PREFIX not in skill.read_text(encoding="utf-8")
                and "my custom rule" in skill.read_text(encoding="utf-8")
                and not exact.exists()
                and addition.read_text(encoding="utf-8") == "keep me"
                and not codex_hook.exists()
                and not pi_extensions["pi"].exists()
                and pi_extensions["omp"].read_bytes() == edited_omp
                and not script.exists()
                and not (common.DATA_DIR / "teach.json").exists()
            )
    finally:
        teach.HOME, teach.REPO, common.DATA_DIR, teach.SKILL_TARGETS = saved

    ok = (
        hooks_ready and present_ok and blip_ok and stripped
        and not leftovers and plist_ok
    )
    return ("PASS" if ok else "FAIL",
            f"hooks={hooks_ready} present-noop={present_ok} "
            f"blip-noop={blip_ok} teardown={stripped} "
            f"templated={not leftovers} plist-lint={plist_ok}")


def t_agent_setup_consent():
    """The agent-driven setup contract: headless without --yes discloses the writes,
    points at `agrep setup --yes`, and writes NOTHING; --yes writes blocks with no
    prompt; an enrolled box converges silently; remove keeps user content and
    deletes block-only files. Sentinel scripts are stubbed here -
    t_sentinel_teardown runs the real ones."""
    import contextlib
    import io
    import tempfile
    from pathlib import Path as P
    import corpusdb
    import removal_fence
    import teach
    common = teach.common
    indexd_runtime = teach.indexd_runtime

    saved = (teach.HOME, common.DATA_DIR, teach.STATE_PATH, teach.MD_TARGETS,
             teach.SKILL_TARGETS, teach._sentinel_install, teach._sentinel_remove,
             indexd_runtime.SEARCH_BEAT_PATH, indexd_runtime.INDEXD_LOCK_PATH,
             indexd_runtime.INDEXD_READY_PATH, indexd_runtime.INDEXD_CHILD_PATH,
             indexd_runtime.LEGACY_INDEXD_LOCK_PATH, indexd_runtime._SPAWN_GUARD_PATH,
             indexd_runtime.DERIVED_OWNER_PATH, indexd_runtime.INGEST_CACHE_PATH,
             corpusdb.DB_PATH,
             removal_fence.DATA_DIR,
             sys.stdin)
    try:
        with tempfile.TemporaryDirectory() as td:
            root = P(td)
            home, data = root / "home", root / "data"
            (home / ".claude").mkdir(parents=True)
            (home / ".codex").mkdir(parents=True)
            (home / ".cursor").mkdir(parents=True)
            data.mkdir()
            teach.HOME, common.DATA_DIR = home, data
            removal_fence.DATA_DIR = data
            teach.STATE_PATH = data / "teach.json"
            indexd_runtime.SEARCH_BEAT_PATH = data / f".agrep.search.v{indexd_runtime.INDEXD_PROTOCOL}"
            indexd_runtime.INDEXD_LOCK_PATH = data / f".indexd.v{indexd_runtime.INDEXD_PROTOCOL}.lock"
            indexd_runtime.INDEXD_READY_PATH = data / f".indexd.v{indexd_runtime.INDEXD_PROTOCOL}.ready"
            indexd_runtime.INDEXD_CHILD_PATH = data / f".indexd.v{indexd_runtime.INDEXD_PROTOCOL}.child"
            indexd_runtime.LEGACY_INDEXD_LOCK_PATH = data / ".indexd.lock"
            indexd_runtime._SPAWN_GUARD_PATH = data / f".indexd.v{indexd_runtime.INDEXD_PROTOCOL}.spawn"
            indexd_runtime.DERIVED_OWNER_PATH = data / ".derived-owner.json"
            indexd_runtime.INGEST_CACHE_PATH = data / ".ingest_cache.bin"
            corpusdb.DB_PATH = data / "corpus.db"
            (home / ".grok").mkdir(parents=True)
            teach.MD_TARGETS = [
                ("claude", home / ".claude", home / ".claude" / "CLAUDE.md"),
                ("codex", home / ".codex", home / ".codex" / "AGENTS.md"),
                ("grok", home / ".grok", home / ".grok" / "GROK.md"),
            ]
            teach.SKILL_TARGETS = [
                ("cursor", home / ".cursor",
                 home / ".cursor" / "skills" / "agrep-recall" / "SKILL.md")]
            teach._sentinel_install = lambda targets: True
            teach._sentinel_remove = lambda: True
            sys.stdin = io.StringIO("")  # deterministically not a terminal
            claude_md = home / ".claude" / "CLAUDE.md"
            codex_md = home / ".codex" / "AGENTS.md"
            codex_md.write_text("my own rules\n", encoding="utf-8")
            grok_md = home / ".grok" / "GROK.md"
            grok_md.write_text("<identity>\nyou are grok\n</identity>\n\n"
                               "<rules>\nbe cool\n</rules>\n", encoding="utf-8")

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = teach.teach(yes=False)
            headless = (rc == 0 and not claude_md.exists()
                        and "agrep setup --yes" in out.getvalue())

            with contextlib.redirect_stdout(io.StringIO()):
                rc = teach.teach(yes=True)
            skill = home / ".cursor" / "skills" / "agrep-recall" / "SKILL.md"
            skill_text = skill.read_text(encoding="utf-8") if skill.exists() else ""
            grok_text = grok_md.read_text(encoding="utf-8")
            opted = (rc == 0 and teach.MARK_PREFIX in claude_md.read_text(encoding="utf-8")
                     and teach.MARK_PREFIX in codex_md.read_text(encoding="utf-8")
                     # a tag-styled host gets the tag-wrapped body; prose hosts don't
                     and "<agrep-recall>" in grok_text
                     and "<agrep-recall>" not in codex_md.read_text(encoding="utf-8")
                     and skill_text.startswith("---\nname: agrep-recall\n")
                     and teach.MARK_PREFIX in skill_text
                     and teach.STATE_PATH.exists())

            with contextlib.redirect_stdout(io.StringIO()):
                converge = teach.teach(yes=False) == 0

            with contextlib.redirect_stdout(io.StringIO()):
                rc = teach._remove()
            kept = codex_md.read_text(encoding="utf-8")
            removed = (rc == 0 and not claude_md.exists() and not skill.parent.exists()
                       and "my own rules" in kept and teach.MARK_PREFIX not in kept)
    finally:
        (teach.HOME, common.DATA_DIR, teach.STATE_PATH, teach.MD_TARGETS,
         teach.SKILL_TARGETS, teach._sentinel_install, teach._sentinel_remove,
         indexd_runtime.SEARCH_BEAT_PATH, indexd_runtime.INDEXD_LOCK_PATH,
         indexd_runtime.INDEXD_READY_PATH, indexd_runtime.INDEXD_CHILD_PATH,
         indexd_runtime.LEGACY_INDEXD_LOCK_PATH, indexd_runtime._SPAWN_GUARD_PATH,
         indexd_runtime.DERIVED_OWNER_PATH, indexd_runtime.INGEST_CACHE_PATH,
         corpusdb.DB_PATH,
         removal_fence.DATA_DIR,
         sys.stdin) = saved

    ok = headless and opted and converge and removed
    return ("PASS" if ok else "FAIL",
            f"headless-noop={headless} yes-writes={opted} "
            f"enrolled-converge={converge} remove={removed}")


def t_block_version():
    """Block version discipline + fight resolution: invariants that keep the
    taught blocks byte-stable (they live in agents' prompt-cache prefixes):
    1. any NUDGE text change must bump NUDGE_V - the version is what stops a stale
       daemon from reverting newer text (upgrade + running daemon = byte flip-flop
       every tick otherwise, i.e. cache shredding);
    2. background reconcile preserves every existing drifted target; explicit
       setup owns upgrades, while newer and same-version user copies stay untouched;
    3. the taught probe example is rendered by the live output contract;
    4. the compact routing block names indexed history and live state separately."""
    import hashlib
    import json
    import compact
    import recall
    import tempfile
    from pathlib import Path as P
    import teach
    pinned = (37, "4b7040519c28")  # (NUDGE_V, sha256(NUDGE)[:12]) - update BOTH together
    h = hashlib.sha256(teach.NUDGE.encode()).hexdigest()[:12]
    if (teach.NUDGE_V, h) != pinned:
        return ("FAIL", f"NUDGE changed (v{teach.NUDGE_V}, {h}) vs pinned {pinned} - "
                        "bump NUDGE_V and re-pin here")
    probe = recall._probe_line(
        ["deadlock"],
        [{"session": "1a2b3c4d", "turn": 214, "agent": "claude",
          "project": "webapp", "matched": "phrase", "ts": 0, "who": "user",
          "content_digest": compact.content_digest("deadlock")}],
        "corpusdb",
        total_sessions=2,
        session_index=("1a2b3c4d",),
    )
    demo = next(
        (line.strip() for line in teach.NUDGE.splitlines()
         if line.strip().startswith("recall:")),
        "",
    )
    # The lean blocks carry no illustrative probe output; the anti-drift
    # check only binds when a demo line exists to drift.
    probe_current = (probe or "") == demo if demo else probe is not None
    routes_current = all(
        route in text
        for text in (teach.NUDGE, teach.NUDGE_CODEX)
        for route in (
            'agrep chats <topic or quote>',
            'agrep recall "<distinctive phrase>"', "agrep around <handle>",
            "agrep postcompact", "agrep board --once", "agrep --help",
        ))
    with tempfile.TemporaryDirectory() as td:
        older = P(td) / "older.md"
        newer = P(td) / "newer.md"
        edited = P(td) / "edited.md"
        older.write_text(f"{teach.MARK_PREFIX} v1 -->\nold text\n{teach.MARK_END}\n",
                         encoding="utf-8")
        future = f"{teach.MARK_PREFIX} v{teach.NUDGE_V + 1} -->\nfuture text\n{teach.MARK_END}\n"
        newer.write_text(future, encoding="utf-8")
        mine = (f"{teach.MARK_PREFIX} v{teach.NUDGE_V} -->\nmy own wording\n"
                f"{teach.MARK_END}\n")
        edited.write_text(mine, encoding="utf-8")
        saved = (teach.STATE_PATH, teach.MD_TARGETS, teach.SKILL_TARGETS,
                 teach.common.DATA_DIR)
        try:
            teach.common.DATA_DIR = P(td)
            teach.STATE_PATH = P(td) / "teach.json"
            teach.MD_TARGETS = [
                (f"fixture-{path.stem}", path.parent, path)
                for path in (older, newer, edited)
            ]
            teach.SKILL_TARGETS = []
            teach.STATE_PATH.write_text(
                json.dumps({"targets": [str(older), str(newer), str(edited)]}),
                encoding="utf-8")
            repaired = teach.reconcile()
            health = teach.current_reconcile_health()
        finally:
            (teach.STATE_PATH, teach.MD_TARGETS, teach.SKILL_TARGETS,
             teach.common.DATA_DIR) = saved
        old_preserved = "old text" in older.read_text(encoding="utf-8")
        untouched = newer.read_text(encoding="utf-8") == future
        kept = edited.read_text(encoding="utf-8") == mine
    drifted = [row["kind"] for row in health["refusals"]] == ["drifted"]
    ok = (old_preserved and untouched and kept and repaired == []
          and drifted and probe_current and routes_current)
    return ("PASS" if ok else "FAIL",
            f"old preserved={old_preserved}, newer untouched={untouched}, "
            f"same-version edit kept={kept}, drift disclosed={drifted}, "
            f"probe-current={probe_current}, routes-current={routes_current}")


def t_enrichment_boundary():
    # Optional enrichment lives outside core; an in-repo producer reference crosses that boundary.
    from pathlib import Path as P
    here = P(__file__).parent
    producers = ("emotion.py", "judge.py", "summarize.py", "titles.py", "vibe.py",
                 "concepts.py", "label_concepts.py", "embed_summaries.py")
    leaks = []
    if (here / "enrich").exists():
        leaks.append("py/enrich still exists")
    for p in [*here.glob("*.py"), here.parent / "cli.py", here.parent / "reindex.py"]:
        if p.name == "selftest.py":
            continue
        try:
            src = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for prod in producers:
            if prod in src:
                leaks.append(f"{p.name} references {prod}")
    return ("PASS" if not leaks else "FAIL", f"leaks={leaks or 'none'}")


def _archive_test_env(home):
    from unittest import mock
    roots = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "APPDATA": str(home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(home / "AppData" / "Local"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "CLINE_DIR": str(home / ".cline"),
        "CRUSH_GLOBAL_DATA": str(home / ".local" / "share" / "crush"),
        "OPENCODE_DB": "",
    }
    return mock.patch.dict(os.environ, roots)


def t_archive():
    import tempfile
    from pathlib import Path as P
    import archive
    if len(archive.ROOTS) != 16:  # a new adapter should land with its archive glob
        return ("FAIL", f"ROOTS has {len(archive.ROOTS)} entries, pinned 16 - "
                        "add the new store glob AND re-pin here")
    sid = "0199aaaa-bbbb-cccc-dddd-eeeeffff0000"
    saved = {k: getattr(archive, k) for k in
             ("HOME", "ARCHIVE_DIR", "MANIFEST", "STORE", "CONFIG", "HEALTH", "ROOTS")}
    with tempfile.TemporaryDirectory() as td:
        home = P(td) / "home"
        src = home / ".codex" / "sessions" / f"rollout-2026-07-06T00-00-00-{sid}.jsonl"
        src.parent.mkdir(parents=True)
        src.write_bytes(b'{"type":"session_meta","x":"one"}\n' * 40)
        try:
            archive.HOME = home
            archive.ARCHIVE_DIR = P(td) / "arch"
            archive.MANIFEST = archive.ARCHIVE_DIR / "manifest.jsonl"
            archive.STORE = archive.ARCHIVE_DIR / "store"
            archive.CONFIG = archive.ARCHIVE_DIR / "config.json"
            archive.HEALTH = archive.ARCHIVE_DIR / "capture-health.json"
            archive.ROOTS = [("codex", ".codex/sessions/rollout-*.jsonl", False)]
            with _archive_test_env(home):
                s1 = archive.capture()
                src.write_bytes(src.read_bytes() + b'{"type":"event_msg","x":"two"}\n' * 20)
                s2 = archive.capture()  # append-only growth -> suffix chunk, not a re-store
                want = src.read_bytes()
                out = P(td) / "out"
                rc_to = archive.restore(sid[:8], to=str(out))
                got = (out / src.name).read_bytes()
                rc_live = archive.restore(sid[:8])  # source still exists -> must refuse
                data = bytearray(src.read_bytes())
                data[10:13] = b"XYZ"  # non-append edit -> prefix check fails -> full store
                src.write_bytes(bytes(data))
                s3 = archive.capture()
                # GC: keep=1 drops old chunks while the newest stays reconstructable.
                archive._set_config(keep=1)
                fd = archive._try_lock()
                try:
                    pr = archive._prune()
                finally:
                    archive._unlock(fd)
                n_chunks = sum(1 for _ in archive.STORE.rglob("*.xz"))
                out2 = P(td) / "out2"
                rc_pruned = archive.restore(sid[:8], to=str(out2))
                got_pruned = (out2 / src.name).read_bytes()
        finally:
            for k, v in saved.items():
                setattr(archive, k, v)
    ok = (s1["full"] == 1 and s2["appended"] == 1 and s3["full"] == 1
          and rc_to == 0 and got == want and rc_live == 1
          and pr["dropped"] == 2 and n_chunks == 1
          and rc_pruned == 0 and got_pruned == bytes(data))
    return ("PASS" if ok else "FAIL",
            f"full={s1['full']},{s3['full']} appended={s2['appended']} "
            f"roundtrip={got == want} live-refused={rc_live == 1} "
            f"pruned={pr['dropped']} chunks-left={n_chunks} "
            f"pruned-roundtrip={got_pruned == bytes(data)}")


def t_archive_sqlite():
    # sqlite stores are imaged via the backup API, not copied raw: a restored image must
    # open valid and complete or `agrep restore` hands back a corpse. Pinned here because
    # the dev box has no such live store.
    import sqlite3
    import tempfile
    from pathlib import Path as P
    import archive
    saved = {k: getattr(archive, k) for k in
             ("HOME", "ARCHIVE_DIR", "MANIFEST", "STORE", "CONFIG", "HEALTH",
              "ROOTS", "_SQLITE_SETTLE_S")}
    td = P(tempfile.mkdtemp())
    try:
        home = td / "home"
        dbp = home / ".local" / "share" / "opencode" / "opencode.db"
        dbp.parent.mkdir(parents=True)
        con = sqlite3.connect(dbp)
        try:
            con.executescript("CREATE TABLE part(id TEXT, text TEXT);")
            con.executemany("INSERT INTO part VALUES(?,?)",
                            [(f"m{i}", f"body {i}") for i in range(400)])
            con.commit()
        finally:
            con.close()
        archive.HOME = home
        archive.ARCHIVE_DIR = td / "arch"; archive.MANIFEST = archive.ARCHIVE_DIR / "manifest.jsonl"
        archive.STORE = archive.ARCHIVE_DIR / "store"; archive.CONFIG = archive.ARCHIVE_DIR / "config.json"
        archive.HEALTH = archive.ARCHIVE_DIR / "capture-health.json"
        archive.ROOTS = [("opencode", ".local/share/opencode/opencode.db", True)]
        archive._SQLITE_SETTLE_S = 0  # image it even though it was just written
        with _archive_test_env(home):
            cap = archive.capture()
            dbp.unlink()  # simulate the store being gone - the premise of restore
            rc = archive.restore("opencode.db")
            con = sqlite3.connect(f"file:{dbp.as_posix()}?mode=ro", uri=True)
            try:
                integ = con.execute("PRAGMA integrity_check").fetchone()[0]
                rows = con.execute("SELECT count(*) FROM part").fetchone()[0]
            finally:
                con.close()
    finally:
        for k, v in saved.items():
            setattr(archive, k, v)
        import shutil; shutil.rmtree(td, ignore_errors=True)
    ok = cap["full"] == 1 and rc == 0 and integ == "ok" and rows == 400
    return ("PASS" if ok else "FAIL",
            f"captured={cap['full']} restore_rc={rc} integrity={integ} rows={rows}/400")


def t_tool_rows():
    import json as _json
    import tempfile
    from pathlib import Path as P
    import events as ev
    import index_lock as il
    import settings as st
    saved = ev.EVENTS_DIR, st.SETTINGS_PATH, il.INDEX_LOCK_PATH
    with tempfile.TemporaryDirectory() as td:
        try:
            ev.EVENTS_DIR = P(td) / "events"
            st.SETTINGS_PATH = P(td) / "settings.json"
            il.INDEX_LOCK_PATH = P(td) / ".index.lock"
            ev.EVENTS_DIR.mkdir()
            evs = [
                {"ts": 50, "kind": "tool", "name": "Bash", "input": "make test",
                 "output": "2 passed", "ok": True, "call_id": "c1", "child": ""},
                {"ts": 150, "kind": "tool", "name": "Read", "input": "src/x.py",
                 "output": "boom", "ok": False, "call_id": "c2", "child": ""},
                {"ts": 0, "kind": "tool", "name": "Grep", "input": "needle",
                 "output": "", "ok": None, "call_id": "c3", "child": ""},
                {"ts": 160, "kind": "tool", "name": "send_message",
                 "input": "review the allocator", "output": "", "ok": True,
                 "call_id": "c4", "child": ""},
                {"ts": 170, "kind": "control", "name": "send_message",
                 "input": '{"message":"gAAAAABopaque"}', "output": "", "ok": True,
                 "call_id": "c5", "child": ""},
            ]
            ev.event_path_candidates("codex", "sess1")[0].write_text(
                "".join(_json.dumps(e) + "\n" for e in evs), encoding="utf-8")
            turns = [(100, 0), (200, 1)]  # turn 0 at ts=100, turn 1 at ts=200
            payload = ev.event_blob("codex", "sess1")
            assert payload is not None
            rows = ev.tool_rows_from_payload(payload, turns)
            st.set_setting("tools", "off")
            off = st.setting("tools")
        finally:
            ev.EVENTS_DIR, st.SETTINGS_PATH, il.INDEX_LOCK_PATH = saved
    by_text = {r["text"].split(":")[0]: r for r in rows}
    collision_names = [ev.event_filename("gemini", session)
                       for session in ("a/b", "a?b", "Session", "session", "x" * 500)]
    maximum_name = ev.event_filename("a" * 500, "s" * 500)
    candidates = ev.event_path_candidates("gemini", "a/b")
    names_ok = (collision_names[0] != collision_names[1]
                and collision_names[2].lower() != collision_names[3].lower()
                and len(maximum_name.encode()) <= ev.EVENT_FILENAME_MAX_BYTES
                and ev.event_filename("gemini", "a/β?Session") ==
                "gemini-a___Session--0cda50f02df0c11ed4b2e40486c8fdb4"
                "c12efbdb2bbe07be.jsonl"
                and candidates[0].name == collision_names[0]
                and candidates[-1].name == "gemini-a_b.jsonl")
    ok = (len(rows) == 4 and off == "off"
          # ts=50 predates turn 0's ts -> clamps to the first turn
          and by_text["Bash"]["turn"] == 0 and "2 passed" in by_text["Bash"]["text"]
          # ts=150 falls inside turn 0 (100 <= 150 < 200)
          and by_text["Read [failed]"]["turn"] == 0
          # ts=0 (unknown) -> first turn
          and by_text["Grep"]["turn"] == 0
          and "send_message" in by_text
          and all("gAAAAABopaque" not in row["text"] for row in rows)
          and names_ok)
    return ("PASS" if ok else "FAIL",
            f"rows={len(rows)} off={off!r} "
            f"turns={[r['turn'] for r in rows]} failed-marked={'Read [failed]' in by_text} "
            f"filenames={names_ok}")


def t_streamed_first_hit():
    """The cold-ingest stream prints its first hit and preserves side-session identity."""
    import contextlib
    import io
    import json
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace

    import search

    # The live binary stamps its normalize-pass classification on every
    # streamed row; the reader consumes `who` instead of re-deriving it.
    rows = [
        {"row": {"agent": "codex", "session": "side-chat", "turn": 1,
                 "project": "/work/agrep", "ts": 1, "who": "subagent",
                 "side": True,
                 "text": "first Needle from a side task", "reply": ""}},
        {"row": {"agent": "codex", "session": "old-binary", "turn": 2,
                 "project": "/work/agrep", "ts": 2, "who": "user",
                 "text": "second Needle from an old binary", "reply": ""}},
        {"row": {"agent": "codex", "session": "control-chat", "turn": 3,
                 "project": "/work/agrep", "ts": 3, "who": "control",
                 "model": "", "text": "continue", "reply": ""}},
        {"row": {"agent": "codex", "session": "terms-chat", "turn": 4,
                 "project": "/work/agrep", "ts": 4, "who": "user", "model": "",
                 "text": "alpha lives far from beta", "reply": ""}},
    ]

    class FakeProc:
        def __init__(self):
            self.stdout = io.BytesIO(b"".join(
                json.dumps(row).encode("utf-8") + b"\n" for row in rows))

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            pass

    import subprocess as sp
    completed_queries = []
    saved = (search.common.MESSAGES_PATH, search.common.INGEST_SIG_PATH,
             search.indexd_runtime.finish_streamed_index,
             search._stream_publication_committed, search.run_query, sp.Popen)
    freshness_story = search.indexd_runtime.freshness_story
    with tempfile.TemporaryDirectory() as td:
        try:
            messages = Path(td) / "messages.jsonl"
            messages.write_text("", encoding="utf-8")
            signature = Path(td) / ".ingest.sig"
            signature.write_text(f"{len(rows)}:fixture\n", encoding="utf-8")
            search.common.MESSAGES_PATH = messages
            search.common.INGEST_SIG_PATH = signature
            search.indexd_runtime.finish_streamed_index = lambda **_kw: None
            search._stream_publication_committed = lambda: True
            search.indexd_runtime.freshness_story = (
                lambda: search.surface.FreshnessStory("current"))
            sp.Popen = lambda *a, **kw: FakeProc()

            def fake_run(q, **kw):
                completed_queries.append(q)
                who = kw.get("who")
                def hit(session, turn, role, snippet):
                    return {"session": session, "agent": "codex", "project": "/work/agrep",
                            "turn": turn, "who": role, "snippet": snippet}
                candidates = {
                    "needle": [
                        hit("side-chat", 1, "subagent", "first Needle from a side task"),
                        hit("old-binary", 2, "user", "second Needle from an old binary"),
                    ],
                    "continue": [hit("control-chat", 3, "control", "continue")],
                    "alpha beta": [hit("terms-chat", 4, "user",
                                       "alpha lives far from beta")],
                    "toolneedle": [hit("tool-chat", 5, "tool", "toolneedle output")],
                }.get(q, [])
                if who:
                    candidates = [row for row in candidates if row["who"] == who]
                since = kw.get("since_ms")
                if since is not None:
                    candidates = []
                return {"hits": candidates, "total": len(candidates),
                        "chats": len({row["session"] for row in candidates}),
                        "engine": "fixture", "mode": kw.get("mode", "keyword")}

            search.run_query = fake_run
            base = dict(max=0, agent=None, project=None, exclude_project=None,
                        chat=None, sort="score")
            all_out = io.StringIO()
            with contextlib.redirect_stdout(all_out), contextlib.redirect_stderr(io.StringIO()):
                rc_all = search._stream_first_run(
                    "needle", "keyword", SimpleNamespace(**base, who_filter=None), False, None, None)
            side_out = io.StringIO()
            with contextlib.redirect_stdout(side_out), contextlib.redirect_stderr(io.StringIO()):
                rc_side = search._stream_first_run(
                    "needle", "keyword", SimpleNamespace(**base, who_filter="subagent"),
                    False, None, None)
            since_out = io.StringIO()
            with contextlib.redirect_stdout(since_out), contextlib.redirect_stderr(io.StringIO()):
                rc_since = search._stream_first_run(
                    "needle", "keyword", SimpleNamespace(**base, who_filter=None),
                    False, 10, None)
            control_user_out = io.StringIO()
            with contextlib.redirect_stdout(control_user_out), contextlib.redirect_stderr(io.StringIO()):
                rc_control_user = search._stream_first_run(
                    "continue", "keyword", SimpleNamespace(**base, who_filter="user"),
                    False, None, None)
            control_out = io.StringIO()
            with contextlib.redirect_stdout(control_out), contextlib.redirect_stderr(io.StringIO()):
                rc_control = search._stream_first_run(
                    "continue", "keyword", SimpleNamespace(**base, who_filter="control"),
                    False, None, None)
            terms_out = io.StringIO()
            with contextlib.redirect_stdout(terms_out), contextlib.redirect_stderr(io.StringIO()):
                rc_terms = search._stream_first_run(
                    "alpha beta", "keyword", SimpleNamespace(**base, who_filter=None),
                    False, None, None)
            tool_out = io.StringIO()
            with contextlib.redirect_stdout(tool_out), contextlib.redirect_stderr(io.StringIO()):
                rc_tool = search._stream_first_run(
                    "toolneedle", "keyword", SimpleNamespace(**base, who_filter="tool"),
                    False, None, None)
            capped_out = io.StringIO()
            original_loads = search.json.loads
            capped_row_loads = 0

            def counted_loads(payload, *args, **kwargs):
                nonlocal capped_row_loads
                if payload.startswith(b'{"row":'):
                    capped_row_loads += 1
                return original_loads(payload, *args, **kwargs)

            search.json.loads = counted_loads
            try:
                with contextlib.redirect_stdout(capped_out), \
                        contextlib.redirect_stderr(io.StringIO()):
                    rc_capped = search._stream_first_run(
                        "needle", "keyword",
                        SimpleNamespace(**{**base, "max": 1}, who_filter=None),
                        False, None, None)
            finally:
                search.json.loads = original_loads
        finally:
            (search.common.MESSAGES_PATH, search.common.INGEST_SIG_PATH,
             search.indexd_runtime.finish_streamed_index,
             search._stream_publication_committed, search.run_query, sp.Popen) = saved
            search.indexd_runtime.freshness_story = freshness_story

    all_text, side_text = all_out.getvalue(), side_out.getvalue()
    ok = (rc_all == 0 and rc_side == 0 and rc_since == 1 and rc_capped == 0
          and rc_control_user == 1 and rc_control == 0
          and rc_terms == 0 and rc_tool == 0
          and "side-chat\tcodex\tagrep\t1\tsubagent\t" in all_text
          and "old-binary\tcodex\tagrep\t2\tuser\t" in all_text
          and "side-chat" in side_text and "old-binary" not in side_text
          and not since_out.getvalue() and not control_user_out.getvalue()
          and "control-chat" in control_out.getvalue()
          and "terms-chat" in terms_out.getvalue()
          and "tool-chat" in tool_out.getvalue()
          and len(capped_out.getvalue().splitlines()) == 1
          and capped_row_loads == 1
          # a full page skips the exhaustive completion re-scan (all/side/since
          # re-scan; capped does not) - totals disclose "at least" instead
          and completed_queries.count("needle") == 3)
    return ("PASS" if ok else "FAIL",
            f"all-rc={rc_all} side-rc={rc_side} since-rc={rc_since} "
            f"control={rc_control_user}/{rc_control} terms={rc_terms} tool={rc_tool} "
            f"capped={rc_capped}/{completed_queries.count('needle')}/{capped_row_loads} "
            f"labels={('subagent' in all_text, 'user' in all_text)}")


def t_run_early_grammar():
    """Early lazy dispatch preserves the real run grammar and `--` boundary."""
    import importlib.util
    from pathlib import Path
    from types import SimpleNamespace
    from hookless import capture

    spec = importlib.util.spec_from_file_location("agrep_cli_selftest", Path(REPO) / "cli.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    saved = capture.run_captured
    try:
        capture.run_captured = lambda agent, extra, cwd=None: (agent, extra, cwd)
        before = module.cmd_run(SimpleNamespace(
            rest=["--cwd", "/tmp/project", "codex"]))
        after = module.cmd_run(SimpleNamespace(
            rest=["codex", "--cwd", "/tmp/project"]))
        boundary = module.cmd_run(SimpleNamespace(
            rest=["codex", "--", "--cwd", "agent-owned"]))
    finally:
        capture.run_captured = saved
    ok = (before == after == ("codex", [], "/tmp/project")
          and boundary == ("codex", ["--cwd", "agent-owned"], None))
    return ("PASS" if ok else "FAIL",
            f"before={before} after={after} boundary={boundary}")


def t_agent_cli_safety_contracts():
    """Agent-facing output is bounded, safe, role-complete, and unambiguous."""
    import contextlib
    import io
    import json
    import shlex
    import recall
    import search

    natural = search.semantic_query_policy("why did the deployment keep retrying")
    identifier = search.semantic_query_policy("REPLY_CAP")
    gibberish = search.semantic_query_policy("qzxvplmbrt")
    sequence = search.semantic_query_policy("abcdefghijkz")
    ordinary = [search.semantic_query_policy(word)["eligible"]
                for word in ("authentication", "memory")]
    policy = search.semantic_result_policy(
        "deployment kept retrying", [
            {"session": "good", "who": "user", "score": .86},
            {"session": "control", "who": "control", "score": .99},
            {"session": "weak", "who": "user", "score": .2},
        ], requested=10, coverage={"complete": True}, score_kind="cosine")
    semantic_ok = (natural["eligible"] and all(ordinary)
                   and not identifier["eligible"] and not gibberish["eligible"]
                   and not sequence["eligible"]
                   and [r["session"] for r in policy["results"]] == ["good"]
                   and policy["semantic_status"]["filtered"] == {
                       "weak": 1, "noise": 1, "invalid": 0})

    unsafe = "hello\x1b]8;;https://evil\x07click\u202eworld"
    safe = search.terminal_safe(unsafe)
    terminal_ok = ("\x1b" not in safe and "\x07" not in safe
                   and "\u202e" not in safe and "\\u001b" in safe)

    huge = {"query": "q", "engine": "fixture", "hits": [{
        "session": "s", "window": [{"kind": "msg", "text": "x" * 10000}]}]}
    encoded = recall._fit_json_payload(huge, 256)
    budget_ok = len(encoded) <= 256 and isinstance(json.loads(encoded), dict)

    query = 'x"; touch /tmp/agrep-must-not-run; echo "'
    command = recall._command("agrep", "recall", query)
    quote_ok = True if os.name == "nt" else shlex.split(command) == [
        "agrep", "recall", query]
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            search._parse_when("yesterday-ish")
    except SystemExit as exc:
        time_ok = exc.code == 2
    else:
        time_ok = False

    commands = [
        ([sys.executable, "cli.py", "search", "q", "-s", "-E"],
         "mutually exclusive"),
        ([sys.executable, "cli.py", "recall", "q", "--budget", "-1"],
         "--budget"),
        ([sys.executable, "cli.py", "recall", "q", "--who", "tool",
          "--context", "-1"], "--context"),
        ([sys.executable, "cli.py", "around", "deadbeef", "0",
          "--context", "-1"], "--context"),
        ([sys.executable, "cli.py", "around", "deadbeef", "0",
          "--max-chars", "-1"], "--max-chars"),
        ([sys.executable, "cli.py", "around", "deadbeef", "0",
          "--tool-output", "-1"], "--tool-output"),
        ([sys.executable, "cli.py", "index", "--definitely-unknown"],
         "unrecognized argument"),
        ([sys.executable, "cli.py", "remove", "--definitely-unknown"],
         "unrecognized argument"),
    ]
    grammar = []
    for argv, marker in commands:
        result = subprocess.run(argv, cwd=REPO, env=ENV, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=20)
        grammar.append(result.returncode == 2 and marker in result.stderr)

    # Exercise the real recall JSON path, not merely the serializer helper.
    saved = (recall.indexd_runtime.ensure_index, recall.search.run_query,
             recall._windows, recall._expand)
    out = io.StringIO()
    try:
        recall.indexd_runtime.ensure_index = lambda auto=True, **_kw: True
        recall.search.run_query = lambda *_a, **_k: {
            "hits": [{"session": "session1", "turn": 1, "who": "user",
                      "agent": "codex", "project": "p", "ts": 1,
                      "score": 1, "matched": "phrase"}],
            "total": 1, "chats": 1, "engine": "fixture", "mode": "keyword",
        }
        window = {"session": "session1", "center": 1, "agent": "codex",
                  "project": "p", "first_turn": 1, "last_turn": 1,
                  "turns": [{"turn": 1, "ts": 1, "who": "user",
                             "text": "z" * 20000, "reply": "r" * 20000}],
                  "events": []}
        # main calls _windows(selected), whose production return is (hit, window).
        recall._windows = lambda hits, context: [(hits[0], window)]
        recall._expand = lambda pairs, *_a, **_k: pairs
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = recall.main(["needle", "--json", "--budget", "2048",
                              "--hits", "1"])
    finally:
        (recall.indexd_runtime.ensure_index, recall.search.run_query,
         recall._windows, recall._expand) = saved
    actual = out.getvalue()
    actual_budget = (rc == 0 and len(actual) <= 2048
                     and isinstance(json.loads(actual), dict))

    ok = (semantic_ok and terminal_ok and budget_ok and quote_ok and time_ok
          and all(grammar) and actual_budget)
    return ("PASS" if ok else "FAIL",
            f"semantic={semantic_ok} terminal={terminal_ok} budget={budget_ok}/"
            f"{actual_budget} quote={quote_ok} time={time_ok} grammar={grammar}")


def t_embedder_download_concurrency():
    """One pinned first download wins; oversized/crashed partials recover."""
    import hashlib
    import io
    import json
    import tempfile
    import threading
    from pathlib import Path
    import embedder

    payload = b"pinned-model-fixture"
    profile = {
        "id": "fixture", "repo": "fixture/repo", "revision": "abc",
        "dim": 1, "max_seq": 1,
        "files": {"model.bin": (len(payload), hashlib.sha256(payload).hexdigest())},
        "remote_dir": {},
    }
    saved = (embedder.PROFILE, embedder.model_dir,
             embedder.urllib.request.urlopen, embedder.MODEL_DOWNLOAD_WAIT_S)
    calls = []

    class Response(io.BytesIO):
        def getheader(self, name):
            return str(len(self.getvalue())) if name == "Content-Length" else None

    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "model"
            embedder.PROFILE = profile
            embedder.model_dir = lambda: root
            embedder.MODEL_DOWNLOAD_WAIT_S = 5

            def good_urlopen(*_a, **_k):
                calls.append("fetch")
                time.sleep(.05)
                return Response(payload)

            embedder.urllib.request.urlopen = good_urlopen
            errors = []
            roots = []

            def fetch():
                try:
                    roots.append(embedder.ensure_model())
                except BaseException as exc:
                    errors.append(f"{type(exc).__name__}: {exc}")

            threads = [threading.Thread(
                target=fetch, daemon=True) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)
            concurrent = (all(not thread.is_alive() for thread in threads)
                          and not errors
                          and roots == [root, root]
                          and calls == ["fetch"]
                          and (root / "model.bin").read_bytes() == payload
                          and not (root / ".download.lock").exists())

            (root / "model.bin").unlink()
            (root / ".download.lock").write_text(json.dumps({
                "pid": os.getpid(), "at": time.time(), "token": "legacy",
            }), encoding="utf-8")
            embedder.urllib.request.urlopen = lambda *_a, **_k: Response(payload)
            legacy_claim_reclaimed = (
                embedder.ensure_model() == root
                and (root / "model.bin").read_bytes() == payload
                and not (root / ".download.lock").exists())

            (root / "model.bin").unlink()
            orphan = root / ".model.bin.999.dead.part"
            orphan.write_bytes(b"partial")
            embedder.urllib.request.urlopen = lambda *_a, **_k: Response(payload + b"!")
            try:
                embedder.ensure_model()
            except embedder.EmbedderUnavailable:
                overrun = True
            else:
                overrun = False
            recovered_debris = (not orphan.exists()
                                and not list(root.glob(".*.part"))
                                and not (root / ".download.lock").exists())
            embedder.urllib.request.urlopen = lambda *_a, **_k: Response(payload)
            repaired = embedder.ensure_model() == root and (root / "model.bin").exists()
    finally:
        (embedder.PROFILE, embedder.model_dir,
         embedder.urllib.request.urlopen, embedder.MODEL_DOWNLOAD_WAIT_S) = saved

    ok = concurrent and legacy_claim_reclaimed and overrun and recovered_debris and repaired
    return ("PASS" if ok else "FAIL",
            f"concurrent={concurrent} legacy={legacy_claim_reclaimed} "
            f"overrun={overrun} debris={recovered_debris} "
            f"repaired={repaired} calls={calls} roots={len(roots)} errors={errors}")


def t_semworker_upgrade_and_reindex_signature():
    """Old residents retire by identity; partial embeddings never become sticky."""
    import importlib.util
    import json
    import tempfile
    from pathlib import Path
    import common
    import semworker

    saved_data = common.DATA_DIR
    checks = {}
    lifetime = None
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        common.DATA_DIR = root
        _establish_current_derived_fixture(root)
        try:
            descriptor = semworker.descriptor_path()
            start = str(common.process_start_identity(os.getpid()) or "")
            lifetime = semworker._acquire_worker_lock(tree_bound=True)
            if lifetime is None:
                return ("FAIL", "semantic worker owner was not acquired")
            owner_nonce = json.loads(lifetime.snapshot.raw)["nonce"]
            old = {
                "version": semworker.PROTOCOL, "pid": os.getpid(), "port": 9,
                "token": "a" * 64, "process_start": start,
                "started_at": time.time(), "tree_bound": True,
                "named_job": common.WIN,
                "owner_nonce": owner_nonce,
                "build_id": "old-build", "capabilities": list(semworker.CAPABILITIES),
            }
            descriptor.write_text(json.dumps(old), encoding="utf-8")
            t0 = time.monotonic()
            checks["old_retired"] = (
                semworker._reconcile_descriptor() is None
                                     and not descriptor.exists()
                                     and time.monotonic() - t0 < 1.0)
            current = {**old, "build_id": semworker.WORKER_BUILD_ID}
            descriptor.write_text(json.dumps(current), encoding="utf-8")
            checks["current_accepted"] = semworker._reconcile_descriptor()[0][
                "build_id"] == semworker.WORKER_BUILD_ID
        finally:
            if lifetime is not None:
                lifetime.release(tombstone=True, require_stable_mtime=True)
            common.DATA_DIR = saved_data

        spec = importlib.util.spec_from_file_location(
            "agrep_reindex_contract_test", Path(REPO) / "reindex.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sigfile = root / ".reindex.sig"
        original_proof = module._embedding_proof
        try:
            module._embedding_proof = lambda: {"coherent": True}
            module._write_sig(sigfile, "source")
            checks["complete_skips"] = module._verified_signature_matches(
                sigfile, "source")
            module._embedding_proof = lambda: {
                "coherent": False, "searchable": True,
                "coverage": {"indexed": 1, "total": 2}}
            checks["partial_no_skip"] = not module._verified_signature_matches(
                sigfile, "source")
            checks["partial_unstamps"] = not module._publish_completion_signature(
                sigfile, "source", module._embedding_proof()) and not sigfile.exists()
            checks["wheel_no_cargo"] = module._source_checkout() is True
            module.ROOT = root
            checks["installed_no_cargo"] = module._source_checkout() is False
        finally:
            module._embedding_proof = original_proof

    ok = all(checks.values())
    return ("PASS" if ok else "FAIL", f"checks={checks}")


def t_chat_prefix_resolution():
    """UUIDv7 short ids collide across sessions started the same minute, so --chat must
    resolve its prefix to exactly one session (or exit 2 with the same disambiguation
    UX as `agrep around`) instead of silently unioning every prefix match."""
    import contextlib
    import io
    import explore
    import search

    full_a = "01997b2e-aaaa-7000-8000-000000000001"
    full_b = "01997b2e-bbbb-7000-8000-000000000002"
    calls = []

    def fake_resolve(q):
        return search.common.match_session_ids([full_a, full_b], q)

    def fake_run(q, **kw):
        calls.append(kw.get("chat"))
        return {"hits": [{"session": kw.get("chat") or full_a, "agent": "codex",
                          "project": "p", "turn": 1, "who": "user", "snippet": "x"}],
                "total": 1, "chats": 1, "tool_hits": 0,
                "engine": "corpusdb", "mode": "keyword"}

    def run(chat):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = search.main(["x", "--chat", chat, "--classic", "--color", "never"])
        return rc, err.getvalue()

    saved = (search.run_query, search.indexd_runtime.ensure_index,
             search._stream_first_run, explore.resolve_session)
    try:
        search.run_query = fake_run
        search.indexd_runtime.ensure_index = lambda auto=True, **_kw: True
        search._stream_first_run = lambda *a, **kw: None
        explore.resolve_session = fake_resolve
        ambiguous_rc, ambiguous_err = run("01997b2e")
        missing_rc, missing_err = run("ffffffff")
        unique_rc, _ = run("01997b2e-a")
        exact_rc, _ = run(full_b)
    finally:
        (search.run_query, search.indexd_runtime.ensure_index,
         search._stream_first_run, explore.resolve_session) = saved

    ok = (ambiguous_rc == 2 and "is ambiguous (2 sessions)" in ambiguous_err
          and "add a char: 01997b2e-a / 01997b2e-b" in ambiguous_err
          and missing_rc == 2 and "no session matches" in missing_err
          and unique_rc == 0 and exact_rc == 0
          and calls == [full_a, full_b])
    return ("PASS" if ok else "FAIL",
            f"rc={ambiguous_rc}/{missing_rc}/{unique_rc}/{exact_rc} "
            f"resolved={calls} hint={'add a char' in ambiguous_err}")


def t_truncation_disclosure():
    """Search JSON leads with one run envelope; flat discloses on stderr."""
    import contextlib
    import io
    import json as _json
    import search

    res = {}

    def fake_run(q, **kw):
        return {key: ([dict(h) for h in value] if key == "hits" else value)
                for key, value in res.items()}

    def run(argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = search.main(argv)
        return rc, out.getvalue().splitlines(), err.getvalue()

    def hit(i):
        return {"session": f"s{i}", "agent": "codex", "project": "p",
                "turn": i, "who": "user", "snippet": "x"}

    saved = (search.run_query, search.indexd_runtime.ensure_index, search._stream_first_run,
             search.indexd_runtime.agent_freshness_notice)
    try:
        search.run_query = fake_run
        search.indexd_runtime.ensure_index = lambda auto=True, **_kw: True
        search._stream_first_run = lambda *a, **kw: None
        search.indexd_runtime.agent_freshness_notice = lambda: ""

        res.update(hits=[hit(i) for i in range(3)], total=386, chats=159,
                   tool_hits=240, engine="corpusdb", mode="keyword")
        _, cut_json, _ = run(["x", "--json"])
        _, cut_flat, cut_note = run(["x", "--classic", "--color", "never"])

        res.update(total=3, chats=3, tool_hits=0)
        _, full_json, _ = run(["x", "--json"])
        _, _, full_note = run(["x", "--classic", "--color", "never"])

        # -l pages by chat: every chat listed is complete even when hits > rows
        res.update(hits=[hit(1), hit(2)], total=50, chats=2)
        _, chats_json, _ = run(["x", "-l", "--json"])

        res.update(hits=[hit(1), hit(2)], total=2, chats=2, totals_exact=False)
        _, bound_json, _ = run(["x", "--json"])
    finally:
        (search.run_query, search.indexd_runtime.ensure_index, search._stream_first_run,
         search.indexd_runtime.agent_freshness_notice) = saved

    def page(lines):
        records = [_json.loads(line) for line in lines]
        if (not records or records[0].get("kind") != "agrep-meta"
                or any(row.get("kind") == "agrep-meta" for row in records[1:])):
            return {}, []
        return records[0], records[1:]

    cut_meta, cut_rows = page(cut_json)
    full_meta, full_rows = page(full_json)
    chats_meta, chats_rows = page(chats_json)
    bound_meta, bound_rows = page(bound_json)
    required_fields = {"completeness", "freshness", "filter_coverage",
                       "self_exclusion", "semantic_coverage", "engine", "query"}
    page_fields = required_fields | {"semantic", "semantic_integrity",
                                     "tools_excluded"}
    all_hits = [*cut_rows, *full_rows, *chats_rows, *bound_rows]
    ok = (len(cut_rows) == 3
          and all(page_fields.isdisjoint(row) for row in all_hits)
          and all(required_fields <= meta.keys() for meta in (
              cut_meta, full_meta, chats_meta, bound_meta))
          # A cut page names a larger bounded pull, not an agent context bomb.
          and cut_note.startswith(
              "showing 3 of 386 matching rows (240 of them in tool output)")
          and " -n 80 -- x" in cut_note
          and " -n 0 " not in cut_note
          and len(cut_flat) == 3 and cut_flat[0].startswith("s0\tcodex\t")
          and len(full_rows) == 3
          and full_note == ""
          and len(chats_rows) == 2
          and len(bound_rows) == 2
          and cut_meta["completeness"]["total"] == 386
          and bound_meta["completeness"]["total_basis"] == "floor")
    return ("PASS" if ok else "FAIL",
            f"hits={len(cut_rows)}/{len(bound_rows)} note={cut_note!r} "
            f"full_note={full_note!r} "
            f"json_lines={len(cut_json)}/{len(full_json)}/{len(chats_json)}")


def t_pipe_gone_windows():
    """Windows reports a vanished pipe reader (`agrep q | head`) as plain OSError
    errno EINVAL, so the broken-pipe teardown must gate on _pipe_gone, which keeps
    posix semantics identical and never claims unrelated OSErrors."""
    import errno as _errno
    import search

    win_lost = OSError(_errno.EINVAL, "The pipe is being closed")
    premise = not isinstance(win_lost, BrokenPipeError)
    cases = [
        (BrokenPipeError(), "posix", True),
        (OSError(_errno.EPIPE, "x"), "posix", True),
        (win_lost, "nt", True),
        (win_lost, "posix", False),
        (OSError(_errno.ENOENT, "x"), "nt", False),
    ]
    got = []
    saved = os.name
    try:
        for exc, plat, want in cases:
            os.name = plat
            got.append(search._pipe_gone(exc) is want)
    finally:
        os.name = saved
    ok = premise and all(got)
    return ("PASS" if ok else "FAIL", f"premise={premise} cases={got}")


# AGREP_CI: hosted runners have no live corpus or agent procs, so a few tests would
# hard-fail rather than SKIP. Run only tests independent of those live resources.
_CI = bool(os.environ.get("AGREP_CI"))
_CI_SAFE = {
    t_clean_agent_environment,
    t_chat_prefix_resolution,
    t_more_survives_reingest,
    t_count_matches_display,
    t_explicit_mode_age_floor,
    t_handle_resolution_unified,
    t_codex_subagent_live,
    t_jsonl_streaming_filters,
    t_pipe_gone_windows,
    t_windows_hook_argv,
    t_agent_cli_safety_contracts,
    t_agent_setup_consent,
    t_terms_spread_ranking,
    t_archive,
    t_archive_sqlite,
    t_binary,
    t_embed_governor,
    t_block_version,
    t_boundary_compact_contract,
    t_bounded_keyword_rows,
    t_bounded_keyword_heads,
    t_bounded_semantic_source_plan,
    t_canonical_windows,
    t_comment_hygiene,
    t_compact_profile_gate,
    t_concept_metadata_refresh,
    t_dense_probe_gate,
    t_ask_private_entrypoint,
    t_run_early_grammar,
    t_embedder_exact_batch_budget,
    t_embedder_download_concurrency,
    t_embedding_memmap_publication,
    t_embedding_publish_serialization,
    t_enrichment_boundary,
    t_recall_full_prose_skips_tools,
    t_ranking,
    t_hookless_boundary,
    t_public_row_serialization,
    t_legacy_stamp_manifest_slot,
    t_live_state_integrity,
    t_live_cold_parity,
    t_lock_liveness,
    t_managed_hook_registry,
    t_matcher,
    t_age_label_vocabulary,
    t_semantic_partial_coverage,
    t_partial_embedding_accumulation,
    t_truncation_disclosure,
    t_semantic_post_ingest_rebase,
    t_probe_prose_session_count,
    t_prose_fts_lane,
    t_conversation_family_retrieval,
    t_semantic_resident_worker,
    t_semantic_candidate_refs,
    t_embed_claim_recovery,
    t_semantic_idle_release,
    t_semworker_claim_recovery,
    t_semworker_upgrade_and_reindex_signature,
    t_session_retrieval_contract,
    t_snippet_cut_boundary,
    t_stale_writer_nonblocking,
    t_star_delta_row_diff,
    t_snip_stitch_merge,
    t_streamed_first_hit,
    t_tool_rows,
}
_TESTS = [
    ("matcher [\\W_]* punctuation bridge", t_matcher),
    ("selftest starts without ambient agent identity", t_clean_agent_environment),
    ("AGREP_DEBUG trace", t_debug),
    ("sticky working flag", t_working),
    ("Codex subagent live fork boundaries", t_codex_subagent_live),
    ("live-vs-cold ingest parity", t_live_cold_parity),
    ("live watcher state and classifier integrity", t_live_state_integrity),
    ("reply cap (64k)", t_cap),
    ("procscan agent processes", t_procscan),
    ("search engines kw/word/regex", t_engines),
    ("filter pushdown parity", t_filter_parity),
    ("JSONL fallback streams filtered rows", t_jsonl_streaming_filters),
    ("heuristic ranking", t_ranking),
    ("snippet cuts cannot mint boundaries", t_snippet_cut_boundary),
    ("stitched snippet windows merge byte-exactly", t_snip_stitch_merge),
    ("all-terms spread ranks tight first", t_terms_spread_ranking),
    ("-w/-E age floor protects exact hits", t_explicit_mode_age_floor),
    ("boundary ranking and compact continuation", t_boundary_compact_contract),
    ("--more survives background reingest", t_more_survives_reingest),
    ("json rows shed ranking internals", t_public_row_serialization),
    ("machine search skips content fallback", t_count_matches_display),
    ("probes count phrase sessions only", t_probe_prose_session_count),
    ("compact profile gate + semantic compact", t_compact_profile_gate),
    ("one age vocabulary across renderers", t_age_label_vocabulary),
    ("@handle resolution is one policy", t_handle_resolution_unified),
    ("session retrieval has no 40-chat ceiling", t_session_retrieval_contract),
    ("ranked retrieval diversifies root conversation families",
     t_conversation_family_retrieval),
    ("prose FTS excludes tools and stays incremental", t_prose_fts_lane),
    ("star delta row-diffs without FTS churn", t_star_delta_row_diff),
    ("legacy stamp gains absent concept-manifest slot in place", t_legacy_stamp_manifest_slot),
    ("concept publication updates metadata without FTS churn", t_concept_metadata_refresh),
    ("canonical batched windows + event attribution", t_canonical_windows),
    ("embedding mmap publication is coherent and replaceable", t_embedding_memmap_publication),
    ("embedder batches use exact token budgets", t_embedder_exact_batch_budget),
    ("embedding publication serializes complete generations", t_embedding_publish_serialization),
    ("semantic embedding coherence tracks generations exactly", t_semantic_embedding_coherence),
    ("partial semantic coverage is searchable and generation-safe",
     t_semantic_partial_coverage),
    ("partial semantic passes accumulate newest-first without recompute",
     t_partial_embedding_accumulation),
    ("bounded semantic source planning retains only selected text",
     t_bounded_semantic_source_plan),
    ("post-ingest semantic generation rebases without ONNX",
     t_semantic_post_ingest_rebase),
    ("stale lane starts one deduped background embed", t_semantic_fresh_async),
    ("background embed governor yields to battery and load", t_embed_governor),
    ("semantic embed crash claims and failures recover",
     t_embed_claim_recovery),
    ("resident semantic worker is authenticated, serial, and disposable",
     t_semantic_resident_worker),
    ("semantic idle reap releases artifact-only residency",
     t_semantic_idle_release),
    ("semantic worker crash claims and PID reuse recover",
     t_semworker_claim_recovery),
    ("recall hybrid survives misleading keyword phrases", t_recall_escalation),
    ("direct ask entrypoint cannot bypass semantic ownership",
     t_ask_private_entrypoint),
    ("semantic candidate refs are bounded and generation-safe", t_semantic_candidate_refs),
    ("dense probes gate on the calibrated cosine floor", t_dense_probe_gate),
    ("Windows post-index argv round-trip", t_windows_hook_argv),
    ("managed post-index hooks preserve ownership", t_managed_hook_registry),
    ("bag-of-words fallback", t_terms_fallback),
    ("query-echo shadowing", t_query_echo),
    ("bounded keyword heads preserve exhaustive ranking", t_bounded_keyword_heads),
    ("bounded broad rows preserve exact ranking and totals", t_bounded_keyword_rows),
    ("full prose recall skips impossible tool lane", t_recall_full_prose_skips_tools),
    ("stale-serve under live writes", t_stale_serve),
    ("stale reader is nonblocking under writer lock", t_stale_writer_nonblocking),
    ("U+2028 jsonl split", t_u2028),
    ("CLI dispatch", t_cli),
    ("status/doctor --json", t_json_surface),
    ("doctor failure-state renders", t_doctor_states),
    ("recall --json shape", t_recall),
    ("lock liveness (dead reclaim / live keep)", t_lock_liveness),
    ("rust binary installed", t_binary),
    ("hookless boundary (no product imports)", t_hookless_boundary),
    ("enrichment boundary (no core producer references)", t_enrichment_boundary),
    ("block version discipline + fight resolution", t_block_version),
    ("uninstall sentinel teardown contract", t_sentinel_teardown),
    ("agent setup consent contract", t_agent_setup_consent),
    ("comment and test hygiene", t_comment_hygiene),
    ("archive capture/append/restore round-trip", t_archive),
    ("archive sqlite store: image + restore validity", t_archive_sqlite),
    ("tool rows: composition + turn mapping", t_tool_rows),
    ("streamed cold-ingest first hit", t_streamed_first_hit),
    ("early run dispatch preserves grammar", t_run_early_grammar),
    ("agent CLI safety and semantic contracts", t_agent_cli_safety_contracts),
    ("embedder first-download concurrency and recovery",
     t_embedder_download_concurrency),
    ("semantic worker upgrade + reindex signature contracts",
     t_semworker_upgrade_and_reindex_signature),
    ("--chat resolves prefixes like around", t_chat_prefix_resolution),
    ("piped/--json disclose truncation", t_truncation_disclosure),
    ("Windows broken-pipe teardown", t_pipe_gone_windows),
]


def main() -> int:
    """Hand the interpreter back the environment it lent us.

    The read-only guard below is for this run, not for the process: anything
    that runs after selftest in the same interpreter would otherwise inherit a
    data dir every writer refuses, and fail for a reason it cannot see.
    """
    def owned(key: str) -> bool:
        return key.startswith("AGREP_") or key in AGENT_CONTEXT_ENV_KEYS

    inherited = {k: v for k, v in os.environ.items() if owned(k)}
    try:
        return _run()
    finally:
        for key in [k for k in os.environ if owned(k) and k not in inherited]:
            os.environ.pop(key, None)
        os.environ.update(inherited)


def _run() -> int:
    global ENV
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    os.environ["AGREP_RS_BIN"] = RS
    # gates read the box's live corpus but never write it: dev-tree writes
    # tear the installed daemon's caches (mixed-version wedge, twice).
    # Names the protected dir, so sandboxed fixtures still build their own.
    import common
    os.environ["AGREP_DATA_READONLY"] = str(common.DATA_DIR)
    for key in (*AGENT_CONTEXT_ENV_KEYS, "AGREP_PROFILE"):
        os.environ.pop(key, None)
    ENV = {**os.environ, "AGREP_RS_BIN": RS}
    results.clear()
    for name, test in _TESTS:
        if _CI and test not in _CI_SAFE:
            results.append((
                name, "SKIP",
                "ci: needs live corpus / processes"))
            continue
        check(name, test)

    print("\n================ agrep self-test ================")
    npass = nfail = nskip = 0
    for name, outcome, detail in results:
        tag = {
            "PASS": "✓ PASS", "FAIL": "✗ FAIL", "SKIP": "– SKIP",
        }.get(outcome, str(outcome))
        print(f"  {tag}  {name:38} {detail}")
        npass += outcome == "PASS"
        nfail += outcome == "FAIL"
        nskip += outcome == "SKIP"
    print(f"  ---- {npass} passed, {nfail} failed, {nskip} skipped ----")
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
