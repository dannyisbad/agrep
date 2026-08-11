from __future__ import annotations

import contextlib
import hashlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import reindex  # noqa: E402


class ReindexSignatureTests(unittest.TestCase):
    def test_public_help_names_the_agrep_command_and_hides_dev_build_plumbing(
            self) -> None:
        stdout = io.StringIO()
        with mock.patch.object(sys, "argv", ["agrep", "reindex", "--help"]), \
                contextlib.redirect_stdout(stdout), \
                self.assertRaises(SystemExit) as raised:
            reindex.main()
        self.assertEqual(raised.exception.code, 0)
        rendered = stdout.getvalue()
        self.assertIn("usage: agrep reindex", rendered)
        self.assertIn("examples:", rendered)
        self.assertIn("exit: 0 refreshed", rendered)
        self.assertNotIn("reindex.py", rendered)
        self.assertNotIn("--no-build", rendered)

    def test_signature_preserves_format_and_streams_inputs(self) -> None:
        messages_payload = b"message\n" * 200_000
        replies_payload = b"reply\n" * 150_000
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            messages = root / "messages.jsonl"
            replies = root / "replies.jsonl"
            messages.write_bytes(messages_payload)
            replies.write_bytes(replies_payload)
            expected = (
                f"{len(messages_payload)}:{hashlib.md5(messages_payload).hexdigest()}:"
                f"{len(replies_payload)}:{hashlib.md5(replies_payload).hexdigest()}"
            )
            with mock.patch.object(Path, "read_bytes", side_effect=AssertionError):
                self.assertEqual(reindex._input_signature(messages, replies), expected)

    def test_signature_handles_missing_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            messages = root / "messages.jsonl"
            replies = root / "replies.jsonl"
            self.assertEqual(reindex._input_signature(messages, replies), "")
            payload = b"message\n"
            messages.write_bytes(payload)
            expected = f"{len(payload)}:{hashlib.md5(payload).hexdigest()}"
            self.assertEqual(reindex._input_signature(messages, replies), expected)

    def _run_semantic_skip(
            self, *, setting: str, payload: str,
    ) -> tuple[int, str, mock.Mock]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "agrep-rs"
            binary.touch()
            messages = root / "messages.jsonl"
            messages.write_text(payload, encoding="utf-8")
            semantic = mock.Mock()
            output = io.StringIO()
            with mock.patch.object(sys, "argv", ["agrep", "--no-build"]), \
                    mock.patch.object(
                        reindex.common, "ingest_bin", return_value=binary), \
                    mock.patch.object(reindex.common, "MESSAGES_PATH", messages), \
                    mock.patch.object(reindex.common, "DATA_DIR", root), \
                    mock.patch.object(
                        reindex.common, "setting", return_value=setting), \
                    mock.patch.object(
                        reindex.indexd_runtime, "rust_writer_env",
                        return_value={"AGREP_RUNTIME_BUILD_ID": "a" * 20}), \
                    mock.patch.object(
                        reindex.indexd_runtime, "derived_writer_mutation_info",
                        return_value=mock.Mock(writable=True)), \
                    mock.patch.object(reindex, "run", return_value=True) as run, \
                    mock.patch.object(
                        reindex.indexd_runtime, "refresh_search_index",
                        return_value=True), \
                    mock.patch.object(
                        reindex, "_publish_completion_signature",
                        return_value=False), \
                    mock.patch.dict(sys.modules, {
                        "semantic": semantic,
                        "indexer": mock.Mock(run_post_index_hooks=lambda: None),
                    }), contextlib.redirect_stdout(output):
                rc = reindex.main()
            return rc, output.getvalue(), run

    def test_embeddings_off_skips_semantic_without_recording_a_failure(self) -> None:
        rc, rendered, run = self._run_semantic_skip(
            setting="off", payload='{"text":"one"}\n')
        self.assertEqual(rc, 0)
        self.assertEqual(run.call_count, 1)
        self.assertIn("semantic embeddings (disabled)", rendered)
        self.assertNotIn("semantic generation was not proven", rendered)

    def test_embeddings_off_bypasses_an_old_completion_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "agrep-rs"
            binary.touch()
            messages = root / "messages.jsonl"
            messages.write_text('{"text":"one"}\n', encoding="utf-8")
            signature = root / ".reindex.sig"
            signature.write_text(
                reindex._input_signature(messages, root / "replies.jsonl") + "\n",
                encoding="utf-8",
            )
            semantic = mock.Mock()
            output = io.StringIO()
            with mock.patch.object(sys, "argv", ["agrep", "--no-build"]), \
                    mock.patch.object(
                        reindex.common, "ingest_bin", return_value=binary), \
                    mock.patch.object(reindex.common, "MESSAGES_PATH", messages), \
                    mock.patch.object(reindex.common, "DATA_DIR", root), \
                    mock.patch.object(
                        reindex.common, "setting", return_value="off"), \
                    mock.patch.object(
                        reindex.indexd_runtime, "rust_writer_env",
                        return_value={"AGREP_RUNTIME_BUILD_ID": "a" * 20}), \
                    mock.patch.object(
                        reindex.indexd_runtime, "derived_writer_mutation_info",
                        return_value=mock.Mock(writable=True)), \
                    mock.patch.object(reindex, "run", return_value=True), \
                    mock.patch.object(
                        reindex.indexd_runtime, "refresh_search_index",
                        return_value=True), \
                    mock.patch.object(
                        reindex, "_embedding_proof",
                        side_effect=AssertionError(
                            "disabled semantic lane was inspected")), \
                    mock.patch.dict(sys.modules, {
                        "semantic": semantic,
                        "indexer": mock.Mock(run_post_index_hooks=lambda: None),
                    }), contextlib.redirect_stdout(output):
                rc = reindex.main()
            marker_exists = signature.exists()
        self.assertEqual(rc, 0)
        self.assertFalse(marker_exists)
        self.assertIn("semantic embeddings (disabled)", output.getvalue())
        semantic.runtime_dependencies_available.assert_not_called()

    def test_empty_corpus_is_not_an_embedding_failure(self) -> None:
        rc, rendered, run = self._run_semantic_skip(
            setting="auto", payload="")
        self.assertEqual(rc, 0)
        self.assertEqual(run.call_count, 1)
        self.assertIn("semantic embeddings (no messages)", rendered)
        self.assertNotIn("semantic generation was not proven", rendered)

    def test_reindex_stops_when_keyword_refresh_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "agrep-rs"
            binary.touch()
            with mock.patch.object(sys, "argv", ["reindex.py", "--no-build"]), \
                    mock.patch.object(reindex.common, "ingest_bin", return_value=binary), \
                    mock.patch.object(
                        reindex.indexd_runtime, "rust_writer_env",
                        return_value={"AGREP_RUNTIME_BUILD_ID": "a" * 20}), \
                    mock.patch.object(
                        reindex.indexd_runtime, "derived_writer_mutation_info",
                        return_value=mock.Mock(writable=True)), \
                    mock.patch.object(reindex, "run", return_value=True) as run, \
                    mock.patch.object(
                        reindex.indexd_runtime, "refresh_search_index", return_value=False
                    ) as refresh:
                self.assertEqual(reindex.main(), 1)
            run.assert_called_once()
            refresh.assert_called_once_with(quiet=False)

    def test_reindex_refreshes_keyword_index_after_ingest(self) -> None:
        order: list[str] = []

        def run_stage(desc, _cmd, optional=False, *, env=None):
            order.append(desc)
            if desc == "ingest transcripts (rust)":
                self.assertEqual(
                    env, {"AGREP_RUNTIME_BUILD_ID": "a" * 20})
            return True

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "agrep-rs"
            binary.touch()
            messages = root / "messages.jsonl"
            messages.write_text("{}\n", encoding="utf-8")
            signature = root / ".reindex.sig"
            with mock.patch.object(sys, "argv", ["reindex.py", "--no-build"]), \
                    mock.patch.object(reindex.common, "ingest_bin", return_value=binary), \
                    mock.patch.object(reindex.common, "MESSAGES_PATH", messages), \
                    mock.patch.object(reindex.common, "DATA_DIR", root), \
                    mock.patch.object(
                        reindex.indexd_runtime, "rust_writer_env",
                        return_value={"AGREP_RUNTIME_BUILD_ID": "a" * 20}), \
                    mock.patch.object(
                        reindex.indexd_runtime, "derived_writer_mutation_info",
                        return_value=mock.Mock(writable=True)), \
                    mock.patch.object(reindex, "run", side_effect=run_stage), \
                    mock.patch.object(
                        reindex.indexd_runtime, "refresh_search_index",
                        side_effect=lambda quiet: order.append("keyword") or True,
                    ), \
                    mock.patch.object(
                        reindex, "_embedding_proof", return_value={"coherent": True}
                    ), \
                    mock.patch.object(reindex, "_write_sig") as write_sig, \
                    mock.patch.dict(sys.modules, {"semantic": mock.Mock(
                        runtime_dependencies_available=lambda: True
                    ), "indexer": mock.Mock(run_post_index_hooks=lambda: None)}):
                self.assertEqual(reindex.main(), 0)
            self.assertEqual(order, ["ingest transcripts (rust)", "keyword", "embed messages"])
            write_sig.assert_called_once_with(signature, mock.ANY)

    def test_full_forwards_the_fresh_lane_flag_into_embed(self) -> None:
        # The seam every "one sanctioned lane move" doc surface rests on:
        # --full only re-decides the lane if it reaches embed.py's argv, where
        # it raises _FRESH_LANE (test_mlx_embed pins what happens after that).
        commands: dict[str, list[str]] = {}

        def run_stage(desc, cmd, optional=False, *, env=None):
            commands[desc] = list(cmd)
            return True

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "agrep-rs"
            binary.touch()
            messages = root / "messages.jsonl"
            messages.write_text("{}\n", encoding="utf-8")
            with mock.patch.object(
                    sys, "argv", ["reindex.py", "--no-build", "--full"]), \
                    mock.patch.object(reindex.common, "ingest_bin", return_value=binary), \
                    mock.patch.object(reindex.common, "MESSAGES_PATH", messages), \
                    mock.patch.object(reindex.common, "DATA_DIR", root), \
                    mock.patch.object(
                        reindex.indexd_runtime, "rust_writer_env",
                        return_value={"AGREP_RUNTIME_BUILD_ID": "a" * 20}), \
                    mock.patch.object(
                        reindex.indexd_runtime, "derived_writer_mutation_info",
                        return_value=mock.Mock(writable=True)), \
                    mock.patch.object(reindex, "run", side_effect=run_stage), \
                    mock.patch.object(
                        reindex.indexd_runtime, "refresh_search_index",
                        return_value=True), \
                    mock.patch.object(
                        reindex, "_embedding_proof", return_value={"coherent": True}
                    ), \
                    mock.patch.object(reindex, "_write_sig"), \
                    mock.patch.dict(sys.modules, {"semantic": mock.Mock(
                        runtime_dependencies_available=lambda: True
                    ), "indexer": mock.Mock(run_post_index_hooks=lambda: None)}):
                self.assertEqual(reindex.main(), 0)
        self.assertIn("--full", commands["ingest transcripts (rust)"])
        self.assertEqual(
            commands["embed messages"][1:], ["py/embed.py", "--full"])

    def test_without_full_the_embed_stage_stays_incremental(self) -> None:
        # The other half of the contract: an ordinary reindex must never
        # discard a store's rows, so --full may not leak into embed's argv.
        commands: dict[str, list[str]] = {}

        def run_stage(desc, cmd, optional=False, *, env=None):
            commands[desc] = list(cmd)
            return True

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "agrep-rs"
            binary.touch()
            messages = root / "messages.jsonl"
            messages.write_text("{}\n", encoding="utf-8")
            with mock.patch.object(sys, "argv", ["reindex.py", "--no-build"]), \
                    mock.patch.object(reindex.common, "ingest_bin", return_value=binary), \
                    mock.patch.object(reindex.common, "MESSAGES_PATH", messages), \
                    mock.patch.object(reindex.common, "DATA_DIR", root), \
                    mock.patch.object(
                        reindex.indexd_runtime, "rust_writer_env",
                        return_value={"AGREP_RUNTIME_BUILD_ID": "a" * 20}), \
                    mock.patch.object(
                        reindex.indexd_runtime, "derived_writer_mutation_info",
                        return_value=mock.Mock(writable=True)), \
                    mock.patch.object(reindex, "run", side_effect=run_stage), \
                    mock.patch.object(
                        reindex.indexd_runtime, "refresh_search_index",
                        return_value=True), \
                    mock.patch.object(
                        reindex, "_embedding_proof", return_value={"coherent": True}
                    ), \
                    mock.patch.object(reindex, "_write_sig"), \
                    mock.patch.dict(sys.modules, {"semantic": mock.Mock(
                        runtime_dependencies_available=lambda: True
                    ), "indexer": mock.Mock(run_post_index_hooks=lambda: None)}):
                self.assertEqual(reindex.main(), 0)
        self.assertNotIn("--full", commands["ingest transcripts (rust)"])
        self.assertEqual(commands["embed messages"][1:], ["py/embed.py"])

    def test_corrupt_completion_signature_is_a_cache_miss_not_a_crash(self) -> None:
        # P5: an invalid-UTF-8 .reindex.sig raised UnicodeDecodeError out of
        # main() on an incremental run; corruption must read as "no signature".
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "agrep-rs"
            binary.touch()
            messages = root / "messages.jsonl"
            messages.write_text('{"m":"stable"}\n', encoding="utf-8")
            signature = root / ".reindex.sig"
            signature.write_bytes(b"\xff\xfe\x00corrupt")
            with mock.patch.object(sys, "argv", ["reindex.py", "--no-build"]), \
                    mock.patch.object(reindex.common, "ingest_bin", return_value=binary), \
                    mock.patch.object(reindex.common, "MESSAGES_PATH", messages), \
                    mock.patch.object(reindex.common, "DATA_DIR", root), \
                    mock.patch.object(
                        reindex.indexd_runtime, "rust_writer_env",
                        return_value={"AGREP_RUNTIME_BUILD_ID": "a" * 20}), \
                    mock.patch.object(
                        reindex.indexd_runtime, "derived_writer_mutation_info",
                        return_value=mock.Mock(writable=True)), \
                    mock.patch.object(reindex, "run", return_value=True), \
                    mock.patch.object(
                        reindex.indexd_runtime, "refresh_search_index",
                        return_value=True), \
                    mock.patch.dict(sys.modules, {"semantic": mock.Mock(
                        runtime_dependencies_available=lambda: False
                    ), "indexer": mock.Mock(run_post_index_hooks=lambda: None)}):
                self.assertEqual(reindex.main(), 0)
            # the corrupt marker must not survive to poison the next run either
            self.assertFalse(signature.exists())

    def test_read_signature_treats_unreadable_marker_as_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / ".reindex.sig"
            self.assertIsNone(reindex._read_signature(marker))
            marker.write_bytes(b"\xff\xfe\x00corrupt")
            self.assertIsNone(reindex._read_signature(marker))
            marker.write_text("abc:123\n", encoding="utf-8")
            self.assertEqual(reindex._read_signature(marker), "abc:123")


if __name__ == "__main__":
    unittest.main()
