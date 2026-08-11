#!/usr/bin/env python3
"""Reproducible, generation-frozen embedder selection campaign.

Every stage consumes real ``embed.py`` or CLI output. The harness never fabricates
quality, latency, or throughput for profiles which have not run successfully.
Downloads are disabled unless ``prepare --allow-download`` is explicit.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
import platform
import random
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / "py"
DEFAULT_MANIFEST = Path(__file__).with_name("embedder_profiles.json")
DEFAULT_CALIBRATION = Path(__file__).with_name("semantic_calibration_tasks.json")
CALIBRATION_PROVENANCE = "synthetic-generalized-no-transcript-content"
DEFAULT_QUALITY = Path(__file__).with_name("semantic_worth_tasks.json")
QUALITY_EXAMPLE = Path(__file__).with_name("semantic_worth_tasks.example.json")
DEFAULT_RUN_DIR = Path(__file__).parent / ".data" / "embedder-selection"
SNAPSHOT_FILES = (
    "messages.jsonl", "replies.jsonl", "sessions.jsonl", ".ingest.sig",
    "session_concepts.jsonl", "concepts.json", "concept_pair.manifest.json",
    "settings.json", "boundary_stats.json", "session_family.meta.json",
    ".derived-owner.json", "corpus.db",
)
EVENT_STORE = "events/.store.sqlite3"
EVENT_GENERATION = "events/.generation"
LEGACY_EVENT_FILE_CAP = 100_000
LEGACY_EVENT_BYTES_CAP = 16 * 1024 * 1024 * 1024
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
REVISION_RE = re.compile(r"[0-9a-f]{40}\Z")
ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")
EMBED_DONE_RE = re.compile(r"embed done \| .* \| count=(\d+) \| .*elapsed=([0-9.]+)s")
EMBED_PHASES_RE = re.compile(
    r"embed phases \| plan=([0-9.]+)s \| load=([0-9.]+)s \| "
    r"inference=([0-9.]+)s \| (?:f32|segment)-publish=([0-9.]+)s")
DEDUPE_RE = re.compile(r"embedding dedupe: inferred (\d+) unique texts, reused (\d+)")
MIB = 1024 * 1024
TEN_MILLION = 10_000_000
CAMPAIGN_CONTRACT_VERSION = 2
CLOSED_SEARCH_BAND = 1.0
INCUMBENT_EXPECTED = {
    "semantic_correct": 14,
    "hybrid_correct": 19,
    "hybrid_total": 20,
    "floor": 0.82,
    "strong": 0.84,
}
_RESOURCE_MODULE = None
_PLATFORM_MODULE = None
_PROJECTION_MODULE = None
REQUIRED_CPU_PLATFORMS = ("darwin-arm64", "win32-x86_64")
Q8_SCALE_ROWS = (100_000, 1_000_000, 2_000_000)
Q8_SCALE_TOP_UP_ROWS = 1_000
SEMANTIC_RESOURCE_SAMPLE_S = 0.5
QUALITY_OUTPUT_BUDGET = 6_000
QUALITY_LANES = {
    "semantic": ["--semantic"],
    "hybrid": [],
}
_DARWIN_HARDWARE = None


class HarnessError(RuntimeError):
    pass


def _current_campaign_contract(value: Any) -> bool:
    return type(value) is int and value == CAMPAIGN_CONTRACT_VERSION


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _object_digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pinned_model_metrics(runtime: dict, model_dir: Path) -> dict:
    declared = runtime["files"]
    pinned_bytes = sum(int(item["size"]) for item in declared.values())
    referenced = {model_dir / name for name in declared}
    unreferenced = sum(
        path.stat().st_size for path in model_dir.rglob("*")
        if path.is_file() and path not in referenced
    ) if model_dir.exists() else 0
    return {
        "model_graph_bytes": int(runtime["model_bytes"]),
        "pinned_cache_bytes": pinned_bytes,
        "campaign_unreferenced_model_bytes": unreferenced,
    }


def _verify_pinned_artifact(path: Path, artifact: dict, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise HarnessError(f"{label} is missing: {path}") from exc
    if (not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size != int(artifact["size"])
            or _file_digest(path) != artifact["sha256"]):
        raise HarnessError(f"{label} failed exact size/SHA verification: {path}")


def _install_pinned_artifact(source: Path, destination: Path,
                             artifact: dict) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        try:
            os.link(source, temporary)
        except OSError:
            shutil.copy2(source, temporary)
        _verify_pinned_artifact(temporary, artifact, "staged model artifact")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _prune_unreferenced_model_files(model_dir: Path, runtime: dict) -> None:
    if not model_dir.exists():
        return
    declared = set(runtime["files"])
    for path in sorted(model_dir.iterdir(), key=lambda item: item.name):
        if path.name in declared and path.is_file():
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)


def _seed_artifact_cache(cache: Path, model_dir: Path, runtime: dict) -> dict:
    source_dir = cache.expanduser().resolve() / runtime["id"]
    sources = {}
    for name, artifact in runtime["files"].items():
        source = source_dir / name
        _verify_pinned_artifact(source, artifact, "artifact-cache seed")
        sources[name] = source
    model_dir.mkdir(parents=True, exist_ok=True)
    for name, artifact in runtime["files"].items():
        _install_pinned_artifact(sources[name], model_dir / name, artifact)
    _prune_unreferenced_model_files(model_dir, runtime)
    for name, artifact in runtime["files"].items():
        _verify_pinned_artifact(model_dir / name, artifact, "campaign model artifact")
    metrics = _pinned_model_metrics(runtime, model_dir)
    if metrics["campaign_unreferenced_model_bytes"]:
        raise HarnessError("campaign model directory retained undeclared artifacts")
    return metrics


def _code_inputs() -> list[Path]:
    candidates = {
        Path(__file__), Path(__file__).with_name("resources.py"), ROOT / "cli.py",
        Path(__file__).with_name("embedder_platform.py"),
        Path(__file__).with_name("embedder_projection.py"),
        Path(__file__).with_name("semantic_q8_scale.py"),
        *PY.glob("*.py"),
    }
    candidates.update(
        path for path in (ROOT / "_bin" / "agrep-rs", ROOT / "_bin" / "agrep-rs.exe")
        if path.is_file()
    )
    candidates.update(
        path for path in (
            ROOT / "target" / "release" / "agrep-rs",
            ROOT / "target" / "release" / "agrep-rs.exe",
        ) if path.is_file()
    )
    return sorted((path for path in candidates if path.is_file()),
                  key=lambda path: path.relative_to(ROOT).as_posix())


def _code_digest() -> str:
    digest = hashlib.sha256()
    for path in _code_inputs():
        digest.update(path.relative_to(ROOT).as_posix().encode("utf-8"))
        digest.update(bytes.fromhex(_file_digest(path)))
    return digest.hexdigest()


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise HarnessError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _read_json(path: Path) -> Any:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object)
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError(f"could not read {path}: {exc}") from exc


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n",
    )
    os.replace(temporary, path)


def _require_keys(value: dict, required: set[str], optional: set[str], where: str) -> None:
    missing = required - set(value)
    unknown = set(value) - required - optional
    if missing or unknown:
        details = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if unknown:
            details.append("unknown " + ", ".join(sorted(unknown)))
        raise HarnessError(f"{where}: {'; '.join(details)}")


def _validate_artifact(value: Any, where: str, *, named: bool) -> None:
    if not isinstance(value, dict):
        raise HarnessError(f"{where} must be an object")
    required = {"remote_path", "size", "sha256"}
    _require_keys(value, required, set(), where)
    remote = value["remote_path"]
    if (not isinstance(remote, str) or not remote or remote.startswith("/")
            or ".." in Path(remote).parts or "\\" in remote):
        raise HarnessError(f"{where}.remote_path is not a safe repository path")
    if named and Path(remote).name == "":
        raise HarnessError(f"{where}.remote_path has no filename")
    if not isinstance(value["size"], int) or value["size"] <= 0:
        raise HarnessError(f"{where}.size must be a positive integer")
    if not isinstance(value["sha256"], str) or not SHA256_RE.fullmatch(value["sha256"]):
        raise HarnessError(f"{where}.sha256 must be a lowercase SHA-256")


def _validate_runtime_profile(value: Any, profile_id: str) -> None:
    if not isinstance(value, dict):
        raise HarnessError(f"profile {profile_id}: runtime_profile must be an object")
    required = {
        "schema", "id", "repo", "revision", "license", "license_permissive",
        "dim", "native_dim", "max_seq", "pooling", "normalize", "query_prefix",
        "document_prefix", "layernorm_before_truncate", "quantization", "runtime",
        "files", "model_file", "tokenizer_file", "model_bytes", "output",
        "search_bands",
    }
    _require_keys(
        value, required, {"provider", "pad_token"},
        f"profile {profile_id}.runtime_profile")
    if value["schema"] != 1 or value["id"] != profile_id:
        raise HarnessError(f"profile {profile_id}: runtime schema/id mismatch")
    if not isinstance(value["repo"], str) or value["repo"].count("/") != 1:
        raise HarnessError(f"profile {profile_id}: repo must be owner/name")
    if not isinstance(value["revision"], str) or not REVISION_RE.fullmatch(value["revision"]):
        raise HarnessError(f"profile {profile_id}: revision must be a full lowercase commit")
    if not isinstance(value["license"], str) or not value["license"]:
        raise HarnessError(f"profile {profile_id}: license is required")
    if not isinstance(value["license_permissive"], bool):
        raise HarnessError(f"profile {profile_id}: license_permissive must be boolean")
    for key in ("dim", "native_dim", "max_seq", "model_bytes"):
        if not isinstance(value[key], int) or value[key] <= 0:
            raise HarnessError(f"profile {profile_id}: {key} must be positive")
    if value["dim"] > value["native_dim"]:
        raise HarnessError(f"profile {profile_id}: dim exceeds native_dim")
    if value["pooling"] not in {"cls", "masked_mean", "last_token", "direct_2d"}:
        raise HarnessError(f"profile {profile_id}: unsupported pooling")
    if value["normalize"] is not True:
        raise HarnessError(f"profile {profile_id}: vectors must be normalized")
    for key in ("query_prefix", "document_prefix", "quantization", "runtime"):
        if not isinstance(value[key], str):
            raise HarnessError(f"profile {profile_id}: {key} must be a string")
    if "pad_token" in value and (
            not isinstance(value["pad_token"], str) or not value["pad_token"]):
        raise HarnessError(f"profile {profile_id}: pad_token must be a non-empty string")
    if not isinstance(value["layernorm_before_truncate"], bool):
        raise HarnessError(f"profile {profile_id}: layernorm flag must be boolean")
    if value["runtime"] != "onnxruntime":
        raise HarnessError(f"profile {profile_id}: only onnxruntime is benchmarked")
    files = value["files"]
    if not isinstance(files, dict) or not files:
        raise HarnessError(f"profile {profile_id}: files must be non-empty")
    for name, artifact in files.items():
        if not isinstance(name, str) or Path(name).name != name or name in {"", ".", ".."}:
            raise HarnessError(f"profile {profile_id}: unsafe local artifact name")
        _validate_artifact(artifact, f"profile {profile_id}.files.{name}", named=True)
    if value["model_file"] not in files or value["tokenizer_file"] not in files:
        raise HarnessError(f"profile {profile_id}: model/tokenizer file is not pinned")
    model_bytes = sum(
        item["size"] for name, item in files.items() if name != value["tokenizer_file"]
    )
    if model_bytes != value["model_bytes"]:
        raise HarnessError(f"profile {profile_id}: model_bytes does not match pinned files")
    output = value["output"]
    if (not isinstance(output, dict) or len(output) != 1
            or set(output) not in ({"index"}, {"name"})):
        raise HarnessError(f"profile {profile_id}: output must select one index or name")
    if "index" in output and (not isinstance(output["index"], int) or output["index"] < 0):
        raise HarnessError(f"profile {profile_id}: output index is invalid")
    if "name" in output and (not isinstance(output["name"], str) or not output["name"]):
        raise HarnessError(f"profile {profile_id}: output name is invalid")
    bands = value["search_bands"]
    if not isinstance(bands, dict) or set(bands) != {"floor", "strong"}:
        raise HarnessError(f"profile {profile_id}: search_bands is invalid")
    if not all(isinstance(bands[key], (int, float)) and math.isfinite(bands[key])
               for key in bands):
        raise HarnessError(f"profile {profile_id}: search bands must be finite")
    if not all(-1.0 <= float(bands[key]) <= 1.0 for key in bands):
        raise HarnessError(f"profile {profile_id}: search bands must be in [-1, 1]")
    if float(bands["floor"]) > float(bands["strong"]):
        raise HarnessError(f"profile {profile_id}: floor exceeds strong band")


def _derived_adoption_eligible(profile: dict) -> bool:
    if profile.get("status") != "runnable":
        return False
    runtime = profile.get("runtime_profile")
    platforms = set(profile.get("platforms") or [])
    portable = "all" in platforms or {
        "darwin-arm64", "win32-x86_64",
    }.issubset(platforms)
    return bool(
        isinstance(runtime, dict)
        and runtime.get("license_permissive") is True
        and runtime.get("runtime") == "onnxruntime"
        and runtime.get("provider", "CPUExecutionProvider") == "CPUExecutionProvider"
        and portable
    )


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict:
    value = _read_json(path)
    if not isinstance(value, dict):
        raise HarnessError("profile manifest must be an object")
    _require_keys(value, {"schema", "profiles"}, set(), "manifest")
    if value["schema"] != 1 or not isinstance(value["profiles"], list):
        raise HarnessError("profile manifest schema must be 1 with a profile list")
    ids = set()
    baselines = 0
    for index, profile in enumerate(value["profiles"]):
        where = f"profiles[{index}]"
        if not isinstance(profile, dict):
            raise HarnessError(f"{where} must be an object")
        required = {"id", "baseline", "status", "adoption_eligible"}
        optional = {
            "runtime_profile", "disabled_reason", "source", "variant",
            "known_artifacts", "native_max_seq", "platforms",
        }
        _require_keys(profile, required, optional, where)
        profile_id = profile["id"]
        if not isinstance(profile_id, str) or not ID_RE.fullmatch(profile_id):
            raise HarnessError(f"{where}.id is invalid")
        if profile_id in ids:
            raise HarnessError(f"duplicate profile id {profile_id}")
        ids.add(profile_id)
        if not isinstance(profile["baseline"], bool) \
                or not isinstance(profile["adoption_eligible"], bool):
            raise HarnessError(f"profile {profile_id}: boolean flags are invalid")
        baselines += int(profile["baseline"])
        if profile["status"] == "runnable":
            if set(profile) != required | {"runtime_profile", "native_max_seq", "platforms"}:
                raise HarnessError(
                    f"profile {profile_id}: runnable entries contain non-runtime fields")
            if (not isinstance(profile["native_max_seq"], int)
                    or profile["native_max_seq"] <= 0
                    or profile["runtime_profile"]["max_seq"] > profile["native_max_seq"]):
                raise HarnessError(f"profile {profile_id}: native sequence cap is invalid")
            platforms = profile["platforms"]
            allowed_platforms = {
                "all", "darwin-arm64", "darwin-x86_64",
                "linux-arm64", "linux-x86_64", "win32-arm64", "win32-x86_64",
            }
            if (not isinstance(platforms, list) or not platforms
                    or any(item not in allowed_platforms for item in platforms)
                    or ("all" in platforms and len(platforms) != 1)):
                raise HarnessError(f"profile {profile_id}: platform list is invalid")
            _validate_runtime_profile(profile["runtime_profile"], profile_id)
        elif profile["status"] in {"pinned", "blocked"}:
            expected = required | {
                "disabled_reason", "source", "variant", "known_artifacts",
            }
            if set(profile) != expected:
                raise HarnessError(f"profile {profile_id}: disabled metadata is incomplete")
            if not isinstance(profile["disabled_reason"], str) or not profile["disabled_reason"]:
                raise HarnessError(f"profile {profile_id}: disabled_reason is required")
            source = profile["source"]
            if not isinstance(source, dict) or set(source) != {"repo", "revision", "license"}:
                raise HarnessError(f"profile {profile_id}: source metadata is invalid")
            if (not isinstance(source["repo"], str) or source["repo"].count("/") != 1
                    or not isinstance(source["revision"], str)
                    or not REVISION_RE.fullmatch(source["revision"])
                    or not isinstance(source["license"], str) or not source["license"]):
                raise HarnessError(f"profile {profile_id}: source pin is invalid")
            variant = profile["variant"]
            if not isinstance(variant, dict) or set(variant) != {"dim", "max_seq", "pooling"}:
                raise HarnessError(f"profile {profile_id}: variant metadata is invalid")
            if (not isinstance(variant["dim"], int) or variant["dim"] <= 0
                    or not isinstance(variant["max_seq"], int) or variant["max_seq"] <= 0
                    or not isinstance(variant["pooling"], str)):
                raise HarnessError(f"profile {profile_id}: variant values are invalid")
            artifacts = profile["known_artifacts"]
            if not isinstance(artifacts, list):
                raise HarnessError(f"profile {profile_id}: known_artifacts must be a list")
            for item_index, artifact in enumerate(artifacts):
                _validate_artifact(
                    artifact, f"profile {profile_id}.known_artifacts[{item_index}]",
                    named=True,
                )
        else:
            raise HarnessError(f"profile {profile_id}: unknown status {profile['status']!r}")
        derived = _derived_adoption_eligible(profile)
        if profile["status"] == "runnable" and profile["adoption_eligible"] != derived:
            raise HarnessError(
                f"profile {profile_id}: adoption eligibility conflicts with runtime metadata")
        profile["adoption_eligible"] = derived
    if baselines != 1:
        raise HarnessError("profile manifest needs exactly one baseline")
    return value


def load_calibration(path: Path = DEFAULT_CALIBRATION) -> dict:
    value = _read_json(path)
    required = {"schema", "provenance", "real", "gibberish"}
    if not isinstance(value, dict) or set(value) != required:
        raise HarnessError("calibration tasks have an invalid shape")
    if value["schema"] != 1 or value["provenance"] != CALIBRATION_PROVENANCE \
            or not isinstance(value["real"], list) \
            or not isinstance(value["gibberish"], list):
        raise HarnessError("calibration task schema or provenance is invalid")
    if len(value["real"]) < 7 or len(value["gibberish"]) < 4:
        raise HarnessError("calibration needs at least 7 real and 4 gibberish queries")
    ids = set()
    for kind in ("real", "gibberish"):
        for index, task in enumerate(value[kind]):
            if not isinstance(task, dict) or set(task) != {"id", "query"}:
                raise HarnessError(f"calibration {kind}[{index}] is invalid")
            if (not isinstance(task["id"], str) or not task["id"]
                    or not isinstance(task["query"], str) or not task["query"].strip()):
                raise HarnessError(f"calibration {kind}[{index}] has empty values")
            if task["id"] in ids:
                raise HarnessError(f"duplicate calibration id {task['id']}")
            ids.add(task["id"])
    return value


def load_quality(path: Path = DEFAULT_QUALITY) -> list[dict]:
    value = _read_json(path)
    if not isinstance(value, list) or len(value) < 20:
        raise HarnessError("quality fixture must contain at least 20 tasks")
    ids = set()
    for index, task in enumerate(value):
        required = {"id", "category", "query", "target", "expected"}
        if not isinstance(task, dict) or set(task) != required:
            raise HarnessError(f"quality task {index} has an invalid shape")
        if (not isinstance(task["id"], str) or task["id"] in ids
                or not isinstance(task["expected"], list) or not task["expected"]
                or any(not isinstance(item, str) or not item for item in task["expected"])):
            raise HarnessError(f"quality task {index} has invalid identity/targets")
        ids.add(task["id"])
    return value


def _platform_tag() -> str:
    machine = platform.machine().lower()
    arch = "arm64" if machine in {"arm64", "aarch64"} else "x86_64"
    return f"{sys.platform}-{arch}"


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError as exc:
        raise HarnessError(f"benchmark runtime is missing {name}") from exc


def _cpu_identity() -> str:
    candidates = [
        os.environ.get("PROCESSOR_IDENTIFIER", ""), platform.processor(),
        platform.uname().processor,
    ]
    if sys.platform == "darwin":
        hardware = _darwin_hardware()
        if isinstance(hardware.get("chip_type"), str):
            candidates.insert(0, hardware["chip_type"])
        try:
            result = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"], check=False,
                capture_output=True, text=True, encoding="utf-8", timeout=5)
            candidates.insert(0, result.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            pass
    elif sys.platform.startswith("linux"):
        try:
            for raw in Path("/proc/cpuinfo").read_text(
                    encoding="utf-8", errors="replace").splitlines():
                if raw.casefold().startswith(("model name", "hardware")):
                    candidates.insert(0, raw.partition(":")[2].strip())
                    break
        except OSError:
            pass
    return next((value for value in candidates if value), "unknown")


def _darwin_hardware() -> dict:
    global _DARWIN_HARDWARE
    if _DARWIN_HARDWARE is not None:
        return _DARWIN_HARDWARE
    value = {}
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["system_profiler", "SPHardwareDataType", "-json"], check=False,
                capture_output=True, text=True, encoding="utf-8", timeout=15)
            payload = json.loads(result.stdout)
            rows = payload.get("SPHardwareDataType")
            if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                value = rows[0]
        except (OSError, json.JSONDecodeError, subprocess.SubprocessError):
            pass
    _DARWIN_HARDWARE = value
    return value


def _physical_cpu_count() -> int | None:
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["sysctl", "-n", "hw.physicalcpu"], check=False,
                capture_output=True, text=True, encoding="utf-8", timeout=5)
            value = int(result.stdout.strip())
            return value if value > 0 else None
        except (OSError, ValueError, subprocess.SubprocessError):
            raw = _darwin_hardware().get("number_processors", "")
            match = re.search(r"\bproc\s+(\d+)\b", str(raw))
            if match is not None:
                return int(match.group(1))
            if platform.machine().lower() in {"arm64", "aarch64"}:
                return os.cpu_count()
            return None
    if sys.platform.startswith("linux"):
        try:
            packages = set()
            physical = core = None
            for raw in Path("/proc/cpuinfo").read_text(
                    encoding="utf-8", errors="replace").splitlines() + [""]:
                if raw.startswith("physical id"):
                    physical = raw.partition(":")[2].strip()
                elif raw.startswith("core id"):
                    core = raw.partition(":")[2].strip()
                elif not raw and physical is not None and core is not None:
                    packages.add((physical, core))
                    physical = core = None
            return len(packages) or None
        except OSError:
            return None
    if sys.platform == "win32":
        try:
            result = subprocess.run([
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "(Get-CimInstance Win32_Processor | "
                "Measure-Object NumberOfCores -Sum).Sum",
            ], check=False, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=15, **_hidden_process_kwargs())
            value = int(result.stdout.strip())
            return value if value > 0 else None
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
    return None


def _process_affinity() -> list[int] | None:
    getter = getattr(os, "sched_getaffinity", None)
    if getter is None:
        return None
    try:
        return sorted(int(item) for item in getter(0))
    except OSError:
        return None


def benchmark_environment(*, requested_provider: str,
                          actual_providers: list[str], semantic_threads: int,
                          power_policy: str) -> dict:
    value = {
        "schema": 1,
        "host": {
            "os": sys.platform,
            "os_release": platform.release(),
            "architecture": platform.machine().lower(),
            "cpu_identity": _cpu_identity(),
            "physical_cores": _physical_cpu_count(),
            "logical_cores": os.cpu_count(),
        },
        "runtime": {
            "python_implementation": platform.python_implementation(),
            "python_version": platform.python_version(),
            "onnxruntime_version": _package_version("onnxruntime"),
            "numpy_version": _package_version("numpy"),
            "tokenizers_version": _package_version("tokenizers"),
        },
        "execution": {
            "requested_provider": requested_provider,
            "actual_providers": list(actual_providers),
            "semantic_threads": semantic_threads,
            "process_affinity": _process_affinity(),
            "power_policy": power_policy,
            "accelerator_policy": (
                "cpu-only" if requested_provider == "CPUExecutionProvider"
                else "requested-provider"),
        },
    }
    validate_benchmark_environment(value)
    return value


def validate_benchmark_environment(value: Any) -> dict:
    if not isinstance(value, dict):
        raise HarnessError("benchmark environment must be an object")
    _require_keys(value, {"schema", "host", "runtime", "execution"}, set(),
                  "benchmark environment")
    if type(value["schema"]) is not int or value["schema"] != 1:
        raise HarnessError("benchmark environment schema must be 1")
    host = value["host"]
    runtime = value["runtime"]
    execution = value["execution"]
    if not isinstance(host, dict) or not isinstance(runtime, dict) \
            or not isinstance(execution, dict):
        raise HarnessError("benchmark environment sections must be objects")
    _require_keys(host, {
        "os", "os_release", "architecture", "cpu_identity", "physical_cores",
        "logical_cores",
    }, set(), "benchmark environment host")
    _require_keys(runtime, {
        "python_implementation", "python_version", "onnxruntime_version",
        "numpy_version", "tokenizers_version",
    }, set(), "benchmark environment runtime")
    _require_keys(execution, {
        "requested_provider", "actual_providers", "semantic_threads",
        "process_affinity", "power_policy", "accelerator_policy",
    }, set(), "benchmark environment execution")
    for key in ("os", "os_release", "architecture", "cpu_identity"):
        if not isinstance(host[key], str) or not host[key]:
            raise HarnessError(f"benchmark environment host {key} is invalid")
    for key in runtime:
        if not isinstance(runtime[key], str) or not runtime[key]:
            raise HarnessError(f"benchmark environment runtime {key} is invalid")
    if (type(host["logical_cores"]) is not int or host["logical_cores"] < 1
            or (host["physical_cores"] is not None
                and (type(host["physical_cores"]) is not int
                     or host["physical_cores"] < 1))):
        raise HarnessError("benchmark environment CPU counts are invalid")
    actual = execution["actual_providers"]
    affinity = execution["process_affinity"]
    if (not isinstance(execution["requested_provider"], str)
            or not execution["requested_provider"]
            or not isinstance(actual, list) or not actual
            or any(not isinstance(item, str) or not item for item in actual)
            or type(execution["semantic_threads"]) is not int
            or execution["semantic_threads"] < 1
            or (affinity is not None and (
                not isinstance(affinity, list) or not affinity
                or any(type(item) is not int or item < 0 for item in affinity)
                or affinity != sorted(set(affinity))))
            or not isinstance(execution["power_policy"], str)
            or not execution["power_policy"]
            or execution["accelerator_policy"] not in {
                "cpu-only", "requested-provider"}):
        raise HarnessError("benchmark environment execution policy is invalid")
    if actual[0] != execution["requested_provider"]:
        raise HarnessError("benchmark environment requested provider was not active")
    return value


def _profile_supported(profile: dict) -> bool:
    return profile["status"] == "runnable" and (
        "all" in profile["platforms"] or _platform_tag() in profile["platforms"]
    )


def _profiles(manifest: dict, selected: list[str]) -> list[dict]:
    if len(selected) != len(set(selected)):
        duplicates = sorted({item for item in selected if selected.count(item) > 1})
        raise HarnessError("duplicate profile selections: " + ", ".join(duplicates))
    by_id = {profile["id"]: profile for profile in manifest["profiles"]}
    missing = set(selected) - set(by_id)
    if missing:
        raise HarnessError("unknown profiles: " + ", ".join(sorted(missing)))
    profiles = [by_id[item] for item in selected] if selected else [
        profile for profile in manifest["profiles"] if _profile_supported(profile)
    ]
    blocked = [profile["id"] for profile in profiles if profile["status"] != "runnable"]
    if blocked:
        raise HarnessError(
            "profiles are pinned but not runnable: " + ", ".join(blocked)
        )
    unsupported = [profile["id"] for profile in profiles if not _profile_supported(profile)]
    if unsupported:
        raise HarnessError(
            f"profiles do not support {_platform_tag()}: " + ", ".join(unsupported)
        )
    return profiles


def _source_data_dir(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PY)
    result = _run(
        [sys.executable, "-c", (
            "import json; from pathlib import Path; import common; "
            "print(json.dumps({'data':str(common.DATA_DIR.resolve()),"
            "'resources':str(Path(common.resources.__file__).resolve())}))")],
        env=env, timeout=30.0,
    )
    _require_success(result, "source data directory resolution")
    resolved = _last_json(result["stdout"], "source data directory resolution")
    if (resolved.get("resources") != str((PY / "resources.py").resolve())
            or not isinstance(resolved.get("data"), str)):
        raise HarnessError("source data directory used a foreign Python runtime")
    return Path(resolved["data"])


def _snapshot_paths(root: Path) -> tuple[list[str], str]:
    names = list(SNAPSHOT_FILES)
    events = root / "events"
    marker = root / EVENT_GENERATION
    try:
        marker_stat = marker.lstat()
    except FileNotFoundError:
        marker_stat = None
    except OSError as exc:
        raise HarnessError(f"could not inspect event generation {marker}: {exc}") from exc
    if marker_stat is not None:
        if not stat.S_ISREG(marker_stat.st_mode):
            raise HarnessError(f"event generation is not a regular file: {marker}")
        store = root / EVENT_STORE
        try:
            store_stat = store.lstat()
        except OSError as exc:
            raise HarnessError(f"current event store is missing or unreadable: {store}") from exc
        if not stat.S_ISREG(store_stat.st_mode):
            raise HarnessError(f"event store is not a regular file: {store}")
        for suffix in ("-wal", "-shm", "-journal"):
            if Path(f"{store}{suffix}").exists():
                raise HarnessError("current event store has a live SQLite sidecar; retry")
        return [*names, EVENT_STORE, EVENT_GENERATION], "current"
    if not events.exists():
        return names, "legacy"
    try:
        entries = list(os.scandir(events))
    except OSError as exc:
        raise HarnessError(f"could not enumerate legacy events {events}: {exc}") from exc
    legacy = []
    total_bytes = 0
    for entry in entries:
        if not entry.name.endswith(".jsonl"):
            continue
        try:
            metadata = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise HarnessError(f"legacy event moved during discovery: {entry.path}") from exc
        if not stat.S_ISREG(metadata.st_mode):
            continue
        legacy.append(f"events/{entry.name}")
        total_bytes += metadata.st_size
    if len(legacy) > LEGACY_EVENT_FILE_CAP or total_bytes > LEGACY_EVENT_BYTES_CAP:
        raise HarnessError(
            f"legacy event snapshot exceeds its safety bound: "
            f"{len(legacy)} files, {total_bytes} bytes")
    return [*names, *sorted(legacy)], "legacy"


def _snapshot_inventory(root: Path) -> dict:
    names, event_mode = _snapshot_paths(root)
    rows = []
    required = {"messages.jsonl", "sessions.jsonl", "corpus.db"}
    required.update(name for name in names if name.startswith("events/"))
    for name in names:
        path = root / name
        if name in required and not path.is_file():
            raise HarnessError(f"snapshot source is missing {path}")
        if path.is_file():
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode):
                raise HarnessError(f"snapshot source is not a regular file: {path}")
            sha256 = _file_digest(path)
            after = path.lstat()
            identity_before = (
                before.st_size, before.st_mtime_ns, before.st_ctime_ns,
                before.st_dev, before.st_ino)
            identity_after = (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns,
                after.st_dev, after.st_ino)
            if identity_before != identity_after:
                raise HarnessError(f"snapshot source moved while hashing {path}")
            rows.append({
                "path": name, "size": after.st_size,
                "mtime_ns": after.st_mtime_ns, "sha256": sha256,
            })
    counts = {"messages.jsonl": 0, "replies.jsonl": 0}
    for name in counts:
        path = root / name
        if not path.is_file():
            continue
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if name == "messages.jsonl":
                    valid = value.get("id") and value.get("text") is not None
                else:
                    valid = value.get("id") and value.get("reply")
                counts[name] += int(bool(valid))
    return {
        "files": rows,
        "message_rows": counts["messages.jsonl"],
        "reply_rows": counts["replies.jsonl"],
        "rows": counts["messages.jsonl"] + counts["replies.jsonl"],
        "event_mode": event_mode,
        "digest": _object_digest(rows),
    }


def _snapshot_sources_match(observed: dict, expected: dict) -> bool:
    def sources(inventory: dict) -> dict:
        return {row["path"]: row for row in inventory.get("files", [])
                if row.get("path") != "corpus.db"}

    return (
        sources(observed) == sources(expected)
        and observed.get("message_rows") == expected.get("message_rows")
        and observed.get("reply_rows") == expected.get("reply_rows")
        and observed.get("rows") == expected.get("rows")
        and observed.get("event_mode") == expected.get("event_mode")
    )


_CORPUS_STATE_SCRIPT = r"""
import json
import sqlite3
import sys
import hashlib
from pathlib import Path

import corpusdb

def open_readonly():
    uri = Path(corpusdb.DB_PATH).resolve().as_uri() + '?mode=ro'
    return sqlite3.connect(uri, uri=True)

def event_state():
    events = corpusdb.common.DATA_DIR / 'events'
    marker = events / '.generation'
    try:
        generation = marker.read_bytes()
    except FileNotFoundError:
        return {'mode': 'legacy'}
    store = events / '.store.sqlite3'
    uri = store.resolve().as_uri() + '?mode=ro'
    db = sqlite3.connect(uri, uri=True)
    try:
        checked = db.execute('PRAGMA quick_check').fetchone()
        if checked is None or checked[0] != 'ok':
            raise RuntimeError('current event store failed quick_check')
        stored = db.execute(
            "SELECT value FROM event_meta WHERE key='generation'").fetchone()
        if stored is None or bytes(stored[0]) != generation:
            raise RuntimeError('event store generation does not match its marker')
        rows, payload_bytes = db.execute(
            'SELECT count(*),coalesce(sum(length(payload)),0) FROM event_sessions'
        ).fetchone()
    finally:
        db.close()
    return {
        'mode': 'current', 'rows': rows, 'payload_bytes': payload_bytes,
        'generation_sha256': hashlib.sha256(generation).hexdigest(),
    }

def state():
    live_before = corpusdb._stamp()
    db = open_readonly()
    try:
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        required = {'meta', 'msgs', 'msgs_fts', 'msgs_prose_fts'}
        if not required.issubset(tables):
            raise RuntimeError('corpus is missing production tables')
        meta = dict(db.execute(
            "SELECT key, value FROM meta WHERE key IN ('schema','stamp','fts_triggers')"))
        if meta.get('schema') != corpusdb._SCHEMA:
            raise RuntimeError('corpus schema does not match this build')
        if meta.get('fts_triggers') != corpusdb._TRIGGER_SCHEMA:
            raise RuntimeError('corpus trigger schema does not match this build')
        value = {
            'schema': meta['schema'],
            'stamp': meta.get('stamp'),
            'events': event_state(),
            'rows': db.execute('SELECT count(*) FROM msgs').fetchone()[0],
            'prose_rows': db.execute(
                "SELECT count(*) FROM msgs WHERE who <> 'tool'").fetchone()[0],
            'text_bytes': db.execute(
                'SELECT coalesce(sum(length(cast(text AS blob))),0) FROM msgs'
            ).fetchone()[0],
            'max_id': db.execute('SELECT coalesce(max(id),0) FROM msgs').fetchone()[0],
        }
    finally:
        db.close()
    live_after = corpusdb._stamp()
    if live_before != live_after:
        raise RuntimeError('corpus sources moved during coherence check')
    if value['stamp'] != live_after:
        raise RuntimeError('stored corpus stamp does not exactly match its sources')
    return value

mode = sys.argv[1]
if mode == 'reconcile':
    db = corpusdb.connect(quiet=True, allow_stale=False)
    if db is None:
        raise RuntimeError('production corpus reconciliation returned no database')
    db.close()
    result = state()
elif mode == 'inspect':
    result = state()
elif mode == 'bind':
    path = Path(corpusdb.DB_PATH)
    before_identity = (path.stat().st_size, path.stat().st_mtime_ns)
    before = state()
    db = corpusdb.connect(quiet=True, allow_stale=False)
    if db is None:
        raise RuntimeError('frozen corpus did not reopen as current')
    changes = db.total_changes
    db.close()
    after = state()
    after_identity = (path.stat().st_size, path.stat().st_mtime_ns)
    if changes or before != after or before_identity != after_identity:
        raise RuntimeError('production connect mutated the frozen corpus')
    result = after
else:
    raise RuntimeError('unknown corpus coherence action')
print(json.dumps(result, sort_keys=True))
"""


def _corpus_action(data: Path, timeout: float, action: str,
                   env: dict[str, str] | None = None) -> dict:
    bound_env = dict(os.environ if env is None else env)
    bound_env.update({
        "AGREP_DATA_DIR": str(data.resolve()),
        "AGREP_DATA_DIR_SOURCE": "env",
        "PYTHONPATH": str(PY),
    })
    result = _run(
        [sys.executable, "-c", _CORPUS_STATE_SCRIPT, action],
        env=bound_env, timeout=timeout)
    label = {
        "reconcile": "source corpus reconciliation",
        "inspect": "frozen corpus coherence check",
        "bind": "frozen corpus binding",
    }[action]
    _require_success(result, label)
    return _last_json(result["stdout"], label)


def _reconcile_source_corpus(source: Path, timeout: float) -> dict:
    return _corpus_action(source, timeout, "reconcile")


def _assert_corpus_coherent(data: Path, timeout: float) -> dict:
    return _corpus_action(data, timeout, "inspect")


def _corpus_content(state: dict) -> dict:
    return {key: state[key] for key in (
        "schema", "events", "rows", "prose_rows", "text_bytes")}


def _sqlite_backup(source: Path, destination: Path) -> None:
    source_uri = source.resolve().as_uri() + "?mode=ro"
    source_db = sqlite3.connect(source_uri, uri=True)
    destination_db = sqlite3.connect(destination)
    try:
        source_db.backup(destination_db)
    finally:
        destination_db.close()
        source_db.close()
    metadata = source.stat()
    os.utime(destination, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))


def _copy_snapshot(source: Path, destination: Path,
                   timeout: float = 3600.0) -> dict:
    reconciled = _reconcile_source_corpus(source, timeout)
    source_inventory = _snapshot_inventory(source)
    if _assert_corpus_coherent(source, timeout) != reconciled:
        raise HarnessError("source corpus moved after production reconciliation")
    if destination.exists():
        existing = _snapshot_inventory(destination)
        existing_corpus = _corpus_action(destination, timeout, "bind")
        source_files = {row["path"]: row for row in source_inventory["files"]}
        existing_files = {row["path"]: row for row in existing["files"]}
        stable_sources = set(source_files) - {"corpus.db", EVENT_STORE}
        if (stable_sources != set(existing_files) - {"corpus.db", EVENT_STORE}
                or any({key: source_files[name][key] for key in ("path", "size", "sha256")}
                != {key: existing_files.get(name, {}).get(key)
                    for key in ("path", "size", "sha256")}
                for name in stable_sources)
                or existing["rows"] != source_inventory["rows"]
                or existing["event_mode"] != source_inventory["event_mode"]
                or existing_corpus["events"] != reconciled["events"]):
            raise HarnessError(
                f"existing frozen snapshot differs at {destination}; choose another run dir"
            )
        if _snapshot_inventory(source) != source_inventory:
            raise HarnessError("snapshot source moved while reuse was being validated")
        return existing
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    temporary.mkdir(mode=0o700)
    try:
        for row in source_inventory["files"]:
            destination_path = temporary / row["path"]
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            if row["path"] in {"corpus.db", EVENT_STORE}:
                _sqlite_backup(source / row["path"], destination_path)
            else:
                shutil.copy2(source / row["path"], destination_path)
        copied_before_rebuild = _snapshot_inventory(temporary)
        after = _snapshot_inventory(source)
        if after != source_inventory:
            raise HarnessError("snapshot source moved while it was being frozen")
        source_files = {row["path"]: row for row in source_inventory["files"]}
        copied_files = {
            row["path"]: row for row in copied_before_rebuild["files"]}
        stable_sources = set(source_files) - {"corpus.db", EVENT_STORE}
        if (any(source_files[name] != copied_files.get(name) for name in stable_sources)
                or copied_before_rebuild["rows"] != source_inventory["rows"]
                or copied_before_rebuild["event_mode"] != source_inventory["event_mode"]):
            raise HarnessError("snapshot copy failed its content digest")
        for row in copied_before_rebuild["files"]:
            if row["path"].startswith("events/"):
                path = temporary / row["path"]
                path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)
        (temporary / "corpus.db").unlink()
        rebuilt = _reconcile_source_corpus(temporary, timeout)
        if (source_inventory["event_mode"] == "current"
                and _corpus_content(rebuilt) != _corpus_content(reconciled)):
            raise HarnessError("current event snapshot changed corpus content")
        copied = _snapshot_inventory(temporary)
        if _corpus_action(temporary, timeout, "bind") != rebuilt:
            raise HarnessError("rebuilt snapshot corpus was not stable on reopen")
        os.replace(temporary, destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return copied


def _copy_profile_data(snapshot: Path, destination: Path, expected: dict,
                       timeout: float = 3600.0) -> None:
    if destination.exists():
        observed = _snapshot_inventory(destination)
        if not _snapshot_sources_match(observed, expected):
            raise HarnessError(f"profile data drifted at {destination}")
        if expected["event_mode"] == "current":
            _reconcile_source_corpus(destination, timeout)
        return
    destination.mkdir(parents=True, mode=0o700)
    for row in expected["files"]:
        output = destination / row["path"]
        output.parent.mkdir(parents=True, exist_ok=True)
        source = snapshot / row["path"]
        if (row["path"].startswith("events/")
                and expected["event_mode"] == "legacy"):
            try:
                os.link(source, output)
            except OSError:
                shutil.copy2(source, output)
        else:
            shutil.copy2(source, output)
    if expected["event_mode"] == "current":
        _reconcile_source_corpus(destination, timeout)
    if not _snapshot_sources_match(_snapshot_inventory(destination), expected):
        raise HarnessError(f"profile data copy failed at {destination}")


def _bind_corpus(data: Path, env: dict[str, str], timeout: float) -> dict:
    return _corpus_action(data, timeout, "bind", env)


def _preflight_quality_targets(data: Path, quality: list[dict]) -> None:
    db = sqlite3.connect((data / "corpus.db").resolve().as_uri() + "?mode=ro", uri=True)
    try:
        for task in quality:
            for handle in task["expected"]:
                match = re.fullmatch(r"([^:\s]+):(\d+)", handle)
                if match is None:
                    raise HarnessError(f"quality target has invalid handle {handle!r}")
                prefix, raw_turn = match.groups()
                sessions = [row[0] for row in db.execute(
                    "SELECT DISTINCT session FROM msgs "
                    "WHERE substr(session,1,length(?))=?", (prefix, prefix))]
                if len(sessions) != 1:
                    raise HarnessError(
                        f"quality target {handle} resolves to {len(sessions)} sessions")
                found = db.execute(
                    "SELECT 1 FROM msgs WHERE session=? AND turn=? AND who <> 'tool' LIMIT 1",
                    (sessions[0], int(raw_turn))).fetchone()
                if found is None:
                    raise HarnessError(f"quality target is absent from frozen prose: {handle}")
    finally:
        db.close()


_EMBEDDING_STATE_SCRIPT = r"""
import json
import sys

import common
import semantic
import semantic_q8

mode = sys.argv[1]
meta = common.EMBEDDINGS_PATH.parent / 'embeddings.meta'
hashes = common.EMBEDDINGS_PATH.with_suffix('.hashes')
marker = semantic.generation_marker_path()
required = [common.EMBEDDINGS_PATH, common.IDS_PATH, hashes, meta, marker]
if not all(path.is_file() for path in required):
    raise RuntimeError('prepared embedding bundle is incomplete')
state = common.committed_embedding_artifact_state(
    meta, common.EMBEDDINGS_PATH, common.IDS_PATH)
commit = state.get('commit') or {}
rows = int(commit.get('rows') or 0)
if rows <= 0:
    raise RuntimeError('prepared embedding bundle has no committed rows')
coherence = semantic.embedding_coherence()
if mode == 'freeze' and not coherence.get('coherent'):
    raise RuntimeError('full embedding output is not source-coherent: ' + str(coherence))
files = [path.relative_to(common.DATA_DIR).as_posix() for path in required]
q8 = None
if semantic_q8.MANIFEST_PATH.exists():
    ready = semantic_q8._validated_manifest(
        state, source_current=(mode == 'freeze'))
    if ready is None:
        raise RuntimeError('q8 manifest is not coherent with the prepared f32 bundle')
    q8_paths = [
        semantic_q8.MANIFEST_PATH, ready['artifact_path'],
        ready['group_artifact_path'], ready['exact_artifact_path'],
    ]
    files.extend(path.relative_to(common.DATA_DIR).as_posix() for path in q8_paths)
    q8 = {
        'rows': int(ready['rows']), 'dim': int(ready['dim']),
        'generation_relation': str(ready['generation_relation']),
        'artifact_generation': str(ready['artifact_generation']),
    }
print(json.dumps({
    'identity': state['identity'], 'commit': commit, 'rows': rows,
    'files': files, 'q8': q8,
}, sort_keys=True))
"""


def _embedding_data_state(data: Path, env: dict[str, str], timeout: float,
                          mode: str) -> dict:
    if mode not in {"freeze", "restored"}:
        raise HarnessError(f"unknown embedding verification mode {mode}")
    bound_env = dict(env)
    bound_env["AGREP_DATA_DIR"] = str(data.resolve())
    result = _run(
        [sys.executable, "-c", _EMBEDDING_STATE_SCRIPT, mode],
        env=bound_env, timeout=timeout)
    _require_success(result, f"{mode} embedding bundle verification")
    payload = _last_json(
        result["stdout"], f"{mode} embedding bundle verification")
    if int(payload.get("rows") or 0) <= 0:
        raise HarnessError(f"{mode} embedding bundle has no committed rows")
    return payload


def _bundle_file_inventory(root: Path, relative_files: list[str]) -> dict:
    rows = []
    for name in sorted(set(relative_files)):
        relative = Path(name)
        if (relative.is_absolute() or not relative.parts
                or any(part in {"", ".", ".."} for part in relative.parts)):
            raise HarnessError(f"unsafe prepared embedding artifact path {name!r}")
        path = root / relative
        try:
            before = path.lstat()
        except OSError as exc:
            raise HarnessError(f"prepared embedding artifact is missing: {path}") from exc
        if not stat.S_ISREG(before.st_mode):
            raise HarnessError(f"prepared embedding artifact is not regular: {path}")
        digest = _file_digest(path)
        after = path.lstat()
        if ((before.st_size, before.st_mtime_ns, before.st_dev, before.st_ino)
                != (after.st_size, after.st_mtime_ns, after.st_dev, after.st_ino)):
            raise HarnessError(f"prepared embedding artifact moved while hashing: {path}")
        rows.append({"path": relative.as_posix(), "size": after.st_size,
                     "mtime_ns": after.st_mtime_ns, "sha256": digest})
    return {"files": rows, "digest": _object_digest(rows),
            "bytes": sum(row["size"] for row in rows)}


def _validate_embedding_bundle(bundle_dir: Path, record: dict) -> dict:
    if (not isinstance(record, dict) or record.get("schema") != 1
            or not isinstance(record.get("inventory"), dict)
            or not isinstance(record.get("embedding"), dict)):
        raise HarnessError("prepared embedding bundle metadata is malformed")
    names = [row.get("path") for row in record["inventory"].get("files", [])]
    if not names or any(not isinstance(name, str) for name in names):
        raise HarnessError("prepared embedding bundle inventory is malformed")
    observed = _bundle_file_inventory(bundle_dir, names)
    if observed != record["inventory"]:
        raise HarnessError(f"prepared embedding bundle drifted at {bundle_dir}")
    return record


def _freeze_embedding_bundle(data: Path, bundle_dir: Path, env: dict[str, str],
                             timeout: float) -> dict:
    embedding = _embedding_data_state(data, env, timeout, "freeze")
    source_inventory = _bundle_file_inventory(data, embedding["files"])
    if bundle_dir.exists():
        raise HarnessError(f"prepared embedding bundle already exists at {bundle_dir}")
    temporary = bundle_dir.with_name(f".{bundle_dir.name}.{os.getpid()}.tmp")
    temporary.mkdir(parents=True, mode=0o700)
    try:
        for row in source_inventory["files"]:
            source = data / row["path"]
            output = temporary / row["path"]
            output.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(source, output)
            except OSError as exc:
                raise HarnessError(
                    "prepared embedding artifacts require same-volume hardlinks") from exc
        inventory = _bundle_file_inventory(temporary, embedding["files"])
        if inventory != source_inventory:
            raise HarnessError("prepared embedding bundle copy changed artifact identity")
        os.replace(temporary, bundle_dir)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {"schema": 1, "inventory": source_inventory, "embedding": embedding}


def _restore_embedding_bundle(bundle_dir: Path, data: Path, record: dict,
                              env: dict[str, str], timeout: float) -> dict:
    record = _validate_embedding_bundle(bundle_dir, record)
    for name in (
            "embeddings.f32", "embeddings.ids", "embeddings.hashes",
            "embeddings.meta", ".semantic-embeddings-generation.json",
            "embeddings.q8.meta"):
        (data / name).unlink(missing_ok=True)
    shutil.rmtree(data / "semantic-q8", ignore_errors=True)
    for row in record["inventory"]["files"]:
        source = bundle_dir / row["path"]
        output = data / row["path"]
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(
            f".{output.name}.{os.getpid()}.{time.time_ns()}.tmp")
        try:
            os.link(source, temporary)
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
    restored = _embedding_data_state(data, env, timeout, "restored")
    expected = record["embedding"]
    if (restored.get("identity") != expected.get("identity")
            or restored.get("rows") != expected.get("rows")
            or restored.get("q8") != expected.get("q8")):
        raise HarnessError("restored embedding bundle changed its committed generation")
    return restored


def _runtime_profile(profile: dict, *, raw_bands: bool = False,
                     calibrated: dict | None = None) -> dict:
    value = json.loads(json.dumps(profile["runtime_profile"]))
    if raw_bands:
        value["search_bands"] = {"floor": -1.0, "strong": 1.0}
    elif calibrated is not None:
        if calibrated.get("floor") is None or calibrated.get("strong") is None:
            raise HarnessError(f"profile {profile['id']} has no usable calibrated bands")
        value["search_bands"] = {
            "floor": float(calibrated["floor"]),
            "strong": float(calibrated["strong"]),
        }
    return value


def _effective_calibration(calibration: dict) -> tuple[dict[str, float], bool]:
    floor = calibration.get("floor")
    strong = calibration.get("strong")
    usable = all(
        isinstance(value, (int, float)) and math.isfinite(float(value))
        for value in (floor, strong)
    )
    if usable:
        return {"floor": float(floor), "strong": float(strong)}, True
    return {"floor": CLOSED_SEARCH_BAND, "strong": CLOSED_SEARCH_BAND}, False


def _profile_root(run_dir: Path, profile_id: str) -> Path:
    return run_dir / "profiles" / profile_id


def _profile_env(run_dir: Path, profile_id: str, profile_path: Path, *,
                 data_dir: Path | None = None) -> dict[str, str]:
    root = _profile_root(run_dir, profile_id)
    selected_data = data_dir or root / "data"
    env = dict(os.environ)
    for name in (
        "AGREP_PROFILE", "CODEX_THREAD_ID", "CODEX_SANDBOX",
        "CLAUDECODE", "CLAUDE_CODE", "OPENCODE", "GEMINI_CLI", "CURSOR_AGENT",
    ):
        env.pop(name, None)
    env.update({
        "AGREP_DATA_DIR": str(selected_data.resolve()),
        "AGREP_DATA_DIR_SOURCE": "env",
        "AGREP_MODEL_DIR": str((root / "models").resolve()),
        "AGREP_BENCH_EMBED_PROFILE": str(profile_path.resolve()),
        "AGREP_EMBED_ANYWAY": "1",
        "PYTHONPATH": str(PY),
        "PYTHONHASHSEED": "0",
    })
    return env


def _resource_module():
    global _RESOURCE_MODULE
    if _RESOURCE_MODULE is None:
        path = Path(__file__).with_name("resources.py")
        spec = importlib.util.spec_from_file_location("agrep_selection_resources", path)
        module = importlib.util.module_from_spec(spec)
        if spec.loader is None:
            raise HarnessError("could not load the resource measurement harness")
        spec.loader.exec_module(module)
        _RESOURCE_MODULE = module
    return _RESOURCE_MODULE


def _platform_module():
    global _PLATFORM_MODULE
    if _PLATFORM_MODULE is None:
        path = Path(__file__).with_name("embedder_platform.py")
        spec = importlib.util.spec_from_file_location(
            "agrep_selection_platform", path)
        module = importlib.util.module_from_spec(spec)
        if spec.loader is None:
            raise HarnessError("could not load the platform evidence harness")
        spec.loader.exec_module(module)
        _PLATFORM_MODULE = module
    return _PLATFORM_MODULE


def _projection_module():
    global _PROJECTION_MODULE
    if _PROJECTION_MODULE is None:
        path = Path(__file__).with_name("embedder_projection.py")
        spec = importlib.util.spec_from_file_location(
            "agrep_selection_projection", path)
        module = importlib.util.module_from_spec(spec)
        if spec.loader is None:
            raise HarnessError("could not load the embedder projection harness")
        spec.loader.exec_module(module)
        _PROJECTION_MODULE = module
    return _PROJECTION_MODULE


def _physical_platform_support(bundle: dict, profile_ids: list[str]) -> dict:
    platforms = bundle["platforms"]
    support = {}
    for profile_id in profile_ids:
        measured = []
        for tag in REQUIRED_CPU_PLATFORMS:
            evidence = platforms.get(tag, {}).get("profiles", {}).get(profile_id)
            active = evidence.get("session_providers") if isinstance(evidence, dict) else None
            if (isinstance(evidence, dict)
                    and evidence.get("requested_provider") == "CPUExecutionProvider"
                    and isinstance(active, list) and active
                    and active[0] == "CPUExecutionProvider"):
                measured.append(tag)
        support[profile_id] = {
            "complete": len(measured) == len(REQUIRED_CPU_PLATFORMS),
            "measured": measured,
            "missing": [
                tag for tag in REQUIRED_CPU_PLATFORMS if tag not in measured
            ],
        }
    return support


def _validated_platform_support(path: Path, manifest: dict,
                                profile_ids: list[str]) -> tuple[dict, dict]:
    value = _read_json(path)
    try:
        bundle = _platform_module().validate_bundle(value, manifest)
    except RuntimeError as exc:
        raise HarnessError(f"platform evidence is invalid: {exc}") from exc
    return bundle, _physical_platform_support(bundle, profile_ids)


def _resource_tracker(pid: int):
    return _resource_module()._TreeAccumulator(pid, new_process=True)


def _popen_platform_kwargs() -> dict[str, int]:
    if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def _run(command: list[str], *, env: dict[str, str], timeout: float) -> dict:
    started = time.perf_counter()
    process = subprocess.Popen(
        command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        **_popen_platform_kwargs(),
    )
    resources = _resource_tracker(process.pid)
    while process.poll() is None:
        resources.observe()
        if time.perf_counter() - started > timeout:
            process.kill()
            stdout, stderr = process.communicate()
            raise HarnessError(
                f"command timed out after {timeout:.0f}s: {' '.join(command)}\n"
                f"{(stdout + stderr)[-1000:]}"
            )
        time.sleep(0.02)
    stdout, stderr = process.communicate()
    resources.observe()
    usage = resources.metrics()
    return {
        "command": command,
        "returncode": process.returncode,
        "wall_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "cpu_ms": (round(float(usage["cpu_seconds"]) * 1000.0, 3)
                   if usage["cpu_seconds"] is not None else None),
        "peak_rss_mib": usage["rss_mib"],
        "peak_handles": usage["handles"],
        "max_processes": usage["processes"],
        "stdout": stdout,
        "stderr": stderr,
    }


def _require_success(result: dict, label: str) -> None:
    if result["returncode"] != 0:
        detail = (result["stdout"] + "\n" + result["stderr"])[-2000:]
        raise HarnessError(f"{label} exited {result['returncode']}: {detail}")


def _last_json(stdout: str, label: str) -> dict:
    for raw in reversed(stdout.splitlines()):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise HarnessError(f"{label} did not emit a JSON object")


def _stop_worker(env: dict[str, str]) -> None:
    result = _run(
        [
            sys.executable, "-c",
            "import semworker; result=semworker.stop_worker_and_wait(); "
            "raise SystemExit(0 if result['ok'] else 1)",
        ],
        env=env, timeout=30,
    )
    _require_success(result, "semantic worker shutdown")


def _worker_resources(env: dict[str, str]) -> dict:
    result = _run(
        [
            sys.executable, "-c",
            "import json,semworker; print(json.dumps("
            "semworker.resident_status(include_resources=True)))",
        ],
        env=env, timeout=30,
    )
    _require_success(result, "semantic worker resource probe")
    status = _last_json(result["stdout"], "semantic worker resource probe")
    return {
        "running": bool(status.get("running")),
        "pid": status.get("pid"),
        "rss_mib": (round(int(status["rss_bytes"]) / MIB, 3)
                    if status.get("rss_bytes") is not None else None),
    }


def _measure_semantic_resources(
        data: Path, env: dict[str, str], model_root: Path) -> dict:
    try:
        measured = _resource_module()._measure_semantic(
            data.resolve(), env, model_root.resolve(),
            sample_s=SEMANTIC_RESOURCE_SAMPLE_S)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise HarnessError(f"semantic resource probe failed: {exc}") from exc
    if measured.get("semantic_status") != "measured":
        raise HarnessError(
            "semantic resource probe was skipped: "
            f"{measured.get('semantic_skip_reason')}")
    return measured


def _state_inputs(manifest: dict, calibration: dict, quality: list[dict]) -> dict:
    return {
        "manifest_digest": _object_digest(manifest),
        "calibration_digest": _object_digest(calibration),
        "quality_digest": _object_digest(quality),
        "code_digest": _code_digest(),
    }


def _load_state(args: argparse.Namespace) -> tuple[dict, dict, dict, list[dict]]:
    manifest = load_manifest(args.manifest)
    calibration = load_calibration(args.calibration_tasks)
    quality = load_quality(args.quality_tasks)
    state_path = args.run_dir / "state.json"
    state = _read_json(state_path)
    if not isinstance(state, dict) or state.get("schema") != 1:
        raise HarnessError(f"invalid or absent prepared campaign at {state_path}")
    if not _current_campaign_contract(state.get("campaign_contract_version")):
        raise HarnessError(
            "prepared campaign uses an older evidence contract; start a new campaign")
    if state.get("inputs") != _state_inputs(manifest, calibration, quality):
        raise HarnessError("manifest/task digest moved after prepare; start a new campaign")
    snapshot = _snapshot_inventory(args.run_dir / "snapshot")
    if snapshot != state.get("snapshot"):
        raise HarnessError("frozen corpus snapshot digest moved after prepare")
    if args.expect_snapshot and snapshot["digest"] != args.expect_snapshot:
        raise HarnessError(
            f"expected snapshot {args.expect_snapshot}, got {snapshot['digest']}"
        )
    prepare = state.get("prepare")
    environments = state.get("benchmark_environments")
    if (not isinstance(prepare, dict) or not isinstance(environments, dict)
            or set(environments) != set(prepare)):
        raise HarnessError("prepared campaign benchmark environments are missing")
    for profile_id, result in prepare.items():
        if (not isinstance(result, dict)
                or environments[profile_id] != result.get("benchmark_environment")):
            raise HarnessError(
                f"{profile_id} benchmark environment is not state-bound")
        validate_benchmark_environment(environments[profile_id])
    return state, manifest, calibration, quality


def _selected_from_state(state: dict, manifest: dict, selected: list[str]) -> list[dict]:
    profiles = _profiles(manifest, selected)
    prepared = set(state.get("profiles") or [])
    missing = [profile["id"] for profile in profiles if profile["id"] not in prepared]
    if missing:
        raise HarnessError("profiles were not prepared: " + ", ".join(missing))
    return profiles


def _read_profile_artifact(root: Path, name: str, state: dict,
                           profile_id: str, expected: dict[str, Any]) -> dict:
    path = root / name
    value = _read_json(path)
    common = {
        "schema": 1,
        "campaign_contract_version": CAMPAIGN_CONTRACT_VERSION,
        "profile": profile_id,
        "snapshot_digest": state["snapshot"]["digest"],
        "campaign_inputs": state["inputs"],
    }
    mismatched = [
        key for key, wanted in {**common, **expected}.items()
        if value.get(key) != wanted
    ] if isinstance(value, dict) else list(common)
    if (isinstance(value, dict)
            and not _current_campaign_contract(
                value.get("campaign_contract_version"))):
        mismatched.append("campaign_contract_version")
    if mismatched:
        raise HarnessError(
            f"{profile_id} {name} is stale or malformed: "
            + ", ".join(sorted(mismatched)))
    return value


def _validated_calibrated_profile(root: Path, profile: dict,
                                  state: dict) -> tuple[dict, Path, str]:
    profile_id = profile["id"]
    report = _read_profile_artifact(
        root, "calibration.json", state, profile_id, {
            "task_digest": state["inputs"]["calibration_digest"],
            "base_profile_digest": _object_digest(_runtime_profile(profile)),
        })
    calibration = report.get("calibration")
    if not isinstance(calibration, dict):
        raise HarnessError(f"{profile_id} calibration artifact is malformed")
    bands, usable = _effective_calibration(calibration)
    if (report.get("calibration_usable") is not usable
            or report.get("effective_search_bands") != bands):
        raise HarnessError(f"{profile_id} calibrated band metadata is stale")
    runtime = _runtime_profile(profile, calibrated=bands)
    path = root / "profile-calibrated.json"
    if _read_json(path) != runtime:
        raise HarnessError(f"{profile_id} calibrated runtime profile is stale")
    return report, path, _object_digest(runtime)


_PREPARE_RESULT_KEYS = {
    "model", "session", "embed", "embed_stats", "embedding_bundle",
    "embed_skipped", "profile_digest", "model_bytes", "model_graph_bytes",
    "pinned_cache_bytes", "campaign_unreferenced_model_bytes", "dim", "corpus",
    "benchmark_environment", "benchmark_environment_digest",
}


def _validate_recovered_prepare(
        value: Any, *, profile_id: str, runtime: dict, snapshot: dict,
        inputs: dict, bundle_dir: Path) -> dict:
    envelope_keys = {
        "schema", "profile", "snapshot_digest", "campaign_inputs",
        "runtime_profile_digest", "result", "campaign_contract_version",
    }
    if not isinstance(value, dict) or set(value) != envelope_keys:
        raise HarnessError(f"{profile_id} prepare artifact has an invalid shape")
    runtime_digest = _object_digest(runtime)
    if (type(value.get("schema")) is not int or value.get("schema") != 1
            or not _current_campaign_contract(
                value.get("campaign_contract_version"))
            or value.get("profile") != profile_id
            or value.get("snapshot_digest") != snapshot["digest"]
            or value.get("campaign_inputs") != inputs
            or value.get("runtime_profile_digest") != runtime_digest):
        raise HarnessError(f"{profile_id} prepare artifact is stale")
    result = value.get("result")
    if not isinstance(result, dict) or set(result) != _PREPARE_RESULT_KEYS:
        raise HarnessError(f"{profile_id} prepare result has an invalid shape")
    expected_pinned = sum(int(item["size"]) for item in runtime["files"].values())
    if (result.get("profile_digest") != runtime_digest
            or result.get("model_bytes") != runtime["model_bytes"]
            or result.get("model_graph_bytes") != runtime["model_bytes"]
            or result.get("pinned_cache_bytes") != expected_pinned
            or result.get("campaign_unreferenced_model_bytes") != 0
            or result.get("dim") != runtime["dim"]
            or not isinstance(result.get("model"), dict)
            or not isinstance(result.get("session"), dict)
            or not isinstance(result.get("corpus"), dict)
            or not isinstance(result.get("embed_skipped"), bool)):
        raise HarnessError(f"{profile_id} prepare result metadata is stale")
    benchmark_environment = validate_benchmark_environment(
        result.get("benchmark_environment"))
    if (result.get("benchmark_environment_digest")
            != _object_digest(benchmark_environment)):
        raise HarnessError(f"{profile_id} benchmark environment digest is stale")
    if (result["model"].get("returncode") != 0
            or result["session"].get("returncode") != 0
            or not isinstance(result["session"].get("actual_providers"), list)
            or not result["session"]["actual_providers"]):
        raise HarnessError(f"{profile_id} prepare runtime proof is incomplete")
    execution = benchmark_environment["execution"]
    if (execution["requested_provider"]
            != runtime.get("provider", "CPUExecutionProvider")
            or execution["actual_providers"]
            != result["session"]["actual_providers"]
            or execution["semantic_threads"]
            != result["session"].get("semantic_threads")):
        raise HarnessError(f"{profile_id} prepare environment is not bound to runtime")
    if result["embed_skipped"]:
        if any(result.get(key) is not None for key in (
                "embed", "embed_stats", "embedding_bundle")):
            raise HarnessError(f"{profile_id} skipped prepare result is inconsistent")
        if bundle_dir.exists():
            raise HarnessError(f"{profile_id} skipped prepare retained a bundle")
    else:
        if (not isinstance(result.get("embed"), dict)
                or not isinstance(result.get("embed_stats"), dict)
                or not isinstance(result.get("embedding_bundle"), dict)):
            raise HarnessError(f"{profile_id} prepared embedding metadata is incomplete")
        bundle = _validate_embedding_bundle(bundle_dir, result["embedding_bundle"])
        rows = bundle["embedding"].get("rows")
        if (result["embed"].get("returncode") != 0 or type(rows) is not int
                or rows <= 0 or result["embed_stats"].get("rows") != rows
                or result["embed_stats"].get("published_rows") != rows):
            raise HarnessError(f"{profile_id} prepared embedding proof is inconsistent")
        if (result["embed"].get("benchmark_environment_digest")
                != result["benchmark_environment_digest"]):
            raise HarnessError(f"{profile_id} full embed is not environment-bound")
    return result


def _prepare_artifact(profile_id: str, runtime: dict, snapshot: dict,
                      inputs: dict, result: dict) -> dict:
    return {
        "schema": 1, "profile": profile_id,
        "campaign_contract_version": CAMPAIGN_CONTRACT_VERSION,
        "snapshot_digest": snapshot["digest"], "campaign_inputs": inputs,
        "runtime_profile_digest": _object_digest(runtime), "result": result,
    }


def _remove_orphaned_bundle(bundle_dir: Path) -> None:
    try:
        metadata = bundle_dir.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise HarnessError(f"orphaned campaign bundle is not a directory: {bundle_dir}")
    shutil.rmtree(bundle_dir)


def _prepare_state(created_at: int, inputs: dict, snapshot: dict,
                   prepare_results: dict[str, dict]) -> dict:
    return {
        "schema": 1, "created_at": created_at, "inputs": inputs,
        "campaign_contract_version": CAMPAIGN_CONTRACT_VERSION,
        "snapshot": snapshot, "profiles": sorted(prepare_results),
        "benchmark_environments": {
            profile_id: result["benchmark_environment"]
            for profile_id, result in sorted(prepare_results.items())
        },
        "prepare": prepare_results,
    }


def stage_prepare(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    calibration = load_calibration(args.calibration_tasks)
    quality = load_quality(args.quality_tasks)
    profiles = _profiles(manifest, args.profile)
    source = _source_data_dir(args.source_data)
    snapshot = _copy_snapshot(source, args.run_dir / "snapshot", args.timeout)
    _preflight_quality_targets(args.run_dir / "snapshot", quality)
    if args.expect_snapshot and snapshot["digest"] != args.expect_snapshot:
        raise HarnessError(
            f"expected snapshot {args.expect_snapshot}, got {snapshot['digest']}"
        )
    state_path = args.run_dir / "state.json"
    old_state = _read_json(state_path) if state_path.exists() else None
    inputs = _state_inputs(manifest, calibration, quality)
    if old_state is not None and not isinstance(old_state, dict):
        raise HarnessError("existing campaign state is malformed")
    old_prepare = (old_state or {}).get("prepare") or {}
    old_environments = (
        {profile_id: result.get("benchmark_environment")
         for profile_id, result in sorted(old_prepare.items())}
        if isinstance(old_prepare, dict) else None)
    if (old_state is not None and not _current_campaign_contract(
            old_state.get("campaign_contract_version"))):
        raise HarnessError(
            "existing campaign uses an older evidence contract; choose another run dir")
    if old_state is not None and (
            old_state.get("schema") != 1 or old_state.get("inputs") != inputs
            or old_state.get("snapshot") != snapshot
            or old_state.get("benchmark_environments") != old_environments):
        raise HarnessError("existing campaign state differs; choose another run dir")
    old_profiles = (old_state or {}).get("profiles") or []
    if (not isinstance(old_prepare, dict) or not isinstance(old_profiles, list)
            or set(old_profiles) != set(old_prepare)):
        raise HarnessError("existing campaign checkpoint is internally inconsistent")
    prepare_results = dict(old_prepare)
    created_at = int((old_state or {}).get("created_at", int(time.time())))
    artifact_cache = getattr(args, "artifact_cache", None)
    for profile in profiles:
        profile_id = profile["id"]
        root = _profile_root(args.run_dir, profile_id)
        data = root / "data"
        _copy_profile_data(
            args.run_dir / "snapshot", data, snapshot, args.timeout)
        profile_path = root / "profile.json"
        runtime = _runtime_profile(profile)
        _write_json(profile_path, runtime)
        env = _profile_env(args.run_dir, profile_id, profile_path)
        bundle_dir = root / "prepared-embedding-bundle"
        prior = prepare_results.get(profile_id)
        prepare_path = root / "prepare.json"
        if prior is None:
            try:
                prepare_metadata = prepare_path.lstat()
            except FileNotFoundError:
                prepare_metadata = None
            if prepare_metadata is not None:
                if not stat.S_ISREG(prepare_metadata.st_mode):
                    raise HarnessError(
                        f"campaign prepare record is not a file: {prepare_path}")
                prior = _validate_recovered_prepare(
                    _read_json(prepare_path), profile_id=profile_id, runtime=runtime,
                    snapshot=snapshot, inputs=inputs, bundle_dir=bundle_dir)
                prepare_results[profile_id] = prior
            else:
                _remove_orphaned_bundle(bundle_dir)
        model_dir = root / "models" / profile_id
        if artifact_cache is not None:
            model_metrics = _seed_artifact_cache(
                artifact_cache, model_dir, runtime)
        else:
            _prune_unreferenced_model_files(model_dir, runtime)
            model_metrics = None
        corpus = _bind_corpus(data, env, args.timeout)
        prior_corpora = {
            _object_digest(_corpus_content(row["corpus"]))
            for row in prepare_results.values()
            if isinstance(row, dict) and isinstance(row.get("corpus"), dict)
        }
        if (prior_corpora
                and _object_digest(_corpus_content(corpus)) not in prior_corpora):
            raise HarnessError(f"{profile_id} did not receive the same frozen corpus")
        if (isinstance(prior, dict) and isinstance(prior.get("corpus"), dict)
                and _corpus_content(prior["corpus"]) != _corpus_content(corpus)):
            raise HarnessError(f"{profile_id} recovered corpus metadata moved")
        ensure = _run(
            [
                sys.executable, "-c",
                "import embedder; print(embedder.ensure_model(download="
                + ("True" if args.allow_download and artifact_cache is None else "False")
                + "))",
            ],
            env=env, timeout=args.timeout,
        )
        _require_success(ensure, f"{profile_id} model verification")
        session = _run(
            [
                sys.executable, "-c",
                "import embedder,json; model=embedder.Embedder(download=False); "
                "print(json.dumps({'providers':model.sess.get_providers(),"
                "'semantic_threads':embedder._thread_budget()}))",
            ],
            env=env, timeout=args.timeout,
        )
        _require_success(session, f"{profile_id} ONNX session validation")
        session_payload = _last_json(session["stdout"], f"{profile_id} session validation")
        providers = session_payload.get("providers")
        if not isinstance(providers, list) or not all(isinstance(item, str) for item in providers):
            raise HarnessError(f"{profile_id} session omitted actual providers")
        semantic_threads = session_payload.get("semantic_threads")
        if type(semantic_threads) is not int or semantic_threads < 1:
            raise HarnessError(f"{profile_id} session omitted its semantic thread budget")
        benchmark_env = benchmark_environment(
            requested_provider=runtime.get("provider", "CPUExecutionProvider"),
            actual_providers=providers, semantic_threads=semantic_threads,
            power_policy=getattr(
                args, "power_policy", "host-default-uncontrolled"))
        if (isinstance(prior, dict)
                and prior.get("benchmark_environment") != benchmark_env):
            raise HarnessError(
                f"{profile_id} benchmark environment changed after prepare")
        _prune_unreferenced_model_files(model_dir, runtime)
        for name, artifact in runtime["files"].items():
            _verify_pinned_artifact(
                model_dir / name, artifact, "verified campaign model artifact")
        observed_model_metrics = _pinned_model_metrics(runtime, model_dir)
        if observed_model_metrics["campaign_unreferenced_model_bytes"]:
            raise HarnessError(f"{profile_id} retained undeclared model artifacts")
        if model_metrics is not None and model_metrics != observed_model_metrics:
            raise HarnessError(f"{profile_id} seeded model metrics moved during load")
        model_metrics = observed_model_metrics
        embed_record = None
        embed_stats = None
        embedding_bundle = None
        if isinstance(prior, dict) and isinstance(prior.get("embedding_bundle"), dict):
            embedding_bundle = _validate_embedding_bundle(
                bundle_dir, prior["embedding_bundle"])
            _restore_embedding_bundle(
                bundle_dir, data, embedding_bundle, env, args.timeout)
            current = _embedding_data_state(data, env, args.timeout, "freeze")
            if current != embedding_bundle["embedding"]:
                raise HarnessError(f"{profile_id} prepared embedding source moved")
            embed_record = prior.get("embed")
            embed_stats = prior.get("embed_stats")
            if not isinstance(embed_record, dict) or not isinstance(embed_stats, dict):
                raise HarnessError(f"{profile_id} prepared embedding metrics are missing")
        elif not args.no_embed:
            embed = _run(
                [sys.executable, str(PY / "embed.py"), "--full"],
                env=env, timeout=args.timeout,
            )
            _require_success(embed, f"{profile_id} full embedding")
            embed_record = {key: embed[key] for key in (
                "returncode", "wall_ms", "cpu_ms", "peak_rss_mib")}
            embed_record["benchmark_environment_digest"] = _object_digest(
                benchmark_env)
            embed_stats = _embed_stats(embed, snapshot["rows"])
            embedding_bundle = _freeze_embedding_bundle(
                data, bundle_dir, env, args.timeout)
        prepare_results[profile_id] = {
            "model": {key: ensure[key] for key in (
                "returncode", "wall_ms", "cpu_ms", "peak_rss_mib")},
            "session": {
                **{key: session[key] for key in (
                    "returncode", "wall_ms", "cpu_ms", "peak_rss_mib")},
                "actual_providers": providers,
                "semantic_threads": semantic_threads,
            },
            "embed": embed_record,
            "embed_stats": embed_stats,
            "embedding_bundle": embedding_bundle,
            "embed_skipped": embedding_bundle is None,
            "profile_digest": _object_digest(runtime),
            "model_bytes": runtime["model_bytes"],
            **model_metrics,
            "dim": runtime["dim"],
            "corpus": corpus,
            "benchmark_environment": benchmark_env,
            "benchmark_environment_digest": _object_digest(benchmark_env),
        }
        _write_json(
            prepare_path,
            _prepare_artifact(
                profile_id, runtime, snapshot, inputs, prepare_results[profile_id]))
        state = _prepare_state(created_at, inputs, snapshot, prepare_results)
        _write_json(state_path, state)
    state = _prepare_state(created_at, inputs, snapshot, prepare_results)
    print(json.dumps(state, sort_keys=True))
    return 0


def _parse_hits(stdout: str) -> list[dict]:
    rows = []
    for raw in stdout.splitlines():
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HarnessError(f"CLI emitted invalid JSON: {raw[-300:]}") from exc
        if isinstance(value, dict) and isinstance(value.get("hits"), list):
            rows.extend(item for item in value["hits"] if isinstance(item, dict))
        elif isinstance(value, dict) and value.get("kind") != "agrep-meta":
            rows.append(value)
    return rows


def _score_rows(rows: list[dict]) -> list[float]:
    scores = []
    for row in rows:
        raw = row.get("sem_score", row.get("score"))
        if isinstance(raw, (int, float)) and math.isfinite(raw):
            scores.append(float(raw))
    return sorted(scores, reverse=True)


def calibrate_scores(observations: list[dict]) -> dict:
    real = [row for row in observations if row.get("kind") == "real"]
    gibberish = [row for row in observations if row.get("kind") == "gibberish"]
    if len(real) < 7 or len(gibberish) < 4:
        raise HarnessError("calibration observations need at least 7 real and 4 gibberish")
    if any(not row.get("scores") for row in observations):
        raise HarnessError("every calibration query must return at least one raw score")
    real_top = [float(row["scores"][0]) for row in real]
    gibberish_top = [float(row["scores"][0]) for row in gibberish]
    lowest_real = min(real_top)
    highest_gibberish = max(gibberish_top)
    grid = [value / 1000.0 for value in range(-1000, 1001)]

    def admitted(row: dict, threshold: float) -> int:
        return sum(float(score) >= threshold for score in row["scores"])

    floor = next((threshold for threshold in grid
                  if sum(admitted(row, threshold) for row in gibberish) <= 3
                  and all(admitted(row, threshold) >= 1 for row in real)), None)
    strong = next((threshold for threshold in grid
                   if threshold > highest_gibberish and threshold <= lowest_real), None)
    thresholds = sorted(set(
        value for value in (floor, strong, 0.0, 0.5, 0.75, 0.8, 0.82, 0.84, 0.9)
        if value is not None
    ))
    table = []
    for threshold in thresholds:
        table.append({
            "threshold": round(threshold, 3),
            "real_rows": sum(admitted(row, threshold) for row in real),
            "real_queries": sum(admitted(row, threshold) > 0 for row in real),
            "gibberish_rows": sum(admitted(row, threshold) for row in gibberish),
            "gibberish_queries": sum(admitted(row, threshold) > 0 for row in gibberish),
        })
    return {
        "real_queries": len(real),
        "gibberish_queries": len(gibberish),
        "lowest_real_top1": round(lowest_real, 6),
        "highest_gibberish_top1": round(highest_gibberish, 6),
        "gap": round(lowest_real - highest_gibberish, 6),
        "floor": round(floor, 3) if floor is not None else None,
        "strong": round(strong, 3) if strong is not None else None,
        "rows_admitted": table,
    }


def _raw_semantic_batch(tasks: list[dict], env: dict[str, str],
                        timeout: float, maximum: int = 200) -> tuple[dict, list[dict]]:
    requests = [{"id": row["id"], "query": row["query"]} for row in tasks]
    script = """
import json
import sys
import time
import semantic

requests = json.loads(sys.argv[1])
maximum = int(sys.argv[2])
observations = []
for request in requests:
    started = time.perf_counter()
    payload = semantic.search(
        request['query'], level='message', k=maximum, refresh_if_stale=False)
    rows = payload.get('results')
    if not isinstance(rows, list):
        raise RuntimeError('semantic search omitted raw results')
    observations.append({
        'id': request['id'], 'query': request['query'], 'rows': rows,
        'exit_ms': round((time.perf_counter() - started) * 1000.0, 3),
    })
print(json.dumps({'observations': observations}, separators=(',', ':')))
"""
    result = _run(
        [sys.executable, "-c", script, json.dumps(requests), str(maximum)],
        env=env, timeout=timeout,
    )
    _require_success(result, "raw semantic calibration batch")
    payload = _last_json(result["stdout"], "raw semantic calibration batch")
    observations = payload.get("observations")
    if (not isinstance(observations, list) or len(observations) != len(tasks)
            or any(not isinstance(row, dict) or not isinstance(row.get("rows"), list)
                   for row in observations)):
        raise HarnessError("raw semantic calibration returned an invalid batch")
    return result, observations


def _query_echo(query: str, hit: dict) -> bool:
    needle = " ".join(query.casefold().split())
    if not needle:
        return False
    for key in ("text", "snippet", "summary"):
        value = hit.get(key)
        if isinstance(value, str) and needle in " ".join(value.casefold().split()):
            return True
    return False


def stage_calibrate(args: argparse.Namespace) -> int:
    state, manifest, calibration, _quality = _load_state(args)
    reports = {}
    for profile in _selected_from_state(state, manifest, args.profile):
        profile_id = profile["id"]
        root = _profile_root(args.run_dir, profile_id)
        calibrated_profile = root / "profile-calibrated.json"
        calibrated_profile.unlink(missing_ok=True)
        raw_profile = root / "profile-calibration.json"
        _write_json(raw_profile, _runtime_profile(profile, raw_bands=True))
        env = _profile_env(args.run_dir, profile_id, raw_profile)
        tagged = [
            {**task, "kind": kind}
            for kind in ("real", "gibberish") for task in calibration[kind]
        ]
        _batch_result, raw_observations = _raw_semantic_batch(
            tagged, env, args.timeout)
        raw_by_id = {row["id"]: row for row in raw_observations}
        if set(raw_by_id) != {row["id"] for row in tagged}:
            raise HarnessError("raw semantic calibration changed task identities")
        observations = []
        for task in tagged:
            raw = raw_by_id[task["id"]]
            hits = raw["rows"]
            echoes = [hit for hit in hits if _query_echo(task["query"], hit)]
            scores = _score_rows(
                [hit for hit in hits if not _query_echo(task["query"], hit)])
            observations.append({
                "id": task["id"], "kind": task["kind"], "query": task["query"],
                "scores": scores, "rows": len(hits),
                "excluded_query_echoes": len(echoes),
                "exit_ms": raw["exit_ms"],
            })
        calculated = calibrate_scores(observations)
        effective_bands, usable_bands = _effective_calibration(calculated)
        report = {
            "schema": 1,
            "campaign_contract_version": CAMPAIGN_CONTRACT_VERSION,
            "profile": profile_id,
            "snapshot_digest": state["snapshot"]["digest"],
            "task_digest": state["inputs"]["calibration_digest"],
            "campaign_inputs": state["inputs"],
            "base_profile_digest": _object_digest(_runtime_profile(profile)),
            "calibration": calculated,
            "calibration_usable": usable_bands,
            "effective_search_bands": effective_bands,
            "observations": observations,
        }
        _write_json(root / "calibration.json", report)
        _write_json(
            calibrated_profile,
            _runtime_profile(profile, calibrated=effective_bands),
        )
        reports[profile_id] = report
    print(json.dumps(reports, sort_keys=True))
    return 0


def _recall(task: dict, lane: str, env: dict[str, str], timeout: float,
            hits: int) -> dict:
    command = [
        sys.executable, str(ROOT / "cli.py"), "recall", task["query"],
        "--json", "--hits", str(hits), "--budget", str(QUALITY_OUTPUT_BUDGET),
        "--no-auto",
        "--no-self",
    ]
    if lane not in QUALITY_LANES:
        raise HarnessError(f"unsupported quality lane {lane!r}")
    command.extend(QUALITY_LANES[lane])
    result = _run(command, env=env, timeout=timeout)
    if result["returncode"] not in (0, 1):
        _require_success(result, f"{task['id']} {lane}")
    try:
        payload = json.loads(result["stdout"]) if result["stdout"].strip() else {"hits": []}
    except json.JSONDecodeError as exc:
        raise HarnessError(f"{task['id']} {lane} returned invalid JSON") from exc
    hits_value = payload.get("hits") if isinstance(payload, dict) else None
    if not isinstance(hits_value, list):
        raise HarnessError(f"{task['id']} {lane} omitted hits")
    expected_rank = _expected_rank(hits_value, task["expected"])
    compact_hits = []
    for hit in hits_value:
        evidence = " ".join(
            " ".join(str(row.get("text") or "").split())
            for row in (hit.get("window") or []) if isinstance(row, dict)
        )[:1200]
        compact_hits.append({
            "session": hit.get("session"), "turn": hit.get("turn"),
            "project": hit.get("project"), "lane": hit.get("lane"),
            "sem_score": hit.get("sem_score"), "evidence": evidence,
        })
    return {
        "task": task["id"], "lane": lane, "correct": expected_rank is not None,
        "expected_rank": expected_rank, "exit_ms": result["wall_ms"],
        "launcher_tree_cpu_ms": result["cpu_ms"],
        "launcher_tree_peak_rss_mib": result["peak_rss_mib"],
        "engine": payload.get("engine"),
        "hits": compact_hits,
    }


def _expected_rank(hits: list[dict], expected: list[str]) -> int | None:
    wanted = []
    for raw in expected:
        session, separator, turn_raw = str(raw).rpartition(":")
        if not separator:
            continue
        try:
            wanted.append((session, int(turn_raw)))
        except ValueError:
            continue
    for rank, hit in enumerate(hits, 1):
        for row in [hit, *(hit.get("window") or [])]:
            try:
                identity = (str(row.get("session") or ""), int(row.get("turn")))
            except (AttributeError, TypeError, ValueError):
                continue
            if any(identity[0].startswith(session) and identity[1] == turn
                   for session, turn in wanted):
                return rank
    return None


def quality_protocol(tasks: list[dict], hits: int) -> dict:
    value = {
        "schema": 1,
        "task_digest": _object_digest(tasks),
        "hits": hits,
        "cli": {
            "mode": "recall-json",
            "lanes": QUALITY_LANES,
            "output_budget": QUALITY_OUTPUT_BUDGET,
            "output_cap": hits,
            "auto_escalation": False,
        },
        "exclusions": {"self": True, "query_echo": False},
    }
    validate_quality_protocol(value, task_digest=_object_digest(tasks), hits=hits)
    return value


def validate_quality_protocol(value: Any, *, task_digest: str | None = None,
                              hits: int | None = None) -> dict:
    if not isinstance(value, dict):
        raise HarnessError("quality protocol must be an object")
    _require_keys(value, {"schema", "task_digest", "hits", "cli", "exclusions"},
                  set(), "quality protocol")
    if type(value["schema"]) is not int or value["schema"] != 1:
        raise HarnessError("quality protocol schema must be 1")
    if (not isinstance(value["task_digest"], str)
            or not SHA256_RE.fullmatch(value["task_digest"])
            or (task_digest is not None and value["task_digest"] != task_digest)
            or type(value["hits"]) is not int or not 1 <= value["hits"] <= 20
            or (hits is not None and value["hits"] != hits)):
        raise HarnessError("quality protocol task or hit binding is invalid")
    cli = value["cli"]
    exclusions = value["exclusions"]
    if not isinstance(cli, dict) or not isinstance(exclusions, dict):
        raise HarnessError("quality protocol sections must be objects")
    _require_keys(cli, {
        "mode", "lanes", "output_budget", "output_cap", "auto_escalation",
    }, set(), "quality CLI protocol")
    _require_keys(exclusions, {"self", "query_echo"}, set(),
                  "quality exclusion protocol")
    if (cli["mode"] != "recall-json" or cli["lanes"] != QUALITY_LANES
            or cli["output_budget"] != QUALITY_OUTPUT_BUDGET
            or cli["output_cap"] != value["hits"]
            or cli["auto_escalation"] is not False
            or exclusions != {"self": True, "query_echo": False}):
        raise HarnessError("quality protocol does not match the canonical CLI lane")
    return value


def validate_quality_artifact_protocol(report: Any,
                                       task_digest: str | None = None) -> dict:
    if not isinstance(report, dict):
        raise HarnessError("quality artifact must be an object")
    if not _current_campaign_contract(report.get("campaign_contract_version")):
        raise HarnessError("quality artifact campaign contract is stale")
    protocol = validate_quality_protocol(
        report.get("protocol"), task_digest=task_digest)
    if (report.get("protocol_digest") != _object_digest(protocol)
            or report.get("task_digest") != protocol["task_digest"]):
        raise HarnessError("quality artifact protocol binding is stale")
    return protocol


def stage_quality(args: argparse.Namespace) -> int:
    state, manifest, _calibration, quality = _load_state(args)
    protocol = quality_protocol(quality, args.hits)
    protocol_digest = _object_digest(protocol)
    profiles = _selected_from_state(state, manifest, args.profile)
    for profile in profiles:
        output = _profile_root(args.run_dir, profile["id"]) / "quality.json"
        if output.is_file():
            existing = _read_json(output)
            validate_quality_artifact_protocol(
                existing, state["inputs"]["quality_digest"])
            if (existing.get("protocol") != protocol
                    or existing.get("protocol_digest") != protocol_digest):
                raise HarnessError(
                    f"{profile['id']} quality protocol override differs from resume")
    reports = {}
    for profile in profiles:
        profile_id = profile["id"]
        root = _profile_root(args.run_dir, profile_id)
        _calibration_report, profile_path, runtime_digest = (
            _validated_calibrated_profile(root, profile, state))
        output = root / "quality.json"
        if output.is_file():
            existing = _read_profile_artifact(
                root, "quality.json", state, profile_id, {
                    "task_digest": state["inputs"]["quality_digest"],
                    "runtime_profile_digest": runtime_digest,
                    "protocol": protocol,
                    "protocol_digest": protocol_digest,
                })
            validate_quality_artifact_protocol(
                existing, state["inputs"]["quality_digest"])
            reports[profile_id] = existing
            continue
        env = _profile_env(args.run_dir, profile_id, profile_path)
        outcomes = []
        try:
            for task in quality:
                outcomes.append(_recall(task, "semantic", env, args.timeout, args.hits))
                outcomes.append(_recall(task, "hybrid", env, args.timeout, args.hits))
        finally:
            _stop_worker(env)
        lane_scores = {}
        for lane in ("semantic", "hybrid"):
            rows = [row for row in outcomes if row["lane"] == lane]
            lane_scores[lane] = {
                "correct": sum(bool(row["correct"]) for row in rows),
                "total": len(rows),
                "exit_ms": [row["exit_ms"] for row in rows],
            }
        report = {
            "schema": 1, "profile": profile_id,
            "campaign_contract_version": CAMPAIGN_CONTRACT_VERSION,
            "snapshot_digest": state["snapshot"]["digest"],
            "task_digest": state["inputs"]["quality_digest"],
            "campaign_inputs": state["inputs"],
            "runtime_profile_digest": runtime_digest,
            "protocol": protocol, "protocol_digest": protocol_digest,
            "scores": lane_scores, "outcomes": outcomes,
        }
        validate_quality_artifact_protocol(
            report, state["inputs"]["quality_digest"])
        _write_json(output, report)
        reports[profile_id] = report
    print(json.dumps(reports, sort_keys=True))
    return 0


def index_projection(dim: int, rows: int = TEN_MILLION) -> dict[str, int]:
    if not isinstance(dim, int) or dim <= 0 or not isinstance(rows, int) or rows <= 0:
        raise HarnessError("projection dimensions and rows must be positive integers")
    q8 = 64 + rows * (dim + 4)
    groups = 64 + rows * 4
    exact_f16 = rows * dim * 2
    return {
        "rows": rows,
        "f32_bytes": rows * dim * 4,
        "q8_bytes": q8,
        "group_bytes": groups,
        "exact_f16_bytes": exact_f16,
        "q8_plus_group_bytes": q8 + groups,
        "q8_plus_exact_f16_bytes": q8 + exact_f16,
        "materialized_q8_group_f16_bytes": q8 + groups + exact_f16,
    }


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * fraction
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return round(ordered[low], 3)
    value = ordered[low] + (ordered[high] - ordered[low]) * (position - low)
    return round(value, 3)


def _embed_stats(result: dict, fallback_rows: int, *, incremental: bool = False) -> dict:
    log = result["stdout"] + "\n" + result["stderr"]
    done = EMBED_DONE_RE.search(log)
    phases = EMBED_PHASES_RE.search(log)
    dedupe = DEDUPE_RE.search(log)
    if done is None or phases is None:
        raise HarnessError("embedding run omitted its done/phases telemetry")
    published_rows = int(done.group(1)) if done else fallback_rows
    rows = fallback_rows if incremental else published_rows
    elapsed_s = float(done.group(2))
    phase_values = [float(phases.group(index)) for index in range(1, 5)]
    inference_s = phase_values[2]
    inferred = int(dedupe.group(1)) if dedupe else rows
    reused = int(dedupe.group(2)) if dedupe else max(0, rows - inferred)
    return {
        "rows": rows, "published_rows": published_rows,
        "inferred": inferred, "reused": reused,
        "elapsed_s": round(elapsed_s, 3),
        "phases_s": dict(zip(
            ("plan", "load", "inference", "f32_publish"), phase_values)),
        "inference_s": round(inference_s, 3),
        "raw_rows_per_s": round(inferred / inference_s, 3) if inference_s else None,
        "dedup_effective_rows_per_s": round(rows / inference_s, 3) if inference_s else None,
    }


def _count_lines(path: Path) -> int:
    count = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            count += chunk.count(b"\n")
    return count


def _incremental_projection_measurement(
        data: Path, stats: dict, added_rows: int) -> dict:
    sources = [
        path for path in (data / "messages.jsonl", data / "replies.jsonl")
        if path.is_file()
    ]
    artifacts = {
        "f32_bytes": data / "embeddings.f32",
        "ids_bytes": data / "embeddings.ids",
        "hashes_bytes": data / "embeddings.hashes",
    }
    if not sources or any(not path.is_file() for path in artifacts.values()):
        raise HarnessError("incremental projection generation is incomplete")
    return {
        "added_rows": added_rows,
        "published_rows": int(stats["published_rows"]),
        "inferred_rows": int(stats["inferred"]),
        "source_rows": sum(_count_lines(path) for path in sources),
        "source_bytes": sum(path.stat().st_size for path in sources),
        **{name: path.stat().st_size for name, path in artifacts.items()},
        "phases_s": stats["phases_s"],
    }


def _q8_scale_cache_path(run_dir: Path, dim: int) -> Path:
    return run_dir.resolve() / "q8-scale" / f"dim-{dim}.json"


def _q8_scale_cache_record(
        report: Any, *, dim: int, code_digest: str,
        benchmark_env: dict) -> dict:
    environment = validate_benchmark_environment(benchmark_env)
    provenance = report.get("provenance") if isinstance(report, dict) else None
    if (type(dim) is not int or dim < 1
            or not isinstance(code_digest, str)
            or not SHA256_RE.fullmatch(code_digest)
            or not isinstance(provenance, dict)):
        raise HarnessError("q8 scale cache provenance is invalid")
    required = {
        "os", "machine", "cpu", "logical_cpus", "physical_memory_bytes",
        "python", "numpy", "git_commit", "git_dirty", "rust_binary",
        "rust_binary_sha256",
    }
    if (required - set(provenance)
            or not isinstance(provenance.get("rust_binary"), str)
            or not Path(provenance["rust_binary"]).is_absolute()
            or not isinstance(provenance.get("rust_binary_sha256"), str)
            or not SHA256_RE.fullmatch(provenance["rust_binary_sha256"])
            or not isinstance(provenance.get("git_dirty"), bool)
            or provenance.get("python") != environment["runtime"]["python_version"]
            or provenance.get("numpy") != environment["runtime"]["numpy_version"]
            or str(provenance.get("machine", "")).lower()
            != environment["host"]["architecture"]
            or type(provenance.get("logical_cpus")) is not int
            or provenance["logical_cpus"]
            != environment["host"]["logical_cores"]):
        raise HarnessError("q8 scale runtime or binary provenance is invalid")
    return {
        "schema": 2,
        "code_digest": code_digest,
        "dim": dim,
        "benchmark_environment": environment,
        "benchmark_environment_digest": _object_digest(environment),
        "scale_provenance": provenance,
        "report": report,
        "report_digest": _object_digest(report),
    }


def _read_q8_scale_cache_record(
        run_dir: Path, dim: int, benchmark_env: dict,
        code_digest: str) -> tuple[dict, Path, str]:
    path = _q8_scale_cache_path(run_dir, dim)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise HarnessError(f"canonical q8 scale cache is missing: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise HarnessError(f"canonical q8 scale cache is not a regular file: {path}")
    before = _file_digest(path)
    record = _read_json(path)
    after = _file_digest(path)
    if before != after:
        raise HarnessError(f"canonical q8 scale cache moved while reading: {path}")
    if (not isinstance(record, dict)
            or record != _q8_scale_cache_record(
                record.get("report"), dim=dim, code_digest=code_digest,
                benchmark_env=benchmark_env)):
        raise HarnessError(f"canonical q8 scale cache drifted at {path}")
    return record, path, after


def _q8_scale_record(
        run_dir: Path, dim: int, timeout: float,
        benchmark_env: dict) -> tuple[dict, Path, str]:
    path = _q8_scale_cache_path(run_dir, dim)
    code_digest = _code_digest()
    if path.is_file():
        return _read_q8_scale_cache_record(
            run_dir, dim, benchmark_env, code_digest)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PY)
    env.pop("AGREP_BENCH_EMBED_PROFILE", None)
    result = _run([
        sys.executable, str(Path(__file__).with_name("semantic_q8_scale.py")),
        "--rows", *(str(row) for row in Q8_SCALE_ROWS),
        "--dim", str(dim), "--topup-rows", str(Q8_SCALE_TOP_UP_ROWS), "--json",
    ], env=env, timeout=timeout)
    _require_success(result, f"{dim}-dimension q8 scale campaign")
    report = _last_json(result["stdout"], f"{dim}-dimension q8 scale campaign")
    binary = ROOT / "target" / "release" / (
        "agrep-rs.exe" if os.name == "nt" else "agrep-rs")
    if (report.get("schema") != 2
            or report.get("provenance", {}).get("rust_binary_sha256")
            != _file_digest(binary)):
        raise HarnessError(f"{dim}-dimension q8 scale provenance is invalid")
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(path, _q8_scale_cache_record(
        report, dim=dim, code_digest=code_digest,
        benchmark_env=benchmark_env))
    return _read_q8_scale_cache_record(
        run_dir, dim, benchmark_env, code_digest)


def _representative_top_up_texts(
        data: Path, count: int, snapshot_digest: str) -> tuple[list[str], int]:
    seed = int(hashlib.sha256(
        f"top-up:{snapshot_digest}".encode("ascii")).hexdigest()[:16], 16)
    rng = random.Random(seed)
    reservoir = []
    seen = 0
    for name, field in (("messages.jsonl", "text"), ("replies.jsonl", "reply")):
        path = data / name
        if not path.is_file():
            continue
        with path.open(encoding="utf-8") as stream:
            for raw in stream:
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                text = row.get(field) if isinstance(row, dict) else None
                if not isinstance(text, str) or not text.strip():
                    continue
                seen += 1
                if len(reservoir) < count:
                    reservoir.append(text)
                    continue
                replacement = rng.randrange(seen)
                if replacement < count:
                    reservoir[replacement] = text
    if not reservoir:
        raise HarnessError("top-up needs at least one frozen message or reply text")
    rng.shuffle(reservoir)
    return [reservoir[index % len(reservoir)] for index in range(count)], seen


def _append_top_up(data: Path, count: int, snapshot_digest: str) -> dict:
    if count <= 0:
        raise HarnessError("top-up row count must be positive")
    texts, source_rows = _representative_top_up_texts(data, count, snapshot_digest)
    session = "embedder-selection-top-up"
    started = 2_000_000_000_000
    with (data / "messages.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
        for turn, source_text in enumerate(texts):
            marker = f"[agrep semantic top-up {snapshot_digest[:12]}:{turn}]"
            stream.write(json.dumps({
                "id": f"bench:{session}:{turn}", "agent": "bench",
                "project": "/embedder-selection", "session": session,
                "ts": started + turn, "turn": turn, "who": "user",
                "model": "fixture", "model_source": "explicit",
                "text": f"{source_text}\n{marker}",
            }, separators=(",", ":")) + "\n")
    with (data / "sessions.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps({
            "session": session, "agent": "bench", "project": "/embedder-selection",
            "n": count, "first_ts": started, "last_ts": started + count - 1,
            "first_text": texts[0][:240],
        }, separators=(",", ":")) + "\n")
    (data / ".ingest.sig").write_text(
        f"embedder-selection-top-up-{snapshot_digest}-{count}\n",
        encoding="utf-8", newline="\n",
    )
    return {
        "source_rows": source_rows,
        "sampled_rows": count,
        "sample_digest": _object_digest(texts),
        "method": "deterministic row-uniform reservoir over frozen messages and replies",
    }


def performance_protocol(args: argparse.Namespace, query_tasks: list[dict],
                         profile_root: Path) -> dict:
    root = profile_root.resolve()
    run_dir = args.run_dir.resolve()
    value = {
        "schema": 1,
        "query_count": len(query_tasks),
        "query_task_digest": _object_digest(query_tasks),
        "query_ids": [task["id"] for task in query_tasks],
        "lanes": QUALITY_LANES,
        "query_hits": 3,
        "output_budget": QUALITY_OUTPUT_BUDGET,
        "warmup_sample_policy": {
            "worker_reset_per_lane": True,
            "cold_samples_per_lane": 1,
            "warm_samples_per_lane": len(query_tasks) - 1,
            "order": "frozen-task-order",
        },
        "top_up_rows": args.top_up_rows,
        "scale": {
            "rows": list(Q8_SCALE_ROWS),
            "top_up_rows": Q8_SCALE_TOP_UP_ROWS,
        },
        "resource_sample_interval_s": SEMANTIC_RESOURCE_SAMPLE_S,
        "paths": {
            "run_dir": str(run_dir),
            "snapshot": str((run_dir / "snapshot").resolve()),
            "profile_root": str(root),
            "prepared_bundle": str((root / "prepared-embedding-bundle").resolve()),
            "model_root": str((root / "models").resolve()),
        },
    }
    validate_performance_protocol(
        value, query_tasks=query_tasks, top_up_rows=args.top_up_rows,
        profile_root=root, run_dir=run_dir)
    return value


def validate_performance_protocol(
        value: Any, *, query_tasks: list[dict] | None = None,
        top_up_rows: int | None = None, profile_root: Path | None = None,
        run_dir: Path | None = None) -> dict:
    if not isinstance(value, dict):
        raise HarnessError("performance protocol must be an object")
    _require_keys(value, {
        "schema", "query_count", "query_task_digest", "query_ids", "lanes",
        "query_hits", "output_budget", "warmup_sample_policy", "top_up_rows",
        "scale", "resource_sample_interval_s", "paths",
    }, set(), "performance protocol")
    if type(value["schema"]) is not int or value["schema"] != 1:
        raise HarnessError("performance protocol schema must be 1")
    count = value["query_count"]
    ids = value["query_ids"]
    if (type(count) is not int or count < 2
            or not isinstance(ids, list) or len(ids) != count
            or len(set(ids)) != count
            or any(not isinstance(item, str) or not item for item in ids)
            or not isinstance(value["query_task_digest"], str)
            or not SHA256_RE.fullmatch(value["query_task_digest"])):
        raise HarnessError("performance query protocol is invalid")
    if query_tasks is not None and (
            count != len(query_tasks)
            or ids != [task["id"] for task in query_tasks]
            or value["query_task_digest"] != _object_digest(query_tasks)):
        raise HarnessError("performance query tasks differ from the frozen protocol")
    policy = value["warmup_sample_policy"]
    if (not isinstance(policy, dict) or policy != {
            "worker_reset_per_lane": True,
            "cold_samples_per_lane": 1,
            "warm_samples_per_lane": count - 1,
            "order": "frozen-task-order",
            }):
        raise HarnessError("performance warmup/sample policy is invalid")
    scale = value["scale"]
    if (value["lanes"] != QUALITY_LANES or value["query_hits"] != 3
            or value["output_budget"] != QUALITY_OUTPUT_BUDGET
            or type(value["top_up_rows"]) is not int
            or value["top_up_rows"] < 1
            or (top_up_rows is not None and value["top_up_rows"] != top_up_rows)
            or scale != {"rows": list(Q8_SCALE_ROWS),
                         "top_up_rows": Q8_SCALE_TOP_UP_ROWS}
            or value["resource_sample_interval_s"]
            != SEMANTIC_RESOURCE_SAMPLE_S):
        raise HarnessError("performance measurement policy is invalid")
    paths = value["paths"]
    if (not isinstance(paths, dict) or set(paths) != {
            "run_dir", "snapshot", "profile_root", "prepared_bundle", "model_root"}
            or any(not isinstance(item, str) or not Path(item).is_absolute()
                   for item in paths.values())):
        raise HarnessError("performance protocol paths must be absolute")
    if run_dir is not None and paths["run_dir"] != str(run_dir.resolve()):
        raise HarnessError("performance protocol run directory drifted")
    if profile_root is not None and paths["profile_root"] \
            != str(profile_root.resolve()):
        raise HarnessError("performance protocol profile root drifted")
    return value


def performance_protocol_comparison(value: Any) -> dict:
    protocol = validate_performance_protocol(value)
    return {key: row for key, row in protocol.items() if key != "paths"}


def validate_performance_artifact_protocol(
        report: Any, prepared: dict, *,
        expected_code_digest: str | None = None) -> dict:
    if not isinstance(report, dict):
        raise HarnessError("performance artifact must be an object")
    if not isinstance(prepared, dict):
        raise HarnessError("performance artifact needs its prepared measurement")
    if not _current_campaign_contract(report.get("campaign_contract_version")):
        raise HarnessError("performance artifact campaign contract is stale")
    protocol = validate_performance_protocol(report.get("protocol"))
    benchmark_env = validate_benchmark_environment(
        report.get("benchmark_environment"))
    if (report.get("protocol_digest") != _object_digest(protocol)
            or report.get("query_task_digest")
            != protocol["query_task_digest"]
            or report.get("benchmark_environment_digest")
            != _object_digest(benchmark_env)):
        raise HarnessError("performance artifact protocol binding is stale")
    if (benchmark_env != prepared.get("benchmark_environment")
            or report.get("benchmark_environment_digest")
            != prepared.get("benchmark_environment_digest")):
        raise HarnessError("performance artifact environment differs from prepare")
    exits = report.get("query_exit_ms")
    if not isinstance(exits, dict):
        raise HarnessError("performance query samples are missing")
    expected_warm = protocol["query_count"] - 1
    for lane in QUALITY_LANES:
        row = exits.get(lane)
        if (not isinstance(row, dict) or not isinstance(row.get("cold"), dict)
                or not isinstance(row.get("warm_samples"), list)
                or len(row["warm_samples"]) != expected_warm):
            raise HarnessError(f"performance {lane} sample count is invalid")
        samples = row["warm_samples"]
        try:
            values = [float(sample["exit_ms"]) for sample in samples]
        except (KeyError, TypeError, ValueError) as exc:
            raise HarnessError(
                f"performance {lane} samples are malformed") from exc
        if (any(not math.isfinite(value) or value < 0 for value in values)
                or row.get("warm_median") != _percentile(values, 0.5)
                or row.get("warm_p90") != _percentile(values, 0.9)
                or row.get("warm_max") != round(max(values), 3)):
            raise HarnessError(f"performance {lane} warm summary is stale")
    top_up = report.get("top_up")
    expected_top_up = protocol["top_up_rows"]
    if (not isinstance(top_up, dict)
            or type(top_up.get("added_rows")) is not int
            or top_up.get("added_rows") != expected_top_up
            or type(top_up.get("rows")) is not int
            or top_up.get("rows") != expected_top_up
            or type(top_up.get("fixture", {}).get("sampled_rows")) is not int
            or top_up.get("fixture", {}).get("sampled_rows") != expected_top_up
            or top_up.get("projection_basis", {}).get("added_rows")
            != expected_top_up):
        raise HarnessError("performance top-up count is not protocol-bound")
    current_projection = report.get("current_layout_top_up_projection_10m")
    full_projection = report.get("full_rebuild_projection_10m")
    q8_evidence = report.get("q8_scale_evidence")
    if (not isinstance(q8_evidence, dict)
            or set(q8_evidence) != {
                "cache_path", "cache_sha256", "record", "record_digest"}
            or not isinstance(q8_evidence.get("cache_sha256"), str)
            or not SHA256_RE.fullmatch(q8_evidence["cache_sha256"])
            or not isinstance(q8_evidence.get("record_digest"), str)
            or not SHA256_RE.fullmatch(q8_evidence["record_digest"])
            or q8_evidence["record_digest"]
            != _object_digest(q8_evidence.get("record"))):
        raise HarnessError("performance q8 scale evidence is missing or stale")
    code_digest = expected_code_digest
    if code_digest is None:
        code_digest = report.get("campaign_inputs", {}).get("code_digest")
    try:
        canonical_record, canonical_path, canonical_sha = (
            _read_q8_scale_cache_record(
                Path(protocol["paths"]["run_dir"]), prepared["dim"],
                benchmark_env, code_digest))
    except (KeyError, TypeError) as exc:
        raise HarnessError("performance q8 cache binding is malformed") from exc
    if (q8_evidence["cache_path"] != str(canonical_path)
            or q8_evidence["cache_sha256"] != canonical_sha
            or q8_evidence["record"] != canonical_record):
        raise HarnessError("performance q8 evidence differs from canonical cache")
    try:
        recomputed_current = _projection_module().project_current_layout(
            dim=prepared["dim"], target_rows=TEN_MILLION,
            measured_incremental=top_up["projection_basis"],
            q8_scale_report=canonical_record["report"])
        if current_projection != recomputed_current:
            raise HarnessError("current-layout projection is stale")
        recomputed_full = _projection_module().project_full_rebuild(
            dim=prepared["dim"], target_rows=TEN_MILLION,
            measured_full={
                "published_rows": prepared["embed_stats"]["published_rows"],
                "inferred_rows": prepared["embed_stats"]["inferred"],
                "phases_s": prepared["embed_stats"]["phases_s"],
            },
            accelerator_projection=recomputed_current)
    except (KeyError, TypeError, ValueError) as exc:
        raise HarnessError("performance projection inputs are malformed") from exc
    if full_projection != recomputed_full:
        raise HarnessError("full-rebuild projection is stale")
    return protocol


def stage_performance(args: argparse.Namespace) -> int:
    state, manifest, calibration, quality = _load_state(args)
    query_tasks = calibration["real"][:args.queries]
    if len(query_tasks) != args.queries:
        raise HarnessError("performance query count exceeds the frozen task set")
    profiles = _selected_from_state(state, manifest, args.profile)
    for profile in profiles:
        root = _profile_root(args.run_dir, profile["id"])
        output = root / "performance.json"
        if not output.is_file():
            continue
        protocol = performance_protocol(args, query_tasks, root)
        existing = _read_json(output)
        validate_performance_protocol(
            existing.get("protocol"), query_tasks=query_tasks,
            top_up_rows=args.top_up_rows, profile_root=root, run_dir=args.run_dir)
        prepared = state.get("prepare", {}).get(profile["id"])
        if not isinstance(prepared, dict):
            raise HarnessError(f"{profile['id']} prepared result is missing")
        validate_performance_artifact_protocol(
            existing, prepared,
            expected_code_digest=state["inputs"]["code_digest"])
        if (existing.get("protocol") != protocol
                or existing.get("protocol_digest") != _object_digest(protocol)):
            raise HarnessError(
                f"{profile['id']} performance protocol override differs from resume")
    reports = {}
    for profile in profiles:
        profile_id = profile["id"]
        root = _profile_root(args.run_dir, profile_id)
        protocol = performance_protocol(args, query_tasks, root)
        protocol_digest = _object_digest(protocol)
        _calibration_report, profile_path, runtime_digest = (
            _validated_calibrated_profile(root, profile, state))
        prepared = state.get("prepare", {}).get(profile_id)
        if (not isinstance(prepared, dict) or prepared.get("embed_skipped")
                or not isinstance(prepared.get("embedding_bundle"), dict)
                or not isinstance(prepared.get("embed_stats"), dict)
                or not isinstance(prepared.get("embed"), dict)):
            raise HarnessError(
                f"{profile_id} performance requires a prepared full embedding bundle")
        validate_benchmark_environment(prepared.get("benchmark_environment"))
        current_environment = benchmark_environment(
            requested_provider=profile["runtime_profile"].get(
                "provider", "CPUExecutionProvider"),
            actual_providers=prepared["session"]["actual_providers"],
            semantic_threads=prepared["session"]["semantic_threads"],
            power_policy=getattr(
                args, "power_policy", "host-default-uncontrolled"))
        if current_environment != prepared["benchmark_environment"]:
            raise HarnessError(
                f"{profile_id} performance environment differs from full embed")
        output = root / "performance.json"
        if output.is_file():
            existing = _read_profile_artifact(
                root, "performance.json", state, profile_id, {
                    "runtime_profile_digest": runtime_digest,
                    "protocol": protocol,
                    "protocol_digest": protocol_digest,
                    "benchmark_environment": current_environment,
                    "benchmark_environment_digest": _object_digest(
                        current_environment),
                })
            validate_performance_protocol(
                existing.get("protocol"), query_tasks=query_tasks,
                top_up_rows=args.top_up_rows, profile_root=root,
                run_dir=args.run_dir)
            validate_performance_artifact_protocol(
                existing, prepared,
                expected_code_digest=state["inputs"]["code_digest"])
            reports[profile_id] = existing
            continue
        bundle_dir = root / "prepared-embedding-bundle"
        embedding_bundle = _validate_embedding_bundle(
            bundle_dir, prepared["embedding_bundle"])
        env = _profile_env(args.run_dir, profile_id, profile_path)
        exits = {"semantic": [], "hybrid": []}
        for lane in exits:
            _stop_worker(env)
            try:
                for task in query_tasks:
                    synthetic = {
                        "id": task["id"], "query": task["query"],
                        "expected": ["__latency_only_no_expected_session__"],
                    }
                    measured = _recall(synthetic, lane, env, args.timeout, 3)
                    exits[lane].append({
                        "exit_ms": measured["exit_ms"],
                        "launcher_tree_cpu_ms": measured[
                            "launcher_tree_cpu_ms"],
                        "launcher_tree_peak_rss_mib": measured[
                            "launcher_tree_peak_rss_mib"],
                        "worker": _worker_resources(env),
                    })
            finally:
                _stop_worker(env)
        with tempfile.TemporaryDirectory(
                prefix=f"performance-{profile_id}-", dir=root) as temporary:
            performance_data = Path(temporary) / "data"
            _copy_profile_data(
                args.run_dir / "snapshot", performance_data, state["snapshot"],
                args.timeout)
            performance_env = _profile_env(
                args.run_dir, profile_id, profile_path, data_dir=performance_data)
            performance_corpus = _bind_corpus(
                performance_data, performance_env, args.timeout)
            if (_corpus_content(performance_corpus)
                    != _corpus_content(state["prepare"][profile_id]["corpus"])):
                raise HarnessError(
                    f"{profile_id} performance clone changed the frozen corpus")
            restored = _restore_embedding_bundle(
                bundle_dir, performance_data, embedding_bundle,
                performance_env, args.timeout)
            if int(restored.get("rows") or 0) <= 0:
                raise HarnessError(f"{profile_id} top-up started from an empty bundle")
            top_up_fixture = _append_top_up(
                performance_data, args.top_up_rows, state["snapshot"]["digest"])
            top_up = _run(
                [sys.executable, str(PY / "embed.py")],
                env=performance_env, timeout=args.timeout,
            )
            _require_success(top_up, f"{profile_id} incremental top-up")
            top_up_stats = _embed_stats(top_up, args.top_up_rows, incremental=True)
            incremental_measurement = _incremental_projection_measurement(
                performance_data, top_up_stats, args.top_up_rows)
            semantic_resources = _measure_semantic_resources(
                performance_data, performance_env, root / "models")
        dim = profile["runtime_profile"]["dim"]
        projection = index_projection(dim)
        scale_record, scale_path, scale_sha = _q8_scale_record(
            args.run_dir, dim, args.timeout, current_environment)
        scale_report = scale_record["report"]
        try:
            top_up_projection = _projection_module().project_current_layout(
                dim=dim, target_rows=TEN_MILLION,
                measured_incremental=incremental_measurement,
                q8_scale_report=scale_report)
            full_projection = _projection_module().project_full_rebuild(
                dim=dim, target_rows=TEN_MILLION,
                measured_full={
                    "published_rows": prepared["embed_stats"]["published_rows"],
                    "inferred_rows": prepared["embed_stats"]["inferred"],
                    "phases_s": prepared["embed_stats"]["phases_s"],
                },
                accelerator_projection=top_up_projection)
        except ValueError as exc:
            raise HarnessError(f"{profile_id} 10M projection failed: {exc}") from exc
        report = {
            "schema": 1, "profile": profile_id,
            "campaign_contract_version": CAMPAIGN_CONTRACT_VERSION,
            "snapshot_digest": state["snapshot"]["digest"],
            "campaign_inputs": state["inputs"],
            "runtime_profile_digest": runtime_digest,
            "query_task_digest": _object_digest(query_tasks),
            "protocol": protocol, "protocol_digest": protocol_digest,
            "benchmark_environment": current_environment,
            "benchmark_environment_digest": _object_digest(current_environment),
            "requested_provider": profile["runtime_profile"].get(
                "provider", "CPUExecutionProvider"),
            "actual_session_providers": state["prepare"][profile_id]["session"][
                "actual_providers"],
            "session_load_peak_rss_mib": state["prepare"][profile_id]["session"][
                "peak_rss_mib"],
            "accelerator_used": any(
                provider != "CPUExecutionProvider"
                for provider in state["prepare"][profile_id]["session"][
                    "actual_providers"]
            ),
            "query_exit_ms": {
                lane: {
                    "cold": values[0] if values else None,
                    "warm_samples": values[1:],
                    "warm_median": _percentile(
                        [row["exit_ms"] for row in values[1:]], 0.5),
                    "warm_p90": _percentile(
                        [row["exit_ms"] for row in values[1:]], 0.9),
                    "warm_max": (round(max(row["exit_ms"] for row in values[1:]), 3)
                                 if len(values) > 1 else None),
                } for lane, values in exits.items()
            },
            "query_resource_scope": (
                "launcher process tree; detached resident worker RSS is recorded "
                "inside each sample"
            ),
            "full_rebuild": {
                **prepared["embed_stats"],
                "measured_stage": "prepare",
                "wall_ms": prepared["embed"]["wall_ms"],
                "cpu_ms": prepared["embed"]["cpu_ms"],
                "peak_rss_mib": prepared["embed"]["peak_rss_mib"],
                "projected_10m_inference_only_s": round(
                    TEN_MILLION
                    / prepared["embed_stats"]["dedup_effective_rows_per_s"], 3
                ) if prepared["embed_stats"]["dedup_effective_rows_per_s"] else None,
            },
            "top_up": {
                **top_up_stats,
                "measured": True,
                "base_rows": state["snapshot"]["rows"],
                "added_rows": args.top_up_rows,
                "fixture": top_up_fixture,
                "projection_basis": incremental_measurement,
                "scope": (
                    f"measured {args.top_up_rows}-row delta against the frozen "
                    "current corpus"),
                "wall_ms": top_up["wall_ms"], "cpu_ms": top_up["cpu_ms"],
                "peak_rss_mib": top_up["peak_rss_mib"],
            },
            "model_bytes": prepared["model_graph_bytes"],
            "model_graph_bytes": prepared["model_graph_bytes"],
            "pinned_cache_bytes": prepared["pinned_cache_bytes"],
            "campaign_unreferenced_model_bytes": prepared[
                "campaign_unreferenced_model_bytes"],
            "model_cache_bytes": prepared["pinned_cache_bytes"],
            "index_projection_10m": projection,
            "q8_scale_evidence": {
                "cache_path": str(scale_path),
                "cache_sha256": scale_sha,
                "record": scale_record,
                "record_digest": _object_digest(scale_record),
            },
            "current_layout_top_up_projection_10m": top_up_projection,
            "full_rebuild_projection_10m": full_projection,
            "semantic_resources": semantic_resources,
            "platform": {"os": sys.platform, "python": sys.version.split()[0]},
            "quality_fixture_tasks": len(quality),
        }
        validate_performance_artifact_protocol(
            report, prepared,
            expected_code_digest=state["inputs"]["code_digest"])
        _write_json(output, report)
        reports[profile_id] = report
    print(json.dumps(reports, sort_keys=True))
    return 0


def _blind_digest(packet: dict) -> str:
    unsigned = {key: value for key, value in packet.items() if key != "blind_digest"}
    return _object_digest(unsigned)


def _selected_quality_contract(
        run_dir: Path, state: dict, profiles: list[dict]) -> tuple[dict, dict, str]:
    records = {}
    shared_protocol = None
    shared_digest = None
    for profile in profiles:
        profile_id = profile["id"]
        root = _profile_root(run_dir, profile_id)
        quality_path = root / "quality.json"
        before = _file_digest(quality_path)
        _calibration_report, _profile_path, runtime_digest = (
            _validated_calibrated_profile(root, profile, state))
        report = _read_profile_artifact(
            root, "quality.json", state, profile_id, {
                "task_digest": state["inputs"]["quality_digest"],
                "runtime_profile_digest": runtime_digest,
            },
        )
        protocol = validate_quality_artifact_protocol(
            report, state["inputs"]["quality_digest"])
        after = _file_digest(quality_path)
        if before != after:
            raise HarnessError(f"{profile_id} quality artifact moved while reading")
        digest = report["protocol_digest"]
        if shared_protocol is None:
            shared_protocol, shared_digest = protocol, digest
        elif protocol != shared_protocol or digest != shared_digest:
            raise HarnessError("selected profiles used different quality protocols")
        records[profile_id] = {
            "artifact": report, "protocol": protocol,
            "runtime_profile_digest": runtime_digest,
            "content_sha256": after,
        }
    if shared_protocol is None or shared_digest is None:
        raise HarnessError("selected profiles have no quality protocol")
    return records, shared_protocol, shared_digest


def _blind_protocol_bound(
        value: Any, protocol: dict, digest: str,
        quality_hashes: dict[str, str]) -> bool:
    if not isinstance(value, dict):
        return False
    hashes = value.get("quality_artifact_sha256")
    return (value.get("quality_protocol") == protocol
            and value.get("quality_protocol_digest") == digest
            and value.get("quality_artifact_digest")
            == _object_digest(quality_hashes)
            and (hashes is None or hashes == quality_hashes))


def stage_blind(args: argparse.Namespace) -> int:
    state, manifest, _calibration, quality = _load_state(args)
    profiles = _selected_from_state(state, manifest, args.profile)
    profile_ids = sorted(profile["id"] for profile in profiles)
    if len(profiles) < 2 or sum(profile["baseline"] for profile in profiles) != 1:
        raise HarnessError("blind review needs the baseline and at least one candidate")
    quality_by_profile, quality_protocol_value, quality_protocol_digest = (
        _selected_quality_contract(args.run_dir, state, profiles))
    quality_hashes = {
        profile_id: record["content_sha256"]
        for profile_id, record in sorted(quality_by_profile.items())
    }
    if args.import_scores:
        hidden = _read_json(args.run_dir / "blind-private.json")
        scores = _read_json(args.import_scores)
        if (not isinstance(hidden, dict) or type(hidden.get("schema")) is not int
                or hidden.get("schema") != 1
                or not _current_campaign_contract(
                    hidden.get("campaign_contract_version"))
                or hidden.get("snapshot_digest") != state["snapshot"]["digest"]
                or hidden.get("campaign_inputs") != state["inputs"]
                or hidden.get("profiles") != profile_ids
                or hidden.get("quality_artifact_sha256") != quality_hashes
                or not _blind_protocol_bound(
                    hidden, quality_protocol_value, quality_protocol_digest,
                    quality_hashes)):
            raise HarnessError("blind private mapping is stale or has another profile set")
        if not isinstance(scores, dict):
            raise HarnessError("blind scores do not match this frozen packet")
        _require_keys(
            scores, {"schema", "campaign_contract_version", "snapshot_digest",
                     "blind_digest", "judgments", "quality_protocol",
                     "quality_protocol_digest", "quality_artifact_digest"},
            set(), "blind scores",
        )
        if (type(scores.get("schema")) is not int or scores.get("schema") != 1
                or not _current_campaign_contract(
                    scores.get("campaign_contract_version"))
                or scores.get("snapshot_digest") != state["snapshot"]["digest"]
                or scores.get("blind_digest") != hidden.get("blind_digest")
                or not _blind_protocol_bound(
                    scores, quality_protocol_value, quality_protocol_digest,
                    quality_hashes)):
            raise HarnessError("blind scores do not match this frozen packet")
        judgments = scores.get("judgments")
        if not isinstance(judgments, list):
            raise HarnessError("blind scores need a judgments list")
        mapping = hidden["mapping"]
        totals = {profile["id"]: {"points": 0, "possible": 0} for profile in profiles}
        expected_tasks = set(mapping)
        seen = set()
        for judgment in judgments:
            if not isinstance(judgment, dict) or set(judgment) - {"task", "ratings", "notes"}:
                raise HarnessError("blind judgment shape is invalid")
            task_id = judgment.get("task")
            ratings = judgment.get("ratings")
            if task_id not in mapping or task_id in seen or not isinstance(ratings, dict):
                raise HarnessError("blind judgment task is missing, duplicate, or unknown")
            if set(ratings) != set(mapping[task_id]):
                raise HarnessError(f"blind ratings for {task_id} do not cover every option")
            seen.add(task_id)
            for label, raw in ratings.items():
                if type(raw) is not int or not 0 <= raw <= 2:
                    raise HarnessError("blind ratings must be integers from 0 through 2")
                profile_id = mapping[task_id][label]
                totals[profile_id]["points"] += raw
                totals[profile_id]["possible"] += 2
        if seen != expected_tasks:
            raise HarnessError("blind scores do not cover every task")
        result = {
            "schema": 1, "snapshot_digest": state["snapshot"]["digest"],
            "campaign_contract_version": CAMPAIGN_CONTRACT_VERSION,
            "campaign_inputs": state["inputs"],
            "blind_digest": hidden["blind_digest"],
            "quality_protocol": quality_protocol_value,
            "quality_protocol_digest": quality_protocol_digest,
            "quality_artifact_sha256": quality_hashes,
            "quality_artifact_digest": _object_digest(quality_hashes),
            "profile_ids": profile_ids,
            "profiles": totals,
        }
        _write_json(args.run_dir / "blind-scores.json", result)
        print(json.dumps(result, sort_keys=True))
        return 0

    seed = int(hashlib.sha256(
        (state["snapshot"]["digest"] + state["inputs"]["quality_digest"]
         + quality_protocol_digest + _object_digest(quality_hashes))
        .encode("ascii")
    ).hexdigest()[:16], 16)
    rng = random.Random(seed)
    mapping = {}
    packet_tasks = []
    for task in quality:
        order = [profile["id"] for profile in profiles]
        rng.shuffle(order)
        labels = [chr(ord("A") + index) for index in range(len(order))]
        mapping[task["id"]] = dict(zip(labels, order))
        options = []
        for label, profile_id in zip(labels, order):
            outcomes = quality_by_profile[profile_id]["artifact"]["outcomes"]
            row = next(item for item in outcomes
                       if item["task"] == task["id"] and item["lane"] == "semantic")
            hits = [{"evidence": hit["evidence"], "project": hit["project"]}
                    for hit in row["hits"]]
            options.append({"label": label, "hits": hits})
        packet_tasks.append({
            "id": task["id"], "query": task["query"],
            "target": task["target"], "options": options,
        })
    packet = {
        "schema": 1, "snapshot_digest": state["snapshot"]["digest"],
        "campaign_contract_version": CAMPAIGN_CONTRACT_VERSION,
        "campaign_digest": _object_digest(state["inputs"]),
        "quality_protocol": quality_protocol_value,
        "quality_protocol_digest": quality_protocol_digest,
        "quality_artifact_digest": _object_digest(quality_hashes),
        "rubric": {"0": "not useful", "1": "useful", "2": "directly answers"},
        "tasks": packet_tasks,
    }
    packet["blind_digest"] = _blind_digest(packet)
    hidden = {
        "schema": 1, "snapshot_digest": state["snapshot"]["digest"],
        "campaign_contract_version": CAMPAIGN_CONTRACT_VERSION,
        "campaign_inputs": state["inputs"],
        "profiles": profile_ids,
        "quality_protocol": quality_protocol_value,
        "quality_protocol_digest": quality_protocol_digest,
        "quality_artifact_sha256": quality_hashes,
        "quality_artifact_digest": _object_digest(quality_hashes),
        "blind_digest": packet["blind_digest"], "mapping": mapping,
    }
    _write_json(args.run_dir / "blind-private.json", hidden)
    output = args.export or args.run_dir / "blind-review.json"
    _write_json(output, packet)
    print(json.dumps({"packet": str(output), "blind_digest": packet["blind_digest"]}))
    return 0


def _blind_not_worse(baseline: dict, candidate: dict) -> bool:
    for name, value in (("baseline", baseline), ("candidate", candidate)):
        if (not isinstance(value, dict) or set(value) != {"points", "possible"}
                or type(value.get("points")) is not int
                or type(value.get("possible")) is not int
                or value["possible"] <= 0 or not 0 <= value["points"] <= value["possible"]):
            raise HarnessError(f"{name} blind score is malformed")
    return (
        candidate["points"] * baseline["possible"]
        >= baseline["points"] * candidate["possible"]
    )


def adoption_decision(
        baseline: dict, candidate: dict, *, baseline_blind: dict | None = None,
        candidate_blind: dict | None = None) -> dict:
    required = {
        "semantic_correct", "hybrid_correct", "hybrid_total", "calibration_gap",
        "calibration_usable", "dedup_effective_rows_per_s", "q8_10m_bytes",
        "adoption_eligible", "physical_cpu_support",
    }
    for name, value in (("baseline", baseline), ("candidate", candidate)):
        if not isinstance(value, dict) or required - set(value):
            raise HarnessError(f"{name} metrics are incomplete")
    checks = {
        "semantic_strictly_better": (
            candidate["semantic_correct"] > baseline["semantic_correct"]),
        "hybrid_at_least_19_of_20": (
            candidate["hybrid_total"] == 20 and candidate["hybrid_correct"] >= 19),
        "calibration_gap_at_least_2x": (
            baseline["calibration_gap"] > 0.0
            and candidate["calibration_gap"] >= 2.0 * baseline["calibration_gap"]),
        "throughput_at_least_half": (
            candidate["dedup_effective_rows_per_s"]
            >= 0.5 * baseline["dedup_effective_rows_per_s"]),
        "q8_10m_at_most_2x": (
            candidate["q8_10m_bytes"] <= 2 * baseline["q8_10m_bytes"]),
        "usable_calibration_bands": candidate["calibration_usable"] is True,
        "license_and_runtime_eligible": candidate["adoption_eligible"] is True,
        "physical_mac_and_windows_cpu": candidate["physical_cpu_support"] is True,
    }
    if (baseline_blind is None) != (candidate_blind is None):
        raise HarnessError("blind adoption gate needs baseline and candidate scores")
    if baseline_blind is not None and candidate_blind is not None:
        checks["blind_not_worse_than_baseline"] = _blind_not_worse(
            baseline_blind, candidate_blind)
    return {"adopt": all(checks.values()), "checks": checks}


def _result_metrics(
        profile: dict, root: Path, state: dict, *,
        expected_quality: dict | None = None,
        expected_performance: dict | None = None) -> dict:
    profile_id = profile["id"]
    calibration_report, _profile_path, runtime_digest = (
        _validated_calibrated_profile(root, profile, state))
    quality_report = _read_profile_artifact(
        root, "quality.json", state, profile_id,
        {"task_digest": state["inputs"]["quality_digest"],
         "runtime_profile_digest": runtime_digest},
    )
    performance = _read_profile_artifact(
        root, "performance.json", state, profile_id,
        {"runtime_profile_digest": runtime_digest})
    if ((expected_quality is not None and quality_report != expected_quality)
            or (expected_performance is not None
                and performance != expected_performance)):
        raise HarnessError(f"{profile_id} measured artifact moved during report")
    quality_protocol_value = validate_quality_artifact_protocol(
        quality_report, state["inputs"]["quality_digest"])
    prepared = state.get("prepare", {}).get(profile_id)
    if not isinstance(prepared, dict):
        raise HarnessError(f"{profile_id} prepared result is missing")
    performance_protocol_value = validate_performance_artifact_protocol(
        performance, prepared,
        expected_code_digest=state["inputs"]["code_digest"])
    try:
        calibration = calibration_report["calibration"]
        effective_bands = calibration_report["effective_search_bands"]
        quality = quality_report["scores"]
        if not isinstance(performance["accelerator_used"], bool):
            raise TypeError("accelerator_used must be boolean")
        metrics = {
            "semantic_correct": int(quality["semantic"]["correct"]),
            "semantic_total": int(quality["semantic"]["total"]),
            "hybrid_correct": int(quality["hybrid"]["correct"]),
            "hybrid_total": int(quality["hybrid"]["total"]),
            "calibration_floor": float(effective_bands["floor"]),
            "calibration_strong": float(effective_bands["strong"]),
            "calibration_gap": float(calibration["gap"]),
            "calibration_usable": calibration_report["calibration_usable"] is True,
            "dedup_effective_rows_per_s": float(performance["full_rebuild"][
                "dedup_effective_rows_per_s"]),
            "q8_10m_bytes": int(performance["index_projection_10m"]["q8_bytes"]),
            "semantic_warm_exit_ms": float(
                performance["query_exit_ms"]["semantic"]["warm_median"]),
            "semantic_resident_rss_mib": float(
                performance["semantic_resources"]["semantic_resident_rss_mib"]),
            "semantic_peak_rss_mib": float(
                performance["semantic_resources"]["semantic_peak_rss_mib"]),
            "model_graph_bytes": int(performance["model_graph_bytes"]),
            "full_rebuild_10m_s": float(
                performance["full_rebuild_projection_10m"][
                    "modeled_components_sum_s"]),
            "top_up_at_10m_s": float(
                performance["current_layout_top_up_projection_10m"][
                    "modeled_components_sum_s"]),
            "top_up_rows": int(performance_protocol_value["top_up_rows"]),
            "quality_protocol_digest": _object_digest(quality_protocol_value),
            "performance_protocol_digest": _object_digest(
                performance_protocol_value),
            "benchmark_environment_digest": performance[
                "benchmark_environment_digest"],
            "accelerator_used": performance["accelerator_used"],
            "adoption_eligible": _derived_adoption_eligible(profile),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise HarnessError(f"{profile_id} measured artifacts are incomplete") from exc
    numeric = [
        value for key, value in metrics.items()
        if key not in {
            "accelerator_used", "adoption_eligible", "calibration_usable",
            "calibration_gap", "quality_protocol_digest",
            "performance_protocol_digest", "benchmark_environment_digest",
        }
    ]
    if (not all(math.isfinite(float(value)) and value >= 0 for value in numeric)
            or not math.isfinite(metrics["calibration_gap"])
            or any(not SHA256_RE.fullmatch(metrics[key]) for key in (
                "quality_protocol_digest", "performance_protocol_digest",
                "benchmark_environment_digest"))
            or metrics["semantic_total"] != 20 or metrics["hybrid_total"] != 20
            or metrics["semantic_correct"] > metrics["semantic_total"]
            or metrics["hybrid_correct"] > metrics["hybrid_total"]):
        raise HarnessError(f"{profile_id} measured artifacts contain invalid metrics")
    return metrics


def baseline_reproduction(metrics: dict) -> dict:
    checks = {
        "usable_calibration_bands": metrics.get("calibration_usable") is True,
        "semantic_14_of_20": (
            metrics.get("semantic_correct") == INCUMBENT_EXPECTED["semantic_correct"]
            and metrics.get("semantic_total") == 20),
        "hybrid_19_of_20": (
            metrics.get("hybrid_correct") == INCUMBENT_EXPECTED["hybrid_correct"]
            and metrics.get("hybrid_total") == INCUMBENT_EXPECTED["hybrid_total"]),
    }
    historical_policy = {
        "derived_floor_matches_shipped_0_82": math.isclose(
            float(metrics.get("calibration_floor", math.nan)),
            INCUMBENT_EXPECTED["floor"], abs_tol=0.0005),
        "derived_strong_matches_shipped_0_84": math.isclose(
            float(metrics.get("calibration_strong", math.nan)),
            INCUMBENT_EXPECTED["strong"], abs_tol=0.0005),
    }
    return {"pass": all(checks.values()), "expected": INCUMBENT_EXPECTED,
            "checks": checks, "historical_policy_reference": historical_policy}


def _report_measurement_contract(
        run_dir: Path, state: dict, profiles: list[dict]) -> tuple[dict, dict]:
    quality_records, quality_protocol_value, quality_protocol_digest = (
        _selected_quality_contract(run_dir, state, profiles))
    records = {}
    shared_environment = None
    shared_performance = None
    for profile in profiles:
        profile_id = profile["id"]
        root = _profile_root(run_dir, profile_id)
        runtime_digest = quality_records[profile_id]["runtime_profile_digest"]
        performance_artifact = _read_profile_artifact(
            root, "performance.json", state, profile_id,
            {"runtime_profile_digest": runtime_digest})
        performance_protocol_value = validate_performance_artifact_protocol(
            performance_artifact, state["prepare"][profile_id],
            expected_code_digest=state["inputs"]["code_digest"])
        performance_comparison = performance_protocol_comparison(
            performance_protocol_value)
        environment = performance_artifact["benchmark_environment"]
        if shared_environment is None:
            shared_environment = environment
            shared_performance = performance_comparison
        elif environment != shared_environment:
            raise HarnessError(
                "selected profiles used different benchmark environments")
        elif performance_comparison != shared_performance:
            raise HarnessError(
                "selected profiles used different performance protocols")
        records[profile_id] = {
            **quality_records[profile_id],
            "performance_artifact": performance_artifact,
            "performance_protocol": performance_protocol_value,
        }
    if shared_environment is None or shared_performance is None:
        raise HarnessError("selected profiles have no performance protocol")
    shared = {
        "campaign_contract_version": CAMPAIGN_CONTRACT_VERSION,
        "benchmark_environment": shared_environment,
        "benchmark_environment_digest": _object_digest(shared_environment),
        "quality_protocol": quality_protocol_value,
        "quality_protocol_digest": quality_protocol_digest,
        "quality_artifact_sha256": {
            profile_id: record["content_sha256"]
            for profile_id, record in sorted(quality_records.items())
        },
        "quality_artifact_digest": _object_digest({
            profile_id: record["content_sha256"]
            for profile_id, record in sorted(quality_records.items())
        }),
        "performance_protocol": shared_performance,
        "performance_protocol_digest": _object_digest(shared_performance),
    }
    return records, shared


def _validated_blind_scores(
        run_dir: Path, state: dict, profile_ids: list[str], tasks: int,
        quality_protocol_value: dict, quality_protocol_digest: str,
        quality_hashes: dict[str, str]) -> dict:
    hidden = _read_json(run_dir / "blind-private.json")
    scores = _read_json(run_dir / "blind-scores.json")
    if (not isinstance(hidden, dict) or type(hidden.get("schema")) is not int
            or hidden.get("schema") != 1
            or not _current_campaign_contract(
                hidden.get("campaign_contract_version"))
            or hidden.get("snapshot_digest") != state["snapshot"]["digest"]
            or hidden.get("campaign_inputs") != state["inputs"]
            or hidden.get("profiles") != profile_ids
            or hidden.get("quality_artifact_sha256") != quality_hashes
            or not _blind_protocol_bound(
                hidden, quality_protocol_value, quality_protocol_digest,
                quality_hashes)):
        raise HarnessError("blind private mapping is stale or has another profile set")
    if (not isinstance(scores, dict) or type(scores.get("schema")) is not int
            or scores.get("schema") != 1
            or not _current_campaign_contract(
                scores.get("campaign_contract_version"))
            or scores.get("snapshot_digest") != state["snapshot"]["digest"]
            or scores.get("campaign_inputs") != state["inputs"]
            or scores.get("profile_ids") != profile_ids
            or scores.get("blind_digest") != hidden.get("blind_digest")
            or scores.get("quality_artifact_sha256") != quality_hashes
            or not _blind_protocol_bound(
                scores, quality_protocol_value, quality_protocol_digest,
                quality_hashes)):
        raise HarnessError("blind scores are required and must match this campaign")
    totals = scores.get("profiles")
    if not isinstance(totals, dict) or set(totals) != set(profile_ids):
        raise HarnessError("blind scores do not cover the report profile set")
    expected_possible = tasks * 2
    for profile_id, row in totals.items():
        if (not isinstance(row, dict) or set(row) != {"points", "possible"}
                or type(row["points"]) is not int
                or type(row["possible"]) is not int
                or row["possible"] != expected_possible
                or not 0 <= row["points"] <= row["possible"]):
            raise HarnessError(f"blind score for {profile_id} is malformed")
    return scores


def _markdown(report: dict) -> str:
    lines = [
        "# Embedder selection report", "",
        f"Snapshot: `{report['snapshot_digest']}` ({report['rows']:,} rows)", "",
        f"Campaign valid: `{'yes' if report['campaign_valid'] else 'no'}`", "",
        "| profile | semantic | hybrid | gap | rows/s | q8 @ 10M | blind | decision |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for profile_id, row in report["profiles"].items():
        metrics = row["metrics"]
        decision = row.get("decision")
        label = "baseline" if decision is None else ("adopt" if decision["adopt"] else "reject")
        lines.append(
            f"| {profile_id} | {metrics['semantic_correct']}/20 | "
            f"{metrics['hybrid_correct']}/{metrics['hybrid_total']} | "
            f"{metrics['calibration_gap']:.3f} | "
            f"{metrics['dedup_effective_rows_per_s']:.1f} | "
            f"{metrics['q8_10m_bytes'] / (1024 ** 3):.2f} GiB | "
            f"{row['blind']['points']}/{row['blind']['possible']} | {label} |"
        )
    lines.extend(["", "## Incumbent reproduction gate", ""])
    for name, passed in report["baseline_reproduction"]["checks"].items():
        lines.append(f"- {'PASS' if passed else 'FAIL'}: {name}")
    lines.extend(["", "## Historical shipped-band reference (non-gating)", ""])
    for name, passed in report["baseline_reproduction"][
            "historical_policy_reference"].items():
        lines.append(f"- {'MATCH' if passed else 'DIFF'}: {name}")
    lines.extend([
        "", "## Runtime and scale evidence", "",
        "| profile | warm semantic exit | resident / peak RSS | model | "
        "full rebuild @ 10M | measured top-up @ 10M | provider | physical CPU |",
        "|---|---:|---:|---:|---:|---:|---|---|",
    ])
    for profile_id, row in report["profiles"].items():
        metrics = row["metrics"]
        physical = ", ".join(row["platform_support"]["measured"]) or "missing"
        lines.append(
            f"| {profile_id} | {metrics['semantic_warm_exit_ms']:.1f} ms | "
            f"{metrics['semantic_resident_rss_mib']:.1f} / "
            f"{metrics['semantic_peak_rss_mib']:.1f} MiB | "
            f"{metrics['model_graph_bytes'] / MIB:.1f} MiB | "
            f"{metrics['full_rebuild_10m_s']:.1f} s | "
            f"{metrics['top_up_rows']:,} rows: "
            f"{metrics['top_up_at_10m_s']:.1f} s | "
            f"{'accelerator' if metrics['accelerator_used'] else 'CPU'} | "
            f"{physical} |"
        )
    lines.extend(["", "## Independent adoption gates", ""])
    for profile_id, row in report["profiles"].items():
        decision = row.get("decision")
        if decision is None:
            continue
        lines.append(f"### {profile_id}")
        lines.append("")
        for name, passed in decision["checks"].items():
            lines.append(f"- {'PASS' if passed else 'FAIL'}: {name}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def stage_report(args: argparse.Namespace) -> int:
    state, manifest, _calibration, quality = _load_state(args)
    profiles = _selected_from_state(state, manifest, args.profile)
    baseline_profiles = [profile for profile in profiles if profile["baseline"]]
    if len(baseline_profiles) != 1:
        raise HarnessError("report selection must contain the one baseline")
    baseline_profile = baseline_profiles[0]
    profile_ids = sorted(profile["id"] for profile in profiles)
    contract_records, measurement_contract = _report_measurement_contract(
        args.run_dir, state, profiles)
    platform_path = (
        args.platform_evidence if args.platform_evidence is not None
        else args.run_dir / "platform-evidence.json"
    )
    platform_bundle, platform_support = _validated_platform_support(
        platform_path, manifest, profile_ids)
    blind = _validated_blind_scores(
        args.run_dir, state, profile_ids, len(quality),
        measurement_contract["quality_protocol"],
        measurement_contract["quality_protocol_digest"],
        measurement_contract["quality_artifact_sha256"])
    baseline_contract = contract_records[baseline_profile["id"]]
    baseline = _result_metrics(
        baseline_profile, _profile_root(args.run_dir, baseline_profile["id"]), state,
        expected_quality=baseline_contract["artifact"],
        expected_performance=baseline_contract["performance_artifact"])
    baseline["physical_cpu_support"] = platform_support[
        baseline_profile["id"]]["complete"]
    reproduction = baseline_reproduction(baseline)
    campaign_valid = bool(
        reproduction["pass"] and baseline["physical_cpu_support"])
    rows = {}
    for profile in profiles:
        profile_root = _profile_root(args.run_dir, profile["id"])
        contract = contract_records[profile["id"]]
        metrics = _result_metrics(
            profile, profile_root, state,
            expected_quality=contract["artifact"],
            expected_performance=contract["performance_artifact"])
        performance_artifact = contract["performance_artifact"]
        q8_evidence = performance_artifact["q8_scale_evidence"]
        metrics["physical_cpu_support"] = platform_support[
            profile["id"]]["complete"]
        decision = None if profile["baseline"] else adoption_decision(
            baseline, metrics,
            baseline_blind=blind["profiles"][baseline_profile["id"]],
            candidate_blind=blind["profiles"][profile["id"]],
        )
        rows[profile["id"]] = {
            "metrics": metrics,
            "blind": blind["profiles"][profile["id"]],
            "decision": decision,
            "protocols": {
                "quality": contract["protocol"],
                "performance": contract["performance_protocol"],
                "benchmark_environment": performance_artifact[
                    "benchmark_environment"],
            },
            "evidence_anchors": {
                "q8_scale_cache_path": q8_evidence["cache_path"],
                "q8_scale_cache_sha256": q8_evidence["cache_sha256"],
                "q8_scale_record_digest": q8_evidence["record_digest"],
                "quality_artifact_sha256": contract["content_sha256"],
            },
            "eligible_for_selection": bool(
                campaign_valid and decision and decision["adopt"]),
            "platform_support": platform_support[profile["id"]],
        }
    eligible = [
        (profile_id, row) for profile_id, row in rows.items()
        if row["eligible_for_selection"]
    ]
    eligible.sort(key=lambda item: (
        -item[1]["blind"]["points"] / item[1]["blind"]["possible"],
        -item[1]["metrics"]["semantic_correct"],
        -item[1]["metrics"]["hybrid_correct"],
        -item[1]["metrics"]["calibration_gap"],
        -item[1]["metrics"]["dedup_effective_rows_per_s"],
        item[1]["metrics"]["q8_10m_bytes"],
        item[0],
    ))
    report = {
        "schema": 1, "snapshot_digest": state["snapshot"]["digest"],
        "campaign_contract_version": CAMPAIGN_CONTRACT_VERSION,
        "rows": state["snapshot"]["rows"], "baseline": baseline_profile["id"],
        "campaign_inputs": state["inputs"],
        "measurement_contract": measurement_contract,
        "campaign_valid": campaign_valid,
        "baseline_reproduction": reproduction,
        "platform_evidence": {
            "digest": _object_digest(platform_bundle),
            "required": list(REQUIRED_CPU_PLATFORMS),
        },
        "profiles": rows,
        "winner": eligible[0][0] if eligible else None,
        "winner_order": [profile_id for profile_id, _row in eligible],
        "blind_review": blind,
    }
    _write_json(args.run_dir / "results.json", report)
    markdown = _markdown(report)
    (args.run_dir / "report.md").write_text(markdown, encoding="utf-8", newline="\n")
    print(json.dumps(report, sort_keys=True))
    return 0


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--calibration-tasks", type=Path, default=DEFAULT_CALIBRATION)
    parser.add_argument("--quality-tasks", type=Path, default=DEFAULT_QUALITY)
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_DIR)
    parser.add_argument("--profile", action="append", default=[])
    parser.add_argument("--expect-snapshot")
    parser.add_argument("--timeout", type=float, default=3600.0)
    parser.add_argument(
        "--power-policy", default="host-default-uncontrolled",
        help="record the externally controlled host power policy")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="stage", required=True)
    prepare = sub.add_parser("prepare", help="freeze corpus, verify models, and embed")
    _common(prepare)
    prepare.add_argument("--source-data", type=Path)
    prepare.add_argument(
        "--artifact-cache", type=Path,
        help="seed exact pinned files from PATH/<profile-id> without downloads")
    prepare.add_argument("--allow-download", action="store_true")
    prepare.add_argument("--no-embed", action="store_true")
    prepare.set_defaults(run=stage_prepare)
    calibrate = sub.add_parser("calibrate", help="derive model-specific confidence bands")
    _common(calibrate)
    calibrate.set_defaults(run=stage_calibrate)
    quality = sub.add_parser("quality", help="run frozen semantic and hybrid tasks")
    _common(quality)
    quality.add_argument("--hits", type=int, default=3)
    quality.set_defaults(run=stage_quality)
    performance = sub.add_parser("performance", help="measure final exit and rebuild cost")
    _common(performance)
    performance.add_argument("--queries", type=int, default=7)
    performance.add_argument("--top-up-rows", type=int, default=1000)
    performance.set_defaults(run=stage_performance)
    blind = sub.add_parser("blind", help="export or import name-free manual review")
    _common(blind)
    blind.add_argument("--export", type=Path)
    blind.add_argument("--import-scores", type=Path)
    blind.set_defaults(run=stage_blind)
    report = sub.add_parser("report", help="apply independent adoption gates")
    _common(report)
    report.add_argument("--platform-evidence", type=Path)
    report.set_defaults(run=stage_report)
    validate = sub.add_parser("validate", help="validate all committed selection inputs")
    validate.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    validate.add_argument("--calibration-tasks", type=Path, default=DEFAULT_CALIBRATION)
    validate.add_argument("--quality-tasks", type=Path, default=QUALITY_EXAMPLE)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.stage == "validate":
            manifest = load_manifest(args.manifest)
            calibration = load_calibration(args.calibration_tasks)
            quality = load_quality(args.quality_tasks)
            print(json.dumps({
                "manifest_digest": _object_digest(manifest),
                "calibration_digest": _object_digest(calibration),
                "quality_digest": _object_digest(quality),
                "profiles": len(manifest["profiles"]),
                "runnable": sum(profile["status"] == "runnable"
                                for profile in manifest["profiles"]),
            }, sort_keys=True))
            return 0
        for name in (
                "manifest", "calibration_tasks", "quality_tasks", "run_dir",
                "source_data", "artifact_cache", "export", "import_scores",
                "platform_evidence"):
            value = getattr(args, name, None)
            if isinstance(value, Path):
                setattr(args, name, value.expanduser().resolve())
        if args.timeout <= 0 or not math.isfinite(args.timeout):
            parser.error("--timeout must be finite and positive")
        if (not isinstance(args.power_policy, str) or not args.power_policy.strip()
                or len(args.power_policy) > 120):
            parser.error("--power-policy must be a non-empty label up to 120 characters")
        if getattr(args, "hits", 3) not in range(1, 21):
            parser.error("--hits must be 1..20")
        if getattr(args, "queries", 7) < 2:
            parser.error("--queries must be at least 2")
        if getattr(args, "top_up_rows", 1000) < 1:
            parser.error("--top-up-rows must be positive")
        return args.run(args)
    except (HarnessError, OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        print(f"embedder selection: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
