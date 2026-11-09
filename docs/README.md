# Documentation

Design:

- [ENGINE.md](ENGINE.md): the data root, locks, census and freshness, refresh,
  storage, FTS indexes and merges, query lanes, counts, caller exclusion, `--chat`
  scopes, background work and known limits.
- [ENGINE-API.md](ENGINE-API.md): the library surface the binary uses.
- [SEMANTIC.md](SEMANTIC.md): the embedding model, query path, vector storage and its
  quality gates.

Contracts:

- [OUTPUT_CONTRACTS.md](OUTPUT_CONTRACTS.md): exit codes, output framing and the
  public output formats.
- [SEARCH_RANKING.md](SEARCH_RANKING.md): matching, ranking, snippets and compact
  pages.
- [HANDLE_IDENTITY.md](HANDLE_IDENTITY.md): content-bound result handles.
- [BROWSE.md](BROWSE.md): chats, summary, resume and run, status, doctor, audit and
  why.
- [PORTING.md](PORTING.md): every deliberate difference from 0.4.9, with its reason.

Building and releasing:

- [DISTRIBUTION.md](DISTRIBUTION.md): checks, the matched performance gate against
  agrep 0.4.9, packages and the tag-only release workflow.
- [RELEASE_VALIDATION.md](RELEASE_VALIDATION.md): what a release requires.
- `perf-budgets.json`: the performance targets and their reasons.
