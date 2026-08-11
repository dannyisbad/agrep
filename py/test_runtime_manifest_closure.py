"""Every module a shipped module imports must itself be shipped.

The failure this guards: `agrep setup` shipped in a wheel but crashed at the
post-compact hook stage because cli.py did `import hookinstall` and
hookinstall.py was never added to py/runtime_manifest.json. The sdist tests
checked the manifest excluded dev files; nothing checked it was closed under
import. This walks the flat-import graph from the shipped py/ modules and
asserts every locally-resolvable import target is a shipped member too.
"""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "py" / "runtime_manifest.json"


def _shipped_sources() -> set[str]:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {entry["source"] for entry in payload["files"]}


def _shipped_flat_py_names(sources: set[str]) -> set[str]:
    """Bare importable names that ARE shipped and resolve to py/*.py.

    agrep runs with py/ on sys.path, so `import common` resolves to
    py/common.py. hookless.* is a real package and resolves normally, so it
    is out of scope for the flat-name check.
    """
    names: set[str] = set()
    for source in sources:
        p = Path(source)
        if len(p.parts) == 2 and p.parts[0] == "py" and p.suffix == ".py":
            names.add(p.stem)
    return names


def _shipped_py_parse_sources(sources: set[str]) -> list[str]:
    """Every shipped .py file whose bare imports we walk.

    This must include the root launchers (cli.py, reindex.py) — the original
    omission was cli.py importing hookinstall, so a walk restricted to py/*.py
    would not have caught it. hookless.* is skipped: it is a package with its
    own resolution, not a bare-name importer of py/ siblings.
    """
    parse: list[str] = []
    for source in sources:
        p = Path(source)
        if p.suffix != ".py":
            continue
        if p.parts[0] == "py" and (len(p.parts) != 2):
            continue  # py/hooks/*, py/hookless/* — not bare-name importers
        parse.append(source)
    return parse


class RuntimeManifestClosureTests(unittest.TestCase):
    def test_shipped_modules_import_only_shipped_modules(self) -> None:
        sources = _shipped_sources()
        shipped_flat = _shipped_flat_py_names(sources)
        # Every bare name that exists in py/ on disk, shipped or not — used to
        # tell "a py/ sibling module" from stdlib / third-party.
        all_py_names = {p.stem for p in (ROOT / "py").glob("*.py")}
        missing: list[tuple[str, str]] = []
        for source in sorted(_shipped_py_parse_sources(sources)):
            tree = ast.parse((ROOT / source).read_text(encoding="utf-8"),
                             filename=source)
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imported.add(alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom):
                    if node.level == 0 and node.module:
                        imported.add(node.module.split(".")[0])
            for target in sorted(imported):
                # A py/ sibling (bare name that exists in py/) must be shipped.
                # Other names are stdlib/third-party and out of scope.
                if target in all_py_names and target not in shipped_flat:
                    missing.append((source, target))
        self.assertEqual(
            missing, [],
            "a shipped module imports a py/ sibling that "
            f"runtime_manifest.json does not ship: {missing}")

    def test_hooks_payloads_are_shipped(self) -> None:
        """Every data file the hook installer reads at runtime."""
        sources = _shipped_sources()
        for required in ("py/hookinstall.py",
                         "py/hooks/compact-contract.md",
                         "py/hooks/codex_compact_payload.py",
                         "py/hooks/pi_postcompact.ts"):
            self.assertIn(required, sources)


if __name__ == "__main__":
    unittest.main()
