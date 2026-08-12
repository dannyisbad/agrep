"""Force a platform-specific, python-agnostic wheel tag (py3-none-<platform>).

agrep bundles a prebuilt rust binary, so each wheel is tied to one OS/arch - pip
must only install the matching one. But the python it carries runs on any 3.10+, so
we don't want the wheel pinned to one interpreter (cp312 etc). The natural tag is
`py3-none-win_amd64` / `py3-none-macosx_11_0_arm64` / `py3-none-manylinux...`.

CI sets $AGREP_WHEEL_PLAT to the precise platform tag for the binary it built
(e.g. from `pip debug` / cibuildwheel naming). Absent that, fall back to this
machine's platform tag, which is correct for a local build.

CI distributable wheels must contain the binary. Local/sdist builds compile it when
Cargo is available; otherwise they install the Python layer and the first run offers
a verified binary download or exact Rust setup instructions.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import stat
import subprocess
import sys
import sysconfig
from pathlib import Path, PurePosixPath

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


def _manifest_path(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"agrep: invalid runtime manifest {label}: {value!r}")
    path = PurePosixPath(value)
    if (not value or "\\" in value or path.is_absolute()
            or path.as_posix() != value
            or any(part in {"", ".", ".."} for part in path.parts)):
        raise RuntimeError(f"agrep: invalid runtime manifest {label}: {value!r}")
    return value


def _runtime_sources(root: Path) -> dict[str, Path]:
    manifest = root / "py" / "runtime_manifest.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        raw_files = payload["files"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError(f"agrep: invalid runtime manifest {manifest}: {exc}") from exc
    if payload.get("version") != 1 or not isinstance(raw_files, list):
        raise RuntimeError(f"agrep: invalid runtime manifest schema: {manifest}")
    sources: dict[str, Path] = {}
    seen_sources: set[str] = set()
    for item in raw_files:
        if not isinstance(item, dict) or set(item) != {"source", "member"}:
            raise RuntimeError(f"agrep: invalid runtime manifest entry: {item!r}")
        source = _manifest_path(item["source"], "source")
        member = _manifest_path(item["member"], "member")
        if not member.startswith("agrep/"):
            raise RuntimeError(f"agrep: runtime member is outside agrep/: {member!r}")
        if source in seen_sources or member in sources:
            raise RuntimeError(f"agrep: duplicate runtime manifest entry: {item!r}")
        path = root / source
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            raise RuntimeError(f"agrep: runtime source is unreadable: {source}: {exc}") from exc
        if path.is_symlink() or not stat.S_ISREG(mode):
            raise RuntimeError(f"agrep: runtime source is not a regular file: {source}")
        seen_sources.add(source)
        sources[member] = path
    if sources.get("agrep/py/runtime_manifest.json") != manifest:
        raise RuntimeError("agrep: runtime manifest must list itself")
    return sources


class PlatformWheelHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        root = Path(self.root)
        force_include = build_data.setdefault("force_include", {})
        for member, source in _runtime_sources(root).items():
            if source.parent == root / "agrep":
                continue
            force_include[str(source)] = member
        build_data["pure_python"] = False
        plat = os.environ.get("AGREP_WHEEL_PLAT") or sysconfig.get_platform()
        plat = plat.replace("-", "_").replace(".", "_")
        build_data["tag"] = f"py3-none-{plat}"
        self._ensure_binary()

    def _ensure_binary(self) -> None:
        root = Path(self.root)
        exe = "agrep-rs.exe" if sys.platform == "win32" else "agrep-rs"
        bin_dir = root / "_bin"
        staged = bin_dir / exe
        if bin_dir.exists():
            unexpected = sorted(
                path.name for path in bin_dir.iterdir()
                if path.name != exe
            )
            if unexpected:
                raise RuntimeError(
                    "agrep: refusing workspace-dependent wheel; unexpected _bin/ "
                    f"entries: {', '.join(unexpected)}"
                )
        staged_error: RuntimeError | None = None
        if staged.exists():
            try:
                self._validate_binary_version(staged)
                self._validate_binary_privacy(staged, root)
                if not self._stale_against_local_build(staged, root, exe):
                    return
            except RuntimeError as exc:
                # Never silently replace a CI-staged binary; local source builds may
                # repair a stale artifact below.
                if os.environ.get("AGREP_WHEEL_PLAT"):
                    raise
                staged_error = exc
        # A CI distributable build MUST carry its binary (CI sets AGREP_WHEEL_PLAT and stages
        # _bin first) - refuse to publish a binary-less platform wheel.
        if os.environ.get("AGREP_WHEEL_PLAT"):
            raise RuntimeError(
                "agrep: AGREP_WHEEL_PLAT is set (CI distributable build) but no binary is "
                "staged at _bin/. Stage the prebuilt agrep-rs before packaging."
            )
        cargo = shutil.which("cargo")
        if cargo and (root / "Cargo.toml").exists():
            build_env = self._sanitized_build_env(root)
            subprocess.run([cargo, "build", "--release", "--locked"],
                           cwd=root, check=True,
                           env=build_env)
            staged.parent.mkdir(exist_ok=True)
            shutil.copy2(
                self._cargo_target_dir(root, build_env) / "release" / exe,
                staged)
            self._validate_binary_version(staged)
            return
        if staged_error is not None:
            raise RuntimeError(
                f"{staged_error} No Rust toolchain is available to rebuild the stale "
                "local _bin artifact; remove/re-stage it or install Rust."
            ) from staged_error
        # Degraded sdist install (no prebuilt binary, no rust): ship the python;
        # agrep fetches or guides at first run.
        staged.parent.mkdir(exist_ok=True)  # empty _bin/ so the force-include has a source
        print(
            "  ! agrep: no prebuilt binary and no rust to build one - installing WITHOUT the "
            "ingest binary. `agrep` will offer to fetch it on first use (or `agrep doctor` "
            "shows how to install Rust). Reading an existing index still works.",
            file=sys.stderr,
        )

    def _project_version(self) -> str:
        init = Path(self.root) / "agrep" / "__init__.py"
        try:
            tree = ast.parse(init.read_text(encoding="utf-8"), filename=str(init))
        except (OSError, SyntaxError) as exc:
            raise RuntimeError(f"agrep: cannot read package version from {init}: {exc}") from exc
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if not any(isinstance(target, ast.Name) and target.id == "__version__"
                       for target in targets):
                continue
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value
        raise RuntimeError(f"agrep: __version__ is missing from {init}")

    @staticmethod
    def _validate_binary_privacy(staged: Path, root: Path) -> None:
        """A staged binary with builder paths in it never reaches a wheel.

        `_sanitized_build_env` only covers binaries this hook builds; a
        binary staged by hand (the Windows deploy does exactly that) would
        otherwise ship its builder's paths. Rebuilding is the repair, so
        this raises the same RuntimeError kind the staleness path handles.
        """
        checker = root / "bench" / "validate_binary_privacy.py"
        if not checker.is_file():
            return
        sys.path.insert(0, str(checker.parent))
        try:
            import validate_binary_privacy as privacy
            privacy.validate(staged)
        except privacy.InvalidBinary as exc:  # noqa: F821 - imported above
            raise RuntimeError(
                f"agrep: staged binary fails the release privacy check: {exc}."
            ) from exc
        except ImportError:
            return
        finally:
            sys.path.pop(0)

    @staticmethod
    def _cargo_target_dir(root: Path, env: dict[str, str]) -> Path:
        configured = env.get("CARGO_TARGET_DIR", "")
        target = Path(configured) if configured else root / "target"
        return target if target.is_absolute() else root / target

    @staticmethod
    def _sanitized_build_env(root: Path) -> dict:
        """Build env whose paths cannot end up inside the shipped binary.

        panic/assert sites bake their source path into .rodata, so a stock
        build embeds the builder's ~/.cargo/registry and checkout paths and
        fails the release privacy gate. Cargo's `trim-paths` is still
        unstable, so remap explicitly and preserve any caller RUSTFLAGS.
        """
        env = dict(os.environ)
        cargo_home = Path(
            env.get("CARGO_HOME") or (Path.home() / ".cargo")).resolve()
        remaps = [f"--remap-path-prefix={cargo_home}=/cargo",
                  f"--remap-path-prefix={root.resolve()}=/agrep"]
        existing = env.get("RUSTFLAGS", "").strip()
        env["RUSTFLAGS"] = " ".join(
            ([existing] if existing else []) + remaps)
        return env

    def _stale_against_local_build(
            self, staged: Path, root: Path, exe: str) -> bool:
        """True when a local build exists and the staged bytes are not it.

        The release version is the only identity `_validate_binary_version`
        checks, and it stays constant across source-only rebuilds - so a
        `_bin/` artifact staged days ago can validate perfectly and ship silently
        in place of the source you just built. That has invalidated live
        measurements twice. CI stages deliberately and is never second-guessed.
        """
        if os.environ.get("AGREP_WHEEL_PLAT"):
            return False
        built = self._cargo_target_dir(root, os.environ) / "release" / exe
        try:
            if not built.is_file():
                return False
            return built.read_bytes() != staged.read_bytes()
        except OSError:
            return False

    def _validate_binary_version(self, path: Path) -> None:
        expected = f"agrep-rs {self._project_version()}"
        try:
            result = subprocess.run(
                [str(path), "--version"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=10, check=False,
                **({"creationflags": subprocess.CREATE_NO_WINDOW}
                   if sys.platform == "win32" else {}),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(
                f"agrep: staged binary {path} cannot run for version verification: {exc}"
            ) from exc
        actual = (result.stdout or result.stderr or "").strip()
        if result.returncode != 0 or actual != expected:
            raise RuntimeError(
                f"agrep: staged binary version mismatch: expected {expected!r}, "
                f"got rc={result.returncode} {actual!r}."
            )
