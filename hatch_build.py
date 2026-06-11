"""Force a platform-specific, python-agnostic wheel tag (py3-none-<platform>).

agtilt bundles a prebuilt rust binary, so each wheel is tied to one OS/arch — pip
must only install the matching one. But the python it carries runs on any 3.10+, so
we don't want the wheel pinned to one interpreter (cp312 etc). The natural tag is
`py3-none-win_amd64` / `py3-none-macosx_11_0_arm64` / `py3-none-manylinux...`.

CI sets $AGTILT_WHEEL_PLAT to the precise platform tag for the binary it built
(e.g. from `pip debug` / cibuildwheel naming). Absent that, fall back to this
machine's platform tag, which is correct for a local build.
"""

from __future__ import annotations

import os
import sysconfig

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class PlatformWheelHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        build_data["pure_python"] = False
        plat = os.environ.get("AGTILT_WHEEL_PLAT") or sysconfig.get_platform()
        plat = plat.replace("-", "_").replace(".", "_")
        build_data["tag"] = f"py3-none-{plat}"
