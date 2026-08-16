# Changelog

## 0.3.0 — 2026-08-15

- `agrep setup` renders its five steps with colored boundaries and opens with
  a one-line step map. Headless runs and env-detected agent contexts never
  sit on a consent prompt: they get the full write disclosure plus explicit
  agent instructions strongly recommending `agrep setup --yes`, and the
  archive question is deferred rather than asked. Nothing consent-gated is
  written without `--yes`, as before.
- Global npm installs now run `agrep setup --yes --no-semantic --no-archive`
  from postinstall, so `npm install -g` ends enrolled with a built index;
  `AGREP_NO_AUTO_SETUP=1` restores warm-only, `AGREP_SKIP_POSTINSTALL=1`
  skips postinstall entirely. The model fetch still waits for first semantic
  use. The uninstall-cleanup sentinel ships with the auto-written blocks by
  design - it is the undo mechanism for exactly those writes - and the
  postinstall log and both READMEs name it.
- The Windows uninstall sentinel now arms without elevation: `schtasks /SC
  ONLOGON` demands administrator rights (observed live: every unelevated
  setup - including npm postinstall - printed "sentinel could not be armed"),
  so registration goes through `Register-ScheduledTask` with an own-user
  logon trigger, keeping schtasks as the fallback. Verified end to end on
  Windows 11 ARM64: arm, doctor "armed", `agrep remove` deletes the task.
- The Windows sentinel task now references uv's managed base interpreter
  instead of the tool-run venv shim: `uv cache clean` deletes the ephemeral
  venv (leaving the logon task dangling) but never the managed install. The
  watcher is stdlib-only, so the base interpreter suffices.
- Bare `agrep` now ends its status page with the five newest indexed chats
  and their copyable `agrep around` follow-ups - the "what was I working on"
  answer without learning a command first.
- Instruction block v37 routes session questions by their tense: running or
  active right now is `agrep board --once`; recent, last, or latest sessions
  is bare `agrep chats` (newest first). Agents measurably reached for the
  live board when asked about recent history.
- Live observation now covers pi and oh-my-pi: `agrep board` and `agrep tail`
  see running omp agents (sessions, subagents, tool activity) by tailing
  their append-mode JSONL stores, the same hook-free route as Claude. The
  `pi.live` registry capability is flipped to supported and `--agent omp`
  aliases to pi everywhere. Previously the board was blind to a box full of
  working omp agents.
- `postcompact` now recovers on pi/oh-my-pi compacted resumes. Four fixes,
  each observed live on omp: staleness-shaped boundary misses retry on a
  short bounded schedule (one immediate ingest, then ~1s and ~3s later)
  because the hook can fire before the compacting agent flushes its boundary
  row to disk; a freshly-resumed session whose recap is turn 1 serves its
  pre-boundary tail from the family root, capped at the boundary timestamp
  and disclosed as `window_source: family_root`; a live freshness story
  no longer refuses a proven packet - after the retries it serves as an
  explicitly partial packet carrying the story, since the compacting
  session's own churn kept the index "behind" at exactly the moment the
  packet exists for; and when that churn starves the generation-stable
  snapshot open outright (each retry ingest advances the generation the
  daemon is also advancing), the final attempt serves the boundary from the
  last published snapshot as a partial packet naming the churn.
- When weak keyword hits print the "chats about this semantically" block, the
  header now counts the neighbors held below the similarity floor, and a dim
  `deeper: agrep recall '<query>'` pointer names the lane-fusing surface.
  Machine modes are unchanged.
- The `meaning unavailable; keyword-only` story lectures once per cause per
  ten-minute window: the first occurrence carries the cause and retry lever,
  repeats of the same cause render the bare line. The lane state itself is
  disclosed on every render, machine `semantic_status` is never dampened,
  and read-only data dirs always get the full story.

## 0.2.0 — 2026-08-12

0.1.x shipped multi-agent ingest, keyword search, and an optional semantic tier
that required torch and a running local server. 0.2.0 keeps the ingest
foundation, replaces the semantic stack with a pinned in-process embedder, and
adds the evidence path that lets an agent use its own history without a human
driving the CLI: `recall`, digest-checked handles, `postcompact`, and the
setup-installed instruction block. Indexing and search remain local and
read-only; transcript content is never uploaded.

### Agent coverage

- Four new adapters bring coverage from six agent tools to eleven: crush
  (including per-project databases named by its registry), Gemini CLI,
  Cursor, and pi, whose adapter also walks oh-my-pi's store, a fork sharing
  pi's format byte for byte, under the same `pi` label. The adapter ingests
  those stores' own compaction records as boundary evidence; setup's optional
  shared extension shapes future summaries and exports the exact live session
  identity. 0.1.1 covered Claude Code, Codex, opencode, Antigravity, Kimi CLI,
  and Cline.
- Live tailing covers Claude, Codex, opencode, Antigravity, and Cursor. Native
  `resume` covers Claude, Codex, opencode, and Antigravity.
- Copilot CLI and qwen-code are now detected and counted by `doctor`, but not
  yet parsed.

### Search

- Keyword search carries over from 0.1.x; the result surface is rebuilt. Default
  matching is documented and pinned by a ranking contract
  (`docs/SEARCH_RANKING.md`, new): multi-word queries bridge punctuation and
  underscores, an independent any-order lane keeps scattered evidence
  reachable, and boundary quality affects rank rather than eligibility, so short
  fragments still behave like grep.
- Exit codes now distinguish two different absences: a current, exhaustive
  keyword miss exits 1 like grep, while an unverified or stale absence exits 2
  rather than claiming nothing matched.
- Agent-driven shells get a byte-budgeted page of one-line hits with
  digest-checked `@session:turn.digest` handles that paste directly into
  `around`, `recall`, and `resume`. The digest is a content claim: a handle
  whose row moved is rescued and disclosed, or refused, instead of silently
  serving different text. Stable turn assignment at ingest remains future work
  (`docs/HANDLE_IDENTITY.md`). `--more <handle>` pages from a short-lived frozen
  top-40 snapshot; `--classic` or `AGREP_PROFILE=classic` restores the human
  renderer.
- Ranked surfaces treat a root chat and its spawned children as one conversation
  family, so one large agent swarm cannot consume the result page; literal grep
  stays exhaustive across side chats, and `--all-side-chats` expands siblings
  into independent slots.
- Tool calls and their output are indexed alongside prose and ranked below it,
  so command echoes do not bury the conversation about them
  (`agrep set tools off` for prose only).
- `agrep around` gained handle input, root-prose defaults that keep generic tool
  events out of an agent's attention path, and `--full` for the same-window
  forensic stream. New sibling commands: `recall` (prose recall),
  `pack` (several recall queries, deduped, one budget), and `chats` (indexed
  main-chat history).

### Semantic search

- The semantic stack is replaced outright. 0.1.1 needed torch,
  transformers, sentence-transformers, and scikit-learn from a separate
  `requirements.txt`, ran Qwen3-Embedding-0.6B, and served `--semantic` only
  while a local server was running. 0.2.0 runs a pinned 47M-parameter embedder
  (`granite-embedding-small-english-r2`, int8 ONNX) in-process behind a
  short-lived worker lease, with no server and no torch. That is the CPU lane,
  which runs everywhere; on Apple silicon the Metal lane below runs the same
  weights on the GPU.
- Dependencies are now numpy, onnxruntime, and tokenizers, installed
  automatically wherever upstream ships compatible wheels. Platforms without
  them stay core-only instead of failing.
- The model is fixed by revision and per-file SHA-256, so a byte of drift is
  rejected rather than served. Sequence length is part of the vector-space
  identity: changing it invalidates old rows even when the weights are equal.
- Recall combines keyword and meaning evidence as independent lanes rather than
  fusing scores. On 20 frozen developer-recall tasks written before anyone saw a
  result page, keyword-only produced 9/20 definite answers, semantic-only 14/20,
  and the shipped hybrid 19/20, at 1.58 mean CLI turns per definite answer.
- The similarity floor is calibrated (0.82 floor, 0.84 strong): weak
  nearest-neighbor tails stay silent instead of padding the page, and meaning
  rows are labeled as such. `-s` forces meaning-only, `--lexical` opts out.
- Embeddings maintain themselves newest-first in the background, capped at 128
  rows for the first searchable publication and then reporting partial coverage
  while history converges. Stale or mismatched vectors are never served;
  incomplete artifacts fall back to keyword results rather than blocking.
- Measured initial-embed throughput is 155.6 rows/s (128-row fixture in 822.7
  ms) at the shipped six threads. CoreML runs ~2.4x slower on this export and
  FP16 CPU buys ~14% for nearly double the model bytes; both were rejected.

### Metal (MLX) lane on Apple silicon

- New in 0.2.0: a GPU embedding lane for Apple silicon, measured at ~10.9x the
  ONNX int8 CPU lane in an interleaved A/B: 8.62s versus 0.79s for the same
  work. 0.1.1 had no Metal lane at all.
- It is the default where it can actually open: the base install carries mlx
  on supported Apple silicon, and the lane engages when the machine is idle
  enough to share the GPU. No extra, no environment variable. `AGREP_MLX=off` opts out, and `AGREP_MLX=on` pins the lane
  through load, for a foreground index the owner is already waiting on.
- The idle check runs once, when a store first picks a lane, never per batch. A
  machine too busy to share the GPU starts a CPU store rather than one that
  alternates engines by load.
- The two lanes are close but not identical. The same weights in fp16 agree
  with int8 CPU to ~0.999 cosine, enough to flip results near a threshold. Each
  store records the lane that built it and is never served by the other.
  Existing stores keep their recorded lane; `agrep reindex --full` is the one
  sanctioned lane move, discarding every row, re-deciding from the machine
  default, and riding the same background rebuild notice as any other identity
  change.
- A predicted lane that cannot open — unreachable weights, a parity refusal —
  lands a new store on CPU instead of erroring. A lane a store already recorded
  still fails loudly, because silently serving CPU vectors against Metal rows is
  the failure worth being noisy about.
- `agrep setup`'s doctor step prints the lane state on Apple silicon and
  recommends the extra when it is absent. The extra costs exactly one dependency
  (`mlx`), Apple-silicon marked because mlx ships no wheels elsewhere.
  Everywhere else, and without the extra, every path is the ONNX CPU lane.

### Post-compact recovery

- New: `agrep postcompact` serves the same session's proven pre-boundary turns,
  bounded, so an agent resumed from a lossy summary recovers the exact values
  and paths the summary dropped. `--session <id>` names the session when it
  cannot be resolved; `--json` emits one bounded packet.
- `agrep setup` can install three compaction-only integrations: Claude's
  `PreCompact`, which shapes the summary; Codex's compact-only `SessionStart`,
  which supplies context to the resumed agent; and a shared pi/oh-my-pi
  extension, which shapes their summaries, exports the exact live session
  identity, and queues recovery only on a compacted resume or retry. None fires
  on an ordinary user message. `--no-hook` skips them, `agrep remove` takes
  them out, they are never auto-repaired, and existing hooks and extensions are
  never overwritten. Removing the package without running `agrep remove` first
  can leave Claude's self-contained entry behind; the cleanup sentinel removes
  the package-dependent Codex hook and the exact pi/oh-my-pi extension copies
  it enrolled.

### Teaching your agents

- New: `agrep setup` writes a short recall block into each detected agent's
  instructions file. A local CLI is absent from an agent's context unless
  something puts it there, and this block is what puts it there. It is plain
  instructions text the agent reads like any other. Setup lists every detected
  target with its reason on one consent screen and asks before writing the
  batch; `--yes` accepts, `--no-teach` declines. `agrep remove` takes the
  blocks, the hooks, and the cleanup sentinel back out.
- There are two block texts, selected by the host filename: an `AGENTS.md` host
  gets the Codex-shaped variant, and every other file gets the default block,
  addressed to the agent whose file it is by name. The two shapes follow the
  prompting styles the vendors document: the default block carries short
  principles with the reason inline (per Anthropic's constitution and prompting
  research), the Codex block is goal/constraints structure plus one worked
  example (per OpenAI's lean-prompt guidance). Both document the core motions
  (`agrep recall` to find a prior moment, `agrep around <handle>` to open it
  at its source, `agrep postcompact` for this session's own pre-boundary tail,
  `agrep board --once` for live activity) and delegate the rest to
  `agrep --help`, which lists the options shipped with the installed version.
  Claims come from the opened source rather than from scores or snippets.
- 0.1.1 had no setup command; installation ended at the package, and the index
  built on first use.

### Performance

- Recall rendering: a 6,395-row window went from ~4.4s to 0.013s (326x) after
  the block fitter was made linear.
- Zero-hit search: 6.19s to 0.36s warm. The self-exclusion probe collapsed from
  walking a 1,146-member conversation family to a single query.
- Emoji and CJK queries: 7.9s to 1.5s, by scoping the Unicode LIKE escape to
  caseful tokens.
- Pathological queries: a 10,000-character repeated-token query took 33s and is
  now bounded by distinct token count, because conjunctive consumers dedupe the
  multiset.
- Clean install from zero: `agrep setup` completes in 3.1s and hands the
  embedding work to the background. The pinned model download is 52.1 MiB across
  three SHA-256-verified files, plus a 90.9 MiB checkpoint where the Metal lane
  engages.
- New committed performance board on isolated generated fixtures — a
  100,000-row search corpus and a 4,600-file / 14,000-row ingest store — holding
  selective engine queries to 120ms, broad 100,000-hit queries to 900ms,
  two-character lanes to 350ms, cold ingest to 3.5s, and a cold command through
  exit to 3.75s. Those committed budgets, not one machine's point measurements,
  are the release contract.

### Privacy and safety

- No telemetry, accounts, or transcript uploads. The index lives in a per-user
  data dir (`AGREP_DATA_DIR` overrides); model weights use a shared per-user
  cache (`AGREP_MODEL_DIR`) so isolated indexes do not duplicate them.
- Indexing, search, and archive capture are read-only over agent stores.
  `agrep restore` is the one command that writes back, and it refuses to
  overwrite a live file without `--force`.
- Keyword search needs no model and makes no transcript-bearing network
  request. The only network fetches are the pinned, checksum-verified model on a
  semantic install (`--no-semantic` skips it) and, on a degraded source install,
  the checksummed Rust binary.
- The explorer is now read-only by construction. `agrep ui` and `agrep serve`
  are authenticated loopback with no resume, command, raw-output, or mutation
  controls, and `agrep board` is observational for the same reason. An
  unreadable store yields an explicit partial result rather than an affirmative
  all-quiet one.

### Removed

- The 0.1.x "smart tier" and its heavy dependency set: topic and concept
  clustering, mood arcs, and Ollama-generated titles and summaries, along with
  `requirements.txt` (torch, transformers, sentence-transformers,
  scikit-learn) and the generated HTML report. They served browsing rather than
  retrieval, and cost more to install than the retrieval path they sat beside.
- The `agrep warm` command, which preloaded semantic models into the long-running
  server. There is no server to warm: the embedder loads under a worker lease
  and releases itself.

### Also in this release

- `agrep archive` keeps compressed, deduplicated snapshots of every store file
  indexed, so an agent's own retention window or a careless `rm` does not end
  the history. Plain files are stored byte-for-byte; SQLite stores are captured
  through SQLite's backup API as consistent database snapshots, because a raw
  copy of a live database can be torn mid-transaction. Off by default, opt-in at
  setup or with `archive --on`; `agrep restore` verifies the archived bytes
  against their pinned SHA-256 before writing, and its path argument accepts any
  substring of the archived path.
- `agrep board` is a new bounded live-activity window across agents, with side
  agents nested under their root; `board --once --json` is its deterministic
  snapshot for agents, distinguishing snapshot completeness from page
  truncation and exiting 2 with the exact retry argv when partial. `agrep tail`
  carries over from 0.1.x and its event shapes are now a supported interface.
- `agrep run <agent>` launches Claude, Codex, opencode, or Antigravity with
  liveness capture from process start. `agrep resume` carries over.
- `agrep doctor` is now a bounded health check, with `--deep` running expanded
  integrity, attribution, and archive checks and printing a remedy only for a
  proven gap; individual deep probes keep their own safety timeouts. New:
  `agrep audit` cross-checks adapter-discovered files against per-file intake
  accounting and independently line-counts the JSONL stores where a dumb recount
  is well defined (Claude, Codex, Kimi). `agrep status --json` is a bounded
  machine-readable index summary.
- Search, recall, and doctor JSON now carry `self_exclusion`, `freshness`, and
  `semantic_coverage`. Search JSON emits them once in a leading
  `{"kind":"agrep-meta", ...}` record, so an empty or unavailable index cannot
  hide its state from a machine caller.
- Index freshness moved off the long-running server onto a lightweight daemon
  that any search can start and that exits after inactivity. Corpus updates
  publish atomically, so a failed refresh leaves the previous good index
  available; `--no-auto` opts out and exits 2 rather than reporting an
  unverified absence.

## 0.1.1 — 2026-06-14

- Multi-agent ingest foundation: a Rust reader for six stores (Claude Code,
  Codex, opencode, Antigravity, Kimi CLI, Cline) normalizing to one row shape,
  plus a derived index for fast cold searches.
- Keyword search over that index, with `around` for the conversation surrounding
  a hit, `resume` to reopen a past session in its own agent, and `tail` for live
  agent events as JSON lines.
- A browser explorer (`agrep ui`) served from a local read-only server on
  127.0.0.1, which also refreshed the index in the background.
- An optional "smart tier" installed separately from `requirements.txt`
  (torch, transformers, sentence-transformers, scikit-learn) adding semantic
  search over Qwen3-Embedding-0.6B, topic clustering, and mood arcs. Meaning
  search required a running server; titles and summaries required a local
  Ollama model. The core install stayed stdlib-only.
- Packaging hardening: prebuilt wheels carrying the Rust ingest binary, so
  `uvx agrep`, `pipx run agrep`, and a global npm install worked without a
  clone or Cargo.
