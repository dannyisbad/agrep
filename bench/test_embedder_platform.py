from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import platform
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    path = Path(__file__).with_name("embedder_platform.py")
    spec = importlib.util.spec_from_file_location("agrep_embedder_platform_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EmbedderPlatformTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.platform = _load_module()
        cls.manifest = cls.platform.selection.load_manifest()
        cls.baseline = next(
            profile for profile in cls.manifest["profiles"] if profile["baseline"])

    def _profile_result(self, profile: dict | None = None) -> dict:
        profile = self.baseline if profile is None else profile
        dim = profile["runtime_profile"]["dim"]
        provider = profile["runtime_profile"].get(
            "provider", "CPUExecutionProvider")
        return {
            "artifact_digest": self.platform._artifact_digest(profile),
            "available_providers": [provider],
            "measurement": {
                "cpu_ms": 20.0,
                "exit_code": 0,
                "max_processes": 1,
                "peak_handles": 20,
                "peak_rss_mib": 150.0,
                "scope": "fresh-process-load-vector-smoke-final-exit",
                "wall_ms": 30.0,
            },
            "profile": profile["id"],
            "requested_provider": provider,
            "runtime_profile_digest": self.platform._runtime_digest(profile),
            "semantic_threads": 4,
            "session_providers": [provider],
            "vectors": {
                "document_count": 4,
                "document_norms": [1.0, 1.0, 1.0, 1.0],
                "document_repeat_max_abs": 0.0,
                "documents_sha256": "b" * 64,
                "documents_shape": [4, dim],
                "dtype": "float32",
                "edge_document_norms": [1.0, 1.0],
                "edge_documents_sha256": "c" * 64,
                "edge_documents_shape": [2, dim],
                "finite": True,
                "mixed_solo_cosines": [1.0, 1.0, 1.0, 1.0],
                "mixed_solo_min_cosine": 1.0,
                "normalized_max_error": 0.0,
                "query_norm": 1.0,
                "query_repeat_max_abs": 0.0,
                "query_sha256": "a" * 64,
                "query_shape": [dim],
                "repeated_outputs_equal": True,
            },
        }

    def _bundle(self, tag: str = "darwin-arm64",
                profile: dict | None = None) -> dict:
        profile = self.baseline if profile is None else profile
        sys_platform, machine = tag.split("-", 1)
        manifest_digest = self.platform._digest(self.manifest)
        return {
            "kind": self.platform.KIND,
            "manifest_digest": manifest_digest,
            "platforms": {
                tag: {
                    "code_digest": self.platform._code_digest(),
                    "identity": {
                        "machine": machine,
                        "onnxruntime_device": "CPU",
                        "onnxruntime_version": "1.22.0",
                        "python_implementation": "CPython",
                        "python_version": "3.13.5",
                        "sys_platform": sys_platform,
                    },
                    "manifest_digest": manifest_digest,
                    "profiles": {profile["id"]: self._profile_result(profile)},
                    "tag": tag,
                },
            },
            "schema": 1,
        }

    def _measured_profile(self, profile: dict) -> dict:
        value = self._profile_result(profile)
        value["_identity"] = {
            "machine": platform.machine().lower(),
            "onnxruntime_device": "CPU",
            "onnxruntime_version": "1.22.0",
            "python_implementation": platform.python_implementation(),
            "python_version": platform.python_version(),
            "sys_platform": sys.platform,
        }
        return value

    def _run_args(self, root: Path, profiles: list[dict],
                  *, keep_going: bool) -> argparse.Namespace:
        return argparse.Namespace(
            artifact_cache=root / "cache",
            keep_going=keep_going,
            manifest=self.platform.DEFAULT_MANIFEST,
            output=root / "evidence.json",
            profile=[profile["id"] for profile in profiles],
            timeout=10.0,
        )

    def test_committed_manifest_bundle_validates(self) -> None:
        value = self._bundle()
        self.assertIs(self.platform.validate_bundle(value, self.manifest), value)

    def test_code_digest_covers_platform_tool_and_runtime(self) -> None:
        self.assertEqual(len(self.platform._code_digest()), 64)
        original = self.platform._file_digest
        seen = []

        def record(path: Path) -> str:
            seen.append(path)
            return original(path)

        self.platform._file_digest = record
        try:
            self.platform._code_digest()
        finally:
            self.platform._file_digest = original
        self.assertIn(ROOT / "bench" / "embedder_platform.py", seen)
        self.assertIn(ROOT / "bench" / "resources.py", seen)
        self.assertIn(ROOT / "py" / "embedder.py", seen)

    def test_seed_copies_only_exact_pinned_files(self) -> None:
        payloads = {"model.onnx": b"model", "tokenizer.json": b"tokenizer"}
        profile = json.loads(json.dumps(self.baseline))
        runtime = profile["runtime_profile"]
        runtime["files"] = {
            name: {
                "remote_path": name,
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            for name, payload in payloads.items()
        }
        runtime["model_file"] = "model.onnx"
        runtime["tokenizer_file"] = "tokenizer.json"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cache = root / "cache" / profile["id"]
            cache.mkdir(parents=True)
            for name, payload in payloads.items():
                (cache / name).write_bytes(payload)
            (cache / "rejected-model.onnx").write_bytes(b"must not seed")
            destination = self.platform._seed_artifacts(
                profile, root / "cache", root / "models")
            self.assertEqual(
                sorted(path.name for path in destination.iterdir()), sorted(payloads))

    def test_corrupt_artifact_pin_fails_closed(self) -> None:
        profile = json.loads(json.dumps(self.baseline))
        name, spec = next(iter(profile["runtime_profile"]["files"].items()))
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cache = root / profile["id"]
            cache.mkdir(parents=True)
            (cache / name).write_bytes(b"x" * spec["size"])
            with self.assertRaisesRegex(
                    self.platform.HarnessError, "size/SHA-256 pin"):
                self.platform._seed_artifacts(profile, root, root / "models")

    def test_mismatched_manifest_digest_fails_closed(self) -> None:
        value = self._bundle()
        value["manifest_digest"] = "0" * 64
        with self.assertRaisesRegex(
                self.platform.HarnessError, "manifest/schema digest mismatch"):
            self.platform.validate_bundle(value, self.manifest)

    def test_boolean_schema_alias_fails_closed(self) -> None:
        value = self._bundle()
        value["schema"] = True
        with self.assertRaisesRegex(
                self.platform.HarnessError, "manifest/schema"):
            self.platform.validate_bundle(value, self.manifest)

    def test_mismatched_code_digest_fails_closed(self) -> None:
        value = self._bundle()
        value["platforms"]["darwin-arm64"]["code_digest"] = "0" * 64
        with self.assertRaisesRegex(
                self.platform.HarnessError, "mismatched code digest"):
            self.platform.validate_bundle(value, self.manifest)

    def test_mismatched_runtime_profile_digest_fails_closed(self) -> None:
        value = self._bundle()
        result = next(iter(value["platforms"]["darwin-arm64"]["profiles"].values()))
        result["runtime_profile_digest"] = "0" * 64
        with self.assertRaisesRegex(
                self.platform.HarnessError, "runtime profile digest mismatch"):
            self.platform.validate_bundle(value, self.manifest)

    def test_profile_key_mismatch_fails_closed(self) -> None:
        value = self._bundle()
        result = next(iter(value["platforms"]["darwin-arm64"]["profiles"].values()))
        result["profile"] = "different-profile"
        with self.assertRaisesRegex(
                self.platform.HarnessError, "key/profile mismatch"):
            self.platform.validate_bundle(value, self.manifest)

    def test_requested_provider_mismatch_fails_closed(self) -> None:
        value = self._bundle()
        result = next(iter(value["platforms"]["darwin-arm64"]["profiles"].values()))
        result["requested_provider"] = "CUDAExecutionProvider"
        result["available_providers"] = ["CUDAExecutionProvider"]
        result["session_providers"] = ["CUDAExecutionProvider"]
        with self.assertRaisesRegex(
                self.platform.HarnessError, "requested provider mismatch"):
            self.platform.validate_bundle(value, self.manifest)

    def test_provider_must_be_first_in_session(self) -> None:
        value = self._bundle()
        result = next(iter(value["platforms"]["darwin-arm64"]["profiles"].values()))
        result["session_providers"] = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        with self.assertRaisesRegex(
                self.platform.HarnessError, "provider proof is invalid"):
            self.platform.validate_bundle(value, self.manifest)

    def test_mixed_batch_instability_fails_closed(self) -> None:
        value = self._bundle()
        result = next(iter(value["platforms"]["darwin-arm64"]["profiles"].values()))
        result["vectors"]["mixed_solo_cosines"][0] = 0.99
        result["vectors"]["mixed_solo_min_cosine"] = 0.99
        with self.assertRaisesRegex(
                self.platform.HarnessError, "batch stability"):
            self.platform.validate_bundle(value, self.manifest)

    def test_conflicting_duplicate_platform_fails_without_overwrite(self) -> None:
        first = self._bundle()
        second = json.loads(json.dumps(first))
        result = next(iter(second["platforms"]["darwin-arm64"]["profiles"].values()))
        result["measurement"]["wall_ms"] = 31.0
        with self.assertRaisesRegex(
                self.platform.HarnessError, "conflicting physical evidence"):
            self.platform.merge_bundles([first, second], self.manifest)

    def test_identical_duplicate_and_distinct_platform_are_additive(self) -> None:
        mac = self._bundle()
        windows = self._bundle("win32-x86_64")
        merged = self.platform.merge_bundles([mac, mac, windows], self.manifest)
        self.assertEqual(
            sorted(merged["platforms"]), ["darwin-arm64", "win32-x86_64"])

    def test_disjoint_profiles_merge_on_the_same_platform(self) -> None:
        candidate = next(
            profile for profile in self.manifest["profiles"]
            if profile["status"] == "runnable" and not profile["baseline"])
        first = self._bundle()
        second = self._bundle(profile=candidate)
        merged = self.platform.merge_bundles([first, second], self.manifest)
        self.assertEqual(
            set(merged["platforms"]["darwin-arm64"]["profiles"]),
            {self.baseline["id"], candidate["id"]},
        )

    def test_keep_going_writes_only_successful_profile_evidence(self) -> None:
        candidate = next(
            profile for profile in self.manifest["profiles"]
            if profile["status"] == "runnable" and not profile["baseline"])
        with tempfile.TemporaryDirectory() as raw:
            args = self._run_args(Path(raw), [self.baseline, candidate],
                                  keep_going=True)

            def measure(profile: dict, *_args) -> dict:
                if profile["id"] == self.baseline["id"]:
                    raise self.platform.HarnessError(
                        "runtime failed at /Users/example/model.onnx\nsecret tail")
                return self._measured_profile(profile)

            stdout = io.StringIO()
            with mock.patch.object(self.platform, "_measure_profile", measure), \
                    contextlib.redirect_stdout(stdout):
                status = self.platform.stage_run(args)
            summary = json.loads(stdout.getvalue())
            evidence = self.platform._read_bundle(args.output)
            tag = self.platform._platform_tag()
            self.assertEqual(status, 2)
            self.assertEqual(summary["status"], "partial")
            self.assertEqual(summary["profiles"], [candidate["id"]])
            self.assertEqual(summary["failures"], [{
                "category": "harness_error",
                "detail": "runtime failed at <path> secret tail",
                "profile": self.baseline["id"],
            }])
            self.assertNotIn("Users", summary["failures"][0]["detail"])
            self.assertEqual(
                set(evidence["platforms"][tag]["profiles"]), {candidate["id"]})

    def test_keep_going_all_failures_do_not_create_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            args = self._run_args(Path(raw), [self.baseline], keep_going=True)
            stdout = io.StringIO()
            failure = OSError("C:\\Users\\private\\model.onnx is unavailable")
            with mock.patch.object(
                    self.platform, "_measure_profile", side_effect=failure), \
                    contextlib.redirect_stdout(stdout):
                status = self.platform.stage_run(args)
            summary = json.loads(stdout.getvalue())
            self.assertEqual(status, 2)
            self.assertEqual(summary["status"], "failed")
            self.assertEqual(summary["profiles"], [])
            self.assertEqual(summary["evidence"], None)
            self.assertEqual(summary["failures"][0]["category"], "os_error")
            self.assertNotIn("Users", summary["failures"][0]["detail"])
            self.assertFalse(args.output.exists())

    def test_default_run_remains_fail_fast(self) -> None:
        candidate = next(
            profile for profile in self.manifest["profiles"]
            if profile["status"] == "runnable" and not profile["baseline"])
        with tempfile.TemporaryDirectory() as raw:
            args = self._run_args(Path(raw), [self.baseline, candidate],
                                  keep_going=False)
            calls = []

            def measure(profile: dict, *_args) -> dict:
                calls.append(profile["id"])
                raise self.platform.HarnessError("first failure")

            with mock.patch.object(self.platform, "_measure_profile", measure), \
                    self.assertRaisesRegex(
                        self.platform.HarnessError, "first failure"):
                self.platform.stage_run(args)
            self.assertEqual(calls, [self.baseline["id"]])
            self.assertFalse(args.output.exists())

    def test_keep_going_success_returns_complete_summary(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            args = self._run_args(Path(raw), [self.baseline], keep_going=True)
            stdout = io.StringIO()
            with mock.patch.object(
                    self.platform, "_measure_profile",
                    return_value=self._measured_profile(self.baseline)), \
                    contextlib.redirect_stdout(stdout):
                status = self.platform.stage_run(args)
            summary = json.loads(stdout.getvalue())
            self.assertEqual(status, 0)
            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["failures"], [])
            self.assertEqual(summary["profiles"], [self.baseline["id"]])
            self.assertTrue(args.output.exists())


if __name__ == "__main__":
    unittest.main()
