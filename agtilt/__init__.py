"""agtilt — your cross-agent chat history, one command.

This package is a thin wrapper that ships the existing flat tilt codebase (tilt.py,
reindex.py, py/, web/) plus the prebuilt rust ingest binary, so `uvx agtilt` /
`pipx run agtilt` / `pip install agtilt` give you the explorer with no clone and no
cargo. The real logic lives in the bundled modules; see __main__.py.
"""

__version__ = "0.1.0"
