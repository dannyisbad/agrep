# Incremental embedding-plan scale

Measured on the same Apple-silicon host used by the semantic
scale gate. Reproduce the bounded metadata ladder with:

```sh
python bench/embed_plan_scale.py --check --json
```

The transcript-walking planner streams and hashes every message and reply before
each bounded backfill pass. A private evaluation snapshot showed that even a
zero-change plan paid hundreds of milliseconds before inference. A source-byte
projection puts that walk near 104 seconds at 10M rows; repeating it for each
128-1,000-row tranche makes a large partial build non-viable.

The bounded temporary SQLite plan stores only ID, source hash, session, timestamp,
and source sequence. It is bound to the exact transcript generation, embedding
publication, and model profile, which includes the engine lane that produced the
existing rows. A committed vector publication advances its cursor in one
transaction; a source or output mismatch falls back to full reconciliation.
Selected prose is resolved from the generation-current corpus DB and re-hashed
before inference. The plan is deleted when the backlog reaches zero.

Idle AC catch-up grows publication tranches geometrically with the indexed row
count, capped at 1M rows. Active, battery, and pressure modes retain their small
128-500 row bounds, and a moving transcript stops an idle pass after its current
500-row inference chunk. Each completed tranche publishes an immutable delta segment.
The 10M storage/publication contract is guarded by
`bench/semantic_segment_scale.py`; compaction correctness is covered by
`py/test_semantic_segment_compact.py`.

| rows | one-time plan build | temporary plan | 1,000-row plan advance | source walks |
|---:|---:|---:|---:|---:|
| 100k | 0.147 s | 14.0 MiB | 1.09 ms | 0 |
| 1M | 1.511 s | 145.1 MiB | 1.11 ms | 0 |
| 10M projected | 15.1 s | 1.42 GiB | 6.38 ms | 0 |

The plan-advance timing covers metadata only. Resolving and hash-checking 1,000
bounded rows through the disposable corpus DB took 16.4 ms. The plan gate measures
metadata planning and source avoidance; vector publication scale belongs to the
segmented-index gate.
