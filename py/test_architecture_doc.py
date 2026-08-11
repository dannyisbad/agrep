"""Pin the design docs' file references to the tree.

The docs' shared law: a structure claim names its enforcing mechanism. This
suite makes the cheapest half of that testable - every backticked file
reference must resolve - so a claim about a test or module that was never
written fails here instead of surviving until the next audit.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOCS = (
    REPO / "docs" / "ARCHITECTURE.md",
    REPO / "docs" / "OUTPUT_CONTRACTS.md",
    REPO / "docs" / "HANDLE_IDENTITY.md",
    REPO / "docs" / "COORDINATION.md",
    REPO / "docs" / "SEARCH_RANKING.md",
)

_REF = re.compile(r"`([\w./-]+\.(?:py|md|json|rs|toml))`")


class DesignDocReferences(unittest.TestCase):
    def test_every_backticked_file_reference_resolves(self) -> None:
        for doc in DOCS:
            with self.subTest(doc=doc.name):
                text = doc.read_text(encoding="utf-8")
                missing = sorted(
                    ref for ref in set(_REF.findall(text))
                    # dot-led names are runtime data-dir artifacts, not repo files
                    if not ref.startswith(".")
                    and not (REPO / ref).exists()
                    and not (REPO / "py" / ref).exists()
                )
                self.assertEqual(
                    missing, [],
                    f"{doc.name} names files that do not exist: {missing}")

    def test_the_docs_exist(self) -> None:
        for doc in DOCS:
            with self.subTest(doc=doc.name):
                self.assertTrue(doc.exists())

    def test_handle_doc_separates_mutable_chat_from_exact_citation(self) -> None:
        text = (REPO / "docs" / "HANDLE_IDENTITY.md").read_text(encoding="utf-8")
        self.assertIn("mutable chat identity", text)
        self.assertIn("latest indexed turn", text)
        self.assertIn("cites\none digest-verified row and refuses a second turn", text)


if __name__ == "__main__":
    unittest.main()
