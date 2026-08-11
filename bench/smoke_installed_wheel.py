#!/usr/bin/env python3
"""Exercise an installed wheel without borrowing user or checkout state."""

from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import sysconfig
import tempfile
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
_MARKER_RE = re.compile(r"<!-- agrep:recall v([0-9]+) -->")
_BLOCK_RE = re.compile(
    r"(?ms)^<!-- agrep:recall v([0-9]+) -->\r?\n"
    r"(.*?)\r?\n<!-- /agrep:recall -->\r?$"
)


class SmokeFailure(RuntimeError):
    pass


def _run(argv: list[str], *, env: dict[str, str], cwd: Path,
         capture: bool = False, label: str = "") -> subprocess.CompletedProcess[str]:
    started = time.perf_counter()
    result = subprocess.run(
        argv, cwd=cwd, env=env, text=True, check=False,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        timeout=120,
    )
    if label:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        print(f"installed wheel smoke: {label} rc={result.returncode} "
              f"full_exit_ms={elapsed_ms:.1f}")
        if capture:
            print(f"installed wheel smoke: {label} stdout={result.stdout!r}")
            print(f"installed wheel smoke: {label} stderr={result.stderr!r}")
    return result


def _failure(label: str, result: subprocess.CompletedProcess[str]) -> SmokeFailure:
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return SmokeFailure(f"{label} failed (exit {result.returncode})")


def _require_exit(
        label: str, result: subprocess.CompletedProcess[str],
        accepted: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[str]:
    if result.returncode not in accepted:
        raise _failure(label, result)
    return result


def _source_nudge_contract() -> tuple[int, str, str]:
    """(version, default-template sha, codex-rendered sha).

    The prompt text lives in py/nudge_default.md and py/nudge_codex.md
    (NUDGE_V still lives in teach.py); templates carry {name}/{be} person
    slots, so the body teach writes into a codex AGENTS.md is the codex
    file rendered as "you"/"are", not the template bytes.
    """
    tree = ast.parse((ROOT / "py" / "teach.py").read_text(encoding="utf-8"))
    version = None
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "NUDGE_V"):
            version = ast.literal_eval(node.value)
    default = (ROOT / "py" / "nudge_default.md").read_text(
        encoding="utf-8").rstrip("\n")
    codex = (ROOT / "py" / "nudge_codex.md").read_text(
        encoding="utf-8").rstrip("\n")
    if type(version) is not int:
        raise SmokeFailure("source instruction contract is unreadable")
    rendered = codex.format(name="you", be="are")
    return (version,
            hashlib.sha256(default.encode("utf-8")).hexdigest(),
            hashlib.sha256(rendered.encode("utf-8")).hexdigest())


def _installed_candidate_contract(
        *, env: dict[str, str], cwd: Path,
) -> dict[str, object]:
    code = (
        "import agrep,hashlib,json,sys;"
        "from pathlib import Path;"
        "package=Path(agrep.__file__).resolve();"
        "py_root=package.parent/'py';"
        "sys.path.insert(0,str(py_root));"
        "import common,dist,indexd_runtime,teach;"
        "binary=Path(common.ingest_bin()).resolve(strict=True);"
        "print(json.dumps({'package':str(package),"
        "'dist_module':str(Path(dist.__file__).resolve()),"
        "'runtime_module':str(Path(indexd_runtime.__file__).resolve()),"
        "'binary':str(binary),"
        "'distribution_build_id':common.distribution_build_id(),"
        "'runtime_build_id':indexd_runtime.INDEXD_BUILD_ID,"
        "'native_binary_build_id':dist.native_binary_build_id(binary),"
        "'writer_build_id':indexd_runtime.derived_writer_build_id("
        "binary,require_binary=True),"
        "'nudge_version':teach.NUDGE_V,"
        "'nudge_sha256':hashlib.sha256(teach.NUDGE.encode('utf-8')).hexdigest()}))"
    )
    result = _require_exit(
        "installed candidate identity",
        _run([sys.executable, "-I", "-c", code], env=env, cwd=cwd,
             capture=True, label="installed candidate identity"),
    )
    try:
        payload = json.loads(result.stdout or "")
    except (TypeError, ValueError) as exc:
        raise SmokeFailure("installed candidate identity is not JSON") from exc
    if not isinstance(payload, dict):
        raise SmokeFailure("installed candidate identity has the wrong shape")
    return payload


def _exact_identity(payload: dict[str, object], field: str) -> str:
    value = payload.get(field)
    if (not isinstance(value, str) or len(value) != 20
            or any(char not in "0123456789abcdef" for char in value)):
        raise SmokeFailure(f"installed {field.replace('_', ' ')} is invalid")
    return value


def _verify_version_identity(output: str, exact: dict[str, str]) -> None:
    token = r"(?:[0-9a-f]{20}|unavailable)"
    matched = re.fullmatch(
        rf"agrep \S+ distribution (?P<distribution>{token}) "
        rf"runtime (?P<runtime>{token}) native (?P<native>{token}) "
        rf"writer (?P<writer>{token})",
        output.strip(),
    )
    if matched is None:
        raise SmokeFailure("installed --version has the wrong identity shape")
    for name, expected in exact.items():
        observed = matched.group(name)
        if observed == "unavailable":
            if name in {"distribution", "runtime"}:
                raise SmokeFailure(f"installed {name} identity is unavailable")
            continue
        if observed != expected:
            raise SmokeFailure(f"installed {name} identity does not match exact proof")


def _inside(path: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath((str(path), str(parent))) == str(parent)
    except ValueError:
        return False


def _write_codex_rollout(root: Path) -> tuple[str, str, str, Path]:
    now = datetime.now(timezone.utc)
    session = str(uuid.uuid4())
    turn = str(uuid.uuid4())
    query = f"late pipeline goal {uuid.uuid4().hex}"
    artifact = f"{query}: LATE_PIPELINE_GOAL.md contains the frozen release stages."
    reply = "PIPELINE_STAGES.md is the approved artifact; use stages one through four."
    stamp = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    records = [
        {
            "timestamp": stamp,
            "type": "session_meta",
            "payload": {
                "type": "session_meta", "id": session,
                "cwd": str(root / "workspace" / "wheel-smoke"),
            },
        },
        {
            "timestamp": stamp,
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": turn},
        },
        {
            "timestamp": stamp,
            "type": "turn_context",
            "payload": {"model": "wheel-smoke", "turn_id": turn},
        },
        {
            "timestamp": stamp,
            "type": "response_item",
            "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": artifact}],
            },
        },
        {
            "timestamp": stamp,
            "type": "event_msg",
            "payload": {"type": "user_message", "message": artifact},
        },
        {
            "timestamp": stamp,
            "type": "response_item",
            "payload": {
                "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": reply}],
            },
        },
    ]
    day = root / "home" / ".codex" / "sessions" / now.strftime("%Y/%m/%d")
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-{now:%Y-%m-%dT%H-%M-%S}-{session}.jsonl"
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in records),
        encoding="utf-8",
    )
    return query, artifact, reply, path


def _instruction_version(path: Path, *, expected_sha256: str) -> int:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SmokeFailure(f"installed Codex instructions are unreadable: {path}") from exc
    versions = _MARKER_RE.findall(text)
    blocks = list(_BLOCK_RE.finditer(text))
    if (len(versions) != 1 or len(blocks) != 1
            or text.count("<!-- /agrep:recall -->") != 1):
        raise SmokeFailure("installed Codex instructions lack one complete recall block")
    version, body = blocks[0].groups()
    normalized = body.replace("\r\n", "\n")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    if digest != expected_sha256:
        raise SmokeFailure("installed Codex instruction body differs from the source candidate")
    return int(version)


def _status(
        cli: Path, *, env: dict[str, str], cwd: Path,
) -> dict[str, object]:
    result = _require_exit(
        "status --json",
        _run([str(cli), "status", "--json"], env=env, cwd=cwd, capture=True),
    )
    try:
        payload = json.loads(result.stdout or "")
    except (TypeError, ValueError) as exc:
        raise SmokeFailure("status --json returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise SmokeFailure("status --json returned the wrong shape")
    return payload


def _wait_for_daemon(
        cli: Path, *, env: dict[str, str], cwd: Path, timeout_s: float = 15.0,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_s
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        last = _status(cli, env=env, cwd=cwd)
        daemon = last.get("daemon")
        if isinstance(daemon, dict) and daemon.get("running") is True:
            print("installed wheel smoke: daemon ready rc=0")
            return last
        time.sleep(0.1)
    raise SmokeFailure(f"installed daemon did not become ready: {last!r}")


def _isolated_env(root: Path) -> dict[str, str]:
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("AGREP_")
    }
    home = root / "home"
    data = root / "data"
    models = root / "models"
    for path in (home, data, models):
        path.mkdir(parents=True)
    env.update({
        "AGREP_DATA_DIR": str(data),
        "AGREP_DATA_DIR_SOURCE": "env",
        "AGREP_HOME": str(home),
        "AGREP_MODEL_DIR": str(models),
        "AGREP_NO_DAEMON": "1",
        "AGREP_NO_FETCH": "1",
        "ALL_PROXY": "http://127.0.0.1:9",
        "APPDATA": str(root / "appdata"),
        "CLINE_DIR": str(root / "cline"),
        "CODEX_HOME": str(home / ".codex"),
        "CRUSH_GLOBAL_DATA": str(root / "crush"),
        "HOME": str(home),
        "LOCALAPPDATA": str(root / "localappdata"),
        "NO_PROXY": "",
        "OPENCODE_DB": "",
        "PYTHONNOUSERSITE": "1",
        "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(root / "xdg-config"),
        "XDG_DATA_HOME": str(root / "xdg-data"),
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "all_proxy": "http://127.0.0.1:9",
        "http_proxy": "http://127.0.0.1:9",
        "https_proxy": "http://127.0.0.1:9",
        "no_proxy": "",
    })
    for name in ("CLAUDECODE", "CLAUDE_CODE", "CLAUDE_CODE_ENTRYPOINT",
                 "CLINE_ACTIVE", "CODEX_SANDBOX", "CODEX_THREAD_ID",
                 "CURSOR_AGENT", "GEMINI_CLI", "OPENCODE",
                 "PYTHONHOME", "PYTHONPATH"):
        env.pop(name, None)
    return env


def _installed_cli(override: str | None) -> Path | None:
    if override:
        return Path(override).resolve()
    suffix = ".exe" if sys.platform == "win32" else ""
    candidate = Path(sysconfig.get_path("scripts")) / f"agrep{suffix}"
    return candidate.resolve() if candidate.is_file() else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agrep")
    args = parser.parse_args()
    cli = _installed_cli(args.agrep)
    if cli is None:
        print("installed agrep entry point is missing from this Python", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="agrep-wheel-smoke-") as raw:
        root = Path(raw)
        env = _isolated_env(root)
        recall_query, artifact, reply, _rollout = _write_codex_rollout(root)
        import_code = (
            "import numpy,onnxruntime,tokenizers,agrep,sys;"
            "from pathlib import Path;"
            "sys.path.insert(0,str(Path(agrep.__file__).parent/'py'));"
            "import embedding_segments,segment_query,"
            "semantic_segment_build,semantic_segment_compact"
        )
        imports = _run(
            [sys.executable, "-I", "-c", import_code],
            env=env, cwd=root, capture=True)
        if imports.returncode:
            sys.stderr.write(imports.stdout or "")
            sys.stderr.write(imports.stderr or "")
            return imports.returncode

        smoke = _run(
            [sys.executable, str(Path(__file__).with_name("onnx_smoke.py"))],
            env=env, cwd=root)
        if smoke.returncode:
            return smoke.returncode
        installed = _installed_candidate_contract(env=env, cwd=root)
        package = Path(str(installed.get("package", ""))).resolve()
        if _inside(package, ROOT.resolve()):
            raise SmokeFailure("installed runtime resolved into the source checkout")
        installed_py = package.parent / "py"
        for field in ("dist_module", "runtime_module"):
            module = Path(str(installed.get(field, ""))).resolve()
            if not _inside(module, installed_py.resolve()):
                raise SmokeFailure(f"installed {field} escaped the wheel runtime")
        suffix = ".exe" if sys.platform == "win32" else ""
        native = package.parent / "_bin" / f"agrep-rs{suffix}"
        binary = Path(str(installed.get("binary", ""))).resolve()
        if not native.is_file() or binary != native.resolve():
            raise SmokeFailure("installed native binary is missing or misresolved")
        identities = {
            "distribution": _exact_identity(installed, "distribution_build_id"),
            "runtime": _exact_identity(installed, "runtime_build_id"),
            "native": _exact_identity(installed, "native_binary_build_id"),
            "writer": _exact_identity(installed, "writer_build_id"),
        }
        expected = hashlib.sha256(native.read_bytes()).hexdigest()[:20]
        if identities["native"] != expected:
            raise SmokeFailure(
                "installed native identity does not match bundled bytes")
        version = _run(
            [str(cli), "--version"], env=env, cwd=root, capture=True)
        if version.returncode:
            sys.stdout.write(version.stdout or "")
            sys.stderr.write(version.stderr or "")
            return version.returncode
        _verify_version_identity(version.stdout or "", identities)

        query = f"smoke-needle-{uuid.uuid4().hex}"
        search = _run([str(cli), query], env=env, cwd=root)
        if search.returncode not in (0, 1):
            print(f"search crashed (exit {search.returncode})", file=sys.stderr)
            return 1
        try:
            owner = json.loads((root / "data" / ".derived-owner.json").read_text(
                encoding="utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            raise SmokeFailure("installed writer published no readable owner") from exc
        if (not isinstance(owner, dict) or owner.get("version") != 1
                or owner.get("build_id") != identities["writer"]):
            raise SmokeFailure("installed writer identity has no publication receipt")

        semantic = _run(
            [str(cli), "-s", "smoke-needle-offline"],
            env=env, cwd=root, capture=True)
        if (semantic.returncode != 2
                or "semantic search unavailable" not in (semantic.stderr or "")):
            sys.stdout.write(semantic.stdout or "")
            sys.stderr.write(semantic.stderr or "")
            print(
                "offline -s violated its strict-unavailable contract "
                f"(exit {semantic.returncode})",
                file=sys.stderr,
            )
            return 1
        if "fetching the semantic model" in (semantic.stderr or ""):
            print(
                "offline no-daemon -s falsely reported a model fetch",
                file=sys.stderr,
            )
            return 1
        if any((root / "models").iterdir()):
            print("offline no-daemon -s mutated the model cache", file=sys.stderr)
            return 1
        if (root / "data" / "semantic-embed.log").exists():
            print(
                "offline no-daemon -s started a background semantic refresh",
                file=sys.stderr,
            )
            return 1

        source_version, source_digest, codex_digest = _source_nudge_contract()
        if (installed.get("nudge_version") != source_version
                or installed.get("nudge_sha256") != source_digest):
            raise SmokeFailure(
                "installed instructions do not match the source candidate")

        live_env = dict(env)
        live_env.pop("AGREP_NO_DAEMON", None)
        setup = _require_exit(
            "setup",
            _run(
                [str(cli), "setup", "-y", "--no-semantic", "--no-archive"],
                env=live_env, cwd=root, capture=True, label="setup"),
        )
        if "setup complete" not in (setup.stdout or ""):
            raise SmokeFailure("setup returned success without its completion receipt")
        instructions = root / "home" / ".codex" / "AGENTS.md"
        if _instruction_version(
                instructions, expected_sha256=codex_digest) != source_version:
            raise SmokeFailure("installed Codex instructions have the wrong version")

        found = _require_exit(
            "keyword search",
            _run(
                [str(cli), recall_query, "--lexical", "--color", "never"],
                env=live_env, cwd=root, capture=True, label="keyword search"),
        )
        if artifact not in (found.stdout or ""):
            raise SmokeFailure("installed keyword search omitted the indexed artifact")

        probe = _require_exit(
            "recall probe",
            _run(
                [str(cli), "recall", recall_query, "--probe", "--lexical",
                 "--color", "never"],
                env=live_env, cwd=root, capture=True, label="recall probe"),
        )
        handle = re.search(
            r"@[A-Za-z0-9._-]+:\d+\.[0-9a-f]{4}", probe.stdout or "")
        if handle is None:
            raise SmokeFailure("installed recall probe returned no reusable handle")
        around = _require_exit(
            "around",
            _run(
                [str(cli), "around", handle.group(0), "--full"],
                env=live_env, cwd=root, capture=True, label="around"),
        )
        if artifact not in (around.stdout or "") or reply not in (around.stdout or ""):
            raise SmokeFailure("installed around omitted the artifact or paired answer")

        _wait_for_daemon(cli, env=live_env, cwd=root)
        removed = _require_exit(
            "remove",
            _run([str(cli), "remove"], env=live_env, cwd=root,
                 capture=True, label="remove"),
        )
        if "removing agrep integration" not in (removed.stdout or ""):
            raise SmokeFailure("remove returned success without its teardown receipt")
        daemon = _status(cli, env=live_env, cwd=root).get("daemon")
        if not isinstance(daemon, dict) or daemon.get("running") is not False:
            raise SmokeFailure("installed remove left the freshness daemon running")
        if instructions.is_file() and _MARKER_RE.search(
                instructions.read_text(encoding="utf-8")):
            raise SmokeFailure("installed remove left the Codex instruction block")
    print("installed wheel smoke: PASS")
    return 0


if __name__ == "__main__":
    try:
        result = main()
    except subprocess.TimeoutExpired as exc:
        print(f"installed wheel smoke timed out: {exc.cmd}", file=sys.stderr)
        result = 1
    except SmokeFailure as exc:
        print(f"installed wheel smoke failed: {exc}", file=sys.stderr)
        result = 1
    raise SystemExit(result)
