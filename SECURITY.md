# Security policy

## Reporting a vulnerability

Please use GitHub's private **Report a vulnerability** form for this repository. Do not
include private transcript data, access tokens, or a working exploit in a public issue.
Include the affected agrep version, operating system, reproduction steps, and whether the
CLI, an agent-store adapter, or the downloaded Rust binary is involved.

The latest released version receives security fixes. A report is acknowledged as soon as
practical; confirmed issues are coordinated privately until a fixed release is available.

## Local-data threat model

agrep indexes private agent transcripts on the user's machine. The corpus and model inputs
remain local, but the index is sensitive plaintext and should be protected like the source
transcripts. The default data directory is private to the current OS user. `AGREP_DATA_DIR`
may point elsewhere; `agrep doctor` warns when a POSIX directory is group/world-readable.

Transcript text is untrusted. Human terminal renderers quote control and bidirectional
format characters; JSON modes preserve content through JSON escaping. Store adapters and
archive restore paths must remain confined to their documented roots.

Downloaded `agrep-rs` executables are version-matched and SHA-256 verified before they are
installed. Missing or invalid checksums fail closed unless the user explicitly sets
`AGREP_ALLOW_UNVERIFIED_BINARY=1`, which is intended only for a trusted custom mirror.
