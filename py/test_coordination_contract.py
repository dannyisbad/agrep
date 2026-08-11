"""Structural contract for docs/COORDINATION.md exclusive-create ownership."""

from __future__ import annotations

from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
PY_DIR = ROOT / "py"

def _product_sources() -> list[Path]:
    sources = [
        path for path in sorted(PY_DIR.rglob("*.py"))
        if "__pycache__" not in path.parts
        and not path.name.startswith("test_")
        and path.name not in ("_test_support.py", "selftest.py")
    ]
    for name in ("cli.py", "reindex.py", "synth_index.py"):
        sources.append(ROOT / name)
    return sources


class CoordinationContractTests(unittest.TestCase):
    def test_python_o_excl_only_in_ownerfile(self) -> None:
        offenders: list[str] = []
        for path in _product_sources():
            text = path.read_text(encoding="utf-8")
            hits = [line for line in text.splitlines() if "os.O_EXCL" in line]
            if not hits:
                continue
            name = path.relative_to(ROOT).as_posix()
            if name == "py/ownerfile.py":
                continue
            offenders.append(name)
        self.assertIn(
            "os.O_EXCL", (PY_DIR / "ownerfile.py").read_text(encoding="utf-8"))
        self.assertEqual(offenders, [],
                         "raw O_EXCL outside ownerfile.py; route new "
                         "coordination records through ownerfile and index "
                         "them in docs/COORDINATION.md")

    def test_rust_create_new_sites_are_pinned(self) -> None:
        expected = {
            # Rust half of the shared IndexLock byte protocol.
            "crates/agrep-cli/src/index_lock.rs",
            # Temporary legacy/current daemon claims fence derived adoption.
            "crates/agrep-cli/src/main.rs",
            # Unique-named staging temps for atomic publish, not coordination.
            "crates/agrep-core/src/ingest_cache.rs",
            # Private SQLite snapshots use exclusive names, not shared claims.
            "crates/agrep-core/src/ingest/mod.rs",
            "crates/agrep-core/src/ingest/registry.rs",
        }
        found = {
            path.relative_to(ROOT).as_posix()
            for path in sorted((ROOT / "crates").rglob("*.rs"))
            if "src" in path.parts
            and "create_new(true)" in path.read_text(encoding="utf-8")
        }
        self.assertEqual(found, expected)


if __name__ == "__main__":
    unittest.main()
