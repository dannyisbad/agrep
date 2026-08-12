# Documentation map

Every design and measurement document in the tree, one line each. The root
[README](../README.md) is the product introduction and the install/usage
reference; everything below is for readers who need to know *why* something
behaves the way it does, or what number backs a claim.

## Design

How the system is built and what its surfaces promise.

- [ARCHITECTURE.md](ARCHITECTURE.md): the module and layer structure, the test
  that enforces each claim, and the known exceptions.
- [OUTPUT_CONTRACTS.md](OUTPUT_CONTRACTS.md): the two output layers.
  Deterministic machine output (`--json`, `--flat`, `-c`) never routes;
  interactive output may. Start here before changing anything a script parses.
- [SEARCH_RANKING.md](SEARCH_RANKING.md): keyword match lanes, the scoring
  formula, the code-aware boundary factor, and how compact pages are assembled.
- [HANDLE_IDENTITY.md](HANDLE_IDENTITY.md): why `@session:turn.digest` carries
  a content claim, and what happens when the content moves, vanishes, or is
  ambiguous.
- [COORDINATION.md](COORDINATION.md): the exclusive-create ownership protocol:
  every cross-process claim file, its exact byte format, and its reclaim policy,
  in one table. Read it before adding anything that locks.
- [../py/README.md](../py/README.md): the Python side: the on-disk data
  contract, the semantic lane's two embedding engines, and a file-by-file map.

## Benchmarks

Measured evidence. Each names what it measured and how to reproduce it.

- [EMBED_SPEED.md](../bench/EMBED_SPEED.md): initial-embed throughput in
  rows/s, including which noisy samples were discarded and why.
- [EMBED_TOPUP.md](../bench/EMBED_TOPUP.md): incremental-plan scale, the cost
  of the planner's transcript walk before any inference happens.
- [SEMANTIC_SCALE.md](../bench/SEMANTIC_SCALE.md): candidate-selection scale to
  2M rows against an adversarial session distribution.
- [SEMANTIC_CLI_SCALE.md](../bench/SEMANTIC_CLI_SCALE.md): process start to
  process exit for real CLI semantic queries, cold and warm.
- [SEMANTIC_SEGMENTS.md](../bench/SEMANTIC_SEGMENTS.md): the design contract for
  immutable segmented publication (a design doc that lives with its harnesses).

## Meta

Policy and process.

- [../CONTRIBUTING.md](../CONTRIBUTING.md): the repo layout, how to run the
  build and the tests, the conventions a patch follows, and how to add support
  for a new agent's store.
- [../SECURITY.md](../SECURITY.md): supported versions and how to report a
  vulnerability.
