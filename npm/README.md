# agrep

`npm install -g @mundy/agrep`, then `agrep setup`.

Search and resume local AI agent history. The first run downloads the binary for
this package version from GitHub, checks its package-pinned SHA-256, and caches
it under that hash. Later runs execute the cached binary without hashing it again.
Node 22.15+ is required; Python is not. Supports Linux glibc 2.28+, macOS and
Windows, x64 and arm64.

See https://github.com/dannyisbad/agrep for commands and data locations.
