"""Console entry for `agrep` (and `python -m agrep`).

The wheel installs the flat agrep tree as package data under this package:

    site-packages/agrep/
        cli.py  reindex.py  py/*.py  _bin/agrep-rs[.exe]

We point the code at the bundled rust binary (AGREP_RS_BIN), put the bundled dirs on
sys.path so the existing flat imports (`import common`, `import cli`) resolve, then
hand off to cli.py. Bare `agrep` prints status + usage and exits, like `python cli.py`.
Data and model artifacts go to a per-user directory because site-packages is read-only.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent


def main() -> int:
    exe = "agrep-rs.exe" if sys.platform == "win32" else "agrep-rs"
    bundled = PKG / "_bin" / exe
    if bundled.exists():
        os.environ.setdefault("AGREP_RS_BIN", str(bundled))
    # py/ first so flat `import common` wins; PKG so `import cli` finds cli.py
    sys.path.insert(0, str(PKG / "py"))
    sys.path.insert(0, str(PKG))
    import cli  # noqa: PLC0415 -- bundled module, resolvable only after the path setup
    return cli.main()


if __name__ == "__main__":
    raise SystemExit(main())
