"""Structural budget for the compatibility facade."""

from __future__ import annotations

import ast
import io
from pathlib import Path
import tempfile
import tokenize
from types import SimpleNamespace
import unittest
from unittest import mock


COMMON_PATH = Path(__file__).with_name("common.py")
DEFINITION_LINES = {
    "ingest_bin": 2,
    "data_dir_usage": 2,
    "open_bounded_log": 4,
    "index_summary": 77,
    "detected_stores": 45,
    "store_freshness": 15,
    # M10: background children stamp datable log lines; M14: corpus-size
    # disclosure for fork probes.
    "_LineStampWriter": 17,
    "stamp_stdio_lines": 6,
    "committed_message_total": 15,
}
ASSIGNMENT_SHAPES = {
    "LOG_STAMP_ENV": "'AGREP_LOG_STAMP'",
    "SEMANTIC_DEFAULT_EXCLUDED_ROLES":
        "surface.SEMANTIC_DEFAULT_EXCLUDED_ROLES",
    "_INGEST_SIGNATURE_MAX_BYTES": "4096",
    "INGEST_SIG_PATH": "DATA_DIR / '.ingest.sig'",
}
ASSIGNMENT_LINES = {name: 1 for name in ASSIGNMENT_SHAPES}
EXECUTABLE_LINE_BUDGET = 187
STATEMENT_BUDGET = 148


def _docstring_nodes(tree: ast.AST) -> set[ast.Expr]:
    nodes = set()
    owners = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    for node in ast.walk(tree):
        if not isinstance(node, owners) or not node.body:
            continue
        first = node.body[0]
        if (isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            nodes.add(first)
    return nodes


def _executable_lines(source: str, docstrings: set[ast.Expr]) -> set[int]:
    doc_lines = {
        line
        for node in docstrings
        for line in range(node.lineno, node.end_lineno + 1)
    }
    ignored = {
        tokenize.ENCODING, tokenize.ENDMARKER, tokenize.INDENT,
        tokenize.DEDENT, tokenize.NEWLINE, tokenize.NL, tokenize.COMMENT,
    }
    return {
        token.start[0]
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type not in ignored and token.start[0] not in doc_lines
    }


class CommonFacadeBudgetTest(unittest.TestCase):
    def test_owned_logic_matches_the_exact_budget(self) -> None:
        source = COMMON_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        docstrings = _docstring_nodes(tree)
        executable = _executable_lines(source, docstrings)
        definitions = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        self.assertEqual(set(definitions), set(DEFINITION_LINES))
        self.assertEqual(
            {name: node.decorator_list
             for name, node in definitions.items()
             if node.decorator_list},
            {})
        actual_lines = {
            name: len(executable.intersection(
                range(node.lineno, node.end_lineno + 1)))
            for name, node in definitions.items()
        }
        self.assertEqual(actual_lines, DEFINITION_LINES)

        assignments = {}
        assignment_nodes = {}
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            self.assertEqual(len(node.targets), 1)
            self.assertIsInstance(node.targets[0], ast.Name)
            name = node.targets[0].id
            assignments[name] = ast.unparse(node.value)
            assignment_nodes[name] = node
        self.assertEqual(assignments, ASSIGNMENT_SHAPES)
        assignment_lines = {
            name: len(executable.intersection(
                range(node.lineno, node.end_lineno + 1)))
            for name, node in assignment_nodes.items()
        }
        self.assertEqual(assignment_lines, ASSIGNMENT_LINES)

        allowed = (
            ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef,
            ast.ClassDef, ast.Assign,
        )
        unexpected = [
            type(node).__name__
            for node in tree.body
            if node not in docstrings and not isinstance(node, allowed)
        ]
        self.assertEqual(unexpected, [])

        assignment_line_set = {
            line
            for node in assignment_nodes.values()
            for line in executable.intersection(
                range(node.lineno, node.end_lineno + 1))
        }
        owned_lines = sum(actual_lines.values()) + len(assignment_line_set)
        self.assertEqual(owned_lines, EXECUTABLE_LINE_BUDGET)
        top_level_imports = {
            node
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
        }
        statements = sum(
            isinstance(node, ast.stmt)
            and node not in docstrings
            and node not in top_level_imports
            for node in ast.walk(tree)
        )
        self.assertEqual(statements, STATEMENT_BUDGET)


class CommonFacadeDeadlineTest(unittest.TestCase):
    def test_expired_index_and_family_census_raise_timeout(self) -> None:
        import common
        import session_context

        with self.assertRaises(TimeoutError):
            common.index_summary(deadline=0.0)
        with self.assertRaises(TimeoutError):
            session_context.read_session_family_census(deadline=0.0)

    def test_sessions_rows_are_bounded_and_require_newline(self) -> None:
        import common
        import session_context

        cases = {
            "unterminated": b'{"session":"s","parent":""}',
            "oversize": (
                b"x" * (session_context.SESSION_JSONL_ROW_MAX_BYTES + 1)
                + b"\n"
            ),
        }
        for label, payload in cases.items():
            with self.subTest(reader="family-census", case=label), \
                    tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "sessions.jsonl").write_bytes(payload)
                (root / session_context.SESSION_FAMILY_META_FILE).write_bytes(
                    b"{}")
                (root / ".ingest.sig").write_bytes(b"0:test")
                session_context._SESSION_FAMILY_CENSUS.clear()
                with mock.patch.object(
                        session_context.json, "loads",
                        side_effect=AssertionError("bounded row was parsed")):
                    self.assertIsNone(session_context.read_session_family_census(
                        root, attempts=1))

            with self.subTest(reader="index-summary", case=label), \
                    tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "sessions.jsonl").write_bytes(payload)
                census = SimpleNamespace(
                    sessions=frozenset({"s"}),
                    proof=SimpleNamespace(ingest_signature="0:test"),
                )
                with mock.patch.object(common, "DATA_DIR", root), \
                        mock.patch.object(
                            common, "read_session_family_census",
                            return_value=census), \
                        mock.patch.object(
                            common.json, "loads",
                            side_effect=AssertionError("bounded row was parsed")):
                    self.assertIsNone(common.index_summary())

    def test_budget_is_checked_after_row_read_and_json_decode(self) -> None:
        import common
        import session_context

        family_row = b'{"session":"s","parent":""}\n'
        summary_row = b'{"session":"s","n":0,"agent":"codex","last_ts":0}\n'
        for stage, preceding_checks in (("row-read", 5), ("json-decode", 7)):
            with self.subTest(reader="family-census", stage=stage), \
                    tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "sessions.jsonl").write_bytes(family_row)
                (root / session_context.SESSION_FAMILY_META_FILE).write_bytes(
                    b"{}")
                (root / ".ingest.sig").write_bytes(b"0:test")
                session_context._SESSION_FAMILY_CENSUS.clear()
                clock = [0.0] * preceding_checks + [2.0]
                with mock.patch.object(
                        session_context.time, "monotonic", side_effect=clock):
                    with self.assertRaises(TimeoutError):
                        session_context.read_session_family_census(
                            root, attempts=1, deadline=1.0)

        census = SimpleNamespace(
            sessions=frozenset({"s"}),
            proof=SimpleNamespace(ingest_signature="0:test"),
        )
        for stage, preceding_checks in (("row-read", 3), ("json-decode", 5)):
            with self.subTest(reader="index-summary", stage=stage), \
                    tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "sessions.jsonl").write_bytes(summary_row)
                clock = [0.0] * preceding_checks + [2.0]
                with mock.patch.object(common, "DATA_DIR", root), \
                        mock.patch.object(
                            common, "read_session_family_census",
                            return_value=census), \
                        mock.patch.object(
                            common.time, "monotonic", side_effect=clock):
                    with self.assertRaises(TimeoutError):
                        common.index_summary(deadline=1.0)

    def test_index_summary_treats_recursive_json_as_proof_damage(self) -> None:
        import common

        census = SimpleNamespace(
            sessions=frozenset({"s"}),
            proof=SimpleNamespace(ingest_signature="0:test"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sessions.jsonl").write_bytes(
                b'{"session":"s","n":0,"agent":"codex","last_ts":0}\n')
            with mock.patch.object(common, "DATA_DIR", root), \
                    mock.patch.object(
                        common, "read_session_family_census",
                        return_value=census), \
                    mock.patch.object(
                        common.json, "loads", side_effect=RecursionError):
                self.assertIsNone(common.index_summary())


class DetectedStoresSchemaTest(unittest.TestCase):
    def _detect(self, stdout: str) -> tuple[list[dict], dict]:
        import common

        observation: dict = {}
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "agrep-rs"
            binary.touch()
            result = SimpleNamespace(returncode=0, stdout=stdout)
            with mock.patch.object(common, "ingest_bin", return_value=binary), \
                    mock.patch.object(
                        common.subprocess, "run", return_value=result):
                rows = common.detected_stores(observation=observation)
        return rows, observation

    def test_valid_rows_are_preserved(self) -> None:
        rows, observation = self._detect(
            '[{"name":"cursor","count":0,"detail":"present"},'
            '{"name":"windsurf","count":12}]')
        self.assertEqual(rows, [
            {"name": "cursor", "count": 0, "detail": "present"},
            {"name": "windsurf", "count": 12},
        ])
        self.assertEqual(observation["state"], "complete")

    def test_malformed_rows_fail_closed(self) -> None:
        cases = {
            "top-level-object": '{"name":"cursor","count":1}',
            "scalar-row": '["cursor"]',
            "missing-name": '[{"count":1}]',
            "empty-name": '[{"name":"","count":1}]',
            "non-string-name": '[{"name":7,"count":1}]',
            "missing-count": '[{"name":"cursor"}]',
            "boolean-count": '[{"name":"cursor","count":true}]',
            "negative-count": '[{"name":"cursor","count":-1}]',
            "fractional-count": '[{"name":"cursor","count":1.5}]',
        }
        for label, stdout in cases.items():
            with self.subTest(case=label):
                rows, observation = self._detect(stdout)
                self.assertEqual(rows, [])
                self.assertEqual(observation["state"], "unavailable")
                self.assertIn("malformed", observation["detail"])


if __name__ == "__main__":
    unittest.main()
