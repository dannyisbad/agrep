"""Regression proofs for the degraded JSONL search lane.

These tests pin work avoided, not just a generous wall-clock timeout: phrase and
all-terms search share one corpus walk, and event payloads reject non-candidate
JSON rows before parsing.  The real-corpus timing gate is exercised separately.
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from _test_support import isolate_data_dir


isolate_data_dir()

import compact
import events
import explore
import indexd_runtime
import search


BOUND_FIXTURE = (
    Path(__file__).parent / "fixtures" / "fallback_scan_conformance.json")


def _row(session: str, turn: int, text: str, *, who: str = "user") -> dict:
    return {
        "session": session,
        "turn": turn,
        "ts": turn * 1000,
        "agent": "codex",
        "project": "fixture",
        "concept": "",
        "model": "gpt-fixture",
        "model_source": "explicit",
        "who": who,
        "text": text,
        "low": text.lower(),
        "content_digest": compact.content_digest(text),
    }


def _spec(q: str = "the") -> search.QuerySpec:
    return search.QuerySpec(
        q=q, mode="keyword", limit=7, sort="score", agent=None,
        project=None, who=None, model=None, model_soft=False, chat=None,
        since_ms=None, until_ms=None, exhaustive=False, session_limit=None,
        include_tools=True, exclude_session=None,
        exclude_session_from_turn=None, allow_fallback=True,
        exact_totals=False, family_diverse=False,
        semantic_timeout_s=None, exclude_project=None,
    )


def _write_event_proof(
        directory: Path, generation: bytes, *, agent: str = "codex",
        count: int, root_a: int, root_b: int) -> Path:
    store = directory / events.EVENT_STORE_NAME
    marker = directory / events.EVENT_GENERATION_NAME
    family = dict(events._event_store_stamp(store))
    proof = {
        "version": events.EVENT_PROOF_VERSION, "agents": [agent],
        "store": events._event_seal_identity(family[""]),
        "wal": (None if family.get("-wal") is None
                else events._event_seal_identity(family["-wal"])),
        "generation": events._event_seal_identity(
            events._event_file_stamp(marker)),
        "generation_value": list(generation),
        "inventory_hash": root_a, "inventory_hash_b": root_b,
        "inventory_count": count, "stats_hash": 0, "stats_hash_b": 0,
        "canaries": [],
    }
    path = directory.parent / f".events_complete.{agent}.json"
    path.write_text(json.dumps(proof, separators=(",", ":")))
    return path


def _create_sealed_event_store(directory: Path, payloads: list[bytes]) -> bytes:
    directory.mkdir()
    store = directory / events.EVENT_STORE_NAME
    db = sqlite3.connect(store)
    db.executescript(
        "CREATE TABLE event_sessions (name TEXT PRIMARY KEY, agent TEXT NOT NULL, "
        "session TEXT NOT NULL, hash INTEGER NOT NULL, n_events INTEGER NOT NULL, "
        "payload BLOB NOT NULL, digest BLOB NOT NULL, stats BLOB NOT NULL) WITHOUT ROWID;"
        "CREATE TABLE event_meta (key TEXT PRIMARY KEY, value BLOB NOT NULL) WITHOUT ROWID;"
        "CREATE TABLE event_agent_state (agent TEXT PRIMARY KEY, row_count INTEGER NOT NULL, "
        "root_a INTEGER NOT NULL, root_b INTEGER NOT NULL, calls INTEGER NOT NULL, "
        "fails INTEGER NOT NULL, known INTEGER NOT NULL, subagents INTEGER NOT NULL) WITHOUT ROWID;")
    db.executemany(
        "INSERT INTO event_sessions VALUES(?,?,?,?,?,?,?,?)",
        [(f"s{i}.jsonl", "codex", f"s{i}", 0, 1, payload,
          events._event_payload_digest(payload), b"{}")
         for i, payload in enumerate(payloads)])
    db.execute(
        "INSERT INTO event_agent_state VALUES(?,?,?,?,?,?,?,?)",
        ("codex", len(payloads), 11, 12, 0, 0, 0, 0))
    manifest = b"{}"
    generation = events._event_generation_token(manifest)
    db.executemany(
        "INSERT INTO event_meta VALUES(?,?)",
        [("manifest", manifest), ("generation", generation)])
    db.commit()
    db.close()
    (directory / events.EVENT_GENERATION_NAME).write_bytes(generation)
    _write_event_proof(
        directory, generation, count=len(payloads), root_a=11, root_b=12)
    return generation


class OnePassKeywordTerms(unittest.TestCase):
    def test_combined_lane_matches_two_pass_reference_in_one_walk(self) -> None:
        rows = [
            _row("phrase", 1, "the race condition is reproducible"),
            _row("scatter", 2, "race persists after retries; condition confirmed"),
            _row("reverse", 3, "condition appears only after the race"),
            _row("unicode", 4, "race persists; condıtıon confirmed"),
            _row("miss", 5, "race without the other token"),
        ]
        calls = 0

        def corpus(_flt=None):
            nonlocal calls
            calls += 1
            return iter(rows)

        with mock.patch.object(explore, "_freshen", return_value=None), \
                mock.patch.object(explore, "_iter_kw_corpus", side_effect=corpus):
            combined = explore.keyword_search(
                "race condition", 100, row_keys=True, terms=True)
            merged = search._augment_phrase_hits(
                combined["hits"], combined["term_hits"])
        self.assertEqual(calls, 1)

        calls = 0
        with mock.patch.object(explore, "_freshen", return_value=None), \
                mock.patch.object(explore, "_iter_kw_corpus", side_effect=corpus):
            phrase = explore.keyword_search(
                "race condition", 100, row_keys=True)["hits"]
            terms = search._terms_scan("race condition", 100)["hits"]
            reference = search._augment_phrase_hits(phrase, terms)
        self.assertEqual(calls, 2)
        self.assertEqual(merged, reference)


class BoundedJsonlFrontier(unittest.TestCase):
    def test_only_non_exhaustive_single_ascii_score_shape_is_bounded(self) -> None:
        spec = _spec()
        self.assertEqual(search._jsonl_bounded_single_shape(spec), "the")
        self.assertEqual(
            search._jsonl_bounded_single_shape(
                replace(spec, exact_totals=True)),
            "the")
        exhaustive = [
            replace(spec, exhaustive=True), replace(spec, family_diverse=True),
            replace(spec, sort="time"), replace(spec, mode="word"),
            replace(spec, q="two words"), replace(spec, q="café"),
        ]
        self.assertTrue(all(
            search._jsonl_bounded_single_shape(item) is None
            for item in exhaustive))
        self.assertEqual(
            search._jsonl_bounded_single_shape(replace(spec, q="issue")),
            "issue")

    def test_frontier_matches_exhaustive_score_and_ties_on_random_rows(self) -> None:
        rng = random.Random(731)
        now_ms = 2_000_000_000_000
        rows = []
        for index in range(240):
            occurrences = rng.randrange(5)
            text = " prefix ".join(["the"] * occurrences) or "ordinary miss"
            who = rng.choice(("user", "agent", "tool"))
            row = _row(f"session-{index:03}", rng.randrange(8), text, who=who)
            row["ts"] = now_ms - rng.randrange(180) * 86_400_000
            row["project"] = rng.choice(("product", "bench", "workbench"))
            if who == "tool":
                row["text"] = "shell: command\n" + text
                row["low"] = row["text"].lower()
                row["payload_bounds"] = (len("shell: command\n"), len(row["text"]))
                row["content_digest"] = compact.content_digest(row["text"])
            rows.append(row)
        rng.shuffle(rows)

        def corpus(_flt=None):
            return iter(rows)

        spec = _spec()
        boundary = search._prepare_boundary("the", "keyword", None)
        with mock.patch.object(explore, "_freshen", return_value=None), \
                mock.patch.object(explore, "_iter_kw_corpus", side_effect=corpus), \
                mock.patch.object(search.time, "time", return_value=now_ms / 1000), \
                mock.patch.object(search, "_native_boundary_scores", return_value=False):
            exhaustive = explore.keyword_search("the", 10_000)
            ranked = search._rank(
                exhaustive["hits"], "the", "keyword", "score",
                boundary=boundary, top_k=spec.limit)
            expected = ranked[:spec.limit]
            with mock.patch.object(
                    explore, "scan_hit", wraps=explore.scan_hit) as rendered:
                bounded = search._jsonl_bounded_single_keyword_rows(
                    spec, {}, boundary)

        self.assertIsNotNone(bounded)
        self.assertEqual(bounded["hits"], expected)
        self.assertEqual(bounded["total"], exhaustive["total"])
        self.assertEqual(bounded["chats"], exhaustive["chats"])
        self.assertEqual(bounded["phrase_chats"], exhaustive["chats"])
        self.assertEqual(
            bounded["tool_hits"],
            sum(hit["who"] == "tool" for hit in ranked))
        self.assertTrue(bounded["totals_exact"])
        self.assertLess(rendered.call_count, bounded["total"])
        result = search._finalize_query(spec, search.LaneResult(
            hits=list(bounded["hits"]), engine="jsonl", boundary=boundary,
            pre_ranked=True, bounded_rows=bounded))
        self.assertEqual(result["phrase_chats"], bounded["chats"])

    def test_frontier_does_not_render_known_losers(self) -> None:
        now_ms = 2_000_000_000_000
        rows = [_row("winner", 1, "the winner")]
        rows[0]["ts"] = now_ms
        for index in range(100):
            row = _row(f"old-{index:03}", 1, "the old candidate")
            row["ts"] = 0
            rows.append(row)

        with mock.patch.object(explore, "_freshen", return_value=None), \
                mock.patch.object(
                    explore, "_iter_kw_corpus",
                    side_effect=lambda _flt=None: iter(rows)), \
                mock.patch.object(search.time, "time", return_value=now_ms / 1000), \
                mock.patch.object(
                    explore, "scan_hit", wraps=explore.scan_hit) as rendered:
            bounded = search._jsonl_bounded_single_keyword_rows(
                replace(_spec(), limit=1), {},
                search._prepare_boundary("the", "keyword", None))

        self.assertEqual(bounded["total"], 101)
        self.assertEqual(bounded["hits"][0]["session"], "winner")
        published = [
            call for call in rendered.call_args_list
            if call.kwargs.get("provenance", True)
        ]
        self.assertEqual(len(published), 1)
        self.assertEqual(rendered.call_count, 2)

    def test_frontier_counts_python_re_i_exceptional_matches_exactly(self) -> None:
        now_ms = 2_000_000_000_000
        rows = [
            _row("ordinary", 1, "issue"),
            _row("exceptional", 1, "ıſſue ıſſue ıſſue"),
        ]
        for row in rows:
            row["ts"] = now_ms

        def corpus(_flt=None):
            return iter(rows)

        spec = replace(_spec(), q="issue", limit=1)
        boundary = search._prepare_boundary("issue", "keyword", None)
        with mock.patch.object(explore, "_freshen", return_value=None), \
                mock.patch.object(explore, "_iter_kw_corpus", side_effect=corpus), \
                mock.patch.object(search.time, "time", return_value=now_ms / 1000), \
                mock.patch.object(search, "_native_boundary_scores", return_value=False):
            exhaustive = explore.keyword_search("issue", 10_000)
            expected = search._rank(
                exhaustive["hits"], "issue", "keyword", "score",
                boundary=boundary, top_k=1)[:1]
            bounded = search._jsonl_bounded_single_keyword_rows(
                spec, {}, boundary)

        self.assertEqual(bounded["hits"], expected)
        self.assertEqual(bounded["hits"][0]["session"], "exceptional")


class NativeBoundaryEnvelope(unittest.TestCase):
    def test_rendered_snippet_adversaries_stay_inside_global_bounds(self) -> None:
        fixture = json.loads(BOUND_FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(fixture["schema"], 2)
        ingest = b"fixture-ingest"
        generation = __import__("hashlib").sha256(ingest).hexdigest()
        messages = {"one": [{
            "agent": "codex", "project": "product", "ts": 1000, "turn": 1,
        }]}
        for case in fixture["bound_cases"]:
            event = case["event"]
            candidate = {
                "agent": "codex", "session": "one", "ordinal": 0,
                "event_ordinal": 0, "ts": 1000, "event": event,
                "matched": case["lane"],
                "occurrences": 0 if case["lane"] == "all_terms" else 1,
                "lower_score": case["lower"], "upper_score": case["upper"],
                "refined_score": False,
            }
            response = {
                "ingest_generation": generation,
                "event_generation": "events", "candidates": [candidate],
                "_owner_order": [{
                    "agent": "codex", "session": "one", "project": "product",
                }],
            }
            with self.subTest(case=case["name"]), \
                    mock.patch.object(
                        explore, "_native_messages_for_sessions",
                        return_value=messages), \
                    mock.patch.object(explore, "_session_concept", return_value={}), \
                    mock.patch.object(
                        explore, "_read_native_generation", return_value=ingest), \
                    mock.patch.object(
                        search, "_native_boundary_scores", return_value=False):
                hits = explore.native_event_candidate_hits(case["query"], response)
                self.assertIsNotNone(hits)
                self.assertEqual(len(hits), 1)
                hit = hits[0]
                self.assertEqual(
                    hit.get("matched", "phrase").replace("-", "_"),
                    case["lane"])
                search._rank(
                    hits, case["query"], "keyword", "score",
                    refine_all=True, now_ms=1000)
                exact = hits[0]["score"]
                self.assertLessEqual(case["lower"], exact)
                self.assertLessEqual(exact, case["upper"])


class EventPayloadPrefilter(unittest.TestCase):
    def test_legacy_ascii_unicode_escape_is_not_rejected(self) -> None:
        payload = (
            b'{"ts":1000,"kind":"tool","name":"shell",'
            b'"input":"f\\u006fo bar","output":"done"}\n'
        )
        self.assertTrue(events.event_payload_contains_literals(
            payload, ("foo", "bar")))
        rows = events.tool_rows_from_payload(
            payload, [(1000, 1)], ("foo", "bar"))
        self.assertEqual(len(rows), 1)
        self.assertIn("foo bar", rows[0]["text"])

    def test_line_prefilter_parses_only_candidate_events(self) -> None:
        irrelevant = {
            "ts": 1000, "kind": "tool", "name": "shell",
            "input": "ordinary command", "output": "ordinary result",
        }
        match = {
            "ts": 1001, "kind": "tool", "name": "shell",
            "input": "web explorer", "output": "removed tilt ui",
        }
        payload = (
            (json.dumps(irrelevant, separators=(",", ":")) + "\n") * 1000
            + json.dumps(match, separators=(",", ":")) + "\n"
        ).encode()
        real_loads = json.loads
        with mock.patch.object(events.json, "loads", wraps=real_loads) as loads:
            rows = events.tool_rows_from_payload(
                payload, [(1000, 1)],
                ("web", "explorer", "removed", "tilt", "ui"))
        self.assertEqual(loads.call_count, 1)
        self.assertEqual(len(rows), 1)
        self.assertIn("web explorer", rows[0]["text"])

    def test_sql_filter_prefers_one_safe_run(self) -> None:
        # The SQL stage uses only safe `loc`; the raw matcher remains responsible
        # for long-s, dotless-i, and Kelvin-sign Python-re.I semantics.
        payloads = [
            "plain SITE lock",
            "unicode ſıte locK",
            r"escaped \u017f\u0131te loc\u212a",
            "unrelated payload",
        ]
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE event_sessions(payload BLOB NOT NULL)")
        db.executemany(
            "INSERT INTO event_sessions(payload) VALUES(?)",
            [(value.encode(),) for value in payloads])
        clause, params = events._event_sql_literal_filter(("site", "lock"))
        self.assertEqual(clause.count("instr"), 2)
        self.assertEqual(params, ("loc", b"\\u00"))
        kept = [row[0] for row in db.execute(
            "SELECT CAST(payload AS TEXT) FROM event_sessions WHERE " + clause,
            params)]
        db.close()
        self.assertEqual(kept, payloads[:3])

    def test_sql_filter_bounds_exceptional_unicode_scans(self) -> None:
        payloads = ["plain ISSUE", "unicode İssue", "unicode ıſſue", "unrelated"]
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE event_sessions(payload BLOB NOT NULL)")
        db.executemany(
            "INSERT INTO event_sessions(payload) VALUES(?)",
            [(value.encode(),) for value in payloads])
        clause, params = events._event_sql_literal_filter(("issue",))
        self.assertLessEqual(clause.count("instr"), 5)
        self.assertEqual(params[0], "issue")
        self.assertEqual(params[1], b"\\u0")
        kept = [row[0] for row in db.execute(
            "SELECT CAST(payload AS TEXT) FROM event_sessions WHERE " + clause,
            params)]
        db.close()
        self.assertEqual(kept, payloads[:3])

        miss_clause, miss_params = events._event_sql_literal_filter(
            ("zzqxvnothing",))
        self.assertEqual(miss_clause.count("instr"), 2)
        self.assertEqual(miss_params, ("zzqxvnoth", b"\\u00"))
        worst_clause, worst_params = events._event_sql_literal_filter(("kiss",))
        self.assertLessEqual(worst_clause.count("instr"), 7)
        self.assertLessEqual(len(worst_params), 7)

        false_prefix = b'{"kind":"tool","input":"\\u0180 unrelated"}\n'
        self.assertFalse(events.event_payload_contains_literals(
            false_prefix, ("site",)))

    def test_store_filter_matches_unfiltered_canonical_tool_text(self) -> None:
        payloads = [
            b'{"ts":1,"kind":"tool","name":"shell",'
            b'"input":"f\\u006fo command","output":"done"}\n',
            b'{"ts":2,"kind":"tool","name":"shell",'
            b'"input":"broken command","output":"done","ok":false}\n',
            b'{"ts":3,"kind":"tool","name":"read",'
            b'"input":"ordinary command","output":"done"}\n',
            b'{"ts":4,"kind":"tool","name":"read",'
            b'"input":"\\u017Fite marker","output":"done"}\n',
            b'{"ts":5,"kind":"tool","name":"read",'
            b'"input":"\\u212Aey marker","output":"done"}\n',
        ]
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
                events, "EVENTS_DIR", Path(td) / "events"):
            events.EVENTS_DIR.mkdir()
            store = events.EVENTS_DIR / events.EVENT_STORE_NAME
            db = sqlite3.connect(store)
            db.executescript(
                "CREATE TABLE event_sessions (name TEXT PRIMARY KEY, agent TEXT NOT NULL, "
                "session TEXT NOT NULL, hash INTEGER NOT NULL, n_events INTEGER NOT NULL, "
                "payload BLOB NOT NULL, digest BLOB NOT NULL, stats BLOB NOT NULL) WITHOUT ROWID;"
                "CREATE TABLE event_meta (key TEXT PRIMARY KEY, value BLOB NOT NULL) WITHOUT ROWID;")
            db.executemany(
                "INSERT INTO event_sessions VALUES(?,?,?,?,?,?,?,?)",
                [(f"s{i}.jsonl", "codex", f"s{i}", 0, 1, payload,
                  events._event_payload_digest(payload), b"{}")
                 for i, payload in enumerate(payloads)])
            manifest = b"{}"
            generation = events._event_generation_token(manifest)
            db.executemany(
                "INSERT INTO event_meta VALUES(?,?)",
                [("manifest", manifest), ("generation", generation)])
            db.commit()
            db.close()
            (events.EVENTS_DIR / events.EVENT_GENERATION_NAME).write_bytes(
                generation)

            keys = [("codex", f"s{i}") for i in range(len(payloads))]

            def matching_texts(query: str, filtered: bool) -> list[str]:
                literals = (query,) if filtered else ()
                blobs = events.event_blobs_bulk(
                    keys, full=True, required_literals=literals)
                texts = []
                for _agent, _session, payload in blobs:
                    rows = events.tool_rows_from_payload(
                        payload, [(1, 1)], literals)
                    texts.extend(row["text"] for row in rows
                                 if re.search(
                                     re.escape(query), row["text"], re.I))
                return texts

            for query in (
                    "foo", "oo", "failed", "ail", "shell:", "ell:",
                    "site", "key"):
                with self.subTest(query=query):
                    expected = matching_texts(query, False)
                    self.assertTrue(expected)
                    self.assertEqual(
                        matching_texts(query, True),
                        expected,
                    )

    def test_unsealed_filter_validates_non_candidates_before_searching(
        self,
    ) -> None:
        nonmatch = b'{"ts":1,"kind":"tool","input":"needle only"}\n'
        matching = b'{"ts":2,"kind":"tool","input":"needle other"}\n'
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
                events, "EVENTS_DIR", Path(td) / "events"):
            events.EVENTS_DIR.mkdir()
            store = events.EVENTS_DIR / events.EVENT_STORE_NAME
            db = sqlite3.connect(store)
            db.executescript(
                "CREATE TABLE event_sessions (name TEXT PRIMARY KEY, agent TEXT NOT NULL, "
                "session TEXT NOT NULL, hash INTEGER NOT NULL, n_events INTEGER NOT NULL, "
                "payload BLOB NOT NULL, digest BLOB NOT NULL, stats BLOB NOT NULL) WITHOUT ROWID;"
                "CREATE TABLE event_meta (key TEXT PRIMARY KEY, value BLOB NOT NULL) WITHOUT ROWID;")
            db.executemany(
                "INSERT INTO event_sessions VALUES(?,?,?,?,?,?,?,?)",
                [("a.jsonl", "codex", "a", 0, 1, nonmatch, b"bad", b"{}"),
                 ("b.jsonl", "codex", "b", 0, 1, matching, b"bad", b"{}")])
            manifest = b"{}"
            generation = events._event_generation_token(manifest)
            db.executemany(
                "INSERT INTO event_meta VALUES(?,?)",
                [("manifest", manifest), ("generation", generation)])
            db.commit()
            db.close()
            (events.EVENTS_DIR / events.EVENT_GENERATION_NAME).write_bytes(generation)

            real_digest = events._event_payload_digest
            with mock.patch.object(
                    events, "_event_payload_digest", wraps=real_digest) as digest:
                with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
                    list(events.event_blobs_bulk(
                        [("codex", "a"), ("codex", "b")], full=True,
                        required_literals=("needle", "other")))
            self.assertEqual(digest.call_count, 1)
            self.assertEqual(digest.call_args.args[0], nonmatch)

    def test_unsealed_replay_spills_and_closes_its_buffer_on_error(self) -> None:
        matching = (
            b'{"ts":1,"kind":"tool","input":"needle other '
            + b"x" * (8 * 1024 * 1024) + b'"}\n')
        nonmatch = b'{"ts":2,"kind":"tool","input":"ordinary"}\n'
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
                events, "EVENTS_DIR", Path(td) / "events"):
            _create_sealed_event_store(events.EVENTS_DIR, [matching, nonmatch])
            (events.EVENTS_DIR.parent / ".events_complete.codex.json").unlink()
            store = events.EVENTS_DIR / events.EVENT_STORE_NAME
            db = sqlite3.connect(store)
            db.execute(
                "UPDATE event_sessions SET payload=? WHERE name='s1.jsonl'",
                (b'{"ts":2,"kind":"tool","input":"corrupt"}\n',))
            db.commit()
            db.close()
            buffers = []
            real_spool = tempfile.SpooledTemporaryFile

            def tracked_spool(*args, **kwargs):
                buffer = real_spool(*args, **kwargs)
                buffers.append(buffer)
                return buffer

            with mock.patch(
                    "tempfile.SpooledTemporaryFile",
                    side_effect=tracked_spool):
                with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
                    list(events.event_blobs_bulk(
                        [("codex", "s0"), ("codex", "s1")], full=True,
                        required_literals=("needle", "other")))
            self.assertEqual(len(buffers), 1)
            self.assertTrue(buffers[0]._rolled)
            self.assertTrue(buffers[0].closed)

    def test_v10_seal_allows_candidate_only_digest_work(self) -> None:
        nonmatch = b'{"ts":1,"kind":"tool","input":"ordinary only"}\n'
        matching = b'{"ts":2,"kind":"tool","input":"needle other"}\n'
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
                events, "EVENTS_DIR", Path(td) / "events"):
            _create_sealed_event_store(events.EVENTS_DIR, [nonmatch, matching])
            real_digest = events._event_payload_digest
            with mock.patch.object(
                    events, "_event_payload_digest", wraps=real_digest) as digest:
                rows = list(events.event_blobs_bulk(
                    [("codex", "s0"), ("codex", "s1")], full=True,
                    required_literals=("needle", "other")))
            self.assertEqual(rows, [("codex", "s1", matching)])
            self.assertEqual(digest.call_count, 1)
            self.assertEqual(digest.call_args.args[0], matching)

    def test_restored_mtime_tamper_cannot_hide_a_former_match(self) -> None:
        matching = b'{"ts":1,"kind":"tool","input":"needle other"}\n'
        hidden = matching.replace(b"needle other", b"xxxxxx xxxxx")
        self.assertEqual(len(hidden), len(matching))
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
                events, "EVENTS_DIR", Path(td) / "events"):
            _create_sealed_event_store(events.EVENTS_DIR, [matching])
            store = events.EVENTS_DIR / events.EVENT_STORE_NAME
            before = store.stat()
            db = sqlite3.connect(store)
            db.execute("UPDATE event_sessions SET payload=? WHERE name='s0.jsonl'", (hidden,))
            db.commit()
            db.close()
            os.utime(store, ns=(before.st_atime_ns, before.st_mtime_ns))
            with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
                list(events.event_blobs_bulk(
                    [("codex", "s0")], full=True,
                    required_literals=("needle", "other")))

    def test_same_mtime_replacement_forces_exhaustive_validation(self) -> None:
        nonmatch = b'{"ts":1,"kind":"tool","input":"ordinary only"}\n'
        matching = b'{"ts":2,"kind":"tool","input":"needle other"}\n'
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
                events, "EVENTS_DIR", Path(td) / "events"):
            _create_sealed_event_store(events.EVENTS_DIR, [nonmatch, matching])
            store = events.EVENTS_DIR / events.EVENT_STORE_NAME
            replacement = store.with_suffix(".replacement")
            shutil.copy2(store, replacement)
            os.replace(replacement, store)
            real_digest = events._event_payload_digest
            with mock.patch.object(
                    events, "_event_payload_digest", wraps=real_digest) as digest:
                rows = list(events.event_blobs_bulk(
                    [("codex", "s0"), ("codex", "s1")], full=True,
                    required_literals=("needle", "other")))
            self.assertEqual(rows, [("codex", "s1", matching)])
            self.assertEqual(digest.call_count, 2)

    @unittest.skipUnless(os.name == "posix", "proof symlink fixture is POSIX-only")
    def test_proof_symlink_is_not_an_authority(self) -> None:
        nonmatch = b'{"ts":1,"kind":"tool","input":"ordinary only"}\n'
        matching = b'{"ts":2,"kind":"tool","input":"needle other"}\n'
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
                events, "EVENTS_DIR", Path(td) / "events"):
            _create_sealed_event_store(events.EVENTS_DIR, [nonmatch, matching])
            proof = events.EVENTS_DIR.parent / ".events_complete.codex.json"
            outside = events.EVENTS_DIR.parent / "outside-proof.json"
            outside_body = proof.read_bytes()
            outside.write_bytes(outside_body)
            proof.unlink()
            proof.symlink_to(outside)
            real_digest = events._event_payload_digest
            with mock.patch.object(
                    events, "_event_payload_digest", wraps=real_digest) as digest:
                rows = list(events.event_blobs_bulk(
                    [("codex", "s0"), ("codex", "s1")], full=True,
                    required_literals=("needle", "other")))
            self.assertEqual(rows, [("codex", "s1", matching)])
            self.assertEqual(digest.call_count, 2)
            self.assertEqual(outside.read_bytes(), outside_body)

    def test_wal_or_proof_race_never_returns_a_trusted_partial_scan(self) -> None:
        nonmatch = b'{"ts":1,"kind":"tool","input":"ordinary only"}\n'
        matching = b'{"ts":2,"kind":"tool","input":"needle other"}\n'
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
                events, "EVENTS_DIR", Path(td) / "events"):
            _create_sealed_event_store(events.EVENTS_DIR, [nonmatch, matching])
            with mock.patch.object(
                    events, "_event_filter_authority_stable", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "proof changed"):
                    list(events.event_blobs_bulk(
                        [("codex", "s0"), ("codex", "s1")], full=True,
                        required_literals=("needle", "other")))

            Path(f"{events.EVENTS_DIR / events.EVENT_STORE_NAME}-wal").write_bytes(b"")
            real_digest = events._event_payload_digest
            with mock.patch.object(
                    events, "_event_payload_digest", wraps=real_digest) as digest:
                rows = list(events.event_blobs_bulk(
                    [("codex", "s0"), ("codex", "s1")], full=True,
                    required_literals=("needle", "other")))
            self.assertEqual(rows, [("codex", "s1", matching)])
            self.assertEqual(digest.call_count, 2)

    def test_direct_iterator_cannot_yield_before_final_proof_check(self) -> None:
        matching = b'{"ts":2,"kind":"tool","input":"needle other"}\n'
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
                events, "EVENTS_DIR", Path(td) / "events"):
            _create_sealed_event_store(events.EVENTS_DIR, [matching])
            rows = events.event_blobs_bulk(
                [("codex", "s0")], full=True,
                required_literals=("needle", "other"))
            with mock.patch.object(
                    events, "_event_filter_authority_stable", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "proof changed"):
                    next(rows)

    def test_sealed_filter_rechecks_after_same_size_store_tamper(self) -> None:
        ordinary = (
            b'{"ts":1,"kind":"tool","input":"ordinary '
            + b"x" * 3800 + b'"}\n')
        matching = ordinary.replace(b"ordinary", b"needle other", 1)
        payloads = [ordinary] * 3999 + [matching]
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
                events, "EVENTS_DIR", Path(td) / "events"):
            _create_sealed_event_store(events.EVENTS_DIR, payloads)
            store = events.EVENTS_DIR / events.EVENT_STORE_NAME
            raw = store.read_bytes()
            offset = raw.find(b"needle other")
            self.assertGreaterEqual(offset, 0)
            real_stable = events._event_filter_authority_stable
            checks = 0

            def stable_then_tamper(*args):
                nonlocal checks
                stable = real_stable(*args)
                if stable and checks == 0:
                    with store.open("r+b", buffering=0) as handle:
                        handle.seek(offset)
                        handle.write(b"xxxxxx xxxxx")
                        os.fsync(handle.fileno())
                checks += 1
                return stable

            rows = events.event_blobs_bulk(
                [("codex", f"s{index}") for index in range(4000)],
                full=True, required_literals=("needle", "other"))
            with mock.patch.object(
                    events, "_event_filter_authority_stable",
                    side_effect=stable_then_tamper):
                with self.assertRaisesRegex(RuntimeError, "proof changed"):
                    next(rows)
            self.assertEqual(checks, 2)

    def test_no_auto_decline_does_not_consume_the_repair_retry(self) -> None:
        old_requested = events._EVENT_REPAIR_REQUESTED
        events._EVENT_REPAIR_REQUESTED = False
        try:
            indexd_runtime.defer_foreground_refresh(
                indexd_runtime.NO_AUTO_REFRESH_REASON)
            with mock.patch.object(
                    events, "data_dir_readonly", return_value=False), \
                    mock.patch.object(
                        indexd_runtime, "kick_background_repair",
                        wraps=indexd_runtime.kick_background_repair):
                events._request_event_repair()
            self.assertFalse(events._EVENT_REPAIR_REQUESTED)

            indexd_runtime.defer_foreground_refresh("")
            with tempfile.TemporaryDirectory() as td, \
                    mock.patch.object(
                        events, "data_dir_readonly", return_value=False), \
                    mock.patch.object(
                        indexd_runtime, "SEARCH_BEAT_PATH",
                        Path(td) / "search.beat"), \
                    mock.patch.object(
                        indexd_runtime, "kick_background_repair",
                        return_value=indexd_runtime.RepairKick(True, "started")) as kick:
                events._request_event_repair()
            self.assertEqual(kick.call_count, 1)
            self.assertTrue(events._EVENT_REPAIR_REQUESTED)
        finally:
            indexd_runtime.defer_foreground_refresh("")
            events._EVENT_REPAIR_REQUESTED = old_requested


if __name__ == "__main__":
    unittest.main()
