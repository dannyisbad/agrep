from __future__ import annotations

import copy
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from _test_support import isolate_data_dir

isolate_data_dir()
import embedder


def _raw_profile() -> dict:
    return {
        "schema": 1,
        "id": "candidate-q8",
        "repo": "owner/candidate-ONNX",
        "revision": "a" * 40,
        "license": "apache-2.0",
        "license_permissive": True,
        "dim": 2,
        "native_dim": 4,
        "max_seq": 512,
        "pooling": "masked_mean",
        "normalize": True,
        "query_prefix": "query: ",
        "document_prefix": "passage: ",
        "layernorm_before_truncate": False,
        "quantization": "int8",
        "runtime": "onnxruntime",
        "provider": "CPUExecutionProvider",
        "files": {
            "candidate.onnx": {
                "remote_path": "onnx/candidate.onnx",
                "size": 10,
                "sha256": "1" * 64,
            },
            "candidate.onnx_data": {
                "remote_path": "onnx/candidate.onnx_data",
                "size": 20,
                "sha256": "2" * 64,
            },
            "candidate-tokenizer.json": {
                "remote_path": "tokenizer.json",
                "size": 5,
                "sha256": "3" * 64,
            },
        },
        "model_file": "candidate.onnx",
        "tokenizer_file": "candidate-tokenizer.json",
        "model_bytes": 30,
        "output": {"name": "sentence_embedding"},
        "search_bands": {"floor": 0.2, "strong": 0.6},
    }


def _load(raw: dict) -> dict:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "profile.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return embedder._load_bench_profile(path)


class _Node:
    def __init__(self, name: str, shape=None, dtype="tensor(float)"):
        self.name = name
        self.shape = shape
        self.type = dtype


class _Session:
    def __init__(self, output, inputs=None, outputs=None):
        self.output = output
        self.input_names = inputs or ["input_ids", "attention_mask"]
        self.output_names = outputs or ["sentence_embedding"]
        self.calls = []

    def get_inputs(self):
        return [name if isinstance(name, _Node) else _Node(name)
                for name in self.input_names]

    def get_outputs(self):
        return [_Node(name) for name in self.output_names]

    def run(self, names, feed):
        self.calls.append((names, feed))
        return [self.output]


class _Encoding:
    def __init__(self, ids, mask=None, types=None):
        self.ids = ids
        self.attention_mask = mask if mask is not None else [1] * len(ids)
        self.type_ids = types if types is not None else [0] * len(ids)


def _runtime(profile: dict, output, inputs=None, outputs=None):
    instance = embedder.Embedder.__new__(embedder.Embedder)
    instance.profile = profile
    instance.sess = _Session(output, inputs=inputs, outputs=outputs)
    instance.inputs = instance._validate_session()
    instance.pad_id = 0
    return instance


class BenchProfileSchemaTests(unittest.TestCase):
    def test_full_manifest_normalizes_runtime_fields(self):
        profile = _load(_raw_profile())
        self.assertEqual(profile["files"]["candidate.onnx"], (10, "1" * 64))
        self.assertEqual(profile["remote_paths"]["candidate.onnx"],
                         "onnx/candidate.onnx")
        self.assertEqual(profile["provider"], "CPUExecutionProvider")
        self.assertEqual(profile["output"], {"name": "sentence_embedding"})

    def test_provider_defaults_to_cpu(self):
        raw = _raw_profile()
        raw.pop("provider")
        self.assertEqual(_load(raw)["provider"], "CPUExecutionProvider")

    def test_nonpermissive_profile_remains_benchmarkable(self):
        raw = _raw_profile()
        raw["license_permissive"] = False
        self.assertFalse(_load(raw)["license_permissive"])

    def test_schema_rejects_unknown_keys_and_partial_pins(self):
        raw = _raw_profile()
        raw["surprise"] = True
        with self.assertRaisesRegex(embedder.EmbedderUnavailable, "unknown keys"):
            _load(raw)
        raw = _raw_profile()
        raw["revision"] = "main"
        with self.assertRaisesRegex(embedder.EmbedderUnavailable, "full lowercase"):
            _load(raw)
        raw = _raw_profile()
        raw["files"]["candidate.onnx"]["sha256"] = "1" * 63
        with self.assertRaisesRegex(embedder.EmbedderUnavailable, "64 lowercase"):
            _load(raw)

    def test_schema_version_rejects_boolean_and_float_aliases(self):
        for value in (True, 1.0):
            with self.subTest(value=value):
                raw = _raw_profile()
                raw["schema"] = value
                with self.assertRaisesRegex(embedder.EmbedderUnavailable, "integer"):
                    _load(raw)

    def test_schema_rejects_duplicate_json_keys(self):
        raw = json.dumps(_raw_profile()).replace(
            '"schema": 1,', '"schema": 1, "schema": 1,', 1)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "profile.json"
            path.write_text(raw, encoding="utf-8")
            with self.assertRaisesRegex(embedder.EmbedderUnavailable,
                                        "duplicate JSON key 'schema'"):
                embedder._load_bench_profile(path)

    def test_schema_rejects_unsafe_and_case_colliding_names(self):
        raw = _raw_profile()
        spec = raw["files"].pop("candidate.onnx")
        raw["files"]["../candidate.onnx"] = spec
        raw["model_file"] = "../candidate.onnx"
        with self.assertRaisesRegex(embedder.EmbedderUnavailable, "portable"):
            _load(raw)
        raw = _raw_profile()
        raw["files"]["CANDIDATE.ONNX"] = copy.deepcopy(
            raw["files"]["candidate.onnx"])
        raw["model_bytes"] += 10
        with self.assertRaisesRegex(embedder.EmbedderUnavailable,
                                    "unique case-insensitively"):
            _load(raw)

    def test_schema_rejects_unpinned_files_and_bad_model_byte_total(self):
        raw = _raw_profile()
        raw["model_file"] = "other.onnx"
        with self.assertRaisesRegex(embedder.EmbedderUnavailable, "pinned artifacts"):
            _load(raw)
        raw = _raw_profile()
        raw["model_bytes"] += 1
        with self.assertRaisesRegex(embedder.EmbedderUnavailable, "pinned non-tokenizer"):
            _load(raw)

    def test_vector_identity_excludes_policy_but_includes_provider(self):
        first = _load(_raw_profile())
        second = copy.deepcopy(first)
        second["search_bands"] = {"floor": -1.0, "strong": 1.0}
        second["id"] = "another-label"
        second["repo"] = "moved/repository"
        second["revision"] = "b" * 40
        self.assertEqual(embedder._vector_identity(first),
                         embedder._vector_identity(second))
        second["provider"] = "CoreMLExecutionProvider"
        self.assertNotEqual(embedder._vector_identity(first),
                            embedder._vector_identity(second))
        second["provider"] = first["provider"]
        second["query_prefix"] = "search_query: "
        self.assertNotEqual(embedder._vector_identity(first),
                            embedder._vector_identity(second))

    def test_search_bands_are_policy_only(self):
        profile = _load(_raw_profile())
        with mock.patch.object(embedder, "PROFILE", profile):
            self.assertEqual(embedder.semantic_bands(), (0.2, 0.6))
        with mock.patch.object(embedder, "PROFILE", {"dim": 384}):
            self.assertEqual(embedder.semantic_bands(), (0.82, 0.84))

    def test_search_uses_candidate_bands_without_changing_production_default(self):
        with tempfile.TemporaryDirectory() as td:
            profile_path = Path(td) / "profile.json"
            profile_path.write_text(json.dumps(_raw_profile()), encoding="utf-8")
            env = dict(os.environ)
            env[embedder._BENCH_PROFILE_ENV] = str(profile_path)
            result = subprocess.run(
                [sys.executable, "-c", "import search; print("
                 "search.SEMANTIC_MIN_COSINE, search._RECALL_STRONG_SEM)"],
                cwd=Path(embedder.__file__).parent, env=env, capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "0.2 0.6")

    def test_profile_environment_requires_absolute_path(self):
        with mock.patch.dict(embedder.os.environ,
                             {embedder._BENCH_PROFILE_ENV: "relative.json"}):
            with self.assertRaisesRegex(embedder.EmbedderUnavailable, "absolute path"):
                embedder._activate_bench_profile()

    def test_absolute_profile_environment_activates_canonical_identity(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "profile.json"
            path.write_text(json.dumps(_raw_profile()), encoding="utf-8")
            prior = embedder.PROFILE, embedder.PROFILE_STRING
            try:
                with mock.patch.dict(embedder.os.environ,
                                     {embedder._BENCH_PROFILE_ENV: str(path)}):
                    embedder._activate_bench_profile()
                self.assertEqual(embedder.PROFILE["model_file"], "candidate.onnx")
                self.assertEqual(embedder.PROFILE_STRING,
                                 embedder._vector_identity(embedder.PROFILE))
            finally:
                embedder.PROFILE, embedder.PROFILE_STRING = prior


class EmbedderRuntimeProfileTests(unittest.TestCase):
    def setUp(self):
        self.profile = _load(_raw_profile())

    def test_named_3d_output_uses_masked_mean_then_mrl_normalization(self):
        output = np.asarray([[
            [1.0, 2.0, 9.0, 9.0],
            [3.0, 4.0, 9.0, 9.0],
            [100.0, 100.0, 9.0, 9.0],
        ]], dtype=np.float32)
        instance = _runtime(self.profile, output)
        got = instance._run_encoded([_Encoding([7, 8, 0], [1, 1, 0])])
        expected = np.asarray([[2.0, 3.0]], dtype=np.float32)
        expected /= np.linalg.norm(expected, axis=1, keepdims=True)
        np.testing.assert_allclose(got, expected, rtol=1e-6)
        names, feed = instance.sess.calls[0]
        self.assertEqual(names, ["sentence_embedding"])
        self.assertEqual(feed["input_ids"].shape, (1, 3))

    def test_declared_padding_contract_wins_over_token_spelling(self):
        tokenizer = mock.Mock()
        tokenizer.padding = {
            "pad_id": 179935, "pad_type_id": 7,
            "pad_token": "<|endoftext|>", "direction": "right",
        }
        tokenizer.token_to_id.return_value = 0
        self.assertEqual(
            embedder.Embedder._padding_contract(tokenizer),
            (179935, 7, "<|endoftext|>"))
        tokenizer.token_to_id.assert_not_called()

    def test_undeclared_padding_uses_known_token(self):
        tokenizer = mock.Mock()
        tokenizer.padding = None
        tokenizer.token_to_id.side_effect = (
            lambda token: 50283 if token == "[PAD]" else None)
        self.assertEqual(
            embedder.Embedder._padding_contract(tokenizer), (50283, 0, "[PAD]"))

    def test_padding_contract_rejects_unknown_padding(self):
        tokenizer = mock.Mock()
        tokenizer.padding = None
        tokenizer.token_to_id.return_value = None
        with self.assertRaisesRegex(embedder.EmbedderUnavailable, "no declared"):
            embedder.Embedder._padding_contract(tokenizer)

    def test_profile_padding_token_is_required_exactly(self):
        tokenizer = mock.Mock()
        tokenizer.token_to_id.return_value = 151643
        self.assertEqual(
            embedder.Embedder._padding_contract(tokenizer, "<|endoftext|>"),
            (151643, 0, "<|endoftext|>"))
        tokenizer.token_to_id.return_value = None
        with self.assertRaisesRegex(embedder.EmbedderUnavailable, "absent"):
            embedder.Embedder._padding_contract(tokenizer, "<|endoftext|>")

    def test_2d_output_is_direct_and_index_selectable(self):
        self.profile["output"] = {"index": 1}
        self.profile["pooling"] = "direct_2d"
        output = np.asarray([[3.0, 4.0, 8.0, 9.0]], dtype=np.float32)
        instance = _runtime(self.profile, output, outputs=["tokens", "pooled"])
        instance.sess.run = mock.Mock(return_value=[
            np.zeros((1, 2, 4), dtype=np.float32), output])
        got = instance._run_encoded([_Encoding([1, 2])])
        np.testing.assert_allclose(got, [[0.6, 0.8]], rtol=1e-6)
        self.assertEqual(instance.sess.run.call_args.args[0], None)

    def test_direct_2d_rejects_token_output(self):
        self.profile["pooling"] = "direct_2d"
        output = np.zeros((1, 1, 4), dtype=np.float32)
        instance = _runtime(self.profile, output)
        with self.assertRaisesRegex(embedder.EmbedderUnavailable, "requires a rank-2"):
            instance._run_encoded([_Encoding([1])])

    def test_token_pooling_rejects_rank_2_output(self):
        output = np.zeros((1, 4), dtype=np.float32)
        instance = _runtime(self.profile, output)
        with self.assertRaisesRegex(embedder.EmbedderUnavailable,
                                    "requires a rank-3"):
            instance._run_encoded([_Encoding([1])])

    def test_zero_norm_output_is_rejected(self):
        self.profile["pooling"] = "direct_2d"
        output = np.zeros((1, 4), dtype=np.float32)
        instance = _runtime(self.profile, output)
        with self.assertRaisesRegex(embedder.EmbedderUnavailable, "zero-norm"):
            instance._run_encoded([_Encoding([1])])

    def test_backend_inference_error_becomes_unavailable(self):
        self.profile["pooling"] = "direct_2d"
        instance = _runtime(self.profile, np.zeros((1, 4), dtype=np.float32))
        instance.sess.run = mock.Mock(side_effect=Exception("provider rejected graph"))
        with self.assertRaisesRegex(embedder.EmbedderUnavailable,
                                    "ONNX inference failed"):
            instance._run_encoded([_Encoding([1])])

    def test_cls_and_last_token_pooling(self):
        output = np.asarray([[
            [3.0, 4.0, 0.0, 0.0],
            [6.0, 8.0, 0.0, 0.0],
            [5.0, 12.0, 0.0, 0.0],
        ]], dtype=np.float32)
        self.profile["pooling"] = "cls"
        cls = _runtime(self.profile, output)._run_encoded(
            [_Encoding([1, 2, 0], [1, 1, 0])])
        np.testing.assert_allclose(cls, [[0.6, 0.8]], rtol=1e-6)
        self.profile["pooling"] = "last_token"
        last = _runtime(self.profile, output)._run_encoded(
            [_Encoding([1, 2, 0], [1, 1, 0])])
        np.testing.assert_allclose(last, [[0.6, 0.8]], rtol=1e-6)

    def test_layernorm_runs_before_mrl_truncation(self):
        self.profile["layernorm_before_truncate"] = True
        output = np.asarray([[[1.0, 2.0, 10.0, 20.0]]], dtype=np.float32)
        got = _runtime(self.profile, output)._run_encoded([_Encoding([1])])
        pooled = output[:, 0, :]
        mean = pooled.mean(axis=1, keepdims=True)
        variance = ((pooled - mean) ** 2).mean(axis=1, keepdims=True)
        expected = ((pooled - mean) / np.sqrt(variance + 1e-5))[:, :2]
        expected /= np.linalg.norm(expected, axis=1, keepdims=True)
        np.testing.assert_allclose(got, expected, rtol=1e-6)

    def test_prefixes_apply_only_to_their_lane(self):
        instance = embedder.Embedder.__new__(embedder.Embedder)
        instance.profile = self.profile
        instance._run = mock.Mock(return_value=np.asarray([[1.0, 0.0]]))
        instance.embed_query("deadlock")
        instance._run.assert_called_once_with(["query: deadlock"])
        instance.tok = mock.Mock()
        instance.tok.encode.side_effect = lambda text: _Encoding([len(text)])
        instance._run_encoded = mock.Mock(return_value=np.asarray([[1.0, 0.0]]))
        instance.embed_texts(["race"])
        instance.tok.encode.assert_called_once_with("passage: race")

    def test_known_position_and_token_type_inputs_are_materialized(self):
        output = np.asarray([[3.0, 4.0, 0.0, 0.0]], dtype=np.float32)
        self.profile["pooling"] = "direct_2d"
        inputs = ["input_ids", "attention_mask", "token_type_ids", "position_ids"]
        instance = _runtime(self.profile, output, inputs=inputs)
        instance._run_encoded([_Encoding([4, 5], types=[7, 8])])
        feed = instance.sess.calls[0][1]
        np.testing.assert_array_equal(feed["token_type_ids"], [[7, 8]])
        np.testing.assert_array_equal(feed["position_ids"], [[0, 1]])

    def test_decoder_cache_inputs_receive_an_empty_prefix(self):
        output = np.asarray([[
            [1.0, 2.0, 0.0, 0.0], [3.0, 4.0, 0.0, 0.0],
        ]], dtype=np.float32)
        self.profile["pooling"] = "last_token"
        cache = _Node(
            "past_key_values.0.key", ["batch", 8, "past", 128],
            "tensor(float)")
        instance = _runtime(
            self.profile, output,
            inputs=["input_ids", "attention_mask", "position_ids", cache])
        instance._run_encoded([_Encoding([4, 5])])
        feed = instance.sess.calls[0][1]
        self.assertEqual(feed[cache.name].shape, (1, 8, 0, 128))
        self.assertEqual(feed[cache.name].dtype, np.float32)

    def test_decoder_cache_inputs_fail_closed_on_unknown_layout(self):
        instance = embedder.Embedder.__new__(embedder.Embedder)
        instance.profile = self.profile
        cache = _Node(
            "past_key_values.0.key", ["batch", "heads", "past", 128],
            "tensor(float)")
        instance.sess = _Session(
            np.zeros((1, 4)), inputs=["input_ids", cache])
        with self.assertRaisesRegex(embedder.EmbedderUnavailable,
                                    "unsupported shape or type"):
            instance._validate_session()

    def test_unknown_mandatory_input_and_bad_output_selector_fail_closed(self):
        instance = embedder.Embedder.__new__(embedder.Embedder)
        instance.profile = self.profile
        instance.sess = _Session(np.zeros((1, 4)),
                                 inputs=["input_ids", "mystery_required"])
        with self.assertRaisesRegex(embedder.EmbedderUnavailable,
                                    "unsupported mandatory"):
            instance._validate_session()
        instance.sess = _Session(np.zeros((1, 4)), outputs=["other"])
        with self.assertRaisesRegex(embedder.EmbedderUnavailable, "is absent"):
            instance._validate_session()

    def test_session_requires_attention_mask(self):
        instance = embedder.Embedder.__new__(embedder.Embedder)
        instance.profile = self.profile
        instance.sess = _Session(np.zeros((1, 4)), inputs=["input_ids"])
        with self.assertRaisesRegex(embedder.EmbedderUnavailable, "attention_mask"):
            instance._validate_session()

    def test_empty_encoded_batch_preserves_vector_width(self):
        output = np.zeros((0, 0, 4), dtype=np.float32)
        instance = _runtime(self.profile, output)
        got = instance._run_encoded([])
        self.assertEqual(got.shape, (0, 2))

    def test_singleton_reloads_when_vector_identity_changes(self):
        original = dict(embedder._CACHED)
        try:
            embedder._CACHED.update({"emb": None, "identity": None})
            with mock.patch.object(embedder, "Embedder", side_effect=["a", "b"]), \
                    mock.patch.object(embedder, "PROFILE_STRING", "profile-a"):
                self.assertEqual(embedder.get(download=False), "a")
                self.assertEqual(embedder.get(download=False), "a")
                embedder.PROFILE_STRING = "profile-b"
                self.assertEqual(embedder.get(download=False), "b")
        finally:
            embedder._CACHED.clear()
            embedder._CACHED.update(original)

    def test_wrong_rank_width_batch_and_nonfinite_outputs_fail_closed(self):
        cases = [
            (np.zeros((4,), dtype=np.float32), "rank 2 or 3"),
            (np.zeros((1, 1, 3), dtype=np.float32), "width is 3"),
            (np.zeros((2, 1, 4), dtype=np.float32), "batch dimension"),
            (np.asarray([[[math.nan, 0.0, 0.0, 0.0]]], dtype=np.float32),
             "non-finite"),
        ]
        for output, message in cases:
            with self.subTest(message=message):
                instance = _runtime(self.profile, output)
                with self.assertRaisesRegex(embedder.EmbedderUnavailable, message):
                    instance._run_encoded([_Encoding([1])])


if __name__ == "__main__":
    unittest.main()
