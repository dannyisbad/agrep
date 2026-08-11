#!/usr/bin/env python
"""agrep reindex - rebuild the keyword and semantic indexes.

Stages, in order, all generation-aware. ``--full`` bypasses source-parse and
semantic-vector reuse; the derived keyword database still reconciles from the
newly published transcript generation.

    cargo build (release)         -> the agrep-rs ingest binary
    agrep-rs index                -> normalized transcript and event publications
    corpusdb refresh              -> the derived SQLite keyword index
    embed.py                      -> generation-pinned vector segments and manifest

Optional external enrichment can attach summaries and concepts through the post-index hook.

Usage:
    agrep reindex                # incremental rebuild
    agrep reindex --full         # reparse sources and rebuild embeddings
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WIN = sys.platform == "win32"
sys.path.insert(0, str(ROOT / "py"))
import common  # noqa: E402  -- single source for binary and data paths
import dist  # noqa: E402
import indexd_runtime  # noqa: E402
import surface_policy as surface  # noqa: E402

_DIGEST_CHUNK = 1024 * 1024


def _file_signature(path: Path) -> str:
    """Return the existing ``size:md5`` marker without buffering the file."""
    digest = hashlib.md5()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(_DIGEST_CHUNK):
            size += len(chunk)
            digest.update(chunk)
    return f"{size}:{digest.hexdigest()}"


def _input_signature(messages: Path, replies: Path) -> str:
    """Fingerprint both normalized inputs using the established marker format."""
    if not messages.exists():
        return ""
    signature = _file_signature(messages)
    if replies.exists():
        signature += f":{_file_signature(replies)}"
    return signature


def _read_signature(path: Path) -> str | None:
    """The stored completion marker; a signature that cannot be read or
    decoded (external corruption/truncation) is a cache miss, not a crash."""
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None


def run(desc: str, cmd: list[str], optional: bool = False, *,
        env: dict[str, str] | None = None) -> bool:
    print(f"\n=== {desc} ===", flush=True)
    t = time.perf_counter()
    r = subprocess.run(cmd, cwd=str(ROOT), env=env)
    if r.returncode != 0:
        if optional:
            print(f"  ! {desc} failed (exit {r.returncode}); skipping -- keyword search "
                  "works without this stage.", flush=True)
            return False
        print(f"  ! {desc} failed (exit {r.returncode}); stopping.", flush=True)
        sys.exit(r.returncode)
    print(f"  ({time.perf_counter() - t:.1f}s)", flush=True)
    return True


def _source_checkout() -> bool:
    """A wheel contains this script but deliberately not a buildable Cargo tree."""
    return (ROOT / "Cargo.toml").is_file() and (ROOT / "crates").is_dir()


def _embedding_proof() -> dict:
    """Prove vectors are bound to the just-published transcript generation."""
    try:
        import semantic
        return semantic.embedding_coherence()
    except Exception as exc:  # noqa: BLE001 -- a failed proof must never stamp success
        return {"coherent": False, "searchable": False,
                "state": "proof-failed", "reason": type(exc).__name__}


def _path_within(root: Path, target: Path) -> bool:
    try:
        resolved_root = os.path.normcase(os.path.realpath(os.fspath(root)))
        resolved_target = os.path.normcase(
            os.path.realpath(os.fspath(target)))
        return os.path.commonpath((resolved_root, resolved_target)) == resolved_root
    except (OSError, ValueError):
        raise PermissionError(
            f"cannot verify reindex signature target {target}")


def _signature_refusal(path: Path) -> str | None:
    protected = os.environ.get("AGREP_DATA_READONLY")
    if protected and _path_within(Path(protected), path):
        return "AGREP_DATA_READONLY protects the reindex signature target"
    if _path_within(common.DATA_DIR, path):
        ownership = indexd_runtime.derived_writer_mutation_info()
        if not ownership.writable:
            return ownership.reason
    return None


def _require_signature_target(path: Path) -> None:
    refusal = _signature_refusal(path)
    if refusal is not None:
        raise PermissionError(refusal)


def _write_sig(path: Path, signature: str) -> None:
    _require_signature_target(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(signature + "\n", encoding="utf-8")
        common.replace_with_retry(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _verified_signature_matches(path: Path, signature: str) -> bool:
    matches = bool(signature) and _read_signature(path) == signature
    return matches and bool(_embedding_proof().get("coherent"))


def _publish_completion_signature(path: Path, signature: str, proof: dict) -> bool:
    """Only a complete, source-bound semantic generation earns the skip marker."""
    _require_signature_target(path)
    if signature and proof.get("coherent"):
        _write_sig(path, signature)
        return True
    path.unlink(missing_ok=True)
    return False


def _remove_completion_signature(path: Path) -> None:
    _require_signature_target(path)
    path.unlink(missing_ok=True)


def _empty_corpus(path: Path) -> bool:
    try:
        return path.stat().st_size == 0
    except FileNotFoundError:
        return True
    except OSError:
        return False


def main() -> int:
    ap = surface.ArgumentParser(
        prog="agrep reindex",
        description="refresh transcript ingest, keyword search, and semantic embeddings",
        allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  agrep reindex          refresh only changed sources\n"
            "  agrep reindex --full   reparse all sources and rebuild embeddings\n"
            "\nexit: 0 refreshed, 1 a required stage failed, 2 invalid arguments."
        ),
    )
    ap.add_argument("--full", action="store_true",
                    help="reparse every source and rebuild semantic embeddings")
    # Developer/test seam; installed wheels have no Cargo workspace to skip.
    ap.add_argument("--no-build", action="store_true", help=argparse.SUPPRESS)
    # tolerated: an old caller (UI button on a stale server) must not crash the rebuild
    ap.add_argument("--judge", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--max-new", type=int, default=None,
                    help="cap messages embedded this run (background/bounded refresh)")
    args = ap.parse_args()
    if args.max_new is not None and args.max_new < 1:
        ap.error("--max-new must be at least 1")
    if indexd_runtime._data_dir_readonly():
        print(
            "  ! reindex skipped: AGREP_DATA_READONLY protects this data directory.",
            flush=True)
        return 1
    full = ["--full"] if args.full else []

    t0 = time.perf_counter()
    if not args.no_build and _source_checkout():
        run("build rust (release)", ["cargo", "build", "--release"])
    elif not common.ingest_bin().exists():
        # Installed wheels use their bundled executable. A source distribution may
        # have neither that binary nor a Cargo checkout, so use the same verified
        # fetch path as first search instead of running Cargo in site-packages.
        fetched = common.fetch_binary()
        if not fetched or not fetched.exists():
            print("  ! no ingest binary available; run `agrep doctor --fix`.", flush=True)
            return 1
    ownership = indexd_runtime.derived_writer_mutation_info(
        allow_legacy_adoption=True)
    if not ownership.writable:
        print(f"  ! reindex skipped: {ownership.reason}.", flush=True)
        return 1
    # --full also bypasses the Rust per-file parse cache (clean re-parse of every store file)
    ingest = common.ingest_bin()
    run(
        "ingest transcripts (rust)",
        [str(ingest), "index", "--agent", "all", *full],
        env=indexd_runtime.rust_writer_env(ingest))

    refreshed = indexd_runtime.refresh_search_index(quiet=False)
    if refreshed is False:
        print("  ! derived search-index refresh failed; stopping.", flush=True)
        return 1

    # Byte-identical ingest output means downstream would reproduce what's on disk; skip unless --full.
    msgs = common.MESSAGES_PATH
    replies = common.DATA_DIR / "replies.jsonl"
    sig_file = common.DATA_DIR / ".reindex.sig"
    # A reply can land without changing messages.jsonl; both inputs bind vectors.
    sig = _input_signature(msgs, replies)
    semantic_disabled = common.setting("embeddings") == "off"
    corpus_empty = _empty_corpus(msgs)
    matching_sig = (
        not args.full
        and sig
        and _read_signature(sig_file) == sig
    )
    if matching_sig and not semantic_disabled and not corpus_empty:
        if _verified_signature_matches(sig_file, sig):
            print(f"\n  no new messages since last index - already up to date. "
                  f"({time.perf_counter() - t0:.0f}s)")
            print("  (pass --full to rebuild embeddings anyway.)")
            return 0
        # A signature without a complete source-bound vector generation is an old
        # sticky marker (typically a failed or --max-new pass). Remove it before
        # doing work so a crash cannot make the next run skip again.
        try:
            if _read_signature(sig_file) == sig:
                _remove_completion_signature(sig_file)
        except PermissionError as exc:
            print(f"  ! reindex completion refused: {exc}.", flush=True)
            return 1
        except OSError:
            pass

    # Semantic embeddings: failure warns and skips - keyword search works from the Rust ingest alone.
    if semantic_disabled:
        print("\n=== semantic embeddings (disabled) ===")
        print("  skipped because embeddings=off; keyword search is ready.")
        embedded = False
        proof = {"coherent": False, "searchable": False, "state": "disabled"}
    elif corpus_empty:
        print("\n=== semantic embeddings (no messages) ===")
        print("  nothing to embed; keyword search is ready.")
        embedded = False
        proof = {"coherent": False, "searchable": False, "state": "empty"}
    else:
        import semantic
    if not semantic_disabled and not corpus_empty \
            and semantic.runtime_dependencies_available():
        embed_cmd = [sys.executable, "py/embed.py", *full]
        if args.max_new is not None:
            embed_cmd += ["--max-new", str(args.max_new)]
        embedded = run("embed messages", embed_cmd, optional=True)
        proof = _embedding_proof() if embedded else {
            "coherent": False, "searchable": False, "state": "embed-failed"}
    elif not semantic_disabled and not corpus_empty:
        print("\n=== semantic embeddings (optional; not installed) ===")
        print("  skipped without launching a worker; keyword search is ready. "
              "Enable on a supported OS/Python with "
              f"{dist.semantic_install_hint()}.")
        embedded = False
        proof = {"coherent": False, "searchable": False,
                 "state": "optional-runtime-unavailable"}
    try:
        completion = _publish_completion_signature(sig_file, sig, proof)
    except PermissionError as exc:
        print(f"  ! reindex completion refused: {exc}.", flush=True)
        return 1
    if not completion and not (semantic_disabled or corpus_empty):
        # Partial generations stay unstamped so the next reindex continues the backlog.
        coverage = proof.get("coverage") or {}
        if proof.get("searchable"):
            print("  ! semantic index is usable but incomplete "
                  f"({coverage.get('indexed', '?')}/{coverage.get('total', '?')}); "
                  "the next reindex will continue it.", flush=True)
        else:
            print("  ! semantic generation was not proven current "
                  f"({proof.get('state', 'unknown')}); no success signature written.",
                  flush=True)

    # This orchestration bypasses indexd_runtime.build_index(), so notify hooks.
    # after transcripts, keyword data, and embeddings are published.
    try:
        import indexer
        indexer.run_post_index_hooks()
    except Exception as exc:  # noqa: BLE001 -- optional enrichment never wounds reindex
        common.log(f"post_index hooks unavailable: {type(exc).__name__}: {exc}")

    print(f"\n  reindex complete in {time.perf_counter() - t0:.0f}s.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
