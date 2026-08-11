"""Phase C capture: launch an agent with ground-truth liveness monitoring.

`agrep run <agent>` wraps the agent CLI in a child process we own. The agent
inherits the real terminal (so its TUI works normally), but we KNOW whether it's
alive or dead — ground-truth `working`/`done` that no file-tailer can provide.

On Windows a Job-bound bootstrap owns the agent tree. On Unix a private process
group does. Both inherit the terminal, relay wrapper signals, drain descendants,
and emit events to LiveWatcher so the live view and live.jsonl sink see them.

The capture daemon also tags the session in the watcher with `pty_pid` and
`pty_launch_ts` so the correlation logic can bind the child to its real session.
"""

from __future__ import annotations

import os
import shutil
import time
from collections import deque

from hookless import _log, native
from hookless.registry import normalize_agent_name

def _find_exe(agent: str) -> str | None:
    spec = native._RESUME.get(agent)
    return shutil.which(spec[0]) if spec is not None else None


def _agent_new_session_argv(agent: str, extra_args: list[str]) -> list[str]:
    """Build the argv to start a NEW session (not resume). Each agent's fresh-start
    command — just the bare executable name + any user-provided extra args."""
    exe = _find_exe(agent)
    if not exe:
        raise FileNotFoundError(f"{agent} CLI not found on PATH")
    return [exe] + extra_args


def run_captured(agent: str, extra_args: list[str], cwd: str | None = None) -> int:
    """Launch `agent` as a child process, monitor its liveness, and emit ground-truth
    working/done events. The agent inherits the real terminal. Returns exit code."""

    agent = normalize_agent_name(agent)
    if agent not in native._RESUME:
        _log.log(f"capture: unsupported agent {native._terminal_safe(agent)!r}")
        return 2
    argv = _agent_new_session_argv(agent, extra_args)
    work_dir = cwd or os.getcwd()

    _log.dbg(f"capture: launching {argv} in {work_dir}", ">")

    launch_ts = time.time()
    state: dict[str, object] = {
        "watcher": None, "correlated": None, "tagged": False, "last_tick": 0.0}

    def tick(proc) -> None:
        if not state["tagged"]:
            try:
                from hookless import live as live_mod
                state["watcher"] = live_mod.watcher()
            except Exception:
                state["watcher"] = None
            watcher = state["watcher"]
            if watcher:
                _tag_session(watcher, agent, work_dir, launch_ts, proc.pid)
            state["tagged"] = True
            _log.dbg(f"capture: child pid={proc.pid}", "*")
        now = time.monotonic()
        if now - float(state["last_tick"]) < 1.0:
            return
        state["last_tick"] = now
        watcher = state["watcher"]
        if watcher:
            try:
                _heartbeat(watcher, agent, work_dir, launch_ts, proc.pid)
                if not state["correlated"]:
                    state["correlated"] = _try_correlate(
                        watcher, agent, work_dir, launch_ts)
                    if state["correlated"]:
                        _log.dbg(
                            f"capture: correlated pty -> real session {state['correlated']}",
                            "*")
            except Exception:
                state["watcher"] = None
    try:
        result = native.run_owned_process(
            argv, cwd=work_dir, relay_signals=True, tick=tick)
        rc = result.returncode
    except FileNotFoundError:
        _log.log(f"capture: {argv[0]!r} not found on PATH")
        return 127
    except OSError as exc:
        _log.log(f"capture: owned process failed: {native._terminal_safe(exc)}")
        rc = 1
    watcher = state["watcher"]
    if watcher:
        _emit_done(
            watcher, agent, work_dir, launch_ts, rc,
            state["correlated"])
    _log.dbg(f"capture: child exited rc={rc}", "<")
    return rc


def _synthetic_key(agent: str, cwd: str, launch_ts: float) -> str:
    return f"pty:{agent}:{int(launch_ts * 1000)}"


def _tag_session(w, agent: str, cwd: str, launch_ts: float, pid: int) -> None:
    """Tag the watcher session with capture metadata for correlation."""
    key = _synthetic_key(agent, cwd, launch_ts)
    from hookless import live as live_mod
    s = w.sessions.setdefault(key, {
        "agent": agent, "session": key, "project": live_mod._project_of(cwd),
        "title": "", "model": "", "last_ts": 0, "state": "thinking",
        "working": True, "turn_n": 0, "pending": {},
        "recent": deque(maxlen=live_mod.PER_SESSION),
    })
    s["pty_pid"] = pid
    s["pty_launch_ts"] = launch_ts
    s["pty_cwd"] = cwd
    s["working"] = True
    s["state"] = "thinking"
    s["state_ts"] = int(launch_ts * 1000)


def _heartbeat(w, agent: str, cwd: str, launch_ts: float, pid: int) -> None:
    """Keep the session marked alive while the child runs."""
    key = _synthetic_key(agent, cwd, launch_ts)
    s = w.sessions.get(key)
    if s:
        now = time.time()
        s["last_ts"] = int(now * 1000)
        s["live_ts"] = now * 1000
        s["working"] = True
        w._last_event_wall = now


def _try_correlate(w, agent: str, cwd: str, launch_ts: float) -> str | None:
    """Try to bind the synthetic pty session to a real session the watcher discovered.
    Matches on: same agent, cwd matches, and session appeared AFTER the capture launched."""
    from hookless import live as live_mod
    syn_key = _synthetic_key(agent, cwd, launch_ts)
    launch_ms = int(launch_ts * 1000)
    cwd_norm = cwd.replace("\\", "/").rstrip("/").lower()
    for key, s in w.sessions.items():
        if key == syn_key or key.startswith("pty:"):
            continue
        if s.get("agent") != agent:
            continue
        if s.get("_correlated"):
            continue
        sess_ts = s.get("last_ts", 0)
        if sess_ts < launch_ms - 5000:
            continue
        sess_cwd = (s.get("pty_cwd") or "").replace("\\", "/").rstrip("/").lower()
        if not sess_cwd:
            recent = s.get("recent") or []
            for ev in recent:
                p = (ev.get("project") or "")
                if p and p != "~":
                    proj = live_mod._project_of(cwd)
                    if p == proj:
                        _rebind(w, syn_key, key, s)
                        return s.get("session")
            continue
        if sess_cwd == cwd_norm:
            _rebind(w, syn_key, key, s)
            return s.get("session")
    return None


def _rebind(w, syn_key: str, real_key: str, real_session: dict) -> None:
    """Transfer capture metadata from the synthetic session to the real one."""
    syn = w.sessions.get(syn_key)
    if syn:
        real_session["pty_pid"] = syn.get("pty_pid")
        real_session["pty_launch_ts"] = syn.get("pty_launch_ts")
        real_session["pty_cwd"] = syn.get("pty_cwd")
        real_session["working"] = syn.get("working", True)
        real_session["_correlated"] = True
        del w.sessions[syn_key]


def _emit_done(w, agent: str, cwd: str, launch_ts: float, rc: int | None,
               correlated_session: str | None = None) -> None:
    """Mark the session done when the child exits."""
    key = _synthetic_key(agent, cwd, launch_ts)
    now_ms = int(time.time() * 1000)
    targets = []
    s = w.sessions.get(key)
    if s:
        targets.append(s)
    if correlated_session:
        for sess in w.sessions.values():
            if sess.get("session") == correlated_session:
                targets.append(sess)
    for s in targets:
        s["working"] = False
        s["state"] = f"exited ({rc})" if rc else "done"
        s["state_ts"] = now_ms
        s["last_ts"] = now_ms
