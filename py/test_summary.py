"""`agrep summary` - per-project briefings, pending items and estimated active time.

Black-box: the fixture store under py/fixtures/summary is ingested by the real
binary into an isolated sandbox, then `python py/summary.py ...` is driven as a
subprocess. Timestamps in the fixtures are absolute (March 2026) except one
chat templated relative to now, which proves the default 7d window.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "py" / "fixtures" / "summary"
TIME_TEMPLATE = re.compile(r"\{\{t((?:[+-]\d+[dhms])+)\}\}")
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

        for source in sorted((FIXTURES / "store").rglob("*.jsonl")):
            relative = source.relative_to(FIXTURES / "store")
            destination = self.home / ("." + relative.parts[0]) / Path(*relative.parts[1:])
            destination.parent.mkdir(parents=True, exist_ok=True)
            content = TIME_TEMPLATE.sub(timestamp, source.read_text(encoding="utf-8"))
            content = content.replace("{{home}}", str(self.home))
            if "{{" in content:
                raise AssertionError(f"unexpanded fixture template: {source}")
            destination.write_text(content, encoding="utf-8")
        # opencode keeps its chats in SQLite; the seed carries the same synthetic matrix
        seed = FIXTURES / "store" / "opencode" / "seed.sql"
        database = self.home / ".local" / "share" / "opencode" / "opencode.db"
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

    def test_capped_todo_list_recovers_complete_items_or_is_no_evidence(self) -> None:
        # both lists exceed the 800-char event cap; S13's kept items are all done, so its clean
        # final reply decides; S14's kept items include open ones, the ones past the cap unseen
        _meta, items = _rows(self._ok("pending", *APRIL, "--project", "foxtrot", "--json"))
        self.assertEqual([(i["session"], i["status"]) for i in items], [(S14, "todo_open")])
        self.assertEqual(items[0]["items"], [f"migrate the foxtrot {name} service"
                                             for name in ("beta", "gamma", "delta", "epsilon", "zeta")])
        self.assertEqual(items[0]["caveats"],
                         ["todo list capped at index time; items past the cap were not seen"])

    def test_next_steps_section_counts_only_when_it_ends_the_reply(self) -> None:
        # S15 planned "Next steps" before its tool call and finished with "All done."; S16's
        # section ends the reply with only a sign-off after it, its first bullet two sentences long
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
        self.assertFalse(any(i["status"] == "unknown" for i in pending.values()), pending)

    def test_codex_update_plan_is_the_chats_todo_list(self) -> None:
        pending = self._pending_by_session("--agent", "codex")
        plan = pending[MX["codex"]["todo"]]
        self.assertEqual((plan["status"], plan["items"]),
                         ("todo_open", ["port the papa writer", "update the papa docs"]))
        self.assertNotIn(MX["codex"]["finished"], pending)

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
        self.assertEqual((meta["chats"], meta["side_chats"]), (7, 2))

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
        self.assertEqual([(p["project"], p["chats"]) for p in projects], [("atlas", 1)])
        self.assertEqual([c["session"] for c in projects[0]["worked_on"]], [S7])

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
        hidden = self.sandbox.summary("pending", "--json", env_overrides=caller)
        self.assertNotEqual(hidden.returncode, 0)
        meta, items = _rows(hidden)
        self.assertEqual((items, meta["self_excluded"]), ([], 1))
        human = self.sandbox.summary("pending", env_overrides=caller)
        self.assertIn("excluded 1 chat from the current window", human.stderr)
        with_self = self._ok("pending", "--self", "--json", env_overrides=caller)
        self.assertEqual([i["session"] for i in _rows(with_self)[1]], [S7])
        # a human shell sees the chat without any notice
        plain = self._ok("pending", "--json")
        self.assertEqual([i["session"] for i in _rows(plain)[1]], [S7])
        self.assertEqual(_rows(plain)[0]["self_excluded"], 0)

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


if __name__ == "__main__":
    unittest.main()
