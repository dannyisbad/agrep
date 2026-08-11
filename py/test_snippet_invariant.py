"""The snippet-match invariant (goal-10 owner bug): every rendered snippet
contains the matched span, and highlighting marks the actual matched text.

Pinned per lane: corpusdb FTS (phrase/word/terms/regex/content), the jsonl
scans, the compact-page 32-char re-cut, both classic emitters, and recall's
collapsed weak line. Substring matches (~substr: 'search' inside 'research')
and deep matches (past any head window) are the owner-reported shapes.
Semantic rows make no lexical claim and are exempt but must stay labeled.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "py"))

from _test_support import isolate_data_dir  # noqa: E402

DATA_DIR = isolate_data_dir()
import boundary_rank  # noqa: E402
import common  # noqa: E402
import corpusdb  # noqa: E402
import explore  # noqa: E402
import recall  # noqa: E402
import search  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;]*m")
MARK = "\x1b[1;31m"

DEEP_PAD = "filler word block " * 40  # ~720 chars: past every head window
ROWS = (
    # session, turn, who, text
    ("substrhost1", 1, "user",
     "we are doing an independent web-research sweep for a real apartment"),
    ("deepmatch01", 2, "agent",
     DEEP_PAD + " the hidden search target sits deep in this row " + DEEP_PAD),
    ("capitalrow1", 3, "user", "Search me: the capitalized opener"),
    ("scatterrow1", 4, "user",
     "alpha token opens this row " + DEEP_PAD + " and omega closes it"),
    ("toolrow0001", 5, "tool",
     '{"name":"grep","input":"-r pattern","output":"' + "pad " * 60
     + 'search inside tool payload"}'),
)


def _memory_corpus() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.executescript(corpusdb._SCHEMA_SQL)
    db.executemany(corpusdb._INS, [
        (session, turn, 1000 + turn, "codex", "proj", "", "", "", who, text)
        for session, turn, who, text in ROWS])
    db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
    return db


def _write_scan_fixture() -> None:
    messages = common.MESSAGES_PATH
    with messages.open("w", encoding="utf-8") as f:
        for session, turn, who, text in ROWS:
            f.write(json.dumps({
                "id": f"codex:{session}:{turn}", "agent": "codex",
                "project": "proj", "session": session, "turn": turn,
                "ts": 1000 + turn, "who": who, "text": text,
            }) + "\n")
    with (common.DATA_DIR / "sessions.jsonl").open(
            "w", encoding="utf-8") as f:
        for session, turn, _who, text in ROWS:
            f.write(json.dumps({
                "session": session, "agent": "codex", "project": "proj",
                "n": 1, "first_ts": 1000 + turn, "last_ts": 1000 + turn,
                "first_text": text[:120],
            }) + "\n")
    explore._freshen()


def _snippets(result: dict) -> list[str]:
    hits = result["hits"]
    assert hits, "lane found nothing - the fixture no longer exercises it"
    return [hit["snippet"] for hit in hits]


class CorpusDbLaneSnippets(unittest.TestCase):
    """The FTS engine: every lane's snippet window holds its own match."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = _memory_corpus()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.close()

    def test_keyword_lane_including_substring_hosts(self) -> None:
        result = corpusdb.keyword(self.db, "search", 40)
        self.assertGreaterEqual(len(result["hits"]), 3)
        for snippet in _snippets(result):
            self.assertIn("search", snippet.lower(), snippet)

    def test_word_lane(self) -> None:
        for snippet in _snippets(corpusdb.word(self.db, "search", 40)):
            self.assertIn("search", snippet.lower(), snippet)

    def test_terms_lane_stitches_every_scattered_token(self) -> None:
        result = corpusdb.keyword_terms(self.db, "alpha omega", 40)["terms"]
        for snippet in _snippets(result):
            self.assertIn("alpha", snippet.lower(), snippet)
            self.assertIn("omega", snippet.lower(), snippet)

    def test_regex_lane(self) -> None:
        result = corpusdb.regex(self.db, r"sea\w+ target", 40)
        for snippet in _snippets(result):
            self.assertIn("search target", snippet.lower(), snippet)

    def test_content_lane_shows_a_matched_term(self) -> None:
        result = corpusdb.content(
            self.db, "hidden apartment capitalized nomatchword", 40)
        terms = ("hidden", "apartment", "capitalized")
        for snippet in _snippets(result):
            self.assertTrue(
                any(term in snippet.lower() for term in terms), snippet)


class ScanLaneSnippets(unittest.TestCase):
    """The jsonl fallback scans mirror the engine's containment."""

    @classmethod
    def setUpClass(cls) -> None:
        _write_scan_fixture()

    def test_word_scan(self) -> None:
        for snippet in _snippets(search._word_scan("search", 40)):
            self.assertIn("search", snippet.lower(), snippet)

    def test_regex_scan(self) -> None:
        for snippet in _snippets(search._regex_scan(r"sea\w+ target", 40)):
            self.assertIn("search target", snippet.lower(), snippet)

    def test_terms_scan_multi_span_stitcher(self) -> None:
        for snippet in _snippets(search._terms_scan("alpha omega", 40)):
            self.assertIn("alpha", snippet.lower(), snippet)
            self.assertIn("omega", snippet.lower(), snippet)

    def test_content_scan(self) -> None:
        result = search._content_scan(
            ["hidden", "apartment", "capitalized", "nomatchword"], 40)
        terms = ("hidden", "apartment", "capitalized")
        for snippet in _snippets(result):
            self.assertTrue(
                any(term in snippet.lower() for term in terms), snippet)

    def test_keyword_scan_entrypoint(self) -> None:
        for snippet in _snippets(explore.keyword_search("search", 40)):
            self.assertIn("search", snippet.lower(), snippet)

    def test_payload_snip_keeps_the_match_inside_tool_json(self) -> None:
        text = ROWS[4][3]
        start = text.lower().index("search")
        hit = explore.scan_hit(
            {"session": "toolrow0001", "agent": "codex", "project": "proj",
             "concept": "", "model": "", "model_source": "", "turn": 5,
             "ts": 1005, "who": "tool", "text": text,
             "payload_bounds": (text.index('"output"') + 10, len(text) - 2)},
            start, start + len("search"))
        self.assertIn("search", hit["snippet"].lower(), hit["snippet"])


class CompactReCutSnippets(unittest.TestCase):
    """The 32-char compact re-cut and the full page keep the matched span."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = _memory_corpus()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.close()

    def _compact(self, hit: dict, q: str) -> str:
        pat = search._match_pat(q, "keyword")
        prepared = boundary_rank.prepare_query(q)
        return search._compact_snippet(dict(hit), pat, prepared)

    def test_recut_keeps_deep_and_substring_matches(self) -> None:
        for hit in corpusdb.keyword(self.db, "search", 40)["hits"]:
            self.assertIn("search", self._compact(hit, "search").lower())

    def test_recut_keeps_every_all_terms_token(self) -> None:
        for hit in corpusdb.keyword_terms(
                self.db, "alpha omega", 40)["terms"]["hits"]:
            hit = {**hit, "matched": "all-terms"}
            recut = self._compact(hit, "alpha omega").lower()
            self.assertIn("alpha", recut)
            self.assertIn("omega", recut)

    def test_recut_passes_unmatched_windows_through_unchanged(self) -> None:
        # a semantic row merged into a keyword page has no lexical span; the
        # re-cut must not fabricate one or drop the window
        hit = {"snippet": "a meaning-lane window without the query word"}
        self.assertEqual(
            self._compact(hit, "search"), hit["snippet"])

    def test_full_compact_page_rows_carry_their_match(self) -> None:
        hits = corpusdb.keyword(self.db, "search", 40)["hits"]
        for hit in hits:
            hit.setdefault("content_digest", None)
        pat = search._match_pat("search", "keyword")
        page = search._start_compact_page(
            hits, "search", pat, corpus_more=False)
        self.assertTrue(page.lines)
        for line in page.lines:
            self.assertIn("search", line.lower(), line)


class ClassicRenderHighlight(unittest.TestCase):
    """Color on: the mark wraps the actual matched text, substring included."""

    def _grouped(self, hits, q, color=True):
        out = io.StringIO()
        pat = search._match_pat(q, "keyword")
        terms_pat = search._terms_hl_pat(q)
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            search._emit_grouped(hits, pat, color, terms_pat)
        return out.getvalue()

    @staticmethod
    def _marked(rendered: str) -> list[str]:
        return re.findall(r"\x1b\[1;31m(.*?)\x1b\[0m", rendered)

    def _hit(self, text: str, q: str, matched: str | None = None) -> dict:
        import compact
        hit = {"session": "substrhost1", "turn": 1, "ts": 1, "who": "user",
               "agent": "codex", "project": "proj",
               "snippet": text,
               "content_digest": compact.content_digest(text)}
        if matched:
            hit["matched"] = matched
        return hit

    def test_substring_match_is_highlighted_inside_its_host_word(self) -> None:
        rendered = self._grouped(
            [self._hit("an independent web-research sweep", "search")],
            "search")
        self.assertIn("search", ANSI.sub("", rendered).lower())
        self.assertIn("search", " ".join(self._marked(rendered)).lower())

    def test_any_order_rows_highlight_their_own_terms(self) -> None:
        rendered = self._grouped(
            [self._hit("alpha token … and omega closes it",
                       "alpha omega", matched="all-terms")],
            "alpha omega")
        marked = " ".join(self._marked(rendered)).lower()
        self.assertIn("alpha", marked)
        self.assertIn("omega", marked)

    def test_flat_rows_mark_the_same_spans(self) -> None:
        out = io.StringIO()
        q = "alpha omega"
        hits = [self._hit("alpha token … and omega closes it",
                          q, matched="all-terms")]
        with contextlib.redirect_stdout(out):
            search._emit_flat(hits, search._match_pat(q, "keyword"),
                              True, search._terms_hl_pat(q))
        marked = " ".join(self._marked(out.getvalue())).lower()
        self.assertIn("alpha", marked)
        self.assertIn("omega", marked)


class RecallWeakLineSnippets(unittest.TestCase):
    """A collapsed weak block keeps the row's own match (law 6)."""

    ANCHORS = staticmethod(recall._anchor_patterns)

    def test_window_prose_match_is_kept(self) -> None:
        line = recall._weak_line(
            "@substrhost1:1", "prose that mentions the search step",
            self.ANCHORS("search"))
        self.assertIn("search", line.lower())

    def test_falls_back_to_the_hits_own_snippet(self) -> None:
        # the fixed seam: a tool-output hit whose window prose never repeats
        # the query previously rendered head-of-prose without the match
        line = recall._weak_line(
            "@toolrow0001:5", "unrelated surrounding prose " * 20,
            self.ANCHORS("search"),
            fallback="…pad search inside tool payload…")
        self.assertIn("search", line.lower())

    def test_no_lexical_match_keeps_the_honest_head(self) -> None:
        line = recall._weak_line(
            "@meaningrow1:9", "semantic neighborhood text " * 20,
            self.ANCHORS("search"), fallback="")
        self.assertNotIn("search", line.lower())
        self.assertTrue(line.startswith("── @meaningrow1:9"))


if __name__ == "__main__":
    unittest.main()
