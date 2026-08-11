"""Layering contract: intra-package imports point downward or sideways.

LAYERS assigns every non-test module in py/ and hookless/ a layer, bottom-up.
An import from a lower layer into a higher one is a violation unless
ALLOWED_UPWARD still carries that exact edge; edges within a layer are free.
The table is judged on every import in the file, lazy function-level included,
in the same spirit as the common-below-indexd_runtime structural test.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

PY_DIR = Path(__file__).resolve().parent

LAYERS = {
    # 0 — foundation: leaf primitives with no intra-package imports
    "_test_support": 0,
    "boundary_rank": 0,
    "conceptpair": 0,
    "console": 0,
    "display_policy": 0,
    "fileops": 0,
    "install_lag": 0,
    "ownerfile": 0,
    "proc": 0,
    "mlx_modernbert": 0,
    "sabel_pool": 0,
    "sabel_shadow": 0,
    "surface_policy": 0,
    "winjob": 0,
    "hookless.__init__": 0,
    "hookless._log": 0,
    "hookless.proc": 0,
    "hookless.procscan": 0,
    "hookless.registry": 0,
    # 1 — observation and the store floor: the hookless watchers (product-free
    # by selftest law), the event-store reader that resolves the data dir, and
    # the settings, distribution identity, and host resource probes beside it
    "dist": 1,
    "events": 1,
    "hookless.capture": 1,
    "hookless.fswatch": 1,
    "hookless.live": 1,
    "hookless.locators": 1,
    "hookless.native": 1,
    "index_lock": 1,
    "legacy_cleanup": 1,
    "sabel_observer": 1,
    "sabel_physical": 1,
    "removal_fence": 1,
    "resources": 1,
    "settings": 1,
    # 2 — shared coordination and publication owners
    "common": 2,
    "embedding_segments": 2,
    "embedding_store": 2,
    "session_context": 2,
    # 3 — codecs and sidecars: store/page/segment formats and lane locks
    "archive": 3,
    "audit": 3,
    "compact": 3,
    "embedder": 3,
    "lifetime": 3,
    "mlx_embed": 3,
    "semantic_q8": 3,
    "semantic_segment_build": 3,
    # 4 — engine: the store/index/semantic core; its internal cycles are
    # sideways here and stay legal until the extraction train splits them
    "ask": 4,
    "corpusdb": 4,
    "embed": 4,
    "explore": 4,
    "indexd_runtime": 4,
    "indexer": 4,
    "segment_query": 4,
    "semantic": 4,
    "semworker": 4,
    "hookinstall": 4,
    "teach": 4,
    # 5 — services: composed operations over the engine
    "around": 5,
    "postcompact": 5,
    "recall": 5,
    "semantic_segment_compact": 5,
    # 6 — surfaces: user-facing verbs, process entry points, dev tools
    "doctor": 6,
    "generate_boundary_fixtures": 6,
    "indexd": 6,
    "livetui": 6,
    "resume": 6,
    "search": 6,
    "server": 6,
    "tail": 6,
    # 7 — harness: selftest exercises every layer, the sweep loads every module
    "global_state_sweep": 7,
    "selftest": 7,
}

# Upward edges that still exist. This set only ever shrinks: removing the
# import must delete its row here in the same change, and a row without a
# live edge fails the suite so fixed debt cannot quietly reappear later.
ALLOWED_UPWARD = {
    ("recall", "search"),
}


def _module_files() -> dict[str, Path]:
    files = {
        path.stem: path
        for path in PY_DIR.glob("*.py")
        # _check.py is the gitignored local scratch helper, never shipped
        if not path.name.startswith("test_") and path.name != "_check.py"
    }
    files.update({
        f"hookless.{path.stem}": path
        for path in (PY_DIR / "hookless").glob("*.py")
    })
    return files


def _as_module(dotted: str) -> str | None:
    """Map one dotted import target onto a table module; None if external."""
    if dotted == "hookless":
        return "hookless.__init__"
    if dotted.startswith("hookless."):
        # `from hookless import live` binds the submodule; a plain attribute
        # import falls back to the package body itself
        parts = dotted.split(".")
        candidate = ".".join(parts[:2])
        return candidate if candidate in LAYERS else "hookless.__init__"
    root = dotted.split(".")[0]
    return root if root in LAYERS else None


def _import_targets(node: ast.AST, importer: str) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if not isinstance(node, ast.ImportFrom):
        return []
    module = node.module or ""
    if node.level:
        package = importer.rsplit(".", 1)[0] if "." in importer else ""
        module = f"{package}.{module}".rstrip(".")
    return [f"{module}.{alias.name}" if module else alias.name
            for alias in node.names]


def _upward_edges() -> dict[tuple[str, str], list[str]]:
    sites: dict[tuple[str, str], list[str]] = {}
    for name, path in _module_files().items():
        if name not in LAYERS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            for dotted in _import_targets(node, name):
                target = _as_module(dotted)
                if target is None or target == name:
                    continue
                if LAYERS[name] < LAYERS[target]:
                    sites.setdefault((name, target), []).append(
                        f"{path.name}:{node.lineno}")
    return sites


class LayeringContractTests(unittest.TestCase):
    def test_layer_table_covers_exactly_the_shipped_modules(self) -> None:
        self.assertEqual(
            sorted(_module_files()),
            sorted(LAYERS),
            "LAYERS must name every non-test module exactly once: assign a "
            "layer to each new module and drop rows for deleted ones",
        )

    def test_every_import_edge_points_downward_or_sideways(self) -> None:
        unexpected = {
            edge: where
            for edge, where in _upward_edges().items()
            if edge not in ALLOWED_UPWARD
        }
        self.assertFalse(
            unexpected,
            "new upward imports break the layering contract; move the code "
            "down or invert the dependency instead of widening the table: "
            + "; ".join(
                f"{src}->{dst} at {', '.join(where)}"
                for (src, dst), where in sorted(unexpected.items())
            ),
        )

    def test_the_upward_allowlist_only_shrinks(self) -> None:
        stale = ALLOWED_UPWARD - set(_upward_edges())
        self.assertFalse(
            stale,
            "these upward edges no longer exist; delete their ALLOWED_UPWARD "
            f"rows so the debt cannot return: {sorted(stale)}",
        )


if __name__ == "__main__":
    unittest.main()
