"""Console entry for `agrep` (and `python -m agrep`).

The wheel installs the flat tilt tree as package data under this package:

    site-packages/agrep/
        tilt.py  reindex.py  py/*.py  web/app.html  _bin/tilt-rs[.exe]

We point the code at the bundled rust binary (TILT_RS_BIN), put the bundled dirs on
sys.path so the existing flat imports (`import common`, `import tilt`) resolve, then
hand off to tilt.py's CLI. Bare `agrep` == `agrep up` (index, serve, open), same as
`python tilt.py`. Data and the smart-tier venv go to a per-user dir (see common.py),
since site-packages is read-only.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent


def main() -> int:
    exe = "tilt-rs.exe" if sys.platform == "win32" else "tilt-rs"
    bundled = PKG / "_bin" / exe
    if bundled.exists():
        os.environ.setdefault("TILT_RS_BIN", str(bundled))
    # py/ first so flat `import common` wins; PKG so `import tilt` finds tilt.py
    sys.path.insert(0, str(PKG / "py"))
    sys.path.insert(0, str(PKG))
    import tilt  # noqa: PLC0415 -- bundled module, resolvable only after the path setup
    return tilt.main()


if __name__ == "__main__":
    raise SystemExit(main())
