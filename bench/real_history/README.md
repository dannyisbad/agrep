# Regular tests on real histories

Every serious ingest bug this cycle came from transcript shapes the synthetic fixtures
lacked: advisor mirrors flagged `synthetic`, pi/omp header ids that drift from filenames,
Codex Desktop `item_completed` submissions, Claude cwds under container directories,
opencode's `session_v2` migration. This directory holds two lanes that keep the parsers
honest against those shapes without ever committing a transcript.

## Lane A: committed scrubbed shape samples (runs in every test pass)

`bench/fixtures/real_shapes/` holds one record per distinct record shape, per adapter,
extracted from the operator's own stores and scrubbed. `py/test_real_shapes.py` copies
them into a sealed sandbox home, indexes them with the checkout's CLI and binary, and
asserts the invariants below. The lane is hermetic: no network, no daemon, no resident,
embeddings off, nothing outside the temp dir.

A *shape* is the record type plus its sorted key set, recursed through message/content
containers (`shapes.shape`). Each sample keeps the structural context its file needs to
ingest: pi session headers, Codex `session_meta`/`turn_context` and the submission event
that attests a kept user message, the first cwd-bearing and first human record of a
Claude file. pi records are relinked into one active branch.

### Refreshing the fixtures (local only)

```
python bench/real_history/refresh_shapes.py            # reads $HOME stores read-only
python bench/real_history/refresh_shapes.py --per-shape 2 --max-files 60
```

The sampler discovers files through `agrep-rs stores --paths` under a sealed environment,
so only what the registry would parse is read. Everything else is fail-closed scrubbing
(`scrub.py`):

- every string becomes deterministic same-length filler (whitespace kept, classification
  markers such as `<command-name>` or `# AGENTS.md` kept, UTF-8 width classes kept);
- ids become consistent fakes of the same format; timestamps shift by one run-wide offset
  so the earliest sample lands on 2001-01-01 and every interval survives;
- paths collapse to `/home/u/projects/<word>` with words from the fixed neutral list;
- only allowlisted (key, value) vocabulary survives: record types, roles, statuses, tool
  names, model names are replaced by `model-N`;
- strings are capped at `--max-string-chars`, records over `--max-record-bytes` and files
  over `--max-file-bytes` drop their largest non-context records (recorded in the manifest);
- opencode is sampled into `opencode/seed.sql` from a backup of the live DB; only the two
  transcript tables are read, credential tables are never opened.

Before anything is written the sampler scrubs, then runs `bench/validate_repo_privacy.py`
over the output plus a token scan built from the home directory name, the project names
`agrep chats --json -n 5000` reports from the installed release, and any `--name-token`.
A single hit deletes the output and exits 1. Review the samples by hand before committing;
the manifest lists uncovered shapes so a reviewer can decide whether to raise
`--max-files`.

## Lane B: full-history run over a frozen copy of the real stores (local, on demand)

```
python bench/real_history/run.py --freeze                 # snapshot, index, check, delete
python bench/real_history/run.py --freeze --keep --report /tmp/real-history.json
python bench/real_history/snapshot.py ~/.agrep-real-history/frozen  # or freeze once ...
python bench/real_history/run.py --home ~/.agrep-real-history/frozen # ... and rerun against it
python bench/real_history/snapshot.py ~/.agrep-real-history/frozen --delete
```

Agents append to the live stores while a run takes minutes, which breaks every invariant that
compares two readings of a file (`warm_reindex_identity`, `source_bounds`, `audit --full`).
`snapshot.py` freezes the stores first: every store root `agrep-rs stores --paths` reports is
cloned copy-on-write (`cp -c` / clonefile on APFS, `cp --reflink=always` on btrfs/xfs; the tool
refuses other filesystems and other volumes), SQLite stores are copied through the backup API
from a read-only connection (never a raw copy of a live db plus WAL), and the manifest
`.agrep-snapshot.json` records what the clone really allocated, measured as the free-space delta;
a clone that silently fell back to copying bytes is deleted and refused. The live stores are
only read. Running `--home ~` directly still works but its findings carry the live caveat.

This is the one sanctioned exception to the sandbox-HOME rule. Safeguards:

- `env -i` style sealed environment; `HOME`/`AGREP_HOME` point at the frozen (or real) home only
  so store discovery works; `AGREP_DATA_DIR` is a fresh dir under `~/.agrep-real-history`
  and the run refuses to start if it resolves into any production data dir: the indexed home's,
  the snapshot source's, or the operator's own;
- `AGREP_DATA_READONLY` names the source home's production dir as a second fence;
- `AGREP_NO_DAEMON`, `AGREP_NO_RESIDENT`, `AGREP_NO_SEM_WORKER`, `AGREP_NO_FETCH` set,
  embeddings off in the scratch `settings.json`, `TMPDIR`/`XDG_RUNTIME_DIR` in scratch;
- never `setup`, `teach`, `remove` or `doctor --fix`; the only commands are `index`,
  `search --json`, `chats --json`, `around --json`, `audit --full --json`,
  `agrep-rs stores --paths`;
- free-space guard over the content bytes the index will read (the files `stores --paths`
  lists), not `du` of the roots: clones share their blocks with the live store, so `du` would
  count the snapshot twice, while the run only allocates derived data (measured 0.26 x content).
  Refuses unless free disk is at least `--min-free-ratio` (1.0) times the content;
- the scratch dir and a `--freeze` snapshot are deleted at the end unless `--keep`; background
  children bound to the scratch data dir are reaped.

## Invariants (shared by both lanes, `invariants.py`)

| check | statement |
|---|---|
| `intake_identity` | per file `seen == rows + agent_rows + Σskips + errors`, `rows <= seen`, no negatives |
| `source_bounds` | per file `rows <= text-bearing non-synthetic candidates`; synthetic mirrors are `sidechain`/`unreferenced` skips; oracle `seen` equals the tally. The oracle reads the generation the tally covered: the whole file while its `s:<mtime>:<size>` key still matches, the recorded prefix when a live file only grew since; rewritten, shrunk or compressed files are counted `uncomparable`, not blamed |
| `file_coverage` | every discovered content file has a tally; a file with rows names a published session or alias |
| `per_adapter_row_bounds` | published rows per adapter never exceed tallied rows |
| `duplicate_ids` | message ids and `(session, turn)` are unique |
| `family_closure` | messages and sessions name the same set; every session has a `session_family` row; roots are fixed points; `side` matches `parent`; aliases are unique, never an indexed id, and share their session's root |
| `project_labels` | name-form labels are never a generic container (`projects`, `private`, `tmp`, `Users`, `home`, ...); pi publishes the raw cwd by contract and is exempt |
| `warm_reindex_identity` | an unchanged rerun leaves messages/sessions/replies/intake/boundary/event artifacts byte-identical |
| `handle_round_trip` | sampled `chats`/`search` handles reopen via `around --json` at the same session, turn and content digest; sampled aliases open their canonical session |
| `search_first_lines` | a sample of published first lines is searchable: the rarest words of the line (document frequency over all first lines), scoped to the session with `--chat`, return the row that carries the line. Corpus-wide rank is not asserted: thousands of sessions share boilerplate openers, so top-k is a ranking property, not a missing-row signal |

A failing invariant on real data is reported as a finding with the check name, the
aggregate counts and the adapters involved; it is a potential real bug and must not be
answered by weakening the check.

## Privacy

Scrubbed samples are still shaped like the originals (record counts, intervals, string
lengths). Keep that in mind when choosing `--per-shape` and `--max-files`; the defaults
keep one representative per shape. Never add raw snapshots, digests of originals, or the
sampler's scratch directories to the repository. `bench/validate_repo_privacy.py` must stay
clean on the fixture tree, and the fixtures live under `bench/fixtures/real_shapes/<adapter>`
without leading dots so the gate's dot-store rule keeps applying to accidental captures.
