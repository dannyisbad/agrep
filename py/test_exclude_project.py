"""F1 --exclude-project: one parser, engine-level subtraction before top-k."""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _test_support import isolate_data_dir

isolate_data_dir()
import ask  # noqa: E402
import corpusdb  # noqa: E402
import recall  # noqa: E402
import search  # noqa: E402
import segment_query  # noqa: E402
import semworker  # noqa: E402


NOW_MS = 2_000_000_000_000


class ExcludeProjectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        search._load_corpusdb()

    def _database(self, path: Path, *, noisy_rows: int = 40) -> None:
        db = sqlite3.connect(path)
        db.executescript(corpusdb._SCHEMA_SQL)
        rows = [(f"noisy-{i:02}", 0, NOW_MS - i * 60_000, "codex",
                 "/work/noisy-bench", "", "", "", "user",
                 f"needle evidence {i}") for i in range(noisy_rows)]
        # one lived hit, older and outnumbered: a page post-filter would lose it
        rows.append(("lived", 3, NOW_MS - noisy_rows * 120_000, "claude",
                     "/work/webapp", "", "", "", "user", "needle evidence kept"))
        db.executemany(corpusdb._INS, rows)
        db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
        db.execute("INSERT INTO msgs_prose_fts(rowid, text) "
                   "SELECT id, text FROM msgs WHERE who <> 'tool'")
        db.commit()
        db.close()

    def _connect_patch(self, path: Path):
        return mock.patch.object(
            search.corpusdb, "connect",
            side_effect=lambda **_kwargs: sqlite3.connect(path))

    def test_exclusion_runs_before_top_k_with_exact_totals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "corpus.db"
            self._database(path)
            with self._connect_patch(path):
                page = search.run_query(
                    "needle", limit=3, exclude_project="noisy-bench",
                    allow_fallback=False)
        # a dominant excluded project must not mask the surviving hit or its count
        self.assertEqual([hit["session"] for hit in page["hits"]], ["lived"])
        self.assertEqual(page["total"], 1)
        self.assertTrue(page.get("totals_exact", True))

    def test_exclusion_mirrors_project_substring_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "corpus.db"
            self._database(path, noisy_rows=4)
            with self._connect_patch(path):
                kept = search.run_query("needle", limit=0,
                                        exclude_project="WEBAPP",
                                        allow_fallback=False)
        sessions = {hit["session"] for hit in kept["hits"]}
        self.assertNotIn("lived", sessions)
        self.assertEqual(len(sessions), 4)

    def test_semantic_candidates_honor_the_exclusion(self) -> None:
        rows = [
            {"session": "noisy", "turn": 1, "project": "/work/noisy-bench",
             "who": "user", "ts": 1, "sem_score": 0.9},
            {"session": "lived", "turn": 2, "project": "/work/webapp",
             "who": "user", "ts": 1, "sem_score": 0.8},
        ]
        kept = search._filtered(
            rows, None, None, None, None, False,
            exclude_project="noisy-bench")
        self.assertEqual([hit["session"] for hit in kept], ["lived"])

    def test_semantic_exclusion_is_applied_before_top_k(self) -> None:
        excluded = [
            {"session": f"noisy-{index}", "turn": index,
             "project": "noisy-bench", "who": "user", "ts": index,
             "sem_score": 0.99 - index / 100}
            for index in range(8)
        ]
        lived = {"session": "lived", "turn": 9, "project": "webapp",
                 "who": "user", "ts": 9, "sem_score": 0.8}
        captured = {}

        def semantic_local(_query, k, **kwargs):
            captured.update(kwargs)
            candidates = [*excluded, lived]
            needle = kwargs.get("exclude_project")
            if needle:
                candidates = [row for row in candidates
                              if needle.lower() not in row["project"].lower()]
            return {
                "hits": candidates[:k], "truncated": False,
                "score_kind": "cosine", "semantic_status": {"state": "ready"},
                "fallback_recommended": False,
            }

        with mock.patch.object(search, "_semantic_local", side_effect=semantic_local):
            result = search.run_query(
                "meaningful query", mode="semantic", limit=3,
                exclude_project="noisy-bench")
        self.assertEqual(captured["exclude_project"], "noisy-bench")
        self.assertEqual([hit["session"] for hit in result["hits"]], ["lived"])

    def test_semantic_filter_protocol_and_metadata_selection_agree(self) -> None:
        validated = semworker._validate_request({
            "query": "meaningful query", "level": "hybrid", "k": 3,
            "filters": {"exclude_project": "noisy-bench"}, "timing": False,
        })
        self.assertEqual(validated[3]["exclude_project"], "noisy-bench")
        self.assertFalse(ask._matches(
            {"project": "NOISY-BENCH"}, {"exclude_project": "noisy"}))
        self.assertTrue(ask._matches(
            {"project": "webapp"}, {"exclude_project": "noisy"}))

        db = sqlite3.connect(":memory:")
        segment_query.register_metadata_functions(db)
        db.execute("CREATE TABLE refs(project TEXT)")
        db.executemany("INSERT INTO refs VALUES (?)", [("noisy-bench",), ("webapp",)])
        where, params = segment_query.metadata_where(
            {"exclude_project": "NOISY"})
        selected = [row[0] for row in db.execute(
            f"SELECT project FROM refs WHERE {where} ORDER BY project", params)]
        db.close()
        self.assertEqual(selected, ["webapp"])

    def test_public_parser_carries_the_flag_to_the_engine(self) -> None:
        captured = {}

        def run_query(_query, **kwargs):
            captured.update(kwargs)
            return {"hits": [], "total": 0, "chats": 0, "tool_hits": 0,
                    "engine": "corpusdb", "mode": "keyword",
                    "totals_exact": True}

        stdout = io.StringIO()
        with mock.patch.object(search.indexd_runtime, "ensure_index",
                               return_value=True), \
                mock.patch.object(search, "run_query", side_effect=run_query), \
                mock.patch.object(search, "_stream_first_run",
                                  return_value=None), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = search.main(["needle", "--exclude-project", "noisy-bench",
                              "--json"])
        self.assertEqual(rc, 1)
        self.assertEqual(captured.get("exclude_project"), "noisy-bench")
        meta = json.loads(stdout.getvalue())
        self.assertEqual(meta["kind"], "agrep-meta")

    def test_project_help_names_the_stored_label_not_a_source_path(self) -> None:
        for entrypoint in (search.main, recall.main):
            stdout = io.StringIO()
            with self.subTest(entrypoint=entrypoint.__module__), \
                    contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as stopped:
                entrypoint(["--help"])
            self.assertEqual(stopped.exception.code, 0)
            rendered = stdout.getvalue()
            self.assertIn("project label", rendered)
            self.assertNotIn("project path", rendered)


if __name__ == "__main__":
    unittest.main()
