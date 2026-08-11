"""Pin the output-contract rules to their enforcement claims.

The rule-6 lesson: a rule was added to docs/OUTPUT_CONTRACTS.md by a later
commit, the Status paragraph that classifies rules was never touched, and the
unclassified rule went unenforced until an audit found it. Written rules do
not self-enforce; this suite makes an unclassified or unbacked rule a test
failure instead of a discovery.
"""

from __future__ import annotations

import contextlib
import io
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOC = REPO / "docs" / "OUTPUT_CONTRACTS.md"
sys.path.insert(0, str(REPO / "py"))
from _test_support import isolate_data_dir  # noqa: E402

isolate_data_dir()
import compact  # noqa: E402
import search  # noqa: E402
import surface_policy as surface  # noqa: E402

_RULE = re.compile(r"^(\d+)\.\s+\*\*(.+?)\*\*", re.M)
_MECHANISM = re.compile(r"`([\w./-]+\.py)`|contract-test|parity-pinned")
_STANDING_RULE = re.compile(r"^- Rule (\d+)\b", re.M)
_TARGET_RULE = re.compile(r"\brule[- ](\d+)\b", re.I)


def _status() -> str:
    text = DOC.read_text(encoding="utf-8")
    section = text.split("## Status", 1)[1] if "## Status" in text else ""
    return section.split("\n## ", 1)[0]


def _classified_rules(status: str) -> set[str]:
    standing, marker, target = status.partition("\nTarget")
    target_rules = _TARGET_RULE.findall(target) if marker else []
    return {*_STANDING_RULE.findall(standing), *target_rules}


class OutputContractRules(unittest.TestCase):
    def test_machine_contract_names_every_non_routing_search_surface(self) -> None:
        machine = DOC.read_text(encoding="utf-8").split(
            "## Deterministic machine output", 1)[1].split(
                "## Interactive output", 1)[0]
        for surface in ("--flat", "piped TSV", "--lexical", "-l", "-c",
                        "--count-by-tier", "--json"):
            self.assertIn(surface, machine)
        self.assertIn(
            "interactive content-term recovery", " ".join(machine.split()))

    def test_every_rule_is_classified_standing_or_target(self) -> None:
        text = DOC.read_text(encoding="utf-8")
        rules = [n for n, _ in _RULE.findall(text)]
        self.assertTrue(rules, "no numbered rules found - the parser drifted")
        classified = _classified_rules(_status())
        unclassified = [n for n in rules if n not in classified]
        self.assertEqual(
            unclassified, [],
            "rules exist in the doc but are named nowhere in Status - an "
            f"unclassified rule is enforced by nobody: {unclassified}")

    def test_classification_parser_ignores_prose_and_numeric_prefixes(self) -> None:
        status = ("Standing today:\n\nprose mentions rule 1.\n"
                  "- Rule 10 (future) - contract-tested.\n\n"
                  "Target-state: rule 2 generalized.")
        self.assertEqual(_classified_rules(status), {"2", "10"})

    def test_standing_rules_name_an_enforcing_mechanism(self) -> None:
        standing = _status().split("Target", 1)[0]
        # only the classification bullets count; prose may name a rule while
        # explaining it, which is not a claim that anything enforces it
        clauses = re.split(r"(?=^- Rule \d)", standing, flags=re.M)
        missing = [
            match.group(1) for clause in clauses
            if (match := re.match(r"- Rule (\d)", clause))
            and not _MECHANISM.search(clause)
        ]
        self.assertEqual(
            missing, [],
            "rules claim to be standing without naming the test or module "
            f"that enforces them: {missing}")

    def test_compact_completeness_claim_uses_the_fixture_locked_renderer(
            self) -> None:
        text = DOC.read_text(encoding="utf-8")
        machine, interactive = text.split("## Interactive output", 1)
        self.assertNotIn("Compact output stays silent", machine)
        self.assertIn("Compact output stays silent", interactive)
        self.assertIn("Compact output stays silent when an exact", text)
        self.assertRegex(
            text, r"Every\s+incomplete compact page emits one line")
        self.assertEqual(
            surface.compact_completeness_line(
                exact_total=20, floor=None, shown=16, exhaustible=True,
                continuation="agrep --more m.fixture"),
            "20 matches · more: agrep --more m.fixture")
        self.assertEqual(
            surface.compact_completeness_line(
                exact_total=None, floor=195_998, shown=16, exhaustible=True,
                continuation="agrep --more m.fixture"),
            "195,998+ matches (floor; -c exact) · more: "
            "agrep --more m.fixture")
        self.assertEqual(
            surface.compact_completeness_line(
                exact_total=None, floor=None, shown=16, exhaustible=True,
                continuation="agrep --more m.fixture"),
            "16 shown · total unknown · more: agrep --more m.fixture")

        page = compact.CompactPage(
            tuple({"hit": {}, "line": str(index)} for index in range(16)),
            True, "m.fixture", "row-cap", 17, exact_total=20)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            search._compact_summary(page)
        self.assertEqual(
            stderr.getvalue(),
            "20 matches · more: agrep --more m.fixture\n",
        )

        exhausted = compact.CompactPage(
            ({"hit": {}, "line": "one"},), False, None, "exhausted", 1,
            exact_total=1)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            search._compact_summary(exhausted)
        self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
