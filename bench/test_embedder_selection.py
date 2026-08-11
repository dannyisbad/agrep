from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
_PROFILE_VALIDATOR_SCRIPT = r"""
import json
import sys
from pathlib import Path

import embedder

observed = {}
for path in sorted(Path(sys.argv[1]).glob('*.json')):
    loaded = embedder._load_bench_profile(path)
    profile_id = loaded['id']
    if profile_id in observed:
        raise RuntimeError(f'duplicate validated profile {profile_id}')
    observed[profile_id] = {'dim': loaded['dim']}
print(json.dumps(observed, sort_keys=True))
"""


def _load_module():
    path = Path(__file__).with_name("embedder_selection.py")
    spec = importlib.util.spec_from_file_location("agrep_embedder_selection_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _pinned_artifact(payload: bytes, remote_path: str) -> dict:
    return {
        "remote_path": remote_path,
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


class EmbedderSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.selection = _load_module()
        with tempfile.TemporaryDirectory() as raw:
            env = {
                key: value for key, value in os.environ.items()
                if not key.startswith("AGREP_")
            }
            env.update({
                "AGREP_DATA_DIR": str(Path(raw) / "data"),
                "AGREP_DATA_DIR_SOURCE": "test",
                "AGREP_HOME": str(Path(raw) / "home"),
                "PYTHONPATH": str(ROOT / "py"),
            })
            result = subprocess.run(
                [sys.executable, "-c", (
                    "import indexd_runtime; "
                    "print(indexd_runtime.derived_writer_build_id("
                    "require_binary=True))")],
                cwd=ROOT, env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30,
            )
        writer_id = result.stdout.strip()
        if result.returncode != 0 or len(writer_id) != 20 or any(
                byte not in "0123456789abcdef" for byte in writer_id):
            raise RuntimeError(
                "could not resolve the exact test writer identity: "
                + (result.stderr or result.stdout)[-1000:])
        cls.writer_id = writer_id

    def _own_empty_derived_family(self, source: Path) -> None:
        (source / "corpus.db").unlink(missing_ok=True)
        families = []
        with (source / "sessions.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                row = json.loads(line)
                families.append((str(row["session"]), str(row.get("parent") or "")))
        families.sort()
        digest = hashlib.md5(usedforsecurity=False)
        fnv = 0xCBF29CE484222325

        def update(chunk: bytes) -> None:
            nonlocal fnv
            digest.update(chunk)
            for value in chunk:
                fnv ^= value
                fnv = (fnv * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF

        fnv ^= 1
        fnv = (fnv * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
        update(b"agrep-session-family-v1\0")
        for session, parent in families:
            for value in (session, parent):
                encoded = value.encode("utf-8")
                update(len(encoded).to_bytes(8, "little"))
                update(encoded)
        signature = "embedder-selection-test"
        (source / ".ingest.sig").write_text(signature + "\n", encoding="utf-8")
        (source / "session_family.meta.json").write_text(json.dumps({
            "version": 2,
            "algorithm": "md5-fnv64-v1",
            "ingest_signature": signature,
            "count": len(families),
            "digest": f"{digest.hexdigest()}{fnv:016x}",
        }, separators=(",", ":")), encoding="utf-8")
        (source / ".derived-owner.json").write_text(
            json.dumps({"version": 1, "build_id": self.writer_id},
                       separators=(",", ":")),
            encoding="utf-8",
        )

    def test_committed_manifest_is_strict_and_contains_every_survey_configuration(self) -> None:
        manifest = self.selection.load_manifest()
        ids = {row["id"] for row in manifest["profiles"]}
        self.assertEqual(len(ids), 15)
        self.assertIn("granite-small-r2-q8-384", ids)
        self.assertIn("static-retrieval-mrl-en-v1-256", ids)
        self.assertIn("embeddinggemma-300m-256", ids)
        self.assertIn("nomic-embed-text-v1.5-384", ids)
        self.assertIn("gte-modernbert-base-768", ids)
        self.assertIn("f2llm-v2-80m-320", ids)
        baseline = next(row for row in manifest["profiles"] if row["baseline"])
        self.assertEqual(baseline["status"], "runnable")
        self.assertEqual(baseline["runtime_profile"]["model_bytes"], 52_484_470)
        blocked = next(row for row in manifest["profiles"]
                       if row["id"] == "f2llm-v2-80m-320")
        self.assertFalse(blocked["adoption_eligible"])
        static = next(row for row in manifest["profiles"]
                      if row["id"] == "static-retrieval-mrl-en-v1-256")
        self.assertTrue(static["adoption_eligible"])
        self.assertEqual(static["runtime_profile"]["dim"], 256)
        self.assertEqual(static["runtime_profile"]["pooling"], "direct_2d")

    def test_committed_calibration_is_explicitly_synthetic(self) -> None:
        calibration = self.selection.load_calibration()
        self.assertEqual(
            calibration["provenance"], self.selection.CALIBRATION_PROVENANCE)
        self.assertGreaterEqual(len(calibration["real"]), 7)
        self.assertGreaterEqual(len(calibration["gibberish"]), 4)

    def test_manifest_rejects_revision_hash_and_size_drift(self) -> None:
        manifest = self.selection.load_manifest()
        baseline = next(row for row in manifest["profiles"] if row["baseline"])
        for field, value in (
            ("revision", "main"),
            ("sha256", "0" * 63),
            ("size", 0),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as raw:
                changed = json.loads(json.dumps(manifest))
                profile = next(row for row in changed["profiles"]
                               if row["id"] == baseline["id"])["runtime_profile"]
                if field == "revision":
                    profile[field] = value
                else:
                    profile["files"][profile["model_file"]][field] = value
                path = Path(raw) / "manifest.json"
                path.write_text(json.dumps(changed), encoding="utf-8")
                with self.assertRaises(self.selection.HarnessError):
                    self.selection.load_manifest(path)

    def test_artifact_cache_seeds_split_onnx_with_exact_byte_accounting(self) -> None:
        payloads = {
            "model.onnx": b"graph",
            "model.onnx_data": b"split-weights",
            "tokenizer.json": b'{"tokenizer":true}',
        }
        runtime = {
            "id": "split-model",
            "model_bytes": len(payloads["model.onnx"])
            + len(payloads["model.onnx_data"]),
            "files": {
                name: _pinned_artifact(payload, f"onnx/{name}")
                for name, payload in payloads.items()
            },
        }
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cache = root / "cache"
            source = cache / runtime["id"]
            source.mkdir(parents=True)
            for name, payload in payloads.items():
                (source / name).write_bytes(payload)
            model_dir = root / "campaign"
            metrics = self.selection._seed_artifact_cache(
                cache, model_dir, runtime)
            self.assertEqual(
                {path.name for path in model_dir.iterdir()}, set(payloads))
            self.assertEqual(
                metrics["model_graph_bytes"], runtime["model_bytes"])
            self.assertEqual(
                metrics["pinned_cache_bytes"], sum(map(len, payloads.values())))
            self.assertEqual(metrics["campaign_unreferenced_model_bytes"], 0)

    def test_artifact_cache_rejects_missing_and_corrupt_pinned_files(self) -> None:
        payloads = {"model.onnx": b"model", "tokenizer.json": b"tokenizer"}
        runtime = {
            "id": "candidate",
            "model_bytes": len(payloads["model.onnx"]),
            "files": {
                name: _pinned_artifact(payload, name)
                for name, payload in payloads.items()
            },
        }
        for failure in ("missing", "corrupt"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                source = root / "cache" / runtime["id"]
                source.mkdir(parents=True)
                (source / "model.onnx").write_bytes(payloads["model.onnx"])
                if failure == "corrupt":
                    (source / "tokenizer.json").write_bytes(b"corruptxx")
                with self.assertRaises(self.selection.HarnessError):
                    self.selection._seed_artifact_cache(
                        root / "cache", root / "campaign", runtime)

    def test_artifact_cache_excludes_undeclared_qwen_int8_file(self) -> None:
        payloads = {
            "model_q4f16.onnx": b"q4f16",
            "tokenizer.json": b"tokenizer",
        }
        runtime = {
            "id": "qwen-candidate",
            "model_bytes": len(payloads["model_q4f16.onnx"]),
            "files": {
                name: _pinned_artifact(payload, name)
                for name, payload in payloads.items()
            },
        }
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "cache" / runtime["id"]
            source.mkdir(parents=True)
            for name, payload in payloads.items():
                (source / name).write_bytes(payload)
            (source / "model_int8.onnx").write_bytes(b"rejected-int8")
            model_dir = root / "campaign"
            metrics = self.selection._seed_artifact_cache(
                root / "cache", model_dir, runtime)
            self.assertFalse((model_dir / "model_int8.onnx").exists())
            self.assertEqual(
                metrics["pinned_cache_bytes"], sum(map(len, payloads.values())))
            self.assertEqual(metrics["campaign_unreferenced_model_bytes"], 0)

    def test_prepare_and_performance_contain_exactly_one_full_embed(self) -> None:
        tree = ast.parse(
            (ROOT / "bench/embedder_selection.py").read_text(encoding="utf-8"))
        functions = {
            node.name: node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        def occurrences(name: str) -> int:
            return sum(
                isinstance(node, ast.Constant) and node.value == "--full"
                for node in ast.walk(functions[name]))

        self.assertEqual(occurrences("stage_prepare"), 1)
        self.assertEqual(occurrences("stage_performance"), 0)

    def test_interrupted_prepare_recovers_without_a_second_full_embed(self) -> None:
        def candidate(profile_id: str) -> dict:
            return {
                "id": profile_id,
                "runtime_profile": {
                    "id": profile_id, "files": {}, "model_bytes": 1, "dim": 2,
                },
            }

        profiles = [candidate("first"), candidate("second")]
        manifest = {"profiles": profiles}
        snapshot = {"digest": "a" * 64, "rows": 1, "files": []}
        inputs = {
            "manifest_digest": "b" * 64, "calibration_digest": "c" * 64,
            "quality_digest": "d" * 64, "code_digest": "e" * 64,
        }
        corpus = {
            "schema": "1", "events": {"mode": "legacy"}, "rows": 1,
            "prose_rows": 1, "text_bytes": 4,
        }
        interrupted = [True]
        full_calls: list[str] = []

        def copy_profile(_snapshot, destination, _expected, _timeout) -> None:
            profile_id = destination.parent.name
            if profile_id == "second" and interrupted[0]:
                interrupted[0] = False
                raise self.selection.HarnessError("simulated interruption")
            destination.mkdir(parents=True, exist_ok=True)

        def run(command, *, env, timeout) -> dict:
            del timeout
            profile_id = Path(env["AGREP_BENCH_EMBED_PROFILE"]).parent.name
            result = {
                "command": command, "returncode": 0, "wall_ms": 1.0,
                "cpu_ms": 1.0, "peak_rss_mib": 1.0, "peak_handles": None,
                "max_processes": 1, "stdout": "", "stderr": "",
            }
            joined = " ".join(command)
            if "model.sess.get_providers" in joined:
                result["stdout"] = (
                    '{"providers":["CPUExecutionProvider"],'
                    '"semantic_threads":4}\n')
            elif "embed.py" in joined and "--full" in command:
                full_calls.append(profile_id)
                result["stdout"] = (
                    "embed phases | plan=0.1s | load=0.1s | inference=1.0s | "
                    "f32-publish=0.1s\n"
                    "embed done | model=test | count=1 | dim=2 | elapsed=1.2s\n"
                )
            return result

        def freeze(_data, bundle_dir, _env, _timeout) -> dict:
            bundle_dir.mkdir()
            artifact = bundle_dir / "embeddings.f32"
            artifact.write_bytes(bundle_dir.parent.name.encode("ascii"))
            embedding = {
                "identity": bundle_dir.parent.name, "rows": 1, "q8": None,
            }
            return {
                "schema": 1,
                "inventory": self.selection._bundle_file_inventory(
                    bundle_dir, [artifact.name]),
                "embedding": embedding,
            }

        def embedding_state(data, _env, _timeout, _mode) -> dict:
            return {"identity": data.parent.name, "rows": 1, "q8": None}

        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            args = argparse.Namespace(
                manifest=Path("manifest.json"), calibration_tasks=Path("cal.json"),
                quality_tasks=Path("quality.json"), run_dir=run_dir,
                profile=[], source_data=Path("source"), expect_snapshot=None,
                timeout=1.0, artifact_cache=None, allow_download=False,
                no_embed=False,
            )
            patches = (
                mock.patch.object(self.selection, "load_manifest", return_value=manifest),
                mock.patch.object(self.selection, "load_calibration", return_value={}),
                mock.patch.object(self.selection, "load_quality", return_value=[]),
                mock.patch.object(self.selection, "_profiles", return_value=profiles),
                mock.patch.object(self.selection, "_source_data_dir", return_value=Path("source")),
                mock.patch.object(self.selection, "_copy_snapshot", return_value=snapshot),
                mock.patch.object(self.selection, "_preflight_quality_targets"),
                mock.patch.object(self.selection, "_state_inputs", return_value=inputs),
                mock.patch.object(self.selection, "_copy_profile_data", side_effect=copy_profile),
                mock.patch.object(self.selection, "_bind_corpus", return_value=corpus),
                mock.patch.object(self.selection, "_run", side_effect=run),
                mock.patch.object(self.selection, "_freeze_embedding_bundle", side_effect=freeze),
                mock.patch.object(self.selection, "_embedding_data_state",
                                  side_effect=embedding_state),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], \
                    patches[5], patches[6], patches[7], patches[8], patches[9], \
                    patches[10], patches[11], patches[12]:
                with self.assertRaisesRegex(
                        self.selection.HarnessError, "simulated interruption"):
                    self.selection.stage_prepare(args)
                checkpoint = self.selection._read_json(run_dir / "state.json")
                self.assertEqual(checkpoint["profiles"], ["first"])
                artifact = self.selection._read_json(
                    run_dir / "profiles/first/prepare.json")
                self.assertEqual(artifact["profile"], "first")
                self.assertEqual(artifact["snapshot_digest"], snapshot["digest"])
                self.assertEqual(artifact["campaign_inputs"], inputs)

                (run_dir / "state.json").unlink()
                self.selection.stage_prepare(args)

            finished = self.selection._read_json(run_dir / "state.json")
            self.assertEqual(finished["profiles"], ["first", "second"])
            self.assertEqual(full_calls, ["first", "second"])

    def test_orphan_cleanup_is_confined_to_a_real_bundle_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bundle = root / "prepared-embedding-bundle"
            bundle.mkdir()
            (bundle / "artifact").write_bytes(b"orphan")
            self.selection._remove_orphaned_bundle(bundle)
            self.assertFalse(bundle.exists())

            bundle.write_bytes(b"not-a-directory")
            with self.assertRaisesRegex(
                    self.selection.HarnessError, "not a directory"):
                self.selection._remove_orphaned_bundle(bundle)
            self.assertEqual(bundle.read_bytes(), b"not-a-directory")

    def test_prepared_embedding_bundle_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = Path(raw)
            artifact = bundle / "embeddings.f32"
            artifact.write_bytes(b"first")
            record = {
                "schema": 1,
                "inventory": self.selection._bundle_file_inventory(
                    bundle, [artifact.name]),
                "embedding": {"rows": 1},
            }
            self.selection._validate_embedding_bundle(bundle, record)
            artifact.write_bytes(b"later")
            with self.assertRaisesRegex(self.selection.HarnessError, "drifted"):
                self.selection._validate_embedding_bundle(bundle, record)

    def test_embedding_state_refuses_an_empty_prepared_bundle(self) -> None:
        result = {
            "returncode": 0, "stdout": '{"rows":0}\n', "stderr": "",
            "wall_ms": 1.0, "cpu_ms": 1.0, "peak_rss_mib": 1.0,
        }
        runner = mock.Mock(return_value=result)
        with mock.patch.object(self.selection, "_run", runner), \
                self.assertRaisesRegex(self.selection.HarnessError, "no committed rows"):
            self.selection._embedding_data_state(Path("data"), {}, 1.0, "freeze")
        self.assertTrue(Path(runner.call_args.kwargs["env"]["AGREP_DATA_DIR"]).is_absolute())

    def test_corpus_subprocess_receives_an_absolute_data_dir(self) -> None:
        result = {
            "returncode": 0, "stdout": '{"bound":true}\n', "stderr": "",
            "wall_ms": 1.0, "cpu_ms": 1.0, "peak_rss_mib": 1.0,
        }
        runner = mock.Mock(return_value=result)
        with mock.patch.object(self.selection, "_run", runner):
            self.assertEqual(
                self.selection._corpus_action(Path("data"), 1.0, "inspect"),
                {"bound": True})
        self.assertTrue(Path(runner.call_args.kwargs["env"]["AGREP_DATA_DIR"]).is_absolute())

    def test_default_source_import_prefers_product_resources_from_bench(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            expected = Path(raw) / "data"
            env = {
                key: value for key, value in os.environ.items()
                if not key.startswith("AGREP_")
            }
            env.update({
                "AGREP_DATA_DIR": str(expected),
                "AGREP_DATA_DIR_SOURCE": "test",
                "AGREP_HOME": str(Path(raw) / "home"),
                "PYTHONPATH": os.pathsep.join((str(ROOT), str(ROOT / "py"))),
            })
            result = subprocess.run(
                [sys.executable, "-c", (
                    "import resources; import embedder_selection as selection; "
                    "print(selection._source_data_dir(None))")],
                cwd=ROOT / "bench", env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()).resolve(), expected.resolve())

    def test_restore_preserves_committed_nonempty_bundle_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bundle = root / "bundle"
            bundle.mkdir()
            artifact = bundle / "embeddings.f32"
            artifact.write_bytes(b"prepared-vectors")
            embedding = {"identity": "generation", "rows": 3, "q8": None}
            record = {
                "schema": 1,
                "inventory": self.selection._bundle_file_inventory(
                    bundle, [artifact.name]),
                "embedding": embedding,
            }
            data = root / "data"
            data.mkdir()
            with mock.patch.object(
                    self.selection, "_embedding_data_state",
                    return_value=embedding):
                restored = self.selection._restore_embedding_bundle(
                    bundle, data, record, {}, 1.0)
            self.assertEqual(restored["rows"], 3)
            self.assertEqual(
                artifact.stat().st_ino, (data / artifact.name).stat().st_ino)

    def test_embedding_phase_telemetry_is_required_and_parsed(self) -> None:
        result = {
            "stdout": "\n".join((
                "embedding dedupe: inferred 8 unique texts, reused 2",
                "embed phases | plan=0.100s | load=0.200s | "
                "inference=2.000s | segment-publish=0.300s",
                "embed done | profile=test | count=10 | elapsed=5.000s",
            )),
            "stderr": "",
        }
        stats = self.selection._embed_stats(result, 10)
        self.assertEqual(stats["phases_s"], {
            "plan": 0.1, "load": 0.2, "inference": 2.0, "f32_publish": 0.3,
        })
        self.assertEqual(stats["inferred"], 8)
        self.assertEqual(stats["dedup_effective_rows_per_s"], 5.0)

    def test_performance_refuses_prepare_no_embed_state(self) -> None:
        manifest = self.selection.load_manifest()
        profile = next(row for row in manifest["profiles"] if row["baseline"])
        state = {
            "profiles": [profile["id"]],
            "prepare": {profile["id"]: {"embed_skipped": True}},
        }
        args = argparse.Namespace(
            run_dir=Path("run"), profile=[profile["id"]], timeout=1.0,
            queries=2, top_up_rows=1000)
        with mock.patch.object(
                self.selection, "_load_state",
                return_value=(state, manifest, {"real": [
                    {"id": "one", "query": "one"},
                    {"id": "two", "query": "two"},
                ]}, [])), \
                mock.patch.object(
                    self.selection, "_validated_calibrated_profile",
                    return_value=({}, Path("profile.json"), "digest")), \
                self.assertRaisesRegex(
                    self.selection.HarnessError, "prepared full embedding bundle"):
            self.selection.stage_performance(args)

    def test_json_reader_rejects_duplicate_keys(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "duplicate.json"
            path.write_text('{"schema":1,"schema":2}', encoding="utf-8")
            with self.assertRaisesRegex(self.selection.HarnessError, "duplicate JSON key"):
                self.selection._read_json(path)

    def test_code_digest_covers_python_runtime_cli_resources_and_binary(self) -> None:
        inputs = set(self.selection._code_inputs())
        self.assertIn(ROOT / "cli.py", inputs)
        self.assertIn(ROOT / "bench/resources.py", inputs)
        self.assertTrue(set((ROOT / "py").glob("*.py")).issubset(inputs))
        for binary in (ROOT / "_bin/agrep-rs", ROOT / "_bin/agrep-rs.exe"):
            if binary.is_file():
                self.assertIn(binary, inputs)
        self.assertEqual(len(self.selection._code_digest()), 64)

    def test_windows_subprocesses_are_hidden(self) -> None:
        flag = 0x08000000
        with mock.patch.object(self.selection.sys, "platform", "win32"), \
                mock.patch.object(
                    self.selection.subprocess, "CREATE_NO_WINDOW", flag, create=True):
            self.assertEqual(
                self.selection._popen_platform_kwargs(), {"creationflags": flag})

    def test_every_runnable_profile_passes_the_production_runtime_validator(self) -> None:
        manifest = self.selection.load_manifest()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            expected = {}
            for profile in manifest["profiles"]:
                if profile["status"] != "runnable":
                    continue
                path = root / f"{profile['id']}.json"
                path.write_text(
                    json.dumps(profile["runtime_profile"]), encoding="utf-8")
                expected[profile["id"]] = {
                    "dim": profile["runtime_profile"]["dim"],
                }
            env = {
                key: value for key, value in os.environ.items()
                if not key.startswith("AGREP_")
            }
            env.update({
                "AGREP_DATA_DIR": str(root / "data"),
                "AGREP_DATA_DIR_SOURCE": "test",
                "AGREP_HOME": str(root / "home"),
                "AGREP_NO_DAEMON": "1",
                "PYTHONPATH": str(ROOT / "py"),
            })
            result = subprocess.run(
                [sys.executable, "-c", _PROFILE_VALIDATOR_SCRIPT, str(root)],
                cwd=ROOT, env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=60,
                **self.selection._popen_platform_kwargs(),
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        try:
            observed = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            self.fail(f"runtime validator emitted invalid JSON: {exc}: {result.stdout!r}")
        self.assertEqual(observed, expected)

    def test_calibration_grid_uses_inclusive_admission_and_strict_strong_gap(self) -> None:
        observations = [
            {"kind": "real", "scores": [0.85 + index / 1000]}
            for index in range(7)
        ] + [
            {"kind": "gibberish", "scores": [score]}
            for score in (0.82, 0.81, 0.80, 0.79)
        ]
        result = self.selection.calibrate_scores(observations)
        self.assertEqual(result["floor"], 0.791)
        self.assertEqual(result["strong"], 0.821)
        self.assertEqual(result["gap"], 0.03)
        floor = next(row for row in result["rows_admitted"]
                     if row["threshold"] == 0.791)
        self.assertEqual(floor["gibberish_rows"], 3)
        self.assertEqual(floor["real_queries"], 7)

    def test_calibration_refuses_a_real_query_without_any_neighbor(self) -> None:
        observations = [
            {"kind": "real", "scores": [0.9]} for _ in range(6)
        ] + [{"kind": "real", "scores": []}] + [
            {"kind": "gibberish", "scores": [0.1]} for _ in range(4)
        ]
        with self.assertRaisesRegex(self.selection.HarnessError, "every calibration"):
            self.selection.calibrate_scores(observations)

    def test_calibration_removes_stale_profile_before_measurement(self) -> None:
        manifest = self.selection.load_manifest()
        profile = next(row for row in manifest["profiles"] if row["baseline"])
        profile_id = profile["id"]
        state = {
            "snapshot": {"digest": "a" * 64},
            "inputs": {"calibration_digest": "b" * 64},
            "profiles": [profile_id],
        }
        calibration = {
            "real": [{"id": "real", "query": "real query"}],
            "gibberish": [{"id": "noise", "query": "qzxv"}],
        }
        with tempfile.TemporaryDirectory() as raw:
            run = Path(raw)
            root = run / "profiles" / profile_id
            root.mkdir(parents=True)
            stale = root / "profile-calibrated.json"
            stale.write_text('{"stale":true}', encoding="utf-8")
            args = argparse.Namespace(
                run_dir=run, profile=[profile_id], timeout=1.0)
            with mock.patch.object(
                    self.selection, "_load_state",
                    return_value=(state, manifest, calibration, [])), \
                    mock.patch.object(
                        self.selection, "_raw_semantic_batch",
                        side_effect=self.selection.HarnessError("measurement failed")), \
                    self.assertRaisesRegex(self.selection.HarnessError, "measurement failed"):
                self.selection.stage_calibrate(args)
            self.assertFalse(stale.exists())

    def test_calibration_without_separation_writes_a_closed_runtime(self) -> None:
        manifest = self.selection.load_manifest()
        profile = next(row for row in manifest["profiles"] if row["baseline"])
        profile_id = profile["id"]
        state = {
            "snapshot": {"digest": "a" * 64},
            "inputs": {"calibration_digest": "b" * 64},
            "profiles": [profile_id],
        }
        calibration = {
            "real": [{"id": "real", "query": "real query"}],
            "gibberish": [{"id": "noise", "query": "qzxv"}],
        }
        raw_rows = [
            {"id": task["id"], "rows": [{"score": 0.5}], "exit_ms": 1.0}
            for tasks in calibration.values() for task in tasks
        ]
        calculated = {
            "floor": None, "strong": None, "gap": -0.1,
            "real_queries": 1, "gibberish_queries": 1,
            "lowest_real_top1": 0.4, "highest_gibberish_top1": 0.5,
            "rows_admitted": [],
        }
        with tempfile.TemporaryDirectory() as raw:
            run = Path(raw)
            root = run / "profiles" / profile_id
            root.mkdir(parents=True)
            args = argparse.Namespace(
                run_dir=run, profile=[profile_id], timeout=1.0)
            with mock.patch.object(
                    self.selection, "_load_state",
                    return_value=(state, manifest, calibration, [])), \
                    mock.patch.object(
                        self.selection, "_raw_semantic_batch",
                        return_value=({}, raw_rows)), \
                    mock.patch.object(
                        self.selection, "calibrate_scores", return_value=calculated), \
                    mock.patch("builtins.print"):
                self.assertEqual(self.selection.stage_calibrate(args), 0)
            report = self.selection._read_json(root / "calibration.json")
            runtime = self.selection._read_json(root / "profile-calibrated.json")
            self.assertFalse(report["calibration_usable"])
            self.assertEqual(
                runtime["search_bands"], report["effective_search_bands"])
            self.assertEqual(runtime["search_bands"]["floor"],
                             self.selection.CLOSED_SEARCH_BAND)

    def test_result_metrics_keep_unseparated_candidate_measurable(self) -> None:
        profile = next(row for row in self.selection.load_manifest()["profiles"]
                       if row["baseline"])
        calibration = {
            "calibration": {"floor": None, "strong": None, "gap": -0.1},
            "calibration_usable": False,
            "effective_search_bands": {"floor": 1.0, "strong": 1.0},
        }
        quality = {"scores": {
            "semantic": {"correct": 0, "total": 20},
            "hybrid": {"correct": 0, "total": 20},
        }}
        performance = {
            "full_rebuild": {"dedup_effective_rows_per_s": 100.0},
            "index_projection_10m": {"q8_bytes": 3_880_000_064},
            "query_exit_ms": {"semantic": {"warm_median": 80.0}},
            "semantic_resources": {
                "semantic_resident_rss_mib": 200.0,
                "semantic_peak_rss_mib": 250.0,
            },
            "model_graph_bytes": 52_000_000,
            "full_rebuild_projection_10m": {
                "modeled_components_sum_s": 1000.0,
            },
            "current_layout_top_up_projection_10m": {
                "modeled_components_sum_s": 40.0,
            },
            "accelerator_used": False,
            "benchmark_environment_digest": "b" * 64,
        }
        state = {
            "inputs": {"quality_digest": "a" * 64, "code_digest": "c" * 64},
            "prepare": {profile["id"]: {}},
        }
        with mock.patch.object(
                self.selection, "_validated_calibrated_profile",
                return_value=(calibration, Path("profile.json"), "digest")), \
                mock.patch.object(
                    self.selection, "_read_profile_artifact",
                    side_effect=[quality, performance]), \
                mock.patch.object(
                    self.selection, "validate_quality_artifact_protocol",
                    return_value={}), \
                mock.patch.object(
                    self.selection, "validate_performance_artifact_protocol",
                    return_value={"top_up_rows": 1000}):
            metrics = self.selection._result_metrics(profile, Path("root"), state)
        self.assertFalse(metrics["calibration_usable"])
        self.assertEqual(metrics["calibration_floor"], 1.0)
        self.assertEqual(metrics["calibration_gap"], -0.1)

    def test_adoption_gates_are_independent_and_all_required(self) -> None:
        baseline = {
            "semantic_correct": 14, "hybrid_correct": 19, "hybrid_total": 20,
            "calibration_gap": 0.02, "dedup_effective_rows_per_s": 200.0,
            "q8_10m_bytes": 3_880_000_064, "adoption_eligible": True,
            "calibration_usable": True, "physical_cpu_support": True,
        }
        candidate = {
            "semantic_correct": 15, "hybrid_correct": 19, "hybrid_total": 20,
            "calibration_gap": 0.04, "dedup_effective_rows_per_s": 100.0,
            "q8_10m_bytes": 7_760_000_128, "adoption_eligible": True,
            "calibration_usable": True, "physical_cpu_support": True,
        }
        passed = self.selection.adoption_decision(baseline, candidate)
        self.assertTrue(passed["adopt"])
        for check_name in passed["checks"]:
            with self.subTest(check_name=check_name):
                changed = dict(candidate)
                if check_name == "semantic_strictly_better":
                    changed["semantic_correct"] = 14
                elif check_name == "hybrid_at_least_19_of_20":
                    changed["hybrid_correct"] = 18
                elif check_name == "calibration_gap_at_least_2x":
                    changed["calibration_gap"] = 0.039
                elif check_name == "throughput_at_least_half":
                    changed["dedup_effective_rows_per_s"] = 99.9
                elif check_name == "q8_10m_at_most_2x":
                    changed["q8_10m_bytes"] += 1
                elif check_name == "usable_calibration_bands":
                    changed["calibration_usable"] = False
                elif check_name == "physical_mac_and_windows_cpu":
                    changed["physical_cpu_support"] = False
                else:
                    changed["adoption_eligible"] = False
                result = self.selection.adoption_decision(baseline, changed)
                self.assertFalse(result["adopt"])
                self.assertFalse(result["checks"][check_name])
        zero_baseline = dict(baseline, calibration_gap=0.0)
        separated = dict(candidate, calibration_gap=0.1)
        result = self.selection.adoption_decision(zero_baseline, separated)
        self.assertFalse(result["checks"]["calibration_gap_at_least_2x"])
        blind_reject = self.selection.adoption_decision(
            baseline, candidate,
            baseline_blind={"points": 30, "possible": 40},
            candidate_blind={"points": 29, "possible": 40},
        )
        self.assertFalse(blind_reject["adopt"])
        self.assertFalse(blind_reject["checks"]["blind_not_worse_than_baseline"])
        blind_pass = self.selection.adoption_decision(
            baseline, candidate,
            baseline_blind={"points": 30, "possible": 40},
            candidate_blind={"points": 30, "possible": 40},
        )
        self.assertTrue(blind_pass["checks"]["blind_not_worse_than_baseline"])

    def test_physical_platform_support_needs_mac_and_windows_per_profile(self) -> None:
        profiles = ["baseline", "candidate"]
        cpu = {
            "requested_provider": "CPUExecutionProvider",
            "session_providers": ["CPUExecutionProvider"],
        }
        mac_only = {
            "platforms": {
                "darwin-arm64": {"profiles": {
                    "baseline": cpu, "candidate": cpu,
                }},
            },
        }
        support = self.selection._physical_platform_support(mac_only, profiles)
        self.assertFalse(support["baseline"]["complete"])
        self.assertEqual(support["candidate"]["missing"], ["win32-x86_64"])
        mac_only["platforms"]["win32-x86_64"] = {
            "profiles": {"baseline": cpu, "candidate": cpu},
        }
        support = self.selection._physical_platform_support(mac_only, profiles)
        self.assertTrue(support["baseline"]["complete"])
        self.assertTrue(support["candidate"]["complete"])

    def test_physical_platform_support_is_profile_specific(self) -> None:
        cpu = {
            "requested_provider": "CPUExecutionProvider",
            "session_providers": ["CPUExecutionProvider"],
        }
        bundle = {
            "platforms": {
                "darwin-arm64": {"profiles": {
                    "baseline": cpu, "candidate": cpu,
                }},
                "win32-x86_64": {"profiles": {"baseline": cpu}},
            },
        }
        support = self.selection._physical_platform_support(
            bundle, ["baseline", "candidate"])
        self.assertTrue(support["baseline"]["complete"])
        self.assertFalse(support["candidate"]["complete"])

    def test_baseline_quality_gates_but_historical_bands_are_reference_only(self) -> None:
        metrics = {
            "semantic_correct": 14, "semantic_total": 20,
            "hybrid_correct": 19, "hybrid_total": 20,
            "calibration_usable": True,
            "calibration_floor": 0.801, "calibration_strong": 0.812,
        }
        result = self.selection.baseline_reproduction(metrics)
        self.assertTrue(result["pass"])
        self.assertFalse(all(result["historical_policy_reference"].values()))
        metrics["semantic_correct"] = 13
        self.assertFalse(self.selection.baseline_reproduction(metrics)["pass"])

    def test_projection_records_each_materialized_vector_width(self) -> None:
        result = self.selection.index_projection(384)
        self.assertEqual(result["q8_bytes"], 3_880_000_064)
        self.assertEqual(result["group_bytes"], 40_000_064)
        self.assertEqual(result["exact_f16_bytes"], 7_680_000_000)
        self.assertEqual(result["q8_plus_exact_f16_bytes"], 11_560_000_064)
        self.assertEqual(
            result["materialized_q8_group_f16_bytes"], 11_600_000_128)

    def test_quality_target_requires_session_and_turn_in_hit_or_window(self) -> None:
        hits = [
            {"session": "0199aaaa-deadbeef", "turn": 5, "window": []},
            {"session": "other", "turn": 1, "window": [
                {"session": "0199aaaa-deadbeef", "turn": 12},
            ]},
        ]
        self.assertEqual(
            self.selection._expected_rank(hits, ["0199aaaa:12"]), 2)
        self.assertIsNone(
            self.selection._expected_rank(hits, ["0199aaaa:13"]))

    def test_prepare_is_offline_by_default(self) -> None:
        parsed = self.selection._parser().parse_args(["prepare"])
        self.assertFalse(parsed.allow_download)
        self.assertFalse(parsed.no_embed)

    def test_snapshot_reuse_refuses_content_drift(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            frozen = root / "frozen"
            source.mkdir()
            (source / "messages.jsonl").write_text(
                '{"id":"a","text":"a"}\n', encoding="utf-8")
            (source / "sessions.jsonl").write_text(
                '{"session":"a"}\n', encoding="utf-8")
            self._own_empty_derived_family(source)
            self.selection._copy_snapshot(source, frozen)
            (source / "messages.jsonl").write_text(
                '{"id":"b","text":"b"}\n', encoding="utf-8")
            with self.assertRaisesRegex(self.selection.HarnessError, "differs"):
                self.selection._copy_snapshot(source, frozen)

    def test_bind_rejects_a_stale_stamp_without_laundering_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "source"
            source.mkdir()
            (source / "messages.jsonl").write_text(
                '{"id":"codex:s:1","session":"s","turn":1,"text":"one"}\n',
                encoding="utf-8")
            (source / "sessions.jsonl").write_text(
                '{"session":"s"}\n', encoding="utf-8")
            self._own_empty_derived_family(source)
            self.selection._reconcile_source_corpus(source, 30.0)
            db = sqlite3.connect(source / "corpus.db")
            old_stamp = db.execute(
                "SELECT value FROM meta WHERE key='stamp'").fetchone()[0]
            db.close()
            (source / "messages.jsonl").write_text(
                '{"id":"codex:s:1","session":"s","turn":1,"text":"changed"}\n',
                encoding="utf-8")
            with self.assertRaisesRegex(
                    self.selection.HarnessError, "does not exactly match"):
                self.selection._bind_corpus(source, {}, 30.0)
            db = sqlite3.connect(source / "corpus.db")
            observed_stamp = db.execute(
                "SELECT value FROM meta WHERE key='stamp'").fetchone()[0]
            db.close()
            self.assertEqual(observed_stamp, old_stamp)

    def test_snapshot_reconciles_stale_source_before_copying(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            source.mkdir()
            (source / "messages.jsonl").write_text(
                '{"id":"codex:s:1","session":"s","turn":1,"text":"one"}\n',
                encoding="utf-8")
            (source / "sessions.jsonl").write_text(
                '{"session":"s"}\n', encoding="utf-8")
            self._own_empty_derived_family(source)
            self.selection._reconcile_source_corpus(source, 30.0)
            (source / "messages.jsonl").write_text(
                '{"id":"codex:s:1","session":"s","turn":1,"text":"one"}\n'
                '{"id":"codex:s:2","session":"s","turn":2,"text":"two"}\n',
                encoding="utf-8")
            frozen = root / "frozen"
            snapshot = self.selection._copy_snapshot(source, frozen, 30.0)
            corpus = self.selection._bind_corpus(frozen, {}, 30.0)
            self.assertEqual(snapshot["message_rows"], 2)
            self.assertEqual(corpus["rows"], 2)

    def test_snapshot_rejects_a_source_race(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            source.mkdir()
            messages = source / "messages.jsonl"
            messages.write_text(
                '{"id":"codex:s:1","session":"s","turn":1,"text":"one"}\n',
                encoding="utf-8")
            (source / "sessions.jsonl").write_text(
                '{"session":"s"}\n', encoding="utf-8")
            self._own_empty_derived_family(source)
            original = self.selection._snapshot_inventory
            calls = 0

            def moving_inventory(path: Path) -> dict:
                nonlocal calls
                if path == source:
                    calls += 1
                    if calls == 2:
                        messages.write_text(
                            '{"id":"codex:s:1","session":"s","turn":1,'
                            '"text":"moved during copy"}\n', encoding="utf-8")
                return original(path)

            with mock.patch.object(
                    self.selection, "_snapshot_inventory",
                    side_effect=moving_inventory), self.assertRaisesRegex(
                        self.selection.HarnessError, "moved while it was being frozen"):
                self.selection._copy_snapshot(source, root / "frozen", 30.0)
            self.assertFalse((root / "frozen").exists())

    def test_snapshot_reuse_rejects_an_incoherent_frozen_db(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            source.mkdir()
            (source / "messages.jsonl").write_text(
                '{"id":"codex:s:1","session":"s","turn":1,"text":"one"}\n',
                encoding="utf-8")
            (source / "sessions.jsonl").write_text(
                '{"session":"s"}\n', encoding="utf-8")
            self._own_empty_derived_family(source)
            frozen = root / "frozen"
            self.selection._copy_snapshot(source, frozen, 30.0)
            db = sqlite3.connect(frozen / "corpus.db")
            db.execute("UPDATE meta SET value='stale' WHERE key='stamp'")
            db.commit()
            db.close()
            with self.assertRaisesRegex(
                    self.selection.HarnessError, "does not exactly match"):
                self.selection._copy_snapshot(source, frozen, 30.0)
            db = sqlite3.connect(frozen / "corpus.db")
            stamp = db.execute(
                "SELECT value FROM meta WHERE key='stamp'").fetchone()[0]
            db.close()
            self.assertEqual(stamp, "stale")

    def test_binding_a_coherent_snapshot_is_byte_stable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            source.mkdir()
            (source / "messages.jsonl").write_text(
                '{"id":"codex:s:1","session":"s","turn":1,"text":"one"}\n',
                encoding="utf-8")
            (source / "sessions.jsonl").write_text(
                '{"session":"s"}\n', encoding="utf-8")
            self._own_empty_derived_family(source)
            frozen = root / "frozen"
            self.selection._copy_snapshot(source, frozen, 30.0)
            path = frozen / "corpus.db"
            before = (self.selection._file_digest(path), path.stat().st_mtime_ns)
            corpus = self.selection._bind_corpus(frozen, {}, 30.0)
            after = (self.selection._file_digest(path), path.stat().st_mtime_ns)
            self.assertEqual(corpus["rows"], 1)
            self.assertEqual(after, before)

    def test_legacy_event_mutation_invalidates_snapshot_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            events = source / "events"
            events.mkdir(parents=True)
            (source / "messages.jsonl").write_text(
                '{"id":"codex:s:1","agent":"codex","session":"s",'
                '"turn":1,"ts":100,"text":"one"}\n', encoding="utf-8")
            (source / "sessions.jsonl").write_text(
                '{"session":"s"}\n', encoding="utf-8")
            event = events / "codex-s.jsonl"
            event.write_text(
                '{"ts":150,"kind":"tool","name":"first"}\n', encoding="utf-8")
            self._own_empty_derived_family(source)
            frozen = root / "frozen"
            self.selection._copy_snapshot(source, frozen, 30.0)
            corpus = self.selection._bind_corpus(frozen, {}, 30.0)
            self.assertEqual(corpus["rows"] - corpus["prose_rows"], 1)
            event.write_text(
                '{"ts":150,"kind":"tool","name":"changed"}\n', encoding="utf-8")
            with self.assertRaisesRegex(self.selection.HarnessError, "differs"):
                self.selection._copy_snapshot(source, frozen, 30.0)

    def test_performance_clone_refresh_preserves_legacy_tool_rows(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            events = source / "events"
            events.mkdir(parents=True)
            messages = source / "messages.jsonl"
            messages.write_text(
                '{"id":"codex:s:1","agent":"codex","session":"s",'
                '"turn":1,"ts":100,"text":"one"}\n', encoding="utf-8")
            (source / "sessions.jsonl").write_text(
                '{"session":"s"}\n', encoding="utf-8")
            (events / "codex-s.jsonl").write_text(
                '{"ts":150,"kind":"tool","name":"kept-tool"}\n', encoding="utf-8")
            self._own_empty_derived_family(source)
            frozen = root / "frozen"
            snapshot = self.selection._copy_snapshot(source, frozen, 30.0)
            clone = root / "clone"
            self.selection._copy_profile_data(frozen, clone, snapshot)
            before = self.selection._bind_corpus(clone, {}, 30.0)
            messages_clone = clone / "messages.jsonl"
            messages_clone.write_text(
                messages_clone.read_text(encoding="utf-8")
                + '{"id":"codex:s:2","agent":"codex","session":"s",'
                '"turn":2,"ts":200,"text":"two"}\n', encoding="utf-8")
            after = self.selection._reconcile_source_corpus(clone, 30.0)
            self.assertEqual(before["rows"] - before["prose_rows"], 1)
            self.assertEqual(after["rows"] - after["prose_rows"], 1)
            self.assertEqual(after["prose_rows"], 2)

    def test_current_event_store_stays_coherent_through_profile_copy(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            events = source / "events"
            events.mkdir(parents=True)
            (source / "messages.jsonl").write_text(
                '{"id":"codex:s:1","agent":"codex","session":"s",'
                '"turn":1,"ts":100,"text":"one"}\n', encoding="utf-8")
            (source / "sessions.jsonl").write_text(
                '{"session":"s"}\n', encoding="utf-8")
            payload = b'{"ts":150,"kind":"tool","name":"current-tool"}\n'
            store = sqlite3.connect(events / ".store.sqlite3")
            store.executescript(
                "CREATE TABLE event_sessions (name TEXT PRIMARY KEY,agent TEXT NOT NULL,"
                "session TEXT NOT NULL,hash INTEGER NOT NULL,n_events INTEGER NOT NULL,"
                "payload BLOB NOT NULL,digest BLOB NOT NULL,stats BLOB NOT NULL) WITHOUT ROWID;"
                "CREATE TABLE event_meta (key TEXT PRIMARY KEY,value BLOB NOT NULL) WITHOUT ROWID;")
            store.execute(
                "INSERT INTO event_sessions VALUES(?,?,?,?,?,?,?,?)",
                ("codex-s.jsonl", "codex", "s", 0, 1, payload,
                 hashlib.md5(payload).digest(), b"{}"))
            store.execute("INSERT INTO event_meta VALUES('generation',?)", (b"g1",))
            store.commit()
            store.close()
            (events / ".generation").write_bytes(b"g1")
            self._own_empty_derived_family(source)
            frozen = root / "frozen"
            snapshot = self.selection._copy_snapshot(source, frozen, 30.0)
            clone = root / "clone"
            self.selection._copy_profile_data(frozen, clone, snapshot)
            corpus = self.selection._bind_corpus(clone, {}, 30.0)
            self.assertEqual(snapshot["event_mode"], "current")
            self.assertEqual(corpus["events"]["rows"], 1)
            self.assertEqual(corpus["rows"] - corpus["prose_rows"], 1)

    def test_quality_target_preflight_rejects_absent_turn(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db = sqlite3.connect(root / "corpus.db")
            db.execute("CREATE TABLE msgs(session TEXT,turn INTEGER,who TEXT)")
            db.execute("INSERT INTO msgs VALUES('abc-full',1,'user')")
            db.commit()
            db.close()
            with self.assertRaisesRegex(self.selection.HarnessError, "absent"):
                self.selection._preflight_quality_targets(
                    root, [{"expected": ["abc:2"]}])

    def test_quality_target_preflight_rejects_ambiguous_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db = sqlite3.connect(root / "corpus.db")
            db.execute("CREATE TABLE msgs(session TEXT,turn INTEGER,who TEXT)")
            db.executemany("INSERT INTO msgs VALUES(?,?,?)", [
                ("abc-one", 1, "user"), ("abc-two", 1, "agent")])
            db.commit()
            db.close()
            with self.assertRaisesRegex(self.selection.HarnessError, "2 sessions"):
                self.selection._preflight_quality_targets(
                    root, [{"expected": ["abc:1"]}])

    def test_quality_target_preflight_accepts_one_prose_handle(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db = sqlite3.connect(root / "corpus.db")
            db.execute("CREATE TABLE msgs(session TEXT,turn INTEGER,who TEXT)")
            db.executemany("INSERT INTO msgs VALUES(?,?,?)", [
                ("abc-full", 1, "user"), ("abc-full", 1, "agent")])
            db.commit()
            db.close()
            self.selection._preflight_quality_targets(
                root, [{"expected": ["abc:1"]}])

    def test_snapshot_sqlite_uri_handles_reserved_path_characters(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source #1?"
            frozen = root / "frozen"
            source.mkdir()
            (source / "messages.jsonl").write_text(
                '{"id":"a","text":"a"}\n', encoding="utf-8")
            (source / "sessions.jsonl").write_text(
                '{"session":"a"}\n', encoding="utf-8")
            self._own_empty_derived_family(source)
            copied = self.selection._copy_snapshot(source, frozen)
            self.assertEqual(copied["message_rows"], 1)
            self.assertTrue((frozen / "corpus.db").is_file())

    def test_snapshot_row_total_includes_reply_vectors_and_corpus_db(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "messages.jsonl").write_text(
                '{"id":"a","text":"a"}\n{"id":"b","text":"b"}\n', encoding="utf-8")
            (root / "replies.jsonl").write_text(
                '{"id":"a","reply":"x"}\n', encoding="utf-8")
            (root / "sessions.jsonl").write_text(
                '{"session":"a"}\n', encoding="utf-8")
            sqlite3.connect(root / "corpus.db").close()
            snapshot = self.selection._snapshot_inventory(root)
        self.assertEqual(snapshot["message_rows"], 2)
        self.assertEqual(snapshot["reply_rows"], 1)
        self.assertEqual(snapshot["rows"], 3)
        self.assertIn("corpus.db", {row["path"] for row in snapshot["files"]})

    def test_blind_digest_changes_if_review_content_changes(self) -> None:
        packet = {
            "schema": 1, "snapshot_digest": "a" * 64,
            "tasks": [{"id": "x", "options": [{"label": "A", "hits": []}]}],
        }
        first = self.selection._blind_digest(packet)
        packet["tasks"][0]["options"][0]["hits"].append({"evidence": "changed"})
        self.assertNotEqual(first, self.selection._blind_digest(packet))

    def test_closed_calibration_band_is_explicit_and_non_stale(self) -> None:
        bands, usable = self.selection._effective_calibration({
            "floor": None, "strong": None,
        })
        self.assertFalse(usable)
        self.assertEqual(bands, {
            "floor": self.selection.CLOSED_SEARCH_BAND,
            "strong": self.selection.CLOSED_SEARCH_BAND,
        })

    def test_top_up_samples_frozen_messages_and_replies_deterministically(self) -> None:
        digest = "a" * 64

        def make_data(root: Path) -> Path:
            root.mkdir()
            (root / "messages.jsonl").write_text(
                '{"id":"m1","text":"tiny source"}\n'
                '{"id":"m2","text":"a much longer source message from reality"}\n',
                encoding="utf-8")
            (root / "replies.jsonl").write_text(
                '{"id":"r1","reply":"reply source alpha"}\n'
                '{"id":"r2","reply":"reply source beta with code_name"}\n',
                encoding="utf-8")
            (root / "sessions.jsonl").write_text("", encoding="utf-8")
            return root

        with tempfile.TemporaryDirectory() as raw:
            first = make_data(Path(raw) / "first")
            second = make_data(Path(raw) / "second")
            first_meta = self.selection._append_top_up(first, 4, digest)
            second_meta = self.selection._append_top_up(second, 4, digest)
            self.assertEqual(first_meta, second_meta)
            rows = [json.loads(line) for line in
                    (first / "messages.jsonl").read_text(encoding="utf-8").splitlines()]
            added = rows[-4:]
            sources = {
                "tiny source", "a much longer source message from reality",
                "reply source alpha", "reply source beta with code_name",
            }
            self.assertEqual(first_meta["source_rows"], 4)
            self.assertEqual(len({row["text"].splitlines()[0] for row in added}), 4)
            self.assertEqual({row["text"].splitlines()[0] for row in added}, sources)
            self.assertEqual(len({row["id"] for row in added}), 4)

    def test_resource_probe_requires_a_real_measurement(self) -> None:
        module = mock.Mock()
        module._measure_semantic.return_value = {
            "semantic_status": "measured", "semantic_resident_rss_mib": 123.0,
        }
        with mock.patch.object(self.selection, "_RESOURCE_MODULE", module):
            measured = self.selection._measure_semantic_resources(
                Path("data"), {}, Path("models"))
        self.assertEqual(measured["semantic_resident_rss_mib"], 123.0)
        module._measure_semantic.assert_called_once_with(
            Path("data").resolve(), {}, Path("models").resolve(), sample_s=0.5)
        module._measure_semantic.return_value = {
            "semantic_status": "skipped", "semantic_skip_reason": "missing",
        }
        with mock.patch.object(self.selection, "_RESOURCE_MODULE", module), \
                self.assertRaisesRegex(self.selection.HarnessError, "was skipped"):
            self.selection._measure_semantic_resources(Path("data"), {}, Path("models"))

    def test_quality_stops_worker_when_a_query_fails(self) -> None:
        manifest = self.selection.load_manifest()
        profile = next(row for row in manifest["profiles"] if row["baseline"])
        state = {"profiles": [profile["id"]]}
        args = argparse.Namespace(
            run_dir=Path("run"), profile=[profile["id"]], timeout=1.0, hits=3)
        stopper = mock.Mock()
        with mock.patch.object(
                self.selection, "_load_state",
                return_value=(state, manifest, {}, [{"id": "task"}])), \
                mock.patch.object(
                self.selection, "_validated_calibrated_profile",
                return_value=({}, Path("profile.json"), "digest")), \
                mock.patch.object(self.selection, "_profile_env", return_value={}), \
                mock.patch.object(
                    self.selection, "_recall", side_effect=RuntimeError("query failed")), \
                mock.patch.object(self.selection, "_stop_worker", stopper), \
                self.assertRaisesRegex(RuntimeError, "query failed"):
            self.selection.stage_quality(args)
        stopper.assert_called_once_with({})

    def test_performance_stops_worker_when_a_query_fails(self) -> None:
        manifest = self.selection.load_manifest()
        profile = next(row for row in manifest["profiles"] if row["baseline"])
        state = {
            "profiles": [profile["id"]],
            "prepare": {profile["id"]: {
                "embed_skipped": False, "embedding_bundle": {"schema": 1},
                "embed_stats": {"dedup_effective_rows_per_s": 1.0},
                "embed": {"wall_ms": 1.0},
                "benchmark_environment": {"fixture": True},
                "benchmark_environment_digest": "a" * 64,
                "session": {
                    "actual_providers": ["CPUExecutionProvider"],
                    "semantic_threads": 4,
                },
            }},
        }
        calibration = {"real": [
            {"id": "task-1", "query": "query one"},
            {"id": "task-2", "query": "query two"},
        ]}
        args = argparse.Namespace(
            run_dir=Path("run"), profile=[profile["id"]], timeout=1.0,
            queries=2, top_up_rows=1000)
        stopper = mock.Mock()
        with mock.patch.object(
                self.selection, "_load_state",
                return_value=(state, manifest, calibration, [])), \
                mock.patch.object(
                self.selection, "_validated_calibrated_profile",
                return_value=({}, Path("profile.json"), "digest")), \
                mock.patch.object(
                    self.selection, "_validate_embedding_bundle",
                    return_value={"schema": 1}), \
                mock.patch.object(
                    self.selection, "validate_benchmark_environment"), \
                mock.patch.object(
                    self.selection, "benchmark_environment",
                    return_value={"fixture": True}), \
                mock.patch.object(self.selection, "_profile_env", return_value={}), \
                mock.patch.object(
                    self.selection, "_recall", side_effect=RuntimeError("query failed")), \
                mock.patch.object(self.selection, "_stop_worker", stopper), \
                self.assertRaisesRegex(RuntimeError, "query failed"):
            self.selection.stage_performance(args)
        self.assertEqual(stopper.call_count, 2)

    def test_profile_environment_isolated_and_uses_absolute_profile(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run = Path(raw).resolve()
            profile = run / "profile.json"
            env = self.selection._profile_env(run, "candidate", profile)
        self.assertEqual(env["AGREP_DATA_DIR"], str(run / "profiles/candidate/data"))
        self.assertEqual(env["AGREP_MODEL_DIR"], str(run / "profiles/candidate/models"))
        self.assertEqual(env["AGREP_BENCH_EMBED_PROFILE"], str(profile))
        relative = Path("relative-campaign")
        env = self.selection._profile_env(
            relative, "candidate", relative / "profile.json")
        self.assertEqual(
            env["AGREP_BENCH_EMBED_PROFILE"],
            str((relative / "profile.json").resolve()))
        self.assertTrue(Path(env["AGREP_DATA_DIR"]).is_absolute())
        self.assertTrue(Path(env["AGREP_MODEL_DIR"]).is_absolute())

    def test_duplicate_profile_cli_selections_fail_closed(self) -> None:
        manifest = self.selection.load_manifest()
        profile_id = next(row["id"] for row in manifest["profiles"]
                          if row["status"] == "runnable")
        with self.assertRaisesRegex(
                self.selection.HarnessError, "duplicate profile selections"):
            self.selection._profiles(manifest, [profile_id, profile_id])

    def test_benchmark_environment_is_strict_and_provider_bound(self) -> None:
        environment = self.selection.benchmark_environment(
            requested_provider="CPUExecutionProvider",
            actual_providers=["CPUExecutionProvider"], semantic_threads=4,
            power_policy="host-default-uncontrolled")
        self.assertEqual(
            self.selection.validate_benchmark_environment(environment), environment)
        self.assertIsInstance(environment["host"]["logical_cores"], int)
        self.assertTrue(environment["host"]["cpu_identity"])
        changed = json.loads(json.dumps(environment))
        changed["execution"]["actual_providers"] = ["CoreMLExecutionProvider"]
        with self.assertRaisesRegex(
                self.selection.HarnessError, "requested provider was not active"):
            self.selection.validate_benchmark_environment(changed)

    def test_stage_protocols_bind_tasks_counts_and_absolute_paths(self) -> None:
        quality_tasks = [{"id": "task", "query": "query"}]
        quality = self.selection.quality_protocol(quality_tasks, 3)
        self.assertEqual(
            self.selection.validate_quality_protocol(
                quality, task_digest=self.selection._object_digest(quality_tasks),
                hits=3),
            quality)
        changed = json.loads(json.dumps(quality))
        changed["hits"] = 4
        with self.assertRaisesRegex(
                self.selection.HarnessError, "task or hit binding"):
            self.selection.validate_quality_protocol(changed, hits=3)
        with tempfile.TemporaryDirectory() as raw:
            run = Path(raw).resolve()
            args = argparse.Namespace(run_dir=run, top_up_rows=321)
            tasks = [
                {"id": "one", "query": "first"},
                {"id": "two", "query": "second"},
            ]
            protocol = self.selection.performance_protocol(
                args, tasks, run / "profiles/model")
            self.assertEqual(protocol["query_count"], 2)
            self.assertEqual(protocol["top_up_rows"], 321)
            self.assertTrue(all(Path(path).is_absolute()
                                for path in protocol["paths"].values()))
            protocol["query_count"] = 1
            with self.assertRaisesRegex(
                    self.selection.HarnessError, "query protocol"):
                self.selection.validate_performance_protocol(protocol)

    def test_main_resolves_campaign_paths_once_before_stage_execution(self) -> None:
        captured = {}

        def run(args: argparse.Namespace) -> int:
            captured.update({
                "run_dir": args.run_dir,
                "manifest": args.manifest,
                "quality": args.quality_tasks,
            })
            return 0

        with mock.patch.object(self.selection, "stage_quality", side_effect=run):
            self.assertEqual(self.selection.main([
                "quality", "--run-dir", "relative-run",
                "--manifest", "relative-manifest.json",
                "--quality-tasks", "relative-quality.json",
            ]), 0)
        self.assertTrue(all(path.is_absolute() for path in captured.values()))
    def test_stage_state_rejects_moved_input_digest(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            snapshot = root / "snapshot"
            snapshot.mkdir()
            (snapshot / "messages.jsonl").write_text(
                '{"id":"a","text":"a"}\n', encoding="utf-8")
            (snapshot / "sessions.jsonl").write_text(
                '{"session":"a"}\n', encoding="utf-8")
            sqlite3.connect(snapshot / "corpus.db").close()
            state = {
                "schema": 1,
                "campaign_contract_version": self.selection.CAMPAIGN_CONTRACT_VERSION,
                "inputs": {"manifest_digest": "wrong"},
                "snapshot": self.selection._snapshot_inventory(snapshot),
            }
            (root / "state.json").write_text(json.dumps(state), encoding="utf-8")
            args = argparse.Namespace(
                manifest=self.selection.DEFAULT_MANIFEST,
                calibration_tasks=self.selection.DEFAULT_CALIBRATION,
                quality_tasks=self.selection.QUALITY_EXAMPLE,
                run_dir=root, expect_snapshot=None,
            )
            with self.assertRaisesRegex(self.selection.HarnessError, "digest moved"):
                self.selection._load_state(args)

    def test_campaign_contract_requires_an_exact_integer(self) -> None:
        self.assertTrue(self.selection._current_campaign_contract(2))
        for value in (2.0, True, "2", None):
            with self.subTest(value=value):
                self.assertFalse(self.selection._current_campaign_contract(value))
        quality = {"campaign_contract_version": 2.0}
        with self.assertRaisesRegex(
                self.selection.HarnessError, "campaign contract is stale"):
            self.selection.validate_quality_artifact_protocol(quality)
        performance = {"campaign_contract_version": 2.0}
        with self.assertRaisesRegex(
                self.selection.HarnessError, "campaign contract is stale"):
            self.selection.validate_performance_artifact_protocol(performance, {})

    def test_report_contract_rejects_every_cross_profile_drift(self) -> None:
        profiles = [{"id": "baseline"}, {"id": "candidate"}]
        task = [{"id": "task", "query": "query"}]
        quality_digest = self.selection._object_digest(task)
        state = {
            "inputs": {
                "quality_digest": quality_digest, "code_digest": "c" * 64},
            "prepare": {"baseline": {}, "candidate": {}},
        }
        quality_protocol = self.selection.quality_protocol(task, 3)

        def artifacts(run: Path) -> dict:
            values = {}
            for profile in profiles:
                profile_id = profile["id"]
                root = run / "profiles" / profile_id
                perf_args = argparse.Namespace(run_dir=run, top_up_rows=1_000)
                performance_protocol = self.selection.performance_protocol(
                    perf_args,
                    [{"id": "one", "query": "one"},
                     {"id": "two", "query": "two"}], root)
                values[(profile_id, "quality.json")] = {
                    "campaign_contract_version": 2,
                    "task_digest": quality_digest,
                    "protocol": json.loads(json.dumps(quality_protocol)),
                    "protocol_digest": self.selection._object_digest(
                        quality_protocol),
                }
                values[(profile_id, "performance.json")] = {
                    "protocol": performance_protocol,
                    "benchmark_environment": {"fixture": "same"},
                }
            return values

        cases = ("benchmark environments", "quality protocols",
                 "performance protocols")
        with tempfile.TemporaryDirectory() as raw:
            run = Path(raw).resolve()
            for case in cases:
                with self.subTest(case=case):
                    values = artifacts(run)
                    if case == "benchmark environments":
                        values[("candidate", "performance.json")][
                            "benchmark_environment"] = {"fixture": "different"}
                    elif case == "quality protocols":
                        changed = values[("candidate", "quality.json")]
                        changed["protocol"]["hits"] = 4
                        changed["protocol"]["cli"]["output_cap"] = 4
                        changed["protocol_digest"] = self.selection._object_digest(
                            changed["protocol"])
                    else:
                        values[("candidate", "performance.json")]["protocol"][
                            "top_up_rows"] = 2_000

                    def read(_root, name, _state, profile_id, _expected):
                        return values[(profile_id, name)]

                    with mock.patch.object(
                            self.selection, "_validated_calibrated_profile",
                            return_value=({}, Path("profile.json"), "runtime")), \
                            mock.patch.object(
                                self.selection, "_read_profile_artifact",
                                side_effect=read), \
                            mock.patch.object(
                                self.selection, "_file_digest",
                                return_value="f" * 64), \
                            mock.patch.object(
                                self.selection,
                                "validate_performance_artifact_protocol",
                                side_effect=lambda report, _prepared, **_kwargs: (
                                    report["protocol"])), \
                            self.assertRaisesRegex(
                                self.selection.HarnessError, case):
                        self.selection._report_measurement_contract(
                            run, state, profiles)

    def test_stage_report_preflights_contract_before_decisions(self) -> None:
        profiles = [
            {"id": "baseline", "baseline": True},
            {"id": "candidate", "baseline": False},
        ]
        args = argparse.Namespace(
            run_dir=Path("campaign"), profile=[], platform_evidence=None)
        decision = mock.Mock()
        with mock.patch.object(
                self.selection, "_load_state",
                return_value=({}, {}, None, [])), \
                mock.patch.object(
                    self.selection, "_selected_from_state",
                    return_value=profiles), \
                mock.patch.object(
                    self.selection, "_report_measurement_contract",
                    side_effect=self.selection.HarnessError(
                        "selected profiles used different performance protocols")), \
                mock.patch.object(
                    self.selection, "adoption_decision", decision), \
                self.assertRaisesRegex(
                    self.selection.HarnessError, "performance protocols"):
            self.selection.stage_report(args)
        decision.assert_not_called()

    def test_blind_results_are_bound_to_the_current_quality_protocol(self) -> None:
        state = {
            "snapshot": {"digest": "a" * 64},
            "inputs": {"quality_digest": "b" * 64},
        }
        protocol = self.selection.quality_protocol(
            [{"id": "task", "query": "query"}], 3)
        digest = self.selection._object_digest(protocol)
        profiles = ["baseline", "candidate"]
        quality_hashes = {"baseline": "d" * 64, "candidate": "e" * 64}
        quality_artifact_digest = self.selection._object_digest(quality_hashes)
        hidden = {
            "schema": 1, "campaign_contract_version": 2,
            "snapshot_digest": state["snapshot"]["digest"],
            "campaign_inputs": state["inputs"], "profiles": profiles,
            "quality_protocol": protocol, "quality_protocol_digest": digest,
            "quality_artifact_sha256": quality_hashes,
            "quality_artifact_digest": quality_artifact_digest,
            "blind_digest": "c" * 64, "mapping": {},
        }
        scores = {
            "schema": 1, "campaign_contract_version": 2,
            "snapshot_digest": state["snapshot"]["digest"],
            "campaign_inputs": state["inputs"], "profile_ids": profiles,
            "quality_protocol": protocol, "quality_protocol_digest": digest,
            "quality_artifact_sha256": quality_hashes,
            "quality_artifact_digest": quality_artifact_digest,
            "blind_digest": hidden["blind_digest"],
            "profiles": {
                profile_id: {"points": 1, "possible": 2}
                for profile_id in profiles
            },
        }
        with tempfile.TemporaryDirectory() as raw:
            run = Path(raw)
            for target, field, value in (
                    ("hidden", "quality_protocol_digest", "d" * 64),
                    ("scores", "quality_protocol", {"stale": True}),
                    ("scores", "quality_artifact_sha256",
                     {"baseline": "0" * 64, "candidate": "e" * 64}),
                    ("scores", "campaign_contract_version", 2.0)):
                with self.subTest(target=target, field=field):
                    current_hidden = json.loads(json.dumps(hidden))
                    current_scores = json.loads(json.dumps(scores))
                    (current_hidden if target == "hidden" else current_scores)[
                        field] = value
                    (run / "blind-private.json").write_text(
                        json.dumps(current_hidden), encoding="utf-8")
                    (run / "blind-scores.json").write_text(
                        json.dumps(current_scores), encoding="utf-8")
                    with self.assertRaises(self.selection.HarnessError):
                        self.selection._validated_blind_scores(
                            run, state, profiles, 1, protocol, digest,
                            quality_hashes)

    def test_stage_blind_import_rejects_stale_quality_output_hashes(self) -> None:
        profiles = [{"id": "baseline", "baseline": True},
                    {"id": "candidate", "baseline": False}]
        state = {
            "snapshot": {"digest": "a" * 64},
            "inputs": {"quality_digest": "b" * 64},
        }
        protocol = self.selection.quality_protocol(
            [{"id": "task", "query": "query"}], 3)
        digest = self.selection._object_digest(protocol)
        current_hashes = {"baseline": "d" * 64, "candidate": "e" * 64}
        with tempfile.TemporaryDirectory() as raw:
            run = Path(raw)
            hidden = {
                "schema": 1, "campaign_contract_version": 2,
                "snapshot_digest": state["snapshot"]["digest"],
                "campaign_inputs": state["inputs"],
                "profiles": ["baseline", "candidate"],
                "quality_protocol": protocol,
                "quality_protocol_digest": digest,
                "quality_artifact_sha256": {
                    "baseline": "0" * 64, "candidate": "e" * 64},
                "quality_artifact_digest": self.selection._object_digest({
                    "baseline": "0" * 64, "candidate": "e" * 64}),
                "blind_digest": "c" * 64, "mapping": {},
            }
            (run / "blind-private.json").write_text(
                json.dumps(hidden), encoding="utf-8")
            imported = {
                "schema": 1, "campaign_contract_version": 2,
                "snapshot_digest": state["snapshot"]["digest"],
                "blind_digest": hidden["blind_digest"], "judgments": [],
                "quality_protocol": protocol,
                "quality_protocol_digest": digest,
                "quality_artifact_digest": self.selection._object_digest(
                    current_hashes),
            }
            source = run / "scores.json"
            source.write_text(json.dumps(imported), encoding="utf-8")
            args = argparse.Namespace(
                run_dir=run, profile=[], import_scores=source, export=None)
            with mock.patch.object(
                    self.selection, "_load_state",
                    return_value=(state, {}, None, [])), \
                    mock.patch.object(
                        self.selection, "_selected_from_state",
                        return_value=profiles), \
                    mock.patch.object(
                        self.selection, "_selected_quality_contract",
                        return_value=({
                            profile_id: {"content_sha256": content_hash}
                            for profile_id, content_hash in current_hashes.items()
                        }, protocol, digest)), \
                    self.assertRaisesRegex(
                        self.selection.HarnessError, "private mapping"):
                self.selection.stage_blind(args)

    def test_blind_scores_reject_same_protocol_changed_hit_evidence(self) -> None:
        profiles = [{"id": "baseline"}, {"id": "candidate"}]
        tasks = [{"id": "task", "query": "query"}]
        quality_digest = self.selection._object_digest(tasks)
        state = {
            "snapshot": {"digest": "a" * 64},
            "inputs": {"quality_digest": quality_digest},
        }
        protocol = self.selection.quality_protocol(tasks, 3)
        protocol_digest = self.selection._object_digest(protocol)
        with tempfile.TemporaryDirectory() as raw:
            run = Path(raw)
            for profile in profiles:
                report = {
                    "schema": 1, "campaign_contract_version": 2,
                    "profile": profile["id"],
                    "snapshot_digest": state["snapshot"]["digest"],
                    "campaign_inputs": state["inputs"],
                    "task_digest": quality_digest,
                    "runtime_profile_digest": "runtime",
                    "protocol": protocol, "protocol_digest": protocol_digest,
                    "outcomes": [{"hits": [{"evidence": "original"}]}],
                }
                self.selection._write_json(
                    run / "profiles" / profile["id"] / "quality.json", report)
            with mock.patch.object(
                    self.selection, "_validated_calibrated_profile",
                    return_value=({}, Path("profile.json"), "runtime")):
                records, _protocol, _digest = (
                    self.selection._selected_quality_contract(
                        run, state, profiles))
                original_hashes = {
                    profile_id: record["content_sha256"]
                    for profile_id, record in records.items()
                }
                candidate_path = run / "profiles/candidate/quality.json"
                changed = self.selection._read_json(candidate_path)
                changed["outcomes"][0]["hits"][0]["evidence"] = "changed"
                self.selection._write_json(candidate_path, changed)
                current, _protocol, _digest = (
                    self.selection._selected_quality_contract(
                        run, state, profiles))
            current_hashes = {
                profile_id: record["content_sha256"]
                for profile_id, record in current.items()
            }
            self.assertNotEqual(original_hashes, current_hashes)
            hidden = {
                "schema": 1, "campaign_contract_version": 2,
                "snapshot_digest": state["snapshot"]["digest"],
                "campaign_inputs": state["inputs"],
                "profiles": ["baseline", "candidate"],
                "quality_protocol": protocol,
                "quality_protocol_digest": protocol_digest,
                "quality_artifact_sha256": original_hashes,
                "quality_artifact_digest": self.selection._object_digest(
                    original_hashes),
                "blind_digest": "c" * 64, "mapping": {},
            }
            scores = {
                "schema": 1, "campaign_contract_version": 2,
                "snapshot_digest": state["snapshot"]["digest"],
                "campaign_inputs": state["inputs"],
                "profile_ids": ["baseline", "candidate"],
                "quality_protocol": protocol,
                "quality_protocol_digest": protocol_digest,
                "quality_artifact_sha256": original_hashes,
                "quality_artifact_digest": self.selection._object_digest(
                    original_hashes),
                "blind_digest": hidden["blind_digest"],
                "profiles": {
                    profile["id"]: {"points": 1, "possible": 2}
                    for profile in profiles
                },
            }
            self.selection._write_json(run / "blind-private.json", hidden)
            self.selection._write_json(run / "blind-scores.json", scores)
            with self.assertRaisesRegex(
                    self.selection.HarnessError, "private mapping"):
                self.selection._validated_blind_scores(
                    run, state, ["baseline", "candidate"], 1,
                    protocol, protocol_digest, current_hashes)


if __name__ == "__main__":
    unittest.main()
