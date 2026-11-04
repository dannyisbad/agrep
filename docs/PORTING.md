# Deliberate differences from 0.4.9

agrep 0.5 matches 0.4.9's output except where an entry below says otherwise, and each
entry gives its reason. Browse and diagnostics differences are in [BROWSE.md](BROWSE.md);
integration and archive evidence is in [INTEGRATIONS-ARCHIVE.md](INTEGRATIONS-ARCHIVE.md).

## Search, recall and pack

- The JSON envelope names the engine `sqlite` and has no publication age, owner or
  generation fields: the daemon and publication machinery they described is gone.
- JSON lines use compact separators and 0.5's own key order everywhere (0.4.9's around
  spaced its separators); decoded values are identical: one serializer writes every surface.
- Meaning-match rows carry `(meaning match)` after `~semantic`: SPEC §4 requires semantic
  rows to be labeled so.
- Search hits carry no `concept`: 0.4.9 filled it only from an optional concept-naming
  stage that 0.5 does not run, and printed it empty without one.
- No automatic over-specification lane runs, so a compact page 0.4.9 would have retried
  keeps its exact count and notice: coverage recovery is the explicit `--coverage` lane.
- Under `--no-meta`, recall and pack take each chat's best row from the ranking `--chats`
  uses, which folds tiers into one score, where 0.4.9 took it tier-first: one chat ranking
  serves both, and they differ only for a chat whose best phrase row scores below one of
  its all-terms rows.
- A tool event keeps only its trimmed search text, so `--tool-output` and JSON
  `input`/`output` lack the surrounding whitespace 0.4.9 stored separately, and
  `[+N indexed chars]` counts the trimmed output: the index stores one text per event.
- Continuation pages live in a small SQLite store and survive changes to source content
  until their TTL: the 0.5 frozen-page contract.
- Recall's structured hit also carries its verified handle and `self` bit, and source reply
  truncation is known from retained reply metadata where 0.4.9 reported it as unknown: 0.5
  keeps that metadata.

## Caller exclusion, truncation flags and terminal quoting

- summary, chats and around also carry the self-exclusion disclosure (summary and chats keep
  `excluded_hits` null because they count chats in `self_excluded`, and a bare `chats`
  listing reports `not-applicable`): every surface that withholds rows says so.
- `-s` results keep `excluded_hits` null where 0.4.9 ran semantic probes: 0.5 has no exact
  semantic count to subtract.
- Indexing diagnostics use the shared `terminal_safe` quoting, where 0.4.9's Rust helper
  quoted with four or five hex digits instead of `\U` and also escaped the two joiners: one
  sanitizer serves every surface.

## Status

- `status` prints one line per remaining fact without `[ok ]` markers instead of 0.4.9's
  sectioned doctor report: its other rows (ingest binary, Python, runtime and installed
  build, daemon freshness, search database generation) describe machinery 0.5 removed.
- The counts line names sessions, prose rows and tool rows where 0.4.9 counted messages per
  agent: tool rows are searchable.
- The JSON object is 0.5's own (`data_root`, `format`, `index`, `sources`, `semantic`,
  `integrations`, `archive`, `permissions`, `freshness`, `pending_sources`) without 0.4.9's
  daemon, diagnostics-tier and build-identity keys: those described removed machinery.
- A bare `agrep` runs `status` rather than 0.4.9's welcome page of recent chats and example
  commands: SPEC §6 (`agrep chats` lists the recent chats).

## Integrations and archive port

- Setup replaces every agrep-marked block, including an edited one: SPEC §7. Unmarked skills
  and edited helper files stay protected.
- Hook registration merges only the owned entry into existing hook arrays, and JSON
  whitespace may be reformatted with the original LF/CRLF convention: other hooks and values
  must survive the merge.
- Consent and completion output omit the dependency, daemon, owner and sentinel stages, and
  invalid settings use the common CLI error prefix: those stages no longer exist.
- Archives use a version-1 SQLite catalog and immutable 1 MiB SHA-256 gzip chunks, never read
  a 0.4.x manifest or blob, and report stored compressed bytes: SPEC §2 shares no data with
  0.4.x.
- An explicit capture snapshots SQLite through the backup API, and scheduled captures run at
  most once per ten minutes inside `__work`: there is no daemon to settle captures.
- A forced restore into an existing SQLite file uses the backup API under SQLite's write
  lock and preserves contents rather than WAL file bytes: live readers stay coherent.

## Distribution, CI, release and documentation

- No sdist, no runtime Python, and npm downloads the standalone binary: SPEC §1 ships one
  binary.
- `--version` carries no distribution, runtime or build identity, and there is no legacy
  install recovery: SPEC §1 removes that machinery.
- Binaries build for the portable target baseline instead of `target-cpu=native`: one
  binary must run on every machine of its target.

## Browse and diagnostics port

Browse and diagnostics differences are listed in [BROWSE.md](BROWSE.md).
