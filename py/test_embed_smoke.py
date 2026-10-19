from __future__ import annotations

import contextlib
import io
import json
import os
import site
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest import mock
from _test_support import isolate_data_dir

isolate_data_dir()

def setUpModule() -> None:
    home = tempfile.TemporaryDirectory(prefix="agrep-embed-home-")
    user_base = site.getuserbase()
    home_patch = mock.patch.dict(
        os.environ, {"HOME": home.name, "PYTHONUSERBASE": user_base})
    home_patch.start()
    unittest.addModuleCleanup(home_patch.stop)
    unittest.addModuleCleanup(home.cleanup)

import numpy as np

import common
import embed


class _Model:
    def embed_texts(self, texts):
        return np.ones((len(texts), 2), dtype=np.float32)


class _BoundedMessages:
    def __init__(self, messages, limit):
        self._messages = iter(messages)
        self._limit = limit
        self.consumed = 0
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if self.consumed >= self._limit:
            raise AssertionError("smoke exhausted rows beyond its requested sample")
        self.consumed += 1
        return next(self._messages)

    def close(self):
        self.closed = True


def _chunk_message(mid: str, text: str, who: str = "user") -> common.Message:
    return common.Message(
        id=mid, agent=mid.split(":", 1)[0], project="p", session="s", ts=1, turn=1,
        text=text, who=who, model="", model_source="unknown")


def _long_text(target_chars: int) -> str:
    line = "filler payload " + "x" * 65
    lines = target_chars // (len(line) + 1) + 1
    return "\n".join(f"{index} {line}" for index in range(lines))


# Shaped like a pi/omp recap: instruction prose, then underlined
# FILES/HISTORY sections carrying the content. The wording is deliberately
# NOT the production preamble: the strip anchors on shape, never prose.
_RECAP_PREAMBLE = (
    "Continue from the archive that follows; read every section first.\n"
    "\n"
    "- markers like `x:` label archived scopes.\n"
    "- middle sections may be dropped to fit a budget.\n"
    "\n"
    "Prefer re-deriving details from the workspace over guessing.\n"
    "\n"
)
_RECAP_CONTENT = (
    "FILES\n"
    "===================\n"
    "src/app.py (Read)\n"
    "\n"
    "HISTORY\n"
    "===================\n"
    "the actual conversation content lives here\n"
)


_DENSE_ROWS = {
    "ascii": "x!?" * 2000,
    "cjk": "中文内容检索" * 500,
    "dense-json": json.dumps(
        [{"id": i, "ok": True, "n": [1, 2, 3]} for i in range(150)],
        separators=(",", ":")),
    "emoji": "🫠🎉🧬🚀" * 500,
    "normalization-expansion": "\U0001d160" * 700,
}


class LongRowChunkingTests(unittest.TestCase):
    def test_short_row_embeds_exactly_one_unsuffixed_vector(self) -> None:
        message = _chunk_message("codex:s:1", "a short prompt")
        rows = list(embed._expand_source_message(message))
        self.assertEqual([row.id for row in rows], ["codex:s:1"])
        self.assertEqual(embed._embed_input(message), "a short prompt")

    def test_long_row_expands_to_chunk_ids_carrying_the_full_text(self) -> None:
        text = _long_text(4 * embed._CHUNK_BYTES)
        message = _chunk_message("codex:s:2", text)
        rows = list(embed._expand_source_message(message))
        chunks = embed._row_chunks("codex", "user", text)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(
            [row.id for row in rows],
            ["codex:s:2"] + [f"codex:s:2#c{n}" for n in range(1, len(chunks))])
        # every store id keeps the logical row's text (and therefore hash)
        self.assertTrue(all(row.text == text for row in rows))
        self.assertEqual([embed._embed_input(row) for row in rows], chunks)
        # deterministic: same text always yields the same expansion
        self.assertEqual(chunks, embed._row_chunks("codex", "user", text))

    def test_long_reply_chunks_extend_the_reply_suffix(self) -> None:
        message = _chunk_message(
            "codex:s:3#r", _long_text(2 * embed._CHUNK_BYTES), who="agent")
        rows = list(embed._expand_source_message(message))
        self.assertGreater(len(rows), 1)
        self.assertEqual(rows[1].id, "codex:s:3#r#c1")
        self.assertEqual(
            common.semantic_chunk_split("codex:s:3#r#c1"), ("codex:s:3#r", 1))

    def test_dense_rows_cover_every_character_within_token_budget(self) -> None:
        for name, text in _DENSE_ROWS.items():
            with self.subTest(script=name):
                message = _chunk_message(f"codex:{name}:1", text)
                spans = embed._row_chunk_spans(text)
                rows = list(embed._expand_source_message(message))
                chunks = [embed._embed_input(row) for row in rows]
                self.assertEqual(
                    len(rows), embed._row_chunk_count("codex", "user", text))
                self.assertEqual(len(chunks), len(spans))
                covered = 0
                for (start, end), chunk in zip(spans, chunks):
                    self.assertLessEqual(start, covered)
                    self.assertEqual(chunk, text[start:end])
                    self.assertLessEqual(
                        len(unicodedata.normalize("NFC", chunk).encode("utf-8")) + 2,
                        embed.embedder.PROFILE["max_seq"])
                    covered = end
                self.assertEqual(covered, len(text))

    def test_dense_chunks_fit_the_available_pinned_tokenizer(self) -> None:
        try:
            from tokenizers import Tokenizer
        except ImportError:
            self.skipTest("optional tokenizer runtime is unavailable")
        path = embed.embedder.model_dir() / "tokenizer.json"
        spec = embed.embedder.PROFILE["files"]["tokenizer.json"]
        if not embed.embedder._file_ok(path, *spec):
            self.skipTest("pinned tokenizer is not cached")
        tokenizer = Tokenizer.from_file(str(path))
        tokenizer.no_truncation()
        tokenizer.no_padding()
        for name, text in _DENSE_ROWS.items():
            with self.subTest(script=name):
                self.assertGreater(
                    len(tokenizer.encode(text).ids), embed.embedder.PROFILE["max_seq"])
                message = _chunk_message(f"codex:{name}:2", text)
                for row in embed._expand_source_message(message):
                    encoded = tokenizer.encode(embed._embed_input(row))
                    self.assertLessEqual(
                        len(encoded.ids), embed.embedder.PROFILE["max_seq"])

    def test_chunks_split_at_line_boundaries_and_reconstruct_exactly(self) -> None:
        text = _long_text(6 * embed._CHUNK_BYTES)
        chunks = embed._row_chunks("codex", "user", text)
        for chunk in chunks[:-1]:
            self.assertTrue(chunk.endswith("\n"))
        rebuilt = chunks[0] + "".join(
            chunk[embed._CHUNK_OVERLAP_CHARS:] for chunk in chunks[1:])
        self.assertEqual(rebuilt, text)

    def test_multibyte_text_never_splits_inside_a_codepoint(self) -> None:
        text = ("多言語のテキスト mixed with emoji 🎉 and ascii\n"
                * (3 * embed._CHUNK_BYTES // 40))
        chunks = embed._row_chunks("codex", "user", text)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            # a mid-codepoint split cannot round-trip through utf-8
            self.assertEqual(chunk.encode("utf-8").decode("utf-8"), chunk)
        rebuilt = chunks[0] + "".join(
            chunk[embed._CHUNK_OVERLAP_CHARS:] for chunk in chunks[1:])
        self.assertEqual(rebuilt, text)

    def test_multi_megabyte_row_is_capped_head_first_then_sampled(self) -> None:
        text = _long_text(3_000_000)
        spans = embed._row_chunk_spans(text)
        self.assertGreater(len(spans), embed._MAX_ROW_CHUNKS)
        message = _chunk_message("codex:s:4", text)
        rows = list(embed._expand_source_message(message))
        self.assertEqual(len(rows), embed._MAX_ROW_CHUNKS)
        capped = embed._capped_chunk_spans(spans)
        # the head stays contiguous, the remainder samples through the tail
        self.assertEqual(capped[:embed._HEAD_ROW_CHUNKS],
                         spans[:embed._HEAD_ROW_CHUNKS])
        self.assertEqual(capped[-1], spans[-1])
        self.assertEqual(sorted(set(capped)), capped)

    def test_recap_embeds_content_not_resume_instructions(self) -> None:
        message = _chunk_message(
            "pi:s:5", _RECAP_PREAMBLE + _RECAP_CONTENT, who="recap")
        self.assertEqual(embed._embed_input(message), _RECAP_CONTENT)

    def test_recap_strip_tolerates_leading_whitespace_variance(self) -> None:
        varied = "\n\n   \n " + _RECAP_PREAMBLE + _RECAP_CONTENT
        message = _chunk_message("pi:s:6", varied, who="recap")
        self.assertEqual(embed._embed_input(message), _RECAP_CONTENT)

    def test_recap_without_preamble_embeds_unmodified(self) -> None:
        plain = "remote compaction kept provider-native history\nno sections\n"
        message = _chunk_message("pi:s:7", plain, who="recap")
        self.assertEqual(embed._embed_input(message), plain)
        sectioned = _chunk_message("pi:s:8", _RECAP_CONTENT, who="recap")
        self.assertEqual(embed._embed_input(sectioned), _RECAP_CONTENT)

    def test_provider_recap_with_setext_heading_keeps_its_summary(self) -> None:
        text = "The deployment investigation found a stale cache.\n\nResults\n=======\nfixed\n"
        for agent in ("claude", "codex"):
            with self.subTest(agent=agent):
                message = _chunk_message(f"{agent}:s:8", text, who="recap")
                self.assertEqual(embed._embed_input(message), text)

    def test_recap_agent_controls_chunk_count_after_a_long_preamble(self) -> None:
        text = "Resume archive instructions.\n" * 100 + "\n" + _RECAP_CONTENT
        for agent, expected in (("pi", _RECAP_CONTENT), ("claude", text)):
            with self.subTest(agent=agent):
                message = _chunk_message(f"{agent}:long-recap:1", text, who="recap")
                rows = list(embed._expand_source_message(message))
                chunks = embed._row_chunks(agent, "recap", text)
                self.assertEqual(len(rows), embed._row_chunk_count(agent, "recap", text))
                self.assertEqual([embed._embed_input(row) for row in rows], chunks)
                reconstructed = chunks[0] + "".join(
                    chunk[embed._CHUNK_OVERLAP_CHARS:] for chunk in chunks[1:])
                self.assertEqual(reconstructed, expected)
                if agent == "pi":
                    self.assertEqual(len(rows), 1)
                else:
                    self.assertGreater(len(rows), 1)

    def test_non_recap_giant_row_chunks_without_any_stripping(self) -> None:
        text = _RECAP_PREAMBLE + _RECAP_CONTENT + _long_text(
            3 * embed._CHUNK_BYTES)
        message = _chunk_message("codex:s:9", text, who="user")
        chunks = embed._row_chunks("codex", "user", text)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(embed._embed_input(message), chunks[0])
        self.assertTrue(chunks[0].startswith(_RECAP_PREAMBLE[:64]))


class EmbedSmokeTests(unittest.TestCase):
    @staticmethod
    def _message(index: int) -> common.Message:
        return common.Message(
            id=f"m{index}", agent="codex", project="p", session="s",
            ts=index, turn=index, text=f"message {index}", who="user",
            model="", model_source="unknown")

    def test_smoke_embeds_subset_without_touching_production_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifacts = [root / "embeddings.meta", root / "segment.f16"]
            for index, path in enumerate(artifacts):
                path.write_bytes(f"production-{index}".encode())
            before = [path.read_bytes() for path in artifacts]
            args = mock.Mock(smoke=2, no_model_download=True)
            messages = [self._message(index) for index in range(5)]
            with (
                mock.patch.object(embed, "_iter_source_messages",
                                  return_value=iter(messages)),
                mock.patch.object(
                    embed.semantic, "source_generation",
                    side_effect=[{"generation": "g"}, {"generation": "g"}]),
                mock.patch.object(embed.embedder, "get", return_value=_Model()),
                mock.patch.dict(embed.embedder.PROFILE, {"dim": 2}),
                mock.patch.object(common, "log") as log,
            ):
                self.assertEqual(embed._run_smoke(args), 0)
            self.assertEqual([path.read_bytes() for path in artifacts], before)
            self.assertIn("embedded the first 2 source rows", log.call_args.args[0])
            self.assertIn("source total was not counted", log.call_args.args[0])
            self.assertIn("coverage was not changed", log.call_args.args[0])

    def test_smoke_does_not_scan_past_the_bounded_sample(self) -> None:
        args = mock.Mock(smoke=3, no_model_download=True)
        messages = _BoundedMessages(
            (self._message(index) for index in range(1_000_000)), args.smoke)
        with (
            mock.patch.object(embed, "_iter_source_messages", return_value=messages),
            mock.patch.object(
                embed.semantic, "source_generation",
                side_effect=[{"generation": "g"}, {"generation": "g"}]),
            mock.patch.object(embed.embedder, "get", return_value=_Model()),
            mock.patch.dict(embed.embedder.PROFILE, {"dim": 2}),
            mock.patch.object(common, "log") as log,
        ):
            self.assertEqual(embed._run_smoke(args), 0)
        self.assertEqual(messages.consumed, args.smoke)
        self.assertTrue(messages.closed)
        output = log.call_args.args[0]
        self.assertIn("bounded sample", output)
        self.assertNotIn("/1000000", output)

    def test_cli_smoke_uses_nonpublishing_path(self) -> None:
        with (
            mock.patch.object(sys, "argv", ["embed.py", "--smoke", "2"]),
            mock.patch.object(embed, "_mutation_refusal_reason") as refusal,
            mock.patch.object(embed, "_acquire_claim") as claim,
            mock.patch.object(embed, "_release_claim") as release,
            mock.patch.object(embed, "_run_smoke", return_value=0) as smoke,
            mock.patch.object(embed, "_run") as production,
        ):
            self.assertEqual(embed.main(), 0)
        smoke.assert_called_once()
        production.assert_not_called()
        refusal.assert_not_called()
        claim.assert_not_called()
        release.assert_not_called()

    def test_cli_smoke_reports_expected_offline_model_unavailability(self) -> None:
        error = embed.embedder.EmbedderUnavailable(
            "model missing\n\x1b[31mforged diagnostic")
        stderr = io.StringIO()
        with (
            mock.patch.object(
                sys, "argv", ["embed.py", "--smoke", "2", "--no-model-download"]),
            mock.patch.object(embed, "_mutation_refusal_reason") as refusal,
            mock.patch.object(embed, "_acquire_claim") as claim,
            mock.patch.object(embed, "_release_claim") as release,
            mock.patch.object(embed, "_run_smoke", side_effect=error),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(embed.main(), 1)
        output = stderr.getvalue()
        self.assertEqual(output.count("\n"), 1)
        self.assertIn("semantic smoke unavailable: model missing\\u000a", output)
        self.assertIn("\\u001b[31mforged diagnostic", output)
        self.assertIn("rerun without --no-model-download when online", output)
        refusal.assert_not_called()
        claim.assert_not_called()
        release.assert_not_called()

    def test_cli_smoke_does_not_hide_unexpected_failures(self) -> None:
        with (
            mock.patch.object(sys, "argv", ["embed.py", "--smoke", "2"]),
            mock.patch.object(embed, "_run_smoke", side_effect=RuntimeError("bug")),
            self.assertRaisesRegex(RuntimeError, "bug"),
        ):
            embed.main()

    def test_unmanaged_background_policy_label_still_defers(self) -> None:
        with (
            mock.patch.object(sys, "argv", ["embed.py", "--background"]),
            mock.patch.dict(
                os.environ, {"AGREP_SEM_BG_POLICY": "polite"}, clear=True),
            mock.patch.object(embed, "_mutation_refusal_reason", return_value=None),
            mock.patch.object(
                embed, "_governor_deferral", return_value="on battery (10%)") as gate,
            mock.patch.object(embed, "_acquire_claim") as claim,
            mock.patch.object(common, "log") as log,
        ):
            self.assertEqual(embed.main(), 0)
        gate.assert_called_once_with(ignore_battery=False, ignore_load=False)
        claim.assert_not_called()
        log.assert_called_once_with("embedding deferred: on battery (10%)")

    def test_indexd_bootstrap_override_still_honors_other_pressure(self) -> None:
        with (
            mock.patch.object(sys, "argv", ["embed.py", "--background"]),
            mock.patch.dict(
                os.environ, {"AGREP_EMBED_IGNORE_BATTERY": "1"}, clear=True),
            mock.patch.object(embed, "_mutation_refusal_reason", return_value=None),
            mock.patch.object(
                embed, "_governor_deferral",
                return_value="memory pressure (10% available)") as gate,
            mock.patch.object(embed, "_acquire_claim") as claim,
            mock.patch.object(common, "log") as log,
        ):
            self.assertEqual(embed.main(), 0)
        gate.assert_called_once_with(ignore_battery=True, ignore_load=False)
        claim.assert_not_called()
        log.assert_called_once_with(
            "embedding deferred: memory pressure (10% available)")

    def test_doctor_fix_build_runs_under_cpu_load_but_yields_to_memory_pressure(self) -> None:
        import semantic

        class Proceeded(Exception):
            pass

        def child_env(coherence: dict | None = None, **overrides) -> dict:
            # The spawn decision reads the process environment: a shared test
            # interpreter may carry another suite's AGREP_NO_DAEMON, which
            # answers "disabled" before any probe runs. Pin what this test means.
            environment = {key: value for key, value in os.environ.items()
                           if key not in ("AGREP_NO_DAEMON", "AGREP_NO_SEM_WORKER")}
            with (mock.patch.dict(os.environ, environment, clear=True),
                  mock.patch.object(semantic, "runtime_dependencies_available",
                                    return_value=True),
                  mock.patch.object(semantic, "embedding_coherence",
                                    return_value=coherence or {"state": "stale"}),
                  mock.patch.object(semantic, "embed_running", return_value=False),
                  mock.patch.object(semantic, "read_embed_state", return_value={}),
                  mock.patch("embedder.ensure_model"),
                  mock.patch.object(semantic, "_needs_unverified_bundle_rebuild",
                                    return_value=False),
                  mock.patch.object(semantic.subprocess, "Popen") as popen):
                self.assertEqual(semantic.ensure_fresh_async(
                    max_new=100, **overrides)["state"], "running")
            return popen.call_args.kwargs["env"]

        def run_child(env: dict, *, memory: float) -> bool:
            with (mock.patch.object(sys, "argv", ["embed.py", "--background"]),
                  mock.patch.dict(os.environ, env, clear=True),
                  mock.patch.object(embed, "_mutation_refusal_reason", return_value=None),
                  mock.patch.object(embed, "_normalized_load", return_value=30.0),
                  mock.patch.object(embed, "_battery_state", return_value=(False, None)),
                  mock.patch.object(common, "available_memory_fraction",
                                    return_value=memory),
                  mock.patch.object(embed, "_acquire_claim", side_effect=Proceeded),
                  mock.patch.object(embed.os, "nice", return_value=0, create=True),
                  mock.patch.object(common, "log")):
                try:
                    self.assertEqual(embed.main(), 0)
                except Proceeded:
                    return True
            return False

        explicit = child_env(ignore_battery=True, ignore_load=True)
        self.assertTrue(run_child(explicit, memory=0.9))
        self.assertFalse(run_child(explicit, memory=0.01))
        # a searchable lane's catch-up is the pass --fix's consent exists for
        searchable = {"coherent": False, "searchable": True, "state": "partial"}
        self.assertFalse(run_child(child_env(searchable), memory=0.9))
        self.assertTrue(run_child(
            child_env(searchable, ignore_battery=True, ignore_load=True),
            memory=0.9))

    def test_cold_cli_smoke_is_one_line_and_preserves_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            data.mkdir()
            row = self._message(1)._asdict()
            (data / "messages.jsonl").write_text(
                json.dumps(row) + "\n", encoding="utf-8")
            artifacts = {
                "embeddings.meta": b"published metadata",
                "embeddings.f32": b"published vectors",
                "embeddings.ids": b"published ids",
            }
            for name, payload in artifacts.items():
                (data / name).write_bytes(payload)
            env = dict(os.environ)
            env.update({
                "AGREP_DATA_DIR": str(data),
                "AGREP_DATA_DIR_SOURCE": "test",
                "AGREP_HOME": str(root / "home"),
                "AGREP_MODEL_DIR": str(root / "missing-model-cache"),
                "AGREP_NO_DAEMON": "1",
            })
            result = subprocess.run(
                [sys.executable, str(Path(embed.__file__)),
                 "--smoke", "1", "--no-model-download"],
                capture_output=True, text=True, env=env, timeout=10,
                check=False)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr.count("\n"), 1)
            self.assertIn("semantic smoke unavailable:", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertIn("rerun without --no-model-download when online", result.stderr)
            self.assertEqual(
                {name: (data / name).read_bytes() for name in artifacts}, artifacts)

    def test_a_lost_publication_race_is_contended_not_a_failed_build(
            self) -> None:
        import embedding_segments
        race = embedding_segments.SegmentPublicationRace(
            "segmented embedding prefix moved during publication")
        published = []
        with (
            mock.patch.object(sys, "argv", ["embed.py"]),
            mock.patch.object(embed, "_mutation_refusal_reason", return_value=None),
            mock.patch.object(embed, "_acquire_claim", return_value=True),
            mock.patch.object(embed, "_release_claim"),
            mock.patch.object(
                embed.semantic, "read_embed_state", return_value={}),
            mock.patch.object(embed, "_publish_state", published.append),
            mock.patch.object(embed, "_run", side_effect=race),
            mock.patch.object(common, "log") as log,
        ):
            self.assertEqual(embed.main(), 0)
        self.assertEqual([state["state"] for state in published], ["contended"])
        self.assertNotIn("failures", published[0])
        self.assertNotIn("error", published[0])
        self.assertIn("lost a publication race", log.call_args.args[0])

    def test_a_moving_transcript_publication_is_contended_not_failed(
            self) -> None:
        race = common.TranscriptPublicationRace(
            "session-family publication precedes its ingest signature")
        published = []
        with (
            mock.patch.object(sys, "argv", ["embed.py"]),
            mock.patch.object(embed, "_mutation_refusal_reason", return_value=None),
            mock.patch.object(embed, "_acquire_claim", return_value=True),
            mock.patch.object(embed, "_release_claim"),
            mock.patch.object(
                embed.semantic, "read_embed_state", return_value={}),
            mock.patch.object(embed, "_publish_state", published.append),
            mock.patch.object(embed, "_run", side_effect=race),
            mock.patch.object(common, "log") as log,
        ):
            self.assertEqual(embed.main(), 0)
        self.assertEqual([state["state"] for state in published], ["contended"])
        self.assertNotIn("failures", published[0])
        self.assertNotIn("error", published[0])
        self.assertIn("lost a publication race", log.call_args.args[0])


class UnsearchableLaneGovernorTests(unittest.TestCase):
    """A lane nobody can query gets its bounded pass through battery and CPU
    load; a searchable lane's catch-up and every unbounded pass keep the full
    governor, and memory pressure defers everything."""

    class _Proceeded(Exception):
        pass

    @staticmethod
    def _lane(state: str, *, searchable: bool) -> dict:
        return {"coherent": False, "searchable": searchable, "state": state}

    @staticmethod
    def _spawn_mocks(coherence: dict) -> list:
        import semantic

        return [
            # the isolation default would read as 'disabled'; the spawn is the point
            mock.patch.dict(os.environ, {"AGREP_NO_DAEMON": ""}),
            mock.patch.object(semantic, "runtime_dependencies_available",
                              return_value=True),
            mock.patch.object(semantic, "embedding_coherence",
                              return_value=coherence),
            mock.patch.object(semantic, "embed_running", return_value=False),
            mock.patch.object(semantic, "read_embed_state", return_value={}),
            mock.patch("embedder.ensure_model"),
            mock.patch.object(semantic, "_needs_unverified_bundle_rebuild",
                              return_value=False),
        ]

    def _spawned(self, coherence: dict, **kwargs) -> tuple[list, dict]:
        """(argv, child env) of the embed.py ensure_fresh_async launches."""
        import semantic

        with contextlib.ExitStack() as stack:
            for patcher in self._spawn_mocks(coherence):
                stack.enter_context(patcher)
            popen = stack.enter_context(
                mock.patch.object(semantic.subprocess, "Popen"))
            self.assertEqual(
                semantic.ensure_fresh_async(**kwargs)["state"], "running")
        return popen.call_args.args[0], popen.call_args.kwargs["env"]

    def _child_outcome(self, env: dict, *, memory: float, load: float,
                       battery: tuple) -> tuple[bool, list]:
        """(proceeded past the governor, deferral log lines) for one child."""
        logs: list[str] = []
        with (mock.patch.object(sys, "argv", ["embed.py", "--background"]),
              mock.patch.dict(os.environ, env, clear=True),
              mock.patch.object(embed, "_mutation_refusal_reason", return_value=None),
              mock.patch.object(embed, "_normalized_load", return_value=load),
              mock.patch.object(embed, "_battery_state", return_value=battery),
              mock.patch.object(common, "available_memory_fraction",
                                return_value=memory),
              mock.patch.object(embed, "_acquire_claim",
                                side_effect=self._Proceeded),
              mock.patch.object(embed.os, "nice", return_value=0, create=True),
              mock.patch.object(common, "log", logs.append)):
            try:
                self.assertEqual(embed.main(), 0)
            except self._Proceeded:
                return True, logs
        return False, logs

    def test_unsearchable_lane_bootstrap_runs_through_battery_and_load(self) -> None:
        import semantic

        for state in ("stale", "missing-embeddings", "unstable-source"):
            with self.subTest(state=state):
                argv, env = self._spawned(
                    self._lane(state, searchable=False),
                    max_new=semantic.SEMANTIC_BOOTSTRAP_MAX_NEW)
                self.assertEqual(
                    argv[argv.index("--max-new") + 1],
                    str(semantic.SEMANTIC_BOOTSTRAP_MAX_NEW))
                proceeded, logs = self._child_outcome(
                    env, memory=0.9, load=30.0, battery=(True, 10))
                self.assertTrue(proceeded, logs)
                self.assertEqual(logs, [])

    def test_unsearchable_lane_bootstrap_still_yields_to_memory_pressure(
            self) -> None:
        import semantic

        _argv, env = self._spawned(
            self._lane("stale", searchable=False),
            max_new=semantic.SEMANTIC_BOOTSTRAP_MAX_NEW)
        proceeded, logs = self._child_outcome(
            env, memory=0.01, load=0.1, battery=(False, None))
        self.assertFalse(proceeded)
        self.assertEqual(logs, ["embedding deferred: memory pressure (1% available)"])

    def test_searchable_lane_catch_up_keeps_the_full_governor(self) -> None:
        partial = {**self._lane("partial", searchable=True),
                   "coverage": {"indexed": 128, "total": 400, "pending": 272}}
        argv, env = self._spawned(partial, max_new=128)
        self.assertEqual(argv[argv.index("--max-new") + 1], "128")
        self.assertNotIn("AGREP_EMBED_IGNORE_BATTERY", env)
        self.assertNotIn("AGREP_EMBED_IGNORE_LOAD", env)
        loaded, logs = self._child_outcome(
            env, memory=0.9, load=30.0, battery=(False, None))
        self.assertFalse(loaded)
        self.assertEqual(logs, ["embedding deferred: load 30.0/core"])
        drained, logs = self._child_outcome(
            env, memory=0.9, load=0.1, battery=(True, 10))
        self.assertFalse(drained)
        self.assertEqual(logs, ["embedding deferred: on battery (10%)"])

    def test_unbounded_refresh_keeps_the_governor_even_when_unsearchable(
            self) -> None:
        argv, env = self._spawned(self._lane("stale", searchable=False))
        self.assertNotIn("--max-new", argv)
        self.assertNotIn("AGREP_EMBED_IGNORE_BATTERY", env)
        self.assertNotIn("AGREP_EMBED_IGNORE_LOAD", env)
        proceeded, logs = self._child_outcome(
            env, memory=0.9, load=30.0, battery=(False, None))
        self.assertFalse(proceeded)
        self.assertEqual(logs, ["embedding deferred: load 30.0/core"])

    def test_explicit_semantic_query_bootstraps_an_unsearchable_lane_through_load(
            self) -> None:
        import semantic

        stale = self._lane("stale", searchable=False)
        with contextlib.ExitStack() as stack:
            for patcher in self._spawn_mocks(stale):
                stack.enter_context(patcher)
            stack.enter_context(mock.patch.object(semantic, "note_semantic_use"))
            stack.enter_context(mock.patch.object(
                semantic, "_await_publishing_coherence", lambda initial: initial))
            popen = stack.enter_context(
                mock.patch.object(semantic.subprocess, "Popen"))
            with self.assertRaises(semantic.SemanticUnavailable):
                semantic.search("deployment retry loop", level="message")
        popen.assert_called_once()
        argv = popen.call_args.args[0]
        env = popen.call_args.kwargs["env"]
        self.assertIn("--background", argv)
        self.assertEqual(
            argv[argv.index("--max-new") + 1],
            str(semantic.SEMANTIC_BOOTSTRAP_MAX_NEW))
        proceeded, logs = self._child_outcome(
            env, memory=0.9, load=30.0, battery=(True, 10))
        self.assertTrue(proceeded, logs)

    def _scheduled_compaction_env(
            self, env: dict, *, delta_count: int, **kwargs) -> dict | None:
        """Env of the compaction child one pass schedules under ``env``."""
        import embedding_segments
        import removal_fence

        manifest = {"delta_count": delta_count}
        with (mock.patch.dict(os.environ, env, clear=True),
              mock.patch.object(embed, "_data_dir_readonly", return_value=False),
              mock.patch.object(removal_fence, "background_removal_active",
                                return_value=False),
              mock.patch.object(embedding_segments, "load_manifest",
                                return_value=manifest),
              mock.patch.object(embedding_segments, "prune_orphans"),
              mock.patch.object(embedding_segments, "should_compact",
                                side_effect=lambda m: m["delta_count"] >= 8),
              mock.patch.object(common, "open_bounded_log",
                                return_value=mock.Mock()),
              mock.patch.object(embed.subprocess, "Popen") as popen):
            launched = embed._schedule_segment_compaction(**kwargs)
        if not launched:
            return None
        return popen.call_args.kwargs["env"]

    def test_compaction_that_unblocks_the_lane_inherits_the_overrides(
            self) -> None:
        # At the 16-delta cap the pass cannot publish until compaction runs;
        # the bounded pass got through load and battery, so must the child.
        overrides = {"AGREP_EMBED_IGNORE_BATTERY": "1",
                     "AGREP_EMBED_IGNORE_LOAD": "1"}
        env = self._scheduled_compaction_env(
            overrides, delta_count=16, unblocks_lane=True)
        self.assertEqual(env["AGREP_EMBED_IGNORE_BATTERY"], "1")
        self.assertEqual(env["AGREP_EMBED_IGNORE_LOAD"], "1")
        upgrade = self._scheduled_compaction_env(
            overrides, delta_count=0, refresh_metadata=True, unblocks_lane=True)
        self.assertEqual(upgrade["AGREP_EMBED_IGNORE_LOAD"], "1")

    def test_routine_post_publication_compaction_drops_the_overrides(
            self) -> None:
        overrides = {"AGREP_EMBED_IGNORE_BATTERY": "1",
                     "AGREP_EMBED_IGNORE_LOAD": "1"}
        env = self._scheduled_compaction_env(overrides, delta_count=9)
        self.assertNotIn("AGREP_EMBED_IGNORE_BATTERY", env)
        self.assertNotIn("AGREP_EMBED_IGNORE_LOAD", env)

    def test_the_delta_cap_schedules_an_unblocking_compaction(self) -> None:
        segments = {
            "delta_count": 16, "live_rows": 100,
            "coverage": {"total": 120, "pending": 20},
        }
        manifest = {"segmented": True, "segments": segments,
                    "refs_versions": frozenset({5})}
        with (mock.patch.object(embed, "_schedule_segment_compaction",
                                return_value=True) as schedule,
              mock.patch.object(embed, "_publish_state"),
              mock.patch.object(common, "log")):
            self.assertTrue(embed._defer_segment_maintenance(manifest, True))
            schedule.assert_called_once_with(unblocks_lane=True)
            schedule.reset_mock()
            upgrade = {**manifest, "refs_versions": frozenset({4})}
            self.assertTrue(embed._defer_segment_maintenance(upgrade, True))
            schedule.assert_called_once_with(
                refresh_metadata=True, unblocks_lane=True)
            schedule.reset_mock()
            below = {**manifest, "segments": {**segments, "delta_count": 15}}
            self.assertFalse(embed._defer_segment_maintenance(below, True))
            schedule.assert_not_called()


if __name__ == "__main__":
    unittest.main()
