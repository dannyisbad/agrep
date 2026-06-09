"""Shared helpers for the tilt semantic sidecar.

This module is the Python side of the EMBEDDING CONTRACT that the Rust reader
(crates/tilt-core) and these scripts must agree on EXACTLY:

  data/messages.jsonl  one JSON object per line:
                         {id, agent, project, session, ts, turn, text}
                       id == "agent:session:turn". Produced by `tilt scan`.

  data/embeddings.f32  raw little-endian float32, row-major, N rows x D cols.
                       Each ROW is L2-normalized. D == 256 (Matryoshka
                       truncation of a 1024-d model, then renormalized).

  data/embeddings.ids  UTF-8, one message id per line. Row r of embeddings.f32
                       corresponds to line r here. The ids file is the
                       AUTHORITY for row order; it need not match the order in
                       messages.jsonl.

  data/query.f32       a single D-dim L2-normalized float32 vector.

Because every stored row is L2-normalized, cosine similarity == dot product,
which is exactly what the Rust AVX2 brute-force kernel computes.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence

import numpy as np

# --- Layout ---------------------------------------------------------------

# This file lives in <repo>/py/. Data lives in <repo>/data/.
PY_DIR = Path(__file__).resolve().parent
REPO_ROOT = PY_DIR.parent
DATA_DIR = REPO_ROOT / "data"

MESSAGES_PATH = DATA_DIR / "messages.jsonl"
EMBEDDINGS_PATH = DATA_DIR / "embeddings.f32"
IDS_PATH = DATA_DIR / "embeddings.ids"
QUERY_PATH = DATA_DIR / "query.f32"
EMOTIONS_PATH = DATA_DIR / "emotions.jsonl"

# The contract dimension. Matryoshka truncation target; renormalized after.
# Bumped 256 -> 1024 (full Qwen3-Embedding width) for better recall. The Rust reader
# is self-describing (reads embeddings.meta), so this stays in sync automatically.
EMBED_DIM = 1024


# --- Message loading ------------------------------------------------------


@dataclass(frozen=True)
class Message:
    """One row from messages.jsonl. Mirrors crates/tilt-core/src/model.rs."""

    id: str
    agent: str
    project: str
    session: str
    ts: int
    turn: int
    text: str


def iter_messages(path: Path = MESSAGES_PATH, limit: int | None = None) -> Iterator[Message]:
    """Yield Message records from a JSONL file.

    Skips blank lines and lines that don't parse / are missing id+text, logging
    a warning to stderr so a single bad row never aborts a long embed run.
    `limit` (used by --smoke) caps how many *valid* rows are yielded.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Run `tilt scan` first to produce messages.jsonl."
        )

    yielded = 0
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                log(f"warn: skipping malformed JSON at {path.name}:{lineno}: {exc}")
                continue
            mid = obj.get("id")
            text = obj.get("text")
            if not mid or text is None:
                log(f"warn: skipping row missing id/text at {path.name}:{lineno}")
                continue
            yield Message(
                id=mid,
                agent=obj.get("agent", ""),
                project=obj.get("project", ""),
                session=obj.get("session", ""),
                ts=int(obj.get("ts", 0)),
                turn=int(obj.get("turn", 0)),
                text=text,
            )
            yielded += 1
            if limit is not None and yielded >= limit:
                return


def count_messages(path: Path = MESSAGES_PATH) -> int:
    """Count non-blank lines without fully parsing each row."""
    n = 0
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                n += 1
    return n


# --- Embedding math + IO --------------------------------------------------


def l2_normalize(mat: np.ndarray, eps: float = 1e-12) -> np.ndarray:
    """Row-wise L2 normalization. Returns float32, contiguous, row-major.

    Zero (or near-zero) rows are left as zeros rather than divided — a zero
    vector has cosine 0 against everything, which is the sane fallback for an
    empty/degenerate message.
    """
    mat = np.asarray(mat, dtype=np.float32)
    if mat.ndim == 1:
        mat = mat[None, :]
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms = np.where(norms < eps, 1.0, norms)
    out = mat / norms
    return np.ascontiguousarray(out, dtype=np.float32)


def matryoshka_truncate(mat: np.ndarray, dim: int = EMBED_DIM) -> np.ndarray:
    """Truncate to the first `dim` columns (Matryoshka), then L2-renormalize.

    Qwen3-Embedding and the BGE family are trained so the leading dimensions
    carry the most information, so a plain prefix slice is a valid lower-d
    embedding once renormalized. If the model already emits <= dim columns we
    just renormalize the whole thing.
    """
    mat = np.asarray(mat, dtype=np.float32)
    if mat.ndim == 1:
        mat = mat[None, :]
    if mat.shape[1] > dim:
        mat = mat[:, :dim]
    return l2_normalize(mat)


def jsonl_ids(path: Path, key: str = "id") -> set[str]:
    """Set of `key` values already present in a JSONL file (empty if it's absent).

    The incremental pipeline uses this to skip work already done: only messages /
    sessions whose key is NOT in here get re-embedded / re-scored / re-summarized.
    """
    out: set[str] = set()
    if not path.exists():
        return out
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                v = json.loads(line).get(key)
            except json.JSONDecodeError:
                continue
            if v:
                out.add(v)
    return out


def write_embeddings(
    ids: Sequence[str],
    embeddings: np.ndarray,
    embeddings_path: Path = EMBEDDINGS_PATH,
    ids_path: Path = IDS_PATH,
    dim: int = EMBED_DIM,
) -> None:
    """Write the embeddings.f32 + embeddings.ids pair atomically-ish.

    `embeddings` must be (N, dim) and is assumed already truncated+normalized
    (use matryoshka_truncate first). We assert the shape and dtype rather than
    silently coercing, so a contract mismatch fails loudly here instead of in
    the Rust reader.
    """
    embeddings = np.ascontiguousarray(embeddings, dtype="<f4")  # little-endian f32
    if embeddings.ndim != 2 or embeddings.shape[1] != dim:
        raise ValueError(
            f"embeddings must be (N, {dim}); got {embeddings.shape}. "
            "Did you forget matryoshka_truncate()?"
        )
    if len(ids) != embeddings.shape[0]:
        raise ValueError(
            f"ids/embeddings row mismatch: {len(ids)} ids vs {embeddings.shape[0]} rows"
        )

    embeddings_path.parent.mkdir(parents=True, exist_ok=True)

    # Write to temp files then rename, so a crash mid-write can't leave the
    # Rust reader staring at a half-written matrix whose row count disagrees
    # with the ids file.
    tmp_emb = embeddings_path.with_suffix(embeddings_path.suffix + ".tmp")
    tmp_ids = ids_path.with_suffix(ids_path.suffix + ".tmp")

    # tobytes() respects the '<f4' dtype above => guaranteed little-endian.
    tmp_emb.write_bytes(embeddings.tobytes(order="C"))
    with tmp_ids.open("w", encoding="utf-8", newline="\n") as f:
        for mid in ids:
            f.write(mid)
            f.write("\n")

    tmp_emb.replace(embeddings_path)
    tmp_ids.replace(ids_path)

    # Self-describing index: the Rust reader reads this instead of hardcoding the dim.
    (embeddings_path.parent / "embeddings.meta").write_text(str(dim), encoding="utf-8")


def write_query(vec: np.ndarray, dim: int = EMBED_DIM, query_path: Path = QUERY_PATH) -> None:
    """Write a single D-dim L2-normalized query vector to data/query.f32."""
    vec = np.asarray(vec, dtype=np.float32).reshape(-1)
    if vec.shape[0] > dim:
        vec = vec[:dim]
    vec = l2_normalize(vec).reshape(-1)
    if vec.shape[0] != dim:
        raise ValueError(f"query must be {dim}-dim; got {vec.shape[0]}")
    vec = np.ascontiguousarray(vec, dtype="<f4")
    query_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = query_path.with_suffix(query_path.suffix + ".tmp")
    tmp.write_bytes(vec.tobytes(order="C"))
    tmp.replace(query_path)


def read_embeddings(
    embeddings_path: Path = EMBEDDINGS_PATH,
    ids_path: Path = IDS_PATH,
    dim: int = EMBED_DIM,
) -> tuple[list[str], np.ndarray]:
    """Read back the (ids, matrix) pair. Mirror of the Rust reader; handy for
    sanity-checking the contract from Python."""
    ids = ids_path.read_text(encoding="utf-8").splitlines()
    raw = np.fromfile(embeddings_path, dtype="<f4")
    if raw.size % dim != 0:
        raise ValueError(
            f"{embeddings_path} has {raw.size} floats, not a multiple of dim={dim}"
        )
    mat = raw.reshape(-1, dim)
    if mat.shape[0] != len(ids):
        raise ValueError(
            f"row/id count mismatch: {mat.shape[0]} rows vs {len(ids)} ids"
        )
    return ids, mat


# --- resolve-or-fallback model loader -------------------------------------


@dataclass(frozen=True)
class ResolvedModel:
    """Which model id actually loaded, and the index into the candidate list
    (0 == primary). Callers branch on `index`/`id` to pick model-specific
    pooling and instruction handling."""

    id: str
    index: int
    obj: object  # the loaded SentenceTransformer / pipeline / model+tokenizer


def resolve_model(
    candidates: Sequence[str],
    loader,
    label: str = "model",
) -> ResolvedModel:
    """Try each candidate id in order; return the first that loads.

    `loader(model_id)` is a callable that actually loads the model (e.g. wraps
    SentenceTransformer(...) or a transformers pipeline). On ANY exception
    (404 / gated repo / OOM / load error) we log which id failed and why, then
    fall through to the next candidate. Raises RuntimeError only if every
    candidate fails.
    """
    if not candidates:
        raise ValueError("resolve_model: empty candidate list")

    last_exc: Exception | None = None
    for idx, model_id in enumerate(candidates):
        tier = "PRIMARY" if idx == 0 else f"FALLBACK[{idx}]"
        log(f"{label}: trying {tier} '{model_id}' ...")
        try:
            obj = loader(model_id)
        except Exception as exc:  # noqa: BLE001 - any load failure => next candidate
            last_exc = exc
            log(f"{label}: '{model_id}' failed to load ({type(exc).__name__}: {exc}); "
                f"falling back.")
            continue
        log(f"{label}: USING '{model_id}' ({tier}).")
        return ResolvedModel(id=model_id, index=idx, obj=obj)

    raise RuntimeError(
        f"{label}: every candidate failed to load: {list(candidates)} "
        f"(last error: {type(last_exc).__name__ if last_exc else 'none'}: {last_exc})"
    )


# --- misc -----------------------------------------------------------------


def pick_device() -> str:
    """Return 'cuda' if a usable GPU is present, else 'cpu'. Imports torch
    lazily so this module stays importable even before torch is installed."""
    try:
        import torch  # noqa: PLC0415

        if torch.cuda.is_available():
            return "cuda"
    except Exception:  # noqa: BLE001
        pass
    return "cpu"


def approx_vram_mb() -> float | None:
    """Best-effort peak CUDA allocation in MiB, or None on CPU / no torch."""
    try:
        import torch  # noqa: PLC0415

        if torch.cuda.is_available():
            return torch.cuda.max_memory_allocated() / (1024 * 1024)
    except Exception:  # noqa: BLE001
        pass
    return None


def log(msg: str) -> None:
    """Stderr logging so stdout stays clean for any machine-readable output."""
    print(msg, file=sys.stderr, flush=True)


def iter_batches(seq: Sequence, size: int) -> Iterable[Sequence]:
    """Yield consecutive slices of `seq` of length `size`."""
    for i in range(0, len(seq), size):
        yield seq[i : i + size]
