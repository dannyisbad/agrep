"""tilt doctor - what's installed, what each tier needs, and how to fix gaps.

The CLI twin of the in-app status chip. tilt works in tiers: a stdlib-only clone
browses, keyword-searches, watches live sessions, and resumes; the smart tier adds
semantic search + topics + mood arcs; the named tier adds generated titles/summaries.
This prints which tiers are live and the exact command to unlock each missing one.

  python tilt.py doctor          # report
  python tilt.py doctor --fix    # do the safe setup steps (venv + pip install)
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import common

ROOT = common.REPO_ROOT
WIN = sys.platform == "win32"
VENV_PY = ROOT / "py" / ".venv" / ("Scripts" if WIN else "bin") / ("python.exe" if WIN else "python")
TILT_RS = ROOT / "target" / "release" / ("tilt-rs.exe" if WIN else "tilt-rs")
HOME = Path.home()

OK, MISS, OPT = "ok", "MISSING", "--"


def _row(name: str, status: str, detail: str = "") -> None:
    glyph = {OK: "ok ", MISS: "MISS", OPT: "-- "}.get(status, "?  ")
    print(f"  [{glyph}] {name:<22} {detail}")


def _venv_has(mod: str) -> bool:
    if not VENV_PY.exists():
        return False
    r = subprocess.run([str(VENV_PY), "-c", f"import {mod}"],
                       capture_output=True)
    return r.returncode == 0


def _ollama_models() -> list[str] | None:
    """Returns the pulled model list, or None if Ollama isn't reachable."""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2) as r:
            return [m["name"] for m in json.loads(r.read()).get("models", [])]
    except Exception:  # noqa: BLE001
        return None


def _stores() -> list[tuple[str, Path, str]]:
    """(agent, root, glob) for each agent store under home."""
    return [
        ("claude", HOME / ".claude" / "projects", "*/*.jsonl"),
        ("codex", HOME / ".codex" / "sessions", "**/rollout-*.jsonl"),
        ("opencode", HOME / ".local" / "share" / "opencode", "*.db"),
        ("antigravity", HOME / ".gemini" / "antigravity-cli" / "brain", "*"),
    ]


def probe() -> dict:
    """Structured tier report for the in-app setup panel (GET /doctor). Same checks
    as `report()`, no printing. The venv module probes spawn a python each, so this
    costs ~1-2s — the UI calls it on demand (setup open / recheck), never on boot."""
    has_cargo = shutil.which("cargo") is not None
    has_bin = TILT_RS.exists()
    has_venv = VENV_PY.exists()
    deps = {m: (_venv_has(m) if has_venv else False)
            for m in ("numpy", "torch", "sentence_transformers", "sklearn")}
    models = _ollama_models()
    stores = []
    for agent, root, glob in _stores():
        n = len(list(root.glob(glob))) if root.exists() else 0
        stores.append({"agent": agent, "found": n})
    sj = common.DATA_DIR / "sessions.jsonl"
    indexed = None
    if sj.exists():
        import time
        indexed = {"sessions": sum(1 for _ in sj.open(encoding="utf-8")),
                   "age_s": int(time.time() - sj.stat().st_mtime)}
    return {
        "core": {"live": has_bin, "rust": has_cargo, "binary": has_bin,
                 "stores": stores, "indexed": indexed},
        "smart": {"live": all(deps.values()), "venv": has_venv, "deps": deps},
        "named": {"live": bool(models), "ollama": models is not None,
                  "models": (models or [])[:5]},
    }


def report() -> dict:
    fixes: list[str] = []

    print("\ncore (required)")
    has_cargo = shutil.which("cargo") is not None
    _row("rust / cargo", OK if has_cargo else MISS,
         "" if has_cargo else "install from https://rustup.rs")
    if not has_cargo:
        fixes.append("install Rust: https://rustup.rs")

    has_bin = TILT_RS.exists()
    _row("ingest binary", OK if has_bin else MISS,
         str(TILT_RS.relative_to(ROOT)) if has_bin else "build it: python tilt.py index")
    if not has_bin and has_cargo:
        fixes.append("build the ingest binary: cargo build --release")

    pyok = sys.version_info >= (3, 10)
    _row("python >= 3.10", OK if pyok else MISS,
         ".".join(map(str, sys.version_info[:3])))

    found_stores = []
    for agent, root, glob in _stores():
        n = len(list(root.glob(glob))) if root.exists() else 0
        if n:
            found_stores.append(agent)
    _row("agent stores", OK if found_stores else MISS,
         ", ".join(found_stores) if found_stores
         else "none found under ~ (run an agent first)")

    print("\nsmart tier - semantic search, topics, mood arcs (optional)")
    has_venv = VENV_PY.exists()
    _row("python venv", OK if has_venv else OPT,
         str(VENV_PY.relative_to(ROOT)) if has_venv else "py/.venv")
    deps = {m: (_venv_has(m) if has_venv else False)
            for m in ("numpy", "torch", "sentence_transformers", "sklearn")}
    for m, present in deps.items():
        _row(m, OK if present else OPT, "" if present else "not installed")
    if not all(deps.values()):
        fixes.append("smart tier: python tilt.py doctor --fix  "
                     "(creates py/.venv, installs requirements.txt)")

    print("\nnamed tier - generated titles & summaries (optional)")
    models = _ollama_models()
    if models is None:
        _row("ollama", OPT, "not running (install: https://ollama.com)")
        fixes.append("named tier: install Ollama, then `ollama pull qwen2.5:3b-instruct`")
    else:
        _row("ollama", OK, "reachable")
        _row("a model pulled", OK if models else OPT,
             ", ".join(models[:3]) if models else "none (e.g. `ollama pull qwen2.5:3b-instruct`)")
        if not models:
            fixes.append("named tier: ollama pull qwen2.5:3b-instruct")

    print("\nindex")
    sj = common.DATA_DIR / "sessions.jsonl"
    if sj.exists():
        import time
        n = sum(1 for _ in sj.open(encoding="utf-8"))
        age = int(time.time() - sj.stat().st_mtime)
        _row("built", OK, f"{n} sessions, updated {age // 60}m ago")
    else:
        _row("built", MISS, "none yet - run: python tilt.py up")
        fixes.append("build the index: python tilt.py up")

    tiers = []
    if has_bin or has_cargo:
        tiers.append("core")
    if all(deps.values()):
        tiers.append("smart")
    if models:
        tiers.append("named")
    print(f"\ntiers available: {', '.join(tiers) or 'none - install Rust first'}")
    if fixes:
        print("\nto unlock more:")
        for f in dict.fromkeys(fixes):  # dedupe, keep order
            print(f"  - {f}")
    print()
    return {"tiers": tiers, "fixes": fixes, "has_cargo": has_cargo,
            "has_venv": has_venv, "deps": deps}


def fix() -> int:
    """Do the safe, automatable setup: create the venv and install requirements.
    Deliberately does NOT touch torch/CUDA specifics or pull Ollama models - those
    are platform-specific and printed as instructions instead."""
    if not VENV_PY.exists():
        print("creating py/.venv ...")
        r = subprocess.run([sys.executable, "-m", "venv", str(ROOT / "py" / ".venv")])
        if r.returncode != 0:
            print("  ! venv creation failed.")
            return 1
    req = ROOT / "requirements.txt"
    if req.exists():
        print("installing requirements.txt (this pulls torch - large) ...")
        pip = VENV_PY.parent / ("pip.exe" if WIN else "pip")
        r = subprocess.run([str(pip), "install", "-r", str(req)])
        if r.returncode != 0:
            print("  ! pip install failed. If it's a torch/CUDA wheel issue, install the "
                  "build for your platform from https://pytorch.org, then re-run.")
            return 1
    print("\ndone. re-run `python tilt.py doctor` to confirm the smart tier is live.")
    print("for the named tier: install Ollama (https://ollama.com) and `ollama pull qwen2.5:3b-instruct`.")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if "--fix" in argv:
        report()
        return fix()
    report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
