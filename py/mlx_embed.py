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
So this backend yields the GPU back rather than holding it - but through
pacing, not lane choice: indexer._embedding_backfill_policy widens the gap
between batches while the owner is active, on battery, or under memory
pressure. Lane choice cannot carry that duty, because a store keeps one
vector space for life and alternating engines by load would mix them.
"""
from __future__ import annotations

import os
import platform
import secrets
import shutil
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

class MLXUnavailable(RuntimeError):
    """This machine, build, or library cannot serve the Metal lane."""


def _apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"


# Without a Metal device the first `import mlx.core` raises ImportError and a
# second one SIGABRTs inside nanobind (duplicate DeviceType enum): ask once.
_IMPORT_VERDICT: tuple[bool, str] | None = None


def _import_verdict() -> tuple[bool, str]:
    global _IMPORT_VERDICT
    if _IMPORT_VERDICT is None:
        try:
            import mlx.core  # noqa: F401
        except ModuleNotFoundError as exc:
            _IMPORT_VERDICT = (False, f"mlx missing: {exc}")
        except ImportError as exc:
            _IMPORT_VERDICT = (
                False, f"mlx installed but no metal device opened: {exc}")
        else:
            _IMPORT_VERDICT = (True, "available")
    return _IMPORT_VERDICT


def available() -> tuple[bool, str]:
    """Capability only - never a benchmark. Returns (ok, reason).

    Kept to imports and platform facts so it can run on every load without
    costing anything; how FAST the lane is belongs to calibration, not here.
    The import itself is asked of the interpreter once per process.
    """
    if not _apple_silicon():
        return False, "metal lane requires apple silicon"
    if os.environ.get("AGREP_MLX") == "off":
        return False, "disabled by AGREP_MLX=off"
    return _import_verdict()


def weights_dir(root=None):
    """Where the Metal checkpoint lives: the ONNX lane's shared model root."""
    import embedder
    base = Path(root) if root is not None else embedder.model_root()
    return base / SUBDIR


def _legacy_weights_dir() -> Path:
    """Where releases before 0.4.3 kept the checkpoint: inside the data dir."""
    import common
    return Path(common.DATA_DIR) / "models" / SUBDIR


def _legacy_side_mutable(legacy: Path) -> bool:
    """Whether the legacy checkpoint's data dir may be touched at all."""
    import common
    import embedder
    if embedder._readonly_contains(legacy):
        return False
    return common.data_dir_mutable()


def _adopt_legacy_weights(target: Path) -> None:
    """Bring a verified pre-0.4.3 checkpoint into the shared cache, not re-fetch it.

    The legacy file stays in place until its copy is verified and published,
    so a kill at any point leaves at least one complete copy; the staging
    name is per process and `.adopt`, outside the downloader's `.part` sweep.
    """
    import embedder
    legacy = _legacy_weights_dir()
    if (not legacy.is_dir() or embedder._readonly_contains(target)
            or not _legacy_side_mutable(legacy)
            or os.path.realpath(legacy) == os.path.realpath(target)):
        return
    for name, (size, sha) in FILES.items():
        source = legacy / name
        # Releases before 0.4.3 downloaded into one fixed partial name; an
        # interrupted fetch left it behind with no owner and nothing sweeping.
        try:
            (legacy / f"{name}.part").unlink(missing_ok=True)
        except OSError:
            pass
        if embedder._file_ok(target / name, size, sha):
            try:
                source.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        if not embedder._file_ok(source, size, sha):
            continue
        staged = target / f".{name}.{os.getpid()}.{secrets.token_hex(8)}.adopt"
        try:
            target.mkdir(parents=True, exist_ok=True)
            try:
                os.link(source, staged)
            except OSError:
                shutil.copyfile(source, staged)
            if embedder._file_ok(staged, size, sha):
                os.replace(staged, target / name)
                if embedder._file_ok(target / name, size, sha):
                    source.unlink(missing_ok=True)
        except OSError:
            pass
        finally:
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                pass
    for directory in (legacy, legacy.parent):
        try:
            directory.rmdir()
        except OSError:
            break


def _sweep_dead_adopters(target: Path) -> None:
    """Drop `.name.pid.hex.adopt` staging files whose adopter is gone; the
    legacy copy they were taken from is still in place, so nothing is lost."""
    import common
    import embedder
    if embedder._readonly_contains(target):
        return
    for staged in target.glob(".*.adopt"):
        parts = staged.name.split(".")
        try:
            pid = int(parts[-3])
        except (IndexError, ValueError):
            continue
        if pid == os.getpid() or common.pid_alive(pid):
            continue
        try:
            staged.unlink()
        except OSError:
            pass


def ensure_weights(root=None, download: bool = True):
    """The pinned checkpoint on disk, fetched once if absent.

    Reuses the ONNX lane's verified fetch rather than growing a second
    downloader: same size+digest check, same per-process partials and
    download claim, same refusal to accept bytes that do not match, so an
    accelerator cannot become the soft path into the model directory.
    """
    import embedder
    target = weights_dir(root)
    _adopt_legacy_weights(target)
    _sweep_dead_adopters(target)
    urls = {name: (f"https://huggingface.co/{SUPPORTED_REPO}/resolve/"
                   f"{REVISION}/{name}") for name in FILES}
    try:
        return embedder._ensure_pinned_files(
            target, FILES, urls, download=download, noun="metal weights")
    except embedder.EmbedderUnavailable as exc:
        raise MLXUnavailable(str(exc)) from exc


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
