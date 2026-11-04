# 0.5.0 keyword engine

## Refresh stalls: merges leave the foreground, census splits large directories

A measured checkpoint, **not SPEC §9 acceptance**. CI's dense workload (2M rows in 11,764 session files) found `one_session_refresh` p95 at 6.9–29.6 s after the previous section's round and `no_change_index` 1.4–2.2× slower on every runner.

**Why refreshes stalled.** On a local dense corpus a refresh's own work stayed at 50–75 ms (commit about 1 ms, no WAL left between samples), but `index` ran `__work merge` inline after it. The 2M-row prose segment (108,096 pages) sits at level 1 after the first build, so the second refresh's carry rewrote it: 4.7–9.0 s in the sampled process, twice in 20 samples. Started detached instead, the same merge held `work.lock`, and `index` waited on it for the whole merge (4.6 s, its refresh 122 ms). The Windows sample whose commit took 10 s in CI was not reproduced on macOS, where the merge's own commits checkpointed its WAL; a one-turn commit that long points to the auto-checkpoint of frames another process left, which the merger now checkpoints itself.

- **Merges are background work.** `index` and `reindex` start a detached `__work merge` when segments need merging and none is merging. The merge holds `merge.lock`, not `work.lock`, so the other background tasks and later workers never wait for it.
- **A queued writer goes first.** Every blocking writer holds `writer.queue` shared while it waits; a merge step takes `writer.queue` exclusively before `writer.lock`, so it starts only when no writer is queued. flock and LockFileEx do not hand a released lock to a waiter, and a merger re-taking it in a loop could otherwise starve one. `index` and `reindex` hold their queue place for their whole run, so after the step in progress none of their writer locks waits for the merge. A detached refresh whose try-lock meets a merge step waits for that step instead of leaving the change pending.
- **Short steps.** FTS5 ends a step only between terms, so a common trigram's doclist is one step whatever the budget. Replaying the dense rewrite, the median step took 39.5 ms at 500 pages, 7.4 ms at 128 and 2.5 ms at 32, with the same 6 s total; at 64 pages the longest step of two background rewrites of that segment (304 steps, 4.6–5.0 s each) took 59–61 ms.
- **The merger checkpoints its WAL.** Its connection disables auto-checkpoint and runs a PASSIVE checkpoint whenever the WAL reaches 4 MiB and after each table, so merge frames never accumulate to the 4096-page auto-checkpoint of a foreground commit.
- **Unfinished merges resume.** The census also reports whether segments need merging, as after a worker killed mid-merge; a command whose sources are unchanged then starts a merger.
- **Large directories split.** The previous round stat'ed each directory's sources in that directory's walk task, so the dense store's one directory of 11,764 sources had one worker: its census took 38 ms against 22 ms before that round, 36 ms of it stats. A directory's sources are now stat'ed in chunks of 64 across the pool; smaller directories stay one task each.

On that corpus (this Mac, `nice 10`, load 4–7), with each binary on its own copy of one fresh build and fresh processes alternating, `one_session_refresh` p50 / p95 over 20 rounds was 67.8 / 74.7 ms before the previous round (3ea801ab), 91.2 / 4,490 ms at the previous head (0495e568) and 65.8 / 77.4 ms now; over 30 rounds against the pre-round binary alone, 69.2 / 77.5 against 71.7 / 106.9 ms, the p95 being the step in progress when a refresh arrived during one of the two background rewrites of the large segment. `no_change_index` went from 63.9 / 68.4 to 39.5 / 43.7 ms (pre-round 40.7 / 43.5), the dense `__census` from 47.5 / 51.2 to 26.3 / 31.5 ms (pre-round 29.9 / 33.6), and the census of the owner's history (11,435 sources in 2,849 directories) from 33.5 / 40.2 to 33.1 / 35.8 ms. Queries on the merged indexes took the previous head's time within 3% (`deepold --json -n 40` and its phrase, regex and project forms).

## Census: one walk per directory level, index fingerprints read meanwhile

A measured checkpoint, **not SPEC §9 acceptance**. Timed from exec to exit on the owner's live history (`AGREP_QUERY_TIMING`, 40 fresh `error` processes while the stores changed at their ordinary rate): exec to `main` 7 ms, the keyword search ended at 24–26 ms, and the census thread at 31 ms (read 2, cached listings and directory stats 8, 11,432 source stats 14, sort and comparison 6.4), so the command waited 4.5–5.5 ms for it. Fast commands are census-bound: `around`, `chats` and `rare` spend 18–27 ms more with the census than with `--no-auto`. Spawning the detached worker cost 0.3 ms (lock 0.03, log 0.02, `posix_spawn` 0.25), and a command running beside a six-hour refresh took 39–48 ms against 40 ms on a quiet index, so neither was the critical path.

- **One walk.** Each directory's sources are stat'ed in the same parallel task that confirmed (or relisted) the directory, instead of after the whole tree, which removes a separate pass and the sequential path building between them. (A large directory's stats now split across the pool; see the section above.)
- **Overlap.** The index's fingerprints (11,432 `sources` rows, about 3 ms) are read on the stat pool while the walk runs, and the stamps are sorted in parallel.
- **Same answer.** Every source and directory is still stat'ed; a new test checks, after each of an append, a same-bytes rewrite, a new file, a new directory, a removed file, a removed directory and a non-source file, that the census counts exactly the sources whose fingerprint differs from a fresh full build's (it also passes on the previous census).

In one process on the same index (load 4–9; [evidence](measurements/census-eng3.json)) the census went from 24–28.5 ms to 20–22.6 ms: cached listings 2.4–2.6 ms, walk 15–17 ms, comparison 2.2–2.8 ms. A bare parallel `lstat` of the same 13,990 paths takes 10–11.7 ms with 8 workers (8.1 ms with 4, 11.9 ms with 12; 32–38 ms sequentially), so about 5 ms of the walk is above the syscall floor. Rejected: `fstatat` relative to each opened directory (21–24 ms for the same paths), four walk workers (20–32 ms, noisier), and starting the worker from the census thread (count and status 3–5% slower in a twelve-pair run, since the refresh then runs beside the rest of the query).

Sixteen ABBA pairs of fresh processes on the live history, both binaries on one index with normal census and detached refresh (median load 4 / 5 / 6); outputs identical:

| Case | Previous head (82868206) p50 / p95 ms | This change p50 / p95 ms | Head ÷ new |
|---|---:|---:|---:|
| `agrep __census` | 35.7 / 38.0 | 32.1 / 34.1 | 1.11× |
| `error` | 40.1 / 84.2 | 35.7 / 329.7 | 1.12× |
| `around <first session> 0` | 36.8 / 39.1 | 32.9 / 34.8 | 1.12× |
| `chats` | 45.2 / 52.6 | 41.3 / 43.5 | 1.09× |
| `zzperfnever_7f91c44` | 36.3 / 39.8 | 31.8 / 36.2 | 1.14× |
| `-E release.*workflow` | 39.0 / 45.8 | 35.8 / 38.0 | 1.09× |
| `error -c` | 51.9 / 66.3 | 52.7 / 68.5 | 0.98× |
| `status` | 43.9 / 47.0 | 44.9 / 48.3 | 0.98× |
| `release workflow` | 56.3 / 60.2 | 55.7 / 60.1 | 1.01× |

Query-bound cases are unchanged (the census already ended before their query). Both `error` p95s are single samples of sixteen; their cause was not isolated. Remaining: the stat syscalls themselves (10–11 ms with 8 workers) and process start (7 ms, about 1.5 ms of it the system frameworks); an exact census below that needs an OS change journal.

## FTS segments: one per refresh, merged by the detached worker

A measured checkpoint, **not SPEC §9 acceptance**. FTS5 writes its pending terms into a new segment whenever a write's rowid is not above the previous write's and at every statement savepoint, which each row statement with a trigger opens. Refresh interleaved FTS writes with such statements (row upserts, visibility changes, the dictionary's trigger inserts), so it added about one segment per written row; FTS5's own merges (`automerge=4`, `crisismerge=16`) ran inside refreshes and never caught up. Over a six-hour replay of the owner's history the previous head held 32 prose, 102 tool and 13 lexicon segments on average (at most 49 / 143 / 21). Lookups seek every segment for each term, and tool queries look up hundreds of expanded words, so after that replay its queries ran 2.5–8× slower than the same index merged to one segment per table.

### What changed

- **One segment per refresh.** Row writes record each touched FTS row's state at the start and end of the transaction; after the canonical replay the net change is written once, in rowid order (delete if it was indexed, then insert the row's stored text if it is), with no other statement in between. New dictionary words are inserted in one statement, so their lexicon rows share one segment too, and the scratch word batch is discarded with `delete-all` before any savepoint, so it never becomes a segment. `hashsize` is 8 MiB after the first build: in the replay no refresh added more than one segment to any table, including the largest step (14.8 MB appended to 68 sources).
- **Refreshes never merge** (`automerge=0`).
- **`__work merge`** runs after `refresh` in a detached worker, which an explicit `index` or `reindex` starts instead of merging itself (see the section above). With `usermerge=2`, FTS5 `merge` steps merge any level holding two segments into the next, so each refresh's segment carries upward like a binary counter and the large segment is rewritten only when a carry reaches its level. The first build leaves that segment at level 1, so early carries rewrite it often and then ever less often, each rewrite raising its level (in the replay below: after steps 1, 5, 9 and 21 for prose and 1, 8, 16 and 34 for tools). When more than 5 segments remain, the table is merged into one with negative `merge` steps (in the replay: tools once, the lexicon once). Every step is its own transaction under the writer lock: readers never wait.

A test fails on the previous head (a fixture refresh took the tables from 1 / 1 / 1 to 13 / 13 / 1 segments): every refresh adds at most one segment per table, the refreshed index's vocabularies and dictionary equal a fresh build's, and `__work merge` leaves at most 5 segments with the same contents.

### Choosing the cap

The cost of segments was measured on copies of a replay index that only accumulated segments (one binary, `--no-auto`, each copy against itself merged to one segment per table; [evidence](measurements/fts-segments-eng3.json)): with 3 segments per table `json` / `count` / `tool_release` took 1.05 / 1.18 / 1.13× the merged time, with 5 segments 1.13 / 1.28 / 1.20×, with 9 segments 1.24 / 1.45 / 1.38×. A large-segment rewrite costs 2.9 s for prose, 5.2 s for tools and 0.24 s for the lexicon. Replaying which refreshes added a segment to each table 40 times over (2,920 refreshes) through the level merges and a cap:

| Cap (segments per table) | Merge work | Large rewrites | Mean / max segments |
|---:|---:|---:|---|
| 1 (merge everything every refresh) | 22,622 s | 8,080 | 1.00 / 1 |
| 3 | 3,239 s | 1,155 | 2.29 / 3 |
| 4 | 1,527 s | 542 | 2.86 / 4 |
| **5** | **758 s** | **266** | **3.41 / 5** |
| 6 | 400 s | 136 | 3.94 / 6 |
| none | 103 s | 26 | 5.52 / 11 |

Each step up halves the merge work and adds about half a segment. At 5, merge work is about 0.26 s per refresh, roughly what the refresh itself saves (below), so total background work stays where it was while every table keeps at most 5 segments, about 1.1× the merged lookup cost on average; 4 doubles the merge work for half a segment.

### Replay of the owner's history

An APFS clone of the owner's stores, with the 135 sources written in its last six hours cut back to six hours earlier and appended back by their line timestamps in 73 steps of 5 minutes (102 MB). Each binary built its own index of the cut state, then refreshed after every step, alternating order; this change also ran `__work merge`. Every step, exit and load is in the [report](measurements/fts-segments-eng3.json); median load 6 / 10 / 10.

| | Previous head (1a1e0bcc) | This change |
|---|---:|---:|
| Refresh p50 / p95 | 2,392 / 4,179 ms | 2,108 / 3,647 ms |
| Refresh total (72 refreshes) | 184.0 s | 159.6 s (faster in 69 of 72) |
| Merge total | (inside refresh) | 28.8 s; p50 17 ms, 8 steps over 1 s |
| Prose / tool / lexicon segments, mean | 32.0 / 102.4 / 13.0 | 3.1 / 3.1 / 3.3 |
| Prose / tool / lexicon segments, max | 49 / 143 / 21 | 5 / 5 / 5 |

The 28.8 s include the early carries into the large segments. The previous head's index was also 285 MB larger at the end (2.78 against 2.49 GB).

### Queries after the replay

Ten ABBA pairs of fresh processes per case, each binary on its own replayed index, `--no-auto`. With normal census both pay the same 37 ms census, so ratios are slightly lower (`error -c` 6.53×, `release workflow` 3.83×, `recall` 2.37×); both runs are in the report. Outputs are identical except `status`, which reports each index's path, refresh time and size.

| Case | Previous head p50 / p95 ms | This change p50 / p95 ms | Head ÷ new |
|---|---:|---:|---:|
| `release workflow` | 226.0 / 303.3 | 53.3 / 145.0 | 4.24× |
| natural question | 210.7 / 259.1 | 58.3 / 146.3 | 3.62× |
| `release workflow --json` | 219.8 / 229.0 | 50.6 / 52.9 | 4.35× |
| `release workflow -l` | 354.0 / 391.8 | 90.3 / 124.6 | 3.92× |
| `error -c` | 344.6 / 348.8 | 44.0 / 46.3 | 7.83× |
| `error --who tool --json` | 235.5 / 264.3 | 80.5 / 118.6 | 2.93× |
| `release workflow --who tool` | 334.4 / 337.1 | 57.9 / 59.3 | 5.77× |
| `recall release workflow` | 429.6 / 469.4 | 174.0 / 214.1 | 2.47× |
| `pack …` | 128.2 / 130.0 | 102.2 / 104.0 | 1.25× |
| `error --chat <largest> -c` | 451.3 / 649.6 | 152.0 / 563.6 | 2.97× |
| `error -c --agent codex --since 14d` | 439.2 / 449.6 | 143.1 / 147.8 | 3.07× |
| pi caller: `error -c` | 461.5 / 467.8 | 162.2 / 170.1 | 2.85× |
| Codex caller: `release workflow --json` | 227.9 / 245.6 | 57.8 / 63.5 | 3.95× |
| `error` | 29.8 / 42.5 | 28.3 / 95.7 | 1.05× |
| `hi` | 73.5 / 210.9 | 73.4 / 264.7 | 1.00× |
| `chats` / `around` / `status` | 28.6 / 11.5 / 36.9 | 28.4 / 11.4 / 37.5 | ≈1.0× |

Cases with one short or common term barely touch the text index; their p95 outliers are single samples at load 6–8.

## Caller exclusion: indexed probes and a seeking session walk

A measured checkpoint, **not SPEC §9 acceptance**. Inside an agent shell every search resolves the caller and counts the rows it withholds. Three costs dominated, measured in-process on the owner's history:

- **Live side chats** were collected with a recursive walk that, at each step, scanned all of the agent's sessions: 72–146 ms per command for pi callers with large families. Each step now seeks the children of one session through `sessions_parent`: 1.5–3 ms, same members.
- **Window probes** counted the caller's window as a `--chat <session>` scope from the window's oldest timestamp, then subtracted earlier turns of that range. Without table statistics the planner read such a scope through the agent/time index, every pi row since the window opened. `query::Filters` now has `session_rows` (an exact session and a first position), read through `prose_session` and `event_position`, so the window is counted directly.
- **The "matched earlier" probe** keeps 0.4.9's `--chat` prefix scope. Any session scope (`--chat` or `session_rows`) now keeps agent, time and project predicates off the access path with a unary plus, so it is read through the session indexes (131–148 ms → 4–17 ms for that probe; `--chat` user queries with `--agent`/`--since` benefit the same way). The probes of one count share one plan cache (word expansions, postings, term weights), since they search one pattern over different scopes.

Probe counts follow 0.4.9's definition (each probe a `--chat <session>` query, then the session's withheld rows): an integration test compares every count against exactly that, over a window whose earlier turns are newer than its first ones, tool rows, side chats, `--no-self`, `--no-meta` and related-term fallback; another fails if probe work grows with other sessions' rows. One corner changed: for a caller with no recap whose session id another id extends, a related-term count stood in when the extending session held the exact phrase only in rows older than the caller's first row; it is now 0, as in 0.4.9 (the test covers it). With one interposed wall-clock instant, stdout, stderr and exit codes of the previous head (0415e32d) and this binary match on **400 of 400** caller cases ([report](measurements/caller-exclusion-eng2.json)), 172 with a nonzero withheld count.

Eight ABBA pairs per case, normal census and detached refresh, run as a caller (`AGREP_PI_SESSION_ID` / `CODEX_THREAD_ID`). The owner's stores had changed for an hour, so every command also paid a full census and refresh spawn (the unchanged `error` control is 145 ms here, against 41 ms on a quiet index); both binaries saw the same.

| Case | Previous head p50 / p95 ms | This change p50 / p95 ms | Head ÷ new | Load 1 / 5 / 15 |
|---|---:|---:|---:|---|
| pi caller (window, 4 sessions): `error` | 125.4 / 131.0 | 48.9 / 67.4 | 2.56× | 4 / 7 / 9 |
| pi caller: `release workflow --json` | 162.4 / 165.0 | 85.0 / 87.1 | 1.91× | 4 / 7 / 9 |
| pi caller, compact: natural question | 352.0 / 363.4 | 123.8 / 127.8 | 2.84× | 4 / 7 / 9 |
| pi caller, compact: `flaky windows cache` | 284.3 / 396.7 | 57.0 / 163.1 | 4.99× | 4 / 7 / 9 |
| pi caller: `error --who tool --json` | 287.6 / 320.7 | 212.9 / 231.5 | 1.35× | 4 / 7 / 9 |
| pi caller: `error -c` | 385.3 / 404.0 | 303.5 / 324.3 | 1.27× | 4 / 7 / 9 |
| codex caller: `error` | 161.1 / 176.8 | 154.4 / 178.9 | 1.04× | 4 / 7 / 9 |
| codex caller: `release workflow --json` | 181.8 / 202.4 | 186.3 / 203.0 | 0.98× | 4 / 7 / 9 |
| codex caller, compact: natural question | 183.9 / 189.3 | 184.2 / 196.5 | 1.00× | 4 / 7 / 9 |
| codex caller, compact: `flaky windows cache` | 159.7 / 171.8 | 153.0 / 172.7 | 1.04× | 4 / 7 / 9 |
| codex caller: `error --who tool --json` | 225.7 / 246.3 | 220.8 / 240.7 | 1.02× | 4 / 7 / 9 |
| largest pi session as caller: `error` | 286.5 / 319.5 | 151.0 / 167.9 | 1.90× | 4 / 7 / 9 |
| largest pi session, compact: natural question | 321.3 / 341.1 | 176.6 / 197.6 | 1.82× | 4 / 7 / 9 |
| `error`, no caller (unchanged code) | 145.9 / 171.9 | 144.6 / 164.6 | 1.01× | 4 / 7 / 9 |

The Codex caller's probe was already 8–15 ms (its window is one small session); the largest pi session has more than eight live side chats, so it gains only the resolution. Remaining: a caller-filtered `-c` joins every corpus-wide match to the row table (about 150 ms over the unfiltered count); the probes could reuse the main search's plans.

## `--chat` scopes read their sessions through the session indexes

A measured checkpoint, **not SPEC §9 acceptance**. `--chat` filtered rows with `session LIKE 'prefix%'`. A case-insensitive LIKE cannot use the session indexes, so the bundled SQLite (3.46.0) answered the scope probe by walking the tool lane's stored-text index, every active event row, and by scanning the whole prose table: 361 ms and 43 ms for the largest session's scope on the owner's history, repeated by every pass. The prefix is now matched once, with the same LIKE, on the 8,640-row `sessions` table, and rows are filtered with `session IN (SELECT value FROM json_each(?))`, which the planner reads through `prose_session` and `event_sessions_session` → `event_position` (6.6 ms and 1.0 ms). The store writes a `sessions` row for every row's session and deletes it only when no row remains, so the same rows match. A plan test fails on the LIKE path; an integration test compares prefix scopes (ids that extend each other, differ in case or hold LIKE wildcards) with the unscoped rows in both lanes, as pages and counts.

With one interposed wall-clock instant, stdout, stderr and exit codes of the previous head (5fdd762a) and this binary match on **425 of 432** real-history cases (18 queries over the newest Codex session, the largest session and its tool lane; text, JSON, `-l`, `-c`, `-n 0`, `--sort time`, `-n 5`, compact). The other 7 differ only in the floor count of that tool lane (`N+ matches (floor)`, JSON `completeness.total` with basis `floor`): the old plan read the lane's rows in stored-text order and stopped counting earlier, the new one reads them in id order, and each new floor equals the exact `-c` count. Rows, order, scores, snippets, handles and exit codes are identical.

Ten ABBA-interleaved fresh-process pairs per case on one isolated index of the owner's history, normal census and detached refresh; every sample and load is in the [report](measurements/chat-scope-eng2.json).

| Case | Previous head p50 / p95 ms | This change p50 / p95 ms | Head ÷ new | Load 1 / 5 / 15 |
|---|---:|---:|---:|---|
| `error --chat <largest> --who tool --json` | 818.9 / 1013.1 | 120.6 / 153.5 | 6.79× | 9 / 9 / 9 |
| `error --chat <largest>` | 814.8 / 1460.0 | 90.5 / 106.7 | 9.00× | 10 / 9 / 9 |
| `release workflow --chat <largest>` | 1141.1 / 1219.3 | 82.7 / 93.5 | 13.80× | 10 / 9 / 9 |
| `error --chat <largest> -c` | 337.8 / 361.8 | 222.0 / 231.9 | 1.52× | 9 / 9 / 9 |
| `error --chat <newest codex>` | 759.0 / 812.8 | 72.0 / 126.0 | 10.55× | 9 / 9 / 9 |
| `release workflow --chat <newest codex> --who tool` | 692.9 / 724.5 | 42.6 / 65.9 | 16.27× | 9 / 9 / 9 |
| `error` (no `--chat`, unchanged code) | 40.7 / 49.2 | 41.8 / 96.4 | 0.97× | 9 / 9 / 9 |

The `-c` case still counts through the prose index's corpus-wide match stream, joined to the scope; the unscoped control runs identical code and its p95 is one slow sample.

## Windowed ranking at the previous head's speed

A measured checkpoint, **not SPEC §9 acceptance**. Scoring keyword hits inside 0.4.9's snippet window (b23d2727) lowered page scores, so more blocks cleared their ceilings and more tool lanes ran; against the previous head (084e77af) it was 0.43–0.88× on release, regex, json, natural, compact_release, tool_error and recall ([before and after](measurements/window-speed-eng.json)). Profiles: `tool_error` decoded 42,012 tool rows to find 3,847 holding the term (57% of samples in decompression); release spent 62% of its samples in per-block FTS queries, each seeking every word in each of the 13 tool segments; regex read 200 prose and 95 tool blocks. Windowed gram counts would not help: replayed on the owner's newest tool blocks, they would drop 4 of 104 blocks for `error` and 1 of 177 for `release workflow`.

### What changed

- **Run admission.** A bounded scan no longer queries the text index once per block. When it reaches a block, one rowid-range query admits the rows of up to 64 blocks the page still admits, using the words those blocks hold; visited, excluded and ceiling-less blocks between them (up to 8 in a row) are stepped over. Each word's postings are opened once per run instead of once per block. Tool blocks of a common single term are admitted the same way instead of decoding every row.
- **Regular expressions** require their suffix literal as well as their prefix literal. A pattern `A.*B` with `A` never empty and `.*` greedy and unbounded matches a snippet at most once (its first match's `.*` runs to the last place `B` matches, and snippets hold no line break), so its blocks are bounded with one match.
- **Smaller costs.** Candidate ids bind as one JSON list, so one statement stays prepared. Tool candidates read their text by blob id instead of through the `tools` view. Each thread keeps one inflate state and decodes into uninitialized capacity. Session ids are sorted in Rust instead of by `SELECT DISTINCT`, and a command loads them once for all its hit handles.

Results are unchanged except one disclosed number: with one interposed wall-clock instant, stdout, stderr and exit codes match the merged previous commit byte for byte on **1,105 of 1,116** real-history cases (18 queries, 14 scopes, text, JSON, `-l`, `-c`, `-n 0`, `--sort time`, `-n 5`, compact). The 11 others are `-E release.*workflow`, whose floor count (`N+ matches (floor)`, JSON `completeness.total` with basis `floor`) is smaller because its scan stops earlier; rows, order, scores, snippets, handles and exit codes are identical. The 0.4.9 ranking fixture, the exhaustive and randomized bounded-page tests and the rest of the suite pass; unit tests cover literal extraction and the single-match analysis.

Eight ABBA-interleaved fresh-process pairs per case on one isolated index of the owner's history, normal census and detached refresh; every sample and load is in the [report](measurements/window-speed-eng.json).

| Case | Previous head p50 / p95 ms | This change p50 / p95 ms | Head ÷ new | Load 1 / 5 / 15 |
|---|---:|---:|---:|---|
| release | 91.1 / 118.7 | 76.9 / 92.3 | 1.18× | 21 / 12 / 11 |
| error | 41.9 / 45.9 | 42.9 / 46.2 | 0.98× | 21 / 12 / 11 |
| natural | 102.3 / 110.2 | 71.5 / 73.5 | 1.43× | 21 / 12 / 11 |
| flaky | 44.7 / 47.1 | 42.2 / 45.8 | 1.06× | 21 / 12 / 11 |
| short | 173.4 / 200.5 | 74.4 / 79.5 | 2.33× | 19 / 12 / 11 |
| regex | 48.8 / 225.2 | 42.1 / 84.4 | 1.16× | 19 / 12 / 11 |
| list | 240.2 / 256.3 | 149.4 / 184.2 | 1.61× | 19 / 12 / 11 |
| json | 118.6 / 137.0 | 103.5 / 112.5 | 1.15× | 18 / 12 / 11 |
| rare_hit | 44.4 / 131.2 | 44.7 / 48.1 | 0.99× | 18 / 12 / 11 |
| compact_release | 98.7 / 145.1 | 83.9 / 97.2 | 1.18× | 18 / 12 / 11 |
| compact_natural | 131.3 / 143.2 | 95.1 / 102.0 | 1.38× | 17 / 11 / 11 |
| tool_error | 144.9 / 159.3 | 127.4 / 141.8 | 1.14× | 17 / 11 / 11 |
| recall | 292.7 / 327.2 | 253.4 / 298.5 | 1.16× | 16 / 11 / 11 |
| pack | 195.7 / 224.5 | 169.7 / 206.7 | 1.15× | 15 / 11 / 11 |

`error` and `rare_hit` run the plans they ran before (a common prose term with no tool lane, a selective term); their medians differ by 1 ms, within the spread of their samples.

### Where the remaining time goes

- **Run admission** is the largest remaining query cost (a third of tool_error's samples): FTS5 seeks each word in each of the 13–15 segments that incremental refreshes leave behind.
- **tool_error** still decodes about 3,800 rows; **recall** spends a quarter of its samples loading tool events for windows.
- `--chat` filters and the caller-exclusion probes still match sessions with `LIKE`, which cannot use the session indexes.

## Dense first build: parallel family statistics

A measured checkpoint, **not SPEC §9 acceptance**. The dense 2M first index was the matched run's only dense loss (0.4.9 114.8 s, 0.5 187.9 s, at loads 34–105). Its profile spent about 37 s in family statistics, 11 s in FTS segments forced by 64-row statements, and 7 s each in secondary indexes and parser graph pages. Derived data is unchanged.

### What changed

- **Family statistics in parallel.** The first build tallied per-family token counts on one thread, fed by a query that read every row through the time index and sorted the result by family, and each row built two sets of owned strings. One id-order pass now assigns visible rows to families; each family's rows are read in runs of consecutive ids and tallied on the pool, every token going straight into its family's count, once per row and column. Tokens of up to four bytes, nearly all of them, are packed into integer keys. `family_stats`, written on every build and read by nothing, is removed.
- **One row per FTS statement.** A multi-row INSERT opens a statement savepoint, and FTS5 flushes its pending terms into a new segment at every savepoint, so each 64-row statement wrote a segment for the merges to rewrite. Single-row statements leave flushing to the 16 MiB pending-term limit.
- **Parser graph pages.** A replaced source no longer reads back the graph pages it has just deleted, and pages are packed in parallel batches of about 8,192 nodes instead of one pool round trip per source. Packing on the pool while the writer went on was measured and dropped: on the live history it competed with the writer, and graph time rose from 18–26 s to 31–36 s (`rejected_variant` in the [report](measurements/first-build-eng.json)).

Every derived table is identical between the previous head (951fc4cd) and this binary on the dense corpus and on a frozen clone of the owner's history: prose rows, sessions, row and term statistics, family token counts (decoded), `prose_terms`, both FTS vocabularies, parser graph pages byte for byte, block bounds and tool words. A unit test compares the new tally with the original per-row token sets across chunk boundaries, interleaved and contiguous families, orphan and invisible rows and Unicode text, then refreshes incrementally from the new bodies.

### First builds, paired

ABBA pairs of fresh processes into empty data directories; every sample and load is in the [report](measurements/first-build-eng.json). The real-history pairs indexed one APFS clone of the owner's Claude, Codex and OMP stores, so both binaries read identical inputs.

| Corpus | Previous head s | This binary s | Load 1 min |
|---|---:|---:|---|
| dense 2M | 83.5, 79.9 | 43.2, 45.3 | 12–16 |
| real history (frozen clone) | 179.4, 174.4 | 154.7, 169.8 | 12–20 |

| Phase s, previous head → this binary | Dense 2M | Real history |
|---|---|---|
| statistics | 34.0, 32.1 → 5.7, 6.2 | 6.7, 6.5 → 3.3, 3.5 |
| FTS | 18.2, 18.5 → 10.4, 11.7 | 38.3, 36.3 → 28.9, 29.9 |
| parser graph pages | 6.2, 5.6 → 3.5, 4.9 | 27.5, 27.1 → 15.9, 26.2 |
| secondary indexes | 5.7, 6.0 → 5.6, 5.4 | 3.6, 3.2 → 3.4, 3.6 |

### Matched against 0.4.9

`matched --workload dense --only first_index,old_dense,old_dense_project,old_dense_count,version_control`, a diagnostic subset, so its gate lists the other cases as missing. 0.4.9 and this binary alternated on one generated 2M corpus with temporary data directories ([report](measurements/matched-dense-2000000-eng.json)):

| Case | 0.4.9 p50 / p95 ms | This binary p50 / p95 ms | Load 1 min |
|---|---:|---:|---|
| first_index | 62,874 / 72,491 | 42,718 / 45,923 | 9–16 |
| old_dense | 6,462 / 6,945 | 63.0 / 75.6 | 11–13 |
| old_dense_project | 1,297 / 1,992 | 103.4 / 119.1 | 11 |
| old_dense_count | 631 / 1,523 | 142.8 / 258.7 | 21–32 |
| version_control | 44.0 / 72.9 | 10.0 / 13.8 | 31 |

0.4.9's first index took 62.9 s at load 11–16 here, against 114.8 s at loads 34–105 in the earlier matched run.

### Where the remaining time goes

- **Dense**, profiled at load about 100 (durations inflated): FTS 10 s (trigram tokenizing about 3 s, docsize rows 1.7 s, segment flushes 1.4 s, the final merge 3.3 s); row writes 8 s, about 3 s of it reply shapes and 2.3 s prose inserts; five secondary indexes 5 s; the semantic queue filled after the build 2.7 s; `prose_terms` read from the FTS vocabulary 2.3 s; family statistics 2.2 s, half of it the `row_stats` join. SQLite's sorter worker threads (`PRAGMA threads=4`, with the build's or a smaller cache) built the five indexes in 6.0–7.0 s against 6.1 s without, so they stay off.
- **Real history**: parsing 26–30 s, row writes 31–35 s, FTS 29–30 s, graph pages 16–26 s, the tool dictionary 14 s.

## Selective metadata scopes read their text index

A measured checkpoint, **not SPEC §9 acceptance**. A scope at most the selectivity limit (`--agent codex --since 14d`: 581 prose and 9,736 tool rows) was scanned by decoding and scoring every row, once per tier: 20,634 decoded rows and 130–280 ms of hydration per query, the cause of the four filtered real-history losses. The scan now keeps its order, ceilings and floor marking, so the same rows fill the page and the same totals and bases result, but a row is decoded only if the lane's text index admits it. The admission sets use the expressions unselective plans already rely on as necessary conditions (positional word plans for phrases, the two rarest groups otherwise), with one rowid-range query per run of consecutive scope blocks; a run under 32 scope rows is decoded directly. The scope probe now returns its per-block row counts from SQL. On that scope, flaky decodes 26 rows instead of 20,634 and its retrieval fell from 170 to 24 ms.

Results are unchanged: stdout, stderr and exit codes of the previous head (a2aebecb) and this binary are byte-identical on **682 of 682** real-history cases with one interposed wall-clock instant, except the random saved-page handle in compact stderr (11 queries across 14 scopes, in text, JSON, `-l`, `-c`, `-n 0`, `--sort time`, `-n 5` and compact). A new integration test fails before the change: on a 3,136-row scope it requires pages identical to the unscoped and exhaustive rankings, exact totals when the page is not full, and fewer than 512 decoded rows.

Twenty ABBA-interleaved fresh-process pairs per case on one isolated index of the owner's history, normal census and detached refresh. 0.4.9 numbers are from the matched run, at different loads; a matched rerun against 0.4.9 under the same load is still needed. [Every sample and load](measurements/selective-scope-eng.json), including a first campaign at load 13–18 (flaky 233.3 → 63.6 ms, natural 415.1 → 132.4, list 226.3 → 84.9, rare_hit 226.9 → 73.5) with a per-block variant of the same change.

| Case | 0.4.9 matched p50 / p95 ms | Previous head p50 / p95 ms | This binary p50 / p95 ms | Head ÷ new | Load 1 / 5 / 15 |
|---|---:|---:|---:|---:|---|
| natural_filtered | 174 / 267 | 269 / 324 | 100 / 188 | 2.69× | 50 / 86 / 78 |
| flaky_filtered | 122 / 170 | 300 / 361 | 166 / 241 | 1.81× | 47 / 84 / 77 |
| list_filtered | 268 / 424 | 314 / 372 | 133 / 180 | 2.35× | 45 / 83 / 77 |
| rare_hit_filtered | 246 / 2116 | 285 / 326 | 118 / 162 | 2.41× | 44 / 81 / 76 |
| release_filtered | 213 / 318 | 198 / 244 | 131 / 187 | 1.52× | 42 / 80 / 76 |
| rare_filtered | 288 / 379 | 289 / 386 | 118 / 163 | 2.45× | 38 / 77 / 75 |
| regex_filtered | 273 / 1116 | 190 / 224 | 148 / 173 | 1.28× | 36 / 75 / 74 |
| json_filtered | 442 / 825 | 195 / 220 | 96 / 138 | 2.02× | 35 / 74 / 74 |

error, short, count and rare_hit_pi filtered run unchanged plans (a common term, a two-character term, counts, an unselective scope) and stayed within noise. After this change the census (40–90 ms under load, concurrent) is the critical path of these commands; in-process, the natural query still spends about 10 ms planning (tool expansions, the phrase regex) and 13 ms in SQLite.

## V9: recall and pack windows and depth

A measured checkpoint, **not SPEC §9 acceptance**. The previous head (ffc57df4) and this binary ran on one isolated index of the owner's history, built read-only by the previous head, in ABBA-interleaved pairs of fresh processes with normal census and detached refresh, so both saw the same load. The machine was far busier than for V8 (median loads 60–113). Every sample and load is kept: [report](measurements/recall-pack-w3.json).

### What changed

- **One sorted session index.** A window resolved its session by reading every session row with its title and family (8–14 ms each, eight times per recall), then read every id again for its shortest unique target. `SessionIndex` reads the distinct ids once per batch; exact and prefix references resolve by binary search, and the target comes from the two sorted neighbors, whose common prefix is the longest any id shares. Other references still read every session. Search hit handles use the same index, and the handle pattern compiles once per process.
- **Incremental budget trimming.** Recall rebuilt every block for each dropped context row (33 full renders of about 450 KB for `release workflow`). Each row's line now depends only on the cap, so a drop re-renders nothing, a smaller cap renders each remaining row once, and the page length is a sum. JSON pages serialize only the block a drop changed.
- **Shallow rankings for selection.** Recall searched 12 hits per wanted hit (96) to pick one per family, and search cost grows with depth: `release workflow` took 11 ms at 16 hits and 342 ms at 96 in one process. Without a meaning lane or `--probe`, each query is first searched twice the target deep; a ranking prefix selects exactly what the full ranking selects unless selection ran past its end, and only such a query is searched again at full depth.
- **Tool text** is split at character offsets by slicing instead of collecting characters, and stored counts are not recomputed.

Results are unchanged: with one interposed wall-clock instant (recency scores read the clock), stdout, stderr and exit codes are byte-identical on **315 of 315** cases: recall and pack in text and JSON at budgets from 64 bytes to unbounded, context 0 and 5, `--hits`, `--probe`, colour, tool and filtered scopes, direct handles, `around` by handle, session, prefix and title, `--chat` searches and `chats`. Tests compare the incremental pages with full re-rendering at every budget, deepened selection with full rankings over random families and depths, sorted targets and candidates with a scan of every session, and offset splitting with collected characters.

| Case | 0.4.9 ms | Previous head ms | This binary ms | Head ÷ new | 0.4.9 ÷ new | Load 1 / 5 / 15 |
|---|---:|---:|---:|---:|---:|---|
| recall | 849.74 | 735.3 | 351.4 | 2.09× | 2.42× | 113 / 96 / 78 |
| pack | 1057.00 | 782.2 | 284.9 | 2.75× | 3.71× | 105 / 95 / 78 |
| around | 329.71 | 118.0 | 134.8 | 0.88× | 2.45× | 100 / 94 / 78 |
| release | 457.09 | 187.0 | 223.4 | 0.84× | 2.05× | 98 / 93 / 78 |
| json | 505.40 | 228.6 | 240.9 | 0.95× | 2.10× | 99 / 94 / 78 |
| error | 347.78 | 85.5 | 96.4 | 0.89× | 3.61× | 99 / 94 / 78 |
| chats | 287.44 | 176.4 | 193.2 | 0.91× | 1.49× | 99 / 94 / 78 |

Seven pairs per case. The searches, `around` and `chats` change only in how handles find their targets, and at load 100 their seven-pair medians sit within the spread of the samples. Fifteen further pairs at load 61–62 measured around 97.3 → 93.0, error 124.8 → 111.3 and release 218.2 → 209.6 ms; all samples are in the report.

### Where the remaining time goes

- **recall / pack**: the shallow searches; event decoding for windows with thousands of tool calls (about 30 ms of decompression, 15 ms of JSON parsing and 10 ms of hashing across 4 MB in one profile); the concurrent census. Decoding events in parallel was measured and dropped: in its campaign at load 85, `around` took 131.3 ms against the previous head's 90.0 (samples in the report).
- **short** (`hi`) and the fixed startup and census floor of error, count, chats, status and around are unchanged from V8.

## V8: exact tool word presence

A measured checkpoint, **not SPEC §9 acceptance**. The machine was far busier than for V7 (median loads about 40 / 48 / 45 against 14 / 26 / 27), so the comparison below pairs adjacent seven-process campaigns of the previous head and this binary under the same conditions instead of comparing with V7's quieter numbers. Every sample and its load is kept: [previous head](measurements/real-query-p8-head.json), [this binary, run A](measurements/real-query-p8-v2.json), [run B](measurements/real-query-p8-v2b.json).

### What changed

- **Tool word presence.** Each dictionary word now records the 512-row tool blocks containing it (`tool_words.blocks`), computed with the FTS5 unicode61 tokenizer itself, so it lists exactly the tokens FTS5 stores. Removed rows may leave a block listed; a block is never omitted. Block bounds drop tool blocks in which a fully expandable term has no word, and each block's FTS expression lists only the words that block holds.
- **No corpus-wide stream of broad expansions.** FTS5 advances every OR child on each step. `release workflow` expands to 1,741 tool words; its single corpus-wide positional stream alone cost 171–272 ms. Expressions above 32 words now use the bounded per-block plan, whose expressions average 10–27 words.
- **Three-character terms plan without expansion.** `tool_trigrams` keeps, per trigram, the summed frequency of the words containing it, equal to the expansion's total. Words are read only for the groups a plan uses, so `the`, `run` and `did` (8,149 words) are no longer read for the natural query.
- **Presence, not counts, in the matcher.** Outside the phrase tier, which counts its own regex matches, a term's occurrence count was never used; `contains` replaces counting every occurrence.

Results are unchanged: the previous head and this binary return identical pages, order, totals, total basis and exit codes on **62 of 62** real-history cases on one index ([equivalence](measurements/equivalence-p8.json); scores compared within 1e-5 for the process clock). A new integration test compares bounded pages with the exhaustive `-n 0` ranking across blocks with differing words, Unicode separators and an incremental append, and fails when one block loses one word. Unit tests check stored presence against FTS5 for every word, its growth after changes, and trigram totals against expansions.

### Real history, paired campaigns

| Case | 0.4.9 ms | V7 ms | Previous head now | This binary A / B | Head ÷ best |
|---|---:|---:|---:|---:|---:|
| release | 457.09 | 186.01 | 575.3 | 93.7 / 119.8 | 6.14× |
| natural | 270.82 | 206.91 | 562.8 | 104.1 / 154.7 | 5.41× |
| list | 862.46 | 326.63 | 1024.3 | 248.7 / 495.7 | 4.12× |
| json | 505.40 | 182.46 | 454.7 | 121.7 / 141.6 | 3.74× |
| compact_release | 508.09 | 193.39 | 392.7 | 112.8 / 132.3 | 3.48× |
| compact_natural | 362.40 | 210.78 | 458.2 | 175.8 / 180.3 | 2.61× |
| short | 408.58 | 145.61 | 283.6 | 238.0 / 247.5 | 1.19× |
| error | 347.78 | 43.74 | 130.7 | 70.1 / 68.5 | 1.91× |
| count | 462.77 | 57.00 | 149.7 | 119.1 / 127.2 | 1.26× |
| recall | 849.74 | 465.86 | 1000.9 | 788.2 / 453.4 | 2.21× |
| pack | 1057.00 | 408.75 | 692.9 | 852.4 / 408.0 | 1.70× |
| chats | 287.44 | 60.11 | 126.2 | 79.6 / 86.0 | 1.59× |
| status | 568.60 | 55.02 | 86.7 | 110.7 / 89.6 | 0.97× |
| around | 329.71 | 50.02 | 63.3 | 71.1 / 65.3 | 0.97× |
| no_change_census | — | 38.98 | 108.7 | 64.2 / 72.7 | 1.69× |

The reports hold every other case (flaky, rare, regex, filtered, tool and all-lane searches); none moved beyond the spread of these loaded runs. Status and around run no changed code. On one index, query-only medians changed from 166–172 to 42–45 ms for `release workflow` and from 192–199 to 77 ms for the natural query.

### Where the remaining time goes

- **natural**: about 300 tool blocks have a ceiling above the 40th score and hold every term; 143 of them return no row. Their per-block FTS queries (~107 µs each) take 32–39 ms, tool planning 17–21 ms (the two selected expansions and the bounds), prose candidates 5.5 ms.
- **release / json / list**: about 62 tool blocks at ~200 µs, plus ~10 ms planning; `list` adds per-session grouping.
- **short** (`hi`): 112–116 ms scoring 3,219 rows. For rows with non-ASCII text, `BoundaryOnlyText::new` hashes every cluster boundary of the row into `HashSet`s (61% of samples). Next: evaluate boundaries only at match positions.
- **error, count, chats, status, around**: process start and the census join; the census is 29 ms internally on a quiet machine, 64–109 ms at these loads.
- **recall / pack**: their search is faster; the window and expansion work around hits (~400 ms in run B) is unchanged.

First build took 386.6 s at load 71 / 66 / 45 (previous head: 186.0 s at 22 / 34 / 31); that pair ran at different loads. The first build now collects word presence while its FTS rows are prepared, instead of re-reading and decoding every tool row in a second pass; a rebuild still uses the separate pass. Alternating from-scratch builds of the separate-pass binary and this one ([every sample](measurements/build-ab-p8.json)) cut the dictionary phase from 19.2–25.7 s to 15.6–16.3 s but lengthened the FTS phase from 35.1–36.6 s to 41.2–44.5 s: word merging now bounds the FTS worker, even with one thread per shard. The closest-load pair took 185.8 s (separate pass, load 27 / 50 / 51) and 191.5 s (load 18 / 35 / 45); the total is unchanged within the load noise. The dictionary phase is still about 7 s above V7's 8.2 s, from inserting every word and its block list row by row. Results stay identical (62 of 62 cases on the newest build). The index is 2,428,379,136 bytes; presence adds about 9.6 MB to `tool_words`.

Synthetic, seven processes and one build each, all oracle checks valid: [mixed 100k](measurements/synthetic-p8-mixed-100k.json) first index 11.2 s, census 30.1 ms, one-session append refresh 43.0 / 50.1 ms p50 / p95; [dense 2M](measurements/synthetic-p8-dense-2m.json) `old_dense` 65.1 / 69.1 ms (V7 58.2 / 60.7 at 13.5 / 15.8 / 21.1) at load 54 / 47 / 46, append 73.4 / 82.0 ms. The dense corpus has no tool rows, so its path is unchanged.

## V7: measured retrieval, append and build changes

This is a measured checkpoint, **not SPEC §9 acceptance**. All real-history cases that succeeded in 0.4.9 remain faster than that release, but the unchanged keyword, census, append, first-build and dense limits still fail. The index is **2,365,624,320 bytes (2.366 GB)**, below the 2.5 GB storage limit. Measurements include every sample and load; no outlier was removed. The real source snapshot grew from 10,629 to 10,657 sources between V6 and V7.

### Full-process real history

Each median below is seven fresh processes, with ordinary concurrent census and detached refresh enabled. Load is the componentwise median of the 1/5/15-minute readings. 0.4.9 did not record load; its `rare` case exited 2 and is not a successful-search comparison. V6 loads remain in the preceding checkpoint and [V6 report](measurements/real-query-merged-v6-fresh.json). [V7 samples](measurements/real-query-merged-final7-fresh.json) retain timings, exits, all loads and the baseline arguments.

| Case | 0.4.9 ms | V6 ms | V7 ms | V7 load 1 / 5 / 15 |
|---|---:|---:|---:|---|
| release | 457.09 | 196.53 | 186.01 | 14.86 / 26.12 / 27.43 |
| error | 347.78 | 81.06 | 43.74 | 14.86 / 26.12 / 27.43 |
| natural | 270.82 | 227.09 | 206.91 | 14.86 / 26.12 / 27.43 |
| flaky | 220.43 | 66.45 | 42.23 | 14.23 / 25.80 / 27.31 |
| rare | 269.63 | 85.18 | 42.61 | 14.23 / 25.80 / 27.31 |
| short | 408.58 | 166.14 | 145.61 | 14.23 / 25.80 / 27.31 |
| regex | 309.89 | 67.13 | 45.60 | 14.23 / 25.80 / 27.31 |
| list | 862.46 | 337.60 | 326.63 | 14.23 / 25.80 / 27.31 |
| json | 505.40 | 216.07 | 182.46 | 13.41 / 25.44 / 27.17 |
| count | 462.77 | 99.99 | 57.00 | 13.41 / 25.44 / 27.17 |
| rare_hit | 174.93 | 60.23 | 40.90 | 13.41 / 25.44 / 27.17 |
| rare_hit_pi_filtered | 173.82 | 84.64 | 42.48 | 12.58 / 25.07 / 27.03 |
| short_filtered | 2734.58 | 83.35 | 131.32 | 14.56 / 25.08 / 27.01 |
| compact_release | 508.09 | 214.43 | 193.39 | 13.41 / 25.44 / 27.17 |
| compact_error | 347.83 | 70.80 | 43.62 | 13.41 / 25.44 / 27.17 |
| compact_natural | 362.40 | 281.58 | 210.78 | 13.41 / 25.44 / 27.17 |
| compact_flaky | 293.81 | 122.14 | 42.38 | 12.58 / 25.07 / 27.03 |

| Other command | V6 ms | V7 ms | V7 load 1 / 5 / 15 |
|---|---:|---:|---|
| no_change_census | 59.22 | 38.98 | 14.86 / 26.12 / 27.43 |
| tool_error | 148.07 | 98.54 | 17.80 / 25.58 / 27.17 |
| recall | 452.46 | 465.86 | 17.49 / 25.38 / 27.09 |
| pack | 384.71 | 408.75 | 17.45 / 25.25 / 27.03 |
| chats | 89.20 | 60.11 | 17.45 / 25.25 / 27.03 |
| status | 84.29 | 55.02 | 17.45 / 25.25 / 27.03 |
| around | 91.26 | 50.02 | 17.45 / 25.25 / 27.03 |
| version_control | 10.80 | 8.51 | 17.45 / 25.25 / 27.03 |

The filtered-short regression (83.35 → 131.32 ms) is retained. The count meets the requested 400 ms ubiquitous-count target, but not CI’s stricter common 40 ms keyword threshold. Navigation remains above its 40 ms deadline.

### Where the command time goes

Timing is opt-in (`AGREP_QUERY_TIMING`); the schema-read probe deliberately materializes the schema only while profiling. The normal benchmark above does not enable it. Profiles use the same frozen binary and hold `work.lock` to exclude detached writes. They include concurrent census and record foreground/background phases separately. Median components overlap or come from different samples and must not be summed as an exact critical path.

| Release phase, ms | Same real index, V6 | Same real index, V7 | 100k mixed, V7 |
|---|---:|---:|---:|
| Full process | 197.75 | 197.17 | 46.34 |
| Exec to main | 10.51 | 8.42 | 7.94 |
| CLI parse | 0.25 | 0.25 | 0.24 |
| Reader open | 0.21 | 0.19 | 0.17 |
| Reader pragmas | 0.03 | 0.03 | 0.02 |
| Schema read | 2.02 | 1.94 | 1.62 |
| plan_ms | 9.85 | 8.56 | 0.59 |
| sqlite_ms | 145.67 | 151.13 | 11.52 |
| hydrate_ms | 3.76 | 3.20 | 2.86 |
| score_ms | 13.56 | 12.80 | 16.77 |
| Wait after retrieval for census | 0.12 | 0.13 | 0.06 |
| Render | 0.11 | 0.12 | 0.10 |
| Exit after main | 1.20 | 1.18 | 0.94 |
| Load 1 / 5 / 15 | 17.45 / 25.25 / 27.03 | 16.59 / 24.81 / 26.86 | 19.27 / 23.78 / 25.62 |

[Same-index V6](measurements/process-real-same-before.json), [V7 real](measurements/process-real-final7.json), and [V7 100k](measurements/process-mixed-final7-mixed.json) show no release-query critical-path reduction on the same real snapshot: 197.75 → 197.17 ms. The broad tool posting queries dominate. On this machine, process entry/exit plus reader setup are about 12 ms, rather than the 126–159 ms entire commands reported on hosted runners. The 100k natural case is 238.74 ms: planning 3.68, SQLite 64.83, hydration 30.41 and scoring 123.40 ms. Its cost comes from repeated candidate verification on that corpus, not a fixed startup charge. Hosted-runner stage attribution remains unmeasured here.

Census runs concurrently from `ready()`, before retrieval begins, and is joined after retrieval. The final real release profile relisted **zero** directories, reused **2,846** directory listings and statted **10,658** files. Its last census took 29.22 ms, including 8.17 ms directory checks, 12.54 ms file stats and 7.36 ms comparison; release waited only 0.13 ms after retrieval. The full census-only command is 38.98 ms versus V6’s 59.22 ms, still above 15 ms.

Two measured alternatives were rejected: [relative-parent `fstatat`](measurements/census-paired-7c.json) worsened paired medians 57.90 → 84.99 ms; macOS bulk metadata enumeration took 42.89 ms at its best tested width. [Plain syscall sweeps](measurements/stat-workers-v7.json) over 13,504 paths took 40.63/26.90/18.03/13.69/13.23/13.41 ms with 1/2/4/8/12/16 workers. The best syscall-only time almost consumes the entire 15 ms process budget before startup, cached-directory decoding or comparison. Meeting 15 ms needs an OS change-journal or a persistent trusted metadata snapshot with invalidation, not another wider stat pool. Neither weakening of complete census is shipped. [Bulk samples](measurements/bulk-stat-v7.json) retain every run and load.

### Retrieval changes and remaining bottlenecks

- Metadata selectivity probes conservatively count invisible rows too, allowing covering agent/time indexes, and reuse their result across query tiers. Broad `pi` scopes remain broad; no selectivity is invented.
- Coverage scoring can load selective prose term-presence lists after four unsuccessful blocks. Their per-row IDF sums tighten block maxima without an OR scan of common/short terms. Unknown terms keep their full upper bound. Allocation limits switch plans to the complete block cursor; they never truncate results. Unicode and exhaustive-result regressions cover this path.
- Existing admissible block ceilings now apply before row hydration. Equal-score blocks restrict older timestamps before text fetch; page losers skip metadata joins and snippet/handle construction after exact scoring. Counts observed by that plan are preserved, with floor totals when enumeration stops.
- Tool phrase position checks use the row’s ASCII flag instead of a block’s unrelated Unicode gap. The decoder uses zlib-rs, retaining miniz compression to keep the storage ratio. On 5,000 sampled rows / 4.17 MB text, decoder medians were 99.20 → 32.20 ms. The alternative encoder enlarged those blobs from 2.263 to 2.726 MB and was rejected. [Codec samples](measurements/compression-backends-v7.json).

A global tool-phrase candidate buffer was also rejected: paired seven-process release/natural medians worsened 246.26/299.88 → 285.10/310.99 ms with identical winners. [All pairs](measurements/tool-stream-paired-final7.json) are retained. The final binary keeps the existing small-selective-stream plan.

The remaining broad-phrase cost is FTS cursor work in multiple physical blocks; single short terms spend most of their time scanning/scoring text. The next required change is a positional posting iterator that exposes row-level span/boundary ceilings and skips entire lower-scoring blocks before opening their SQL cursors. It must preserve tier ordering, Unicode conservative bounds and exact deterministic ties. Merely buffering the full broad stream is disproved above.

| Query-only case | Plan ms | SQLite ms | Hydrate ms | Score ms | Rows / blocks |
|---|---:|---:|---:|---:|---:|
| release | 9.54 | 207.42 | 2.44 | 9.99 | 455 / 68 |
| error | 1.32 | 5.11 | 1.16 | 6.41 | 370 / 23 |
| natural | 20.63 | 191.02 | 4.23 | 15.95 | 391 / 158 |
| natural_prose | 4.89 | 14.20 | 1.71 | 63.51 | 220 / 74 |
| short | 1.15 | 11.39 | 3.23 | 103.87 | 2325 / 38 |
| regex | 1.60 | 12.61 | 2.52 | 2.17 | 913 / 77 |
| rare_hit_pi_filtered | 14.07 | 2.97 | 0.49 | 0.26 | 58 / 29 |
| tool_error | 5.55 | 3.76 | 39.26 | 11.91 | 6842 / 31 |

[Seven-process query-only profiles](measurements/final7-query-phases.json) preserve load and every timing. These omit concurrent census and rendering and do not replace end-to-end budgets.

### Tails, including the owner-scale keyword component

All twenty ordinary full-history samples per case are retained in [the tail report](measurements/keyword-tails-final7.json). No plain-query sample exceeds twice its case median. [Tail attribution](measurements/tail-attribution-final7.json) retains each case’s slowest observation and every owner-component observation above twice its mode median.

| Case | V7 process p50 / p95 ms | Keyword p50 / p95 ms | Load 1 / 5 / 15 |
|---|---:|---:|---|
| release | 204.51 / 220.04 | 189.75 / 207.39 | 19.62 / 25.10 / 26.93 |
| error | 43.96 / 59.78 | 29.46 / 37.77 | 19.62 / 25.10 / 26.93 |
| natural | 225.64 / 236.80 | 213.84 / 224.70 | 18.45 / 24.77 / 26.80 |
| short | 148.35 / 156.66 | 135.15 / 143.17 | 17.53 / 24.47 / 26.68 |
| json | 212.68 / 244.53 | 196.42 / 230.65 | 17.53 / 24.47 / 26.68 |
| compact_natural | 216.54 / 228.91 | 201.56 / 212.08 | 17.25 / 24.30 / 26.61 |

On the identical 200k-reference owner-shaped fixture, interleaved V6/V7 keyword-component processes changed **171.14/403.49 → 114.66/246.55 ms p50/p95**; the keyword stage changed **148.24/384.16 → 94.28/225.67 ms**. All twenty paired pages have identical handles. This is **not a new hybrid acceptance run**: the pinned model cache was removed by the semantic campaign, no replacement cache was found, and network beyond crates.io is prohibited. [Paired component samples](measurements/owner-keyword-final7.json).

The original actual-model hybrid run’s six >2×-median samples (query IDs 1, 5 and 8, twice each) spend 1.13–1.90 s in keyword retrieval, with 1.26–1.89 s process-wide CPU and only 1–2 ms waiting for census. They implicate candidate/scoring work, not a census barrier. The semantic-only startup outlier is retained separately. [Every original outlier](measurements/owner-tail-attribution-before.json) records phase timing, load, faults and context switches.

V7’s three >2×-median component outliers are samples 1/11 (query ID 1) and 15 (query ID 5): 246.55/299.58/230.11 ms process, 225.67/280.17/214.95 ms keyword, 222.6/266.9/218.3 ms process CPU during that phase. These again arise inside retrieval. Counts are process-wide and include concurrent census; they are not exclusive CPU attribution. The reduced tail is evidence for the coverage/row-bound changes, but a model-enabled end-to-end rerun is still required.

### Append guarantee and first build

Append cursors now retain 1 MiB SHA-256 chunk digests. Positional reads verify **every byte of the previously indexed prefix**, in parallel, before accepting an append. The last partial chunk is reused for the new checkpoint. Arbitrary interior edits, edits spanning chunk edges, replacement, truncation and format change force full parsing; the regression suite includes interior/chunk-boundary modifications. This preserves cryptographic full-prefix rewrite detection, not an edge-only heuristic. Old private cursor encodings fall back to full parsing. SHA-256 uses the same digest algorithm via sha2 0.11’s ARM implementation.

| Copied source size | V6 append ms | V7 append ms | V7 parse/verify ms | V7 bounds ms | V7 load 1 / 5 / 15 |
|---|---:|---:|---:|---:|---|
| 119,046,533 B | 437.56 | 52.14 | 18.47 | 0.80 | 37.52 / 27.99 / 27.26 |
| 199,454,501 B | 668.59 | 87.47 | 37.79 | 0.83 | 33.69 / 27.48 / 27.09 |

[All seven interleaved append pairs](measurements/append-paired-final7.json) assert identical stored rows. Only 381 appended bytes are parsed. New-row-only prose blocks merge postings/maxima instead of re-reading their previous 512 texts; existing row changes/deletes rebuild the block. Full-bound-image tests cover text and metadata updates. The larger copy still misses 50 ms: exact prefix verification is size-dependent. Buffer reuse can reduce allocation, but removing that dependence while retaining exact arbitrary rewrite detection requires a trusted change journal or an append-only source guarantee.

The fresh full build took **165,301.52 ms** (V6 172,680.87 ms), load **20.19 / 33.90 / 29.90** versus V6 **38.35 / 55.82 / 58.73**. These are different live snapshots and loads; the overall difference is not an isolated speedup. It read 24,377,009,647 bytes, stored 1,512,184 changed rows and recorded one parse error. [Build report](measurements/real-build-merged-final7.json).

| Build phase | V6 ms | V7 ms |
|---|---:|---:|
| Parallel parse wall | 30,665.32 | 26,251.95 |
| Writes, enclosing | 123,721.76 | 123,276.45 |
| Row/event/blob writes | 31,049.67 | 33,744.69 |
| FTS | 40,609.54 | 32,057.70 |
| Graph persistence | 28,824.67 | 31,906.22 |
| Canonical reconciliation | 8,629.18 | 7,776.69 |
| Dictionary | 3,594.41 | 3,427.42 |
| Secondary indexes | 3,169.71 | 2,965.53 |
| Bounds | 9,806.32 | 8,339.14 |
| Statistics | 7,462.01 | 6,187.51 |
| Commit | 209.87 | 445.21 |

| Adapter | Sources | Summed parse work ms | Source-write loop ms |
|---|---:|---:|---:|
| claude | 46 | 309.65 | 67.44 |
| codex | 88 | 1,679.34 | 287.63 |
| pi | 10523 | 177,062.17 | 76,660.84 |

FTS decode/normalization now runs in a bounded two-batch pipeline beside the sole SQLite writer, and inserts bind borrowed values. Native node bodies move as parsed values rather than serialize/reparse at the write boundary. Graph compression already runs in parallel per source. These changes leave **123 s in writes**, including 33.7 s row/event/blob work and 31.9 s graph persistence. The 60 s requirement is therefore not met. Next: prepare typed event/blob/graph batches concurrently across sources before the SQLite write boundary, assign stable references once, and combine inserts; preserve one atomic publication transaction. Merely parallelizing FTS preprocessing cannot remove the remaining serial source loop.

| Object group | V6 bytes | V7 bytes |
|---|---:|---:|
| Tool text + digest key | 532,316,160 | 535,056,384 |
| Events + lookup tables/indexes | 437,833,728 | 439,537,664 |
| Parser state + graph | 369,311,744 | 371,875,840 |
| Gram postings + block keys | 139,915,264 | 140,607,488 |
| Prose FTS | 192,094,208 | 193,757,184 |
| Tool FTS | 314,011,648 | 315,936,768 |

[Per-object storage](measurements/real-storage-merged-final7.json) accounts for every object. The tool layout remains positional words with complete substring dictionary expansion and canonical verification for edge cases; no tool-output truncation or dropped history is used to reach 2.366 GB. The earlier measured full-trigram projection remains above the entire product size budget.

### Synthetic gates and validation

The mixed and dense campaigns retain twenty fresh processes per warm case and three independent first builds. Every hand-built warm case now emits a finite `p95_ms`, including `one_session_refresh` and changed-source search, even outside the minimum twenty-sample gate. No budget was changed. The dense corpus still has older sparse matches, boundary/possessive/interior variants, varied timestamps and an independent exact top-40 oracle for all ten query shapes.

| Workload | Build median ms | Census ms | Append ms | Result oracles | Strict gate |
|---|---:|---:|---:|---|---|
| [mixed-100k](measurements/synthetic-final7-mixed-100k.json) | 7575.35 | 23.04 | 36.42 | 763 valid / 0 invalid | failed |
| [dense-1m](measurements/synthetic-final7-dense-1m.json) | 39930.71 | 15.49 | 39.31 | 263 valid / 0 invalid | diagnostic 1M |
| [dense-2m](measurements/synthetic-final7-dense-2m.json) | 77563.40 | 23.98 | 61.71 | 263 valid / 0 invalid | failed |

| Dense shape | V6 2M p50 / p95 ms | V7 1M p50 / p95 ms | V7 2M p50 / p95 ms |
|---|---:|---:|---:|
| old_dense | 81.66 / 87.03 | 55.55 / 57.63 | 58.22 / 60.66 |
| old_dense_no_auto | 77.29 / 78.32 | 52.98 / 54.73 | 56.99 / 58.62 |
| old_dense_word | 35.69 / 55.64 | 26.55 / 30.63 | 31.25 / 35.40 |
| old_dense_regex | 45.36 / 58.05 | 23.19 / 26.47 | 26.35 / 30.17 |
| old_dense_time | 49.52 / 56.65 | 32.85 / 37.85 | 35.53 / 37.29 |
| old_dense_agent | 187.50 / 194.95 | 58.94 / 61.64 | 63.13 / 67.48 |
| old_dense_project | 94.68 / 98.50 | 46.26 / 50.44 | 76.86 / 81.29 |
| old_dense_since | 187.12 / 194.68 | 58.28 / 63.44 | 62.65 / 66.73 |
| old_dense_phrase | 120.15 / 124.24 | 92.17 / 96.29 | 98.58 / 102.28 |
| old_dense_count | 118.45 / 120.44 | 65.02 / 67.49 | 120.45 / 123.77 |

Primary-case median loads were 25.57 / 26.14 / 26.60 (mixed), 14.43 / 21.58 / 24.63 (1M), and 13.50 / 15.83 / 21.11 (2M); each linked report retains the load for every sample. The primary 2M dense case improved from 81.66/87.03 to 58.22/60.66 ms, but fails the 20/40 ms headroom gate. On the same 2M index, seven interleaved query-only probes isolate the code change: 74.16 → 51.75 ms for the primary query, 170.76 → 55.07 ms for its agent filter, 112.62 → 89.36 ms for the phrase and 113.70 → 114.94 ms for exact count. [All probe samples](measurements/dense-query-phases-final7.json) include plan, candidate, hydration, scoring and load. This does not excuse the failed full-process gate. The primary query still visits 119 blocks and hydrates 41,268 rows (previously 60,927). The phrase hydrates only 653 rows but spends 74.59 ms in SQLite; exact count spends 107.39 ms in SQLite and hydrates none. The remaining bound/SQL traversal and exact-count work require the posting-iterator change described above.

The semantic-1000 campaign completed first index, census, append and changed-source query cases, then stopped at missing-model validation before any semantic search. Its [partial samples](measurements/synthetic-final7-semantic-1000-partial.json.events.jsonl) and [blocker](measurements/synthetic-final7-semantic-blocker.json) are retained. This is an incomplete gate, not a skipped test or a pass.

Formatting, workspace/all-target Clippy with warnings denied, and all **291 workspace tests** pass (zero failures, zero ignored). License validation, the test-skip policy and the worktree privacy scan pass. [Validation results](measurements/final7-validation.json). Native Windows/Linux execution is not claimed from this macOS run; their existing conditional imports, byte-range-lock handling and empty lock-file policy are preserved.

The reader table columns and retrieval signatures remain stable. `freshness::Pending` adds only the diagnostic counts `listed_directories`, `cached_directories` and `stat_files`; private cursor/codec/bound tables are described in [ENGINE-API.md](ENGINE-API.md). Event timestamps, assigned turn/position and input/output offsets remain available to the merged readers.

All committed real-history evidence is aggregate-only. Raw observations stay under ignored `scratch/`. The real-history index, copied active sessions, owner-shaped fixture, synthetic databases and codec/stat probes have been removed. Recorded cleanup totals **7.607 GB** of logical files across successive runs. Final allocated usage is **3.610 GB target + 0.248 GB scratch**; guarded peak was **4.909 GB**, below 6 GB. Minimum observed free space was **177.115 GB**, with no disk-guard interruption and no scratch database left. [Disk accounting](measurements/final7-cleanup.json). No real agent store or config was modified, and no default/old agrep data root was used. Nothing was pushed.

## Merged V6 acceptance and measurements

The keyword changes are committed in `faa81ed7`; `e244c937` merges the semantic work through `23e83e94`. The Windows hook quoting correction is included. This is a measured checkpoint, **not performance acceptance**. Unchanged SPEC and CI limits still fail where listed below. Formatting, Clippy with warnings denied, all **286 workspace tests**, license validation and the repository privacy audit pass. No tests are ignored. Native Linux and Windows execution is not claimed from this macOS run.

The fresh database is **2,352,742,400 bytes** (2.353 GB), under the 2.5 GB storage requirement. It contains 131,207 visible prose rows, 1,089,831 visible tool rows, 7,838 sessions and 10,629 source fingerprints. The first build took **172.68 s** at load **38.35 / 55.82 / 58.73**, missing the 60 s limit. It read 24,312,865,862 source bytes and recorded one source parse error; last-good handling remains enabled. Each rebuild observes a later live snapshot, so the corpus is not identical between revisions.

Each table median below uses seven fresh processes with normal census and detached refresh enabled. Every sample and its load is retained; load columns are componentwise medians of the 1/5/15-minute readings. The 0.4.9 baseline did not record load. Its `rare` query exited 2 and is not a successful-search comparison. Initial-foundation medians used five processes; those records did not retain load. The [complete V6 aggregate report](measurements/real-query-merged-v6-fresh.json) includes exits, all timings and navigation cases.

| Case | 0.4.9 ms | Initial foundation ms | V5 ms | V6 ms | V6 load 1 / 5 / 15 |
|---|---:|---:|---:|---:|---|
| release | 457.09 | 4888.84 | 171.25 | 196.53 | 30.86 / 43.41 / 52.90 |
| error | 347.78 | 8747.58 | 70.11 | 81.06 | 30.86 / 43.41 / 52.90 |
| natural | 270.82 | 97442.06 | 182.74 | 227.09 | 35.44 / 44.15 / 53.11 |
| flaky | 220.43 | 5356.16 | 70.24 | 66.45 | 35.44 / 44.15 / 53.11 |
| rare | 269.63 | 2031.27 | 87.70 | 85.18 | 35.44 / 44.15 / 53.11 |
| short | 408.58 | 16582.95 | 148.33 | 166.14 | 35.44 / 44.15 / 53.11 |
| regex | 309.89 | 4009.84 | 70.59 | 67.13 | 35.44 / 44.15 / 53.11 |
| list | 862.46 | 14171.59 | 318.27 | 337.60 | 34.84 / 43.88 / 52.96 |
| json | 505.40 | 5077.19 | 196.11 | 216.07 | 34.84 / 43.88 / 52.96 |
| count | 462.77 | 59237.10 | 83.50 | 99.99 | 34.84 / 43.88 / 52.96 |
| rare_hit | 174.93 | 3876.58 | 47.15 | 60.23 | 34.84 / 43.88 / 52.96 |
| rare_hit_pi_filtered | 173.82 | — | 47.58 | 84.64 | 34.58 / 43.52 / 52.73 |
| short_filtered | 2734.58 | — | 72.00 | 83.35 | 34.29 / 43.31 / 52.60 |
| compact_release | 508.09 | 5147.67 | 192.60 | 214.43 | 34.45 / 43.65 / 52.83 |
| compact_error | 347.83 | 6443.22 | 44.90 | 70.80 | 34.45 / 43.65 / 52.83 |
| compact_natural | 362.40 | — | 208.55 | 281.58 | 34.45 / 43.65 / 52.83 |
| compact_flaky | 293.81 | — | 46.06 | 122.14 | 34.45 / 43.65 / 52.83 |

| Navigation | V6 ms | Load 1 / 5 / 15 |
|---|---:|---|
| recall | 452.46 | 32.32 / 42.44 / 52.13 |
| pack | 384.71 | 32.32 / 42.44 / 52.13 |
| chats | 89.20 | 34.14 / 42.65 / 52.15 |
| status | 84.29 | 34.14 / 42.65 / 52.15 |
| around | 91.26 | 34.14 / 42.65 / 52.15 |

### First build and storage

| Build phase | ms |
|---|---:|
| Parallel parse wall | 30,665.32 |
| Source writes (enclosing total) | 123,721.76 |
| Row/event/blob writes | 31,049.67 |
| FTS construction | 40,609.54 |
| Parser graph persistence | 28,824.67 |
| Canonical reconciliation | 8,629.18 |
| Word dictionary | 3,594.41 |
| Secondary indexes | 3,169.71 |
| Bounds | 9,806.32 |
| Family and term statistics | 7,462.01 |
| Commit | 209.87 |

| Adapter | Sources | Summed parse work ms | Source-write loop ms |
|---|---:|---:|---:|
| claude | 46 | 552.40 | 78.45 |
| codex | 83 | 2,797.63 | 297.19 |
| pi | 10,500 | 229,569.40 | 67,311.32 |

Write subphases are contained in source writes and must not be added twice. Per-adapter aggregate parse work and source write times are in [the build report](measurements/real-build-merged-v6.json). Parsing is parallel, initial construction skips obsolete-row diffs, and FTS plus secondary indexes are deferred. These changes do not meet the build target: parser-state serialization and per-event/blob SQL calls still consume substantial serial time. The next implementation step is a typed, streamed parse-to-write batch, with compact resumable graph records produced directly rather than repeated JSON transformations. The previous event-only batching experiment showed no reliable gain and was removed.

| Object group | V8 bytes | V6 bytes |
|---|---:|---:|
| Tool text and digest key | 1,367,445,504 | 532,316,160 |
| Events and lookup dictionaries/indexes | 834,797,568 | 437,420,032 |
| Parser state and graph | 1,273,192,448 | 369,311,744 |
| Gram postings and block keys | 507,617,280 | 139,915,264 |
| Prose FTS | 357,273,600 | 192,094,208 |
| Tool FTS | 307,597,312 | 314,011,648 |

Total database: **5,152,804,864 → 2,352,742,400 bytes**. Exact per-object accounting is in [the V6 storage report](measurements/real-storage-merged-v6.json). Stable reader tables remain views where needed; lossless tool text uses an integer-keyed deduplicated blob store.

The measured tool layout decision remains positional words plus a trigram word dictionary, with canonical verification for edge cases. A 28,553-row sample used 67,043,328 bytes for full positional trigrams versus 13,516,800 bytes for words plus dictionary. Full tool trigrams alone projected to roughly 2.54 GB before other tables, exceeding the entire product storage target. Word-only indexing lost 1,685 of 3,780 `error` matches and all seven interior `000d` matches; dictionary expansion recovered them. This is a measured departure from the full-trigram default, preserving substring semantics rather than dropping tool output.

Compression has a measured latency cost. On paired copies of a real-source sample,
mixed-codec / all-LZ4 medians were 30.75 / 26.97 ms for release, 67.05 / 47.50 ms
for natural, and 26.11 / 22.35 ms for tool error. The mixed layout remains necessary
for size; [the paired codec report](measurements/storage-v3-codec-latency.json)
retains all seven fresh-process samples and loads. In the full V6 tool-error
probe, hydrating 8,494 rows costs 89.41 ms. Candidate-level occurrence and boundary
bounds need to prevent those losing rows from reaching decompression.

### Freshness and append costs

The stat-only census took **59.22 ms** through process exit (load 30.86 / 43.41 / 52.90), against 15 ms. The last internal sample took 38.08 ms: read 0.16, directory listing 13.18, parallel stat 18.15, comparison 6.58. It found 38 changed fingerprints; a live-history census is not literally a no-change snapshot. Directory relisting is mtime-cached and stat uses eight workers. A faster OS-level fingerprint enumeration needs measurement before changing the complete-census contract.

An isolated copy of one real 31,987,752-byte active transcript appended two rows in **143.70 ms** median (seven fresh processes; load 29.33 / 40.73 / 51.12), against 50 ms. It parsed 393 appended bytes but read 31,989,302 bytes to verify the old prefix. Parse/verification was 92.03 ms, writes 4.33, bounds 24.09, statistics 4.42. [All samples and phases](measurements/active-copy-merged-v6.json) are retained. Exact detection of arbitrary prefix rewrites with only ordinary filesystem metadata still requires that read; append-only trust or an OS change journal would be a contract/design change, not an honest speedup. No such shortcut is enabled.

### Plain keyword and owner-scale hybrid tails

The previous actual-model owner run measured 200k vector references at one-minute loads 54.8–64.9: semantic-only 127.90/171.81 ms p50/p95, hybrid 197.19/1935.37 ms, and the keyword stage inside hybrid 115.79/1886.01 ms. Six hybrid samples exceeded twice the median and were keyword-dominated. These are pre-V6 measurements, retained in [SEMANTIC-INTEGRATION.md](SEMANTIC-INTEGRATION.md); stage percentiles overlap and must not be added.

The V6 instrumented plain-query follow-up uses twenty fresh processes per case and normal freshness. It is diagnostic evidence separate from the uninstrumented N=7 comparison.

| Case | Process p50 / p95 ms | Keyword p50 / p95 ms | Load 1 / 5 / 15 |
|---|---:|---:|---|
| release | 208.00 / 232.41 | 196.17 / 210.56 | 33.34 / 42.19 / 51.87 |
| error | 73.14 / 108.98 | 27.82 / 36.69 | 33.34 / 42.19 / 51.87 |
| natural | 220.46 / 241.26 | 205.05 / 217.75 | 32.75 / 41.92 / 51.72 |
| short | 161.37 / 182.10 | 147.47 / 166.87 | 31.89 / 41.59 / 51.54 |
| json | 191.78 / 218.27 | 178.07 / 200.43 | 31.10 / 41.26 / 51.37 |
| compact_natural | 206.66 / 243.75 | 194.06 / 229.80 | 29.33 / 40.73 / 51.12 |

[Tail samples](measurements/keyword-tails-v6.json) include process CPU, faults and phase counters for attribution. [The tail attribution](measurements/tail-attribution-v6.json) retains each p95 process and every sample exceeding twice its median. CPU counters are process-wide, so keyword and concurrent census CPU overlap and must not be added. [Query-only phase measurements](measurements/final-v6-query-phases.json) isolate plan, SQL candidates, hydration and scoring. The 100k stage probe from the preceding iteration measured 0.43 ms opening the reader, 3.13 ms planning, 37.22 ms in SQLite and 56.53 ms scoring; startup/schema work was not the dominant cost. The forced primary-key access path fixes repeated broad metadata scans, but ranking still verifies too many candidate rows. Next: retain admissible per-term boundary/span maxima with postings and enumerate candidates by those maxima, so proven losing rows never hydrate or run the Unicode scorer. The Unicode conservative-bound regression remains tested; lowering a bound without proof would lose winners.

| Query-only case | Plan ms | SQLite ms | Hydrate ms | Score ms | Hydrated rows / blocks |
|---|---:|---:|---:|---:|---:|
| release | 8.04 | 149.70 | 2.40 | 9.35 | 439 / 69 |
| error | 1.20 | 4.27 | 0.96 | 5.67 | 353 / 24 |
| natural | 15.86 | 155.53 | 5.10 | 14.46 | 406 / 162 |
| natural_prose | 4.86 | 13.26 | 1.51 | 59.80 | 212 / 72 |
| short | 1.31 | 11.47 | 2.93 | 93.98 | 2023 / 37 |
| regex | 1.61 | 12.23 | 2.45 | 2.19 | 879 / 77 |
| rare_hit_pi_filtered | 21.68 | 4.42 | 0.75 | 0.27 | 54 / 29 |
| tool_error | 5.97 | 4.49 | 89.41 | 15.91 | 8494 / 34 |

These query-only components omit concurrent census and rendering; they are attribution measurements, not substitutes for the complete-process deadlines. Their own loads and all seven observations are in the linked report.

The filtered rare-hit case spends 21.68 ms in planning before only 0.27 ms of
scoring. Code inspection identifies another avoidable cost: the selectivity
probe counts up to 65,537 rows while reading `visible`, preventing a covering
metadata-index count. A conservative count that includes invisible rows, or
transactionally maintained lane/filter cardinalities, can choose the same safe
fallback without those table reads. This is a proposed next change, not a
measured improvement in V6.

The matching V6 200k-reference **keyword-only component**, with no model installed, measured process 121.95/310.78 ms and keyword 104.92/285.22 ms p50/p95 at load 38.28 / 41.29 / 50.88. [Its samples](measurements/owner-keyword-v6.json) are not a new hybrid acceptance run. Different mode and load prevent attributing the difference from the prior hybrid run solely to code. A fresh model-enabled hybrid rerun is pending an existing pinned model cache; the semantic thread deleted its cache, and this task prohibits network access beyond crates.io.

### Synthetic gates

The corrected mixed generator uses about 1% Codex source files and predominantly pi/OMP tool-bearing rows. Actual role counts and generated text bytes are recorded. It replaces the earlier roughly 30% prose corpus, so timing changes across that correction are not isolated code comparisons. Every dense workload has sparse older matches among newer nonmatches, varied timestamps, aligned/possessive/interior forms, ten query shapes, and an independent exact top-40 handle oracle. No latency threshold has been relaxed.

| Workload | Build median ms | Census ms | Append ms | Result evidence | Strict gate |
|---|---:|---:|---:|---|---|
| mixed-100k | 6293.70 | 8.86 | 26.63 | 763 valid / 0 invalid | failed |
| dense-1m | 43639.70 | 37.55 | 88.01 | 263 valid / 0 invalid | diagnostic 1M |
| dense-2m | 86421.23 | 27.19 | 107.84 | 263 valid / 0 invalid | failed |
| semantic-1000 | — | — | — | not completed | blocked |

The 1M run is diagnostic; 2M runs the acceptance gate with half-SPEC p50 and p95 on every latency case. First-build limits scale by the larger row/text fraction. All twenty warm samples and all three build samples are retained in the linked reports. A failed strict gate remains a failure even when all result oracles pass.

The semantic fixture completed three index builds and twenty samples each of
census, append refresh and changed-source search. It then exited 1 at model
verification, before any semantic query. Those partial measurements and the
missing-cache reason are retained in
[the blocker record](measurements/synthetic-v6-semantic-blocker.json) and
[partial event report](measurements/synthetic-v6-semantic-1000-partial.json.events.jsonl).

| Dense shape | 1M p50 / p95 ms | 2M p50 / p95 ms |
|---|---:|---:|
| old_dense | 90.99 / 106.65 | 81.66 / 87.03 |
| old_dense_no_auto | 82.51 / 92.74 | 77.29 / 78.32 |
| old_dense_word | 39.31 / 55.16 | 35.69 / 55.64 |
| old_dense_regex | 30.49 / 38.41 | 45.36 / 58.05 |
| old_dense_time | 52.00 / 60.71 | 49.52 / 56.65 |
| old_dense_agent | 183.15 / 193.41 | 187.50 / 194.95 |
| old_dense_project | 58.69 / 67.72 | 94.68 / 98.50 |
| old_dense_since | 177.31 / 184.63 | 187.12 / 194.68 |
| old_dense_phrase | 121.44 / 130.72 | 120.15 / 124.24 |
| old_dense_count | 70.31 / 74.32 | 118.45 / 120.44 |

The largest remaining misses are not excused by load: workload samples preserve load and CPU measurements, and the same high candidate/score counts recur on smaller fixtures. Required follow-up is tighter proven posting bounds and direct candidate ordering; full-build follow-up is streamed typed batches. The exact prefix-rewrite guarantee remains an independent append cost.

### Surface, validation, privacy and cleanup

The stable reader function signatures and result types remain intact apart from documented additive fields. `Results.pending_sources`, `Filters.caller`/`CallerFilter`, and aggregate `Results.timings` are described in [ENGINE-API.md](ENGINE-API.md). `events` exposes `id`, `ts`, `position`, `visible`, and encoded `text`; body fields include millisecond `ts`, `native_turn`, assigned `turn` and `position`, plus Unicode-scalar `input_end`/`output_start` for tool text. Control events retain timestamps and positions. The forced tool ID projection is private and changes no external table columns.

All committed real-history evidence contains aggregate numbers and baseline query arguments only. Raw output remains under ignored `scratch/`. The measured full-history index, copied transcript and generated benchmark databases are removed after each campaign. Final allocated usage is 1.564 GB for `target/` and 0.253 GB for `scratch/`; the measured peak of those trees was 4.505 GB. Cleanup removed 9.058 GB of logical files cumulatively across sequential runs. Minimum observed free space was 182.509 GB, safely above the 2 GB floor. [Disk accounting](measurements/merged-v6-cleanup.json) records the categories and guard results. No source agent store or config was modified, no default/old agrep data root was accessed, and nothing was pushed.

## Prior checkpoints (retained evidence)

## Access-path checkpoint before the semantic merge (V5)

The V5 read-path correction fixes bounded prose scans to their primary-key range
and uses an explicit ID-index projection for tool candidates and filtered tool
counts. The unselective 1M agent query fell from **5,479.42 to 182.36 ms** in
seven interleaved pairs with identical winners; the time filter was unchanged.
All pair timings and loads are in `dense-agent-paired-v4-v5.json`.

The new full-history build took **188.19 s** at load
**33.21 / 62.59 / 63.94**, with a database of
**2,349,367,296 bytes**. It still fails the 60-second build target.
Seven fresh normal processes per real query produced:

| Case | V5 ms | Load 1 / 5 / 15 |
|---|---:|---|
| release | 171.25 | 39.95 / 48.48 / 57.49 |
| error | 70.11 | 39.95 / 48.48 / 57.49 |
| natural | 182.74 | 39.95 / 48.48 / 57.49 |
| short | 148.33 | 39.95 / 48.48 / 57.49 |
| rare_hit_pi_filtered | 47.58 | 44.93 / 49.14 / 57.57 |
| compact_natural | 208.55 | 42.40 / 48.72 / 57.47 |
| count | 83.50 | 40.52 / 48.46 / 57.43 |

The complete baseline query list, every sample and the remaining failures are
in `real-query-merged-v5-fresh.json`. This checkpoint is not a claim that the
SPEC gates pass. Formatting, Clippy and 271 workspace tests pass; final merged
measurements follow integration of the semantic kernels.

The mixed synthetic generator now uses approximately 1% Codex sources instead
of 20%, fixing its former 30% prose share. Text-length bands distinguish short
prompts/subagent inputs, longer replies and tool output. These approximate the
provided role totals; actual role counts and generated text bytes are recorded.
The older mixed runs remain diagnostic evidence, and the dense corpus is
unchanged. All latency budgets, p95 checks and dense headroom remain unchanged.

## Owner-scale hybrid tail before the latest keyword correction

The semantic integration at `1f94289e` measured twenty fresh processes per mode
on 200,000 synthetic vector references with actual model inference. One-minute
load ranged from 54.8 to 64.9. These figures precede the current ID-access fix:

| Component / mode | p50 ms | p95 ms |
|---|---:|---:|
| Semantic only | 127.90 | 171.81 |
| Hybrid complete process | 197.19 | 1935.37 |
| Keyword stage inside hybrid | 115.79 | 1886.01 |

Six hybrid samples exceeded twice the hybrid median; their time was dominated
by the keyword stage. Stage percentiles overlap and must not be added. The
underlying samples, loads and phase counters are retained in
[the semantic integration report](SEMANTIC-INTEGRATION.md) and its linked
aggregate evidence. The merged follow-up includes a matching 200k-reference
keyword-only component run and separate plain-query tails; neither substitutes
for a new model-enabled hybrid measurement.

## Final merged measurement (V4)

The measured binary contains the complete command merge from `c1bf7dac` and
Windows hook quoting fix from `ebebc689` (cherry-picked as `f0ea6030`).
**Performance acceptance remains incomplete.** The unchanged SPEC thresholds
still apply; misses are reported below, with measured causes and next work.
Formatting, Clippy with warnings denied, and all **269 workspace tests** pass;
none is skipped. Native Windows/Linux execution is left to CI, not claimed from
this macOS run.

The fresh full-history database contains **2,326,876,160 bytes**,
130,714 visible prose rows, 1,080,562
visible tool rows and 7,702 sessions. It fits the
2.5 GB storage target. First build took **271.59 seconds**
at load **35.87 / 47.94 / 70.03** and fails
the 60-second target. The build read 24,093,154,125 bytes across
10,493 sources and recorded 1 source errors. Source growth
makes successive builds different snapshots.

Each query median below uses seven fresh processes with normal concurrent
census/background refresh and no discarded samples. Load is the componentwise
median of the recorded 1/5/15-minute observations. The 0.4.9 baseline has no
recorded load. `rare` originally exited 2; its later live-history matches prevent
an equivalent successful-result comparison. All samples and exit counts are in
[the V4 aggregate report](measurements/real-query-merged-v4-fresh.json).

| Case | 0.4.9 ms | V3 ms | V4 ms | V4 load 1 / 5 / 15 |
|---|---:|---:|---:|---|
| release | 457.09 | 161.62 | 202.14 | 20.75 / 34.95 / 58.33 |
| error | 347.78 | 49.91 | 49.42 | 20.75 / 34.95 / 58.33 |
| natural | 270.82 | 189.48 | 219.91 | 20.13 / 34.58 / 58.06 |
| flaky | 220.43 | 47.50 | 49.91 | 20.13 / 34.58 / 58.06 |
| rare | 269.63 | 45.56 | 46.57 | 20.13 / 34.58 / 58.06 |
| short | 408.58 | 162.30 | 165.00 | 20.13 / 34.58 / 58.06 |
| regex | 309.89 | 48.39 | 52.95 | 20.13 / 34.58 / 58.06 |
| list | 862.46 | 307.71 | 410.78 | 19.55 / 34.23 / 57.80 |
| json | 505.40 | 195.01 | 261.43 | 19.55 / 34.23 / 57.80 |
| count | 462.77 | 68.43 | 108.70 | 19.55 / 34.23 / 57.80 |
| rare_hit | 174.93 | 65.85 | 68.15 | 19.55 / 34.23 / 57.80 |
| rare_hit_pi_filtered | 173.82 | 73.08 | 71.18 | 20.19 / 33.89 / 57.40 |
| short_filtered | 2734.58 | 58.57 | 88.55 | 22.85 / 33.98 / 57.16 |
| compact_release | 508.09 | 195.68 | 296.49 | 20.47 / 34.17 / 57.64 |
| compact_error | 347.83 | 50.97 | 83.28 | 20.47 / 34.17 / 57.64 |
| compact_natural | 362.40 | 204.27 | 293.41 | 20.47 / 34.17 / 57.64 |
| compact_flaky | 293.81 | 45.94 | 66.85 | 20.19 / 33.89 / 57.40 |

| Navigation | V4 ms | Load 1 / 5 / 15 |
|---|---:|---|
| recall | 668.05 | 21.85 / 33.40 / 56.68 |
| pack | 612.84 | 23.86 / 33.63 / 56.62 |
| chats | 178.14 | 26.52 / 34.01 / 56.63 |
| status | 97.77 | 26.52 / 34.01 / 56.63 |
| around | 85.50 | 26.52 / 34.01 / 56.63 |

V4 adds a proven ASCII all-terms ceiling, a positional term-order prefilter for
prose phrases, and avoids eager global buffering of broad tool streams. Original
Unicode text keeps conservative bounds, including normalization that expands
ligatures. Exact-result comparisons cover both lanes, non-score sorts, and the
Unicode winner. The 100k interleaved comparison returned identical winners for
all seven paired runs of release, natural, flaky and short. Its timings are in
[mixed-100k-paired-v3-v4.json](measurements/mixed-100k-paired-v3-v4.json); the
preceding stage profile below explains why setup is not the main cost.

Census was **51.77 ms** through process
exit. The final internal sample reports 14
pending source changes; this live-history run is not literally a no-change
snapshot. The complete stat census remains mandatory and is not replaced by a
timer cache. A copied active transcript of **118,755,475 bytes**
refreshed at **541.65 ms** median. It parsed only
393 appended bytes but still verified
the old prefix. [The active-copy report](measurements/active-copy-merged-v4.json)
contains its read/parse/write/bounds breakdown and every load observation.

### V4 first-build breakdown

| Phase | ms |
|---|---:|
| Parallel parsing | 51,825.94 |
| Source writes (enclosing total) | 200,689.54 |
| Row/event/blob work | 49,844.91 |
| FTS construction | 52,824.97 |
| Parser graph persistence | 60,882.80 |
| Canonical reconciliation | 14,911.79 |
| Word dictionary | 4,030.88 |
| Secondary indexes | 4,812.00 |
| Bounds | 10,173.28 |
| Statistics | 7,559.01 |
| Commit | 173.40 |

Write subphases are contained in source writes; they must not be added again.
Parallel parse sums are work, not elapsed wall time. Full per-adapter measurements
are in [real-build-merged-v4.json](measurements/real-build-merged-v4.json).
Parsing already runs across sources, initial construction avoids obsolete-row
diffs, and FTS/secondary indexes are deferred. The remaining first-build work
needs bounded parse/write pipelining, typed parser records without JSON round
trips, and fewer row/blob SQL calls. The tested event batch was removed because
it showed no reliable benefit. No first-build budget is widened.

| Object group | V8 bytes | V4 bytes |
|---|---:|---:|
| Tool text and digest key | 1,367,445,504 | 526,241,792 |
| Events and lookup dictionaries/indexes | 834,797,568 | 435,273,728 |
| Parser state and graph | 1,273,192,448 | 366,026,752 |
| Gram postings and block keys | 507,617,280 | 139,046,912 |
| Prose FTS | 357,273,600 | 187,699,200 |
| Tool FTS | 307,597,312 | 310,476,800 |

The final layout retains full prose trigrams, positional tool words plus a
trigram word dictionary, losslessly compressed shared tool text and compact
branch tuples. The 28,553-row tool sample measured 67,043,328 bytes for full
positional trigrams versus 13,516,800 bytes for words plus the dictionary. At the
current tool-row count, trigrams alone project to about 2.54 GB before the rest
of the database. Word-only indexing lost substring hits; dictionary expansion
recovered them. Unicode/punctuation/word/regex edge cases retain canonical
verification. Full object accounting is in
[real-storage-merged-v4.json](measurements/real-storage-merged-v4.json).

### Broad metadata access-path regression

The restored 1M sparse-old workload found an unselective-agent regression:
`old_dense` was 99.40 ms median, but `old_dense_agent` was 7,889.55 ms.
Read-only query plans showed `prose_scope (agent=?)` replacing the bounded
rowid range, so each visited block rescanned the agent's corpus. The analogous
tool plan began with the session dictionary and `event_position` rather than
the candidate IDs. This is an access-path defect, not evidence that `pi` is a
selective filter.

The follow-up fixes bounded prose scans to their rowid range and gives tool
candidate streams an explicit ID-index projection without joining text. Truly
selective metadata scans retain their metadata indexes. Regressions compare
SQLite work as unrelated rows grow and check the actual winners. The complete
V4 1M report is retained as diagnostic evidence; it is not replaced by the
follow-up measurements.

### Remaining risks and acceptance gaps

The stage profiles distinguish large fixed OS scheduling residuals under high
load from retrieval work. Repeated candidate/metadata SQLite work and scoring
still dominate many cases. Closing the 40 ms query target needs tighter
query-specific position bounds and contiguous posting/metadata traversal;
larger SQLite caches did not establish a win. Census needs an OS change-notice
or bulk-stat design to remove its syscall cost. Constant-time appends cannot
also detect arbitrary interior rewrites without a reliable range journal or an
explicit append-only contract; exact prefix verification is retained. First
build remains a serial-write bottleneck despite parallel parsing. Its collector
also retains all parsed sources before writing.

The stable reader surface remains compatible. Additions are
`CallerFilter.excluded_sessions`, `store::refresh_session`, shared `tool_text`,
Explorer freshness reporting, and the `events` view's `id`, `ts`, `position`,
`visible`, `text` columns. Event bodies retain `ts`, `native_turn`, `turn`,
`position`, `visible`, `input_end`, and `output_start`; offsets count Unicode
scalars and include control events without a tool row. Private block `all_ascii`
metadata requires a fresh derived index. Details are in `ENGINE-API.md`.

All full-history indexes and copied active-source inputs were removed after
measurement. Raw hits, source identities, plans and logs remain git-ignored;
committed reports contain aggregate numbers and allowed query arguments only.

## Merged V3 diagnostic

The merged binary includes `c1bf7dac`. The final fresh-history campaign indexed
130,673 visible prose rows and 1,079,304 tool rows in 7,693 sessions. The database
is **2,324,344,832 bytes**, below the 2.5 GB storage target and 54.9% smaller than
V8's 5,152,804,864 bytes. Source growth makes these successive snapshots rather
than byte-identical inputs. Full per-object accounting is in
[real-storage-merged-v3.json](measurements/real-storage-merged-v3.json).

The successful 0.4.9 keyword cases in the table are all faster in this fresh run.
**This is not SPEC performance acceptance:** ordinary keyword targets, census,
large-session refresh and first build still fail. Count passes its separate
400 ms target. The rare baseline exited 2; current rare results reflect history
drift and cannot establish a successful-result speedup for that case.

Each current median uses **seven fresh processes** with no discarded samples,
normal concurrent census and opportunistic background refresh. Each sample's
numeric 1/5/15-minute load readings and timing are retained in
[the final aggregate report](measurements/real-query-merged-v3-fresh.json).
The table's load is the componentwise median of those readings. The old baseline
has no recorded load, so differences cannot be attributed exclusively to code.

| Case | 0.4.9 ms | Merged V1 ms | Final V3 ms | V3 load 1 / 5 / 15 |
|---|---:|---:|---:|---|
| release | 457.09 | 245.05 | 161.62 | 14.70 / 22.73 / 39.27 |
| error | 347.78 | 85.82 | 49.91 | 14.16 / 22.49 / 39.08 |
| natural | 270.82 | 289.79 | 189.48 | 14.16 / 22.49 / 39.08 |
| flaky | 220.43 | 101.07 | 47.50 | 14.16 / 22.49 / 39.08 |
| rare | 269.63 | 123.38 | 45.56 | 14.16 / 22.49 / 39.08 |
| short | 408.58 | 240.03 | 162.30 | 14.16 / 22.49 / 39.08 |
| regex | 309.89 | 108.31 | 48.39 | 14.16 / 22.49 / 39.08 |
| list | 862.46 | 379.44 | 307.71 | 13.59 / 22.23 / 38.90 |
| json | 505.40 | 289.37 | 195.01 | 13.59 / 22.23 / 38.90 |
| count | 462.77 | 123.98 | 68.43 | 13.59 / 22.23 / 38.90 |
| rare_hit | 174.93 | 87.33 | 65.85 | 13.59 / 22.23 / 38.90 |
| rare_hit_pi_filtered | 173.82 | 100.23 | 73.08 | 12.82 / 21.93 / 38.69 |
| short_filtered | 2734.58 | 138.45 | 58.57 | 12.35 / 21.68 / 38.50 |
| compact_release | 508.09 | 227.04 | 195.68 | 13.59 / 22.23 / 38.90 |
| compact_error | 347.83 | 90.73 | 50.97 | 12.82 / 21.93 / 38.69 |
| compact_natural | 362.40 | 286.05 | 204.27 | 12.82 / 21.93 / 38.69 |
| compact_flaky | 293.81 | 94.86 | 45.94 | 12.82 / 21.93 / 38.69 |

| Navigation | Final V3 ms | Load 1 / 5 / 15 |
|---|---:|---|
| recall | 435.53 | 12.10 / 21.31 / 38.18 |
| pack | 492.57 | 12.41 / 21.22 / 38.05 |
| chats | 52.94 | 12.41 / 21.22 / 38.05 |
| status | 53.34 | 12.41 / 21.22 / 38.05 |
| around | 50.70 | 12.41 / 21.22 / 38.05 |

V2's recall median was **19,284.66 ms**. Its budget loop removed thousands of
already-invisible tool events individually, rebuilding the same large window
each time before reducing the message cap. V3 trims prose context first and
counts only events at retained turns; a regression includes 4,096 invisible
events. The fresh V3 recall median is **435.53 ms**, still above 250 ms. The event
window also now reads typed position/timestamp columns with indexed session
lookup and a direct blob reference. It no longer materializes the full tools
view to attach a small window. The SQL-work regression checks 4x unrelated
history, not wall-clock timing.

The planner removes redundant containing-substring variants, intersects two
selective required tool-term groups once, and makes candidate-buffer eligibility
relative to lane size. A full prose page whose scores exceed the maximum tool
weight can stop before expanding the tool dictionary. Common single tool terms
use the existing block-bounded scan. These change traversal, not matching rules,
score tiers or exact-count semantics. The production SQLite cache remains
unchanged: the covering-index, cache and mmap probes did not justify their costs.

### Remaining measured costs

The final query-only probe separates retrieval from census and rendering; it is
not substituted for the full-process acceptance samples. Medians in milliseconds:

| Query | SQLite | Hydration | Scoring | Plan | Examined rows / blocks |
|---|---:|---:|---:|---:|---:|
| release | 129.91 | 2.45 | 10.49 | 7.60 | 524 / 55 |
| error | 3.22 | 1.04 | 6.03 | 1.04 | 429 / 22 |
| natural | 133.65 | 4.80 | 26.22 | 14.97 | 488 / 169 |
| natural_prose | 7.64 | 1.91 | 70.68 | 4.54 | 352 / 85 |
| short | 10.74 | 3.40 | 92.62 | 1.00 | 3,056 / 39 |
| regex | 11.42 | 2.16 | 2.07 | 1.64 | 871 / 78 |
| rare_hit_pi_filtered | 3.12 | 0.83 | 0.31 | 20.94 | 59 / 27 |
| tool_error | 4.40 | 72.43 | 13.14 | 6.64 | 5,621 / 30 |

These probes ran at load 12.14 / 21.02 / 37.88 and 11.49 / 20.74 / 37.68.
Release and natural still spend about 130 ms in repeated block/metadata SQLite
work; short-query scoring costs 92.62 ms. Tool-error hydration costs 72.43 ms for
5,621 candidates. Closing the 40 ms target requires a contiguous in-memory
posting/metadata traversal with conservative block bounds, and a scorer that
avoids repeated normalization while preserving the Unicode oracle. A larger
SQLite cache did not close this gap. No candidate-count cap substitutes for an
upper-bound proof.

Census took **79.28 ms** through process exit, with 14 changed sources on the
active real stores. The last internal sample split was opening 0.15 ms,
directory work 11.14 ms, stat work 13.33 ms and comparison 6.41 ms (31.03 ms
internal total). This case's name is `no_change_census`, but the pending count
means the live corpus was not literally unchanged. The dedicated eight-thread
stat pool won the interleaved thread-count probe (83.06 ms versus 113.49 ms for
the previous pool). It still misses 15 ms. Eliminating the directory/stat cost
would require persistent OS change notifications or another freshness contract;
a new process cannot skip the census and claim the same guarantee. No timer
cache hides that work.

A read-only real-source copy measured appending two rows to a **47,510,194-byte**
pi transcript: median **255.06 ms**, load 11.49 / 20.74 / 37.68. Every sample
resumed one source and parsed **393 bytes**, but read about 47.51 MB to verify the
prefix. Internal medians were parse/verification 154.33 ms, write 6.06 ms,
bounds 27.08 ms, statistics 5.58 ms and commit 1.53 ms. This fails 50 ms.
The exact-rewrite guarantee and constant-time append refresh cannot both follow
from ordinary file metadata: a simultaneous interior rewrite and append leaves
edge guards unchanged. Keep the prefix hash until an explicit append-only
contract or trustworthy OS range journal is chosen. Per-source incremental
bounds would then remove the remaining 27 ms of global work. The copied source
and its index were deleted; only numeric aggregates are committed in
[active-copy-merged-v3.json](measurements/active-copy-merged-v3.json).

### Fresh-process 100k diagnosis

The hosted-runner results prompted a separate instrumented run on the 100k
mixed corpus. Seven fresh processes per mode, with load near **87 / 96 / 103**,
are retained in [mixed-100k-process-stages.json](measurements/mixed-100k-process-stages.json).
Instrumentation existed only in that diagnostic binary and was removed before
validation. The first normal process took 1,177.66 ms and remains in the report;
no warm-up was discarded.

| Normal release stage | Median ms |
|---|---:|
| Complete process | 151.78 |
| Exec / exit / scheduling residual | 15.18 |
| Argument parsing | 0.53 |
| Data root and census launch | 0.18 |
| Reader, functions and read transaction | 0.43 |
| Scope validation | 0.03 |
| Caller resolution | 0.05 |
| Keyword query and handle decoration | 114.99 |
| Remaining census wait and worker scheduling | 0.08 |
| Rendering | 0.13 |

Componentwise medians do not add to the median whole. Within the query, median
plan / SQLite / hydration / scoring were **3.13 / 37.22 / 10.07 / 56.53 ms**.
Reader observations split open from setup: pooled median open was 0.35 ms and
open-plus-function/pragma setup 0.39 ms. Census overlaps retrieval; the 0.08 ms
row measures only its remaining wait. `--no-auto` was 149.54 ms and JSON was
144.04 ms. The query-only probe examined **13,368 rows across 59 blocks** for a
40-row result. Setup and output work do not explain the main gap. The synthetic
corpus repeats a small vocabulary densely; its all-terms pass examines far more
rows than the real release probe's 524. Similar small/full-history latency does
not by itself establish fixed startup overhead.

The all-terms ceiling was one even when ASCII word boundaries made it
unattainable. For distinct ASCII alphanumeric term groups, fully aligned spans
need at least one separator; any partial boundary sets the all-terms tightness
to one half. The new bound is the larger attainable aligned ceiling, applied
only to blocks whose original text is entirely ASCII. Repeated/overlapping
variants and Unicode blocks retain the conservative bound. A regression compares
2,048 and 8,192 rows with exhaustive ranking and includes a ligature winner that
must remain eligible. Broad tool streams also avoid eager global candidate
materialization; selective streams still share their complete candidate buffer.
The phrase pass now rejects impossible term order directly from positional
trigram postings before loading prose. A separate row-level fix keeps Unicode
normalization expansion from making the proximity bound too small. These changes
require fresh measurements below before any speedup claim.

A separate six-build comparison did not establish a benefit for 64-event SQL
batching. V1 / V3 medians were **12,417.20 / 12,802.02 ms**, with row-write
medians **2,580.38 / 2,475.35 ms** and load readings ranging from 115 to 386.
The first V1 run was 247,402.71 ms and is retained. The batching code was removed;
[initial-write-paired-v1-v3.json](measurements/initial-write-paired-v1-v3.json)
contains every observation. No build speedup is attributed to that experiment.

### First build and storage

The final first build took **192,071.33 ms** at load **15.17 / 29.79 / 45.42**,
reading 24,069,315,479 bytes from 10,484 sources. One parse error is retained in
private diagnostics; last-good behavior is covered by tests. It does not satisfy
60 seconds. Internal phases and every adapter count are retained in
[real-build-merged-v3.json](measurements/real-build-merged-v3.json).

| Phase | Elapsed ms |
|---|---:|
| Parallel parsing | 40,998.21 |
| Source writes (enclosing total) | 127,706.51 |
| Row/event/blob work | 41,663.69 |
| FTS construction | 35,120.01 |
| Parser graph persistence | 27,168.67 |
| Canonical reconciliation | 7,776.50 |
| Tool word dictionary | 4,280.33 |
| Secondary indexes | 2,799.90 |
| Bounds | 12,307.63 |
| Statistics | 10,201.81 |
| Commit | 181.67 |

Write subphases are included in source writes, not added to it. Parsing already
runs across sources; first load skips obsolete-row diff/cleanup, defers secondary
indexes and builds FTS in batches. Source-parse sums / source-write times were
669.55 / 91.25 ms for 46 Claude sources, 2,201.00 / 349.83 ms for 29 Codex
sources, and 312,085.39 / 77,263.04 ms for 10,409 pi sources. Parallel parse
sums are not elapsed wall time. The next build work is bounded parse/write
pipelining, compact typed parser records instead of JSON conversion, and fewer
blob/event SQL calls. The current initial collector still retains all parsed
sources before writing. The 60-second target needs a different write path, not
another budget multiplier.

| Object group | V8 bytes | Final merged bytes |
|---|---:|---:|
| Tool text and digest key | 1,367,445,504 | 525,406,208 |
| Events, session/symbol dictionaries and lookup indexes | 834,797,568 | 434,958,336 |
| Parser state and graph | 1,273,192,448 | 365,740,032 |
| Gram postings and block keys | 507,617,280 | 139,001,856 |
| Prose full-text index | 357,273,600 | 187,486,208 |
| Tool full-text index | 307,597,312 | 310,030,336 |

The physical layout keeps positional trigram FTS for prose, positional word FTS
for tools and a trigram dictionary to expand substring terms into complete tool
words. The measured 28,553-row tool sample used 67,043,328 bytes for positional
trigrams versus 13,516,800 bytes for words plus substring dictionary. Word-only
matching lost 1,685 of 3,780 `error` matches and all seven interior-token matches;
the dictionary recovered those counts. Arbitrary punctuation, Unicode folding,
word and regex cases retain verification. Tool text uses lossless LZ4 or DEFLATE
when smaller, shared by digest; integer event metadata and packed branch tuples
remove the old duplicate graph payload. No tool output is truncated for storage.
The full object table includes all ancillary indexes and semantic queue pages.

## Merged binary baseline (V1)

The binary from merge `0e4aa605` completed a full build in **184.62 s** at
load **67.07 / 66.91 / 56.03**. Its database was **2,295,169,024 bytes**,
with 130,648 visible prose rows and 1,078,010 tool rows. The storage target
is met; the 60-second build target is not. Time in milliseconds: parsing
36,051; source writes 125,979 (row/event writes 35,503, parser graph 30,709,
FTS 35,952, canonicalization 8,304, dictionary 4,113, secondary indexes 2,606);
bounds 13,027; statistics 8,308; commit 521. The next build experiment batches
event inserts; graph conversion and FTS construction remain the largest
unresolved costs after that change.

Every completed keyword case below has seven fresh processes, no discarded
samples. Load is the median 1/5/15-minute triple sampled before each process.
The 0.4.9 rare case exited 2; current rare hits reflect history drift.

| Case | 0.4.9 ms | Packed V7 ms | Merged V1 ms | Load 1 / 5 / 15 |
|---|---:|---:|---:|---|
| release | 457.09 | 216.60 | 245.05 | 36.00 / 57.45 / 54.66 |
| error | 347.78 | 89.10 | 85.82 | 36.00 / 57.45 / 54.66 |
| natural | 270.82 | 262.44 | 289.79 | 35.36 / 56.96 / 54.50 |
| flaky | 220.43 | 77.28 | 101.07 | 35.36 / 56.96 / 54.50 |
| rare | 269.63 | 49.48 | 123.38 | 35.36 / 56.96 / 54.50 |
| short | 408.58 | 166.58 | 240.03 | 35.36 / 56.96 / 54.50 |
| regex | 309.89 | 60.99 | 108.31 | 39.97 / 57.56 / 54.73 |
| list | 862.46 | 292.37 | 379.44 | 39.97 / 57.56 / 54.73 |
| json | 505.40 | 326.47 | 289.37 | 44.62 / 58.23 / 54.98 |
| count | 462.77 | 85.13 | 123.98 | 44.62 / 58.23 / 54.98 |
| rare_hit | 174.93 | 57.36 | 87.33 | 44.62 / 58.23 / 54.98 |
| rare_hit_pi_filtered | 173.82 | 98.13 | 100.23 | 57.66 / 60.51 / 55.82 |
| short_filtered | 2734.58 | 52.79 | 138.45 | 57.44 / 60.42 / 55.82 |
| compact_release | 508.09 | 282.30 | 227.04 | 44.62 / 58.23 / 54.98 |
| compact_error | 347.83 | 91.80 | 90.73 | 46.57 / 58.41 / 55.06 |
| compact_natural | 362.40 | 279.23 | 286.05 | 46.57 / 58.41 / 55.06 |
| compact_flaky | 293.81 | 47.13 | 94.86 | 46.57 / 58.41 / 55.06 |

V1 census was **63.87 ms**, with one observed phase split of 0.15 ms opening,
15.36 ms directory work, 35.03 ms stat work, and 8.40 ms comparison. Nineteen
sources were pending in that sample. Natural language remains slower than
0.4.9 and most cases miss the 0.5 target. These are baseline failures, not
accepted budgets.

Navigation was interrupted after a recall process exceeded one minute.
Process sampling showed repeated temporary-file writes from its event read;
the outer join to the tool view materialized far more rows than the window
needed. The next version reads event metadata and its referenced blob directly
through the event view, with an index on assigned position. There is no
completed V1 navigation median. The completed keyword samples remain in
[the aggregate report](measurements/real-query-merged-v1.json).

Two smaller probes tested candidate access. A 23.29 MB covering metadata index
reduced the sampled release metadata lookup from 4.44 to 2.82 ms and natural
from 2.38 to 1.11 ms: too small to explain the observed latency, so it is not
added to the product. Removing redundant containing-substring alternatives
from FTS reduced release candidate enumeration from 10.47 to 4.35 ms and
natural from 10.89 to 6.14 ms, with the same 629 and 212 candidate counts.
These are SQL microbenchmarks, not claimed full-command speedups.

The next planner experiment also removes a cache eligibility discontinuity:
the tool term estimates for `last`, `release`, and `drafting` were about 40k,
51k, and 93k. The previous 32,768 cutoff reopened a large word-posting query
in every block. Two selective required groups can instead intersect once;
the remaining terms retain canonical verification. No retrieval budget is
relaxed.


The command integration now uses the committed-snapshot engine throughout. A stat
census overlaps retrieval, one `__work` task list refreshes in the background, and
explicit indexing drains work in the foreground. Caller predicates run before
ranking in both lanes; postcompact refreshes only the caller's indexed sources.
Event readers consume stored timestamps, turns, visibility, and multiline input
boundaries. Integration validation passes 264 tests with zero failures or skips.
This merge makes no new latency claim; the packed V7 measurements below remain
the last measured engine baseline until the merged-binary campaign completes.
The strengthened dense generator now checks exact top-40 handles across ten
query shapes, with sparse older matches and varied boundaries and timestamps.


**Performance status:** several SPEC latency budgets remain unmet. The gate is unchanged; the final measurements, diagnosed costs and work needed to close the gaps are below.

The following sections preserve the foundation's earlier measurements. Command and semantic slices are now merged. An unsupported command's exit 2 in a historical report is not a successful zero result or a latency win.

## Packed storage follow-up

The fixed storage sample contains 99 sources, 475,378,004 source bytes, 2,619 prose rows and 30,541 tool rows. The original V9 sample database occupied 132,698,112 bytes. Integer event rows and shared text reduced it to 96,833,536 bytes (storage V1); packed ASCII bounds and event references reduced it to 76,582,912 bytes (V2). The V3 layout occupies **62,656,512 bytes**. Its conservative full-history projection is **2,320,276,161 bytes**, or 2,784,331,394 bytes with 20% temporary space. The projection retains the measured full V8 objects where unchanged, scales new tool/event objects by the larger physical-row ratio, parser pages by source bytes, cursors by source count, and the changed prose index by its fixed-sample size ratio. It is a projection, not a completed full-history measurement.

| Sample object | V2 bytes | V3 bytes |
|---|---:|---:|
| Tool text blobs | 18,944,000 | 14,454,784 |
| Parser graph | 6,455,296 | 4,517,888 |
| Parser cursors | 475,136 | 475,136 |
| Typed event rows | 4,132,864 | 4,132,864 |
| Event identity index | 2,523,136 | 2,523,136 |
| Prose FTS data | 15,343,616 | 7,983,104 |
| Gram postings | 1,449,984 | 1,449,984 |
| Block gram keys | 393,216 | 393,216 |

`tools` and `events` remain read-compatible views. Their private storage uses integer session/symbol keys and immutable native event revisions. Tool text is stored once per content digest and shared with parser branches. Rewriting or deleting a source collects unreferenced blobs; leaving and restoring branches retains their exact earlier tool results. Parser pages store compact entry/parent/order tuples, ignored-node ancestry without ignored payload, and references to typed event text. They no longer duplicate full tool payloads or the complete parsed native graph. The cursor retains the parser state and exact-prefix digest, without duplicate edge fragments.

The tool codec measurement round-tripped all 30,541 sample rows (23,127,335 uncompressed bytes). LZ4 used 16,956,839 bytes and decoded the entire sample in 17.6 ms median; DEFLATE level 3 used 12,608,556 bytes and decoded in 307.7 ms. Brotli level 4 used 12,534,672 bytes but encoded in 1,321 ms and decoded in 365 ms, versus DEFLATE's 554 ms encoding. Brotli is a benchmark-only dependency and is not used by the product. Runtime selection keeps small/unhelpful cases in LZ4 and uses DEFLATE when it saves at least 16 bytes. Full query measurements must establish the cost of decoding only winning rows. Graph pages use DEFLATE level 6 with parallel bucket compression. All codec trials retain their numeric load observations in `storage-codecs-*-v2.json`.

The duplicate joined-text prose FTS field is removed. The normal positional trigram field supplies candidates; canonical phrase verification retains punctuation, case folding and Unicode boundary behavior. Block bounds pack only ASCII alphanumeric grams into integer keys and compressed posting deltas. Unrepresented characters retain a conservative bound and complete matching, including Unicode-only blocks. The regression checks 700 Unicode rows, a 40-row page, and the exact count.

Storage V3's sample first build took 9,471.70 ms at load 71.73 / 61.35 / 43.28. Its internal parse/write/bounds/statistics times were 1,429.71 / 6,583.27 / 548.04 / 455.62 ms; within writes, FTS took 1,928.56 ms and graph persistence 2,255.58 ms. This does not satisfy or predict the full-history 60-second target. The typed event/blob writes are now included in `row_diff_ms`; earlier V1/V2 subphase counters omitted that work, though their total write and process times included it. Reports `storage-v1.json`, `storage-v2.json`, and `storage-v3.json` retain object accounting and projections; adjacent query files retain aggregate samples only. The original copied inputs are read-only and superseded sample indexes were deleted before the full-history run.

### Full-history storage V3

The full build verified the size projection: **2,153,697,280 bytes**, compared with V8's 5,152,804,864 bytes (58.2% smaller). It holds 130,592 visible prose rows and 1,075,371 visible tool rows in 7,675 sessions; inputs grew since V8. The run read 24,004,472,482 bytes from 10,465 parsed sources, with one source error retained in the private diagnostics. It completed in **303,885.23 ms**, starting at load **61.99 / 59.97 / 43.41**. This exceeds the 60,000 ms target. The disk monitor observed 2,204,899,917 peak data bytes and 178,138,628,096 minimum free bytes. No superseded full index was retained.

| Object group | V8 bytes | Storage V3 bytes |
|---|---:|---:|
| Tool text and digest key | 1,367,445,504 | 509,526,016 |
| Events and lookup indexes | 834,797,568 | 411,815,936 |
| Parser graph and indexes | 1,092,403,200 | 288,071,680 |
| Parser cursors | 180,789,248 | 48,332,800 |
| Gram postings and block keys | 507,617,280 | 51,818,496 |
| Prose full-text index | 357,273,600 | 186,884,096 |
| Tool full-text index | 307,597,312 | 308,756,480 |

The V3 write breakdown was graph persistence 78,317.54 ms, row/event/blob handling 55,659.68 ms, FTS 48,854.44 ms, replay reconciliation 31,942.70 ms, word dictionary 5,341.82 ms and secondary indexes 1,928.66 ms. Parsing took 39,928.72 ms, block bounds 18,815.04 ms, statistics 7,950.99 ms and commit 307.91 ms. Write subdivisions overlap their enclosing write total. Per-adapter source counts, overlapping parse wall times, source-parse sums and write times are retained in the aggregate JSON. The next changes target repeated graph SQL/decompression and substring hashing during bounds construction; no faster full build is claimed from those plans.

| Adapter | Sources | Source parse sum ms | Adapter parse wall ms | Source writes ms |
|---|---:|---:|---:|---:|
| Claude | 46 | 413.86 | 39,918.53 | 87.58 |
| Codex | 29 | 1,383.36 | 39,915.87 | 341.25 |
| pi/OMP | 10,390 | 301,922.30 | 39,923.65 | 147,792.50 |

Adapter wall times include shared-pool scheduling and overlap. Source-parse sums add per-source observations and are not the 39,928.72 ms elapsed parse phase.

Seven fresh processes per case, with no discarded warm-up:

| Case | 0.4.9 ms | V8 ms | Storage V3 ms | V3 load 1/5/15 min |
|---|---:|---:|---:|---|
| release | 457.09 | 191.06 | 247.32 | 28.12 / 42.64 / 40.73 |
| error | 347.78 | 68.03 | 88.04 | 28.12 / 42.64 / 40.73 |
| natural | 270.82 | 701.95 | 395.30 | 29.15 / 42.62 / 40.73 |
| flaky | 220.43 | 90.81 | 123.29 | 29.15 / 42.62 / 40.73 |
| rare | 269.63 (exit 2) | 41.61 | 65.73 | 29.15 / 42.62 / 40.73 |
| short | 408.58 | 153.08 | 179.01 | 29.15 / 42.62 / 40.73 |
| regex | 309.89 | 60.95 | 84.81 | 28.66 / 42.29 / 40.62 |
| list | 862.46 | 339.26 | 473.76 | 28.66 / 42.29 / 40.62 |
| json | 505.40 | 229.04 | 305.60 | 28.21 / 41.97 / 40.52 |
| count | 462.77 | 89.11 | 113.53 | 28.21 / 41.97 / 40.52 |
| rare_hit | 174.93 | 45.59 | 67.40 | 28.21 / 41.97 / 40.52 |
| compact_release | 508.09 | 224.64 | 289.04 | 28.21 / 41.97 / 40.52 |
| compact_error | 347.83 | 75.88 | 88.63 | 28.91 / 41.89 / 40.50 |
| compact_natural | 362.40 | 767.28 | 598.70 | 28.91 / 41.89 / 40.50 |
| compact_flaky | 293.81 | 89.37 | 132.67 | 27.64 / 41.41 / 40.34 |
| rare_hit_pi_filtered | 173.82 | 865.09 | 90.73 | 27.64 / 41.41 / 40.34 |
| short_filtered | 2734.58 | 47.21 | 64.22 | 27.90 / 41.23 / 40.28 |

Census was 62.70 ms median and still misses 15 ms. The storage target passes; natural and compact-natural still exceed 0.4.9, and every listed case remains above its tighter SPEC target. Higher load prevents attributing V8-to-V3 latency differences solely to code. `real-storage-v3-queries.json` retains all sample durations, loads, exit-code counts and aggregate work. `real-storage-v3.json` retains every SQLite object size. Unsupported pre-merge command cases exited 2 and are not counted as successful performance results.

## Engine checkpoint before command integration

The latest full database contains **2,279,034,880 bytes**, plus 11,189,952 bytes of WAL after the query campaign. `real-storage-packed-v6.json` records the complete object accounting. Storage is below 2.5 GB despite source growth: 130,627 visible prose rows, 1,076,916 visible tool rows and 7,681 sessions. All retained measurement files contain aggregate values and allowlisted query arguments only; raw diagnostics remain in ignored scratch storage.

| Object group | V8 bytes | Current bytes |
|---|---:|---:|
| Tool text and digest key | 1,367,445,504 | 523,837,440 |
| Typed events, session/symbol dictionaries and event indexes | 834,797,568 | 407,461,888 |
| Parser state and graph | 1,273,192,448 | 363,667,456 |
| Gram postings and block keys | 507,617,280 | 138,641,408 |
| Prose full-text index | 357,273,600 | 187,207,680 |
| Tool full-text index | 307,597,312 | 309,305,344 |

The block postings now retain the maximum timestamp and role weight of rows that actually contain each gram. The clock and tie-time ceilings use these postings; recent non-matching rows cannot inflate an old-match block's ceiling. Phrase occurrence ceilings use the original literal tokens, so overlapping plural alternatives cannot double-count one occurrence. Mandatory-separator bounds use existing cross-token grams. The sparse-old regression checks exact top-40 positions among newer non-matches and possessive/interior forms at both 2,048 and 8,192 rows. The repeated-phrase regression also checks SQLite work, not only hydrated rows.

The latest initial build took **167,212.01 ms**, starting at load **47.02 / 66.10 / 65.94**. Parsing took 24,364.89 ms, row/event writes 36,285.95 ms, graph persistence 26,344.59 ms, FTS construction 37,163.08 ms, replay reconciliation 8,433.79 ms, bounds 10,839.22 ms, statistics 7,787.84 ms and commit 206.81 ms. The first-build obsolete-row scan is gone. Tool and graph DEFLATE levels are now 1; repeated blob digests reuse their stored body before attempting compression. The 60-second target still fails. The measured next steps are batching the remaining per-event/blob SQL calls and overlapping bounded parsing with writes; the current collector retains all parsed source batches before writing. FTS tokenization and graph preparation also remain material costs. No build-budget increase is proposed.

The following are seven fresh processes per case using the isolated full-history database, with all individual durations and load readings retained in `real-query-storage-v7.json`. The baseline has no recorded load. These are pre-integration keyword results; unsupported command timings are excluded.

| Case | 0.4.9 ms | 39b78a24 ms | Current ms | Load 1/5/15 min |
|---|---:|---:|---:|---|
| release | 457.09 | 4888.84 | 216.60 | 27.10 / 39.01 / 52.48 |
| error | 347.78 | 8747.58 | 89.10 | 27.10 / 39.01 / 52.48 |
| natural | 270.82 | 97442.06 | 262.44 | 26.69 / 38.73 / 52.30 |
| flaky | 220.43 | 5356.16 | 77.28 | 26.69 / 38.73 / 52.30 |
| rare | 269.63 (exit 2) | 2031.27 | 49.48 | 26.69 / 38.73 / 52.30 |
| short | 408.58 | 16582.95 | 166.58 | 26.69 / 38.73 / 52.30 |
| regex | 309.89 | 4009.84 | 60.99 | 26.15 / 38.42 / 52.11 |
| list | 862.46 | 14171.59 | 292.37 | 26.15 / 38.42 / 52.11 |
| json | 505.40 | 5077.19 | 326.47 | 26.15 / 38.42 / 52.11 |
| count | 462.77 | 59237.10 | 85.13 | 25.98 / 38.18 / 51.95 |
| rare_hit | 174.93 | 3876.58 | 57.36 | 25.98 / 38.18 / 51.95 |
| compact_release | 508.09 | 5147.67 | 282.30 | 25.98 / 38.18 / 51.95 |
| compact_error | 347.83 | 6443.22 | 91.80 | 25.98 / 38.18 / 51.95 |
| compact_natural | 362.40 | — | 279.23 | 25.34 / 37.84 / 51.75 |
| compact_flaky | 293.81 | — | 47.13 | 25.34 / 37.84 / 51.75 |
| rare_hit_pi_filtered | 173.82 | — | 98.13 | 25.34 / 37.84 / 51.75 |
| short_filtered | 2734.58 | — | 52.79 | 24.58 / 37.27 / 51.38 |

The current keyword values beat the successful 0.4.9 values shown, but the SPEC's tighter targets remain unmet. Census was 124.55 ms median. A full-process count was 85.13 ms, below its 400 ms target; it does not hydrate every matching tool row. The synthetic-looking rare token now appears in the live inputs, so its exit status is 0; the source snapshot has grown during development. This is recorded rather than forcing the original zero-result assumption.

Query-only profiling separates these failures: release used 152.78 ms median in SQLite, 2.82 ms hydration and 10.74 ms scoring; natural language used 158.21 / 8.09 / 27.57 ms. Short-query scoring used 86.66 ms. The SQLite figure includes repeated candidate/metadata cursor work. Selective prose intersections now buffer a complete small posting stream after the first block fails to settle the page; common phrases can stop before that allocation. High-frequency single terms use the existing bounded row scan. A certified aligned ASCII occurrence can establish the maximum boundary quality without preparing unrelated Unicode text; the Unicode oracle checks the shortcut around combining marks, ZWJ and apostrophes.

Two paired cache experiments, with rotated order and seven processes per setting, tested 32 MiB SQLite cache and 256 MiB/full-file mapping. They did not improve the main query cases consistently, so product defaults are unchanged. `storage-v5-cache-probe.json` and `storage-v7-mmap-probe.json` retain these results. Bigger caches are not represented as a latency fix.

## Redesign after the 39b78a24 measurements

The measurements below are the before baseline, not claims about the redesigned engine. The current work moves source parsing to a detached task list, caches directory listings, uses positional trigram indexes and compressed block posting lists, and adds bounded coverage/regex retrieval plus caller predicates. Seven-sample real-history measurements with per-sample host load will be recorded before any speedup claim. Resumable source parsing and changed-row family statistics are implemented. See `ENGINE-API.md` for additive integration fields and entry points.

## Disk-limited redesign measurements

The first positional-tool real build was stopped and its partial database removed when the shared-disk constraint was tightened. It produced no completed query samples (`redesign-disk-stop.json`). The subsequent sample selected 338 source files across file-size groups: 830,668,818 source bytes, 4,071 visible prose rows and 57,105 visible tool rows. It built in 51,250.69 ms at load averages 282.94 / 216.07 / 321.91. The sample database was 861,433,856 bytes. Its parser graph alone used 557,711,360 bytes: ignored native payloads were being retained in graph nodes. The original positional-tool projection therefore does not fit the disk reserve (`tool-layout-projection.json`). That rejected layout was not built again. The compact graph and word-dictionary layout below supersede this projection.

A systematic 28,553-row tool sample compared candidate indexes. Full positional trigrams used 67,043,328 bytes; word positions used 10,866,688 bytes in the initial component run. Word-only matching is insufficient: `error` had 2,095 matches versus 3,780 substring matches, and it missed all seven interior `000d` matches. Adding a trigram dictionary of complete indexed words recovered all measured substring counts exactly. The second component run used 13,516,800 bytes including that dictionary, with 4,001,792 bytes attributed to dictionary objects. It expanded 465 / 106 / 48 / 5 word alternatives for the tested baseline-query terms. `tool-layout-components.json` and `tool-layout-components-v2.json` retain both runs; they are component timings, not full-process latency claims.

The revised tool candidate path uses word positions plus this dictionary for ASCII alphanumeric terms; other literals retain verified block scanning. The normal text and its full substring semantics remain available. It does not discard the middle of tool output: the sampled head/tail alternative saved almost no space because adapter summaries were already capped, and it lost observed matches. Parser nodes now retain only fields consumed by the parser; native tool-input summaries and original length counters survive. The next sample must measure the new complete schema and demonstrate space for the database, temporary work and the 2 GiB reserve before a full-history build.

### Fixed-sample follow-up

The 339-source follow-up keeps 4,140 visible prose rows and 57,588 tool rows. Its first build fell from 25,940.38 ms to 15,555.30 ms after bulk row insertion, deferred FTS construction and deferred secondary indexes. The latter run recorded load averages 51.87 / 67.82 / 112.60. Its measured phases were parsing 2,389.08 ms, writes 6,893.39 ms, block bounds 3,949.57 ms, family statistics 1,038.83 ms and commit 947.81 ms. Within writes, row handling took 509.01 ms, FTS construction 3,998.79 ms, parser chunks 849.34 ms, secondary indexes 371.07 ms, canonical reconciliation 67.28 ms and the word dictionary 460.94 ms. `sample-build-v3.json` records this sample and its conservative size projection. These are sample measurements; the full 60-second build target is not established by extrapolation.

The same sample's seven-process query campaign is retained in `layout-sample-queries-v2.json` and `layout-sample-queries-v3.json`, including every load observation. Selecting the rarest tool term reduced the natural-language median from 6,656.38 ms to 1,057.54 ms. Forcing the posting stream before metadata lookups reduced the filtered count from 7,717.54 ms to 64.66 ms. Most keyword targets still fail on the sample. The next iteration removed per-ASCII-grapheme normalization allocations, cached exact ASCII Unicode properties, and used the compact boundary scorer for phrase hits. Its fixed-sample first build took 11,538.87 ms, with FTS construction at 2,974.81 ms and block bounds at 2,170.95 ms (load 20.93 / 36.32 / 80.84). Seven-process sample medians were release 205.30 ms, error 102.26 ms, natural language 685.71 ms, short 148.88 ms, JSON 216.03 ms and count 40.58 ms. These remain failures of the keyword budgets. `sample-build-v4.json` and `layout-sample-queries-v4.json` retain the measurements; full-history validation remains required.

### Segment and boundary follow-up

The same sample was profiled in seven fresh processes per case, with every host-load observation retained. The separator-safe ASCII boundary path reduced the `error` component median from 98.9 to 45.9 ms and `hi` from 146 to 62.3 ms. Compacting the sample's 9 prose / 11 tool / 3 dictionary segments to one each took 1,861.11 ms and changed the database from 246,210,560 to 250,294,272 bytes (including free pages). On that copy, release retrieval fell from 195.0 to 85.2 ms, natural language from 675.0 to 239.2 ms, and count from 37.2 to 21.5 ms. The short-query run varied in off-CPU/plan time despite identical examined rows; both runs are retained rather than selecting the faster sample. These are query-component measurements, excluding CLI census and rendering: `query-profile-v4.json`, `query-profile-v5.json`, `query-profile-v5-optimized.json`, and `fts-optimize-components.json`.

Initial FTS construction now raises the insertion hash limit, defers automatic merging, then optimizes each tree and restores ordinary incremental settings. In the 28,794-tool-row component experiment, word-index construction took 316.73 ms with these settings versus 442.70 ms before; full trigram construction took 3,771.80 versus 3,567.22 ms. This choice trades a small one-time trigram build cost for compact posting streams during subsequent reads. The word dictionary preserves the measured substring counts; `merge-layout-components.json` records both runs and their loads. It is not evidence of a full-history speedup.

The compact complete sample projects a 5,618,538,450-byte real database, or 6,742,246,140 bytes with a conservative 20% temporary-space allowance. The full run is guarded by the shared-disk reserve and the worktree budget; superseded sample databases are removed first.


## Full-history diagnostic after segment compaction

The first completed redesigned build (engine `164da469`) took **461,011.26 ms** at load averages **11.65 / 16.92 / 46.10**. It parsed 23,933,489,701 input bytes from 10,437 sources and emitted 1,486,660 source rows before replay selection. Parsing took 21,840.39 ms; source writes 62,332.45 ms; canonical reconciliation 240,447.25 ms; FTS construction 71,624.97 ms; block bounds 39,689.67 ms; family statistics 12,746.52 ms; commit 572.16 ms. Row handling and node-chunk timings are subdivisions of source writes. Full details, including per-adapter work, are in `measurements/real-build-v6.json`. The 60-second build budget still fails.

After the completed diagnostic cases, the database occupied 5,162,680,320 bytes, with another 144,772,712 bytes in WAL. It held 130,441 visible prose rows and 1,071,580 tool rows in 7,649 sessions. Tool FTS data used 292,761,600 bytes; prose FTS data used 354,902,016 bytes. `real-storage-v6.json` accounts for the other objects. This is the compact word-position/dictionary layout; the larger full-tool-trigram layout was rejected before this build.

Seven-process medians included release 426.61 ms, error 130.64 ms, natural language 13,509.46 ms, short 238.41 ms, list 3,388.95 ms, JSON 1,475.60 ms and count 913.46 ms. The stat-only census median was 49.90 ms; its final internal observation was 37.661 ms. These fail the requested budgets. Every completed sample and load observation remains in `real-query-v6.json.events.jsonl`. The diagnostic was stopped during the filtered list case after finding a planner defect; unfinished cases have no reported median. The later filtered cases overlapped the next release build, so they are retained diagnostics, not the final comparison campaign. The diagnostic index was deleted, freeing 5,307,747,957 bytes when shared free space fell.

Two root causes were identified for the next iteration. First, event turn remapping chose a session-wide primary-key scan instead of the existing native-turn index. On a 27,020-event scope, a seven-sample read-only component probe returned the same four rows in 30.343 ms through that plan and 0.102 ms through the native-turn index (load 23.62 / 26.89 / 32.74; `event-lookup-v7.json`). Second, the Codex/14-day tool scope contained 4,135 rows, just above the fixed 4,096-row planner threshold, which selected repeated global posting searches. The next planner judges metadata selectivity relative to lane size. These component findings do not establish a full-build or CLI speedup; those require the next complete campaign.

## Complete V8 diagnostic campaign

The complete seven-process V8 campaign used the frozen binaries in `measurements/engine-v8-binaries.json`. These are intermediate results, retained before the document-frequency and incremental-write corrections. There were no discarded query samples. Load columns below are the per-case median of the recorded 1/5/15-minute `uptime` observations; the JSON retains all seven observations. The 0.4.9 numbers come from the supplied baseline and have no recorded host load. An exit-2 baseline is a command failure, not a latency success.

| Case | 0.4.9 ms | Before redesign ms | Final ms | Load 1/5/15 min |
|---|---:|---:|---:|---|
| release | 457.09 | 4888.84 | 191.06 | 13.72 / 25.18 / 37.62 |
| error | 347.78 | 8747.58 | 68.03 | 13.72 / 25.18 / 37.62 |
| natural | 270.82 | 97442.06 | 701.95 | 13.42 / 24.93 / 37.46 |
| flaky | 220.43 | 5356.16 | 90.81 | 13.42 / 24.93 / 37.46 |
| rare | 269.63 (exit 2) | 2031.27 | 41.61 | 13.42 / 24.93 / 37.46 |
| short | 408.58 | 16582.95 | 153.08 | 14.99 / 25.06 / 37.43 |
| regex | 309.89 | 4009.84 | 60.95 | 14.99 / 25.06 / 37.43 |
| list | 862.46 | 14171.59 | 339.26 | 14.99 / 25.06 / 37.43 |
| json | 505.40 | 5077.19 | 229.04 | 14.99 / 25.06 / 37.43 |
| count | 462.77 | 59237.10 | 89.11 | 14.51 / 24.79 / 37.26 |
| rare_hit | 174.93 | 3876.58 | 45.59 | 14.51 / 24.79 / 37.26 |
| rare_hit_pi_filtered | 173.82 | — | 865.09 | 13.60 / 24.26 / 36.93 |
| short_filtered | 2734.58 | — | 47.21 | 12.75 / 23.90 / 36.73 |
| error_filtered | 601.61 | — | 60.21 | 12.75 / 23.90 / 36.73 |
| compact_release | 508.09 | 5147.67 | 224.64 | 14.51 / 24.79 / 37.26 |
| compact_error | 347.83 | 6443.22 | 75.88 | 14.51 / 24.79 / 37.26 |
| compact_natural | 362.40 | — | 767.28 | 13.83 / 24.48 / 37.08 |
| compact_flaky | 293.81 | — | 89.37 | 13.83 / 24.48 / 37.08 |

V8 built in 188,365.31 ms at load 14.11 / 34.51 / 43.33. It read 24,030,515,506 source bytes and stored 130,469 visible prose rows, 1,071,908 tool rows and 7,649 sessions. The database was 5,152,804,864 bytes, plus 36,383,752 bytes of WAL. The guarded campaign reached a minimum 5,679,865,856 free bytes. Its index was removed under both OS locks, freeing 5,189,286,933 bytes. The earlier V7 full campaign is also retained; its first build took 194,938 ms at much higher load, 92.62 / 80.76 / 55.02. These observations do not isolate code changes from host contention.

The two V8 synthetic dense campaigns failed the unchanged gate. At 1M / 2M rows, first builds took 68,244 / 142,720 ms; census 19.18 / 26.92 ms; one-session refresh 102.06 / 169.11 ms; dense search 146.94 / 286.30 ms (p95 153.25 / 324.52). The 1M search still examined only 576 rows in two blocks. This ruled out a failure to stop candidate scoring: FTS vocabulary counts in planning were traversing the common posting lists. Seven read-only component samples on the same 2M index measured those five frequency lookups at 169.70 ms versus 0.066 ms through a materialized counter table, with identical document counts. Building that table took 3,606.55 ms. `prose-frequency-components-v9.json` records host SQLite 3.53.4 and every load observation; final bundled-SQLite runs are required to establish the product speedup.

The 1M monitor encountered a file-removal race after the benchmark had completed and deleted its temporary corpus. The completed benchmark report and failed gate are retained; no peak/minimum monitor report is claimed for that run. The monitor now tolerates removal during its size walk. The 2M monitor completed, recording 3,102,059,890 peak data bytes and 8,417,472,512 minimum free bytes.

## V9 checkpoint before storage work

The materialized frequency cache restores bounded dense-query startup. On 1M rows, twenty fresh-process samples gave 26.44 ms median and 30.87 ms p95; retrieval with `--no-auto` took 14.94 ms. First construction (87,651.40 ms), census (21.27 ms) and incremental refresh (72.69 ms) still fail the unchanged gates. No full V9 real-history build was attempted: the restarted host has insufficient free disk for the V8-size database plus the mandatory reserve. The next step is storage reduction before a fresh real-history campaign.

| Case | Median ms | Load 1/5/15 min |
|---|---:|---|
| first_index | 87651.40 | 12.35 / 14.86 / 23.21 |
| no_change_census | 21.27 | 20.07 / 16.30 / 22.89 |
| one_session_refresh | 72.69 | 20.07 / 16.30 / 22.89 |
| old_dense | 26.44 | 19.91 / 16.30 / 22.87 |
| old_dense_no_auto | 14.94 | 19.75 / 16.29 / 22.85 |
| version_control | 8.39 | 19.75 / 16.29 / 22.85 |

`measurements/dense-v9-1000000.json` retains all aggregate samples and loads. The full command correctly exited nonzero for the three missed gates. The successful query gate is not represented as a successful complete benchmark. Initial statistics were parallelized, but the measured total increased relative to V8 under different host load; no first-build speedup is claimed.

## Archived foundation measurements

The remainder records earlier engine iterations. Physical-layout descriptions in this archived section describe those measurements, not the disk-limited redesign above.

## Physical layout decision

Measured on this Mac with the release `bench --layout` tool, 20,000 deterministic mixed prose/tool documents (no network, one transaction per index). Raw evidence: `measurements/layout.json`. Times below are milliseconds, sizes are bytes. These are component measurements, not CLI latencies. Count timings measure FTS posting operations; the product also verifies canonical text, and its end-to-end counts below include that cost.

| Layout | Build ms | Bytes | `error` count ms | `release AND workflow` count ms | Interior `ork` hits |
|---|---:|---:|---:|---:|---:|
| FTS5 trigram, full detail | 1420.21 | 64,012,288 | 1.608 | 8.572 | 17,593 |
| FTS5 trigram, no detail | 755.10 | 27,467,776 | 0.892 | 3.006 | 17,593 |
| FTS5 unicode61, full detail | 245.96 | 26,431,488 | 0.303 | 0.654 | 0 |

Chosen after the end-to-end scale experiment: prose uses a full-detail trigram index with two fields (normal text and text with phrase separators removed); tools use a separate no-detail trigram index. All queries are verified against canonical text. Tool literals expand to ANDs of trigrams because no-detail FTS cannot directly search a token longer than three characters. The full-detail prose index can find a joined phrase directly, without hydrating every bag-of-words candidate. Word FTS is faster, but loses interior substring matches; a full tool-text fallback would move the cost into ordinary reads. Neither FTS trigram variant answers two-character terms by itself. Prose block bounds include one-, two-, and three-character grams, preserving short queries.

Tool text is LZ4-compressed in SQLite; prose remains directly readable. Tool events retain metadata separately without another copy of their text. Prose and tools have independent row IDs, FTS trees and filters. The reference tools-on default is preserved. `--who` scopes that exclude tools and `set tools off` avoid the tool index completely. Both lanes have independent block bounds, so strong prose results can rule out unseen tool rows without reading tool text. The early prototype reports marked below used prose-only defaults; final reports use tools on.

## Ranking and stopping

Each lane’s row IDs form blocks of 512. Each block records the maximum occurrence count of each gram, maximum timestamps/role weight, and a conservative best tie key. For a query term, the minimum of its constituent gram maxima bounds occurrences; alternative spellings are summed, which can overestimate but cannot underestimate. Phrase repetition is bounded by `1 - 2^-n`. Role/recency bounds include the user floor explicitly. Boundary factors never exceed one.

The phrase tier runs first; only a complete, correctly ranked phrase page can bypass the all-terms tier. Chat heads still merge both with their lane weights. A minimal covering-span ceiling avoids full boundary scoring for all-terms candidates that cannot beat the page. Blocks are visited by descending admissible ceiling. A full top-k page stops only when the next block cannot improve the actual page, including its deterministic tie order. This removes the old `0.35` recency-floor versus `0.175` one-occurrence plateau. Within a block, cheap literal matching and a second admissible ceiling avoid Unicode scoring for rows that cannot enter the page. There is no candidate-count budget or planted first-hit shortcut. Exact counts exhaust the eligible stream; ordinary pages disclose exact or measured-floor totals. The dense test checks known winners and requires early termination in a single large session.

The retained Unicode 16 scorer grades original-text boundaries after NFKC case folding. Per-family short-token contributions and aggregate n/s statistics are committed with row changes. Phrase, all-terms and labeled content-term fallback remain separate tiers. Fallback is limited to human/compact prose output; JSON, TSV, counts and `--lexical` keep strict matching. Explicit word/regex queries bypass boundary penalties. Regex uses fancy-regex's backtrack limit and safe required-literal prefilters; constructs without a provable literal use a verified scan.

Selective agent/session/date/model filters are measured with a capped metadata probe. Small scopes scan their filtered rows directly, so a short Codex query does not traverse the large pi corpus. Broad scopes retain the FTS/block route.

## Ingestion and consistency

Every discovery pass enumerates the adapter's source files and stats them in parallel. Directory entries supply file types without a redundant serial stat of every file. Source fingerprints include size, nanosecond mtime, filesystem change identity and SQLite WAL metadata. Only changed sources are parsed. Content digests avoid rewriting unchanged rows/FTS entries. Numeric source IDs avoid repeating long source paths in every message and index entry. Removed sources remove their contributions; failed parses retain last-good rows and record a source error. Native replay copies are reconciled to one canonical message (preferring complete replies and attributed metadata), tool calls are deduplicated by event identity, and colliding native turns receive deterministic public positions. Noncanonical source contributions remain in the same database for incremental recovery; only visible rows enter FTS and block bounds. SQLite triggers maintain canonical row counts in the same transaction. Parser diagnostics are stored, not repeated on each search.

Normal refresh/reindex writes use one SQLite WAL transaction under a permanent OS advisory lock. Readers take no writer lock and hold a SQLite read snapshot. An opportunistic second writer serves the committed snapshot; explicit indexing waits. For the first build only, the writer creates a private SQLite database in DELETE journal mode, commits it, switches it to WAL, closes it, and renames it into place. This avoids a multi-GB initial WAL and its equally large checkpoint copy. No reader is pointed at the private build. It is disposable after interruption; it is not a second authority or compatibility format.

A second diagnostic 1M build was stopped after 230,020.87 ms, before commit. A host SQLite plan probe selected `prose_scope(agent)` before the family lookup, making statistics updates scan the agent corpus per family. The final query starts with `sessions_family` and joins through `prose_session`; the plan and probe version are retained in `measurements/family-query-plan.txt` and `plan-probe-version.txt`. The failure is retained in `dense-initial-aborted.json`.

An initial diagnostic attempt was stopped after 459,668.75 ms, before commit, with a roughly 2.7 GB WAL. It did not produce a usable index. The subsequent changes removed repeated tool-event copies, replaced per-event linear turn lookup with a sorted lookup, cached hot SQL statements, removed a quadratic census membership test, and changed initial construction as above. Raw failure record: `measurements/initial-build-aborted.json`.

## Measurement protocol

`crates/agrep/examples/bench.rs` is a release-built Rust tool. It generates native pi session files under a temporary HOME, including side sessions, assistant replies, varied prose lengths, many capped tool outputs and arbitrary query occurrences. The approximate target shape is 1,166k searchable rows, roughly 89% tools, with substantially longer tool text. Its corpus summary records the actual ingested counts. Dense runs deliberately use one-occurrence old prose and deterministic ties at 1M and 2M rows. Fixtures separately cover every adapter and the recorded Gemini histories.

Each sample starts a new complete CLI process and captures output through exit. For even sample counts the tool reports the upper middle order statistic as median; p95 uses the nearest-rank definition. No warm-up is discarded. `--runs` controls repetitions; `--build-runs` can separately bound the expensive first-build campaign. All benchmark launches use `nice -n 10`. Process-cold does not mean an OS cache purge. Synthetic sources and indexes are deleted by the tool's temporary-directory scope. The owner's sources are read-only and the real index is isolated under this worktree's ignored `scratch/` directory.

`replay-query-plan.json` is a read-only diagnostic plan probe using host SQLite 3.53.4; it is not substituted for the bundled-SQLite process timings.

The shared 12-CPU Apple M2 Max host had substantial concurrent load during this work, including load averages above 100 during the final build (`measurements/host-load.txt`). These are observed full-process latencies on that host, not isolated hardware measurements; the budgets are unchanged and failures remain failures.

Budgets live in `perf-budgets.json`. `--check` fails after preserving the measurements if a budget is exceeded. CI has no retry or advisory performance job. Local macOS results do not establish Linux/Windows latency or behavior; the workflow runs those platforms separately.

The first, prose-only 100k mixed run with no-detail prose took 305.36 ms for `release workflow` and 437.36 ms for `flaky windows cache`. The phrase index/source-ID revision reduced these to 159.38 and 414.57 ms, with first build falling from 15,387.53 to 10,612.02 ms. These are diagnostic iterations, retained in `synthetic-100000-initial.json` and `synthetic-100000-phrase.json`; the final reports below include the final family handling. They are not discarded warm-ups. All-terms ranking still does substantial work on the dense synthetic language workload.

The later layout experiment (`layout-final.json`) measured the dual-field full-detail index at 130,912,256 bytes and 3,668.75 ms for the same 20k mixed documents. The extra index is restricted to the much smaller prose corpus in the product; the 89% tool corpus keeps no-detail postings. This buys a direct phrase access path rather than declaring the word-only index semantically equivalent.

## Measured results

The pre-planner campaign used `b0279c4f` (`measurements/pre-mixed-prefilter-binary.sha256`), including canonical native replay handling. Prior files named `before-source-fix` and `before-replay-fix` remain diagnostic evidence. The source-error correction scopes failures correctly and retries only changed files; native replay copies now select complete canonical rows and preserve source contributions for deletion recovery. The CLI also keeps the reference opaque-session codec and exact output framing. Measurements are not represented as a performance-gate pass.

Committed reports retain aggregate timings, exit-code counts, corpus counts, index sizes and allowlisted baseline query arguments. Raw samples, plans and diagnostics stay under ignored `scratch/private-measurements/`. Reports with `before-source-fix` in the name are retained diagnostic runs, not the final validation campaign. Adjacent aggregate `.events.jsonl` files preserve completed case summaries even if a process is interrupted. The 10k smoke and 100k mixed campaigns each use one first-build sample and respectively three/five fresh-process samples per query. The 1M/2M dense campaigns each use one build and twenty samples per query. First-build figures are single observations, not invented medians of repeated builds. The tool supports repeated first builds via `--build-runs` (defaults to `--runs`).


### Prior synthetic measurements

The complete pre-fix runs remain in `mixed-final-before-source-fix.json`, `dense-1000000-before-source-fix.json` and `dense-2000000-before-source-fix.json`. They measured default dense medians of 48.80 / 81.99 ms and retrieval without census of 14.17 / 16.46 ms. They examined 577 / 641 rows in two blocks. Those are diagnostic observations; the fresh final campaign is reported below.

### Real-build diagnostic before the source-error fix

The first completed real build took **655,267.07 ms**, with 1,481,555 changed rows from 10,413 source files. Internal phases were parse 14,881.93 ms, writes 322,647.69 ms, bounds 307,737.96 ms, and family statistics 8,897.57 ms. It committed successfully. `real-first-build.json` preserves that observation. Its subsequent refresh campaign was stopped after identifying two correctness defects: an unchanged failed file was retried every time, and a single pi failure set failure metadata on every pi source. The final code retries changed sources only, scopes failure metadata to the failed source/dependency, and preserves unchanged last-good errors. The regression test covers a failed file alongside a valid sibling and a later change to the valid sibling. A fresh final build follows below; no corrupted diagnostic labels are presented as final coverage.

The same review found repeated reads of a source's first 16 KiB to extract cwd for each message. The final code reads that once per parsed source batch. The prior phase numbers include that redundant I/O.

The first build's read-only object accounting (`real-storage-before-source-fix.json`, host SQLite 3.53.4) measured tool text 1,185,759,232 bytes, tool gram bounds 947,826,688, their block index 721,670,144, tool FTS data 648,318,976, event metadata 512,077,824, and prose FTS data 425,824,256. These explain why splitting FTS alone did not shrink total storage: the exact stopping bounds cost 1.67 GB in that corpus. The process sample records a peak physical footprint of 8.5 GiB; the parsed source batches are still too large. Neither cost is hidden in a separate sidecar.


The completed canonical-replay build in `real-build-engine8d50.json` took 933,622.12 ms. A subsequent read-only probe found the per-session reply-shape lookup scanning the full shape table: five samples had a 14.78 ms median; an indexed temporary copy returned the same two rows in 0.003 ms. The final schema adds `reply_shapes_session` and the query uses it explicitly. `replay-shape-index.json` records the probe, its host SQLite version, and every sample; this component result is not presented as a full-build speedup. The final campaign also uses match-only verification for ordinary counts, retaining full classification for tier counts.


### Mixed-length query and all-terms planner correction

The complete pre-planner campaign remains in `real-history-before-mixed-prefilter.json`. It measured the natural-language query at **86,005.00 ms**, with compact output at **106,468.93 ms**. Any two-character term previously disabled the FTS prefilter for all terms. The planner now retains every indexable long-term clause in an AND query and verifies the short terms on those candidates. OR fallback still cannot drop short alternatives without changing results. A regression test checks both prose and tools, plus a nonmatching short term.

A second correction gives the all-terms phase its own finite score ceiling: all-terms scores have no phrase repetition penalty. The bound uses a maximum factor of one, and chat scoring applies the documented tier factor. A test requires a full strong prose page to rule out lower-weight tool blocks; the exhaustive-ranking equivalence test remains in place. The final query campaign below uses both corrections against the same real index. Its plain natural-language median was still 97,442.06 ms on the heavily loaded host, so the prefilter change alone is not a demonstrated latency solution. The no-detail tool lane still has to verify whether scattered term occurrences form a phrase before it can finish that tier.


## Measurement privacy

Committed real-history evidence contains aggregates and baseline query arguments only. Session references, source paths, source-derived text and raw process/SQL diagnostics are kept in ignored scratch storage. `bench/validate_repo_privacy.py --worktree` checks the repository snapshot. The first redesign build was interrupted before completion to relocate its raw output and incorporate the parser checkpoint schema; its incomplete duration is not a completed build result.


### Prefix verification constraint

An interior overwrite followed by an append left both 4 KiB edge guards unchanged on a 256 MiB fixture. SHA-256 verification of the prior prefix detected it. Seven warm measurements took 261.27 ms median (223.08–398.73 ms); aggregate evidence is `measurements/source-guard-cost.json`. This is a component experiment using the host SHA-256 implementation, not a CLI latency claim.

The checkpoint reader therefore verifies the old prefix before resuming. Parsing is proportional to appended bytes; exact arbitrary-rewrite detection still requires reading prior bytes. The 50 ms large-file target cannot be inferred from bounded parsing alone. Trusting the native append-only contract with edge guards would remove that read but would relax detection of a simultaneous interior rewrite and append. The implementation retains verification until that tradeoff is explicitly changed.


### Active-session diagnosis

The initial checkpoint build measured a 47.52 ms median append refresh at 1,000 rows and 1,160.38 ms at 100,000 rows. The last large-session sample spent 920.93 ms rebuilding family statistics, 118.15 ms in row/session writes, 78.08 ms parsing and verifying the prefix, and 6.74 ms rebuilding block bounds. These synthetic diagnostics are retained as `active-before-1000.json` and `active-before-100000.json`. They are not presented as real-history speedups.

The subsequent implementation records row contributions before mutation, updates per-family token counts within the same transaction, and limits family resolution to affected sessions and descendants. Ordinary single-source appends keep their existing public positions and avoid a session-wide reconciliation scan. SHA-256 uses the pure-Rust ARM-accelerated backend in sha2 0.11; exact digest output is tested. Seven fresh-process samples after the change measured 32.61 ms for 1,000 rows and 49.08 ms for 100,000 rows. The large-session last sample spent 16.77 ms in prefix verification/parsing, 0.76 ms in writes, 6.65 ms in bounds, and 3.79 ms in statistics. It parsed 180 appended bytes while reading 21,495,518 bytes for exact prefix verification. The corresponding first builds were 210.07 ms and 15,494.69 ms; changed-row counters add first-build cost. These are synthetic measurements, not a real-history speedup claim. Every sample and its three numeric load averages is summarized in `active-after-1000.json` and `active-after-100000.json`.

### Storage V3 component diagnosis

Seven read-only fresh-process retrieval probes separate CLI census from retrieval. Release spent 197.86 ms median in SQLite, 3.01 ms hydrating text and 16.89 ms scoring. Natural language spent 392.18 / 2.11 / 28.59 ms in those phases. Tools-only natural language spent 18,567.46 ms in SQLite, 332.99 ms hydrating and 56.84 ms scoring, examining 24,725 rows across 1,594 blocks. The tools-only error case spent 1,310.40 ms in SQLite and 7.15 ms hydrating. These identify repeated candidate/metadata work, rather than decoding winning tool text, as the dominant retrieval cost. The complete numeric observations and per-sample loads are in `storage-v3-profile.json`.

The codec comparison used two copies of the same real-source sample index, alternating mixed-codec and all-LZ4 order across seven fresh processes with `--no-auto`. Mixed / LZ4 medians in ms were release 30.75 / 26.97, error 20.88 / 21.55, natural 67.05 / 47.50, short 50.27 / 50.50, tool error 26.11 / 22.35 and count 12.54 / 12.37. These are sample observations, not full-history codec speedups. Both copies preserve the same text and postings; the LZ4 copy was deleted after measurement. `storage-v3-codec-latency.json` retains paired durations, exit codes and load observations. The storage decision remains lossless mixed coding; the larger latency gaps must be closed in candidate traversal and initial construction.

### V4 diagnostic and work-bound corrections

The V4 full build occupied 2,149,715,968 bytes but took **526,333.47 ms**, starting at load 66.78 / 43.75 / 40.01. The observed load rose above 130 during this run. A single one-second stack sample was taken during construction; this is an instrumented diagnostic, not an isolated acceptance run. Parsing took 20,071.37 ms, row/event handling 45,471.00 ms, graph persistence 27,327.57 ms, FTS 44,523.69 ms, replay reconciliation 9,551.62 ms, bounds 8,443.65 ms and statistics 6,522.52 ms. The full report retains all phases and per-adapter counts in `real-build-storage-v4.json`.

The source-write total was 430,103.24 ms. Its roughly 357-second remainder exposed an initial-load regression: deferring the source lookup index made the inactive-event cleanup scan the accumulated event table for every newly inserted source. A first build has no obsolete contributions. Its source writer now returns before those diff and cleanup operations. A regression measures SQLite VM work for 128 versus 512 fresh sources with secondary indexes deferred; four times the input must remain below six times the work. This corrects the quadratic path without adding an index that the initial load does not need. A subsequent full build is required to quantify the correction.

The common-query work regression now also covers multiword phrases. It exposed another loose bound: a one-occurrence two-word row scores below 0.5 because its separating space increases the span, while the former block ceiling remained 0.5. Existing cross-token gram postings now prove when a zero-character join is impossible. Each proven mandatory separator contributes one character to the phrase's minimum span. Unknown or Unicode bridges retain the conservative ceiling. This makes the common multiword fixture stop without changing its winning rows or loosening its work assertion.

`--no-meta` is 0.4.9's page filter: the search fetches `max(-n, 40)` rows (chat heads under `--chats`), drops the proven ~meta rows among them, keeps the best one only when nothing else is left, and counts only the kept rows. Its lineage checks therefore follow the page, not the number of matches; a regression requires the extra SQLite work to stay flat when the corpus grows fourfold.
