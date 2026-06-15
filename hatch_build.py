"""Force a platform-specific, python-agnostic wheel tag (py3-none-<platform>).

agrep bundles a prebuilt rust binary, so each wheel is tied to one OS/arch - pip
must only install the matching one. But the python it carries runs on any 3.10+, so
we don't want the wheel pinned to one interpreter (cp312 etc). The natural tag is
`py3-none-win_amd64` / `py3-none-macosx_11_0_arm64` / `py3-none-manylinux...`.

CI sets $AGREP_WHEEL_PLAT to the precise platform tag for the binary it built
(e.g. from `pip debug` / cibuildwheel naming). Absent that, fall back to this
machine's platform tag, which is correct for a local build.

The hook also guarantees the binary is actually present: CI and local builds stage
_bin/ before packaging, but a `pip install` from the sdist arrives with no _bin -
there we cargo-build it (the sdist carries the rust source), and if cargo is missing
we abort the install with a clear message instead of shipping a wheel whose core
tier can't run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class PlatformWheelHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        build_data["pure_python"] = False
        plat = os.environ.get("AGREP_WHEEL_PLAT") or sysconfig.get_platform()
        plat = plat.replace("-", "_").replace(".", "_")
        build_data["tag"] = f"py3-none-{plat}"
        self._ensure_binary()

    def _ensure_binary(self) -> None:
        root = Path(self.root)
        exe = "agrep-rs.exe" if sys.platform == "win32" else "agrep-rs"
        staged = root / "_bin" / exe
        if staged.exists():
            return
        if not (root / "Cargo.toml").exists():  # wheel build without _bin or source
            raise RuntimeError(
                "agrep: no prebuilt binary at _bin/ and no rust source to build it from"
            )
        cargo = shutil.which("cargo")
        if not cargo:
            raise RuntimeError(
                "agrep: this platform has no prebuilt wheel, so pip is building from "
                "source - that needs Rust (https://rustup.rs). Install cargo and retry, "
                "or use a platform with a prebuilt wheel (win/mac/linux x64, mac arm64)."
            )
        subprocess.run([cargo, "build", "--release"], cwd=root, check=True)
        staged.parent.mkdir(exist_ok=True)
        shutil.copy2(root / "target" / "release" / exe, staged)
