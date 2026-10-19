#!/usr/bin/env python3
"""Upgrade a synthetic store with a live released daemon; build the candidate ingest binary first."""

from __future__ import annotations

import argparse
import ast
from contextlib import closing
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "bench" / "fixtures" / "upgrade_store"
# Stored without leading dots: the repository privacy gate treats dot-named agent
# store dirs under bench/ as private captures. The sandbox home gets the real names.
FIXTURE_STORES = ("claude", "codex", "pi", "omp")
QUERY = "copper lantern upgrade beacon"


class SmokeFailure(RuntimeError):
    pass


def run(command: list[str], *, env: dict[str, str], cwd: Path,
        timeout: float = 120, accepted: tuple[int, ...] = (0,),
        label: str = "") -> subprocess.CompletedProcess[str]:
    started = time.monotonic()
    result = subprocess.run(
        command, env=env, cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout, check=False)
    if label:
        print(f"upgrade smoke: {label}: {time.monotonic() - started:.2f}s "
              f"(exit {result.returncode})", flush=True)
    if result.returncode not in accepted:
        raise SmokeFailure(
            f"{label or command[-1]} exited {result.returncode}\n"
            f"{result.stdout}{result.stderr}")
    return result


def isolated_env(root: Path) -> dict[str, str]:
    home = root / "home"
    for name in ("home", "data", "tmp", "models", "config", "share", "cache"):
        (root / name).mkdir(mode=0o700)
    env = {name: os.environ[name] for name in (
        "PATH", "SYSTEMROOT", "WINDIR", "SSL_CERT_FILE", "SSL_CERT_DIR",
        "CARGO_HOME", "RUSTUP_HOME") if name in os.environ}
    env.update({
        "HOME": str(home), "USERPROFILE": str(home),
        "TMPDIR": str(root / "tmp"), "TMP": str(root / "tmp"),
        "TEMP": str(root / "tmp"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_DATA_HOME": str(root / "share"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "APPDATA": str(root / "config"), "LOCALAPPDATA": str(root / "share"),
        "AGREP_HOME": str(home), "AGREP_DATA_DIR": str(root / "data"),
        "AGREP_DATA_DIR_SOURCE": "env", "AGREP_MODEL_DIR": str(root / "models"),
        "AGREP_UPGRADE_SMOKE": str(root), "AGREP_NO_FETCH": "1",
        "AGREP_ON_BATTERY": "0", "AGREP_PROFILE": "compact",
        "CODEX_HOME": str(home / ".codex"),
        "PYTHONNOUSERSITE": "1", "PYTHONUNBUFFERED": "1",
        "PIP_CONFIG_FILE": os.devnull, "NO_COLOR": "1", "TERM": "dumb",
    })
    return env


def checkout_version(checkout: Path) -> str:
    tree = ast.parse((checkout / "agrep" / "__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "__version__"
                        for target in node.targets)):
            version = ast.literal_eval(node.value)
            if isinstance(version, str):
                return version
    raise SmokeFailure("checkout has no readable package version")


def new_venv(path: Path, *, env: dict[str, str], cwd: Path) -> Path:
    run([sys.executable, "-I", "-m", "venv", str(path)], env=env, cwd=cwd,
        label=f"create {path.name} venv")
    return path / "bin" / "python"


def previous_release(python: Path, version: str, *, env: dict[str, str], cwd: Path) -> str:
    code = """
import json, sys, urllib.request
from pip._vendor.packaging.version import InvalidVersion, Version
with urllib.request.urlopen('https://pypi.org/pypi/agrep/json', timeout=30) as response:
    releases = json.load(response)['releases']
current = Version(sys.argv[1])
older = []
for name, files in releases.items():
    try:
        parsed = Version(name)
    except InvalidVersion:
        continue
    if parsed < current and any(not item.get('yanked', False) for item in files):
        older.append((parsed, name))
if not older:
    raise SystemExit('PyPI has no non-yanked agrep release below ' + str(current))
print(max(older)[1])
"""
    return run([str(python), "-I", "-c", code, version], env=env, cwd=cwd,
               timeout=45, label="select previous PyPI release").stdout.strip()


def install(python: Path, requirement: str, *, env: dict[str, str], cwd: Path,
            label: str) -> None:
    run([str(python), "-I", "-m", "pip", "install", "--disable-pip-version-check",
         "--no-input", "--quiet", "--index-url", "https://pypi.org/simple", requirement],
        env=env, cwd=cwd, timeout=600, label=label)


def identity(python: Path, checkout: Path | None, *, env: dict[str, str], cwd: Path) -> dict:
    code = """
import json, sys
from pathlib import Path
if sys.argv[1]:
    root = Path(sys.argv[1])
else:
    import agrep
    root = Path(agrep.__file__).resolve().parent
sys.path.insert(0, str(root / 'py'))
import common, indexd_runtime
binary = common.ingest_bin().resolve(strict=True)
print(json.dumps({
    'version': common.package_version(), 'runtime': indexd_runtime.INDEXD_BUILD_ID,
    'protocol': indexd_runtime.INDEXD_PROTOCOL, 'binary': str(binary),
    'writer': indexd_runtime.derived_writer_build_id(binary, require_binary=True),
    'family_version': common.SESSION_FAMILY_INDEX_VERSION,
}))
"""
    return json.loads(run(
        [str(python), "-I", "-c", code, str(checkout) if checkout else ""],
        env=env, cwd=cwd, label="read runtime identity").stdout)


def sandbox_processes(root: Path) -> dict[int, str]:
    result = subprocess.run(
        ["ps", "axeww", "-o", "pid=", "-o", "command="],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=10, check=True)
    marker = re.compile(r"(?<!\S)" + re.escape(f"AGREP_UPGRADE_SMOKE={root}") + r"(?=\s|$)")
    found = {}
    for line in result.stdout.splitlines():
        fields = line.split(None, 1)
        if len(fields) == 2 and marker.search(fields[1]):
            pid = int(fields[0])
            if pid != os.getpid():
                found[pid] = fields[1]
    return found


def cleanup(root: Path) -> None:
    stopped = set()
    for sig, wait_s in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 3.0)):
        deadline = time.monotonic() + wait_s
        signalled = set()
        while True:
            processes = sandbox_processes(root)
            if not processes:
                print(f"upgrade smoke: cleanup: stopped {len(stopped)} sandbox processes", flush=True)
                return
            for pid in processes.keys() - signalled:
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
                signalled.add(pid)
                stopped.add(pid)
            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)
    raise SmokeFailure(f"sandbox processes survived cleanup: {sorted(sandbox_processes(root))}")


def daemon_receipt(data: Path, expected: dict) -> int | None:
    prefix = f".indexd.v{expected['protocol']}"
    try:
        raw = (data / f"{prefix}.lock").read_bytes()
        fields = dict(item.split("=", 1) for item in raw.decode().split() if "=" in item)
        if (fields.get("build") != expected["runtime"]
                or fields.get("writer") != expected["writer"]):
            return None
        pid = int(fields["pid"])
        os.kill(pid, 0)
        if any(path.read_bytes() == raw for path in data.glob(f"{prefix}.ready*")):
            return pid
    except (OSError, ValueError, KeyError):
        pass
    return None


def corpus_snapshot(data: Path) -> dict:
    with closing(sqlite3.connect(
            (data / "corpus.db").as_uri() + "?mode=ro", uri=True, timeout=0.2)) as db:
        counts = dict(db.execute("SELECT agent, COUNT(*) FROM msgs GROUP BY agent"))
        messages = db.execute(
            "SELECT COUNT(*) FROM msgs WHERE who NOT IN ('agent', 'tool')").fetchone()[0]
        sessions = db.execute("SELECT COUNT(DISTINCT session) FROM msgs").fetchone()[0]
        recaps = db.execute("SELECT COUNT(*) FROM msgs WHERE who = 'recap'").fetchone()[0]
        writer = db.execute("SELECT value FROM meta WHERE key = 'build_id'").fetchone()
    return {"messages": messages, "search_rows": sum(counts.values()), "per_agent": counts,
            "sessions": sessions, "recaps": recaps, "writer": writer[0] if writer else None}


def versions(data: Path) -> dict:
    with (data / ".ingest_cache.bin").open("rb") as stream:
        prefix = stream.read(24)
    if len(prefix) != 24 or prefix[12:20] != b"AGRPCB01":
        raise SmokeFailure("parse cache lacks its framed payload-version header")
    meta = json.loads((data / "session_family.meta.json").read_text(encoding="utf-8"))
    return {"cache_payload": int.from_bytes(prefix[:4], "little"),
            "cache_storage": int.from_bytes(prefix[20:24], "little"),
            "family_meta": meta["version"]}


def upgrade_log(data: Path, offset: int) -> str:
    try:
        with (data / "indexd.log").open("rb") as stream:
            stream.seek(offset)
            return stream.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def wait_publication(data: Path, expected: dict, *, timeout: float,
                     log_offset: int | None = None) -> tuple[int, dict]:
    deadline = time.monotonic() + timeout
    last = "no daemon publication"
    while time.monotonic() < deadline:
        if log_offset is not None and "parse cache discarded" in upgrade_log(data, log_offset):
            raise SmokeFailure("candidate discarded the previous release's parse cache")
        try:
            pid = daemon_receipt(data, expected)
            snapshot = corpus_snapshot(data)
            observed = versions(data)
            last = json.dumps({"pid": pid, **snapshot, **observed}, sort_keys=True)
            if (pid and snapshot["messages"] and snapshot["writer"] == expected["writer"]
                    and observed["family_meta"] == expected["family_version"]):
                return pid, snapshot
        except (OSError, sqlite3.Error, ValueError, KeyError) as exc:
            last = str(exc)
        time.sleep(0.2)
    raise SmokeFailure(f"publication/takeover timed out after {timeout:g}s: {last}")


def wait_search_database(python: Path, checkout: Path | None, *, env: dict[str, str],
                         cwd: Path, timeout: float) -> None:
    code = """
import sys, time
from pathlib import Path
if sys.argv[1]:
    root = Path(sys.argv[1])
else:
    import agrep
    root = Path(agrep.__file__).resolve().parent
sys.path.insert(0, str(root / 'py'))
import indexd_runtime
deadline = time.monotonic() + float(sys.argv[2])
while True:
    state = indexd_runtime._search_db_state()
    if state == 'current':
        break
    if time.monotonic() >= deadline:
        raise SystemExit('search database did not become current: ' + state)
    time.sleep(0.2)
"""
    run([str(python), "-I", "-c", code, str(checkout) if checkout else "", str(timeout)],
        env=env, cwd=cwd, timeout=timeout + 5, label="wait for current search database")


def index_section(output: str) -> str:
    lines = output.splitlines()
    try:
        start = lines.index("index") + 1
    except ValueError as exc:
        raise SmokeFailure("doctor did not print an index section") from exc
    section = []
    for line in lines[start:]:
        if line and not line[0].isspace():
            break
        section.append(line)
    result = "\n".join(section).strip("\n")
    if "search db" not in result or "corpus" not in result:
        raise SmokeFailure(f"doctor index section lacks corpus/search-db evidence:\n{result}")
    if "[!!]" in result or "torn-generation" in result:
        raise SmokeFailure(f"doctor reports an unhealthy index:\n{result}")
    return result


def exercise(root: Path, args: argparse.Namespace) -> None:
    env = isolated_env(root)
    data = root / "data"
    for store in FIXTURE_STORES:
        shutil.copytree(FIXTURES / store, root / "home" / f".{store}", dirs_exist_ok=True)
    (data / "settings.json").write_text('{"embeddings":"off"}\n', encoding="utf-8")
    previous_python = new_venv(root / "previous", env=env, cwd=root)
    previous = args.previous or previous_release(
        previous_python, checkout_version(args.candidate_checkout), env=env, cwd=root)
    install(previous_python, f"agrep=={previous}", env=env, cwd=root,
            label=f"install previous {previous}")
    old_cli = [str(previous_python.parent / "agrep")]
    if args.candidate_wheel:
        candidate_python = new_venv(root / "candidate", env=env, cwd=root)
        install(candidate_python, str(args.candidate_wheel), env=env, cwd=root,
                label="install candidate wheel")
        candidate_cli = [str(candidate_python.parent / "agrep")]
        candidate_checkout = None
    else:
        candidate_python = Path(sys.executable)
        candidate_checkout = args.candidate_checkout
        candidate_cli = [str(candidate_python), str(candidate_checkout / "cli.py")]
    runtime_env = {**env, "HTTP_PROXY": "http://127.0.0.1:9",
                   "HTTPS_PROXY": "http://127.0.0.1:9", "NO_PROXY": ""}
    old = identity(previous_python, None, env=runtime_env, cwd=root)
    candidate = identity(candidate_python, candidate_checkout, env=runtime_env, cwd=root)
    print(f"upgrade smoke: previous={old['version']} candidate={candidate['version']} "
          f"runtime={candidate['runtime']}", flush=True)
    if old["writer"] == candidate["writer"]:
        raise SmokeFailure("candidate and previous writer are identical; no takeover to exercise")
    started = time.monotonic()
    run(old_cli + ["index"], env=runtime_env, cwd=root, timeout=args.timeout,
        label="previous index")
    run(old_cli + [QUERY], env=runtime_env, cwd=root, accepted=(0, 1),
        label="previous ordinary search")
    old_pid, before = wait_publication(data, old, timeout=args.timeout)
    wait_search_database(previous_python, None, env=runtime_env, cwd=root, timeout=args.timeout)
    before = corpus_snapshot(data)
    if (not {"claude", "pi", "codex"} <= before["per_agent"].keys()
            or before["sessions"] != 5 or not before["recaps"]):
        raise SmokeFailure(f"previous release did not index the entire fixture: {before}")
    print(f"upgrade smoke: previous ready in {time.monotonic() - started:.2f}s; "
          f"pid={old_pid} corpus={json.dumps(before, sort_keys=True)} "
          f"versions={json.dumps(versions(data), sort_keys=True)}", flush=True)
    offset = (data / "indexd.log").stat().st_size
    if old_pid not in sandbox_processes(root):
        raise SmokeFailure("previous daemon is not alive immediately before candidate search")
    started = time.monotonic()
    run(candidate_cli + [QUERY], env=runtime_env, cwd=root, accepted=(0, 1),
        timeout=args.timeout, label="candidate ordinary search (old daemon alive)")
    candidate_pid, after = wait_publication(
        data, candidate, timeout=args.timeout, log_offset=offset)
    wait_search_database(candidate_python, candidate_checkout, env=runtime_env,
                         cwd=root, timeout=args.timeout)
    after = corpus_snapshot(data)
    elapsed = time.monotonic() - started
    log = upgrade_log(data, offset)
    adoption = re.search(r"(?:foreign|legacy) parse cache verified and adopted \([1-9]\d* sources\)", log)
    if not adoption or "parse cache discarded" in log:
        raise SmokeFailure("indexd.log does not prove parse-cache adoption without discard")
    if candidate_pid == old_pid or old_pid in sandbox_processes(root):
        raise SmokeFailure("the candidate did not retire the previous daemon")
    for key in ("messages", "search_rows", "per_agent", "sessions", "recaps"):
        if before[key] != after[key]:
            raise SmokeFailure(f"corpus {key} changed during upgrade: {before[key]} -> {after[key]}")
    print(f"upgrade smoke: takeover in {elapsed:.2f}s; pid={candidate_pid}; "
          f"{adoption.group(0)}; versions={json.dumps(versions(data), sort_keys=True)}; "
          f"messages={after['messages']} unchanged", flush=True)
    doctor = run(candidate_cli + ["doctor", "--deep", "--no-semantic"],
                 env=runtime_env, cwd=root, label="candidate doctor")
    print("upgrade smoke: doctor index section:\n" + index_section(doctor.stdout), flush=True)
    found = run(candidate_cli + [QUERY], env=runtime_env, cwd=root, label="candidate fixture search")
    if QUERY not in found.stdout:
        raise SmokeFailure(f"known fixture phrase is absent from search output:\n{found.stdout}")
    print(f"upgrade smoke: found {QUERY!r}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--previous", metavar="VERSION",
                        help="PyPI release to upgrade from (default: newest below checkout version)")
    candidate = parser.add_mutually_exclusive_group()
    candidate.add_argument("--candidate-wheel", type=Path, metavar="PATH")
    candidate.add_argument("--candidate-checkout", type=Path, default=ROOT, metavar="PATH",
                           help="source candidate, including a regression worktree (default: this checkout)")
    parser.add_argument("--timeout", type=float, default=90,
                        help="maximum seconds for each index/takeover wait (default: 90)")
    args = parser.parse_args()
    if os.name != "posix":
        parser.error("upgrade smoke requires POSIX process inspection (macOS or Linux)")
    if not 0 < args.timeout <= 600:
        parser.error("--timeout must be between 0 and 600 seconds")
    args.candidate_checkout = args.candidate_checkout.resolve()
    if args.candidate_wheel:
        args.candidate_wheel = args.candidate_wheel.resolve(strict=True)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="agrep-upgrade-") as raw:
        root = Path(raw).resolve()
        print(f"upgrade smoke: sandbox={root}", flush=True)
        try:
            exercise(root, args)
        except BaseException:
            log = upgrade_log(root / "data", 0)
            if log:
                print("upgrade smoke: indexd.log (last 80 lines):\n"
                      + "\n".join(log.splitlines()[-80:]), file=sys.stderr, flush=True)
            raise
        finally:
            cleanup(root)
    print(f"upgrade smoke: PASS ({time.monotonic() - started:.2f}s total; sandbox removed)", flush=True)
    return 0


if __name__ == "__main__":
    def interrupted(signum, _frame):
        raise SmokeFailure(f"interrupted by signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        raise SystemExit(main())
    except (SmokeFailure, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"upgrade smoke: FAIL: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)
