# Merged semantic measurements

`protocol.json` identifies the merge commit, binary and fixture hashes, machine,
thread counts and sampling policy. `bulk.jsonl` retains one matched process per
engine/backend/fixture, with all four batch durations including the first.
`owner-query.json` retains twenty fresh-process semantic queries and twenty
hybrid queries against 200,000 deterministic references, plus the actual-model
quality check. The gate's nonzero result is retained.

All load arrays contain one-, five- and fifteen-minute averages. Every timed
process includes its own `uptime` reading. Keyword time is an in-command span,
not a difference of independently computed percentiles. Concurrent census and
semantic stages overlap and must not be added together.

`summary.json` aggregates these exact samples and identifies outliers without
discarding them. `validation.json`, `validation-steps.json`, and `quality-*.json`
record the pre-commit source validation; the post-commit build used the same
source and binary content. `budget-resolution.json` records the selected budget
derivation and preserved owner limits.

`privacy-integration.json` records metadata redaction and lossless profile
splitting. The current tree and index pass the privacy checker.
`privacy-history.json` separately identifies an existing reachable blob that
still fails the unchanged historical gate. It is not a clean-history claim.

These files contain aggregates and public synthetic fixture IDs, not query
text, rendered rows, embedding vectors, or owner-store paths. The source
fixture is disclosed synthetic vector geometry; it does not represent 200,000
model inferences or private history.
