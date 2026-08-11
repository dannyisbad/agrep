# Contributing

## Layout

```
cli.py                 the CLI front door (search / around / recall / resume / doctor / setup / tail / ...)
agrep/                 the PyPI package shim: maps this flat tree into a wheel (see pyproject)
crates/agrep-core/     Rust: read each agent's store, normalize, write the index
  src/ingest/          one adapter per agent (claude, codex, opencode, antigravity,
                       kimi, cline, gemini, crush, cursor, pi) + registry.rs (the ADAPTERS list)
crates/agrep-cli/      the `agrep-ingest` binary crate (driven by `cli.py index`) + fixture goldens
py/                    the Python layer: terminal engines and semantic lane (see py/README.md)
  search.py            the terminal grep (`agrep <pattern>`)
  recall.py            budget-capped context packs (`agrep recall` / `pack`)
  around.py            the conversation around one turn
  explore.py           read layer over the index (search/window retrieval)
  resume.py            `agrep resume` - the CLI side of native resume
  indexer.py           background incremental index refresh
  hookless/            observation plus the checked adapter-capability manifest
```

Data flows one way: Rust ingest → the data dir → Python engines → terminal output.
The data dir defaults to the per-user OS location (`AGREP_DATA_DIR` overrides);
it is never committed. Core search is model-free and needs no optional semantic
runtime; the terminal engine itself is Python.

The standing dependency map and accepted structural debt live in
`docs/ARCHITECTURE.md`; `py/test_layering.py` enforces its import direction.
Cross-process ownership and publication rules live in `docs/COORDINATION.md`.

## The hookless/ subpackage

`py/hookless/` observes any agent on the box without touching its config:
process discovery (`procscan.py`), live session state (`live.py`), agent launching
with ground-truth capture (`capture.py`),
and per-agent resolution glue (`native.py` - resume commands + real-cwd
resolution from each agent's store).

The law, enforced by selftest: hookless imports nothing from the product layer
(corpusdb/search/explore/...). Product code consumes hookless, never
the reverse.

`py/hookless/agent_registry.json` is the checked adapter vocabulary and the
capability contract for live observation, native resume, agent-context
detection, and `py/teach.py` enrollment targets. Python loads it directly.
Rust pins its ordered adapter names and schema, Python pins the capability
implementations, and the wheel test requires the manifest and loader.

`py/hookless/locators.py` is the hookless authority for shared store roots and
content-name predicates. Within hookless, static roots live there once.
OpenCode and Cursor remain environment- and platform-aware algorithms;
`test_agent_store_contract` builds a synthetic home and compares their accepted
files with the Rust `stores --paths` census. This contract covers roots and
initial filename acceptance, not event parsing, polling cadence, or cwd
extraction. `AGREP_HOME` is the shared highest-priority discovery override for
Rust ingest, live observation, and native resume; the contract tests both the
normal platform home and a poisoned HOME/XDG override. Locator changes need the
test on macOS and Windows.

## Running it

```
python cli.py reindex    # reingest + rebuild search and semantic embeddings
python cli.py doctor     # bounded health; --deep verifies every tier/artifact
```

Before sending a change, run every local contract layer with Python 3.10+.
One command runs all of them and prints the counts:

```
.venv/bin/python gate.py             # cargo, all of py/, selftest, all of bench/
.venv/bin/python gate.py --list      # the passes, and what is deliberately excluded
```

It exits non-zero if anything failed, and it is the acceptance gate: a roster
that names a subset is a spotlight, so `gate.py` runs the whole tree rather
than a chosen floor. Two passes (`bench/perf`, `bench/resources`) measure this
machine's CPU and memory and breach on a busy box; `--no-host-gates` skips
them and names them in the summary, never silently.

A new module in `py/` needs its row in `LAYERS` (`py/test_layering.py`) in the
same commit that adds the module. The table must name every shipped non-test
module exactly once, so a module without a row leaves the tree red — which is
what the gate is for, but the roster says it here so you read it first.

One pass is not a test suite: `global-state sweep` runs every `py/test_*.py`
module in a single interpreter and fails if any of them hands the next one a
changed signal disposition, environment key, cwd, `sys.path`, locale, resource
limit, or an unbounded atexit queue. Three separate outages came from that
shape. What the sandbox legitimately establishes is an allowlist in
`py/global_state_sweep.py` with the reason beside it, not a tolerance.

The summary separates reds that predate the gate from new ones, so read the
`new:` line first. A pass in `gate.py`'s `KNOWN_RED` prints why it is red and
at which commit that was verified; it still fails the gate. Delete its entry
when it goes green rather than letting the annotation outlive the defect.

The individual commands, for running one layer at a time. On macOS/Linux:

```
cargo build --release
cargo test --release
cargo clippy --workspace --all-targets -- -D warnings
cargo fmt --all -- --check
.venv/bin/python -m unittest discover -s py -p "test_*.py" -v
.venv/bin/python py/selftest.py                     # summary must say 0 failed
.venv/bin/python bench/perf.py --check
.venv/bin/python bench/resources.py --check
git diff --check
```

On Windows PowerShell:

```
cargo build --release
cargo test --release
cargo clippy --workspace --all-targets -- -D warnings
cargo fmt --all -- --check
.\.venv\Scripts\python.exe -m unittest discover -s py -p "test_*.py" -v
.\.venv\Scripts\python.exe py/selftest.py            # summary must say 0 failed
.\.venv\Scripts\python.exe bench/perf.py --check
.\.venv\Scripts\python.exe bench/resources.py --check
git diff --check
```

Build Rust before Python discovery: the cross-language locator test executes
the release binary. Python discovery is required even when selftest passes;
the two commands cover different contracts. If the repository environment is
elsewhere, substitute its Python 3.10+ executable for `.venv`.

The bench suites need one process each: `bench/resources.py` shadows
`py/resources.py` for whatever imports it second. `gate.py` already does this.

## Adding an agent adapter

Each adapter in `crates/agrep-core/src/ingest/` turns one agent's on-disk store
into two streams: `Message`s (the human turns + the agent's reply text) and
`Event`s (tool calls and subagent activity). To add one:

1. Find where the agent journals sessions under `~` and what one record looks
   like. The ten existing adapters are worked examples for JSONL stores
   (claude, codex, antigravity, kimi, gemini, pi) and SQLite ones (opencode,
   crush, cursor); cline reads per-editor task dirs.
2. Implement the `Adapter` trait following the shape in `claude.rs`. Emit only
   real human turns as messages (filter the harness wrappers via `is_wrapper`);
   attach tool/subagent activity as events.
3. Add it to the `ADAPTERS` slice in `ingest/registry.rs`, then add the same
   ordered name to `py/hookless/agent_registry.json`. Cross-language tests make
   either half fail when the pair drifts.
4. For the **live view**, add its capability state to the manifest. When
   supported, add its store rule to `py/hookless/locators.py`, then add the
   tailing logic and its tick to `py/hookless/live.py`
   (JSONL stores are tailed by byte offset; databases are polled). This is
   separate from Rust ingest and reads the same stores in real time. Unsupported
   capabilities require a concrete reason in the manifest. Extend the synthetic
   locator census whenever a root or content predicate changes.
5. For **native resume**, mark the capability in the manifest. When supported,
   add the resume command and cwd resolver to `py/hookless/native.py`.
6. For **agent context**, record the exported environment keys in the manifest,
   or an explicit unsupported reason when no stable shell marker exists.
7. For **agent teaching**, point the adapter capability at its declarative
   manifest target. Agents that can consume recall but have no ingest adapter
   belong in `teach_clients`. Add a portable root rule to `py/teach.py` only
   when the existing `home` and `opencode_data` roots cannot express the target.
8. Add a **fixture + golden** (see below). A new adapter without one won't
   catch a regression, and reviewers can't see what shapes it handles.

Keep adapters read-only. agrep must never write to, move, or delete an agent's data.

## Fixture goldens

`crates/agrep-cli/tests/golden.rs` runs the real ingest binary over a synthetic
fixture home per adapter and diffs its normalized output (sorted
messages/replies/sessions + the DB-backed per-session event streams) against a checked-in
golden. This freezes current ingest behavior so a parser change that shifts
output is loud instead of silent.

```
cargo test -p agrep-ingest --test golden                    # check
UPDATE_GOLDEN=1 cargo test -p agrep-ingest --test golden    # regenerate (intended change)
```

The seam is `AGREP_HOME` (`ingest::home()` honors it first): the harness points
discovery at `tests/fixtures/<adapter>/home` and writes to a throwaway tempdir,
so tests never touch your real stores. SQLite fixture stores are built at test
time from checked-in `.sql` seeds (auditable; no binary committed).

**Fixture privacy is a hard rule.** Fixtures are hand-written or heavily
scrubbed synthetic content - never a real transcript. If you derive one from a
real store, replace ALL message text, paths, ids, and models with synthetic
values, and re-read every file before committing. Run
`python bench/validate_repo_privacy.py --worktree` before committing and
`python bench/validate_repo_privacy.py --index --all-refs` before every release.
The latter checks staged bytes and every Git blob, commit, and tag reachable from
any ref, not merely the mutable checkout or current branch.

A clean working tree is not a clean history. If private material reaches a
commit, rebuild the public candidate from a clean root and verify every reachable
blob before publishing; deleting it in a later commit is insufficient. Configure
the clean root's author identity deliberately before committing, using the public
name and email you intend to publish rather than a machine-generated local address.

## Conventions

- No per-prompt or gating hooks, ever. agrep's premise is passive reading - it
  must work for sessions it didn't start and agents it didn't install into.
  The sanctioned exceptions are setup's post-compact recovery integrations
  (opt out with `--no-hook`; removed by `agrep remove`; never auto-repaired;
  compaction lifecycle only). Reading must never depend on them.
- Anything that needs a model is optional. Core indexing and keyword search must
  stay model-free.
- Don't hardcode usernames or absolute paths; derive from `home()` / `Path.home()`.
- Verify changes against real store data, not just synthetic fixtures.
