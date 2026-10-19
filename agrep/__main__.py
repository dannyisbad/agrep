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
    entry = _process_entry()
    exe = "agrep-rs.exe" if sys.platform == "win32" else "agrep-rs"
    bundled = PKG / "_bin" / exe
    if bundled.exists():
        os.environ.setdefault("AGREP_RS_BIN", str(bundled))
    root = PKG if (PKG / "cli.py").is_file() else PKG.parent
    # py/ first so flat imports resolve in both wheels and source checkouts.
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "py"))
    import resident
    result = resident.try_run()
    if result is not None:
        return result
    import cli  # noqa: PLC0415 -- bundled module, resolvable only after the path setup
    import console  # noqa: PLC0415 -- loaded by cli already
    status = cli.main()
    try:
        while entry:
            try:
                console.settle_interrupts()
                break
            except KeyboardInterrupt:  # a Ctrl-C caught before the switch
                status = 130
    except KeyboardInterrupt:
        # one handled at the loop's own back-edge, outside the inner try: end
        # now, as the switched handler would (a builtin call is not checked
        # for signals before it runs)
        os._exit(130)
    return status


def _process_entry() -> bool:
    """Whether main() runs as the process itself: called from the top level of the
    main script (`python -m agrep`, the installed `agrep` script) with no prompt to
    come back to. A REPL, `python -i` or PYTHONINSPECT, IPython, a Jupyter cell or any
    other caller keeps its own Ctrl-C handling."""
    if (hasattr(sys, "ps1") or sys.flags.interactive or sys.flags.inspect
            or (os.environ.get("PYTHONINSPECT") and not sys.flags.ignore_environment)):
        return False
    try:
        caller = sys._getframe(2)  # 0 is this function, 1 is main()
    except (AttributeError, ValueError):
        return False
    code = caller.f_code
    script = getattr(sys.modules.get("__main__"), "__file__", None)
    return (code.co_name == "<module>" and not code.co_filename.startswith("<")
            and caller.f_globals.get("__name__") == "__main__" and script is not None
            and os.path.abspath(code.co_filename) == os.path.abspath(script))


if __name__ == "__main__":
    raise SystemExit(main())
