"""`agrep summary` - per-project briefings, pending items and estimated active time.

Black-box: the fixture store under py/fixtures/summary is ingested by the real
binary into an isolated sandbox, then `python py/summary.py ...` is driven as a
subprocess. Timestamps in the fixtures are absolute (March 2026) except one
chat templated relative to now, which proves the default 7d window.
"""

from __future__ import annotations
from _test_support import isolate_data_dir

isolate_data_dir()

import contextlib
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "py" / "fixtures" / "summary"
TIME_TEMPLATE = re.compile(r"\{\{t((?:[+-]\d+[dhms])+)\}\}")
# `{{repeat:z:70000}}`: a block past the 64,000-char reply cap, without a 70 KB fixture file
REPEAT_TEMPLATE = re.compile(r"\{\{repeat:([^:}]+):(\d+)\}\}")
MINUTE = 60_000

S1 = "a1a1a1a1-0001-4000-8000-000000000001"  # atlas: 5m gap, 2h gap capped, 1m event tail
S2 = "a2a2a2a2-0002-4000-8000-000000000002"  # atlas: 15m across local midnight, open next steps
S3 = "b3b3b3b3-0003-4000-8000-000000000003"  # beacon: root + overlapping side chat, open todo
S3_SIDE = "agent-b3side0001"
S4 = "c4c4c4c4-0004-4000-8000-000000000004"  # cairn/codex: everything checked off
S5 = "c5c5c5c5-0005-4000-8000-000000000005"  # cairn: no reply, failed tool
S6 = "c6c6c6c6-0006-4000-8000-000000000006"  # cairn: todos all completed, one ts=0 turn
S7 = "d7d7d7d7-0007-4000-8000-000000000007"  # atlas, recent: the calling agent's own chat
S8 = "b8b8b8b8-0008-4000-8000-000000000008"  # beacon: done list + sign-off, nothing pending
S9 = "e9e9e9e9-0009-4000-8000-000000000009"  # cedar/claude: project label is the basename
S10 = "f0f0f0f0-0010-4000-8000-000000000010"  # cedar/pi: project label is the full cwd path
S11 = "d1d1d1d1-0011-4000-8000-000000000011"  # delta: subagent handed back, then a clean reply
S11_SIDE = "agent-d1side0001"
S12 = "e2e2e2e2-0012-4000-8000-000000000012"  # echo: background subagent outlives the reply
S12_SIDE = "agent-e2side0001"
S13 = "f3f3f3f3-0013-4000-8000-000000000013"  # foxtrot: capped todo list, all done, clean reply
S14 = "f4f4f4f4-0014-4000-8000-000000000014"  # foxtrot: capped todo list with open items
S15 = "g5g5g5g5-0015-4000-8000-000000000015"  # golf: next-steps plan mid-turn, then completion
S16 = "g6g6g6g6-0016-4000-8000-000000000016"  # golf: next-steps section ends the reply
S17 = "h7h7h7h7-0017-4000-8000-000000000017"  # hotel: parent transcript never indexed
S17_SIDE = "agent-h7side0001"
MARCH = ("--since", "2026-03-01", "--until", "2026-03-20")
APRIL = ("--since", "2026-04-01", "--until", "2026-04-30")
DAY_MS = 86_400_000
# the per-agent matrix (May/June 2026): finished, finished after compaction, open native todo,
# open next steps, waiting on user, subagent handed back, background subagent still running
MATRIX = ("--since", "2026-05-01", "--until", "2026-06-30")
MX = {
    "claude": {
        "finished": "ca000001-0501-4000-8000-000000000501",
        "compacted": "ca000002-0502-4000-8000-000000000502",
        "todo": "ca000003-0503-4000-8000-000000000503",
        "next_steps": "ca000004-0504-4000-8000-000000000504",
        "waiting": "ca000005-0505-4000-8000-000000000505",
        "handed_back": "ca000006-0506-4000-8000-000000000506",
        "background": "ca000007-0507-4000-8000-000000000507",
        "background_side": "agent-ca07side01",
    },
    "omp": {
        "finished": "om000001-0521-4000-8000-000000000521",
        "compacted": "om000002-0522-4000-8000-000000000522",
        "todo": "om000003-0523-4000-8000-000000000523",
        "next_steps": "om000004-0524-4000-8000-000000000524",
        "waiting": "om000005-0525-4000-8000-000000000525",
        "handed_back": "om000006-0526-4000-8000-000000000526",
        "background": "om000007-0527-4000-8000-000000000527",
        "background_side": "om0007s1-0527-4000-8000-00000000s527",
    },
    "codex": {
        "finished": "cx000001-0541-4000-8000-000000000541",
        "compacted": "cx000002-0542-4000-8000-000000000542",
        "todo": "cx000003-0543-4000-8000-000000000543",
        "next_steps": "cx000004-0544-4000-8000-000000000544",
        "waiting": "cx000005-0545-4000-8000-000000000545",
        "handed_back": "cx000006-0546-4000-8000-000000000546",
        "background": "cx000007-0547-4000-8000-000000000547",
        "background_side": "01990707-0001-7000-8000-000000000701",
    },
    "opencode": {
        "finished": "ses_oc000001mx",
        "compacted": "ses_oc000002mx",
        "todo": "ses_oc000003mx",
        "next_steps": "ses_oc000004mx",
        "waiting": "ses_oc000005mx",
        "handed_back": "ses_oc000006mx",
        "background": "ses_oc000007mx",
        "background_side": "ses_oc000007sx",
    },
}
MX_CLAUDE_NONE_BULLET = "ca000008-0508-4000-8000-000000000508"  # "## Remaining" + "- None"
MX_CLAUDE_MID_COMPACTION = "ca000009-0509-4000-8000-000000000509"  # recap, then a question
MX_CLAUDE_COMPACTED_MID_TURN = "ca000010-0510-4000-8000-000000000510"  # auto-compact mid-turn
MX_CLAUDE_NONE_TAILS = (  # "- None remaining." / "- None for now." / "- None left." / "- None at the moment."
    "ca000011-0511-4000-8000-000000000511", "ca000012-0512-4000-8000-000000000512",
    "ca000013-0513-4000-8000-000000000513", "ca000014-0514-4000-8000-000000000514")
MX_OMP_CAPPED_INIT = "om000008-0528-4000-8000-000000000528"  # 14-item init cut by the event cap
MX_OMP_COMPACTED_MID_TURN = "om000009-0529-4000-8000-000000000529"
MX_CODEX_WAIT_TIMED_OUT = "cx000008-0548-4000-8000-000000000548"
MX_CODEX_WAIT_TIMED_OUT_SIDE = "01990808-0001-7000-8000-000000000801"
MX_CODEX_SHORT_HAND_BACK = "cx000009-0549-4000-8000-000000000549"  # completed text is 22 chars
# self-mx, timed relative to now: the caller's own chat auto-compacted mid-turn at t-90m
SELF_TOOLS_ONLY = "db000001-0601-4000-8000-000000000601"  # only a tool call after the recap
SELF_LIVE_PROMPT = "db000002-0602-4000-8000-000000000602"  # a live-window prompt at t-70m
# codex compacted mid-turn: each compaction is an empty-text recap row
MX_CODEX_COMPACTED_FINISHED = "cx000010-0550-4000-8000-000000000550"
MX_CODEX_COMPACTED_NEXT_STEPS = "cx000011-0551-4000-8000-000000000551"
MX_CODEX_COMPACTED_BETWEEN = "cx000012-0552-4000-8000-000000000552"  # 09:00, 09:30, 10:00
# claude synthetic API-error rows: alone, after work, after a sentence, in a subagent
MX_CLAUDE_API_ERROR_ONLY = "ca000015-0515-4000-8000-000000000515"
MX_CLAUDE_USAGE_LIMIT = "ca000016-0516-4000-8000-000000000516"
MX_CLAUDE_PROMPT_TOO_LONG = "ca000017-0517-4000-8000-000000000517"
MX_CLAUDE_SIDE_API_ERROR = "ca000021-0521-4000-8000-000000000521"
MX_CLAUDE_SIDE_API_ERROR_SIDE = "agent-ca21side01"
# finished claude replies that only quote an error: mid-sentence, as a sentence opener, own line
MX_CLAUDE_QUOTED_ERRORS = ("ca000022-0522-4000-8000-000000000522",
                           "ca000023-0523-4000-8000-000000000523",
                           "ca000024-0524-4000-8000-000000000524")
# finished claude replies ending on an error-like phrase with no flagged error row behind them
MX_CLAUDE_PROSE_ERRORS = {
    "ca000025-0525-4000-8000-000000000525": "The failure was Request timed out",
    "ca000026-0526-4000-8000-000000000526": "We could not reach it: Unable to connect to API",
    "ca000027-0527-4000-8000-000000000527": "The real row: Prompt is too long",
    "ca000028-0528-4000-8000-000000000528": "Their key was rejected. Credit balance is too low",
}
# a flagged error row the same turn went on past with more assistant text
MX_CLAUDE_ERROR_THEN_TEXT = "ca000029-0529-4000-8000-000000000529"
# background subagents and the root's task notifications: linked by the notified agent id, linked
# by the launch's tool-use id, and only unlinked or stale notices
MX_CLAUDE_NOTIFIED_BY_AGENT = "ca000030-0530-4000-8000-000000000530"
MX_CLAUDE_NOTIFIED_BY_LAUNCH = "ca000031-0531-4000-8000-000000000531"
MX_CLAUDE_NOTIFIED_UNLINKED = "ca000032-0532-4000-8000-000000000532"
MX_CLAUDE_NOTIFIED_UNLINKED_SIDE = "agent-ca32side01"
# a next-steps list, then a later reply block: a completion, or only a sign-off
MX_CLAUDE_GLUED_BULLET = "ca000033-0533-4000-8000-000000000533"
MX_CLAUDE_LIST_THEN_SIGN_OFF = "ca000035-0535-4000-8000-000000000535"
# claude: commentary, a tool call, then nothing; a tool call, then the final text; the final
# text, then only a todo write closing the list
MX_CLAUDE_TOOL_AFTER_TEXT = "ca000036-0536-4000-8000-000000000536"
MX_CLAUDE_TEXT_AFTER_TOOL = "ca000037-0537-4000-8000-000000000537"
MX_CLAUDE_TODO_AFTER_TEXT = "ca000041-0541-4000-8000-000000000541"
# replies past the 64,000-char cap: the final block asks, finishes, or ran past the kept tail
MX_CLAUDE_CAPPED_QUESTION = "ca000038-0538-4000-8000-000000000538"
MX_CLAUDE_CAPPED_DONE = "ca000039-0539-4000-8000-000000000539"
MX_CLAUDE_CAPPED_TAIL_CUT = "ca000040-0540-4000-8000-000000000540"
# a background agent whose todo list was captured in an unknown shape
MX_CLAUDE_SIDE_TODO_UNKNOWN = "ca000042-0542-4000-8000-000000000542"
MX_CLAUDE_SIDE_TODO_UNKNOWN_SIDE = "agent-ca42side01"
# codex: commentary then a shell call and nothing more; a shell call then the final reply; the
# same shapes inside one millisecond; a commentary row without a timestamp; a rollout whose first
# file ends mid-work and whose resumed file replays it and finishes
MX_CODEX_TOOL_AFTER_TEXT = "cx000014-0554-4000-8000-000000000554"
MX_CODEX_TEXT_AFTER_TOOL = "cx000015-0555-4000-8000-000000000555"
MX_CODEX_SAME_MS_TOOL_LAST = "cx000016-0556-4000-8000-000000000556"
MX_CODEX_SAME_MS_TEXT_LAST = "cx000017-0557-4000-8000-000000000557"
MX_CODEX_UNTIMED_TEXT = "cx000018-0558-4000-8000-000000000558"
MX_CODEX_RESUMED_SPLIT = "cx000020-0560-4000-8000-000000000560"
# the agent finished while the root was still editing: its notification is a queued attachment row
MX_CLAUDE_NOTIFIED_QUEUED = "ca000034-0534-4000-8000-000000000534"
# codex: 09:00 prompt, 09:05 shell, 09:07 text, 09:15 compaction, shells 09:20/09:30/09:38, no reply
MX_CODEX_COMPACTED_NO_REPLY = "cx000013-0553-4000-8000-000000000553"
# parked on a question tool: ExitPlanMode, AskUserQuestion (and one answered), omp ask
MX_CLAUDE_EXIT_PLAN = "ca000018-0518-4000-8000-000000000518"
MX_CLAUDE_ASK = "ca000019-0519-4000-8000-000000000519"
MX_CLAUDE_ASK_ANSWERED = "ca000020-0520-4000-8000-000000000520"
MX_OMP_ASK = "om000010-0530-4000-8000-000000000530"
# July 2026: Claude Code's task tools (TaskCreate/TaskUpdate) and gemini's write_todos
TASKS = ("--since", "2026-07-01", "--until", "2026-07-31")
TASKS_OPEN = "ct000001-0701-4000-8000-000000000701"  # 4 created: 1 done, 1 deleted, 2 open
TASKS_DONE = "ct000002-0702-4000-8000-000000000702"
TASKS_SIDE = "ct000003-0703-4000-8000-000000000703"  # a background subagent's own tasks stay open
TASKS_SIDE_CHAT = "agent-ct03side01"
TASKS_UNSEEN_ID = "ct000004-0704-4000-8000-000000000704"  # updates a task it never created
TASKS_AFTER_TODOWRITE = "ct000005-0705-4000-8000-000000000705"  # resumed with task tools on
TODOWRITE_AFTER_TASKS = "ct000006-0706-4000-8000-000000000706"  # resumed with task tools off
GEMINI_TODOS_OPEN = "9e000001-0713-4000-8000-000000000713"
GEMINI_TODOS_DONE = "9e000002-0714-4000-8000-000000000714"
GEMINI_PLAN_WORK_CLOSER = "9e000003-0715-4000-8000-000000000715"
GEMINI_TODO_SIGN_OFF = "9e000004-0716-4000-8000-000000000716"
MX_CLAUDE_PLAN_COMPACT_CLOSER = "ca000053-0553-4000-8000-000000000553"
MX_CODEX_PLAN_COMPACT_CLOSER = "cx000031-0571-4000-8000-000000000571"
MX_OMP_PLAN_COMPACT_CLOSER = "om000011-0531-4000-8000-000000000531"
# one task list per Claude session, shared with its subagents: a subagent closes the root's tasks
# (the root's TaskList confirms), creates tasks the root closes, or closes them as the root's last
# step; a TaskList also restores a list an unresolved id made unknown
TASKS_KIN_CLOSES = "ct000007-0707-4000-8000-000000000707"
TASKS_KIN_CREATES = "ct000008-0708-4000-8000-000000000708"
TASKS_KIN_LAST_STEP = "ct000009-0709-4000-8000-000000000709"
TASKS_LISTED = "ct000010-0710-4000-8000-000000000710"
# Claude Code 2.1.289 results end in an `agentId: … (use SendMessage …` trailer: a narrated subagent
# with a short final message, then root -> A -> B where A closes a task after B, or only relays B
TASKS_KIN_NARRATED = "ct000011-0711-4000-8000-000000000711"
TASKS_KIN_NESTED = "ct000012-0712-4000-8000-000000000712"
TASKS_KIN_NESTED_RELAY = "ct000013-0713-4000-8000-000000000713"
# a resumed agent keeps its id: SendMessage resumed it in the background (2.1.84), Agent(resume=)
# never returned (2.1.71), or the resumed run returned and closed the root's task
TASKS_RESUMED_BACKGROUND = "ct000014-0714-4000-8000-000000000714"
TASKS_RESUMED_UNRETURNED = "ct000015-0715-4000-8000-000000000715"
TASKS_RESUMED_RETURNED = "ct000016-0716-4000-8000-000000000716"
TASKS_BATCH_OUT = "ct000017-0717-4000-8000-000000000717"  # A returned, its batch-mate C did not
# 2.1.289 SendMessage resumes keep the id and write an isMeta prompt the index skips: by id with
# resumedAgentId, by a name no family call gave (shown only by activity after the first run
# returned, also past a later prompt), and a resume that waited and handed its result back inline
TASKS_RESUMED_META = "ct000018-0718-4000-8000-000000000718"
TASKS_RESUMED_BY_NAME = "ct000019-0719-4000-8000-000000000719"
TASKS_RESUMED_AFTER_PROMPT = "ct000020-0720-4000-8000-000000000720"
TASKS_RESUMED_INLINE = "ct000021-0721-4000-8000-000000000721"
# an older agentId line with no usage block, then an unreturned Task(resume=) in the same turn
TASKS_RESUMED_OLD_LINE = "ct000022-0722-4000-8000-000000000722"
TASKS_USAGE_CUT_IN_DIGITS = "ct000023-0723-4000-8000-000000000723"  # one run, handed back
# 2.1.289 with toolUseResult: a narrated agent's framed long report, the same one call deeper read
# as text, inline resumes by a name the root gave and by an id cut from the kept input, and by a
# recipient the ingest can't keep, alone and beside a busy background agent
TASKS_FRAMED_REPORT = "ct000024-0724-4000-8000-000000000724"
TASKS_FRAMED_NESTED = "ct000025-0725-4000-8000-000000000725"
TASKS_INLINE_BY_NAME = "ct000026-0726-4000-8000-000000000726"
TASKS_INLINE_BY_LONG_ID = "ct000027-0727-4000-8000-000000000727"
TASKS_INLINE_UNKEPT_NAME = "ct000028-0728-4000-8000-000000000728"
TASKS_INLINE_UNKEPT_BUSY = "ct000029-0729-4000-8000-000000000729"
# cursor todo_write merge=true: statuses by id, content plus a new id, a new id without content
CURSOR_MERGE_STATUS = "k1000001-0715-4000-8000-000000000715"
CURSOR_MERGE_CONTENT = "k2000002-0716-4000-8000-000000000716"
CURSOR_MERGE_UNMATCHED = "k3000003-0717-4000-8000-000000000717"
CURSOR_MERGE_CAPPED = "k4000004-0718-4000-8000-000000000718"  # the last merge passes the event cap
CURSOR_PLAN_EDIT_DONE = "k5000005-0719-4000-8000-000000000719"
CURSOR_PLAN_EDIT_SIGN_OFF = "k6000006-0720-4000-8000-000000000720"
# the ingest keeps a TaskList's listing and a TaskUpdate's fields whole beside the capped display
# text: a listing past the cap, one whose last subject ends in an ellipsis, an update that also
# carries a description
TASKS_LISTED_LONG = "ct000030-0730-4000-8000-000000000730"
TASKS_LISTED_ELLIPSIS = "ct000031-0731-4000-8000-000000000731"
TASKS_UPDATE_DESCRIBED = "ct000032-0732-4000-8000-000000000732"
# kimi's SetTodoList before 2025-12 wrote Pending / In Progress / Done
KIMI_TITLE_CASE = "4b000001-1120-4000-8000-000000001120"
KIMI_WINDOW = ("--since", "2025-11-01", "--until", "2025-11-30")


class SummarySandbox:
    """One isolated, synchronously indexed fixture home."""

    def __init__(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="summary-", dir=os.environ.get("TMPDIR"))
        self.root = Path(self._temp.name).resolve()
        self.home = self.root / "home"
        self.data = self.root / "data"
        for name in ("home", "data", "tmp", "config", "cache", "share", "models", "runtime"):
            (self.root / name).mkdir()
        for name in ("atlas", "beacon", "cairn"):
            (self.home / "projects" / name).mkdir(parents=True)
        self.env = {
            "HOME": str(self.home), "AGREP_HOME": str(self.home),
            "AGREP_DATA_DIR": str(self.data), "TMPDIR": str(self.root / "tmp"),
            "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "TZ": "UTC", "COLUMNS": "120", "NO_COLOR": "1", "TERM": "dumb",
            "AGREP_NO_DAEMON": "1", "AGREP_NO_SEM_WORKER": "1",
            "AGREP_NO_RESIDENT": "1", "AGREP_NO_FETCH": "1",
            "AGREP_CALLER_PUBLICATION_DIR": str(self.root / "no-callers"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "PYTHONNOUSERSITE": "1", "PYTHONUTF8": "1",
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "XDG_DATA_HOME": str(self.root / "share"),
            "AGREP_MODEL_DIR": str(self.root / "models"),
        }
        if "AGREP_RS_BIN" in os.environ:
            self.env["AGREP_RS_BIN"] = os.environ["AGREP_RS_BIN"]
        self._materialize()
        (self.data / "settings.json").write_text('{"embeddings":"off"}\n', encoding="utf-8")

    def _materialize(self) -> None:
        origin = datetime.now(timezone.utc).replace(microsecond=0)

        def timestamp(match: re.Match[str]) -> str:
            seconds = sum(int(amount) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
                          for amount, unit in re.findall(r"([+-]\d+)([dhms])", match[1]))
            return (origin + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        # gemini keeps one JSON document per session; every other store is JSONL
        for source in sorted(path for path in (FIXTURES / "store").rglob("*")
                             if path.suffix in (".jsonl", ".json")):
            relative = source.relative_to(FIXTURES / "store")
            destination = self.home / ("." + relative.parts[0]) / Path(*relative.parts[1:])
            destination.parent.mkdir(parents=True, exist_ok=True)
            content = TIME_TEMPLATE.sub(timestamp, source.read_text(encoding="utf-8"))
            content = content.replace("{{home}}", str(self.home))
            content = REPEAT_TEMPLATE.sub(lambda m: m[1] * int(m[2]), content)
            if "{{" in content:
                raise AssertionError(f"unexpanded fixture template: {source}")
            destination.write_text(content, encoding="utf-8")
        # opencode and cursor keep their chats in SQLite; the cursor path is the Linux one, which
        # the ingest also checks on macOS
        for seed, database in (
                (FIXTURES / "store" / "opencode" / "seed.sql",
                 self.home / ".local" / "share" / "opencode" / "opencode.db"),
                (FIXTURES / "store" / "cursor" / "seed.sql",
                 self.home / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb")):
            database.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(database)
            try:
                connection.executescript(
                    seed.read_text(encoding="utf-8").replace("{{home}}", str(self.home)))
            finally:
                connection.close()

    def spawn(self, command, *, env_overrides=None, executable=None):
        env = dict(self.env)
        for key, value in (env_overrides or {}).items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = str(value)
        return subprocess.run(
            [executable or sys.executable, *command], cwd=self.home, env=env, input="",
            capture_output=True, text=True, encoding="utf-8", errors="strict",
            timeout=120, check=False)

    def summary(self, *argv, env_overrides=None):
        return self.spawn([str(ROOT / "py" / "summary.py"), *argv], env_overrides=env_overrides)

    def index(self) -> None:
        indexed = self.spawn([str(ROOT / "cli.py"), "index"])
        if indexed.returncode:
            raise AssertionError(f"fixture indexing failed:\n{indexed.stdout}{indexed.stderr}")
        if not (self.data / "corpus.db").is_file():
            raise AssertionError("index did not synchronously publish the search database")

    def rust_index(self) -> None:
        """The Rust ingest alone: the publication moves on while the search database lags."""
        binary = self.env.get("AGREP_RS_BIN") or str(ROOT / "target" / "release" / "agrep-rs")
        result = self.spawn(["index", "--agent", "all"], executable=binary)
        if result.returncode:
            raise AssertionError(f"rust ingest failed:\n{result.stdout}{result.stderr}")

    def close(self) -> None:
        self._temp.cleanup()


def _rows(result: subprocess.CompletedProcess[str]) -> tuple[dict, list[dict]]:
    lines = [json.loads(line) for line in result.stdout.splitlines() if line]
    if not lines or lines[0].get("kind") != "agrep-meta":
        raise AssertionError(f"no agrep-meta envelope:\n{result.stdout}{result.stderr}")
    return lines[0], lines[1:]


def _time_table(result: subprocess.CompletedProcess[str]) -> dict[tuple[str, str], dict]:
    _meta, rows = _rows(result)
    return {(row["period"], row["project"]): row for row in rows}


@unittest.skipUnless(os.name == "posix", "the summary sandbox relies on POSIX paths")
class SummaryTests(unittest.TestCase):
    sandbox: SummarySandbox

    @classmethod
    def setUpClass(cls) -> None:
        cls.sandbox = SummarySandbox()
        cls.addClassCleanup(cls.sandbox.close)
        cls.sandbox.index()

    def _ok(self, *argv, env_overrides=None):
        result = self.sandbox.summary(*argv, env_overrides=env_overrides)
        self.assertEqual(result.returncode, 0, f"{argv}\n{result.stdout}{result.stderr}")
        return result

    # --- time math ---

    def test_gaps_below_and_above_the_cap_and_the_event_tail(self) -> None:
        table = _time_table(self._ok("time", *MARCH, "--json"))
        # 5m gap counts 5m; a 2h gap counts the 20m cap; the final turn extends 1m to its tool call
        self.assertEqual(table[("2026-03-10", "atlas")]["estimated_active_ms"], 26 * MINUTE)
        self.assertEqual(table[("2026-03-10", "atlas")]["chats"], 1)
        # codex 7m (no events: no tail) + 3m10s (10s tail to a failed tool) + 10s (todo call)
        self.assertEqual(table[("2026-03-14", "cairn")]["estimated_active_ms"],
                         7 * MINUTE + 190_000 + 10_000)
        self.assertEqual(table[("2026-03-14", "cairn")]["chats"], 3)

    def test_idle_cap_is_a_parameter_disclosed_in_metadata(self) -> None:
        result = self._ok("time", *MARCH, "--idle-cap", "5m", "--json")
        meta, _rows_ = _rows(result)
        self.assertEqual((meta["idle_cap"], meta["idle_cap_ms"]), ("5m", 5 * MINUTE))
        table = _time_table(result)
        self.assertEqual(table[("2026-03-10", "atlas")]["estimated_active_ms"], 11 * MINUTE)

    def test_intervals_split_at_local_midnight(self) -> None:
        utc = _time_table(self._ok("time", *MARCH, "--project", "atlas", "--json"))
        self.assertEqual(utc[("2026-03-11", "atlas")]["estimated_active_ms"], 10 * MINUTE)
        self.assertEqual(utc[("2026-03-12", "atlas")]["estimated_active_ms"], 5 * MINUTE)
        tokyo = self._ok("time", *MARCH, "--project", "atlas", "--json",
                         env_overrides={"TZ": "Asia/Tokyo"})
        meta, _rows_ = _rows(tokyo)
        self.assertIn("+09:00", meta["timezone"])
        table = _time_table(tokyo)
        self.assertNotIn(("2026-03-11", "atlas"), table)
        self.assertEqual(table[("2026-03-12", "atlas")]["estimated_active_ms"], 15 * MINUTE)

    def test_overlapping_side_chat_counts_once(self) -> None:
        result = self._ok("time", *MARCH, "--project", "beacon", "--json")
        meta, rows = _rows(result)
        # root 09:00-09:10 and side 09:02-09:12 union to 12m; 8m of overlap de-duplicated
        self.assertEqual(rows[0]["estimated_active_ms"], 12 * MINUTE)
        self.assertEqual(rows[0]["chats"], 1)
        self.assertEqual(meta["family_dedup_ms"], 8 * MINUTE)
        self.assertEqual((meta["chats"], meta["side_chats"]), (2, 1))
        briefing = self._ok(*MARCH, "--project", "beacon", "--json")
        _meta, projects = _rows(briefing)
        self.assertEqual([chat["session"] for chat in projects[0]["worked_on"]], [S8, S3])
        self.assertEqual(projects[0]["worked_on"][1]["side_chats"], 1)

    def test_side_chat_activity_keeps_its_family_in_a_narrow_window(self) -> None:
        # the root ended 09:10:30; only its side chat (09:02-09:12) was active in this window
        window = ("--since", "2026-03-13 09:11", "--until", "2026-03-13 09:13")
        meta, rows = _rows(self._ok("time", "--project", "beacon", *window, "--json"))
        self.assertEqual((meta["chats"], meta["side_chats"]), (1, 1))
        self.assertEqual([(r["period"], r["project"], r["estimated_active_ms"], r["chats"])
                          for r in rows], [("2026-03-13", "beacon", MINUTE, 1)])
        self.assertEqual(meta["total_estimated_active_ms"], MINUTE)
        _meta, items = _rows(self._ok("pending", "--project", "beacon", *window, "--json"))
        self.assertEqual([(i["session"], i["status"], i["source"]) for i in items],
                         [(S3, "todo_open", "root")])
        _meta, projects = _rows(self._ok("--project", "beacon", *window, "--json"))
        self.assertEqual([(c["session"], c["side_chats"], c["estimated_active_ms"])
                          for c in projects[0]["worked_on"]], [(S3, 1, MINUTE)])

    def test_family_entirely_outside_the_window_is_excluded(self) -> None:
        # root and side chat both end by 09:12:30; a window starting after that admits neither
        result = self.sandbox.summary("time", "--project", "beacon", "--since", "2026-03-13 09:13",
                                      "--until", "2026-03-13 10:00", "--json")
        self.assertNotEqual(result.returncode, 0)
        meta, rows = _rows(result)
        self.assertEqual((rows, meta["hits"], meta["chats"], meta["side_chats"]), ([], [], 0, 0))

    def test_unknown_timestamps_are_excluded_and_counted(self) -> None:
        result = self._ok("time", *MARCH, "--json")
        meta, _rows_ = _rows(result)
        self.assertEqual(meta["unknown_timestamp_rows"], 1)
        self.assertTrue(any("without a usable timestamp" in line for line in meta["caveats"]))
        human = self._ok(*MARCH, "--project", "cairn")
        self.assertIn("1 turn without a usable timestamp excluded", human.stderr)
        _meta, projects = _rows(self._ok(*MARCH, "--project", "cairn", "--json"))
        chats = {chat["session"]: chat for chat in projects[0]["worked_on"]}
        self.assertEqual(chats[S6]["turns"], 1)

    def test_grouping_by_week_and_month(self) -> None:
        week = _time_table(self._ok("time", *MARCH, "--group", "week", "--json"))
        self.assertEqual(week[("2026-W11", "atlas")]["estimated_active_ms"], 41 * MINUTE)
        self.assertEqual(week[("2026-W11", "atlas")]["chats"], 2)
        month = self._ok("time", *MARCH, "--group", "month", "--json")
        meta, rows = _rows(month)
        self.assertEqual(meta["group"], "month")
        self.assertEqual([(row["period"], row["project"]) for row in rows],
                         [("2026-03", "atlas"), ("2026-03", "cedar"), ("2026-03", "beacon"),
                          ("2026-03", "cairn")])
        self.assertEqual(meta["total_estimated_active_ms"], sum(r["estimated_active_ms"] for r in rows))

    # --- pending ---

    def test_each_pending_status_with_confidence_and_handle(self) -> None:
        _meta, items = _rows(self._ok("pending", *MARCH, "--json"))
        by_session = {item["session"]: item for item in items}
        self.assertEqual(
            {s: (i["status"], i["confidence"]) for s, i in by_session.items()},
            {S1: ("waiting_on_user", "high"), S2: ("open_next_steps", "high"),
             S3: ("todo_open", "medium"), S5: ("agent_work_incomplete", "medium")})
        self.assertEqual(by_session[S1]["evidence"],
                         "Should I apply it to the production schema now, or keep it staged?")
        self.assertEqual(by_session[S2]["items"], ["publish the atlas release notes"])
        self.assertEqual(by_session[S3]["items"], ["write the beacon audit report"])
        self.assertIn("latest tool failed (Bash)", by_session[S5]["signals"])
        for item in items:
            self.assertRegex(item["handle"], r"^@[0-9a-f]{8}:\d+\.[0-9a-f]{4}$")
            self.assertEqual(item["source"], "root")
        # ordering: high confidence first, then newest
        self.assertEqual([i["session"] for i in items], [S2, S1, S5, S3])

    def test_explicit_completion_is_not_pending(self) -> None:
        _meta, items = _rows(self._ok("pending", *MARCH, "--project", "cairn", "--json"))
        self.assertEqual([item["session"] for item in items], [S5])
        checked = self.sandbox.summary("pending", *MARCH, "--agent", "codex", "--json")
        self.assertNotEqual(checked.returncode, 0)
        meta, items = _rows(checked)
        self.assertEqual((items, meta["hits"], meta["chats"]), ([], [], 1))

    def test_done_lists_and_sign_offs_are_not_pending(self) -> None:
        # "To do this, I changed:" + bullets is a report; "Let me know if..." is a sign-off
        _meta, items = _rows(self._ok("pending", *MARCH, "--project", "beacon", "--json"))
        self.assertEqual([item["session"] for item in items], [S3])

    def test_pending_human_lines_carry_status_handle_and_evidence(self) -> None:
        result = self._ok("pending", *MARCH)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 4)
        self.assertRegex(
            lines[0],
            r"^HIGH +open_next_steps +@a2a2a2a2:1\.[0-9a-f]{4}  "
            r"1 open: publish the atlas release notes  atlas$")
        self.assertIn(
            "“Should I apply it to the production schema now, or keep it staged?”", lines[1])
        self.assertIn("4 open items across 9 chats", result.stderr)

    def test_side_chat_handed_back_before_the_root_replied_is_not_pending(self) -> None:
        # delta: the subagent's final reply came back as the root's Task result and the root then
        # replied cleanly, so the subagent's unclosed todo list is not the family's latest activity
        finished = self.sandbox.summary("pending", *APRIL, "--project", "delta", "--json")
        self.assertNotEqual(finished.returncode, 0)
        meta, items = _rows(finished)
        self.assertEqual((items, meta["chats"], meta["side_chats"]), ([], 1, 1))
        # echo: a background subagent still working after the root replied does roll up
        _meta, items = _rows(self._ok("pending", *APRIL, "--project", "echo", "--json"))
        self.assertEqual([(i["session"], i["status"], i["source"], i["evidence_session"])
                          for i in items], [(S12, "todo_open", "side-chat", S12_SIDE)])
        self.assertEqual(items[0]["items"], ["rebuild the echo index", "report the echo rebuild"])

    def test_todo_list_past_the_display_cap_is_replayed_whole(self) -> None:
        # both lists exceed the 800-char event cap the display input keeps; the ingest keeps
        # the list facts whole, so S13's all-done list and clean reply finish it and S14 lists
        # every open item, the ones past the cap included, with no caveat
        _meta, items = _rows(self._ok("pending", *APRIL, "--project", "foxtrot", "--json"))
        self.assertEqual([(i["session"], i["status"]) for i in items], [(S14, "todo_open")])
        self.assertEqual(items[0]["items"], [f"migrate the foxtrot {name} service" for name in
                                             ("beta", "gamma", "delta", "epsilon", "zeta", "eta",
                                              "theta")])
        self.assertNotIn("caveats", items[0])

    def test_next_steps_section_counts_only_when_it_ends_the_reply(self) -> None:
        # S15 planned "Next steps", did tool work and finished with "All done.": the terminal
        # block decides, so it is not pending; S16's section ends the reply with only a sign-off
        # after it in the same block, its first bullet two sentences long
        _meta, items = _rows(self._ok("pending", *APRIL, "--project", "golf", "--json"))
        self.assertEqual([(i["session"], i["status"]) for i in items], [(S16, "open_next_steps")])
        self.assertEqual(items[0]["items"], ["publish the golf release notes. They are drafted in docs.",
                                             "tag the golf release"])

    def test_orphan_side_chat_heads_its_own_family_and_says_so(self) -> None:
        meta, projects = _rows(self._ok(*APRIL, "--project", "hotel", "--json"))
        self.assertEqual((meta["chats"], meta["side_chats"]), (1, 0))
        chat = projects[0]["worked_on"][0]
        self.assertEqual((chat["session"], chat["parent"], chat["parent_indexed"], chat["turns"]),
                         (S17_SIDE, S17, False, 2))
        self.assertEqual(projects[0]["estimated_active_ms"], 5 * MINUTE)
        self.assertEqual([(o["session"], o["status"], o["parent"], o["parent_indexed"])
                          for o in projects[0]["open"]], [(S17_SIDE, "todo_open", S17, False)])
        human = self._ok(*APRIL, "--project", "hotel")
        self.assertIn("@agent-h7 claude ", human.stdout)
        self.assertEqual(human.stdout.count("[side chat; parent not indexed]"), 2, human.stdout)
        table = _time_table(self._ok("time", *APRIL, "--project", "hotel", "--json"))
        self.assertEqual(table[("2026-04-06", "hotel")], {
            "kind": "time", "period": "2026-04-06", "group": "day", "project": "hotel",
            "estimated_active_ms": 5 * MINUTE, "estimated_active": "5m", "chats": 1})

    def test_turns_come_from_the_publication_when_the_search_db_lags(self) -> None:
        """The Rust ingest republishes sessions.jsonl and messages.jsonl; the search database
        keeps its older copy until a Python refresh, and one chat must not mix the two."""
        sandbox = SummarySandbox()
        self.addCleanup(sandbox.close)
        sandbox.index()
        path = sandbox.home / ".claude" / "projects" / "-projects-atlas" / f"{S1}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            for stamp, role, text in (("2026-03-10T12:10:00.000Z", "user", "Keep it staged."),
                                      ("2026-03-10T12:10:30.000Z", "assistant",
                                       "Kept the atlas migration staged.")):
                row = {"sessionId": S1, "cwd": str(sandbox.home / "projects" / "atlas"),
                       "type": role, "timestamp": stamp,
                       "message": {"role": role, "content": text}}
                if role == "user":
                    row["userType"] = "external"
                handle.write(json.dumps(row) + "\n")
        sandbox.rust_index()
        _meta, items = _rows(sandbox.summary("pending", *MARCH, "--project", "atlas", "--json"))
        self.assertEqual([(i["session"], i["status"]) for i in items], [(S2, "open_next_steps")])
        _meta, projects = _rows(sandbox.summary(*MARCH, "--project", "atlas", "--json"))
        self.assertEqual(projects[0]["turns"], 6)
        chats = {chat["session"]: chat for chat in projects[0]["worked_on"]}
        self.assertEqual(chats[S1]["turns"], 4)
        self.assertRegex(chats[S1]["latest_handle"], r"^@a1a1a1a1:3\.[0-9a-f]{4}$")

    # --- pending: per-agent matrix and the shapes the third review found ---

    def _pending_by_session(self, *argv) -> dict[str, dict]:
        result = self.sandbox.summary("pending", *MATRIX, *argv, "--json")
        _meta, items = _rows(result)
        return {item["session"]: item for item in items}

    def test_matrix_finished_chats_are_not_pending(self) -> None:
        pending = self._pending_by_session()
        for agent, chats in MX.items():
            with self.subTest(agent=agent, shape="finished"):
                self.assertNotIn(chats["finished"], pending)
            with self.subTest(agent=agent, shape="finished-after-compaction"):
                self.assertNotIn(chats["compacted"], pending)
            with self.subTest(agent=agent, shape="subagent-handed-back"):
                self.assertNotIn(chats["handed_back"], pending)

    def test_matrix_open_shapes_report_their_status(self) -> None:
        pending = self._pending_by_session()
        expected = {
            ("claude", "todo"): ("todo_open", ["write the tango report"]),
            ("omp", "todo"): ("todo_open", ["port the env loader", "port the yaml loader"]),
            ("codex", "todo"): ("todo_open", ["port the papa writer", "update the papa docs"]),
            ("opencode", "todo"): ("todo_open", ["port the env loader"]),
            ("claude", "next_steps"): ("open_next_steps", ["add tests for the tango parser",
                                                           "update the tango docs"]),
            ("omp", "next_steps"): ("open_next_steps", ["wire the romeo reader into the CLI",
                                                        "add romeo fixtures"]),
            ("codex", "next_steps"): ("open_next_steps", ["add tests for the quebec parser",
                                                          "update the quebec docs"]),
            ("opencode", "next_steps"): ("open_next_steps", ["add tests for the tango parser",
                                                             "update the tango docs"]),
            ("claude", "waiting"): ("waiting_on_user", []),
            ("omp", "waiting"): ("waiting_on_user", []),
            ("codex", "waiting"): ("waiting_on_user", []),
            ("opencode", "waiting"): ("waiting_on_user", []),
        }
        for (agent, shape), (status, items) in expected.items():
            with self.subTest(agent=agent, shape=shape):
                item = pending[MX[agent][shape]]
                # the .omp store is read by the pi adapter and carries its label
                self.assertEqual((item["status"], item["items"], item["source"], item["agent"]),
                                 (status, items, "root", "pi" if agent == "omp" else agent))
                self.assertNotIn("caveats", item)

    def test_matrix_background_subagent_rolls_up_from_the_side_chat(self) -> None:
        # claude and omp children spoke before their todo list; codex and opencode children have
        # no reply yet, so their unfinished turn is the evidence
        pending = self._pending_by_session()
        expected = {"claude": "todo_open", "omp": "todo_open",
                    "codex": "agent_work_incomplete", "opencode": "agent_work_incomplete"}
        for agent, status in expected.items():
            with self.subTest(agent=agent):
                item = pending[MX[agent]["background"]]
                self.assertEqual((item["status"], item["source"], item["evidence_session"]),
                                 (status, "side-chat", MX[agent]["background_side"]))
        self.assertEqual(pending[MX["claude"]["background"]]["items"],
                         ["rebuild the whiskey index", "report the whiskey rebuild"])
        self.assertEqual(pending[MX["omp"]["background"]]["items"],
                         ["rebuild the uniform index", "report the uniform rebuild"])

    def test_omp_todo_ops_replay_into_the_current_list(self) -> None:
        # init + start + done(all) ends clean; init/start/done/append/start/block leaves two
        # open items, the blocked one included, and neither chat is an unknown shape
        pending = self._pending_by_session("--project", "mx-omp")
        self.assertNotIn(MX["omp"]["finished"], pending)
        todo = pending[MX["omp"]["todo"]]
        self.assertEqual((todo["status"], todo["confidence"]), ("todo_open", "medium"))
        self.assertEqual(todo["items"], ["port the env loader", "port the yaml loader"])
        self.assertNotIn("caveats", todo)

    def test_codex_update_plan_is_the_chats_todo_list(self) -> None:
        pending = self._pending_by_session("--agent", "codex")
        plan = pending[MX["codex"]["todo"]]
        self.assertEqual((plan["status"], plan["items"]),
                         ("todo_open", ["port the papa writer", "update the papa docs"]))
        self.assertNotIn(MX["codex"]["finished"], pending)

    def _tasks_pending(self, *argv) -> dict[str, dict]:
        _meta, items = _rows(self.sandbox.summary("pending", *TASKS, *argv, "--json"))
        return {item["session"]: item for item in items}

    def test_claude_task_tools_replay_into_the_open_task_list(self) -> None:
        # ids and subjects come from TaskCreate's result; #4 was deleted, the update to #9 was
        # rejected and #3's description-only update kept its status; TaskList/TaskGet only read
        pending = self._tasks_pending("--project", "mx-tasks")
        item = pending[TASKS_OPEN]
        self.assertEqual((item["status"], item["confidence"], item["source"], item["items"]),
                         ("todo_open", "medium", "root",
                          ["Port the yaml loader", "Update the loader docs"]))
        self.assertNotIn("caveats", item)
        self.assertNotIn(TASKS_DONE, pending)

    def test_claude_side_chat_task_list_rolls_up(self) -> None:
        pending = self._tasks_pending("--project", "mx-tasks")
        item = pending[TASKS_SIDE]
        self.assertEqual((item["status"], item["source"], item["evidence_session"], item["items"]),
                         ("todo_open", "side-chat", TASKS_SIDE_CHAT,
                          ["Rebuild the kilo index", "Report the kilo rebuild"]))

    def test_claude_task_update_to_an_unseen_id_is_unknown(self) -> None:
        # Claude accepted an update to #2, which this chat never created: its own tasks all read
        # completed, but that is no proof nothing is open
        item = self._tasks_pending("--project", "mx-tasks")[TASKS_UNSEEN_ID]
        self.assertEqual((item["status"], item["confidence"], item["items"], item["caveats"]),
                         ("unknown", "low", [],
                          ["task list changes a task whose id could not be resolved"]))

    def test_store_written_last_decides_when_todowrite_and_tasks_mix(self) -> None:
        # Claude never enables TodoWrite and the task tools together, so a chat resumed across
        # the switch tracks its work in the store it wrote last; the other one is stale
        pending = self._tasks_pending("--project", "mx-tasks")
        self.assertNotIn(TASKS_AFTER_TODOWRITE, pending)
        item = pending[TODOWRITE_AFTER_TASKS]
        self.assertEqual((item["status"], item["items"]),
                         ("todo_open", ["send the november notes"]))

    def test_gemini_write_todos_items_are_named_by_description(self) -> None:
        pending = self._tasks_pending("--agent", "gemini")
        item = pending[GEMINI_TODOS_OPEN]
        self.assertEqual((item["status"], item["agent"], item["items"]),
                         ("todo_open", "gemini",
                          ["Port the yaml loader", "Update the loader docs"]))
        self.assertNotIn("caveats", item)
        self.assertNotIn(GEMINI_TODOS_DONE, pending)

    def test_claude_family_replays_one_shared_task_list(self) -> None:
        # Claude keys the task list by session id and foreground subagents run under the root's,
        # so an update in either chat changes the list the other one sees
        pending = self._tasks_pending("--project", "mx-tasks")
        for session in (TASKS_KIN_CLOSES, TASKS_KIN_CREATES, TASKS_KIN_LAST_STEP):
            with self.subTest(session=session):
                self.assertNotIn(session, pending)
        # a background subagent's tasks, all made after the root's reply, stay with the side chat
        self.assertEqual(pending[TASKS_SIDE]["source"], "side-chat")

    def test_claude_task_list_result_resets_the_replay(self) -> None:
        # TaskList prints the whole list: the unresolved #2 is known again, #4 keeps the subject
        # the replay knows without its owner, and the blocked-by suffix is no part of a subject
        item = self._tasks_pending("--project", "mx-tasks")[TASKS_LISTED]
        self.assertEqual((item["status"], item["confidence"], item["items"]),
                         ("todo_open", "medium",
                          ["Publish the oscar notes", "Announce the oscar release"]))
        self.assertNotIn("caveats", item)

    def test_task_list_past_the_display_cap_and_one_ending_in_an_ellipsis_reset(self) -> None:
        # the ingest keeps a TaskList's whole listing beside its capped display output, so a
        # listing past 800 chars still resets the replay to the twelve tasks it printed, and a
        # listing whose last subject ends in an ellipsis is not mistaken for a cut one
        pending = self._tasks_pending("--project", "mx-tasks")
        item = pending[TASKS_LISTED_LONG]
        self.assertEqual((item["status"], item["items"][:2], len(item["items"])),
                         ("todo_open", ["Review the papa plan",
                                        "Port the alpha module of the papa service to the new "
                                        "loader API and refresh its docs"], 11))
        self.assertNotIn("caveats", item)
        item = pending[TASKS_LISTED_ELLIPSIS]
        self.assertEqual((item["status"], item["items"]),
                         ("todo_open", ["Write the quebec docs: sections 1\u20133, then\u2026"]))
        self.assertNotIn("caveats", item)

    def test_task_update_carrying_a_description_still_resolves_its_status(self) -> None:
        # the display input keeps only the update's new description; the fields kept whole say
        # #1 completed, so #2 is the one open task rather than an unresolved change
        item = self._tasks_pending("--project", "mx-tasks")[TASKS_UPDATE_DESCRIBED]
        self.assertEqual((item["status"], item["items"]), ("todo_open", ["Write the romeo docs"]))
        self.assertNotIn("caveats", item)

    def test_cursor_todo_merge_updates_items_by_id(self) -> None:
        # Cursor's todo_write merges by id when `merge` is true, leaving unsent fields as they were
        pending = self._tasks_pending("--agent", "cursor")
        expected = {
            CURSOR_MERGE_STATUS: ["Document the retry policy", "Add a changelog entry for retries"],
            CURSOR_MERGE_CONTENT: ["Document the grpc retry policy",
                                   "Add a changelog entry for grpc retries",
                                   "Benchmark the grpc retries"],
        }
        for session, items in expected.items():
            with self.subTest(session=session):
                item = pending[session]
                self.assertEqual((item["status"], item["agent"], item["items"]),
                                 ("todo_open", "cursor", items))
        unmatched = pending[CURSOR_MERGE_UNMATCHED]
        self.assertEqual(
            (unmatched["status"], unmatched["items"], unmatched["caveats"]),
            ("unknown", [], ["todo list merges an item whose id could not be resolved"]))

    def test_claude_agent_result_naming_the_subagent_is_its_hand_back(self) -> None:
        # the subagent narrated before a short final message, so the root's result, which ends in
        # Claude's agentId trailer, matches neither the reply's head nor its whole text
        self.assertNotIn(TASKS_KIN_NARRATED, self._tasks_pending("--project", "mx-tasks"))

    def test_nested_subagent_tasks_count_once_the_root_is_past_them(self) -> None:
        # B hands back to A, never to the root; the root received A after B finished
        pending = self._tasks_pending("--project", "mx-tasks")
        for session in (TASKS_KIN_NESTED, TASKS_KIN_NESTED_RELAY):
            with self.subTest(session=session):
                self.assertNotIn(session, pending)

    def test_an_earlier_runs_result_never_hands_back_a_resumed_agent(self) -> None:
        # the first run's agentId line proves only that run; the resumed one is still working
        pending = self._tasks_pending("--project", "mx-tasks")
        for session, side in ((TASKS_RESUMED_BACKGROUND, "agent-ct14side01"),
                              (TASKS_RESUMED_UNRETURNED, "agent-ct15side01"),
                              (TASKS_RESUMED_OLD_LINE, "agent-a22c0ffee0000beef")):
            with self.subTest(session=session):
                item = pending.get(session, {})
                self.assertEqual(
                    (item.get("status"), item.get("source"), item.get("evidence_session"),
                     item.get("signals")),
                    ("agent_work_incomplete", "side-chat", side,
                     ["side chat has no captured reply to its last turn"]))
        # a resumed run that returned names the agent again, so its task closure counts
        self.assertNotIn(TASKS_RESUMED_RETURNED, pending)

    def test_a_hand_back_counts_for_the_root_only_once_its_batch_returned(self) -> None:
        # Claude goes on only after every agent call of a batch returned; C's is still out, so
        # the root never saw C close #2, while A's closure came back with A's result
        item = self._tasks_pending("--project", "mx-tasks").get(TASKS_BATCH_OUT, {})
        self.assertEqual((item.get("status"), item.get("source"), item.get("items")),
                         ("todo_open", "root", ["Port the yaml reader"]))

    def test_a_resume_with_an_unrecorded_prompt_supersedes_the_earlier_result(self) -> None:
        # the resumed agent closed #1 after the root's last moment and is still working; the
        # first run's agentId line and its reply text speak only for that run
        item = self._tasks_pending("--project", "mx-tasks").get(TASKS_RESUMED_META, {})
        self.assertEqual((item.get("status"), item.get("source"), item.get("items")),
                         ("todo_open", "root", ["Update the csv writer docs"]))

    def test_side_activity_after_the_returned_run_ended_supersedes_its_result(self) -> None:
        # nothing in the root ties the agent to its later run: its activity past the moment the
        # call that returned its first run came back, or past the root's next prompt, is a later run
        pending = self._tasks_pending("--project", "mx-tasks")
        for session, task in ((TASKS_RESUMED_BY_NAME, "Update the json writer docs"),
                              (TASKS_RESUMED_AFTER_PROMPT, "Update the yaml writer docs")):
            with self.subTest(session=session):
                item = pending.get(session, {})
                self.assertEqual((item.get("status"), item.get("source"), item.get("items")),
                                 ("todo_open", "root", [task]))
        # a resume that waited returns the run's result inline, which hands that run back
        self.assertNotIn(TASKS_RESUMED_INLINE, pending)
        # a duration the cap cut inside its digits bounds nothing: this run closed #1 and returned
        self.assertNotIn(TASKS_USAGE_CUT_IN_DIGITS, pending)

    def test_framed_long_report_hands_back_through_the_stored_link(self) -> None:
        # the cap cuts a framed long report's agentId line and the frame hides its head; the
        # agentId and status the ingest kept from toolUseResult still prove the hand-back
        self.assertNotIn(TASKS_FRAMED_REPORT, self._tasks_pending("--project", "mx-tasks"))

    def test_inline_resume_by_name_or_cut_id_hands_back_through_the_stored_link(self) -> None:
        # the ingest keeps each message's recipient and inline hand-back uncapped, and each agent
        # call's name and agentId, so neither a name nor a cut input hides the resumed run
        pending = self._tasks_pending("--project", "mx-tasks")
        # what stays open in these families is the background agent, still running
        for session, busy in ((TASKS_INLINE_BY_NAME, "agent-a26c0ffee0000c0de"),
                              (TASKS_INLINE_BY_LONG_ID, "agent-a27c0ffee0000c0de")):
            with self.subTest(session=session):
                item = pending.get(session, {})
                self.assertEqual(
                    (item.get("status"), item.get("source"), item.get("evidence_session")),
                    ("agent_work_incomplete", "side-chat", busy))

    def test_framed_report_read_as_text_still_hands_back(self) -> None:
        # a call made inside a subagent keeps no toolUseResult: past the frame's header the
        # result still carries the head of the nested agent's reply
        self.assertNotIn(TASKS_FRAMED_NESTED, self._tasks_pending("--project", "mx-tasks"))

    def test_inline_resume_tied_to_no_agent_is_the_one_run_it_blocked_on(self) -> None:
        # an inline resume holds the root until the run ends, so the one agent active only in
        # that span is the run it handed back; with a second agent busy then, nothing is proven
        pending = self._tasks_pending("--project", "mx-tasks")
        self.assertNotIn(TASKS_INLINE_UNKEPT_NAME, pending)
        item = pending.get(TASKS_INLINE_UNKEPT_BUSY, {})
        self.assertEqual((item.get("status"), item.get("source"), item.get("items")),
                         ("todo_open", "root", ["Update the xml reader docs"]))

    def test_events_without_stored_results_keep_the_text_rules(self) -> None:
        # an event file written before the ingest kept toolUseResult has no `child` or `meta`
        import summary

        def chat(session: str, turns: list[int], events: list[dict]) -> summary._Chat:
            out = summary._Chat(session=session, agent="claude", project="p", root="r",
                                side=session != "r", first_ts=0, last_ts=0, first_text="")
            out.turns = [summary._Turn(turn=n, ts=ts, who="user", text="go", digest=None)
                         for n, ts in enumerate(turns)]
            out.events = events
            return out

        agent = "a30c0ffee0000beef"
        returned = {"kind": "subagent_start", "name": "Agent", "ts": 1_000, "ok": True,
                    "output": f"Done.\nagentId: {agent} (use SendMessage with to: '{agent}') "
                              f"\n<usage>tool_uses: 1\nduration_ms: 5000</usage>"}
        tool = {"kind": "tool", "name": "Read", "ok": True}
        for later, handed_back in ((5_500, True), (9_000, False)):
            with self.subTest(later=later):
                side = chat(f"agent-{agent}", [1_001], [{**tool, "ts": later}])
                root = chat("r", [0], [returned])
                root.task_kin, side.task_kin = [side], [root]
                # past the run's reported duration, the activity is a later run
                self.assertIs(summary._agent_returned(root, side), handed_back)
        message = {"kind": "tool", "name": "SendMessage", "ts": 2_000, "ok": True,
                   "input": json.dumps({"message": "m", "to": agent}), "output": "{}"}
        side = chat(f"agent-{agent}", [1_001], [])
        root = chat("r", [0], [returned, message])
        root.task_kin, side.task_kin = [side], [root]
        self.assertTrue(summary._addresses(root, message, side))

    @staticmethod
    def _large_family(agents: int, calls: int, messages: int):
        """`agents` sync agents of `calls` calls each handed back, `messages` messages to them by
        id or by name, and one background agent still running: (root, sides, stub handles)."""
        import summary

        def chat(session: str, turn_ts: int, events: list[dict],
                 reply: str = "Done.") -> summary._Chat:
            out = summary._Chat(session=session, agent="claude", project="p", root="r",
                                side=session != "r", first_ts=0, last_ts=0, first_text="go")
            out.turns = [summary._Turn(turn=0, ts=turn_ts, who="user", text="go", digest=None)]
            out.replies = {0: reply} if reply else {}
            out.events = events
            return out

        clock, launches, sides = 1_800_000_000_000, [], []
        for n in range(agents):
            launches.append({"kind": "subagent_start", "name": "Agent", "ts": clock, "ok": True,
                             "output": "[Subagent hand-back] …", "child": f"agent-a{n:016x}",
                             "meta": f"name=w{n} status=completed returned={clock + 6_000}"})
            sides.append(chat(f"agent-a{n:016x}", clock + 1, [
                {"kind": "tool", "name": "Read", "ts": clock + 2 + i, "ok": True}
                for i in range(calls)]))
            clock += 7_000
        sent = []
        for n in range(messages):
            to = f"a{n % agents:016x}" if n % 2 else f"w{n % agents}"
            sent.append({"kind": "tool", "name": "SendMessage", "ts": clock, "ok": True,
                         "output": json.dumps({"success": True, "message": "queued"}),
                         "meta": f"to={to} returned={clock + 1}"})
            clock += 1_000
        busy = {"kind": "subagent_start", "name": "Agent", "ts": clock, "ok": True,
                "child": "agent-a" + "f" * 16, "meta": f"status=async_launched returned={clock}"}
        sides.append(chat("agent-a" + "f" * 16, clock + 1,
                          [{"kind": "tool", "name": "Bash", "ts": clock + 60_000}], reply=""))
        root = chat("r", clock - 10_000_000, [*launches, *sent, busy])
        for member in (root, *sides):
            member.task_kin = [other for other in (root, *sides) if other is not member]

        class Handles:
            def session(self, session: str) -> str:
                return session

            def turn(self, chat_: summary._Chat, turn: int) -> str:
                return f"{chat_.session}:{turn}"

        return root, sides, Handles()

    def test_a_large_agent_family_classifies_in_linear_time(self) -> None:
        # 60 sync agents of 400 calls, 200 messages to them by id or by name, and one background
        # agent still running: each message's target resolves once, not once per side chat
        import summary
        root, sides, handles = self._large_family(60, 400, 200)
        started = time.perf_counter()
        item = summary._pending_item(root, sides, handles)
        elapsed = time.perf_counter() - started
        self.assertEqual((item["status"], item["evidence_session"]),
                         ("agent_work_incomplete", "agent-a" + "f" * 16))
        # well under 0.1 s here; one rescan of the family per message per side took seconds
        self.assertLess(elapsed, 2.0)

    def test_many_agents_and_messages_classify_without_rescanning_the_root(self) -> None:
        # 150 agents and 1,000 messages: the root's calls, messages, results and notifications
        # are grouped once by the agent they reach, so each hand-back proof reads its own few
        # events instead of every root event per side chat (3.9 s before, under 0.1 s here)
        import summary
        root, sides, handles = self._large_family(150, 40, 1_000)
        started = time.perf_counter()
        item = summary._pending_item(root, sides, handles)
        elapsed = time.perf_counter() - started
        self.assertEqual((item["status"], item["evidence_session"]),
                         ("agent_work_incomplete", "agent-a" + "f" * 16))
        # 3.7 s before the index at load 165; 0.02-0.25 s with it at the same load
        self.assertLess(elapsed, 1.0)

    def test_cursor_merge_past_the_display_cap_replays_whole(self) -> None:
        # the merge closing all ten items passes the event cap in its display input; the facts
        # kept whole close them, and the clean final reply finishes the chat
        self.assertNotIn(CURSOR_MERGE_CAPPED, self._tasks_pending("--agent", "cursor"))

    def test_kimi_in_progress_with_a_space_is_open(self) -> None:
        _meta, items = _rows(self.sandbox.summary("pending", *KIMI_WINDOW, "--agent", "kimi",
                                                  "--json"))
        item = {row["session"]: row for row in items}[KIMI_TITLE_CASE]
        self.assertEqual((item["status"], item["agent"], item["items"]),
                         ("todo_open", "kimi", ["Port the yaml loader"]))

    def test_trailing_compaction_recap_does_not_reopen_a_finished_chat(self) -> None:
        # claude: manual /compact after the final reply; omp: auto-compaction after it; codex: a
        # compacted boundary; opencode: a summary message. None is a prompt, none counts a turn
        pending = self._pending_by_session()
        for agent in MX:
            with self.subTest(agent=agent):
                self.assertNotIn(MX[agent]["compacted"], pending)
        _meta, projects = _rows(self._ok(*MATRIX, "-n", "0", "--json"))
        turns = {chat["session"]: chat["turns"] for p in projects for chat in p["worked_on"]}
        for agent in MX:
            with self.subTest(agent=agent):
                self.assertEqual(turns[MX[agent]["compacted"]], 1)
        # a recap in the middle of a chat carries the reply written after it to the prompt before
        item = pending[MX_CLAUDE_MID_COMPACTION]
        self.assertEqual((item["status"], item["evidence"]),
                         ("waiting_on_user", "Should I also port the sierra tests?"))
        self.assertEqual(turns[MX_CLAUDE_MID_COMPACTION], 1)

    def test_next_steps_headings_with_inner_colons_and_remaining_work(self) -> None:
        # **Next steps:** (claude), ### Remaining work (omp), __Next steps:__ (codex),
        # **Open items:** (opencode)
        pending = self._pending_by_session()
        for agent in MX:
            with self.subTest(agent=agent):
                self.assertEqual(pending[MX[agent]["next_steps"]]["status"], "open_next_steps")

    def test_none_bullet_under_a_remaining_heading_is_not_an_open_item(self) -> None:
        pending = self._pending_by_session("--agent", "claude")
        self.assertNotIn(MX_CLAUDE_NONE_BULLET, pending)

    def test_non_claude_subagent_hand_back_is_recognised(self) -> None:
        # omp: the side chat ends in a terminal `yield` with no closing text and the root's `task`
        # result wraps it in <task-result>; codex: wait_agent returns it inside JSON; opencode: the
        # root's `task` tool output is the child's reply. Each root replied cleanly afterwards
        pending = self._pending_by_session()
        for agent in ("omp", "codex", "opencode"):
            with self.subTest(agent=agent):
                self.assertNotIn(MX[agent]["handed_back"], pending)
        meta, _items = _rows(self.sandbox.summary("pending", *MATRIX, "--project", "mx-omp",
                                                   "--json"))
        self.assertEqual((meta["chats"], meta["side_chats"]), (11, 2))

    def test_omp_init_past_the_display_cap_replays_its_later_ops(self) -> None:
        # the display input keeps 800 chars of the 14-item init, so its only phase object and the
        # op are cut there; the list facts kept whole let the accepted start/done/start replay
        pending = self._pending_by_session("--project", "mx-omp")
        item = pending[MX_OMP_CAPPED_INIT]
        self.assertEqual((item["status"], item["confidence"], len(item["items"])),
                         ("todo_open", "medium", 13))
        self.assertEqual(item["items"][:2], [
            "port the echo module bravo to the new loader API and its tests",
            "port the echo module charlie to the new loader API and its tests"])
        self.assertNotIn("caveats", item)

    def test_timed_out_codex_wait_is_not_a_hand_back(self) -> None:
        # wait_agent returned {"status":{id:"running"},"timed_out":true}; the worker's reply
        # contains the word "running" and its plan is still open
        pending = self._pending_by_session("--agent", "codex")
        item = pending[MX_CODEX_WAIT_TIMED_OUT]
        self.assertEqual((item["status"], item["source"], item["evidence_session"], item["items"]),
                         ("todo_open", "side-chat", MX_CODEX_WAIT_TIMED_OUT_SIDE,
                          ["rebuild the lima index", "report the lima rebuild"]))

    def test_short_completed_codex_hand_back_is_received(self) -> None:
        # wait_agent returned {"status":{id:{"completed":"Done, no stale caches."}}} and the
        # worker's final reply is exactly that 22-char text; the root then replied cleanly
        pending = self._pending_by_session("--agent", "codex")
        self.assertNotIn(MX_CODEX_SHORT_HAND_BACK, pending)
        self.assertIn(MX_CODEX_WAIT_TIMED_OUT, pending)

    def _on_every_read_path(self, probe) -> list:
        """``probe()`` on the search db, on that db without codex's empty recap rows (what a
        build from before they were indexed left), and on the transcript with no db at all."""
        database = self.sandbox.data / "corpus.db"
        kept = database.with_name("corpus.db.kept")
        shutil.copy2(database, kept)
        try:
            observed = [probe()]
            with contextlib.closing(sqlite3.connect(database)) as db:
                dropped = db.execute("DELETE FROM msgs WHERE who='recap' AND text=''").rowcount
                db.commit()
            self.assertGreater(dropped, 0)
            observed.append(probe())
            database.unlink()
            observed.append(probe())
        finally:
            kept.replace(database)
        return observed

    def test_codex_compaction_survives_the_search_db(self) -> None:
        # a codex `compacted` recap is an empty-text row with the reply filed under its turn;
        # the search db, an older db that kept only that reply, and the JSONL fallback agree
        def verdicts():
            pending = self._pending_by_session("--agent", "codex")
            table = _time_table(self._ok("time", "--since", "2026-06-03",
                                         "--until", "2026-06-03 23:59", "--project", "mx-codex",
                                         "--json"))
            return (pending.get(MX_CODEX_COMPACTED_FINISHED),
                    pending[MX_CODEX_COMPACTED_NEXT_STEPS]["status"],
                    pending[MX_CODEX_COMPACTED_NEXT_STEPS]["items"],
                    MX_CODEX_COMPACTED_BETWEEN in pending,
                    table[("2026-06-03", "mx-codex")]["estimated_active_ms"])
        expected = (None, "open_next_steps", ["port the oscar writer", "update the oscar docs"],
                    False, 40 * MINUTE)
        self.assertEqual(self._on_every_read_path(verdicts), [expected] * 3)

    def test_codex_compaction_marker_counts_on_both_paths(self) -> None:
        # no reply follows the 09:15 compaction: the recap row and the compaction marker give
        # that moment once on every read path: 15m + the capped 20m tail
        def minutes():
            table = _time_table(self._ok("time", "--since", "2026-06-04",
                                         "--until", "2026-06-04 23:59", "--project", "mx-codex",
                                         "--json"))
            return table[("2026-06-04", "mx-codex")]["estimated_active_ms"]
        self.assertEqual(self._on_every_read_path(minutes), [35 * MINUTE] * 3)

    def test_compaction_markers_dedupe_against_recap_moments(self) -> None:
        import summary
        fields = {"session": "x", "project": "p", "root": "x", "side": False,
                  "first_ts": 0, "last_ts": 0, "first_text": ""}
        marker = {"kind": "control", "name": "compacted", "input": "", "ok": None}
        chat = summary._Chat(agent="codex", **fields)
        chat.turns = [summary._Turn(0, 1_000, "user", "go", None)]
        chat.recap_ts = [5_000]
        chat.events = [dict(marker, ts=5_000), dict(marker, ts=9_000), dict(marker, ts=9_000),
                       {"kind": "tool", "name": "shell", "ts": 9_500, "ok": True}]
        summary._fold_compaction_markers(chat)
        self.assertEqual(sorted(chat.recap_ts), [5_000, 9_000])
        # markers are moments, never the activity tail
        self.assertEqual(summary._latest_ts(chat), 9_500)
        chat.events = chat.events[:3]
        self.assertEqual(summary._latest_ts(chat), 9_000)
        self.assertEqual(summary._chat_intervals(chat, 20 * MINUTE)[0], [(1_000, 9_000)])
        withheld = summary._Chat(agent="codex", **fields)
        withheld.withheld_from = 7_000
        withheld.events = [dict(marker, ts=5_000), dict(marker, ts=9_000)]
        summary._fold_compaction_markers(withheld)
        self.assertEqual(withheld.recap_ts, [5_000])

    def test_final_api_error_or_usage_limit_is_unfinished_work(self) -> None:
        pending = self._pending_by_session("--project", "mx-claude")
        expected = {
            MX_CLAUDE_API_ERROR_ONLY: ("root", "API Error: Repeated 529 Overloaded errors"),
            MX_CLAUDE_USAGE_LIMIT: ("root",
                                    "You've hit your weekly limit · resets May 18 at 4pm (Europe/Berlin)"),
            MX_CLAUDE_PROMPT_TOO_LONG: ("root", "Prompt is too long"),
            MX_CLAUDE_SIDE_API_ERROR: ("side-chat", "API Error: Rate limit reached"),
        }
        for session, (source, evidence) in expected.items():
            with self.subTest(session=session):
                item = pending[session]
                self.assertEqual((item["status"], item["confidence"], item["source"],
                                  item["evidence"]),
                                 ("agent_work_incomplete", "medium", source, evidence))
        self.assertEqual(pending[MX_CLAUDE_SIDE_API_ERROR]["evidence_session"],
                         MX_CLAUDE_SIDE_API_ERROR_SIDE)

    def test_finished_reply_quoting_an_api_error_is_not_pending(self) -> None:
        pending = self._pending_by_session("--project", "mx-claude")
        for session in (*MX_CLAUDE_QUOTED_ERRORS, *MX_CLAUDE_PROSE_ERRORS,
                        MX_CLAUDE_ERROR_THEN_TEXT):
            with self.subTest(session=session):
                self.assertNotIn(session, pending)

    def test_api_error_needs_the_latest_marker_ending_the_reply(self) -> None:
        import summary
        fields = {"session": "x", "project": "p", "root": "x", "side": False,
                  "first_ts": 0, "last_ts": 0, "first_text": ""}
        last = summary._Turn(0, 1_000, "user", "go", None)

        def error(reply, *events, agent="claude"):
            chat = summary._Chat(agent=agent, **fields)
            chat.events = [dict(event, i=index) for index, event in enumerate(events)]
            return summary._api_error(chat, last, reply)

        text = "API Error: Repeated 529 Overloaded errors"
        marker = {"kind": "control", "name": "api_error", "ts": 2_000, "input": text,
                  "input_chars": len(text), "ok": None}
        tool = {"kind": "tool", "name": "Bash", "ts": 1_500, "input": "make", "ok": True}
        self.assertEqual(error(f"Working on it.  {text}\n", tool, marker), text)
        self.assertEqual(error(text, marker), text)
        # prose alone, a marker the reply does not end with, a later event, another agent
        for reply in ("The failure was Request timed out", f"{text} then retried fine",
                      f"Working on it.{text}"):
            with self.subTest(reply=reply):
                self.assertIsNone(error(reply, marker if text in reply else tool))
        self.assertIsNone(error(text, marker, dict(tool, ts=2_500)))
        self.assertIsNone(error(text, marker, agent="codex"))
        self.assertIsNone(error(text, dict(marker, ts=500)))
        # a capped marker compares its head with the reply's tail of the source length
        source = "API Error: 400 " + "x" * 900
        capped = dict(marker, input=source[:800] + "…", input_chars=len(source))
        self.assertEqual(error(f"Reading it. {source}", capped), source[:800] + "…")
        self.assertIsNone(error(f"Reading it. {source}y", capped))
        self.assertIsNone(error(source[:800], capped))

    def test_linked_task_notification_hands_a_background_subagent_back(self) -> None:
        # each side chat ran past every root event; a notification naming it, by agent id or by
        # the launch's tool-use id, hands it back. Unlinked or earlier notices prove nothing
        pending = self._pending_by_session("--project", "mx-claude")
        self.assertNotIn(MX_CLAUDE_NOTIFIED_BY_AGENT, pending)
        self.assertNotIn(MX_CLAUDE_NOTIFIED_BY_LAUNCH, pending)
        item = pending[MX_CLAUDE_NOTIFIED_UNLINKED]
        self.assertEqual((item["status"], item["source"], item["evidence_session"], item["items"]),
                         ("todo_open", "side-chat", MX_CLAUDE_NOTIFIED_UNLINKED_SIDE,
                          ["rebuild the xray index", "report the xray rebuild"]))

    def test_notification_queued_while_the_root_works_hands_the_subagent_back(self) -> None:
        # the side chat ran past the root's last tool and kept open todos; Claude delivered the
        # completion mid-turn as a queued_command attachment, not as an origin-tagged user row
        day = ("--since", "2026-06-14", "--until", "2026-06-15")
        handed_back = self.sandbox.summary("pending", *day, "--project", "mx-claude", "--json")
        meta, items = _rows(handed_back)
        self.assertEqual((items, meta["chats"], meta["side_chats"]), ([], 1, 1))
        self.assertNotEqual(handed_back.returncode, 0)
        pending = self._pending_by_session("--project", "mx-claude")
        self.assertNotIn(MX_CLAUDE_NOTIFIED_QUEUED, pending)

    def test_task_notification_links_only_by_child_or_launch_agent_id(self) -> None:
        import summary
        fields = {"project": "p", "root": "root", "first_ts": 0, "last_ts": 0, "first_text": ""}
        side = summary._Chat(session="agent-ab12", agent="claude", side=True, **fields)
        side.turns = [summary._Turn(0, 1_000, "subagent", "go", None)]
        side.events = [{"kind": "tool", "name": "Bash", "ts": 4_000, "ok": True}]
        root = summary._Chat(session="root", agent="claude", side=False, **fields)
        launch = {"kind": "subagent_start", "name": "Task", "ts": 500, "call_id": "toolu_1",
                  "output": "Async agent launched successfully.\nagentId: ab12 (internal ID)"}

        def note(ts, child="", tool_use_id=""):
            fields = " ".join(part for part in ("status=completed", "task_id=x",
                                                f"tool_use_id={tool_use_id}" if tool_use_id else "")
                              if part)
            return {"kind": "control", "name": "task_notification", "ts": ts, "input": fields,
                    "child": child, "ok": None}

        cases = (
            ([note(5_000, child="agent-ab12")], True),
            ([launch, note(5_000, tool_use_id="toolu_1")], True),
            ([launch, note(4_000, tool_use_id="toolu_1")], True),
            ([launch, note(3_999, tool_use_id="toolu_1")], False),
            ([note(5_000, child="agent-ab123")], False),
            ([launch, note(5_000, tool_use_id="toolu_2")], False),
            ([dict(launch, output="agentId: ab123"), note(5_000, tool_use_id="toolu_1")], False),
            ([dict(launch, kind="tool"), note(5_000, tool_use_id="toolu_1")], False),
            ([note(5_000)], False),
        )
        for events, linked in cases:
            with self.subTest(events=events):
                root.events = events
                self.assertIs(summary._notified(root, side), linked)

    def test_a_completed_linked_tool_call_hands_a_prose_less_subagent_back(self) -> None:
        # gemini's invoke_agent names the subagent it ran, whose last turn can end on a
        # complete_task call with no prose: the call's own completion is the hand-back
        import summary
        fields = {"project": "p", "root": "root", "first_ts": 0, "last_ts": 0, "first_text": ""}
        side = summary._Chat(session="c1c1-agent", agent="gemini", side=True, **fields)
        side.turns = [summary._Turn(0, 1_000, "subagent", "Investigate alpaca.", None)]
        side.events = [{"kind": "tool", "name": "complete_task", "ts": 1_000, "ok": True}]
        root = summary._Chat(session="root", agent="gemini", side=False, **fields)
        call = {"kind": "tool", "name": "invoke_agent", "ts": 900, "call_id": "agent-1", "ok": True,
                "output": "Subagent 'codebase_investigator' finished.", "child": "c1c1-agent",
                "meta": "delegation=completed"}
        # Claude's SendMessage that resumes an agent in the background links the agent too, but
        # returns before it finishes: a link without the recorded completion proves nothing
        resumed = {"kind": "tool", "name": "SendMessage", "ts": 900, "call_id": "sm-1", "ok": True,
                   "output": '{"success":true,"message":"Resuming agent c1c1"}', "child": "c1c1-agent"}
        cases = (
            ([call], True),
            ([dict(call, meta="delegation=cancelled", ok=None)], False),
            ([dict(call, meta="delegation=error", ok=False)], False),
            ([dict(call, meta="")], False),
            ([dict(call, child="c1c1-other")], False),
            ([dict(call, child="")], False),
            ([resumed], False),
            ([], False),
        )
        for events, received in cases:
            with self.subTest(events=events):
                root.events = events
                self.assertIs(summary._result_received(root, side), received)

    def test_a_later_block_after_the_list_is_quoted_unless_work_came_between(self) -> None:
        # list, Edit, completion: finished; list then a sign-off block with no work: high
        pending = self._pending_by_session("--project", "mx-claude")
        self.assertNotIn(MX_CLAUDE_GLUED_BULLET, pending)
        item = pending[MX_CLAUDE_LIST_THEN_SIGN_OFF]
        self.assertEqual((item["status"], item["confidence"], item["items"]),
                         ("open_next_steps", "high", ["update the callers", "run the full suite"]))
        self.assertNotIn("caveats", item)
        plain = pending[MX["claude"]["next_steps"]]
        self.assertEqual(plain["confidence"], "high")
        self.assertNotIn("caveats", plain)

    def test_reply_blocks_keep_their_boundary_on_every_read_path(self) -> None:
        # the search db path reads the block structure from its reply_shape side table for the
        # rows it classifies; the materialized path reads it with the replies: the same verdicts
        def verdict():
            pending = self._pending_by_session("--project", "mx-claude")
            return (MX_CLAUDE_GLUED_BULLET in pending,
                    pending[MX_CLAUDE_LIST_THEN_SIGN_OFF]["items"],
                    pending["ca000050-0550-4000-8000-000000000550"]["confidence"],
                    pending[MX_CLAUDE_TOOL_AFTER_TEXT]["status"],
                    MX_CLAUDE_TEXT_AFTER_TOOL in pending,
                    pending[MX_CLAUDE_CAPPED_QUESTION]["status"],
                    MX_CLAUDE_CAPPED_DONE in pending)
        expected = (False, ["update the callers", "run the full suite"], "medium",
                    "agent_work_incomplete", False, "waiting_on_user", False)
        self.assertEqual(self._on_every_read_path(verdict), [expected] * 3)

    def test_time_mode_never_reads_reply_shapes(self) -> None:
        import summary
        from unittest import mock
        with mock.patch.object(summary, "_load_shapes",
                               side_effect=AssertionError("time mode read shapes")):
            table = _time_table(self._ok("time", *MARCH, "--json"))
        self.assertEqual(table[("2026-03-10", "atlas")]["estimated_active_ms"], 26 * MINUTE)
        # the subprocess ran unpatched; the in-process guard is for the loader itself
        chats = {"x": summary._Chat(session="x", agent="claude", project="p", root="x",
                                    side=False, first_ts=0, last_ts=0, first_text="")}
        with mock.patch.object(summary, "_load_shapes",
                               side_effect=AssertionError("time mode read shapes")):
            summary._load_transcripts(chats, None, shapes=False)

    def test_section_items_keep_the_same_block_rules(self) -> None:
        import summary

        def items(body):
            section = summary._open_section_items(f"Done.\n\nNext steps:\n{body}")
            return None if section is None else section[1]

        # prose after the list in the same block supersedes it unless it is a short sign-off;
        # the list's own punctuation never changes the answer
        self.assertIsNone(items("- update the callers\n- run the full suite\n\n"
                                "Updated the callers and the full suite passes."))
        self.assertEqual(items("- update the callers\n- run the full suite\n\nLet me know."),
                         ["update the callers", "run the full suite"])
        self.assertEqual(items("- update the callers\n- run the full suite. Let me know."),
                         ["update the callers", "run the full suite."])
        self.assertEqual(items("- update the callers\n- run the full suite and it passes."),
                         ["update the callers", "run the full suite and it passes."])
        self.assertEqual(items("- ask Priya whether the Billing Export job should move to Postgres "
                               "before the Friday freeze."),
                         ["ask Priya whether the Billing Export job should move to Postgres "
                          "before the Friday freeze."])
        self.assertEqual(items("- Backfill the epsilon table.\n"
                               "- Point the Reporting service at the new table."),
                         ["Backfill the epsilon table.",
                          "Point the Reporting service at the new table."])
        self.assertEqual(items("- add tests;\n- update the docs;\n- release it."),
                         ["add tests;", "update the docs;", "release it."])

    def test_a_tool_call_after_the_last_reply_block_leaves_the_turn_unfinished(self) -> None:
        # the ingest marks the calls no reply text of their turn followed; the final text after a
        # call finishes the turn, and a todo write closing the list after the final text is
        # bookkeeping, not work cut off
        pending = self._pending_by_session()
        for session, tool in ((MX_CLAUDE_TOOL_AFTER_TEXT, "Bash"),
                              (MX_CODEX_TOOL_AFTER_TEXT, "shell_command"),
                              (MX_CODEX_SAME_MS_TOOL_LAST, "shell_command"),
                              (MX_CODEX_UNTIMED_TEXT, "shell_command"),
                              (MX_CODEX_COMPACTED_NO_REPLY, "shell_command")):
            with self.subTest(session=session):
                item = pending[session]
                self.assertEqual((item["status"], item["confidence"], item["source"],
                                  item["signals"]),
                                 ("agent_work_incomplete", "medium", "root",
                                  [f"reply ended before the turn's last tool call ({tool})"]))
        self.assertEqual(pending[MX_CLAUDE_TOOL_AFTER_TEXT]["evidence"],
                         "I'll run the xray suite now.")
        for session in (MX_CLAUDE_TEXT_AFTER_TOOL, MX_CODEX_TEXT_AFTER_TOOL,
                        MX_CODEX_SAME_MS_TEXT_LAST, MX_CODEX_RESUMED_SPLIT,
                        MX_CLAUDE_TODO_AFTER_TEXT):
            with self.subTest(session=session):
                self.assertNotIn(session, pending)
        # the matrix's finished and open shapes keep their verdicts (asserted elsewhere); every
        # other tool-bearing chat of the matrix stays as it was
        self.assertNotIn(MX["claude"]["finished"], pending)
        self.assertNotIn(MX["codex"]["handed_back"], pending)

    def test_trailing_call_rule_reads_the_recorded_fact_only(self) -> None:
        import summary
        call = {"kind": "tool", "name": "Bash", "ts": 2_000, "ok": True, "i": 1}
        later = {"kind": "tool", "name": "Read", "ts": 3_000, "ok": True, "i": 2}
        self.assertIsNone(summary._trailing_call([call, later]))
        marked = dict(later, meta="after_reply=1")
        self.assertIs(summary._trailing_call([call, marked]), marked)
        # the latest call decides, not an earlier trailing-marked one
        self.assertIsNone(summary._trailing_call([dict(call, meta="after_reply=1"), later]))
        # at one timestamp the later event in the file decides
        tied = dict(later, ts=2_000, meta="after_reply=1")
        self.assertIs(summary._trailing_call([call, tied]), tied)
        self.assertIsNone(summary._trailing_call([tied, dict(call, i=3)]))
        # bookkeeping writes, launches and markers never count
        for name in ("TodoWrite", "TaskUpdate", "update_plan", "todo", "yield"):
            with self.subTest(name=name):
                self.assertIsNone(summary._trailing_call([dict(tied, name=name)]))
        self.assertIsNone(summary._trailing_call([dict(tied, kind="subagent_start")]))
        self.assertIsNone(summary._trailing_call([dict(tied, kind="control")]))

    def test_capped_reply_is_classified_by_its_kept_ending(self) -> None:
        # the text of a reply past 64,000 chars is 0.4.2's head; the ingest keeps the latest
        # block's tail beside it (up to 8,000 chars): a closing question waits, a completion
        # finishes; only a final block cut past the kept tail stays unknown, and says so
        pending = self._pending_by_session("--project", "mx-claude")
        item = pending[MX_CLAUDE_CAPPED_QUESTION]
        self.assertEqual((item["status"], item["confidence"], item["evidence"]),
                         ("waiting_on_user", "high", "Should I apply it to the zulu schema now?"))
        self.assertNotIn(MX_CLAUDE_CAPPED_DONE, pending)
        item = pending[MX_CLAUDE_CAPPED_TAIL_CUT]
        self.assertEqual((item["status"], item["confidence"], item["signals"]),
                         ("unknown", "low",
                          ["reply capped at index time; its final block ran past 8,000 chars, so "
                           "a list above its ending was not indexed"]))
        # the kept tail is one sentence of 8,000 chars (no period before its ending), clipped
        self.assertTrue(item["evidence"].startswith("zzzz"))

    def test_reply_blocks_split_at_the_recorded_joins_and_fold_across_recaps(self) -> None:
        import summary
        fields = {"session": "x", "project": "p", "root": "x", "side": False,
                  "first_ts": 0, "last_ts": 0, "first_text": ""}
        shape = summary._ReplyShape(breaks=[3, 7])
        self.assertEqual(summary._blocks("one two three", shape), ["one", "two", "three"])
        # an offset that is not a joining space is ignored; no shape means one block
        self.assertEqual(summary._blocks("one two", summary._ReplyShape(breaks=[1])), ["one two"])
        self.assertEqual(summary._blocks("one two", None), ["one two"])
        self.assertEqual(summary._blocks("", shape), [])
        # a recap's reply continues the prompt before it as further blocks, with its own ending;
        # its lead join (the work since the prompt's last block) joins the two, unknown when
        # the row carries none
        chat = summary._Chat(agent="claude", **fields)
        chat.turns = [summary._Turn(0, 1_000, "user", "go", None),
                      summary._Turn(1, 2_000, "recap", "summary", None)]
        chat.replies = {0: "a b", 1: "c d"}
        chat.reply_shapes = {0: summary._ReplyShape(breaks=[1], worked=[1], lead=0,
                                                    blocks=["a", "b"]),
                             1: summary._ReplyShape(breaks=[1], worked=[0, 0], lead=1, end="tail",
                                                    end_cut=True, blocks=["c", "d"])}
        summary._fold_recaps(chat)
        self.assertEqual(chat.replies, {0: "a b\n\nc d"})
        folded = chat.reply_shapes[0]
        self.assertEqual((folded.blocks, folded.worked, folded.lead, folded.end, folded.end_cut),
                         (["a", "b", "c", "d"], [1, 1, 0, 0], 0, "tail", True))
        text, ending, missing = summary._terminal(chat, 0)
        self.assertEqual((text, ending.blocks, ending.worked, missing),
                         ("tail", ["a", "b", "c", "d", "tail"], [1, 1, 0, 0],
                          "reply capped at index time; its final block ran past 8,000 chars, so "
                          "a list above its ending was not indexed"))
        chat.turns = [summary._Turn(0, 1_000, "user", "go", None),
                      summary._Turn(1, 2_000, "recap", "summary", None)]
        chat.replies = {0: "a b", 1: "c"}
        chat.reply_shapes = {0: summary._ReplyShape(breaks=[1], worked=[1], blocks=["a", "b"]),
                             1: summary._ReplyShape(blocks=["c"])}
        summary._fold_recaps(chat)
        self.assertEqual(chat.reply_shapes[0].worked, [1, 2])

    def test_tool_work_after_a_list_block_hands_the_turn_to_its_terminal_block(self) -> None:
        # work after the list block: the terminal block decides; no work: a sign-off keeps the
        # list, other prose lowers it to medium, quoted; unrecorded order: sign-offs keep it at
        # medium, anything else hands the turn to the terminal block
        import summary
        body = "Ported the parser.\n\nNext steps:\n- update the callers\n- run the full suite"
        open_items = ["update the callers", "run the full suite"]
        NO, YES, UNKNOWN = 0, 1, 2

        def section(blocks, worked):
            shape = summary._ReplyShape(blocks=blocks, worked=worked)
            result = summary._open_section(shape)
            return None if result is None else (result[1], result[3])

        def same_block(tail):
            return section([f"{body}\n\n{tail}"], [])

        for tail in ("Let me know if I missed anything.", "Happy to help further if needed.",
                     "Let me know if you have any questions about the proposed changes.",
                     "Let me know when you'd like me to get started.",
                     "I can also add tests if you're interested."):
            with self.subTest(tail=tail, block="same"):
                self.assertEqual(same_block(tail), (open_items, None))
        for tail in ("Hope this helped!", "Glad that worked out.", "Thanks, appreciated!",
                     "Both are now in place: callers point at the new parser and the suite is "
                     "clean.", "Updated the callers and the full suite passes."):
            with self.subTest(tail=tail, block="same"):
                self.assertIsNone(same_block(tail))
        quoted = "a later reply block may report this done: "
        unknown = "whether work followed this list is not recorded; a later block: "
        for tail in ("Let me know if you want me to run them.", "Happy to continue tomorrow."):
            with self.subTest(tail=tail, block="later sign-off"):
                self.assertEqual(section([body, tail], [NO]), (open_items, None))
                self.assertEqual(section([body, tail], [UNKNOWN]),
                                 (open_items, unknown + repr(tail)))
                self.assertEqual(section([body, tail, "Just say the word."], [UNKNOWN, UNKNOWN]),
                                 (open_items, unknown + repr(tail)))
        for tail in ("Hope this helps!", "Thanks!", "Good luck with the release!",
                     "I'll stop here for now.", "Updated the callers and the full suite passes.",
                     "Done. I updated the callers and ran the full suite: 412 passed."):
            with self.subTest(tail=tail, block="later prose"):
                self.assertEqual(section([body, tail], [NO]), (open_items, quoted + repr(tail)))
                self.assertIsNone(section([body, tail], [UNKNOWN]))
                # a sign-off before the prose does not hide it
                self.assertEqual(section([body, "Let me know.", tail], [NO, NO]),
                                 (open_items, quoted + repr(tail)))
                self.assertIsNone(section([body, "Let me know.", tail], [UNKNOWN, NO]))
        for tail in ("Updated the callers and the full suite passes.",
                     "Done. I updated the callers and ran the full suite: 412 passed.",
                     "Hope this helps!", "All done."):
            with self.subTest(tail=tail, block="later, after work"):
                self.assertIsNone(section([body, tail], [YES]))
                self.assertIsNone(section([body, tail], [UNKNOWN, YES]))
        # work after the list, then a terminal list of its own: that list, high
        final = "Three tests still fail.\n\nNext steps:\n- fix the fixtures\n- rerun the suite"
        self.assertEqual(section([body, final], [YES]),
                         (["fix the fixtures", "rerun the suite"], None))
        self.assertEqual(section([body, final], [UNKNOWN]),
                         (["fix the fixtures", "rerun the suite"], None))
        # work before the list block but none after it: the list block is the latest word
        self.assertEqual(section(["Starting.", body], [YES]), (open_items, None))
        self.assertEqual(section(["Starting.", body, "Thanks!"], [YES, NO]),
                         (open_items, quoted + "'Thanks!'"))
        self.assertEqual(section(["Starting.", body, "Thanks!"], [NO, YES]), None)
        # missing flags read as unknown
        self.assertIsNone(section([body, "Thanks!"], []))
        self.assertEqual(section([body, "Let me know."], []),
                         (open_items, unknown + "'Let me know.'"))
        self.assertEqual(section([body], []), (open_items, None))
        self.assertIsNone(section(["Done.", "All good."], [NO]))

    def test_structural_rule_on_indexed_chats(self) -> None:
        # executed plans (list, tool, completion) are finished; the terminal block after work
        # decides (its own list high, a question waits); no work: closing medium + quote,
        # sign-off high
        pending = self._pending_by_session("--project", "mx-claude")
        for session in (MX_CLAUDE_GLUED_BULLET, "ca000047-0547-4000-8000-000000000547"):
            with self.subTest(session=session):
                self.assertNotIn(session, pending)
        _meta, golf = _rows(self._ok("pending", *APRIL, "--project", "golf", "--json"))
        self.assertEqual([(i["session"], i["confidence"]) for i in golf], [(S16, "high")])
        item = pending["ca000048-0548-4000-8000-000000000548"]
        self.assertEqual((item["status"], item["confidence"], item["items"]),
                         ("open_next_steps", "high", ["fix the bravo fixtures", "rerun the suite"]))
        self.assertNotIn("caveats", item)
        item = pending["ca000049-0549-4000-8000-000000000549"]
        self.assertEqual((item["status"], item["evidence"]),
                         ("waiting_on_user", "Should I also port the charlie docs?"))
        item = pending["ca000050-0550-4000-8000-000000000550"]
        self.assertEqual((item["status"], item["confidence"], item["items"], item["caveats"]),
                         ("open_next_steps", "medium", ["update the callers", "run the full suite"],
                          ["a later reply block may report this done: 'Hope this helps!'"]))
        item = pending[MX_CLAUDE_LIST_THEN_SIGN_OFF]
        self.assertEqual((item["status"], item["confidence"], item["items"]),
                         ("open_next_steps", "high", ["update the callers", "run the full suite"]))
        self.assertNotIn("caveats", item)
        plain = pending[MX["claude"]["next_steps"]]
        self.assertEqual(plain["confidence"], "high")
        # a delegation is work (claude Agent); the recap row's lead join carries the work
        # across a compaction (claude, codex): the terminal block decides, so none is pending
        for session in ("ca000051-0551-4000-8000-000000000551",
                        "ca000052-0552-4000-8000-000000000552"):
            with self.subTest(session=session):
                self.assertNotIn(session, pending)
        pending = self._pending_by_session("--project", "mx-codex")
        self.assertNotIn("cx000030-0570-4000-8000-000000000570", pending)
        # opencode orders its parts, so its joins carry the fact: plan, work, done is finished;
        # plan then a sign-off with no work keeps the list at high
        pending = self._pending_by_session("--project", "mx-opencode")
        self.assertNotIn("ses_oc000008mx", pending)
        item = pending["ses_oc000009mx"]
        self.assertEqual((item["status"], item["confidence"], item["items"]),
                         ("open_next_steps", "high", ["read the loader config", "fix the parser"]))
        self.assertNotIn("caveats", item)

    def test_a_single_block_recap_reply_keeps_its_lead_on_the_search_db_path(self) -> None:
        # plan, work, compaction, closer: the recap reply is one block, so its only fact is the
        # lead join; the search db keeps that row too, and both read paths agree it is finished
        def verdict():
            return tuple(session in self._pending_by_session("--project", project)
                         for project, session in (("mx-claude", MX_CLAUDE_PLAN_COMPACT_CLOSER),
                                                  ("mx-codex", MX_CODEX_PLAN_COMPACT_CLOSER),
                                                  ("mx-omp", MX_OMP_PLAN_COMPACT_CLOSER)))
        self.assertEqual(self._on_every_read_path(verdict), [(False, False, False)] * 3)
        import corpusdb
        with contextlib.closing(sqlite3.connect(self.sandbox.data / "corpus.db")) as db:
            shapes = corpusdb.reply_shapes(db, [(MX_CLAUDE_PLAN_COMPACT_CLOSER, 1),
                                                (MX_CODEX_PLAN_COMPACT_CLOSER, 1),
                                                (MX_OMP_PLAN_COMPACT_CLOSER, 1)])
        self.assertEqual({key: (shape["reply_breaks"], shape["reply_lead"])
                          for key, shape in shapes.items()},
                         {(MX_CLAUDE_PLAN_COMPACT_CLOSER, 1): ([], 1),
                          (MX_CODEX_PLAN_COMPACT_CLOSER, 1): ([], 1),
                          (MX_OMP_PLAN_COMPACT_CLOSER, 1): ([], 1)})

    def test_every_adapter_records_work_between_its_blocks(self) -> None:
        # gemini orders prose before a message's calls: plan + run_shell_command, then a closer
        # is finished; 'To do:' then a sign-off with no work is an open list at high
        _meta, items = _rows(self._ok("pending", *TASKS, "--agent", "gemini", "--json"))
        by_session = {item["session"]: item for item in items}
        self.assertNotIn(GEMINI_PLAN_WORK_CLOSER, by_session)
        item = by_session[GEMINI_TODO_SIGN_OFF]
        self.assertEqual((item["status"], item["confidence"], item["items"]),
                         ("open_next_steps", "high",
                          ["reproduce the loader bug", "patch the handler"]))
        self.assertNotIn("caveats", item)
        import explore
        path = self.sandbox.data / "replies.jsonl"
        facts = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            agent = str(record["id"]).split(":")[0]
            shape = explore.reply_shape(record)
            if shape["reply_breaks"] or shape["reply_lead"] != 2:
                facts.setdefault(agent, set()).add(
                    (tuple(shape["reply_worked"]), shape["reply_lead"]))
        # every adapter the fixture store covers describes its joins; none is left unknown
        self.assertEqual(sorted(facts), ["claude", "codex", "cursor", "gemini", "kimi",
                                         "opencode", "pi"])
        for agent, seen in facts.items():
            with self.subTest(agent=agent):
                self.assertFalse(any(2 in worked or lead == 2 for worked, lead in seen), seen)

    def test_a_cursor_bubbles_own_call_joins_it_to_the_next_bubble(self) -> None:
        # a bubble's prose precedes its own call, so the call is work after the plan, not before
        # it: plan + edit then a completion is finished, plan + edit then a sign-off is too (the
        # terminal block decides after work), as kimi, crush and cline read the same turn
        pending = self._tasks_pending("--agent", "cursor")
        self.assertNotIn(CURSOR_PLAN_EDIT_DONE, pending)
        self.assertNotIn(CURSOR_PLAN_EDIT_SIGN_OFF, pending)
        import explore
        shapes = {}
        for line in (self.sandbox.data / "replies.jsonl").read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record["id"].startswith("cursor:k5000005") or record["id"].startswith("cursor:k6000006"):
                shape = explore.reply_shape(record)
                shapes[record["id"].split(":")[1]] = (shape["reply_lead"], shape["reply_worked"])
        self.assertEqual(shapes, {CURSOR_PLAN_EDIT_DONE: (0, [1]), CURSOR_PLAN_EDIT_SIGN_OFF: (0, [1])})

    def test_a_capped_replys_ending_refreshes_when_only_its_shape_moves(self) -> None:
        # a reply past the cap keeps its text frozen, so a later block moves only the shape the
        # search db keeps beside it: the session's signature covers the shape, and the refresh
        # reapplies it instead of serving the stale ending
        sandbox = SummarySandbox()
        self.addCleanup(sandbox.close)
        session = "ca000099-0599-4000-8000-000000000599"
        path = (sandbox.home / ".claude" / "projects" / "-projects-mx-claude"
                / f"{session}.jsonl")

        def row(stamp, role, content):
            record = {"sessionId": session, "cwd": str(sandbox.home / "projects" / "mx-claude"),
                      "type": role, "timestamp": stamp,
                      "message": {"role": role, "content": content}}
            if role == "user":
                record["userType"] = "external"
            else:
                record["message"]["model"] = "claude-sonnet-4"
            return json.dumps(record) + "\n"

        def block(stamp, text):
            return row(stamp, "assistant", [{"type": "text", "text": text}])

        with path.open("w", encoding="utf-8") as handle:
            handle.write(row("2026-06-24T10:00:00.000Z", "user", "Draft the zulu migration."))
            handle.write(block("2026-06-24T10:01:00.000Z", "Drafted the zulu migration."))
            handle.write(block("2026-06-24T10:02:00.000Z", "z" * 70_000))
            handle.write(block("2026-06-24T10:03:00.000Z", "Working on the remaining tables now."))

        def pending():
            _meta, items = _rows(sandbox.summary("pending", *MATRIX, "--project", "mx-claude",
                                                 "--json"))
            return {item["session"]: item for item in items}

        sandbox.index()
        self.assertNotIn(session, pending())
        database = sandbox.data / "corpus.db"
        before = database.stat().st_ino
        with path.open("a", encoding="utf-8") as handle:
            handle.write(block("2026-06-24T10:04:00.000Z",
                               "All tables migrated. Should I apply it to production now?"))
        sandbox.index()
        item = pending()[session]
        self.assertEqual((item["status"], item["confidence"], item["evidence"]),
                         ("waiting_on_user", "high", "Should I apply it to production now?"))
        self.assertEqual(database.stat().st_ino, before)
        import corpusdb
        with contextlib.closing(sqlite3.connect(database)) as db:
            shape = corpusdb.reply_shapes(db, [(session, 0)])[(session, 0)]
        self.assertEqual(shape["reply_end"],
                         "All tables migrated. Should I apply it to production now?")
        # the signature itself: the same rows with a different shape are a different session
        rows = [("x", 0, 0, "claude", "p", "", "m", "explicit", "user", "text", "ab12")]
        self.assertNotEqual(corpusdb._session_sig(rows, [(0, "[]", "[]", 0, "old", 0)]),
                            corpusdb._session_sig(rows, [(0, "[]", "[]", 0, "new", 0)]))
        first, second = (0, "[]", "[]", 0, "e", 0), (1, "[]", "[]", 1, "", 0)
        self.assertEqual(corpusdb._session_sig(rows, [first, second]),
                         corpusdb._session_sig(rows, [second, first]))

    def test_a_capped_reply_without_its_ending_is_still_reported_unknown(self) -> None:
        # a reply past the cap whose ending the index does not hold (a last-good row the upgrade
        # could not reparse, or 0.4.2 data read before the first new index) keeps 0.4.2's
        # 'ending not indexed' signal instead of reading its head as the ending
        import summary
        fields = {"session": "x", "project": "p", "root": "x", "side": False,
                  "first_ts": 0, "last_ts": 0, "first_text": ""}
        head = "z" * 64_000 + "…"
        for shape in (None, summary._ReplyShape()):
            with self.subTest(shape=shape):
                chat = summary._Chat(agent="claude", **fields)
                chat.turns = [summary._Turn(0, 1_000, "user", "go", None)]
                chat.replies = {0: head}
                if shape is not None:
                    chat.reply_shapes = {0: shape}
                record = summary._classify_root(chat)
                self.assertEqual((record["status"], record["signals"]),
                                 ("unknown", ["reply capped at index time; ending not indexed"]))
        # with the ending kept the question in it decides
        chat = summary._Chat(agent="claude", **fields)
        chat.turns = [summary._Turn(0, 1_000, "user", "go", None)]
        chat.replies = {0: head}
        chat.reply_shapes = {0: summary._ReplyShape(end="Should I apply it now?", blocks=[head])}
        self.assertEqual(summary._classify_root(chat)["status"], "waiting_on_user")
        # an uncapped single block is read whole
        chat.replies = {0: "Short and done."}
        chat.reply_shapes = {}
        self.assertIsNone(summary._classify_root(chat))

    def test_reply_handles_minted_by_0_4_2_still_resolve(self) -> None:
        # 0.4.2 hashed a reply as its text blocks joined with one space, cut at 64,000 chars,
        # and embedded that same text; the index publishes that exact text, so every digest and
        # embedding hash equals 0.4.2's and a handle quoted from an older index opens the reply
        import compact
        import embedding_store
        cap = 64_000
        expected: dict[str, tuple[str, str]] = {}
        store = FIXTURES / "store" / "claude" / "projects" / "-projects-mx-claude"
        for path in sorted(store.glob("*.jsonl")):
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            prompts = [r for r in rows if r.get("type") == "user" and r.get("userType")]
            if len(prompts) != 1 or rows[0] is not prompts[0]:
                continue
            legacy = ""
            for row in rows:
                if row.get("type") != "assistant":
                    continue
                for block in row["message"]["content"]:
                    text = block.get("text", "").strip() if block.get("type") == "text" else ""
                    if not text or len(legacy) >= cap:
                        continue
                    text = REPEAT_TEMPLATE.sub(lambda m: m[1] * int(m[2]), text)
                    legacy = f"{legacy} {text}" if legacy else text
                    if len(legacy) > cap:
                        legacy = legacy[:cap] + "…"
            if legacy:
                expected[rows[0]["sessionId"]] = (compact.content_digest(legacy),
                                                  embedding_store.semantic_text_hash(legacy))
        # the four in-band shapes: a line of one space inside a diff fence, alone and before a
        # question block; a log with such a line then a closing block; a 70K block with one at 30K
        for session in ("ca000043-0543-4000-8000-000000000543", "ca000044-0544-4000-8000-000000000544",
                        "ca000045-0545-4000-8000-000000000545", "ca000046-0546-4000-8000-000000000546",
                        MX_CLAUDE_GLUED_BULLET, MX_CLAUDE_CAPPED_QUESTION, MX_CLAUDE_CAPPED_TAIL_CUT):
            self.assertIn(session, expected)
        self.assertGreater(len(expected), 36)
        with contextlib.closing(sqlite3.connect(self.sandbox.data / "corpus.db")) as db:
            published = {s: (d, embedding_store.semantic_text_hash(t)) for s, d, t in db.execute(
                "SELECT session, content_digest, text FROM msgs WHERE who='agent' AND turn=0")}
        self.assertEqual({s: published.get(s) for s in expected}, expected)
        for session in ("ca000043-0543-4000-8000-000000000543", "ca000044-0544-4000-8000-000000000544",
                        "ca000046-0546-4000-8000-000000000546", MX_CLAUDE_GLUED_BULLET,
                        MX_CLAUDE_CAPPED_QUESTION, MX_CLAUDE_CAPPED_TAIL_CUT):
            handle = f"@{session[:8]}:0.{expected[session][0]}"
            with self.subTest(handle=handle):
                opened = self.sandbox.spawn([str(ROOT / "cli.py"), "around", handle, "--json",
                                             "--no-auto"])
                self.assertEqual(opened.returncode, 0, opened.stdout + opened.stderr)

    def test_a_refresh_that_moves_most_sessions_rebuilds_the_search_db_fresh(self) -> None:
        # an in-place rewrite of most sessions leaves the db a third larger and takes twice as
        # long as a fresh build; one session moving still refreshes in place
        sandbox = SummarySandbox()
        self.addCleanup(sandbox.close)
        sandbox.index()
        database = sandbox.data / "corpus.db"
        # root and side transcripts alike: more than half of the indexed sessions must move
        files = sorted((sandbox.home / ".claude").rglob("*.jsonl"))
        self.assertGreater(len(files), 100)

        def extend(path: Path) -> None:
            session = json.loads(path.read_text(encoding="utf-8").splitlines()[0])["sessionId"]
            row = {"sessionId": session, "cwd": str(sandbox.home / "projects" / "x"),
                   "type": "assistant", "timestamp": "2026-09-01T00:00:00.000Z",
                   "message": {"role": "assistant", "model": "claude-sonnet-4",
                               "content": [{"type": "text", "text": "A later block."}]}}
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row) + "\n")

        before = database.stat()
        extend(files[0])
        sandbox.index()
        self.assertEqual(database.stat().st_ino, before.st_ino)
        for path in files:
            extend(path)
        sandbox.index()
        after = database.stat()
        # a fresh build is renamed into place; the in-place rewrite kept the inode and grew the
        # file by 29% here (31% on a 3,100-session store), one short block per session apart
        self.assertNotEqual(after.st_ino, before.st_ino)
        self.assertLess(after.st_size, before.st_size * 1.2)

    def test_the_search_db_keeps_each_replys_shape_beside_its_rows(self) -> None:
        # corpus.db's reply_shape table carries the block structure keyed like the msgs row,
        # written by the fresh build, refreshed for a session that changed, dropped with a
        # session that went away; the briefing reads only the keys it classifies
        import corpusdb
        sandbox = SummarySandbox()
        self.addCleanup(sandbox.close)
        sandbox.index()
        database = sandbox.data / "corpus.db"
        glued, signed, capped = (MX_CLAUDE_GLUED_BULLET, MX_CLAUDE_LIST_THEN_SIGN_OFF,
                                 MX_CLAUDE_CAPPED_QUESTION)
        with contextlib.closing(sqlite3.connect(database)) as db:
            shapes = corpusdb.reply_shapes(db, [(glued, 0), (signed, 0), (capped, 0),
                                                (glued, 7)])
        self.assertEqual(set(shapes), {(glued, 0), (signed, 0), (capped, 0)})
        self.assertEqual((shapes[(glued, 0)]["reply_worked"], shapes[(signed, 0)]["reply_worked"]),
                         ([1], [0]))
        self.assertEqual(len(shapes[(glued, 0)]["reply_breaks"]), 1)
        self.assertEqual(shapes[(capped, 0)]["reply_end"],
                         "The zulu migration is drafted. Should I apply it to the zulu schema now?")
        self.assertFalse(shapes[(capped, 0)]["reply_end_cut"])
        # a later block after the sign-off chat's reply: its shape row follows the refresh
        path = (sandbox.home / ".claude" / "projects" / "-projects-mx-claude"
                / f"{MX_CLAUDE_LIST_THEN_SIGN_OFF}.jsonl")
        row = {"sessionId": MX_CLAUDE_LIST_THEN_SIGN_OFF,
               "cwd": str(sandbox.home / "projects" / "mx-claude"), "type": "assistant",
               "timestamp": "2026-09-01T00:00:00.000Z",
               "message": {"role": "assistant", "model": "claude-sonnet-4",
                           "content": [{"type": "text", "text": "A later block."}]}}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
        glued_path = path.with_name(f"{MX_CLAUDE_GLUED_BULLET}.jsonl")
        glued_path.unlink()
        before = database.stat().st_ino
        sandbox.index()
        self.assertEqual(database.stat().st_ino, before)
        with contextlib.closing(sqlite3.connect(database)) as db:
            shapes = corpusdb.reply_shapes(db, [(glued, 0), (signed, 0)])
            rows = db.execute("SELECT count(*) FROM reply_shape WHERE session=?",
                              (glued,)).fetchone()[0]
        self.assertEqual(set(shapes), {(signed, 0)})
        self.assertEqual(rows, 0)
        self.assertEqual((len(shapes[(signed, 0)]["reply_breaks"]),
                          shapes[(signed, 0)]["reply_worked"]), (2, [0, 0]))

    def test_side_chat_todo_caveats_are_reported(self) -> None:
        # the background agent ran past the root's last moment with no result handed back; its
        # todo list came in a shape the replay cannot read, which is unknown evidence, not none
        item = self._pending_by_session("--project", "mx-claude")[MX_CLAUDE_SIDE_TODO_UNKNOWN]
        self.assertEqual((item["status"], item["confidence"], item["source"],
                          item["evidence_session"], item["signals"], item["caveats"]),
                         ("unknown", "low", "side-chat", MX_CLAUDE_SIDE_TODO_UNKNOWN_SIDE,
                          ["side chat's todo list captured in an unknown shape"],
                          ["todo list captured in an unknown shape"]))

    def test_capped_question_input_shows_the_question_never_raw_json(self) -> None:
        import summary
        cut_inside = ('{"questions":[{"header":"Backend","multiSelect":false,"options":[],'
                      '"question":"Which cache backend should I use for the uniform serv…')
        self.assertEqual(summary._question_head({"name": "AskUserQuestion", "input": cut_inside}),
                         "Which cache backend should I use for the uniform serv…")
        cut_before = ('{"questions":[{"header":"Backend","multiSelect":false,"options":'
                      '[{"description":"Shared cache across all API instances; …')
        self.assertEqual(summary._question_head({"name": "AskUserQuestion", "input": cut_before}),
                         "asked a question")
        plan = '{"plan":"## Billing export rewrite\\n\\n1. Split the exporter \\u00e9 into…'
        self.assertEqual(summary._question_head({"name": "ExitPlanMode", "input": plan}),
                         "## Billing export rewrite 1. Split the exporter é into…")
        self.assertEqual(summary._question_head({"name": "ExitPlanMode", "input": '{"options":[{"a'}),
                         "presented a plan")

    def test_unanswered_question_or_plan_tool_is_waiting_on_user(self) -> None:
        pending = self._pending_by_session()
        expected = {
            MX_CLAUDE_EXIT_PLAN: ("ExitPlanMode", "## Tango auth refactor 1. Split login into"),
            MX_CLAUDE_ASK: ("AskUserQuestion",
                            "Which cache backend should I use for the uniform service?"),
            MX_OMP_ASK: ("ask", "Which cache backend should the whiskey service use?"),
        }
        for session, (tool, head) in expected.items():
            with self.subTest(session=session):
                item = pending[session]
                self.assertEqual((item["status"], item["confidence"], item["signals"]),
                                 ("waiting_on_user", "high", [f"{tool} awaits your answer"]))
                self.assertTrue(item["evidence"].startswith(head), item["evidence"])
        # an answered question followed by a clean reply is finished
        self.assertNotIn(MX_CLAUDE_ASK_ANSWERED, pending)

    def test_compaction_moments_count_as_activity_but_not_as_turns(self) -> None:
        # claude: 09:00 prompt, 09:30 auto-compaction, 10:00 prompt -> two capped 20m stretches;
        # omp: 09:00 prompt, 09:22 compaction, 09:24 prompt -> 20m + 2m
        claude = _time_table(self._ok("time", "--since", "2026-05-10", "--until", "2026-05-10 23:59",
                                      "--project", "mx-claude", "--json"))
        self.assertEqual(claude[("2026-05-10", "mx-claude")]["estimated_active_ms"], 40 * MINUTE)
        omp = _time_table(self._ok("time", "--since", "2026-05-19", "--until", "2026-05-19 23:59",
                                   "--project", "mx-omp", "--json"))
        self.assertEqual(omp[("2026-05-19", "mx-omp")]["estimated_active_ms"], 22 * MINUTE)
        _meta, projects = _rows(self._ok(*MATRIX, "-n", "0", "--json"))
        turns = {chat["session"]: chat["turns"] for p in projects for chat in p["worked_on"]}
        self.assertEqual((turns[MX_CLAUDE_COMPACTED_MID_TURN], turns[MX_OMP_COMPACTED_MID_TURN]),
                         (2, 2))
        self.assertNotIn(MX_CLAUDE_COMPACTED_MID_TURN, self._pending_by_session())

    def test_none_bullets_with_a_tail_are_not_open_items(self) -> None:
        pending = self._pending_by_session("--agent", "claude")
        for session in MX_CLAUDE_NONE_TAILS:
            with self.subTest(session=session):
                self.assertNotIn(session, pending)

    # --- briefing ---

    def test_briefing_orders_projects_by_estimated_time(self) -> None:
        result = self._ok(*MARCH, "--json")
        meta, projects = _rows(result)
        self.assertEqual(meta["mode"], "briefing")
        self.assertEqual([p["project"] for p in projects], ["atlas", "cedar", "beacon", "cairn"])
        atlas = projects[0]
        self.assertEqual((atlas["estimated_active_ms"], atlas["chats"], atlas["turns"]),
                         (41 * MINUTE, 2, 5))
        self.assertEqual([c["session"] for c in atlas["worked_on"]], [S2, S1])
        self.assertEqual([o["status"] for o in atlas["open"]],
                         ["open_next_steps", "waiting_on_user"])
        self.assertEqual(atlas["estimated_active"], "41m")
        human = self._ok(*MARCH)
        self.assertIn("atlas  ·  estimated active time 41m  ·  2 chats  ·  5 turns", human.stdout)
        self.assertIn("  worked on:\n    @a2a2a2a2 claude ", human.stdout)
        self.assertIn("  open:\n    HIGH   open_next_steps", human.stdout)
        self.assertNotIn("billable", human.stdout + human.stderr)
        self.assertIn("4 projects, 9 chats from 2026-03-01 to 2026-03-20", human.stderr)

    def test_max_bounds_chats_listed_per_project(self) -> None:
        _meta, projects = _rows(self._ok(*MARCH, "--project", "cairn", "-n", "1", "--json"))
        self.assertEqual(projects[0]["chats"], 3)
        self.assertEqual([c["session"] for c in projects[0]["worked_on"]], [S6])

    def test_projects_group_by_the_displayed_label_across_agents(self) -> None:
        """A basename label and a cwd-path label naming the same folder are one project."""
        result = self._ok(*MARCH, "--project", "cedar", "--json")
        _meta, projects = _rows(result)
        self.assertEqual(len(projects), 1)
        cedar = projects[0]
        self.assertEqual(cedar["project"], "cedar")
        self.assertEqual(sorted(c["session"] for c in cedar["worked_on"]), sorted([S9, S10]))
        self.assertEqual({c["agent"] for c in cedar["worked_on"]}, {"claude", "pi"})
        labels = cedar["project_labels"]
        self.assertIn("cedar", labels)
        self.assertTrue(any(label.endswith("/projects/cedar") and label != "cedar" for label in labels))
        # independent roots are not a family: 6m (claude) + 7m (pi), never one de-duplicated span
        self.assertEqual((cedar["estimated_active_ms"], cedar["chats"], cedar["turns"]),
                         (13 * MINUTE, 2, 4))
        human = self._ok(*MARCH, "--project", "cedar")
        headings = [line for line in human.stdout.splitlines() if line.startswith("cedar  ·")]
        self.assertEqual(len(headings), 1, human.stdout)
        self.assertIn("@e9e9e9e9 claude", human.stdout)
        self.assertIn("@f0f0f0f0 pi", human.stdout)
        self.assertIn("1 project, 2 chats from 2026-03-01 to 2026-03-20", human.stderr)
        table = _time_table(self._ok("time", *MARCH, "--project", "cedar", "--json"))
        self.assertEqual(list(table), [("2026-03-16", "cedar")])
        self.assertEqual(table[("2026-03-16", "cedar")]["chats"], 2)
        # the full stored path still selects only the chat that carries it, as it does for chats
        path_label = next(label for label in labels if label != "cedar")
        _meta, narrowed = _rows(self._ok(*MARCH, "--project", path_label, "--json"))
        self.assertEqual([c["session"] for c in narrowed[0]["worked_on"]], [S10])
        self.assertEqual(narrowed[0]["project_labels"], [path_label])

    def test_default_window_is_the_last_seven_days(self) -> None:
        meta, projects = _rows(self._ok("--json"))
        self.assertEqual(meta["window"]["since"], "7d")
        self.assertEqual([(p["project"], p["chats"]) for p in projects],
                         [("self-mx", 2), ("atlas", 1)])
        self.assertEqual([c["session"] for c in projects[1]["worked_on"]], [S7])

    def test_until_alone_is_the_seven_days_before_it(self) -> None:
        meta, projects = _rows(self._ok("--until", "2026-03-20", "--json"))
        self.assertEqual(meta["window"]["since"], "7d")
        self.assertEqual(meta["window"]["since_ts"], meta["window"]["until_ts"] - 7 * DAY_MS)
        self.assertEqual(sorted(p["project"] for p in projects), ["beacon", "cairn", "cedar"])
        human = self._ok("pending", "--until", "2026-03-20")
        self.assertIn("in the 7d before 2026-03-20", human.stderr)

    def test_project_filter_matches_label_or_leaf(self) -> None:
        _meta, projects = _rows(self._ok(*MARCH, "--project", "Atlas", "--json"))
        self.assertEqual([p["project"] for p in projects], ["atlas"])
        _meta, projects = _rows(self._ok(*MARCH, "--project", "c*", "--json"))
        self.assertEqual([p["project"] for p in projects], ["cedar", "cairn"])
        _meta, projects = _rows(self._ok(*MARCH, "--project", "ca*", "--json"))
        self.assertEqual([p["project"] for p in projects], ["cairn"])
        missing = self.sandbox.summary(*MARCH, "--project", "nothing-here", "--json")
        self.assertNotEqual(missing.returncode, 0)
        meta, rows = _rows(missing)
        self.assertEqual((rows, meta["hits"]), ([], []))

    # --- self exclusion ---

    def test_calling_agents_own_chat_is_excluded_and_disclosed(self) -> None:
        caller = {"CLAUDE_CODE_SESSION_ID": S7}
        hidden = self.sandbox.summary("pending", "--project", "atlas", "--json",
                                      env_overrides=caller)
        self.assertNotEqual(hidden.returncode, 0)
        meta, items = _rows(hidden)
        self.assertEqual((items, meta["self_excluded"]), ([], 1))
        human = self.sandbox.summary("pending", "--project", "atlas", env_overrides=caller)
        self.assertIn("excluded 1 chat from the current window", human.stderr)
        with_self = self._ok("pending", "--project", "atlas", "--self", "--json",
                             env_overrides=caller)
        self.assertEqual([i["session"] for i in _rows(with_self)[1]], [S7])
        # a human shell sees the chat without any notice
        plain = self._ok("pending", "--project", "atlas", "--json")
        self.assertEqual([i["session"] for i in _rows(plain)[1]], [S7])
        self.assertEqual(_rows(plain)[0]["self_excluded"], 0)

    def test_callers_live_window_starts_at_its_recap_before_folding(self) -> None:
        # the live window begins at the newest recap: its moment and everything after it are
        # the caller's own, so neither counts as time, a turn or an open item
        def chats_for(session):
            _meta, projects = _rows(self._ok("--project", "self-mx", "--json",
                                             env_overrides={"CLAUDE_CODE_SESSION_ID": session}))
            entry = projects[0]
            return ({c["session"]: c for c in entry["worked_on"]},
                    [o["session"] for o in entry["open"]])
        chats, open_items = chats_for(SELF_TOOLS_ONLY)
        own = chats[SELF_TOOLS_ONLY]
        self.assertEqual((own.get("self"), own["turns"], own["estimated_active_ms"]),
                         (True, 1, 20 * MINUTE))
        self.assertNotIn(SELF_TOOLS_ONLY, open_items)
        chats, _open = chats_for(SELF_LIVE_PROMPT)
        own = chats[SELF_LIVE_PROMPT]
        self.assertEqual((own.get("self"), own["turns"], own["estimated_active_ms"]),
                         (True, 1, 20 * MINUTE))
        # a human shell sees the whole chat: the compaction is activity and the open turn shows
        _meta, projects = _rows(self._ok("--project", "self-mx", "--json"))
        chats = {c["session"]: c for c in projects[0]["worked_on"]}
        self.assertEqual((chats[SELF_TOOLS_ONLY]["estimated_active_ms"],
                          chats[SELF_LIVE_PROMPT]["estimated_active_ms"]),
                         (30 * MINUTE, 40 * MINUTE))
        self.assertEqual([(o["session"], o["status"]) for o in projects[0]["open"]],
                         [(SELF_TOOLS_ONLY, "agent_work_incomplete")])

    # --- json shape and argument contract ---

    def test_json_envelope_fields(self) -> None:
        meta, rows = _rows(self._ok("time", *MARCH, "--json"))
        for key in ("mode", "window", "timezone", "idle_cap", "idle_cap_ms", "group",
                    "freshness", "caveats", "metric", "unknown_timestamp_rows",
                    "family_dedup_ms", "self_excluded", "chats"):
            self.assertIn(key, meta)
        self.assertEqual(meta["metric"], "estimated active time")
        self.assertEqual(meta["window"]["since"], "2026-03-01")
        self.assertIsInstance(meta["window"]["since_ts"], int)
        self.assertTrue(all(row["kind"] == "time" for row in rows))
        _meta, pending = _rows(self._ok("pending", *MARCH, "--json"))
        self.assertTrue(all(row["kind"] == "pending" for row in pending))
        _meta, projects = _rows(self._ok(*MARCH, "--json"))
        self.assertTrue(all(row["kind"] == "project" for row in projects))

    def test_rejects_malformed_arguments(self) -> None:
        cases = (
            (("--group", "day"), "--group applies to `agrep summary time`"),
            (("time", "--idle-cap", "0"), "--idle-cap must be a positive duration"),
            (("time", "--idle-cap", "soon"), "--idle-cap must be a positive duration"),
            (("--since", "2026-03-20", "--until", "2026-03-01"), "empty time window"),
            (("--since", "whenever"), "bad time 'whenever'"),
            (("nonsense",), "invalid choice: 'nonsense'"),
        )
        for argv, diagnostic in cases:
            with self.subTest(argv=argv):
                result = self.sandbox.summary(*argv)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn(diagnostic, result.stderr)


@unittest.skipUnless(os.name == "posix", "the summary sandbox relies on POSIX paths")
class SummaryEventsUnavailableTests(unittest.TestCase):
    """A briefing taken while the event store cannot be read used to drop every tool
    event without a word: times lost their event tails, statuses their tool evidence."""

    def test_unreadable_tool_events_are_disclosed(self) -> None:
        sandbox = SummarySandbox()
        self.addCleanup(sandbox.close)
        sandbox.index()
        window = ("time", *MARCH, "--project", "atlas", "--no-auto")
        result = sandbox.summary(*window, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        meta, _rows_ = _rows(result)
        self.assertEqual(meta["events_unavailable_chats"], 0)
        self.assertFalse(any("tool events" in line for line in meta["caveats"]))
        # a published database generation the marker does not match: no lookup can verify
        marker = sandbox.data / "events" / ".generation"
        marker.write_bytes(b"0" * len(marker.read_bytes()))
        result = sandbox.summary(*window, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        meta, _rows_ = _rows(result)
        self.assertGreater(meta["events_unavailable_chats"], 0)
        self.assertTrue(any("tool events are unavailable right now" in line
                            for line in meta["caveats"]))
        human = sandbox.summary(*window)
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn("tool events are unavailable right now for", human.stderr)
        # A marker left behind waits for the next index pass, not for a retry.
        self.assertIn("`agrep index`", human.stderr)
        self.assertNotIn("retry in a moment", human.stderr)


if __name__ == "__main__":
    unittest.main()
