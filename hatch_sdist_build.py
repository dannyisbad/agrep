"""Build source archives from agrep's closed source allowlist."""

import importlib.util
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


_SPEC = importlib.util.spec_from_file_location(
    "agrep_sdist_sources", Path(__file__).with_name("sdist_sources.py"))
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("cannot load the sdist source allowlist")
_SOURCES = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_SOURCES)


class SdistPrivacyHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        allowed = _SOURCES.source_files(Path(self.root))
        force_include = build_data["force_include"]
        force_include.clear()
        force_include.update({str(path): name for name, path in allowed.items()})
