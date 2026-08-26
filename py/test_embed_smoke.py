from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        id=mid, agent="codex", project="p", session="s", ts=1, turn=1,
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


class LongRowChunkingTests(unittest.TestCase):
    def test_short_row_embeds_exactly_one_unsuffixed_vector(self) -> None:
        message = _chunk_message("codex:s:1", "a short prompt")
        rows = list(embed._expand_source_message(message))
        self.assertEqual([row.id for row in rows], ["codex:s:1"])
        self.assertEqual(embed._embed_input(message), "a short prompt")

    def test_long_row_expands_to_chunk_ids_carrying_the_full_text(self) -> None:
        text = _long_text(4 * embed._CHUNK_CHARS)
        message = _chunk_message("codex:s:2", text)
        rows = list(embed._expand_source_message(message))
        chunks = embed._row_chunks("user", text)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(
            [row.id for row in rows],
            ["codex:s:2"] + [f"codex:s:2#c{n}" for n in range(1, len(chunks))])
        # every store id keeps the logical row's text (and therefore hash)
        self.assertTrue(all(row.text == text for row in rows))
        self.assertEqual([embed._embed_input(row) for row in rows], chunks)
        # deterministic: same text always yields the same expansion
        self.assertEqual(chunks, embed._row_chunks("user", text))

    def test_long_reply_chunks_extend_the_reply_suffix(self) -> None:
        message = _chunk_message(
            "codex:s:3#r", _long_text(2 * embed._CHUNK_CHARS), who="agent")
        rows = list(embed._expand_source_message(message))
        self.assertGreater(len(rows), 1)
        self.assertEqual(rows[1].id, "codex:s:3#r#c1")
        self.assertEqual(
            common.semantic_chunk_split("codex:s:3#r#c1"), ("codex:s:3#r", 1))

    def test_chunks_split_at_line_boundaries_and_reconstruct_exactly(self) -> None:
        text = _long_text(6 * embed._CHUNK_CHARS)
        chunks = embed._row_chunks("user", text)
        for chunk in chunks[:-1]:
            self.assertTrue(chunk.endswith("\n"))
        rebuilt = chunks[0] + "".join(
            chunk[embed._CHUNK_OVERLAP_CHARS:] for chunk in chunks[1:])
        self.assertEqual(rebuilt, text)

    def test_multibyte_text_never_splits_inside_a_codepoint(self) -> None:
        text = ("多言語のテキスト mixed with emoji 🎉 and ascii\n"
                * (3 * embed._CHUNK_CHARS // 40))
        chunks = embed._row_chunks("user", text)
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

    def test_non_recap_giant_row_chunks_without_any_stripping(self) -> None:
        text = _RECAP_PREAMBLE + _RECAP_CONTENT + _long_text(
            3 * embed._CHUNK_CHARS)
        message = _chunk_message("codex:s:9", text, who="user")
        chunks = embed._row_chunks("user", text)
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
        gate.assert_called_once_with(ignore_battery=False)
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
        gate.assert_called_once_with(ignore_battery=True)
        claim.assert_not_called()
        log.assert_called_once_with(
            "embedding deferred: memory pressure (10% available)")

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


if __name__ == "__main__":
    unittest.main()
