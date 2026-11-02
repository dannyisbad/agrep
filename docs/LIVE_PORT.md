# Live views and explorer

`board` and `tail` read native journals, SQLite stores and passive process
information directly. They do not build the history index. One observer serves
an interactive board or explorer; tail uses the same reader synchronously.

The observer polls sources every 700 ms, skips unchanged databases (including
WAL changes), caches Cursor metadata, and scans processes at most once per two
seconds while sessions are working. Ingest and live share wrapper predicates.
Long tools remain working; only thinking and replying decay to stalled.

Board owns raw mode and restores it on exit and typed Ctrl-Z suspension. Tail,
serve and ui retain default job-control suspension. Board preferences are read
without creating a data root. Only the initial scan offers a longer-wait retry.

The explorer binds to loopback, authenticates a single-use link into an HttpOnly
SameSite cookie, validates Host/Origin, and supplies a per-response CSP nonce.
Its thin JSON adapter calls `history::sessions`, `history::latest`,
`history::turn_count` and `evidence::load`; it contains no independent row or
event selection. Unknown routes return 404 before freshness or storage work.

The explorer currently exposes keyword search. Integrating the shared semantic
route belongs to the combined semantic merge. `Explorer::with_refresh` uses the
CLI freshness entry point; the engine merge supplies its stat-only census.

Platform exclusions and reasons live only in `bench/test-skips.json`.
