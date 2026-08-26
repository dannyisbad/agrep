"""Embedding identity, publication, and legacy matrix storage."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Iterator, NamedTuple, Sequence

from events import DATA_DIR
from fileops import replace_with_retry
from hookless._log import log
import ownerfile
from proc import WIN, pid_alive, process_start_identity
from resources import EMBEDDINGS_PATH
from session_context import cli_name


EMBED_DIM = 384
MESSAGES_PATH = DATA_DIR / "messages.jsonl"
IDS_PATH = DATA_DIR / "embeddings.ids"


def _publication_target_protected(*paths: Path) -> bool:
    """Whether an explicit publication would mutate the protected data root."""
    protected = os.environ.get("AGREP_DATA_READONLY")
    if not protected:
        return False
    try:
        root = os.path.normcase(os.path.realpath(protected))
        for path in paths:
            target = os.path.normcase(os.path.realpath(os.fspath(path)))
            if os.path.commonpath((root, target)) == root:
                return True
        return False
    except (OSError, ValueError):
        return True


def _require_publication_target(action: str, *paths: Path) -> None:
    if _publication_target_protected(*paths):
        raise PermissionError(
            f"AGREP_DATA_READONLY protects the data directory; cannot {action}")


def semantic_text_hash(text: str) -> str:
    """Stable text fingerprint stored beside semantic vectors and references."""
    import hashlib
    return hashlib.blake2b(
        text.encode("utf-8", "replace"), digest_size=8).hexdigest()


_SEMANTIC_CHUNK_SUFFIX = re.compile(r"#c([0-9]+)$")


def semantic_chunk_split(mid: str) -> "tuple[str, int]":
    """(logical row id, chunk ordinal) for one semantic store id.

    A long row embeds as several vectors: the unsuffixed id keeps the head
    chunk and '#cN' siblings carry the rest, following the '#r' reply-suffix
    convention. Ordinal 0 names the base vector; ids without the suffix are
    their own logical row."""
    match = _SEMANTIC_CHUNK_SUFFIX.search(mid)
    return (mid[:match.start()], int(match.group(1))) if match else (mid, 0)


class Message(NamedTuple):
    """One row from messages.jsonl. Mirrors crates/agrep-core/src/model.rs."""

    id: str
    agent: str
    project: str
    session: str
    ts: int
    turn: int
    text: str
    who: str
    model: str
    model_source: str


def iter_messages(path: Path = MESSAGES_PATH, limit: int | None = None) -> Iterator[Message]:
    """Yield Message records from a JSONL file.

    Skips blank lines and lines that don't parse / are missing id+text, logging
    a warning to stderr so a single bad row never aborts a long embed run.
    `limit` (used by --smoke) caps how many *valid* rows are yielded.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Run `{cli_name()} index` first to produce messages.jsonl."
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
                who=obj.get("who", "user"),
                model=obj.get("model", ""),
                model_source=obj.get(
                    "model_source",
                    "explicit" if obj.get("model") else "unknown",
                ),
            )
            yielded += 1
            if limit is not None and yielded >= limit:
                return


# --- Embedding math + IO --------------------------------------------------


def l2_normalize(mat: np.ndarray, eps: float = 1e-12) -> np.ndarray:
    """Row-wise L2 normalization. Returns float32, contiguous, row-major.

    Zero (or near-zero) rows are left as zeros rather than divided - a zero
    vector has cosine 0 against everything, which is the sane fallback for an
    empty/degenerate message.
    """
    import numpy as np  # lazy: embedding-contract helpers only

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
    import numpy as np  # lazy: embedding-contract helpers only

    mat = np.asarray(mat, dtype=np.float32)
    if mat.ndim == 1:
        mat = mat[None, :]
    if mat.shape[1] > dim:
        mat = mat[:, :dim]
    return l2_normalize(mat)


def write_index_meta(meta_path: Path, dim: int, model_id: str | None) -> None:
    """Self-describing index meta: the REAL row width plus the embedder that
    produced it. Recording the model id is what lets a reader refuse a query
    whose embedder lives in a different vector space (a silent garbage result
    otherwise). Written as JSON; read_index_meta still accepts the legacy
    bare-int form so old indexes keep loading."""
    _require_publication_target("write embedding metadata", meta_path)
    meta_path.write_text(json.dumps({"dim": int(dim), "model": model_id or ""}),
                         encoding="utf-8")


def read_index_meta(meta_path: Path) -> tuple[int, str | None]:
    """(dim, model_id) for an *_emb.meta file. model_id is None for a legacy
    bare-int meta (model unknown -> caller can't verify the space, only warn)."""
    raw = meta_path.read_text(encoding="utf-8").strip()
    try:
        o = json.loads(raw)
    except json.JSONDecodeError:
        return int(raw), None
    if isinstance(o, dict):
        if o.get("version") == 2 and isinstance(o.get("model"), dict):
            model = o["model"]
            return int(model["dim"]), (model.get("id") or None)
        return int(o["dim"]), (o.get("model") or None)
    return int(o), None  # json of a bare int


_EMBEDDING_COMMIT_VERSION = 1


def _embedding_file_identity(path: Path) -> dict[str, int]:
    """Cheap identity for one immutable publication artifact.

    ``st_ino`` is the NTFS file id on Windows and the inode on POSIX. Size and
    mtime remain useful on filesystems that report a zero/unstable file id.
    ``ctime`` is deliberately excluded: creating the Windows hardlink snapshot
    changes link metadata without changing the file generation.
    """
    stat = path.stat()
    return {
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "dev": int(stat.st_dev),
        "ino": int(stat.st_ino),
    }


def _embedding_identity_matches(expected: dict, path: Path) -> bool:
    try:
        actual = _embedding_file_identity(path)
    except OSError:
        return False
    return _embedding_identity_records_match(expected, actual)


def _embedding_identity_records_match(expected: dict, actual: dict) -> bool:
    for key in ("size", "mtime_ns"):
        if int(expected.get(key, -1)) != actual[key]:
            return False
    # Some network/FAT filesystems expose no stable file id. Compare it whenever
    # both sides have one, otherwise retain the size+mtime compatibility fallback.
    for key in ("dev", "ino"):
        want = int(expected.get(key, 0) or 0)
        if want and actual[key] and want != actual[key]:
            return False
    return True


def _embedding_commit_from_meta(raw: bytes | str, meta_path: Path) -> dict | None:
    record = json.loads(raw)
    if not isinstance(record, dict):
        return None
    if record.get("state") == "publishing":
        raise ValueError(f"embedding publication is incomplete in {meta_path}")
    if record.get("version") == 2:
        generation = str(record.get("generation") or "")
        rows = record.get("live_rows")
        if (len(generation) != 32 or any(char not in "0123456789abcdef"
                                        for char in generation)
                or type(rows) is not int or rows <= 0):
            raise ValueError(f"invalid segmented embedding commit in {meta_path}")
        return {"version": 2, "generation": generation, "rows": rows,
                "segmented": True}
    commit = record.get("commit")
    if commit is None:
        return None
    if not isinstance(commit, dict) or int(commit.get("version", 0)) != _EMBEDDING_COMMIT_VERSION:
        raise ValueError(f"unsupported embedding commit in {meta_path}")
    if not commit.get("generation"):
        raise ValueError(f"embedding commit in {meta_path} has no generation")
    return commit


def _segmented_embedding_state(meta_path: Path, attempts: int = 4) -> dict:
    """Validated identity for an immutable v2 segment set."""
    import hashlib
    import embedding_segments

    attempts = max(1, int(attempts))
    for attempt in range(attempts):
        before = meta_path.read_bytes()
        manifest = embedding_segments.load_manifest(meta_path, retries=1)
        artifacts = {"meta": _embedding_file_identity(meta_path)}
        for path in embedding_segments.referenced_paths(manifest):
            relative = str(path.relative_to(meta_path.parent)).replace(os.sep, "/")
            artifacts[relative] = _embedding_file_identity(path)
        if before != meta_path.read_bytes():
            if attempt + 1 < attempts:
                time.sleep(0.005 * (attempt + 1))
                continue
            raise ValueError("segmented embedding publication moved during identity read")
        identity_record = {
            "generation": manifest["generation"],
            "model": manifest["model"],
            "live_rows": manifest["live_rows"],
            "artifacts": artifacts,
        }
        digest = hashlib.sha256(json.dumps(
            identity_record, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        return {
            "identity": f"segments:{manifest['generation']}:{digest}",
            "commit": {"version": 2, "generation": manifest["generation"],
                       "rows": manifest["live_rows"], "segmented": True},
            "artifacts": artifacts,
            "manifest": dict(manifest),
        }
    raise ValueError("could not read segmented embedding identity")


def _validate_embedding_commit(
    commit: dict,
    artifacts: dict[str, dict],
    ids_bytes: bytes,
    hashes_bytes: bytes | None,
) -> None:
    import hashlib

    for key in ("matrix", "ids"):
        expected = commit.get(key)
        if (not isinstance(expected, dict)
                or not _embedding_identity_records_match(expected, artifacts[key])):
            raise ValueError(f"embedding {key} is not the committed generation")
    if commit["ids"].get("sha256"):
        if hashlib.sha256(ids_bytes).hexdigest() != commit["ids"]["sha256"]:
            raise ValueError("embedding ids digest does not match committed generation")
    expected_hashes = commit.get("hashes")
    if expected_hashes is not None:
        if hashes_bytes is None or "hashes" not in artifacts:
            raise ValueError("embedding text hashes are missing from committed generation")
        if (not isinstance(expected_hashes, dict)
                or not _embedding_identity_records_match(
                    expected_hashes, artifacts["hashes"])):
            raise ValueError("embedding text hashes are not the committed generation")
        if (expected_hashes.get("sha256")
                and hashlib.sha256(hashes_bytes).hexdigest() != expected_hashes["sha256"]):
            raise ValueError("embedding text hash digest does not match committed generation")


def _embedding_bundle_identity(
    commit: dict | None,
    artifacts: dict[str, dict],
    ids_bytes: bytes,
) -> str:
    """Opaque identity shared by a loaded mmap and its metadata/text sidecar."""
    import hashlib

    if commit is not None:
        digest = hashlib.sha256(json.dumps(
            {"commit": commit, "artifacts": artifacts},
            sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        return f"commit:{commit['generation']}:{digest}"
    legacy = {
        "artifacts": artifacts,
        "ids_sha256": hashlib.sha256(ids_bytes).hexdigest(),
    }
    digest = hashlib.sha256(json.dumps(
        legacy, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return f"legacy:{digest}"


def _stable_embedding_artifacts(
    meta_path: Path,
    embeddings_path: Path,
    ids_path: Path,
    attempts: int = 4,
) -> tuple[dict | None, dict[str, dict], bytes, bytes | None]:
    """Read identities/digests from one stable canonical artifact generation."""
    attempts = max(1, int(attempts))
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            meta_raw = meta_path.read_bytes()
            commit = _embedding_commit_from_meta(meta_raw, meta_path)
            hashes_path = embeddings_path.with_suffix(".hashes")
            include_hashes = bool(commit and commit.get("hashes")) or hashes_path.exists()
            named_paths = {
                "matrix": embeddings_path, "ids": ids_path, "meta": meta_path,
            }
            if include_hashes:
                named_paths["hashes"] = hashes_path
            before = {key: _embedding_file_identity(path)
                      for key, path in named_paths.items()}
            ids_bytes = ids_path.read_bytes()
            hashes_bytes = hashes_path.read_bytes() if include_hashes else None
            after = {key: _embedding_file_identity(path)
                     for key, path in named_paths.items()}
            if before != after or meta_raw != meta_path.read_bytes():
                raise RuntimeError("embedding publication changed during identity read")
            if commit is not None:
                _validate_embedding_commit(commit, after, ids_bytes, hashes_bytes)
                meta_record = json.loads(meta_raw)
                if not isinstance(meta_record, dict):
                    raise ValueError("committed embedding metadata is not an object")
                dim = int(meta_record.get("dim", 0))
                rows = int(commit.get("rows", -1))
                ids_rows = len(ids_bytes.decode("utf-8").splitlines())
                if dim <= 0 or rows < 0:
                    raise ValueError("embedding commit has an invalid dimension or row count")
                if ids_rows != rows:
                    raise ValueError(
                        f"committed row/id count mismatch: {rows} vs {ids_rows}")
                if after["matrix"]["size"] != rows * dim * 4:
                    raise ValueError(
                        "embedding matrix size does not match committed rows and dimension")
                if hashes_bytes is not None:
                    hash_rows = len(hashes_bytes.decode("utf-8").splitlines())
                    if hash_rows != rows:
                        raise ValueError(
                            f"committed row/hash count mismatch: {rows} vs {hash_rows}")
            return commit, after, ids_bytes, hashes_bytes
        except (OSError, RuntimeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.01 * (attempt + 1))
                continue
            raise ValueError(f"could not read coherent embedding identity: {exc}") from exc
    assert last_error is not None
    raise last_error


def read_embedding_commit(
    meta_path: Path,
    embeddings_path: Path | None = None,
    ids_path: Path | None = None,
) -> dict | None:
    """Return and optionally validate a publish-last embedding commit.

    Old metadata contained only ``dim`` and ``model``. Returning ``None`` for
    those artifacts is intentional compatibility: callers still perform stable
    stat and row-count checks, and the next writer run upgrades the pair. A v1
    commit is strict; a mismatched matrix, ids file, or ids digest is a partial or
    corrupt publication and raises instead of serving mixed generations.
    """
    raw = meta_path.read_bytes()
    record = json.loads(raw)
    if isinstance(record, dict) and record.get("version") == 2:
        return _segmented_embedding_state(meta_path)["commit"]
    if embeddings_path is None or ids_path is None:
        return _embedding_commit_from_meta(raw, meta_path)
    commit, _, _, _ = _stable_embedding_artifacts(
        meta_path, embeddings_path, ids_path)
    return commit


def embedding_artifact_identity(
    meta_path: Path,
    embeddings_path: Path,
    ids_path: Path,
) -> str:
    """Identity of the current coherent canonical pair without mapping it."""
    raw = meta_path.read_bytes()
    record = json.loads(raw)
    if isinstance(record, dict) and record.get("version") == 2:
        return str(_segmented_embedding_state(meta_path)["identity"])
    commit, artifacts, ids_bytes, _ = _stable_embedding_artifacts(
        meta_path, embeddings_path, ids_path)
    return _embedding_bundle_identity(commit, artifacts, ids_bytes)


def embedding_artifact_state(
    meta_path: Path,
    embeddings_path: Path,
    ids_path: Path,
) -> dict:
    """Validated identity and commit for one stable canonical generation."""
    raw = meta_path.read_bytes()
    record = json.loads(raw)
    if isinstance(record, dict) and record.get("version") == 2:
        return _segmented_embedding_state(meta_path)
    commit, artifacts, ids_bytes, _ = _stable_embedding_artifacts(
        meta_path, embeddings_path, ids_path)
    return {
        "identity": _embedding_bundle_identity(commit, artifacts, ids_bytes),
        "commit": commit,
        "artifacts": artifacts,
    }


def committed_embedding_artifact_state(
    meta_path: Path,
    embeddings_path: Path,
    ids_path: Path,
    attempts: int = 4,
) -> dict:
    """Validate a publish-last generation from metadata and file identities.

    The writer hashes IDs and source hashes before atomically publishing the commit.
    Query workers can therefore validate that immutable commit with bounded stats;
    re-reading multi-gigabyte sidecars on every request adds no race protection.
    Legacy bundles still take the exhaustive compatibility path.
    """
    attempts = max(1, int(attempts))
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            meta_raw = meta_path.read_bytes()
            record = json.loads(meta_raw)
            if isinstance(record, dict) and record.get("version") == 2:
                return _segmented_embedding_state(meta_path, attempts=attempts)
            commit = _embedding_commit_from_meta(meta_raw, meta_path)
            if commit is None:
                return embedding_artifact_state(
                    meta_path, embeddings_path, ids_path)
            if not isinstance(record, dict):
                raise ValueError("committed embedding metadata is not an object")
            dim = int(record.get("dim", 0))
            rows = int(commit.get("rows", -1))
            generation = str(commit.get("generation") or "")
            if (dim <= 0 or rows <= 0 or len(generation) != 32
                    or any(char not in "0123456789abcdef" for char in generation)):
                raise ValueError("embedding commit has invalid dimensions or generation")

            hashes_path = embeddings_path.with_suffix(".hashes")
            expected_hashes = commit.get("hashes")
            named_paths = {
                "matrix": embeddings_path, "ids": ids_path, "meta": meta_path,
            }
            if expected_hashes is not None:
                named_paths["hashes"] = hashes_path
            before = {key: _embedding_file_identity(path)
                      for key, path in named_paths.items()}
            after = {key: _embedding_file_identity(path)
                     for key, path in named_paths.items()}
            if before != after or meta_raw != meta_path.read_bytes():
                raise RuntimeError("embedding publication changed during identity read")

            for key in ("matrix", "ids", "hashes"):
                expected = commit.get(key)
                if key == "hashes" and expected is None:
                    continue
                if (not isinstance(expected, dict) or key not in after
                        or not _embedding_identity_records_match(expected, after[key])):
                    raise ValueError(f"embedding {key} is not the committed generation")
                if key != "matrix":
                    digest = str(expected.get("sha256") or "")
                    if (len(digest) != 64
                            or any(char not in "0123456789abcdef" for char in digest)):
                        raise ValueError(f"embedding {key} digest is invalid")
            if after["matrix"]["size"] != rows * dim * 4:
                raise ValueError(
                    "embedding matrix size does not match committed rows and dimension")
            return {
                "identity": _embedding_bundle_identity(commit, after, b""),
                "commit": commit,
                "artifacts": after,
            }
        except (OSError, RuntimeError, ValueError, TypeError,
                json.JSONDecodeError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.01 * (attempt + 1))
                continue
            raise ValueError(
                f"could not read committed embedding identity: {exc}") from exc
    assert last_error is not None
    raise last_error


def embedding_matrix_identity(matrix) -> str | None:
    """Identity captured when ``matrix`` was mapped; treat it as opaque."""
    value = getattr(matrix, "_agrep_bundle_identity", None)
    return str(value) if value else None


def embedding_commit_identity(
    meta_path: Path,
    embeddings_path: Path | None = None,
    ids_path: Path | None = None,
) -> str | None:
    """Stable generation id for sidecars that must align with one vector pair."""
    commit = read_embedding_commit(meta_path, embeddings_path, ids_path)
    return str(commit["generation"]) if commit is not None else None


def write_embedding_commit(
    meta_path: Path,
    dim: int,
    model_id: str | None,
    embeddings_path: Path,
    ids_path: Path,
    rows: int,
    hashes_path: Path | None = None,
    before_publish: Callable[[], object] | None = None,
) -> str:
    """Atomically commit an already-published matrix/ids pair.

    Writers replace the two data files first and call this last. Readers can then
    distinguish a complete generation from the same-row-count mixed-pair window
    that row-count validation alone cannot detect.
    """
    targets = [meta_path, embeddings_path, ids_path]
    if hashes_path is not None:
        targets.append(hashes_path)
    _require_publication_target("publish an embedding commit", *targets)
    import hashlib
    import uuid

    ids_bytes = ids_path.read_bytes()
    commit = {
        "version": _EMBEDDING_COMMIT_VERSION,
        "generation": uuid.uuid4().hex,
        "rows": int(rows),
        "matrix": _embedding_file_identity(embeddings_path),
        "ids": {
            **_embedding_file_identity(ids_path),
            "sha256": hashlib.sha256(ids_bytes).hexdigest(),
        },
    }
    if hashes_path is not None:
        hashes_bytes = hashes_path.read_bytes()
        commit["hashes"] = {
            **_embedding_file_identity(hashes_path),
            "sha256": hashlib.sha256(hashes_bytes).hexdigest(),
        }
    payload = {
        "dim": int(dim),
        "model": model_id or "",
        "commit": commit,
    }
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    _prune_embedding_temps(meta_path)
    tmp = embedding_temp_path(meta_path, "commit")
    try:
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        replace_with_retry(tmp, meta_path, before_attempt=before_publish)
    finally:
        tmp.unlink(missing_ok=True)
    return payload["commit"]["generation"]


_EMBEDDING_PUBLISH_RECORD_BYTES = 4096
_EMBEDDING_PUBLISH_MALFORMED_STALE_S = 120.0
# Legacy creation requested 0777 and only falsey births were unverifiable.
_EMBEDDING_PUBLISH_MODE = 0o777
_EMBEDDING_PUBLISH_UNVERIFIABLE_STARTS = frozenset(("",))
_EMBEDDING_PUBLISH_RELEASE_DELAYS = (0.025, 0.05, 0.1, 0.2)


class EmbeddingPublishLock:
    """Exact cross-process ownership for one embedding publication namespace.

    This is deliberately separate from ``IndexLock``: the indexer can hold that
    ingest lock while an embedding child publishes. Callers choose the critical
    section and must verify ownership immediately before canonical mutations.
    """

    def __init__(self, embeddings_path: Path, timeout: float = 120.0):
        import uuid
        self.path = embeddings_path.with_name(f".{embeddings_path.name}.publish.lock")
        self.timeout = max(0.0, float(timeout))
        self.token = uuid.uuid4().hex
        self.owned = False
        self.handle: ownerfile.Handle | None = None

    def _record(self) -> bytes:
        return json.dumps({
            "pid": os.getpid(),
            "token": self.token,
            "process_start": process_start_identity(os.getpid()),
            "created_at": time.time(),
        }).encode("utf-8")

    @staticmethod
    def _reclaimable(observed: ownerfile.Snapshot) -> bool:
        try:
            record = json.loads(observed.raw.decode("utf-8"))
            if not isinstance(record, dict):
                raise TypeError("publication owner record is not an object")
            holder = int(record.get("pid") or 0)
            state = ownerfile.classify_process(
                holder, record.get("process_start"),
                pid_alive=pid_alive,
                process_start=lambda pid: process_start_identity(pid) or None,
                unverifiable_starts=_EMBEDDING_PUBLISH_UNVERIFIABLE_STARTS)
        except (OSError, OverflowError, RecursionError, TypeError, UnicodeError,
                ValueError, json.JSONDecodeError):
            age = time.time() - observed.mtime
            return not 0.0 <= age <= _EMBEDDING_PUBLISH_MALFORMED_STALE_S
        return state in (ownerfile.ProcessOwner.DEAD, ownerfile.ProcessOwner.REUSED)

    def __enter__(self):
        _require_publication_target(
            "acquire an embedding publication lock", self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        delay = 0.01
        retry_after_progress = False
        while True:
            try:
                self.handle = ownerfile.create_exclusive(
                    self.path, self._record(),
                    mode=_EMBEDDING_PUBLISH_MODE, retain_fd=True)
                self.owned = True
                return self
            except FileExistsError:
                if retry_after_progress and time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for {self.path}")
                retry_after_progress = False
                try:
                    observed = ownerfile.snapshot(
                        self.path, max_bytes=_EMBEDDING_PUBLISH_RECORD_BYTES)
                except OSError:
                    try:
                        self.path.lstat()
                    except FileNotFoundError:
                        retry_after_progress = True
                        continue
                    except OSError:
                        pass
                else:
                    if (self._reclaimable(observed)
                            and ownerfile.remove_exact(
                                self.path, observed, tombstone=True,
                                require_stable_mtime=True)):
                        retry_after_progress = True
                        continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for {self.path}")
                time.sleep(delay)
                delay = min(0.25, delay * 1.5)

    def verify(self) -> ownerfile.Snapshot:
        if not self.owned or self.handle is None:
            raise ownerfile.OwnershipLost(
                f"embedding publication ownership is not held: {self.path}")
        return self.handle.verify(require_stable_mtime=True)

    def __exit__(self, exc_type, exc, tb) -> None:
        handle = self.handle
        self.handle = None
        self.owned = False
        if handle is None:
            return
        expected = handle.snapshot
        try:
            removed = handle.release(
                tombstone=True, require_stable_mtime=True)
        except OSError:
            try:
                handle.close()
            except OSError:
                pass
            removed = False
        if removed:
            return
        for delay in _EMBEDDING_PUBLISH_RELEASE_DELAYS:
            time.sleep(delay)
            try:
                if ownerfile.remove_exact(
                        self.path, expected, tombstone=True,
                        require_stable_mtime=True):
                    return
            except OSError:
                pass


class EmbeddingPublicationGuard:
    """Serialize legacy and segmented writers that share one metadata path."""

    def __init__(
            self, meta_path: Path, embeddings_path: Path | None = None,
            timeout: float = 120.0):
        self.meta_path = Path(meta_path)
        self.embeddings_path = Path(
            embeddings_path or self.meta_path.with_name("embeddings.f32"))
        self.timeout = max(0.0, float(timeout))
        self.locks: list[EmbeddingPublishLock] = []
        self._thread_locks: list[threading.RLock] = []
        self._entered = False

    def _targets(self) -> tuple[Path, ...]:
        targets = []
        seen = set()
        for path in (self.embeddings_path, self.meta_path):
            key = os.path.normcase(str(path.resolve()))
            if key not in seen:
                seen.add(key)
                targets.append(path)
        return tuple(targets)

    def __enter__(self):
        _require_publication_target(
            "acquire embedding publication locks", *self._targets())
        started = time.monotonic()
        targets = self._targets()
        try:
            for target in targets:
                thread_lock = embedding_thread_publish_lock(target)
                remaining = max(
                    0.0, self.timeout - (time.monotonic() - started))
                if not thread_lock.acquire(timeout=remaining):
                    raise TimeoutError(
                        f"timed out waiting for embedding publication: {target}")
                self._thread_locks.append(thread_lock)
            for target in targets:
                elapsed = time.monotonic() - started
                lock = EmbeddingPublishLock(
                    target, timeout=max(0.0, self.timeout - elapsed))
                lock.__enter__()
                self.locks.append(lock)
            self._entered = True
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def verify(self) -> None:
        if not self._entered or len(self.locks) != len(self._targets()):
            raise ownerfile.OwnershipLost(
                f"embedding publication guard is not held: {self.meta_path}")
        for lock in self.locks:
            lock.verify()

    def __exit__(self, exc_type, exc, tb) -> None:
        self._entered = False
        while self.locks:
            self.locks.pop().__exit__(exc_type, exc, tb)
        while self._thread_locks:
            self._thread_locks.pop().release()


_EMBEDDING_THREAD_LOCKS: dict[str, threading.RLock] = {}
_EMBEDDING_THREAD_LOCKS_GUARD = threading.Lock()


def embedding_thread_publish_lock(embeddings_path: Path) -> threading.RLock:
    """Per-process half of publication serialization, keyed by canonical path."""
    key = os.path.normcase(str(embeddings_path.resolve()))
    with _EMBEDDING_THREAD_LOCKS_GUARD:
        lock = _EMBEDDING_THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _EMBEDDING_THREAD_LOCKS[key] = lock
        return lock


def _legacy_embedding_commit_inputs(
        meta_path: Path, embeddings_path: Path,
        ids_path: Path) -> tuple[int, str | None, int, Path | None] | str:
    state = embedding_artifact_state(meta_path, embeddings_path, ids_path)
    commit = state["commit"]
    if commit is not None:
        return str(commit["generation"])

    dim, model_id = read_index_meta(meta_path)
    ids = ids_path.read_bytes().decode("utf-8").splitlines()
    if len(set(ids)) != len(ids) or any(not mid for mid in ids):
        raise ValueError("legacy embedding ids must be unique and non-empty")
    expected_size = len(ids) * int(dim) * 4
    actual_size = embeddings_path.stat().st_size
    if dim <= 0 or actual_size != expected_size:
        raise ValueError(
            f"legacy matrix size mismatch: {actual_size} bytes vs {expected_size}")
    hashes_path = embeddings_path.with_suffix(".hashes")
    committed_hashes = hashes_path if hashes_path.exists() else None
    if committed_hashes is not None:
        hash_rows = len(
            committed_hashes.read_bytes().decode("utf-8").splitlines())
        if hash_rows != len(ids):
            raise ValueError(
                f"legacy row/hash count mismatch: {len(ids)} vs {hash_rows}")
    return dim, model_id, len(ids), committed_hashes


def _write_embedding_publication_barrier(
        meta_path: Path, dim: int, model_id: str | None,
        before_publish: Callable[[], object]) -> None:
    _require_publication_target(
        "publish an embedding replacement barrier", meta_path)
    import uuid

    payload = {
        "dim": int(dim),
        "model": model_id or "",
        "state": "publishing",
        "commit": {
            "version": _EMBEDDING_COMMIT_VERSION,
            "generation": uuid.uuid4().hex,
            "rows": -1,
            "matrix": {},
            "ids": {},
        },
    }
    tmp = embedding_temp_path(meta_path, "barrier")
    try:
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        replace_with_retry(
            tmp, meta_path, before_attempt=before_publish)
    finally:
        tmp.unlink(missing_ok=True)


def ensure_embedding_commit(
    embeddings_path: Path = EMBEDDINGS_PATH,
    ids_path: Path = IDS_PATH,
    meta_path: Path | None = None,
) -> str:
    """Upgrade a stable legacy pair to the strict publish-last contract in place.

    This is used by an incremental embed run with zero changed rows. It validates
    the complete legacy bundle while holding both publication locks, then replaces
    only metadata; the matrix is neither copied nor rewritten.
    """
    meta_path = meta_path or embeddings_path.parent / "embeddings.meta"
    _require_publication_target(
        "upgrade an embedding commit", embeddings_path, ids_path, meta_path)
    with embedding_thread_publish_lock(embeddings_path):
        with EmbeddingPublicationGuard(
                meta_path, embeddings_path) as publication:
            prepared = _legacy_embedding_commit_inputs(
                meta_path, embeddings_path, ids_path)
            if isinstance(prepared, str):
                return prepared
            dim, model_id, rows, committed_hashes = prepared
            return write_embedding_commit(
                meta_path, dim, model_id, embeddings_path, ids_path, rows,
                hashes_path=committed_hashes,
                before_publish=publication.verify)


def embedding_temp_path(path: Path, label: str) -> Path:
    import uuid
    start = process_start_identity(os.getpid()) or "unknown"
    label = str(label).replace("-", "_")
    return path.with_name(
        f".{path.name}.{label}-{os.getpid()}-{start}-{uuid.uuid4().hex}.tmp")


def _prune_embedding_temps(path: Path) -> None:
    """Scavenge unique publication temps whose exact process owner is gone."""
    if _publication_target_protected(path):
        return
    prefix = f".{path.name}."
    try:
        candidates = list(path.parent.glob(prefix + "*.tmp"))
    except OSError:
        return
    for candidate in candidates:
        body = candidate.name[len(prefix):-4]
        parts = body.split("-", 3)
        if len(parts) < 4:
            # Legacy shared temp name: only age can distinguish an abandoned file.
            try:
                if time.time() - candidate.stat().st_mtime <= 120:
                    continue
            except OSError:
                continue
            owner, expected_start = 0, None
        else:
            try:
                owner = int(parts[1])
            except ValueError:
                owner = 0
            expected_start = (parts[2] if parts[2].startswith(
                ("win_", "proc_", "darwin_")) else None)
        if owner > 0 and pid_alive(owner):
            if expected_start is None or process_start_identity(owner) == expected_start:
                continue
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            pass


def write_embeddings(
    ids: Sequence[str],
    embeddings: np.ndarray,
    embeddings_path: Path = EMBEDDINGS_PATH,
    ids_path: Path = IDS_PATH,
    dim: int = EMBED_DIM,
    model_id: str | None = None,
    text_hashes: Sequence[str] | None = None,
) -> None:
    """Publish one aligned matrix/ids/hash generation with a commit marker.

    `embeddings` must be (N, dim) and is assumed already truncated+normalized
    (use matryoshka_truncate first). We assert the shape and dtype rather than
    silently coercing, so a contract mismatch fails loudly here instead of in
    the Rust reader.
    """
    _require_publication_target(
        "publish embeddings", embeddings_path, ids_path)
    import numpy as np  # lazy: embedding-contract helpers only

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
    if len(set(ids)) != len(ids):
        raise ValueError("embedding ids must be unique; duplicate ordinals cannot share metadata")
    if any(not isinstance(mid, str) or not mid or "\n" in mid or "\r" in mid for mid in ids):
        raise ValueError("embedding ids must be non-empty single-line strings")
    if text_hashes is not None and len(text_hashes) != len(ids):
        raise ValueError(
            f"hashes/embeddings row mismatch: {len(text_hashes)} hashes vs {len(ids)} rows")
    if text_hashes is not None and any(
            not isinstance(value, str) or not value or "\n" in value or "\r" in value
            for value in text_hashes):
        raise ValueError("embedding text hashes must be non-empty single-line strings")

    # File locks serialize processes, but Windows scanners can hold rapid sibling
    # temp/meta opens long enough to exhaust replace retries when many threads in
    # one process prepare concurrently. Serialize preparation in-process as well.
    with embedding_thread_publish_lock(embeddings_path):
        _write_embedding_files(
            ids, [embeddings], embeddings_path, ids_path, dim, model_id, text_hashes)


def write_embeddings_parts(
    ids: Sequence[str],
    parts: Sequence[np.ndarray],
    embeddings_path: Path = EMBEDDINGS_PATH,
    ids_path: Path = IDS_PATH,
    dim: int = EMBED_DIM,
    model_id: str | None = None,
    text_hashes: Sequence[str] | None = None,
) -> None:
    """Publish aligned contiguous matrix parts without materializing ``vstack``.

    Incremental semantic refresh can retain hundreds of MB of old vectors and add
    only a small fresh tranche. Streaming both parts to the temp generation avoids
    an equally large combined array and ``tobytes()`` copy before publication.
    """
    _require_publication_target(
        "publish embedding parts", embeddings_path, ids_path)
    import numpy as np  # lazy: embedding-contract helpers only

    normalized = []
    rows = 0
    for part in parts:
        array = np.ascontiguousarray(part, dtype="<f4")
        if array.ndim != 2 or array.shape[1] != dim:
            raise ValueError(f"embedding part must be (N, {dim}); got {array.shape}")
        normalized.append(array)
        rows += int(array.shape[0])
    if len(ids) != rows:
        raise ValueError(f"ids/embeddings row mismatch: {len(ids)} ids vs {rows} rows")
    if len(set(ids)) != len(ids):
        raise ValueError("embedding ids must be unique; duplicate ordinals cannot share metadata")
    if any(not isinstance(mid, str) or not mid or "\n" in mid or "\r" in mid for mid in ids):
        raise ValueError("embedding ids must be non-empty single-line strings")
    if text_hashes is not None and len(text_hashes) != len(ids):
        raise ValueError(
            f"hashes/embeddings row mismatch: {len(text_hashes)} hashes vs {len(ids)} rows")
    if text_hashes is not None and any(
            not isinstance(value, str) or not value or "\n" in value or "\r" in value
            for value in text_hashes):
        raise ValueError("embedding text hashes must be non-empty single-line strings")
    with embedding_thread_publish_lock(embeddings_path):
        _write_embedding_files(
            ids, normalized, embeddings_path, ids_path, dim, model_id, text_hashes)


def _write_embedding_files(
    ids: Sequence[str],
    embedding_parts,
    embeddings_path: Path,
    ids_path: Path,
    dim: int,
    model_id: str | None,
    text_hashes: Sequence[str] | None,
) -> None:
    _require_publication_target(
        "publish embedding files", embeddings_path, ids_path)
    embeddings_path.parent.mkdir(parents=True, exist_ok=True)
    hashes_path = embeddings_path.with_suffix(".hashes")
    meta_path = embeddings_path.parent / "embeddings.meta"
    for artifact in (embeddings_path, ids_path, hashes_path):
        _prune_embedding_temps(artifact)
    tmp_emb = embedding_temp_path(embeddings_path, "matrix")
    tmp_ids = embedding_temp_path(ids_path, "ids")
    tmp_hashes = (embedding_temp_path(hashes_path, "hashes")
                  if text_hashes is not None else None)

    try:
        with tmp_emb.open("wb") as stream:
            for part in embedding_parts:
                part.tofile(stream)
        with tmp_ids.open("w", encoding="utf-8", newline="\n") as stream:
            for mid in ids:
                stream.write(mid)
                stream.write("\n")
        if tmp_hashes is not None:
            # The compact ordinal reader expects a fixed 17-byte (16 hex + LF) layout, so pin
            # LF: the platform text newline would publish CRLF hashes on Windows.
            with tmp_hashes.open("w", encoding="utf-8", newline="\n") as stream:
                for value in text_hashes:
                    stream.write(str(value))
                    stream.write("\n")

        with EmbeddingPublicationGuard(
                meta_path, embeddings_path) as publication:
            publication.verify()
            try:
                current_meta = json.loads(meta_path.read_bytes())
            except (FileNotFoundError, OverflowError, RecursionError, TypeError,
                    UnicodeError, ValueError):
                current_meta = None
            if isinstance(current_meta, dict) and current_meta.get("version") == 2:
                raise RuntimeError(
                    "refusing to replace a segmented embedding index with flat artifacts")
            try:
                prepared = _legacy_embedding_commit_inputs(
                    meta_path, embeddings_path, ids_path)
            except (OSError, RuntimeError, TypeError, UnicodeError, ValueError,
                    json.JSONDecodeError):
                _write_embedding_publication_barrier(
                    meta_path, dim, model_id, publication.verify)
            else:
                if not isinstance(prepared, str):
                    old_dim, old_model, old_rows, old_hashes = prepared
                    write_embedding_commit(
                        meta_path, old_dim, old_model, embeddings_path,
                        ids_path, old_rows, hashes_path=old_hashes,
                        before_publish=publication.verify)
            publication.verify()
            _prune_embedding_snapshots(embeddings_path)
            replace_with_retry(
                tmp_emb, embeddings_path, before_attempt=publication.verify)
            replace_with_retry(
                tmp_ids, ids_path, before_attempt=publication.verify)
            if tmp_hashes is not None:
                replace_with_retry(
                    tmp_hashes, hashes_path, before_attempt=publication.verify)
                committed_hashes = hashes_path
            else:
                publication.verify()
                hashes_path.unlink(missing_ok=True)
                committed_hashes = None
            write_embedding_commit(
                meta_path, dim, model_id, embeddings_path, ids_path, len(ids),
                hashes_path=committed_hashes,
                before_publish=publication.verify)
    finally:
        for tmp in (tmp_emb, tmp_ids, tmp_hashes):
            if tmp is not None:
                tmp.unlink(missing_ok=True)


def _embedding_snapshot_name(path: Path) -> Path:
    import uuid
    start = process_start_identity(os.getpid()) or "unknown"
    return path.with_name(
        f".{path.name}.agrep-mmap-{os.getpid()}-{start}-{uuid.uuid4().hex}")


def _copy_to_system_temp(path: Path, label: str) -> Path:
    """Copy one stable canonical artifact outside its publication directory."""
    import shutil

    temp_root = Path(tempfile.gettempdir())
    if _publication_target_protected(temp_root):
        raise OSError(
            "system temporary directory is protected by AGREP_DATA_READONLY")
    fd, raw = tempfile.mkstemp(
        prefix=f"agrep-{label}-{os.getpid()}-", suffix=path.suffix,
        dir=temp_root)
    os.close(fd)
    snapshot = Path(raw)
    try:
        before = _embedding_file_identity(path)
        shutil.copyfile(path, snapshot)
        after = _embedding_file_identity(path)
        if (before != after
                or snapshot.stat().st_size != int(before["size"])):
            raise RuntimeError(
                f"{path} changed while copying a read-only snapshot")
        return snapshot
    except BaseException:
        try:
            snapshot.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _remove_embedding_snapshot(path: Path, *, external: bool) -> None:
    if not external and _publication_target_protected(path):
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _prune_embedding_snapshots(path: Path) -> None:
    """Remove crash leftovers without racing a live mapping owner."""
    if _publication_target_protected(path):
        return
    prefix = f".{path.name}.agrep-mmap-"
    try:
        candidates = list(path.parent.glob(prefix + "*"))
    except OSError:
        return
    for candidate in candidates:
        tail = candidate.name[len(prefix):]
        parts = tail.split("-", 2)
        try:
            owner = int(parts[0])
        except (ValueError, IndexError):
            owner = 0
        expected_start = (parts[1] if len(parts) >= 3
                          and parts[1].startswith(
                              ("win_", "proc_", "darwin_")) else None)
        if owner > 0 and pid_alive(owner) and expected_start is not None:
            if process_start_identity(owner) == expected_start:
                continue
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            # A mapped Windows file is authoritative proof that an owner remains.
            pass


def close_embedding_matrix(matrix) -> None:
    """Close one matrix returned by ``read_embeddings`` and remove its alias."""
    mapping = getattr(matrix, "_mmap", None)
    if mapping is not None:
        try:
            mapping.close()
        except (OSError, ValueError):
            pass
    alias = getattr(matrix, "_agrep_snapshot_path", None)
    if alias:
        _remove_embedding_snapshot(
            Path(alias),
            external=bool(getattr(
                matrix, "_agrep_snapshot_external", False)),
        )
        try:
            matrix._agrep_snapshot_path = None
            matrix._agrep_snapshot_external = False
        except Exception:  # ndarray-like compatibility
            pass


def _map_embedding_snapshot(
        embeddings_path: Path, dim: int, *,
        publication_read_only: bool = False):
    """Map an immutable view without pinning the Windows publication path."""
    import numpy as np  # lazy: embedding-contract helpers only

    alias = None
    target = embeddings_path
    hardlinked = False
    external = False
    if WIN:
        if (publication_read_only
                or _publication_target_protected(embeddings_path)):
            alias = _copy_to_system_temp(embeddings_path, "embedding")
            external = True
        else:
            import shutil
            _prune_embedding_snapshots(embeddings_path)
            alias = _embedding_snapshot_name(embeddings_path)
            try:
                os.link(embeddings_path, alias)
                hardlinked = True
            except OSError:
                # hardlinks can be unavailable (network/FAT);
                # a snapshot copy costs disk but never pins the canonical path.
                shutil.copyfile(embeddings_path, alias)
        target = alias
    try:
        size = target.stat().st_size
        row_bytes = int(dim) * 4
        if dim <= 0 or size % row_bytes:
            raise ValueError(
                f"{embeddings_path} has {size // 4} floats, not a multiple of dim={dim}")
        rows = size // row_bytes
        if not rows:
            empty = np.empty((0, dim), dtype="<f4")
            empty.flags.writeable = False
            if alias is not None:
                _remove_embedding_snapshot(alias, external=external)
            return empty
        matrix = np.memmap(target, dtype="<f4", mode="r", shape=(rows, dim), order="C")
        matrix._agrep_snapshot_path = str(alias) if alias is not None else None
        matrix._agrep_snapshot_hardlink = hardlinked
        matrix._agrep_snapshot_external = external
        return matrix
    except Exception:
        if alias is not None:
            _remove_embedding_snapshot(alias, external=external)
        raise


def read_embeddings(
    embeddings_path: Path = EMBEDDINGS_PATH,
    ids_path: Path = IDS_PATH,
    dim: int = EMBED_DIM,
    meta_path: Path | None = None,
    attempts: int = 4,
    *,
    publication_read_only: bool = False,
) -> tuple[list[str], np.ndarray]:
    """Read a coherent (ids, read-only mmap) pair without private matrix RAM.

    On Windows, mapping the canonical file prevents ``os.replace``. We map a
    same-volume hardlink snapshot instead, so an old reader can finish while a
    writer publishes the next canonical generation. The publish-last commit is
    strict when present; legacy pairs retain stable-stat + row-count validation.
    """
    meta_path = meta_path or embeddings_path.parent / "embeddings.meta"
    attempts = max(1, int(attempts))
    last_error: Exception | None = None
    for attempt in range(attempts):
        matrix = None
        try:
            meta_raw = meta_path.read_bytes()
            meta_record = json.loads(meta_raw)
            declared_dim = int(meta_record["dim"] if isinstance(meta_record, dict)
                               else meta_record)
            if declared_dim != dim:
                raise ValueError(
                    f"embedding dim mismatch: requested {dim}, {meta_path} declares {declared_dim}")
            commit = _embedding_commit_from_meta(meta_raw, meta_path)
            hashes_path = embeddings_path.with_suffix(".hashes")
            include_hashes = bool(commit and commit.get("hashes")) or hashes_path.exists()
            named_paths = {
                "matrix": embeddings_path, "ids": ids_path, "meta": meta_path,
            }
            if include_hashes:
                named_paths["hashes"] = hashes_path
            before = {key: _embedding_file_identity(path)
                      for key, path in named_paths.items()}
            matrix = _map_embedding_snapshot(
                embeddings_path, dim,
                publication_read_only=publication_read_only)
            ids_bytes = ids_path.read_bytes()
            hashes_bytes = hashes_path.read_bytes() if include_hashes else None
            after = {key: _embedding_file_identity(path)
                     for key, path in named_paths.items()}
            if before != after or meta_raw != meta_path.read_bytes():
                raise RuntimeError("embedding publication changed during read")
            alias = getattr(matrix, "_agrep_snapshot_path", None)
            if (alias is not None and getattr(matrix, "_agrep_snapshot_hardlink", False)
                    and not _embedding_identity_matches(after["matrix"], Path(alias))):
                raise RuntimeError("embedding snapshot does not match canonical generation")

            if commit is not None:
                _validate_embedding_commit(commit, after, ids_bytes, hashes_bytes)
            ids = ids_bytes.decode("utf-8").splitlines()
            if matrix.shape[0] != len(ids):
                raise ValueError(
                    f"row/id count mismatch: {matrix.shape[0]} rows vs {len(ids)} ids")
            if commit is not None and int(commit.get("rows", -1)) != len(ids):
                raise ValueError(
                    f"committed row/id count mismatch: {commit.get('rows')} vs {len(ids)}")
            if hasattr(matrix, "__dict__"):
                matrix._agrep_bundle_identity = _embedding_bundle_identity(
                    commit, after, ids_bytes)
                if commit is not None:
                    matrix._agrep_commit_generation = str(commit["generation"])
            return ids, matrix
        except (OSError, RuntimeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            last_error = exc
            if matrix is not None:
                close_embedding_matrix(matrix)
            if attempt + 1 < attempts:
                time.sleep(0.01 * (attempt + 1))
                continue
            raise ValueError(f"could not read coherent embedding pair: {exc}") from exc
    assert last_error is not None
    raise last_error
