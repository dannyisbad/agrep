# agrep

**agrep - agentic grep.** Memory for your AI coding agents. Eleven supported
agent tools keep their session records on disk - Claude Code, Codex,
opencode, Antigravity, Kimi CLI, Cline, Gemini CLI, crush, Cursor, pi, and
oh-my-pi (a pi fork sharing its format) - and none of them can search the
others. agrep reads them all into one local, ranked index, so you (and your
agents) can ask "have I hit this before?" and get the actual session back:
search it, read the conversation around a hit, or resume it in its own
agent. What each tool retains is up to that tool; agrep indexes what is on
disk when it reads.

Indexing and search are local and read-only over agent stores. Transcript content
is never uploaded.

```
$ agrep recall "why the retry backoff was capped at 30s"
── meaning match · cosine 0.91 · @3f2a91c4:88.d1e0 · claude · project=payments · 12d
    88 user: cap retries at 30s - aws support confirmed the throttle window
    88 agent: capping the backoff at 30s and adding jitter; the 60s ceiling was
       what tripped the account-level throttle in us-east-1 ...
$ agrep around @3f2a91c4:88.d1e0
    ... that turn at its source, with a surrounding window on request ...
```

An answer that was about to be re-derived from scratch by the agent that
solved it twelve days ago.

## Quickstart

Before the commands, here is everything `agrep setup` writes outside its own
data directory. Setup prints the same list as one consent screen, each entry
with its reason, and waits for your answer:

- **A recall block in each detected agent's instructions file** (`CLAUDE.md`,
  `AGENTS.md`, a Cursor skill). A local CLI is absent from an agent's context
  unless something puts it there; this block is what tells your agents agrep
  exists. It is plain instructions text the agent reads like any other.
- **Compaction-only integrations**: Claude's `PreCompact` shapes its summary;
  Codex's compact-only `SessionStart` supplies recovery context; and one shared
  pi/oh-my-pi extension shapes their summary, identifies the exact live session,
  and queues recovery only on a compacted resume or retry. None fires on an
  ordinary user message. Existing hooks and extensions are never overwritten,
  and no registration is auto-repaired.
- **A cleanup sentinel** (launchd on macOS, a systemd user timer on Linux, a
  scheduled task on Windows) that takes the instruction blocks, Codex hook, and
  pi/oh-my-pi extension copies back out if the agrep binary disappears. It only
  removes files it froze the bytes of at install time, so anything you edited
  afterwards is yours and survives.
- **The semantic model**, on installs where the optional tier is available:
  52 MiB of SHA-256-verified ONNX artifacts. On supported Apple silicon the
  background worker normally fetches a further 91 MiB Metal checkpoint, so the
  usual total there is about 143 MiB.
- **Compressed archive copies of indexed store files**, only if you accept the
  separate archive prompt, which defaults to no.

`--yes` accepts all of it without prompting. `--no-teach` skips the instruction
blocks and the sentinel, `--no-hook` skips every compaction integration, and
`--no-semantic` skips the model download for that run. `agrep remove` takes the
blocks, integrations, and sentinel back out; the index and your histories are
left alone.

```
uv tool install agrep     # or: pipx install agrep, or: npm i -g @mundy/agrep
agrep setup               # consent screen above, then builds search
agrep "race condition"    # you can search it too
```

To uninstall, run `agrep remove` **before** `uv tool uninstall agrep`. A bare
package uninstall leaves Claude's `PreCompact` entry in your `settings.json`,
still pointing at agrep; it keeps working (the file it reads lives in
`~/.claude`, not in the package) but serves guidance for a tool you no longer
have. The sentinel removes the package-dependent Codex hook and the exact
pi/oh-my-pi extension copies it enrolled.

To try it without installing anything, `uvx agrep "race condition"` (or
`pipx run agrep ...`) fetches the prebuilt package, Rust ingest binary
included, and searches. A global npm install is a shim: it warms uv's ephemeral
cache with the matching PyPI tool, and it needs `uv` or `pipx` already on the
machine to run at all.

When a hit is the session you want back:

```
agrep around 11111111 144   # root/main conversation around that hit
agrep resume 11111111       # reopen the session in its own agent, cd'd there
```

## Fast

Numbers below are full process runs - `agrep` start through exit, not an
internal scanner timer - measured on Apple silicon with the committed gates
that reproduce them.

**Meaning search at millions of rows** (`bench/semantic_cli_scale.py --check`):

| rows | warm semantic | warm hybrid | process-cold |
|---:|---:|---:|---:|
| 1,000,000 | 88 ms | - | 377 ms |
| 2,000,000 | 109 ms | 241 ms | 432 ms |
| 10,000,000 (projected) | 270 ms | - | - |

Hybrid runs the keyword and meaning lanes in one query and merges them, so
its cost over plain semantic is the keyword pass itself. The 2M fixture is a
real segmented publication - 6.13 GiB of vectors, refs,
and corpus - and a warm query touches it with **zero disk reads** and about
150 MiB of private memory. The 10M figure is an affine fit of the measured
1M/2M medians, disclosed as a projection, not a simulation.

**Keyword engine** (committed budgets, `bench/perf.py --check`): selective
queries in 120 ms; a query sweeping 100,000 hits in 900 ms; two-character
lanes in 350 ms; cold ingest of a 4,600-file store in 3.5 s; a cold command
through exit in 3.75 s. CI replays the same proofs with per-platform runner
allowances.

**GPU embedding on Apple silicon**: the Metal lane embeds the same workload
10.9x faster than the int8 CPU lane (interleaved A/B, 8.62 s vs 0.79 s), and
each store records the lane that built it so vector spaces never mix. Fresh
setup hands embedding to the background and returns in ~3 s; the first 128
rows are searchable immediately while the rest of the history is embedded.

## Search

```
agrep deadlock --agent codex     # filter to one agent
agrep "cache bug" --model gpt-5  # exact model filter (--soft for substring)
agrep -E "TODO|FIXME"            # regex (also copyable in Windows cmd.exe)
agrep -l auth                    # list matching chats, not every line (like grep -l)
agrep "memory leak" --json       # one run envelope, then one object per hit
agrep "exit code 137" --who tool # search only tool output (it's indexed too)
agrep "flaky test" --semantic    # force meaning-only search (optional semantic tier)
agrep "rust simd" --sort time    # order hits by recency instead of relevance
agrep akd --count-by-tier        # exhaustive boundary/all-terms diagnostics
```

Tool calls are searchable too: what your agents *ran* and what came back is
indexed alongside what was said, ranked below prose so command echoes never
bury the conversation about them (`agrep set tools off` for prose only).

A current, exhaustive keyword miss exits 1 like grep; an unverified or stale
absence exits 2 rather than claiming nothing matched. Default matching stays
substring-based and code-friendly:
multi-word queries bridge punctuation and underscores, while an independent
any-order lane keeps scattered evidence reachable. Boundary quality affects
rank, never eligibility, so short fragments still behave like grep. `-w` and
`-E` retain explicit whole-word and regex behavior. The full lane, scoring,
Unicode-boundary, count, and early-termination contract is in the
[search-ranking reference][search-ranking].

Agent-driven shells automatically get a byte-budgeted page of one-line hits with
digest-checked `@session:turn.digest` handles: the digest is a content claim, so
a handle whose row moved is either rescued and disclosed or refused, rather than
serving different text under the same citation. `AGREP_PROFILE=compact` can force
that profile when a harness exposes no agent fingerprint. The stderr hint `agrep
--more <handle>` serves the next page from a short-lived frozen top-40 snapshot;
`agrep around @handle` and `agrep recall @handle` pull the corresponding context
directly. `--classic` or `AGREP_PROFILE=classic` selects the human renderer and
disables the compact profile's hybrid retrieval; an interactive query that finds
nothing may still try the meaning lane, and `--lexical` is what turns that off.
`-s` requests semantic search explicitly.
`-c` remains an exhaustive, one-integer grep count. Prose-shaped compact queries
may add labeled meaning evidence when the optional semantic tier is ready;
`--lexical` opts out and `-s` forces semantic-only search.

Literal grep remains exhaustive across real side chats. Ranked meaning/recall
surfaces instead treat a root chat and its spawned children as one conversation
family, retaining whichever root or child holds the strongest evidence; this
prevents one large agent swarm from consuming the result page without deleting
the child answers from the index. Meaning search returns up to 10 confident
conversation families by default (`-n` overrides the cap; weak nearest-neighbor
tail rows stay silent). `--all-side-chats` expands sibling children into independent ranked
slots; ordinary keyword search is already exhaustive. Codex's automatic guardian
approval-review rollouts are internal control traffic and are not indexed as chats.

## `around`

Search tells you *which* session touched a thing; `around` tells you *what
happened* - the error, the attempts, the fix - for a few KB instead of a whole
transcript:

```
agrep around 11111111 144        # ±4 turns around turn 144 of that session
agrep around 11111111:144 -C 10  # wider window; colon form pastes from --json
agrep around @11111111:144       # compact result handle
agrep around 11111111 144 --full # same-window all-event forensic view
```

Root/main prose is the default for positional and latest-session reads, and for
context inside human recall results; generic tool and delegated-workflow events
stay out of the agent's attention path. A selected tool-result handle keeps that
exact event visible and centers its preview on an input or output match.
`--who tool` or `--tool-output N` explicitly opts into tool evidence, while
`--full` restores the same-window forensic stream.
Ingest safety caps remain labeled.

## `postcompact`

When an agent's conversation compacts, the summary is lossy - it keeps what
was decided but drops the exact values and paths those decisions rest on.
`agrep postcompact` serves the same session's proven pre-boundary tail,
bounded, so a resumed agent recovers the dropped detail instead of guessing:

```
agrep postcompact                 # this session's recent pre-compact turns
agrep postcompact --session <id>  # name the session when it can't be resolved
agrep postcompact --json          # one bounded structured packet
```

The compaction integrations (installed by setup, `--no-hook` to skip) make the
summary itself carry the recovery route, so resumed agents reach for it without
being told. The pi/oh-my-pi extension also exports the exact live session ID,
lets their summarizer carry the same recovery schema, and injects hidden
next-turn guidance only when it observes a real compaction boundary. In an
external benchmark: 0 of 35 resumed from a plain summary; **151 of 151**
recovered with the hook-shaped summary, every one calling `postcompact` first.
The same guidance also over-triggered, sending agents to `postcompact` in 23 of
25 controls whose summary already held the answer, and the truncated context
was constructed rather than produced by a real compaction event.
[bench/ADOPTION.md](bench/ADOPTION.md) has the tables and the reproduction
limits.

## Meaning search

Semantic search runs a 47M-parameter embedder
(`granite-embedding-small-english-r2`) in one of two lanes. The CPU lane runs it
as int8 ONNX and is available everywhere the optional tier installs; supported
Apple silicon instead creates fp16 stores on the Metal lane described below.
One-off use receives a short worker lease; repeated queries keep it resident
longer.

It earns its place on measured recall quality: on 20 frozen
developer-recall tasks written before anyone saw a result page, keyword-only
search answered 9/20 definitively, semantic-only 14/20, and the shipped
hybrid **19/20**, at 1.58 mean CLI turns per definite answer.

The Metal lane embeds on Apple silicon GPUs about ten times faster, and it
ships in the default install there with no extra to know about. New embedding
stores use it automatically when the machine is idle enough to share the GPU; `AGREP_MLX=off`
opts out, `AGREP_MLX=on` pins it even under load. It shares the corpus
economics rather than result identity: the same weights in fp16 agree with the
int8 CPU lane to ~0.9993 cosine, so near-threshold results may differ between lanes. Each
store records the lane that built it and is never served by the other one - an
existing CPU store keeps embedding on CPU until you ask for the move with
`agrep reindex --full`, the one operation that re-decides the lane (it rebuilds
the whole history in the background).

Embeddings maintain themselves newest-first in the background:
the first searchable publication is capped at 128 rows, then reports partial
coverage until the backfill finishes. Background work adapts its batch size, priority,
and CPU use to activity, battery, and memory pressure. Old or mismatched vectors
are never served. The model is English-only and embeds the first 1,024 model tokens
of each message or reply row; keyword search still indexes full text. If semantic
artifacts are incomplete, automatic recall falls back to keyword results rather
than waiting.

## Agentic-first design

agrep is built for the agent as its first reader: exit codes an agent can
branch on, byte-budgeted result pages, digest-checked `@session:turn.digest`
handles that paste straight into the next command, and honest partial-result
disclosures instead of silent truncation. The same approach shapes the
instruction block [setup writes](#quickstart).

There are two block texts, and the host filename picks between them. An
`AGENTS.md` host gets the Codex-shaped variant; every other file gets the
default block, which addresses the agent whose file it is by name. This is a
filename convention, not model detection. The two shapes differ because the
vendors document different prompting styles:

**The default block carries its reasons.** Anthropic trains Claude on principles
that carry their own justification - the published [constitution][cc] is
20,000 words of explained intent, and their research ([Teaching Claude
why][tcw]) found that principles-with-reasons generalize where bare rules
either get followed mechanically or loopholed. Their [prompting
guidance][c4bp] says it directly: explain why a rule exists and the model
generalizes from the explanation. So the default block reads like a policy
with its rationale inline: *why* reaching into history beats re-deriving,
*why* a compacted session should use `postcompact` instead of `recall`, *why*
recalled text is evidence rather than instructions.

**The Codex block is structure and one worked example.** OpenAI's [model
guidance][omg] and [Codex prompting guide][cpg] pull the other way: lean
prompts (their own evals improved 10-15% when system prompts shrank),
each instruction stated once, goal/constraints/success-criteria structure,
and examples only where they encode a requirement or correct a measured
gap. So the Codex block is a terse rule list plus one worked transcript of
the post-compaction recovery sequence, which is the routing failure the
adoption benchmark measured.

The compaction integrations address different lifecycle APIs for the same
reason. Claude's `PreCompact` payload instructs the *summary writer* to preserve
verbatim anchors and keep the recovery route first; Codex's compact-only
`SessionStart` payload addresses the *resumed agent* with context, imperatives,
and an example. pi and oh-my-pi load one shared extension: `session.compacting`
adds the summary schema, `session_compact` steers a retry only if that schema
was omitted, and a compacted `session_start` queues hidden recovery for the next
turn. The extension exports their exact session ID so bare `agrep postcompact`
resolves the right conversation; it never hooks ordinary user messages.

Both blocks stay short and delegate the full command surface to
`agrep --help` (and each command's own `--help`), which lists the options
shipped with the installed version.

[cc]: https://www.anthropic.com/constitution
[tcw]: https://www.anthropic.com/research/teaching-claude-why
[c4bp]: https://docs.claude.com/en/docs/build-with-claude/prompt-engineering/claude-4-best-practices
[omg]: https://developers.openai.com/api/docs/guides/prompt-guidance
[cpg]: https://developers.openai.com/cookbook/examples/gpt-5/codex_prompting_guide

## Private by design

- No telemetry, accounts, or transcript uploads. Your transcripts and index stay
  on your machine.
- Indexing, search, and archive capture are read-only over agent data. The explicit
  `agrep restore` command is the only feature that writes a selected archive back
  to an agent store; it refuses to overwrite a live file unless given `--force`.
- Keyword search needs no model or transcript-bearing network request. Setup on a
  supported semantic install downloads a pinned, SHA-256-verified model from
  Hugging Face unless `--no-semantic` is given; later first use retries a missing
  model. A degraded source install may separately offer to fetch its
  checksummed Rust binary from GitHub.
- The index lives in a per-user data dir (`AGREP_DATA_DIR` overrides). Model
  weights use a shared per-user cache so isolated indexes do not duplicate them;
  `AGREP_MODEL_DIR` overrides that cache root.
- Setup's compaction integrations fire only at compaction lifecycle boundaries:
  Claude `PreCompact`, Codex compact-only `SessionStart`, and the shared
  pi/oh-my-pi compaction extension. They are skipped with `--no-hook` and
  removed by `agrep remove`. No existing hook or extension is replaced, and
  nothing fires per prompt. Run `agrep remove` before uninstalling the package:
  a bare package uninstall can leave Claude's self-contained entry behind (see
  [Quickstart](#quickstart)).

## Live view

`agrep tail` streams supported agents' live activity as JSON lines
on stdout - one compact object per event, flushed per line, so it pipes into a
statusline, a dashboard, or another agent's monitor. The event shapes are a
supported interface. Today the hook-free live readers cover Claude, Codex,
OpenCode, Antigravity, and Cursor; indexing covers every adapter.

```
agrep tail                 # turn ends, all supported agents
agrep tail --events all    # firehose: user, reply, tool, tool_done, done, queued, subagent_result
agrep tail --agent claude  # one store; --session <substr> narrows further
agrep tail --snapshot      # current state of every supported live session
```

`agrep board` is the human view of the bounded live-activity window, with each
session's state, model, and last events. It is not the indexed recent-history
list; use `agrep chats` for that. `↑↓` selects, `enter` focuses one session as
a scrollable full-screen feed, and `a` cycles an agent filter. Working side
agents sit beneath their root; completed side agents collapse into a count by
default. It is observational only: no resume, command, raw-output, or mutation
controls. A new watcher discovers journal-backed sessions active in the last
90 seconds; a quiet long tool call appears on its next store write. Unreadable
stores produce an explicit partial result, never an affirmative all-quiet state.

Agents can request one bounded, deterministic snapshot:

```
agrep board --once --json --sort updated
agrep board --once --json --sort updated --agent claude --state active --roots -n 3
```

The object distinguishes snapshot completeness, page truncation, and trimmed
feed events. A partial snapshot exits 2 and carries the exact retry argv. Its
`@session` handles paste directly into `agrep around`, `agrep resume`, and other
session selectors.

`agrep ui` opens the private read-only history explorer. Its Board view carries
the same live overview into the browser, nests side agents under their parent
chat, and links indexed live chats back to History. It polls only while visible;
it has no resume, command, raw-output, or mutation controls. `agrep serve` starts
the same authenticated loopback UI without opening a browser.

`agrep run <agent> [-- <agent args...>]` launches Claude, Codex, OpenCode, or
Antigravity while recording child-process liveness from process start.
`--cwd <path>` selects its working directory; arguments after `--` pass through
to that agent.

## `archive` and `restore`

Agent CLIs delete their own history - retention windows, uninstalls, a careless
`rm`. If you explicitly opt in during setup (or run `agrep archive --on`), agrep
keeps a compressed, deduplicated snapshot of every store file it indexes. Plain
files are stored byte-for-byte. SQLite stores are captured through SQLite's
backup API, which yields a consistent snapshot of the database rather than a
copy of the source file's bytes, because a raw copy of a live database can be
torn mid-transaction. Archive retention is off by default:

```
agrep archive --status   # files, raw vs stored size, ratio
agrep restore <path>     # the archived bytes, back where the agent expects
agrep archive --keep 5   # versions kept per source file (0 = never prune)
agrep archive --off      # disable retention again
```

Restore verifies the archived representation against its pinned SHA-256 before
writing a byte, so a damaged archive refuses instead of resurrecting a corrupt
store. Whether the agent can then resume the session depends on that agent
accepting the format it wrote. `<path>` can be any substring of the archived
file's path. Restore refuses to overwrite a live file unless you `--force`.

## Where it reads

agrep discovers sessions in each agent's normal per-user store and opens those
sources read-only for indexing. In this table, `~` means your home directory;
on Windows that is normally `%USERPROFILE%` (the `~` is notation, not cmd.exe
syntax).

| Agent | Store |
|---|---|
| Claude Code | `~/.claude/projects/<slug>/*.jsonl` |
| Codex | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` |
| opencode | `~/.local/share/opencode/*.db` (SQLite) |
| Antigravity | `~/.gemini/antigravity-cli/brain/<uuid>/` |
| Kimi CLI | `~/.kimi/sessions/<workdir-hash>/<uuid>/` |
| Cline | each editor's `globalStorage/saoudrizwan.claude-dev/tasks/`, plus `~/.cline/data/` |
| Gemini CLI | `~/.gemini/tmp/<hash>/chats/session-*.json` |
| crush | `~/.local/share/crush/crush.db` (SQLite; also legacy `~/.crush/`), plus each per-project `crush.db` at the `data_dir` its registry (`projects.json`) names - wherever that directory lives |
| Cursor | `Cursor/User/globalStorage/state.vscdb` under the editor's per-OS app-data dir (SQLite) |
| pi / oh-my-pi | `~/.pi/agent/sessions/<cwd-slug>/*.jsonl` and `~/.omp/agent/sessions/<cwd-slug>/*.jsonl` |

On Windows, the primary opencode and crush roots are
`%LOCALAPPDATA%\opencode\` and `%LOCALAPPDATA%\crush\`; if `LOCALAPPDATA` is
unset, both fall back under `%USERPROFILE%\AppData\Local\`. On macOS and Linux,
`XDG_DATA_HOME` overrides the corresponding `~/.local/share/` root when set.

Whichever of these exist get indexed; missing ones are skipped. Prebuilt wheels
cover Windows, macOS, and glibc 2.28+ Linux; musl and older-glibc Linux require a
source-built Rust binary. All eleven agent tools are searchable through grep
and recall; live tailing additionally covers claude, codex, opencode,
antigravity, and cursor, and native resume covers claude, codex, opencode, and
antigravity.

Detected but not yet indexed: Copilot CLI (`~/.copilot/session-state`) and
qwen-code (`~/.qwen`). agrep surfaces these - with a session count - in
`doctor`, but doesn't parse their formats yet.

`agrep audit` cross-checks the files the adapters discovered against the
parsers' per-file intake accounting, and independently line-counts the JSONL
stores where a dumb recount is well defined (Claude, Codex, Kimi). A store the
adapters never discovered is outside what audit can see, and the other formats
get the accounting check without an independent census. `agrep audit --strict`
also fails on files that have not yet been tallied; `agrep index --full` seeds
that accounting.

## The tiers

Supported wheels include the core binary, so they need no local build. Semantic
availability depends on platform-compatible runtime wheels and a first-use model download.

| Tier | Needs | Provides |
|---|---|---|
| **Core** | Python 3.10+ (Rust only if building from source) | Grep, `around`, recall, resume, archive/restore, live view, event trees. |
| **Semantic** | Ships automatically where upstream ONNX Runtime provides a compatible wheel (numpy + onnxruntime + tokenizers), plus a pinned 52 MiB model fetched on first need, and a further 91 MiB checkpoint where the Metal lane engages. Py3.13 needs macOS 13+; Py3.14+ currently needs Apple silicon and macOS 14+. Older macOS, Intel Mac Py3.14+, and Windows ARM64 Py3.10 stay safely core-only. | Meaning search: eligible compact prose searches and recall queries can combine lexical and meaning evidence; `-s` forces meaning only and `--lexical` opts out. |

`agrep doctor --deep` verifies the semantic runtime, model, and embeddings and
prints a remedy only for a proven gap. For a normal uv-managed release install,
semantic dependencies can also be installed explicitly with:

```
uv tool install --force "agrep[semantic]"
```

Reproducible latency, scale, throughput, and footprint measurements live in
[`bench/`][bench], including the [full-exit semantic gate][semantic-cli-scale],
[embedding throughput][embed-speed], and the
[semantic scanner scale gate][semantic-scale]. The release contract is the
committed benchmark budgets rather than any one machine's point measurements.

`agrep doctor` is a bounded health check; `agrep doctor --deep` runs expanded
integrity, attribution, and archive checks and reports an exact command for any
proven gap. Individual deep probes keep their own safety timeouts, so a wedged
store cannot hang the report. Deep output lists the shared model cache
separately when it is outside `AGREP_DATA_DIR`.

## Freshness

The first search builds the local index when needed. Later searches start or
reuse a lightweight freshness daemon and can serve the last complete publication
while it catches up; corpus updates publish atomically, so a failed refresh leaves
the previous good index available. The daemon exits after search inactivity and
can be restarted by the next query. `--no-auto` prevents that search from
building the index or checking/starting freshness work, so an empty result exits
2 because absence was not verified.

Semantic freshness is independent and generation-bound. A small newest-first
publication becomes searchable first, then low-priority, resource-aware passes
backfill older rows. Partial coverage is disclosed, and stale or mismatched
vectors are never served; automatic hybrid paths keep the keyword result instead.

## Commands

```
agrep <pattern>          # grep your history (first run builds the index)
agrep recall "<q>"       # prose recall; eligible queries can add meaning evidence
agrep recall "<q>" -s    # force meaning search (short-lived local worker)
agrep pack "<q>" "<q>"   # recall across several queries, deduped, one budget
agrep around <id> <turn> # the conversation around a hit
agrep around @session    # latest indexed context in a printed chat/session
agrep resume <id>        # reopen a past session in its own agent, cd'd there
agrep run <agent> [-- <args...>] # launch captured; pass agent arguments through
agrep tail               # live agent events as JSON lines (--snapshot: state now)
agrep chats [topic]      # indexed chats; topic searches identity + contents
agrep board              # bounded live-activity window across agents
agrep ui                 # private read-only History + live Board explorer
agrep serve              # serve that explorer without opening a browser
agrep doctor             # bounded health check; --deep verifies all tiers/artifacts
agrep status --json      # bounded machine-readable index summary
agrep audit              # verify per-file ingest accounting and coverage
agrep setup              # build search; consent screen for instructions, hooks, model
agrep remove             # take the instruction blocks, hooks, and sentinel back out
agrep index              # ingest + refresh keyword search; cached semantics can follow
agrep reindex            # refresh ingest + keyword search + embeddings
```

Search JSON starts with one `{"kind":"agrep-meta", ...}` run envelope carrying
`self_exclusion`, `freshness`, `semantic_coverage`, completeness, and filter
coverage; zero or more row-only hit objects with digest-bound `handle` fields
follow. Empty and unavailable results use the same one-record envelope, so
state never disappears with the rows. Recall and doctor carry their
corresponding run fields in their single JSON object; grep exit codes remain
authoritative.
Finite recall JSON budgets start at 2,048 bytes so the cap stays hard without
dropping the required machine envelope.

(Yes, the name collides with the 1992 approximate-grep `agrep` by Wu & Manber
and with TRE's - here it means *agentic grep*, an unrelated modern tool.)

## How it's built

A Rust ingest (`crates/agrep-core` + `crates/agrep-cli`) reads the stores and
writes a compact normalized index (`messages.jsonl` / `replies.jsonl` - one
row shape across all agents); a derived SQLite FTS5 index makes cold CLI
searches fast; the Python layer (`py/`) provides command orchestration and terminal
engines; a small pinned ONNX embedder (`py/embedder.py`) powers meaning
search on CPU. The Rust ingest is the only required native component for the
core experience.

To hack on it, clone and use the same commands as `python cli.py <cmd>` (needs
Rust for the ingest binary - https://rustup.rs). See
[CONTRIBUTING.md][contributing] for the layout and how to add an adapter for
another agent; [`py/README.md`][python-readme] covers the Python side's
normalized data contract and the semantic lane.

## License

MIT - see [LICENSE][license].

[bench]: https://github.com/dannyisbad/agrep/tree/master/bench
[contributing]: https://github.com/dannyisbad/agrep/blob/master/CONTRIBUTING.md
[embed-speed]: https://github.com/dannyisbad/agrep/blob/master/bench/EMBED_SPEED.md
[license]: https://github.com/dannyisbad/agrep/blob/master/LICENSE
[python-readme]: https://github.com/dannyisbad/agrep/blob/master/py/README.md
[search-ranking]: https://github.com/dannyisbad/agrep/blob/master/docs/SEARCH_RANKING.md
[semantic-cli-scale]: https://github.com/dannyisbad/agrep/blob/master/bench/SEMANTIC_CLI_SCALE.md
[semantic-scale]: https://github.com/dannyisbad/agrep/blob/master/bench/SEMANTIC_SCALE.md
[semantic-worth]: https://github.com/dannyisbad/agrep/blob/master/bench/SEMANTIC_WORTH.md
