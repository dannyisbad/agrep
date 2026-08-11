"""Metal-backed embedding for Apple Silicon, behind the same vector contract.

WHY THIS EXISTS. The ONNX int8 graph runs on CPU at ~35 texts/s on the owner's
M5 Pro against a real corpus; the same model in MLX runs at ~380 texts/s -
measured interleaved in one process so both engines saw identical machine load
(median of 4: ONNX 8.62s, MLX 0.79s, 300 messages, agrep's own 1,024-token
bucketing). A three-hour backfill becomes ~16 minutes. ONNX Runtime's CoreML
provider is NOT the path: it was 2.5x SLOWER than CPU because the int8 graph
partitions badly and shuttles tensors, and GPU/ANE/ALL all landed within noise
of each other. MLX compiles to Metal directly, so none of that applies.

THE VECTOR CONTRACT IS THE WHOLE RISK. Rows embedded here land in the same
index as rows embedded by ONNX, and a query may be embedded by either engine,
so the two must produce the SAME space - not merely a good space. The first
implementation used mlx-embeddings, whose ModernBERT defaults to MEAN pooling
against agrep's CLS contract: on real corpus messages that measured 0.878 mean
cosine, min 0.769, while every shape and finiteness check passed. That is why
the encoder now lives in py/mlx_modernbert.py with CLS as the only thing it
can do, and why `embedder._start_metal_lane` refuses to open this lane until
it reproduces the ONNX vectors numerically (0.99933 worst case here) rather
than merely importing successfully.

RESOURCE ETIQUETTE. On Apple Silicon the GPU is also the display compositor -
WindowServer was measured at 46% GPU during this work. A background backfill
that saturates Metal does not just compete for FLOPs, it makes scrolling and
window animation stutter, which reads to the owner as "agrep broke my Mac".
So this backend is gated on live load and yields the GPU back rather than
holding it: see `should_use`.
"""
from __future__ import annotations

import os
import platform
from pathlib import Path
import sys

import numpy as np

# The repo id and pooling mode jointly pin this backend's vector space. These are
# the ONNX lane's pre-quantization weights, so the two lanes agree to 0.9987
# rather than merely correlating; a different model profile must not reuse them.
SUPPORTED_REPO = "ibm-granite/granite-embedding-small-english-r2"
REVISION = "2ab6fa8ea2d674564defd37171ae19079b864b33"
REQUIRED_POOLING = "cls"

# Pinned by size and digest exactly like the ONNX artifacts: an accelerator is
# not a reason to relax provenance, and these bytes decide a vector space.
FILES = {
    "model.safetensors": (
        95332048, "dcbfaf2ee50d358763cbc0c08e5b045ecbf6aeb02dc0e86ffb910029bf9ebb5f"),
    "config.json": (
        1315, "154518e6452854eb11123ce433d165f2773437aa0f36935d2204249b2c327198"),
}
# Subdirectory of the shared model root, so `agrep`'s existing model directory
# stays one place a user can delete to reclaim everything.
SUBDIR = "granite-small-r2-mlx"

# Load per core is a free but imprecise proxy for Metal/compositor contention;
# macOS exposes no unprivileged GPU-utilization metric. A measured 0.9 ceiling
# yields only under real saturation, while 0.7 flapped and a flat 6.0 stayed off.
DEFAULT_LOAD_PER_CORE = 0.9


class MLXUnavailable(RuntimeError):
    """This machine, build, or library cannot serve the Metal lane."""


def _apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"


def available() -> tuple[bool, str]:
    """Capability only - never a benchmark. Returns (ok, reason).

    Kept to imports and platform facts so it can run on every load without
    costing anything; how FAST the lane is belongs to calibration, not here.
    """
    if not _apple_silicon():
        return False, "metal lane requires apple silicon"
    if os.environ.get("AGREP_MLX") == "off":
        return False, "disabled by AGREP_MLX=off"
    try:
        import mlx.core  # noqa: F401
    except ImportError as exc:
        return False, f"mlx missing: {exc}"
    return True, "available"


def should_use(per_core: float | None = None) -> tuple[bool, str]:
    """Per-batch decision: is taking the GPU polite RIGHT NOW?

    Cheap enough to call before every batch (one getloadavg), which is the
    point - a boot-time choice cannot know that the owner started a render
    ten minutes later. An explicit AGREP_MLX=on pins the lane on and skips
    the courtesy check, because a foreground `agrep index` is work the owner
    is already waiting for.
    """
    if os.environ.get("AGREP_MLX") == "on":
        return True, "pinned on"
    ceiling = DEFAULT_LOAD_PER_CORE if per_core is None else per_core
    try:
        load1 = os.getloadavg()[0]
        cores = os.cpu_count() or 1
    except (OSError, AttributeError):
        # No load signal is not permission to take the GPU.
        return False, "load unreadable"
    ratio = load1 / max(cores, 1)
    if ratio > ceiling:
        return False, (f"machine busy ({load1:.1f} over {cores} cores "
                       f"= {ratio:.2f} > {ceiling:.2f})")
    return True, f"idle enough ({load1:.1f}/{cores} = {ratio:.2f})"


def weights_dir(root=None):
    """Where the Metal checkpoint lives, under the shared model root."""
    import common
    base = Path(root) if root is not None else Path(common.DATA_DIR) / "models"
    return base / SUBDIR


def ensure_weights(root=None, download: bool = True):
    """The pinned checkpoint on disk, fetched once if absent.

    Reuses the ONNX lane's verified fetch rather than growing a second
    downloader: same size+digest check, same refusal to accept bytes that do
    not match, so an accelerator cannot become the soft path into the model
    directory.
    """
    import embedder
    target = weights_dir(root)
    missing = [name for name, (size, sha) in FILES.items()
               if not embedder._file_ok(target / name, size, sha)]
    if not missing:
        return target
    if not download:
        raise MLXUnavailable(
            f"metal weights absent ({', '.join(missing)}) and download disabled")
    target.mkdir(parents=True, exist_ok=True)
    for name in missing:
        size, sha = FILES[name]
        url = (f"https://huggingface.co/{SUPPORTED_REPO}/resolve/"
               f"{REVISION}/{name}")
        part = target / f"{name}.part"
        try:
            embedder._fetch_pinned(url, part, size)
            if not embedder._file_ok(part, size, sha):
                part.unlink(missing_ok=True)
                raise MLXUnavailable(
                    f"metal weight {name} failed its pinned digest")
            part.replace(target / name)
        except MLXUnavailable:
            raise
        except Exception as exc:
            part.unlink(missing_ok=True)
            raise MLXUnavailable(
                f"metal weight {name} could not be fetched: "
                f"{type(exc).__name__}: {exc}") from exc
    return target


class MLXEmbedder:
    """One loaded Metal model that answers in the ONNX lane's vector space."""

    def __init__(self, root=None, download: bool = True):
        ok, reason = available()
        if not ok:
            raise MLXUnavailable(reason)
        try:
            import mlx.core as mx
            import mlx_modernbert
        except ImportError as exc:  # pragma: no cover - available() covers it
            raise MLXUnavailable(f"mlx import failed: {exc}") from exc
        self._mx = mx
        weights = ensure_weights(root=root, download=download)
        try:
            self.model = mlx_modernbert.load_encoder(
                weights / "model.safetensors", weights / "config.json")
        except Exception as exc:
            raise MLXUnavailable(
                f"metal model load failed: {type(exc).__name__}: {exc}") from exc

    def encode(self, input_ids: np.ndarray,
               attention_mask: np.ndarray) -> np.ndarray:
        """CLS vectors, unnormalized, shaped (batch, native_dim).

        Deliberately stops where ONNX's `_pool_output` picks up: layernorm,
        dimension truncation, normalization and the finite/zero-norm guards
        stay in ONE place so the two lanes cannot drift apart in the tail of
        the pipeline.
        """
        mx = self._mx
        ids = mx.array(np.ascontiguousarray(input_ids, dtype=np.int32))
        mask = mx.array(np.ascontiguousarray(attention_mask, dtype=np.int32))
        pooled = self.model(ids, mask)
        if pooled.ndim != 2:
            raise MLXUnavailable(
                f"metal lane returned rank {pooled.ndim}, expected pooled rank 2")
        return np.asarray(pooled.astype(mx.float32))
