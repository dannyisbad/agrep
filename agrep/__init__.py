"""agrep — grep your cross-agent chat history, one command.

This package is a thin wrapper that ships the existing flat agrep codebase (cli.py,
reindex.py, py/, web/) plus the prebuilt rust ingest binary, so `uvx agrep` /
`pipx run agrep` / `pip install agrep` give you the explorer with no clone and no
cargo. The real logic lives in the bundled modules; see __main__.py.
"""

__version__ = "0.1.0"
