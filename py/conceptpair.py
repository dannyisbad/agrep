"""Generation-coherent publication for concepts + session concept assignments.

The pair is stored in immutable generation files. One atomically replaced manifest
selects the active generation and pins both hashes, so readers never need to accept
the mixed interval inherent in replacing two fixed filenames independently.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path

MANIFEST_NAME = "concept_pair.manifest.json"
PAIR_DIR_NAME = "concept_pairs"
VERSION = 1


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


class IncoherentConceptPair(RuntimeError):
    pass


def manifest_path(data_dir: Path) -> Path:
    return Path(data_dir) / MANIFEST_NAME


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f"{path.name}.{os.getpid()}.{time.time_ns()}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        deadline = time.monotonic() + 2.0
        while True:
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
    finally:
        tmp.unlink(missing_ok=True)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _concept_bytes(concepts: list[dict]) -> bytes:
    return (json.dumps(concepts, indent=1, ensure_ascii=False) + "\n").encode("utf-8")


def _session_bytes(sessions: list[dict]) -> bytes:
    return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in sessions).encode(
        "utf-8")


def publish(data_dir: Path, concepts: list[dict], sessions: list[dict]) -> dict:
    """Publish a complete pair and return its manifest record."""
    data_dir = Path(data_dir)
    if _readonly_contains(data_dir):
        raise PermissionError(
            "AGREP_DATA_READONLY protects the data directory; "
            "cannot publish concept pairs")
    pair_dir = data_dir / PAIR_DIR_NAME
    generation = f"{time.time_ns()}-{os.getpid()}-{uuid.uuid4().hex}"
    concepts_name = f"{generation}.concepts.json"
    sessions_name = f"{generation}.session_concepts.jsonl"
    concepts_payload = _concept_bytes(concepts)
    sessions_payload = _session_bytes(sessions)

    concepts_path = pair_dir / concepts_name
    sessions_path = pair_dir / sessions_name
    _atomic_bytes(concepts_path, concepts_payload)
    _atomic_bytes(sessions_path, sessions_payload)
    manifest = {
        "version": VERSION,
        "generation": generation,
        "concepts": f"{PAIR_DIR_NAME}/{concepts_name}",
        "session_concepts": f"{PAIR_DIR_NAME}/{sessions_name}",
        "concepts_sha256": _sha(concepts_payload),
        "session_concepts_sha256": _sha(sessions_payload),
        "published_at": time.time(),
    }
    _atomic_bytes(manifest_path(data_dir),
                  (json.dumps(manifest, indent=1) + "\n").encode("utf-8"))

    # Compatibility mirrors for older external consumers. Contract-aware readers
    # always follow the manifest and therefore ignore this two-replace interval.
    _atomic_bytes(data_dir / "concepts.json", concepts_payload)
    _atomic_bytes(data_dir / "session_concepts.jsonl", sessions_payload)
    _cleanup_generations(pair_dir, keep=4,
                         protected={concepts_name, sessions_name})
    return manifest


def _manifest_target(data_dir: Path, relative: object, suffix: str) -> Path:
    if not isinstance(relative, str):
        raise IncoherentConceptPair("concept-pair manifest target is not a string")
    rel = Path(relative)
    if rel.is_absolute() or rel.parent != Path(PAIR_DIR_NAME) or not rel.name.endswith(suffix):
        raise IncoherentConceptPair(f"invalid concept-pair manifest target: {relative!r}")
    return data_dir / rel


def read(data_dir: Path, *, allow_legacy: bool = True, retries: int = 2) \
        -> tuple[list[dict], list[dict], dict | None]:
    """Read one verified generation, or a legacy fixed pair before first publish."""
    data_dir = Path(data_dir)
    mp = manifest_path(data_dir)
    if not mp.exists():
        if not allow_legacy:
            raise IncoherentConceptPair("concept-pair manifest is missing")
        legacy = _read_legacy(data_dir)
        # SOUND ONLY because publish() writes the manifest BEFORE the mirror replaces:
        # a still-absent manifest here proves no replace began, so the legacy pair is
        # coherent; if the manifest appeared meanwhile, follow the pinned generation.
        if not mp.exists():
            return legacy

    last_error = "concept-pair publication changed while reading"
    for _ in range(max(1, retries + 1)):
        try:
            before = mp.read_bytes()
            manifest = json.loads(before)
            if not isinstance(manifest, dict) or manifest.get("version") != VERSION:
                raise IncoherentConceptPair("unsupported concept-pair manifest")
            cp = _manifest_target(data_dir, manifest.get("concepts"), ".concepts.json")
            sp = _manifest_target(
                data_dir, manifest.get("session_concepts"), ".session_concepts.jsonl")
            concepts_payload = cp.read_bytes()
            sessions_payload = sp.read_bytes()
            after = mp.read_bytes()
            if before != after:
                last_error = "concept-pair manifest changed while reading"
                continue
            if _sha(concepts_payload) != manifest.get("concepts_sha256"):
                raise IncoherentConceptPair("concepts generation hash mismatch")
            if _sha(sessions_payload) != manifest.get("session_concepts_sha256"):
                raise IncoherentConceptPair("session concepts generation hash mismatch")
            concepts = json.loads(concepts_payload)
            if not isinstance(concepts, list) or not all(isinstance(row, dict) for row in concepts):
                raise IncoherentConceptPair("concepts generation is not a list of objects")
            sessions = _parse_sessions(sessions_payload)
            return concepts, sessions, manifest
        except IncoherentConceptPair:
            raise
        except (OSError, ValueError, TypeError) as exc:
            last_error = f"cannot read concept-pair generation: {exc}"
            time.sleep(0.01)
    raise IncoherentConceptPair(last_error)


def _parse_sessions(payload: bytes) -> list[dict]:
    rows: list[dict] = []
    for line in payload.decode("utf-8").split("\n"):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise IncoherentConceptPair("session concepts row is not an object")
        rows.append(row)
    return rows


def _read_legacy(data_dir: Path) -> tuple[list[dict], list[dict], None]:
    try:
        concepts = json.loads((data_dir / "concepts.json").read_text(encoding="utf-8"))
        sessions = _parse_sessions((data_dir / "session_concepts.jsonl").read_bytes())
    except (OSError, ValueError, TypeError) as exc:
        raise IncoherentConceptPair(f"cannot read legacy concept pair: {exc}") from exc
    if not isinstance(concepts, list) or not all(isinstance(row, dict) for row in concepts):
        raise IncoherentConceptPair("legacy concepts file is not a list of objects")
    return concepts, sessions, None


def _cleanup_generations(pair_dir: Path, *, keep: int, protected: set[str]) -> None:
    try:
        files = sorted((p for p in pair_dir.iterdir() if p.is_file()),
                       key=lambda p: p.stat().st_mtime_ns, reverse=True)
    except OSError:
        return
    # Keep several complete historical generations so a reader that captured the
    # previous manifest before publication cannot lose its immutable files mid-read.
    generations: dict[str, list[Path]] = {}
    for path in files:
        if path.name.endswith(".concepts.json"):
            generation = path.name.removesuffix(".concepts.json")
        elif path.name.endswith(".session_concepts.jsonl"):
            generation = path.name.removesuffix(".session_concepts.jsonl")
        else:
            continue
        generations.setdefault(generation, []).append(path)
    try:
        ordered = sorted(generations.items(),
                         key=lambda item: max(p.stat().st_mtime_ns for p in item[1]),
                         reverse=True)
    except OSError:
        return
    for _, paths in ordered[keep:]:
        for path in paths:
            if path.name not in protected:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
