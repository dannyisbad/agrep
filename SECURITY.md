# Security policy

## Reporting a vulnerability

Please use GitHub's private **Report a vulnerability** form for this repository. Do not
include private transcript data, access tokens, or a working exploit in a public issue.
Include the affected agrep version, operating system, reproduction steps, and whether the
CLI, an agent-store adapter, or the downloaded Rust binary is involved.

The latest released version receives security fixes. A report is acknowledged as soon as
practical; confirmed issues are coordinated privately until a fixed release is available.

## Local-data threat model

agrep indexes private agent transcripts locally. The index and source archives can
contain secrets and should be protected like the original conversations. On POSIX,
agrep creates its data root, archive, model and log directories with mode 0700 and
tightens an existing owned root when it validates FORMAT. `agrep doctor` reports
remaining group/world permission exposure. Filesystem ACLs and account security
remain the operating system's responsibility.

Transcript text is untrusted. One shared human-output sanitizer neutralizes control
characters and bidirectional/format characters, including in evidence headers.
JSON output escapes these characters while preserving their decoded values.

Model downloads use pinned revisions, lengths and SHA-256 digests. Status and doctor
verify model integrity using the same verified-file stamps as model loading.
An installed model failure is disclosed and logged; optional meaning retrieval still
returns the available keyword results.

The npm launcher verifies the selected binary's SHA-256 checksum and fails closed
when the checksum is missing or wrong. There is no unverified-binary override.
Native resume refuses unsafe session identifiers and relative working directories.
Archive restore destinations remain restricted and checked for path traversal.
