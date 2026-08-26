"""The semantic embedding model - the only module in agrep that touches one.

granite-embedding-small-english-r2, int8 ONNX on CPU. Its pinned model-card
contract is CLS pooling, no query/passage prefixes (symmetric), 384 dimensions,
and local L2 normalization.

The profile below is pinned by revision AND per-file sha256 - a byte of drift
re-downloads or fails loud, and PROFILE_STRING lands in embeddings.meta so any
profile change makes the existing index re-embed cleanly (the drift guard in
embed.py). Weights download once into the user model cache (or AGREP_MODEL_DIR)
on first need; offline machines get EmbedderUnavailable and callers fall back.

Two engines can produce those vectors - ONNX int8 on CPU and MLX fp16 on Metal -
and they are NOT interchangeable, so the lane is part of the identity in
embeddings.meta and one store only ever holds one of them. See LANE_CPU below.

External enrichment tools import this module for summary embeddings so both
artifact families stay in one vector space.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import stat
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np

import common
import mlx_embed
import ownerfile
import surface_policy as surface

PROFILE = {
    "id": "granite-small-r2-q8",
    "repo": "onnx-community/granite-embedding-small-english-r2-ONNX",
    "revision": "1dc7835ba0cb9c76a3618d0bf0c427c97671b3c8",
    "dim": 384,
    # Frozen ranking A/B: 1,024 tied 2,048 at far lower backfill cost; rows
    # beyond the window embed as '#cN' chunk vectors (embed.py) so long
    # pastes stay retrievable past their head.
    "max_seq": 1024,
    "files": {
        "model_quantized.onnx": (
            598902, "a3fad524afc3f060216a8ddbb1ac89c9b6498fba8995b5718bde879076a2e9ba"),
        "model_quantized.onnx_data": (
            51885568, "1f4cf47e4adec7f7ae09db03d071ba8667e07f9a4203142c7efa8d37fe453597"),
        "tokenizer.json": (
            2128614, "feeb83348dcb033bc6b9d2e1f7906ca9eb2d122845000c9416d894d7c2927149"),
    },
    # repo path of each file above ("" = repo root)
    "remote_dir": {"model_quantized.onnx": "onnx/", "model_quantized.onnx_data": "onnx/"},
}
# The identity written to embeddings.meta. Sequence length is part of the vector
# space: changing it must invalidate old rows even when the model bytes are equal.
PROFILE_STRING = (f"{PROFILE['repo']}@{PROFILE['revision'][:12]}/{PROFILE['id']}"
                  f":seq{PROFILE['max_seq']}")

# The lane is part of the vector space for the reason sequence length is:
# shared weights, different arithmetic (~0.999 cosine) flipped 3/45 fixture
# queries at thresholds. One store holds one lane; queries embed with it.
LANE_CPU = "onnx-int8-cpu"
LANE_METAL = "mlx-fp16-metal"
LANES = (LANE_CPU, LANE_METAL)


class EmbedderUnavailable(RuntimeError):
    """The optional model/runtime cannot serve semantic search."""


def profile_string(lane: str) -> str:
    """The ``embeddings.meta`` identity for one lane.

    The CPU lane's string is PROFILE_STRING byte for byte, so every store built
    before lanes existed stays valid and nothing re-embeds; only Metal carries a
    suffix. That asymmetry is the whole migration story.
    """
    if lane == LANE_CPU:
        return PROFILE_STRING
    if lane == LANE_METAL:
        return f"{PROFILE_STRING}:lane-{LANE_METAL}"
    raise EmbedderUnavailable(f"unknown embedding lane {lane!r}")


def lane_of(model_id: str | None) -> str | None:
    """Which lane wrote ``model_id``, or None when it is not this profile at all."""
    if not model_id:
        return None
    for lane in LANES:
        if model_id == profile_string(lane):
            return lane
    return None


def default_lane() -> str:
    """The lane a store gets when nothing on disk decides it yet.

    Metal is the default wherever it can open; AGREP_MLX=off opts out. Only a
    store with no recorded lane lands here - resolve_lane conforms to the rest.

    Capability decides this, never current load: a lane is a vector space the
    store keeps for life, and one load average used to strand it on the slow
    engine permanently. Pacing belongs to _embedding_backfill_policy.
    """
    if not mlx_embed.available()[0]:
        # available() also answers False under AGREP_MLX=off.
        return LANE_CPU
    return LANE_METAL


def resolve_lane(model_id: str | None) -> str:
    """The lane this process must use for a store that records ``model_id``.

    Conform, never mix: a store already holding Metal rows keeps getting Metal
    rows even without AGREP_MLX=on, and a CPU store stays CPU even with it. When
    a Metal store's lane cannot open here this answers CPU, which makes the
    identities disagree - and an honest mismatch (a refusal on the query side, an
    announced rebuild on the build side) is the point. Silently serving CPU
    vectors against Metal rows is the failure. The one sanctioned lane move is
    `agrep reindex --full`, which discards every row and re-decides from
    default_lane (see embed._FRESH_LANE).
    """
    lane = lane_of(model_id)
    if lane == LANE_METAL:
        return LANE_METAL if mlx_embed.available()[0] else LANE_CPU
    if lane == LANE_CPU:
        return LANE_CPU
    return default_lane()


def probe_default_lane(*, download: bool = False) -> tuple[str, str | None]:
    """The lane this machine would actually open, and why not, if not metal.

    `default_lane` answers the capability question cheaply; it cannot know that
    the parity gate will decline, or that an ONNX provider is pinned. Only
    building the thing proves it, which costs a model load - so this is for
    diagnostics that already pay that cost, never for the query path.
    """
    if default_lane() != LANE_METAL:
        ok, reason = mlx_embed.available()
        return LANE_CPU, None if ok else reason
    try:
        probe = Embedder(download=download, lane=None)
    except EmbedderUnavailable as exc:
        return LANE_CPU, f"the embedder did not load: {exc}"
    return probe.lane, probe.metal_refusal


def store_profile_string(model_id: str | None) -> str:
    """The identity this process reads and writes for a store recording ``model_id``."""
    return profile_string(resolve_lane(model_id))


_PAST_INPUT_RE = re.compile(r"past_key_values\.\d+\.(?:key|value)\Z")


def semantic_bands() -> surface.SemanticScoreBands:
    bands = PROFILE.get("search_bands")
    if bands is None:
        return surface.DEFAULT_SEMANTIC_SCORE_BANDS
    return surface.SemanticScoreBands(
        floor=float(bands["floor"]),
        strong=float(bands["strong"]),
    )




MODEL_DOWNLOAD_WAIT_S = 600.0
_DOWNLOAD_STALE_S = 600.0
_DOWNLOAD_CLAIM_BYTES = 4096
_DOWNLOAD_CREATE_GRACE_S = 2.0
_DOWNLOAD_POLL_S = 0.05
_DOWNLOAD_RETRY_S = 0.02
_DOWNLOAD_FS_RETRY_DELAYS = (0.0, 0.01, 0.05)


def _readonly_contains(path: Path) -> bool:
    protected = os.environ.get("AGREP_DATA_READONLY")
    if not protected:
        return False
    try:
        root = os.path.normcase(os.path.realpath(protected))
        target = os.path.normcase(os.path.realpath(os.fspath(path)))
        return os.path.commonpath((root, target)) == root
    except (OSError, ValueError):
        return False


def _thread_budget() -> int:
    # The attention graph becomes memory-bound before all logical cores are useful.
    cap = 6 if sys.platform == "darwin" else (5 if sys.platform == "win32" else 8)
    default = max(1, min(cap, (os.cpu_count() or 2) // 2))
    try:
        return max(1, min(64, int(os.environ.get("AGREP_SEM_THREADS", default))))
    except (TypeError, ValueError):
        return default


def model_dir() -> Path:
    # Model ownership is per user, independent of synthetic benchmark data dirs.
    # Moving AGREP_DATA_DIR must not duplicate the separately cached weights.
    override = os.environ.get("AGREP_MODEL_DIR")
    base = Path(override).expanduser() if override else common.DEFAULT_DATA_DIR / "models"
    return base / PROFILE["id"]


def model_cached() -> bool:
    """Whether every pinned artifact has the expected cheap file shape."""
    root = model_dir()
    files = PROFILE.get("files") or {}
    return bool(files) and all(
        _artifact_stamp(root / name, int(spec[0])) is not None
        for name, spec in files.items())


def _artifact_stamp(path: Path, expected_size: int) -> tuple[int, ...] | None:
    try:
        info = path.lstat()
    except OSError:
        return None
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (not stat.S_ISREG(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0) & reparse)
            or info.st_size != expected_size):
        return None
    return (
        int(info.st_dev), int(info.st_ino), int(info.st_size),
        int(getattr(info, "st_mtime_ns", int(info.st_mtime * 1e9))),
        int(getattr(info, "st_ctime_ns", int(info.st_ctime * 1e9))),
    )


def _sha256_fd(fd: int, expected_size: int) -> str | None:
    h = hashlib.sha256()
    remaining = expected_size
    while remaining:
        chunk = os.read(fd, min(1 << 20, remaining))
        if not chunk:
            return None
        h.update(chunk)
        remaining -= len(chunk)
    if os.read(fd, 1):
        return None
    return h.hexdigest()


def _verified_file_stamp(
        path: Path, size: int, sha: str) -> tuple[int, ...] | None:
    expected = _artifact_stamp(path, size)
    if expected is None:
        return None
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_NONBLOCK", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    fd = None
    try:
        fd = os.open(path, flags)
        before = os.fstat(fd)
        opened = (
            int(before.st_dev), int(before.st_ino), int(before.st_size),
            int(getattr(before, "st_mtime_ns", int(before.st_mtime * 1e9))),
            int(getattr(before, "st_ctime_ns", int(before.st_ctime * 1e9))),
        )
        # Windows path and descriptor APIs can expose different ctime views;
        # stability stays strict within each view and on their shared identity.
        if opened[:4] != expected[:4] or not stat.S_ISREG(before.st_mode):
            return None
        digest = _sha256_fd(fd, size)
        after = os.fstat(fd)
        stable = (
            int(after.st_dev), int(after.st_ino), int(after.st_size),
            int(getattr(after, "st_mtime_ns", int(after.st_mtime * 1e9))),
            int(getattr(after, "st_ctime_ns", int(after.st_ctime * 1e9))),
        )
        final = _artifact_stamp(path, size)
        return (final if (stable == opened
                          and final == expected
                          and digest == sha) else None)
    except OSError:
        return None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _file_ok(path: Path, size: int, sha: str) -> bool:
    return _verified_file_stamp(path, size, sha) is not None


def _download_claim_path(root: Path) -> Path:
    return root / ".download.lock"


def _download_claim_state(observed: ownerfile.Snapshot) -> tuple[bool, float]:
    try:
        rec = json.loads(observed.raw.decode("utf-8"))
        if not isinstance(rec, dict):
            raise TypeError("download claim must be an object")
        pid = int(rec.get("pid") or 0)
        expected = str(rec.get("process_start") or "")
        age = time.time() - float(rec.get("at") or 0)
        if not math.isfinite(age):
            raise ValueError("download claim age is not finite")
        alive = (pid > 0 and common.pid_alive(pid)
                 and expected not in ("", "None", "unknown")
                 and str(common.process_start_identity(pid)) == expected)
        return alive, age
    except (OSError, OverflowError, ValueError, TypeError, json.JSONDecodeError):
        age = time.time() - observed.mtime
        return 0.0 <= age < _DOWNLOAD_CREATE_GRACE_S, age


def _download_claim_leaf_problem(path: Path) -> OSError | None:
    try:
        info = path.lstat()
    except OSError:
        return None
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (not stat.S_ISREG(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0) & reparse)):
        return OSError(f"model download claim is not a plain regular file: {path}")
    if info.st_size > _DOWNLOAD_CLAIM_BYTES:
        return OSError(
            f"model download claim exceeds {_DOWNLOAD_CLAIM_BYTES} bytes: {path}")
    return None


def _acquire_download_claim(root: Path) -> ownerfile.Handle | None:
    """One downloader per model directory; ``None`` means a peer finished it."""
    if _readonly_contains(root):
        raise EmbedderUnavailable(
            "AGREP_DATA_READONLY protects the model cache; "
            "cannot acquire a download claim")
    path = _download_claim_path(root)
    pid = os.getpid()
    process_start = common.process_start_identity(pid)
    token = secrets.token_hex(16)
    verified: dict[str, tuple[int, ...]] = {}

    def model_complete() -> bool:
        for name, (size, sha) in PROFILE["files"].items():
            artifact = root / name
            current = _artifact_stamp(artifact, size)
            if current is None:
                verified.pop(name, None)
                return False
            if verified.get(name) != current:
                verified.pop(name, None)
                current = _verified_file_stamp(artifact, size, sha)
                if current is None:
                    return False
                verified[name] = current
        return True

    deadline = time.monotonic() + MODEL_DOWNLOAD_WAIT_S
    reclaimed_last = False
    while time.monotonic() < deadline:
        if model_complete():
            return None
        try:
            raw = json.dumps({
                "pid": pid, "process_start": process_start,
                "at": time.time(), "token": token,
            }, separators=(",", ":")).encode("utf-8")
            return ownerfile.create_exclusive(
                path, raw)
        except FileExistsError:
            if reclaimed_last:
                if time.monotonic() >= deadline:
                    break
                time.sleep(_DOWNLOAD_POLL_S)
                reclaimed_last = False
            try:
                observed = ownerfile.snapshot(
                    path, max_bytes=_DOWNLOAD_CLAIM_BYTES)
            except OSError:
                problem = _download_claim_leaf_problem(path)
                if problem is not None:
                    raise EmbedderUnavailable(
                        f"could not claim model download: {problem}") from problem
                time.sleep(_DOWNLOAD_RETRY_S)
                continue
            protected, age = _download_claim_state(observed)
            if not protected or age > _DOWNLOAD_STALE_S:
                if ownerfile.remove_exact(
                        path, observed, tombstone=True):
                    reclaimed_last = True
                    continue
            time.sleep(_DOWNLOAD_POLL_S)
        except OSError as exc:
            raise EmbedderUnavailable(f"could not claim model download: {exc}") from exc
    if model_complete():
        return None
    raise EmbedderUnavailable("timed out waiting for another model download")


def _release_download_claim(claim: ownerfile.Handle) -> None:
    if _readonly_contains(claim.path):
        claim.close()
        return
    try:
        claim.release(tombstone=True)
    except OSError:
        pass


def _require_download_claim(claim: ownerfile.Handle) -> None:
    try:
        claim.verify()
    except OSError as exc:
        raise EmbedderUnavailable(
            f"model download ownership lost: {exc}") from exc


def _discard_download_part(
        path: Path, claim: ownerfile.Handle | None = None) -> bool:
    if _readonly_contains(path):
        return False
    for delay in _DOWNLOAD_FS_RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        if claim is not None:
            _require_download_claim(claim)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            pass
    return False


def _publish_download_part(
        part: Path, target: Path, claim: ownerfile.Handle) -> None:
    if _readonly_contains(target):
        raise EmbedderUnavailable(
            "AGREP_DATA_READONLY protects the model cache; "
            "cannot publish a model artifact")
    last_error = None
    for delay in _DOWNLOAD_FS_RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        _require_download_claim(claim)
        try:
            os.replace(part, target)
            return
        except OSError as exc:
            last_error = exc
    raise last_error


def _require_model_download_enabled() -> None:
    if common.setting("embeddings") == "off":
        raise EmbedderUnavailable(
            "model download disabled by `agrep set embeddings off`; "
            "run `agrep set embeddings auto` to allow downloads")


def _fetch_pinned(url: str, part: Path, expected_size: int) -> None:
    """Download with an upper bound before the sha pass (no unbounded response)."""
    if _readonly_contains(part):
        raise EmbedderUnavailable(
            "AGREP_DATA_READONLY protects the model cache; "
            "cannot download a model artifact")
    total = 0
    _require_model_download_enabled()
    import dist
    with dist.verified_urlopen(url, timeout=120) as response, part.open("xb") as stream:
        try:
            declared_raw = response.getheader("Content-Length")
            declared = int(declared_raw) if declared_raw is not None else None
        except (AttributeError, TypeError, ValueError):
            declared = None
        if declared is not None and declared > expected_size:
            raise EmbedderUnavailable(
                f"download declared {declared} bytes; expected {expected_size}")
        while True:
            chunk = response.read(min(1 << 20, expected_size - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > expected_size:
                raise EmbedderUnavailable(
                    f"download exceeded pinned size {expected_size}")
            stream.write(chunk)


def ensure_model(download: bool = True) -> Path:
    """Verify-or-fetch the pinned weights. Returns the model dir, or raises
    EmbedderUnavailable. Downloads are atomic (.part + rename) so a killed
    fetch never leaves a half file that passes existence checks."""
    root = model_dir()
    missing = [(name, spec) for name, spec in PROFILE["files"].items()
               if not _file_ok(root / name, *spec)]
    if not missing:
        return root
    if not download:
        raise EmbedderUnavailable(f"model files missing under {root}")
    if _readonly_contains(root):
        raise EmbedderUnavailable(
            "AGREP_DATA_READONLY protects the model cache; "
            "cannot download model files")
    _require_model_download_enabled()
    root.mkdir(parents=True, exist_ok=True)
    claim = _acquire_download_claim(root)
    if claim is None:
        return root
    try:
        _require_download_claim(claim)
        # Partials left under a superseded exact claim are takeover debris.
        for part in root.glob(".*.part"):
            if not _discard_download_part(part, claim):
                raise EmbedderUnavailable(
                    f"could not remove stale model partial: {part}")
        missing = [(name, spec) for name, spec in PROFILE["files"].items()
                   if not _file_ok(root / name, *spec)]
        base = (f"https://huggingface.co/{PROFILE['repo']}/resolve/"
                f"{PROFILE['revision']}/")
        for name, (size, sha) in missing:
            remote_paths = PROFILE.get("remote_paths")
            remote_path = (remote_paths[name] if remote_paths is not None else
                           PROFILE["remote_dir"].get(name, "") + name)
            url = base + remote_path
            part = root / f".{name}.{os.getpid()}.{secrets.token_hex(8)}.part"
            common.log(f"embedder: fetching {name} ({size / 1e6:.0f} MB) ...")
            try:
                _fetch_pinned(url, part, size)
                if not _file_ok(part, size, sha):
                    raise EmbedderUnavailable(
                        f"{name} failed size/sha verification")
                _publish_download_part(part, root / name, claim)
            except (OSError, EmbedderUnavailable) as exc:
                raise EmbedderUnavailable(f"could not fetch {name}: {exc}") from exc
            finally:
                _discard_download_part(part)
    finally:
        _release_download_claim(claim)
    common.log(f"embedder: model ready at {root}")
    return root


# int8 CPU vs BF16 metal: ~0.99 is quantization cost; 0.995 admitted none of
# 240 real messages, and a pooling mismatch scores ~0.77. The 0.82/0.84 score
# bands are int8-calibrated; doctor's lane row discloses the difference.
_METAL_MIN_COSINE = 0.97

# Probes span short rows AND one past the 128-token local-attention window:
# below it local and global layers are identical, so a wrong global RoPE base
# ("silently degrades long rows") would be invisible to this gate.
_METAL_PROBES = (
    "index",
    "the daemon wedged during a warm index pass",
    "semantic search returned no meaning results and fell back to keyword",
    "how do I make embeddings faster on this machine without stealing the gpu "
    "from the compositor while a backfill is running in the background, given "
    "that the display and the model share one piece of silicon and the owner "
    "is actively scrolling a window at the same time",
    # ~190 tokens: exercises the global-attention layers and both RoPE bases.
    "the release gate failed at the two million row campaign because the "
    "sampled process held both of the child's pipes without draining them, "
    "so a lane that emitted more debug output than one pipe buffer blocked "
    "on its own stderr until the query timeout expired and the campaign "
    "died before a single budget was compared. the fix keeps reader threads "
    "on both pipes for the whole run and reaps the child unconditionally, "
    "because a zombie's cpu time lands in the parent's children rusage and "
    "inflates the next run's measurement. separately, the derived stores "
    "were owned by a different build identity after cargo rebuilt the "
    "release binary between corpus construction and the embedding pass, so "
    "the reindex refused to touch them and the semantic store never "
    "published its generation. none of this was visible from the exit code "
    "alone: the harness reported a timeout, the freshness validator "
    "reported a vanished agent, and the actual defect sat in the benchmark "
    "runner rather than in the search engine it was measuring.",
)


def _parity_cache_path():
    return common.EMBEDDINGS_PATH.parent / ".metal_parity.json"


def _parity_key() -> str | None:
    """What the verdict is about: this mlx build, model, and probe set."""
    try:
        import importlib.metadata as md
        probes = hashlib.sha256(
            "\0".join(_METAL_PROBES).encode("utf-8")).hexdigest()[:12]
        return f"{md.version('mlx')}/{PROFILE_STRING}/{probes}"
    except Exception:  # noqa: BLE001 -- no key means no caching, never a wrong hit
        return None


def _cached_parity() -> float | None:
    key = _parity_key()
    if key is None:
        return None
    try:
        record = json.loads(_parity_cache_path().read_bytes())
        if record.get("key") != key:
            return None
        value = float(record["cosine"])
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
    return value if math.isfinite(value) else None


def _store_parity(cosine: float) -> None:
    """Best-effort: a read-only or racing data dir just means we measure again."""
    key = _parity_key()
    if key is None or not math.isfinite(cosine):
        return
    path = _parity_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"key": key, "cosine": round(float(cosine), 6)}),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


class Embedder:
    """One loaded ONNX session and tokenizer; callers own its lifetime."""

    def __init__(self, download: bool = True, lane: str | None = None):
        if lane is not None and lane not in LANES:
            raise EmbedderUnavailable(f"unknown embedding lane {lane!r}")
        try:
            # onnxruntime 1.28+ writes a cpuid_info warning to raw fd 2 at
            # import on CPUs it cannot classify - junk on every agent-parsed
            # stderr, so the one-time import runs with fd 2 silenced.
            try:
                saved = os.dup(2)
            except OSError:
                saved = None
            if saved is None:
                import onnxruntime as ort
            else:
                devnull = os.open(os.devnull, os.O_WRONLY)
                try:
                    os.dup2(devnull, 2)
                    import onnxruntime as ort
                finally:
                    os.dup2(saved, 2)
                    os.close(devnull)
                    os.close(saved)
            from tokenizers import Tokenizer
        except ImportError as e:
            raise EmbedderUnavailable(f"onnx runtime deps missing: {e}") from e
        root = ensure_model(download=download)
        self.profile = PROFILE
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = _thread_budget()
        model_file = self.profile.get("model_file", "model_quantized.onnx")
        tokenizer_file = self.profile.get("tokenizer_file", "tokenizer.json")
        provider = self.profile.get("provider", "CPUExecutionProvider")
        if provider not in ort.get_available_providers():
            raise EmbedderUnavailable(
                f"ONNX provider {provider!r} is unavailable; found {ort.get_available_providers()}")
        try:
            self.sess = ort.InferenceSession(str(root / model_file),
                                             sess_options=opts,
                                             providers=[provider])
        except Exception as exc:
            raise EmbedderUnavailable(
                f"ONNX session could not use {provider}: {type(exc).__name__}: {exc}") from exc
        self.inputs = self._validate_session()
        try:
            self.tok = Tokenizer.from_file(str(root / tokenizer_file))
            self.tok.enable_truncation(max_length=self.profile["max_seq"])
            self.pad_id, self.pad_type_id, pad_token = self._padding_contract(
                self.tok, self.profile.get("pad_token"))
            self.tok.enable_padding(
                direction="right", pad_id=self.pad_id,
                pad_type_id=self.pad_type_id, pad_token=pad_token)
        except EmbedderUnavailable:
            raise
        except Exception as exc:
            raise EmbedderUnavailable(
                f"tokenizer could not initialize: {type(exc).__name__}: {exc}") from exc
        self._metal = None
        self.lane = LANE_CPU
        # Why this instance is not on metal, when it tried and could not be.
        # The reason used to exist only as a debug line, so a box that wanted
        # metal and silently got cpu looked identical to one that never asked.
        self.metal_refusal: str | None = None
        # An unnamed lane is this machine's default and may quietly land on CPU;
        # a named one came from a store's recorded identity, so failing to open
        # it must be an error rather than a different vector space.
        if (lane if lane is not None else default_lane()) == LANE_METAL:
            self._start_metal_lane(required=lane is not None)

    @property
    def profile_string(self) -> str:
        """The identity of the rows this instance produces."""
        return profile_string(getattr(self, "lane", LANE_CPU))

    def _start_metal_lane(self, required: bool = False) -> None:
        """Open the Metal lane only if it proves it shares ONNX's vector space.

        The capability check says "this machine could"; the parity gate says
        "and it agrees with the lane already in the index". Parity is necessary
        but not sufficient - it clears arithmetic drift, not the threshold flips
        that drift causes - so the lane that opens here is also recorded in the
        store, and only stores built by this lane will ever be served by it.
        """
        def refuse(reason: str) -> None:
            if required:
                raise EmbedderUnavailable(
                    f"the {LANE_METAL} lane was named as this vector space but "
                    f"{reason}; run where metal is available, or use the cpu "
                    f"lane (AGREP_MLX=off, and `agrep reindex --full` to rebuild "
                    f"a metal store)")
            self.metal_refusal = reason
            common.dbg(f"metal lane unavailable: {reason}", "~")

        if self.profile.get("provider", "CPUExecutionProvider") != "CPUExecutionProvider":
            # An explicitly pinned provider is the owner's decision; never
            # silently answer with a different engine than the one named.
            refuse("an ONNX execution provider is explicitly pinned")
            return
        ok, reason = mlx_embed.available()
        if not ok:
            refuse(reason)
            return
        cached = _cached_parity()
        if cached is not None and cached < _METAL_MIN_COSINE:
            # Re-proving this verdict cost ~150ms per construction. The key
            # covers the mlx build and model, so it cannot outlive either.
            refuse(f"its parity cosine {cached:.5f} is below "
                   f"{_METAL_MIN_COSINE} against the onnx lane (cached)")
            return
        try:
            lane = mlx_embed.MLXEmbedder()
            agreement = cached if cached is not None else self._metal_parity(lane)
        except Exception as exc:
            refuse(f"it did not load: {type(exc).__name__}: {exc}")
            return
        if cached is None:
            _store_parity(agreement)
        if agreement < _METAL_MIN_COSINE:
            refuse(f"its parity cosine {agreement:.5f} is below "
                   f"{_METAL_MIN_COSINE} against the onnx lane")
            return
        self._metal = lane
        self.lane = LANE_METAL
        # Not "parity" alone: 0.99933 is arithmetic agreement, and the 3/45
        # fixture flips prove that agreement still permits a different top-1
        # or a hit that becomes a refusal. The line has to say so.
        common.dbg(f"metal lane open (parity {agreement:.5f}; near-threshold "
                   f"results may differ from cpu)", "+")

    def _metal_parity(self, lane) -> float:
        """Worst-case cosine between the two lanes on fixed probe text.

        Worst-case, not mean: a lane that agrees on average and diverges on
        long messages is exactly the failure that mean pooling produced
        (0.878 mean but 0.769 min on real corpus rows), and the mean alone
        would have waved it through.
        """
        encodings = self.tok.encode_batch(list(_METAL_PROBES))
        width = max(len(e.ids) for e in encodings)
        ids = np.full((len(encodings), width), self.pad_id, dtype=np.int64)
        attention = np.zeros((len(encodings), width), dtype=np.int64)
        for row, enc in enumerate(encodings):
            n = len(enc.ids)
            ids[row, :n] = enc.ids
            attention[row, :n] = enc.attention_mask
        feed = {"input_ids": ids}
        if "attention_mask" in self.inputs:
            feed["attention_mask"] = attention
        if "token_type_ids" in self.inputs:
            feed["token_type_ids"] = np.full(
                (len(encodings), width), getattr(self, "pad_type_id", 0),
                dtype=np.int64)
        if "position_ids" in self.inputs:
            feed["position_ids"] = np.maximum(
                np.cumsum(attention, axis=1, dtype=np.int64) - 1, 0)
        onnx = self._pool_output(self._session_output(feed), attention)
        metal = self._finalize_pooled(
            lane.encode(ids, attention), len(encodings))
        return float((onnx * metal).sum(axis=1).min())

    @staticmethod
    def _padding_contract(tokenizer, required_token: str | None = None) -> tuple[int, int, str]:
        if required_token is not None:
            pad_id = tokenizer.token_to_id(required_token)
            if pad_id is None:
                raise EmbedderUnavailable(
                    f"profile pad token {required_token!r} is absent from the tokenizer")
            return int(pad_id), 0, required_token
        declared = tokenizer.padding
        if declared is not None:
            if declared.get("direction", "right") != "right":
                raise EmbedderUnavailable("only right-padding tokenizers are supported")
            try:
                return (int(declared["pad_id"]), int(declared.get("pad_type_id", 0)),
                        str(declared.get("pad_token") or ""))
            except (KeyError, TypeError, ValueError) as exc:
                raise EmbedderUnavailable("tokenizer padding metadata is invalid") from exc
        for token in ("[PAD]", "<pad>"):
            pad_id = tokenizer.token_to_id(token)
            if pad_id is not None:
                return int(pad_id), 0, token
        raise EmbedderUnavailable(
            "tokenizer has no declared, profile-pinned, or conventional pad token")

    def _validate_session(self) -> set[str]:
        input_meta = self.sess.get_inputs()
        inputs = {item.name for item in input_meta}
        if "input_ids" not in inputs:
            raise EmbedderUnavailable("ONNX model has no input_ids input")
        self.past_inputs = {}
        for item in input_meta:
            if not _PAST_INPUT_RE.fullmatch(item.name):
                continue
            shape = getattr(item, "shape", None)
            dtype = getattr(item, "type", None)
            if (not isinstance(shape, list) or len(shape) != 4
                    or not isinstance(shape[1], int) or shape[1] < 1
                    or not isinstance(shape[3], int) or shape[3] < 1
                    or dtype not in {"tensor(float)", "tensor(float16)"}):
                raise EmbedderUnavailable(
                    f"ONNX cache input {item.name!r} has an unsupported shape or type")
            self.past_inputs[item.name] = (
                shape[1], shape[3],
                np.float16 if dtype == "tensor(float16)" else np.float32)
        unknown = inputs - {
            "input_ids", "attention_mask", "token_type_ids", "position_ids",
        } - set(self.past_inputs)
        if unknown:
            names = ", ".join(sorted(unknown))
            raise EmbedderUnavailable(f"ONNX model has unsupported mandatory inputs: {names}")
        if "attention_mask" not in inputs:
            raise EmbedderUnavailable(
                "ONNX model must accept attention_mask for variable-length batches")
        outputs = [item.name for item in self.sess.get_outputs()]
        selector = self.profile.get("output", {"index": 0})
        if "index" in selector and selector["index"] >= len(outputs):
            raise EmbedderUnavailable(
                f"ONNX output index {selector['index']} is out of range for {len(outputs)} outputs")
        if "name" in selector and selector["name"] not in outputs:
            raise EmbedderUnavailable(
                f"ONNX output {selector['name']!r} is absent; found {outputs}")
        return inputs

    def _session_output(self, feed: dict[str, np.ndarray]) -> np.ndarray:
        selector = self.profile.get("output", {"index": 0})
        try:
            if "name" in selector:
                return np.asarray(self.sess.run([selector["name"]], feed)[0])
            outputs = self.sess.run(None, feed)
            return np.asarray(outputs[selector["index"]])
        except Exception as exc:
            raise EmbedderUnavailable(
                f"ONNX inference failed: {type(exc).__name__}: {exc}") from exc

    def _pool_output(self, output: np.ndarray, attention: np.ndarray) -> np.ndarray:
        pooling = self.profile.get("pooling", "cls")
        if output.ndim == 2:
            if pooling != "direct_2d":
                raise EmbedderUnavailable(
                    f"{pooling} pooling requires a rank-3 ONNX output")
            pooled = output
        elif output.ndim == 3:
            if output.shape[1] != attention.shape[1]:
                raise EmbedderUnavailable(
                    "ONNX token output width does not match the tokenizer attention mask")
            if pooling == "direct_2d":
                raise EmbedderUnavailable("direct_2d pooling requires a rank-2 ONNX output")
            if pooling == "cls":
                pooled = output[:, 0, :]
            elif pooling == "masked_mean":
                mask = attention.astype(np.float32)[..., None]
                pooled = (output * mask).sum(axis=1) / np.maximum(mask.sum(axis=1), 1.0)
            elif pooling == "last_token":
                present = attention.astype(bool)
                if np.any(~present.any(axis=1)):
                    raise EmbedderUnavailable("last-token pooling received an empty token row")
                last = present.shape[1] - 1 - np.argmax(present[:, ::-1], axis=1)
                pooled = output[np.arange(output.shape[0]), last]
            else:
                raise EmbedderUnavailable(f"unsupported pooling mode {pooling!r}")
        else:
            raise EmbedderUnavailable(
                f"ONNX embedding output must have rank 2 or 3, got rank {output.ndim}")
        return self._finalize_pooled(pooled, attention.shape[0])

    def _finalize_pooled(self, pooled: np.ndarray, rows: int) -> np.ndarray:
        """Everything after pooling: layernorm, truncation, normalize, guards.

        Shared by the ONNX and Metal lanes on purpose. Both write into one
        index, so the tail of the pipeline must be one artifact - a second
        copy is a second place for the two vector spaces to drift apart.
        """
        if pooled.shape[0] != rows:
            raise EmbedderUnavailable("ONNX embedding output batch dimension is wrong")
        native_dim = self.profile.get("native_dim", self.profile["dim"])
        if pooled.shape[1] != native_dim:
            raise EmbedderUnavailable(
                f"ONNX embedding width is {pooled.shape[1]}, expected {native_dim}")
        pooled = pooled.astype(np.float32)
        if not np.isfinite(pooled).all():
            raise EmbedderUnavailable("ONNX embedding output contains non-finite values")
        if self.profile.get("layernorm_before_truncate", False):
            mean = pooled.mean(axis=1, keepdims=True)
            variance = ((pooled - mean) ** 2).mean(axis=1, keepdims=True)
            pooled = (pooled - mean) / np.sqrt(variance + 1e-5)
        pooled = pooled[:, :self.profile["dim"]]
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        if np.any(norms <= 1e-12):
            raise EmbedderUnavailable("ONNX embedding output contains a zero-norm row")
        normalized = pooled / norms
        if not np.isfinite(normalized).all():
            raise EmbedderUnavailable("normalized embedding contains non-finite values")
        return normalized.astype(np.float32, copy=False)

    def _run_encoded(self, encodings) -> np.ndarray:
        """Pad already-tokenized rows once, then run the ONNX graph."""
        profile = getattr(self, "profile", PROFILE)
        if not encodings:
            return np.zeros((0, profile["dim"]), dtype=np.float32)
        width = max(len(e.ids) for e in encodings)
        input_ids = np.full(
            (len(encodings), width), self.pad_id, dtype=np.int64)
        attention = np.zeros((len(encodings), width), dtype=np.int64)
        token_types = np.full(
            (len(encodings), width), getattr(self, "pad_type_id", 0), dtype=np.int64)
        for row, encoding in enumerate(encodings):
            n = len(encoding.ids)
            input_ids[row, :n] = encoding.ids
            attention[row, :n] = encoding.attention_mask
            if encoding.type_ids:
                token_types[row, :n] = encoding.type_ids
        if getattr(self, "_metal", None) is not None:
            return self._finalize_pooled(
                self._metal_pooled(input_ids, attention), len(encodings))
        feed = {"input_ids": input_ids}
        if "attention_mask" in self.inputs:
            feed["attention_mask"] = attention
        if "token_type_ids" in self.inputs:
            feed["token_type_ids"] = token_types
        if "position_ids" in self.inputs:
            feed["position_ids"] = np.maximum(
                np.cumsum(attention, axis=1, dtype=np.int64) - 1, 0)
        for name, (heads, head_width, dtype) in getattr(
                self, "past_inputs", {}).items():
            feed[name] = np.zeros(
                (len(encodings), heads, 0, head_width), dtype=dtype)
        out = self._session_output(feed)
        return self._pool_output(out, attention)

    def _metal_pooled(self, input_ids: np.ndarray,
                      attention: np.ndarray) -> np.ndarray:
        """CLS vectors from the Metal lane.

        There is no per-batch engine choice. Courtesy toward the compositor is
        decided once, when the lane is picked, because the alternative - taking
        the GPU while the machine is idle and dropping to CPU when it is busy -
        writes two vector spaces into one store, keyed on nothing more principled
        than what the owner happened to be doing. A lane that dies mid-store is
        therefore an error and not a quiet fallback: the CPU lane is still
        correct, but it is no longer the space these rows live in.
        """
        try:
            return self._metal.encode(input_ids, attention)
        except Exception as exc:
            raise EmbedderUnavailable(
                f"the metal lane failed mid-store and the cpu lane cannot finish "
                f"its rows without mixing vector spaces: "
                f"{type(exc).__name__}: {exc}") from exc

    def _run(self, texts: list[str]) -> np.ndarray:
        try:
            encodings = self.tok.encode_batch(texts)
        except Exception as exc:
            raise EmbedderUnavailable(
                f"tokenizer encode failed: {type(exc).__name__}: {exc}") from exc
        return self._run_encoded(encodings)

    def embed_texts(self, texts: list[str], token_budget: int = 1024) -> np.ndarray:
        """Length-bucketed batches bounded by (batch_count x max_seq_in_batch):
        long pastes get tiny batches, short messages big ones, padding waste
        stays low, and rows return in ORIGINAL order so ids stay aligned.

        The 1k default is intentional: at the 1,024-token sequence cap it was 25%
        faster and used 56% less peak RSS than a 3k budget on the representative
        production sample. Larger padded batches increase attention/cache pressure
        faster than they amortize ONNX calls. This changes only scheduling—not
        truncation, pooling, or model space (batch shape can still cause ordinary
        floating-point drift).
        """
        token_budget = max(1, int(token_budget))
        prefix = getattr(self, "profile", PROFILE).get("document_prefix", "")
        source_texts = [prefix + text for text in texts] if prefix else texts
        out: list[np.ndarray | None] = [None] * len(texts)
        # Exact tokenizer lengths make the budget real; encode bounded windows
        # to avoid retaining every row's token ids at once.
        encode_window = 512
        for base in range(0, len(source_texts), encode_window):
            block = source_texts[base:base + encode_window]
            try:
                encodings = [self.tok.encode(text) for text in block]
            except Exception as exc:
                raise EmbedderUnavailable(
                    f"tokenizer encode failed: {type(exc).__name__}: {exc}") from exc
            order = sorted(range(len(block)), key=lambda i: len(encodings[i].ids))
            i = 0
            while i < len(order):
                j, maxlen = i, 0
                while j < len(order):
                    cand = len(encodings[order[j]].ids)
                    nm = maxlen if maxlen > cand else cand
                    if (j - i + 1) * nm > token_budget and j > i:
                        break
                    maxlen, j = nm, j + 1
                idx = order[i:j]
                vecs = self._run_encoded([encodings[k] for k in idx])
                for pos, k in enumerate(idx):
                    out[base + k] = vecs[pos]
                i = j
        return np.vstack(out).astype(np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        prefix = getattr(self, "profile", PROFILE).get("query_prefix", "")
        return self._run([prefix + text])[0]


_CACHED: dict = {"emb": None, "identity": None}


def model_loaded() -> bool:
    return _CACHED["emb"] is not None


def get(download: bool = True, lane: str | None = None) -> Embedder:
    """Return the process-wide embedding runtime for ``lane``.

    Cached on the lane that was ASKED for, not the one that opened: a default
    request that fell back to CPU must not re-attempt Metal on every query.
    Callers that need to know what they actually got read ``.profile_string``.
    """
    identity = profile_string(lane if lane is not None else default_lane())
    if _CACHED["emb"] is None or _CACHED["identity"] != identity:
        _CACHED["emb"] = Embedder(download=download, lane=lane)
        _CACHED["identity"] = identity
    return _CACHED["emb"]


def release() -> None:
    _CACHED["emb"] = None
    _CACHED["identity"] = None
