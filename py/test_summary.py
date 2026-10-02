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
MARCH = ("--since", "2026-03-01", "--until", "2026-03-20")


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

    def spawn(self, command, *, env_overrides=None):
        env = dict(self.env)
        for key, value in (env_overrides or {}).items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = str(value)
        return subprocess.run(
            [sys.executable, *command], cwd=self.home, env=env, input="",
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
        self.assertIn("4 projects, 9 chats in the last 2026-03-01..2026-03-20", human.stderr)

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
        self.assertIn("1 project, 2 chats in the last", human.stderr)
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
