#!/usr/bin/env python3
"""Collect and merge physical-host evidence for pinned embedding profiles."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / "py"
DEFAULT_MANIFEST = Path(__file__).with_name("embedder_profiles.json")
KIND = "agrep.embedder-platform-evidence"
SCHEMA = 1
TAG_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
BATCH_COSINE_MIN = 0.998
FAILURE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:[A-Za-z]:[\\/]|/|\\\\)[^\s]*")
FAILURE_DETAIL_MAX = 500


def _load_selection():
    path = Path(__file__).resolve().with_name("embedder_selection.py")
    spec = importlib.util.spec_from_file_location("agrep_embedder_platform_selection", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load embedder selection harness")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


selection = _load_selection()
HarnessError = selection.HarnessError


_CHILD_SCRIPT = r"""
import hashlib
import json
import platform
import sys

import numpy as np
import onnxruntime as ort

import embedder

QUERY = "How did we keep the Windows semantic worker current without blocking search?"
DOCUMENTS = [
    "The resident worker watches generation changes and swaps a completed index atomically.",
    "A short unrelated note about CSS spacing in the local explorer.",
    "Retry peakDetect2 after the cache-miss path; don't split apostrophes "
    "or emoji \U0001f469\u200d\U0001f4bb.",
    "\u8bed\u4e49\u641c\u7d22\u5e94\u5728\u540e\u53f0\u5237\u65b0\uff0c"
    "\u4e0d\u963b\u585e\u547d\u4ee4\u9000\u51fa\u3002",
]


def digest(array):
    value = np.ascontiguousarray(array, dtype="<f4")
    return hashlib.sha256(value.tobytes()).hexdigest()


def rounded(value):
    return round(float(value), 9)


emb = embedder.Embedder(download=False)
requested = emb.profile.get("provider", "CPUExecutionProvider")
available = list(ort.get_available_providers())
active = list(emb.sess.get_providers())
if requested not in available or not active or active[0] != requested:
    raise RuntimeError(
        f"requested provider {requested!r} was not activated first; "
        f"available={available!r}, session={active!r}")

query_a = emb.embed_query(QUERY)
documents_a = emb.embed_texts(DOCUMENTS)
documents_solo = np.vstack([emb.embed_texts([text])[0] for text in DOCUMENTS])
edge_documents = emb.embed_texts(["", "   "])
query_b = emb.embed_query(QUERY)
documents_b = emb.embed_texts(DOCUMENTS)
dim = int(emb.profile["dim"])
if query_a.shape != (dim,) or documents_a.shape != (len(DOCUMENTS), dim):
    raise RuntimeError(
        f"vector shape mismatch: query={query_a.shape}, documents={documents_a.shape}, dim={dim}")
if query_a.dtype != np.float32 or documents_a.dtype != np.float32:
    raise RuntimeError(
        f"vector dtype mismatch: query={query_a.dtype}, documents={documents_a.dtype}")
finite = bool(np.isfinite(query_a).all() and np.isfinite(documents_a).all())
query_norm = float(np.linalg.norm(query_a))
document_norms = np.linalg.norm(documents_a, axis=1)
edge_document_norms = np.linalg.norm(edge_documents, axis=1)
mixed_solo_cosines = np.sum(documents_a * documents_solo, axis=1)
norm_error = max(
    abs(query_norm - 1.0),
    max(abs(float(value) - 1.0) for value in document_norms),
    max(abs(float(value) - 1.0) for value in edge_document_norms),
)
query_delta = float(np.max(np.abs(query_a - query_b)))
document_delta = float(np.max(np.abs(documents_a - documents_b)))
deterministic = bool(np.array_equal(query_a, query_b)
                     and np.array_equal(documents_a, documents_b))
mixed_solo_min_cosine = float(mixed_solo_cosines.min())
if (not finite or not np.isfinite(edge_documents).all() or norm_error > 0.00002
        or not deterministic or mixed_solo_min_cosine < 0.998):
    raise RuntimeError(
        f"vector contract failed: finite={finite}, norm_error={norm_error}, "
        f"deterministic={deterministic}, mixed_solo={mixed_solo_min_cosine}")

print(json.dumps({
    "available_providers": available,
    "document_count": len(DOCUMENTS),
    "document_norms": [rounded(value) for value in document_norms],
    "document_repeat_max_abs": rounded(document_delta),
    "documents_sha256": digest(documents_a),
    "documents_shape": list(documents_a.shape),
    "edge_document_norms": [rounded(value) for value in edge_document_norms],
    "edge_documents_sha256": digest(edge_documents),
    "edge_documents_shape": list(edge_documents.shape),
    "finite": finite,
    "machine": platform.machine().lower(),
    "mixed_solo_cosines": [rounded(value) for value in mixed_solo_cosines],
    "mixed_solo_min_cosine": rounded(mixed_solo_min_cosine),
    "normalized_max_error": rounded(norm_error),
    "onnxruntime_device": str(ort.get_device()),
    "onnxruntime_version": str(ort.__version__),
    "profile": emb.profile["id"],
    "python_implementation": platform.python_implementation(),
    "python_version": platform.python_version(),
    "query_norm": rounded(query_norm),
    "query_repeat_max_abs": rounded(query_delta),
    "query_sha256": digest(query_a),
    "query_shape": list(query_a.shape),
    "requested_provider": requested,
    "semantic_threads": int(embedder._thread_budget()),
    "session_providers": active,
    "sys_platform": sys.platform,
    "vector_dtype": "float32",
    "vectors_deterministic": deterministic,
}, sort_keys=True))
"""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _code_digest() -> str:
    paths = {
        Path(__file__).resolve(),
        Path(__file__).resolve().with_name("embedder_selection.py"),
        Path(__file__).resolve().with_name("resources.py"),
        PY / "common.py",
        PY / "embedder.py",
    }
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.relative_to(ROOT).as_posix()):
        digest.update(path.relative_to(ROOT).as_posix().encode("utf-8"))
        digest.update(bytes.fromhex(_file_digest(path)))
    return digest.hexdigest()


def _artifact_contract(profile: dict) -> dict[str, dict[str, int | str]]:
    runtime = profile["runtime_profile"]
    return {
        name: {"size": spec["size"], "sha256": spec["sha256"]}
        for name, spec in sorted(runtime["files"].items())
    }


def _artifact_digest(profile: dict) -> str:
    return _digest(_artifact_contract(profile))


def _runtime_digest(profile: dict) -> str:
    return _digest(profile["runtime_profile"])


def _platform_tag(sys_platform: str | None = None,
                  machine: str | None = None) -> str:
    raw_platform = sys.platform if sys_platform is None else sys_platform
    raw_machine = platform.machine() if machine is None else machine
    lowered = raw_machine.lower()
    if lowered in {"arm64", "aarch64"}:
        arch = "arm64"
    elif lowered in {"amd64", "x64", "x86_64"}:
        arch = "x86_64"
    else:
        raise HarnessError(f"unsupported physical architecture {raw_machine!r}")
    return f"{raw_platform}-{arch}"


def _verify_pinned(path: Path, spec: dict, label: str) -> None:
    try:
        before = path.stat()
        observed = _file_digest(path)
        after = path.stat()
    except OSError as exc:
        raise HarnessError(f"{label} is unavailable: {exc}") from exc
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise HarnessError(f"{label} moved during pin verification")
    if after.st_size != spec["size"] or observed != spec["sha256"]:
        raise HarnessError(f"{label} does not match its size/SHA-256 pin")


def _seed_artifacts(profile: dict, cache: Path, model_base: Path) -> Path:
    profile_id = profile["id"]
    runtime = profile["runtime_profile"]
    source_root = cache / profile_id
    destination = model_base / profile_id
    destination.mkdir(parents=True, mode=0o700)
    for name, spec in sorted(runtime["files"].items()):
        source = source_root / name
        _verify_pinned(source, spec, f"artifact cache {profile_id}/{name}")
        target = destination / name
        shutil.copy2(source, target)
        _verify_pinned(target, spec, f"seeded artifact {profile_id}/{name}")
    found = sorted(path.name for path in destination.iterdir() if path.is_file())
    expected = sorted(runtime["files"])
    if found != expected:
        raise HarnessError(f"seeded artifact set for {profile_id} is not exact")
    return destination


def _runtime_env(profile_path: Path, model_base: Path, data_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    for name in (
        "AGREP_BENCH_EMBED_PROFILE", "AGREP_MODEL_DIR", "AGREP_DATA_DIR",
        "AGREP_DATA_DIR_SOURCE", "AGREP_SEM_THREADS", "PYTHONPATH",
    ):
        env.pop(name, None)
    env.update({
        "AGREP_BENCH_EMBED_PROFILE": str(profile_path.resolve()),
        "AGREP_MODEL_DIR": str(model_base.resolve()),
        "AGREP_DATA_DIR": str(data_dir.resolve()),
        "AGREP_DATA_DIR_SOURCE": "env",
        "PYTHONHASHSEED": "0",
        "PYTHONPATH": str(PY),
    })
    return env


def _last_json(stdout: str, label: str) -> dict:
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise HarnessError(f"{label} did not emit a JSON object")


def _validate_child_result(result: dict, profile: dict) -> None:
    runtime = profile["runtime_profile"]
    expected = {
        "available_providers", "document_count", "document_norms",
        "document_repeat_max_abs", "documents_sha256", "documents_shape", "finite",
        "edge_document_norms", "edge_documents_sha256", "edge_documents_shape",
        "machine", "mixed_solo_cosines", "mixed_solo_min_cosine",
        "normalized_max_error", "onnxruntime_device",
        "onnxruntime_version", "profile", "python_implementation", "python_version",
        "query_norm", "query_repeat_max_abs", "query_sha256", "query_shape",
        "requested_provider", "semantic_threads", "session_providers", "sys_platform",
        "vector_dtype", "vectors_deterministic",
    }
    if not isinstance(result, dict) or set(result) != expected:
        raise HarnessError(f"profile {profile['id']}: runtime evidence shape is invalid")
    requested = runtime.get("provider", "CPUExecutionProvider")
    dim = runtime["dim"]
    if result["profile"] != profile["id"]:
        raise HarnessError(f"profile {profile['id']}: runtime loaded a different profile")
    if result["requested_provider"] != requested:
        raise HarnessError(f"profile {profile['id']}: requested provider drifted")
    available = result["available_providers"]
    active = result["session_providers"]
    if (not isinstance(available, list) or requested not in available
            or not isinstance(active, list) or not active or active[0] != requested):
        raise HarnessError(f"profile {profile['id']}: requested provider was not active")
    if (result["query_shape"] != [dim] or result["documents_shape"] != [4, dim]
            or result["edge_documents_shape"] != [2, dim]):
        raise HarnessError(f"profile {profile['id']}: vector dimensions are invalid")
    if (result["document_count"] != 4 or result["vector_dtype"] != "float32"
            or result["finite"] is not True or result["vectors_deterministic"] is not True):
        raise HarnessError(f"profile {profile['id']}: vector contract is invalid")
    for key in ("query_sha256", "documents_sha256", "edge_documents_sha256"):
        if not isinstance(result[key], str) or not selection.SHA256_RE.fullmatch(result[key]):
            raise HarnessError(f"profile {profile['id']}: {key} is invalid")
    norms = [result["query_norm"], *(result["document_norms"] or []),
             *(result["edge_document_norms"] or [])]
    if (len(norms) != 7 or any(isinstance(value, bool)
            or not isinstance(value, (int, float)) or not math.isfinite(float(value))
            or abs(float(value) - 1.0) > 0.00002 for value in norms)):
        raise HarnessError(f"profile {profile['id']}: vector norms are invalid")
    cosines = result["mixed_solo_cosines"]
    minimum = result["mixed_solo_min_cosine"]
    if (not isinstance(cosines, list) or len(cosines) != 4
            or any(isinstance(value, bool) or not isinstance(value, (int, float))
                   or not math.isfinite(float(value)) for value in cosines)
            or isinstance(minimum, bool) or not isinstance(minimum, (int, float))
            or not math.isfinite(float(minimum))
            or not math.isclose(float(minimum), min(map(float, cosines)), abs_tol=1e-9)
            or float(minimum) < BATCH_COSINE_MIN):
        raise HarnessError(f"profile {profile['id']}: batch stability is invalid")
    for key in ("normalized_max_error", "query_repeat_max_abs",
                "document_repeat_max_abs"):
        value = result[key]
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value)) or float(value) < 0):
            raise HarnessError(f"profile {profile['id']}: {key} is invalid")
    if (float(result["normalized_max_error"]) > 0.00002
            or float(result["query_repeat_max_abs"]) != 0.0
            or float(result["document_repeat_max_abs"]) != 0.0):
        raise HarnessError(f"profile {profile['id']}: vectors were not stable")
    for key in ("sys_platform", "machine", "python_version", "python_implementation",
                "onnxruntime_version", "onnxruntime_device"):
        if not isinstance(result[key], str) or not result[key]:
            raise HarnessError(f"profile {profile['id']}: {key} is missing")
    if not isinstance(result["semantic_threads"], int) or result["semantic_threads"] < 1:
        raise HarnessError(f"profile {profile['id']}: semantic thread count is invalid")


def _measure_profile(profile: dict, artifact_cache: Path, timeout: float) -> dict:
    with tempfile.TemporaryDirectory(prefix="agrep-embedder-platform-") as raw:
        root = Path(raw)
        model_base = root / "models"
        _seed_artifacts(profile, artifact_cache, model_base)
        profile_path = root / "runtime-profile.json"
        selection._write_json(profile_path, profile["runtime_profile"])
        data_dir = root / "data"
        data_dir.mkdir()
        env = _runtime_env(profile_path, model_base, data_dir)
        measured = selection._run(
            [sys.executable, "-c", _CHILD_SCRIPT], env=env, timeout=timeout)
        if measured["returncode"] != 0:
            detail = (measured["stdout"] + "\n" + measured["stderr"])[-2000:]
            raise HarnessError(
                f"profile {profile['id']} physical smoke exited "
                f"{measured['returncode']}: {detail}")
        child = _last_json(measured["stdout"], f"profile {profile['id']} physical smoke")
        _validate_child_result(child, profile)
        if measured["cpu_ms"] is None or measured["peak_rss_mib"] is None:
            raise HarnessError(f"profile {profile['id']}: CPU/RSS counters are unavailable")
        if child["sys_platform"] != sys.platform:
            raise HarnessError(f"profile {profile['id']}: child platform drifted")
        if _platform_tag(child["sys_platform"], child["machine"]) != _platform_tag():
            raise HarnessError(f"profile {profile['id']}: child architecture drifted")
        return {
            "artifact_digest": _artifact_digest(profile),
            "available_providers": child["available_providers"],
            "measurement": {
                "cpu_ms": measured["cpu_ms"],
                "exit_code": measured["returncode"],
                "max_processes": measured["max_processes"],
                "peak_handles": measured["peak_handles"],
                "peak_rss_mib": measured["peak_rss_mib"],
                "scope": "fresh-process-load-vector-smoke-final-exit",
                "wall_ms": measured["wall_ms"],
            },
            "profile": profile["id"],
            "requested_provider": child["requested_provider"],
            "runtime_profile_digest": _runtime_digest(profile),
            "semantic_threads": child["semantic_threads"],
            "session_providers": child["session_providers"],
            "vectors": {
                "document_count": child["document_count"],
                "document_norms": child["document_norms"],
                "document_repeat_max_abs": child["document_repeat_max_abs"],
                "documents_sha256": child["documents_sha256"],
                "documents_shape": child["documents_shape"],
                "edge_document_norms": child["edge_document_norms"],
                "edge_documents_sha256": child["edge_documents_sha256"],
                "edge_documents_shape": child["edge_documents_shape"],
                "dtype": child["vector_dtype"],
                "finite": child["finite"],
                "mixed_solo_cosines": child["mixed_solo_cosines"],
                "mixed_solo_min_cosine": child["mixed_solo_min_cosine"],
                "normalized_max_error": child["normalized_max_error"],
                "query_norm": child["query_norm"],
                "query_repeat_max_abs": child["query_repeat_max_abs"],
                "query_sha256": child["query_sha256"],
                "query_shape": child["query_shape"],
                "repeated_outputs_equal": child["vectors_deterministic"],
            },
            "_identity": {
                "machine": child["machine"],
                "onnxruntime_device": child["onnxruntime_device"],
                "onnxruntime_version": child["onnxruntime_version"],
                "python_implementation": child["python_implementation"],
                "python_version": child["python_version"],
                "sys_platform": child["sys_platform"],
            },
        }


def _platform_entry(results: list[dict], manifest_digest: str,
                    code_digest: str) -> tuple[str, dict]:
    if not results:
        raise HarnessError("physical platform run selected no profiles")
    identities = [result.pop("_identity") for result in results]
    if any(identity != identities[0] for identity in identities[1:]):
        raise HarnessError("physical profile processes disagree on platform metadata")
    identity = identities[0]
    tag = _platform_tag(identity["sys_platform"], identity["machine"])
    if not TAG_RE.fullmatch(tag):
        raise HarnessError(f"unsafe platform tag {tag!r}")
    return tag, {
        "code_digest": code_digest,
        "identity": identity,
        "manifest_digest": manifest_digest,
        "profiles": {result["profile"]: result for result in results},
        "tag": tag,
    }


def _empty_bundle(manifest_digest: str) -> dict:
    return {
        "kind": KIND,
        "manifest_digest": manifest_digest,
        "platforms": {},
        "schema": SCHEMA,
    }


def _number(value: Any, where: str, *, minimum: float = 0.0) -> None:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(float(value)) or float(value) < minimum):
        raise HarnessError(f"{where} must be a finite number >= {minimum}")


def _validate_profile_evidence(profile_id: str, value: Any, profile: dict) -> None:
    expected = {
        "artifact_digest", "available_providers", "measurement", "profile",
        "requested_provider", "runtime_profile_digest", "semantic_threads",
        "session_providers", "vectors",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise HarnessError(f"platform evidence for {profile_id} has an invalid shape")
    if value["profile"] != profile_id:
        raise HarnessError(f"platform evidence key/profile mismatch for {profile_id}")
    if value["runtime_profile_digest"] != _runtime_digest(profile):
        raise HarnessError(f"platform evidence runtime profile digest mismatch for {profile_id}")
    if value["artifact_digest"] != _artifact_digest(profile):
        raise HarnessError(f"platform evidence artifact digest mismatch for {profile_id}")
    requested = profile["runtime_profile"].get("provider", "CPUExecutionProvider")
    if value["requested_provider"] != requested:
        raise HarnessError(f"platform evidence requested provider mismatch for {profile_id}")
    available = value["available_providers"]
    active = value["session_providers"]
    if (not isinstance(available, list) or requested not in available
            or not isinstance(active, list) or not active or active[0] != requested
            or any(not isinstance(item, str) or not item for item in available + active)):
        raise HarnessError(f"platform evidence provider proof is invalid for {profile_id}")
    if not isinstance(value["semantic_threads"], int) or value["semantic_threads"] < 1:
        raise HarnessError(f"platform evidence thread count is invalid for {profile_id}")
    measurement = value["measurement"]
    measurement_keys = {
        "cpu_ms", "exit_code", "max_processes", "peak_handles", "peak_rss_mib",
        "scope", "wall_ms",
    }
    if not isinstance(measurement, dict) or set(measurement) != measurement_keys:
        raise HarnessError(f"platform evidence measurement is invalid for {profile_id}")
    if (measurement["exit_code"] != 0
            or measurement["scope"] != "fresh-process-load-vector-smoke-final-exit"):
        raise HarnessError(f"platform evidence did not record successful final exit for {profile_id}")
    for key in ("wall_ms", "cpu_ms", "peak_rss_mib"):
        _number(measurement[key], f"{profile_id}.measurement.{key}")
    if not isinstance(measurement["max_processes"], int) or measurement["max_processes"] < 1:
        raise HarnessError(f"platform evidence process count is invalid for {profile_id}")
    handles = measurement["peak_handles"]
    if handles is not None and (not isinstance(handles, int) or handles < 0):
        raise HarnessError(f"platform evidence handle count is invalid for {profile_id}")
    vectors = value["vectors"]
    vector_keys = {
        "document_count", "document_norms", "document_repeat_max_abs",
        "documents_sha256", "documents_shape", "dtype", "edge_document_norms",
        "edge_documents_sha256", "edge_documents_shape", "finite",
        "mixed_solo_cosines", "mixed_solo_min_cosine", "normalized_max_error",
        "query_norm", "query_repeat_max_abs", "query_sha256", "query_shape",
        "repeated_outputs_equal",
    }
    if not isinstance(vectors, dict) or set(vectors) != vector_keys:
        raise HarnessError(f"platform evidence vectors are invalid for {profile_id}")
    child = {
        "available_providers": available,
        "document_count": vectors["document_count"],
        "document_norms": vectors["document_norms"],
        "document_repeat_max_abs": vectors["document_repeat_max_abs"],
        "documents_sha256": vectors["documents_sha256"],
        "documents_shape": vectors["documents_shape"],
        "edge_document_norms": vectors["edge_document_norms"],
        "edge_documents_sha256": vectors["edge_documents_sha256"],
        "edge_documents_shape": vectors["edge_documents_shape"],
        "finite": vectors["finite"],
        "machine": "validation-machine",
        "mixed_solo_cosines": vectors["mixed_solo_cosines"],
        "mixed_solo_min_cosine": vectors["mixed_solo_min_cosine"],
        "normalized_max_error": vectors["normalized_max_error"],
        "onnxruntime_device": "validation-device",
        "onnxruntime_version": "validation-version",
        "profile": profile_id,
        "python_implementation": "validation-python",
        "python_version": "validation-version",
        "query_norm": vectors["query_norm"],
        "query_repeat_max_abs": vectors["query_repeat_max_abs"],
        "query_sha256": vectors["query_sha256"],
        "query_shape": vectors["query_shape"],
        "requested_provider": requested,
        "semantic_threads": value["semantic_threads"],
        "session_providers": active,
        "sys_platform": "validation-platform",
        "vector_dtype": vectors["dtype"],
        "vectors_deterministic": vectors["repeated_outputs_equal"],
    }
    _validate_child_result(child, profile)


def validate_bundle(value: Any, manifest: dict) -> dict:
    expected = {"kind", "manifest_digest", "platforms", "schema"}
    if not isinstance(value, dict) or set(value) != expected:
        raise HarnessError("platform evidence bundle has an invalid shape")
    manifest_digest = _digest(manifest)
    if (type(value["schema"]) is not int or value["schema"] != SCHEMA
            or value["kind"] != KIND
            or value["manifest_digest"] != manifest_digest):
        raise HarnessError("platform evidence manifest/schema digest mismatch")
    platforms = value["platforms"]
    if not isinstance(platforms, dict) or not platforms:
        raise HarnessError("platform evidence platforms must be a non-empty object")
    profiles = {profile["id"]: profile for profile in manifest["profiles"]}
    for tag, entry in platforms.items():
        if not isinstance(tag, str) or not TAG_RE.fullmatch(tag):
            raise HarnessError(f"invalid platform evidence tag {tag!r}")
        entry_keys = {"code_digest", "identity", "manifest_digest", "profiles", "tag"}
        if not isinstance(entry, dict) or set(entry) != entry_keys:
            raise HarnessError(f"platform evidence entry {tag} has an invalid shape")
        if entry["tag"] != tag or entry["manifest_digest"] != manifest_digest:
            raise HarnessError(f"platform evidence entry {tag} has mismatched identity/digest")
        if entry["code_digest"] != _code_digest():
            raise HarnessError(f"platform evidence entry {tag} has a mismatched code digest")
        identity = entry["identity"]
        identity_keys = {
            "machine", "onnxruntime_device", "onnxruntime_version",
            "python_implementation", "python_version", "sys_platform",
        }
        if (not isinstance(identity, dict) or set(identity) != identity_keys
                or any(not isinstance(item, str) or not item for item in identity.values())):
            raise HarnessError(f"platform evidence entry {tag} has invalid host metadata")
        if _platform_tag(identity["sys_platform"], identity["machine"]) != tag:
            raise HarnessError(f"platform evidence entry {tag} has a false platform tag")
        observed = entry["profiles"]
        if not isinstance(observed, dict) or not observed:
            raise HarnessError(f"platform evidence entry {tag} has no profile evidence")
        for profile_id, result in observed.items():
            profile = profiles.get(profile_id)
            if profile is None or profile["status"] != "runnable":
                raise HarnessError(f"platform evidence names unrunnable profile {profile_id!r}")
            if "all" not in profile["platforms"] and tag not in profile["platforms"]:
                raise HarnessError(f"profile {profile_id} does not declare support for {tag}")
            _validate_profile_evidence(profile_id, result, profile)
    return value


def _merge_platform_entry(existing: dict, incoming: dict, tag: str) -> dict:
    metadata = {"code_digest", "identity", "manifest_digest", "tag"}
    if any(existing[key] != incoming[key] for key in metadata):
        raise HarnessError(f"conflicting physical evidence for platform {tag}")
    merged = json.loads(json.dumps(existing))
    for profile_id, result in incoming["profiles"].items():
        prior = merged["profiles"].get(profile_id)
        if prior is not None and prior != result:
            raise HarnessError(
                f"conflicting physical evidence for {tag}/{profile_id}")
        merged["profiles"][profile_id] = result
    return merged


def merge_bundles(bundles: list[dict], manifest: dict) -> dict:
    if not bundles:
        raise HarnessError("no physical platform evidence was supplied")
    manifest_digest = _digest(manifest)
    merged = _empty_bundle(manifest_digest)
    for bundle in bundles:
        validated = validate_bundle(bundle, manifest)
        for tag, entry in validated["platforms"].items():
            prior = merged["platforms"].get(tag)
            merged["platforms"][tag] = (
                json.loads(json.dumps(entry)) if prior is None
                else _merge_platform_entry(prior, entry, tag)
            )
    return validate_bundle(merged, manifest)


def _read_bundle(path: Path) -> dict:
    value = selection._read_json(path)
    if not isinstance(value, dict):
        raise HarnessError(f"platform evidence at {path} must be an object")
    return value


def _write_merged(output: Path, incoming: list[dict], manifest: dict) -> dict:
    bundles = []
    if output.exists():
        bundles.append(_read_bundle(output))
    bundles.extend(incoming)
    merged = merge_bundles(bundles, manifest)
    selection._write_json(output, merged)
    return merged


def _failure_summary(profile_id: str, exc: Exception) -> dict[str, str]:
    if isinstance(exc, HarnessError):
        category = "harness_error"
    elif isinstance(exc, OSError):
        category = "os_error"
    elif isinstance(exc, subprocess.SubprocessError):
        category = "subprocess_error"
    elif isinstance(exc, (ValueError, TypeError)):
        category = "invalid_runtime_value"
    else:
        category = "runtime_error"
    detail = " ".join(str(exc).split()) or "profile measurement failed"
    detail = FAILURE_PATH_RE.sub("<path>", detail)
    if len(detail) > FAILURE_DETAIL_MAX:
        detail = detail[:FAILURE_DETAIL_MAX - 3] + "..."
    return {"category": category, "detail": detail, "profile": profile_id}


def _run_summary(output: Path, results: list[dict], failures: list[dict],
                 tag: str | None, platforms: list[str]) -> dict:
    status = "ok" if not failures else ("partial" if results else "failed")
    return {
        "evidence": str(output) if results else None,
        "failures": sorted(failures, key=lambda item: item["profile"]),
        "platform": tag,
        "platforms_total": sorted(platforms),
        "profiles": sorted(result["profile"] for result in results),
        "status": status,
    }


def stage_run(args: argparse.Namespace) -> int:
    manifest = selection.load_manifest(args.manifest)
    profiles = selection._profiles(manifest, args.profile)
    manifest_digest = _digest(manifest)
    code_digest = _code_digest()
    if not args.keep_going:
        results = [
            _measure_profile(profile, args.artifact_cache.resolve(), args.timeout)
            for profile in profiles
        ]
        tag, entry = _platform_entry(results, manifest_digest, code_digest)
        bundle = _empty_bundle(manifest_digest)
        bundle["platforms"][tag] = entry
        merged = _write_merged(args.output, [bundle], manifest)
        print(json.dumps({
            "evidence": str(args.output), "platform": tag,
            "profiles": sorted(entry["profiles"]),
            "platforms_total": sorted(merged["platforms"]),
        }, sort_keys=True))
        return 0

    results = []
    failures = []
    for profile in profiles:
        try:
            results.append(_measure_profile(
                profile, args.artifact_cache.resolve(), args.timeout))
        except Exception as exc:
            failures.append(_failure_summary(profile["id"], exc))
    if not results:
        print(json.dumps(
            _run_summary(args.output, results, failures, None, []), sort_keys=True))
        return 2
    tag, entry = _platform_entry(results, manifest_digest, code_digest)
    bundle = _empty_bundle(manifest_digest)
    bundle["platforms"][tag] = entry
    merged = _write_merged(args.output, [bundle], manifest)
    print(json.dumps(_run_summary(
        args.output, results, failures, tag, list(merged["platforms"])),
        sort_keys=True))
    return 2 if failures else 0


def stage_merge(args: argparse.Namespace) -> int:
    manifest = selection.load_manifest(args.manifest)
    merged = _write_merged(
        args.output, [_read_bundle(path) for path in args.input], manifest)
    print(json.dumps({
        "evidence": str(args.output), "platforms": sorted(merged["platforms"]),
    }, sort_keys=True))
    return 0


def stage_validate(args: argparse.Namespace) -> int:
    manifest = selection.load_manifest(args.manifest)
    value = validate_bundle(_read_bundle(args.input), manifest)
    print(json.dumps({
        "manifest_digest": value["manifest_digest"],
        "platforms": sorted(value["platforms"]),
        "profiles": {
            tag: sorted(entry["profiles"])
            for tag, entry in sorted(value["platforms"].items())
        },
    }, sort_keys=True))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="stage", required=True)
    run = sub.add_parser("run", help="measure exact pinned profiles on this host")
    run.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    run.add_argument("--artifact-cache", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--profile", action="append", default=[])
    run.add_argument("--timeout", type=float, default=600.0)
    run.add_argument(
        "--keep-going", action="store_true",
        help="retain successful evidence and report all profile failures")
    run.set_defaults(action=stage_run)
    merge = sub.add_parser("merge", help="import distinct physical-host evidence")
    merge.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    merge.add_argument("--output", type=Path, required=True)
    merge.add_argument("--input", type=Path, action="append", required=True)
    merge.set_defaults(action=stage_merge)
    validate = sub.add_parser("validate", help="validate imported physical-host evidence")
    validate.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    validate.add_argument("--input", type=Path, required=True)
    validate.set_defaults(action=stage_validate)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if getattr(args, "timeout", 1.0) <= 0 \
                or not math.isfinite(getattr(args, "timeout", 1.0)):
            parser.error("--timeout must be finite and positive")
        return args.action(args)
    except (HarnessError, OSError, ValueError, TypeError,
            subprocess.SubprocessError) as exc:
        print(f"embedder platform: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
