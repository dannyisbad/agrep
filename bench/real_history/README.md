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

## Lane B: full-history run over the real stores (local, on demand)

```
python bench/real_history/run.py                 # ~minutes; report is aggregates only
python bench/real_history/run.py --keep --report /tmp/real-history.json
```

This is the one sanctioned exception to the sandbox-HOME rule. Safeguards:

- `env -i` style sealed environment; `HOME`/`AGREP_HOME` point at the real home only so
  store discovery works; `AGREP_DATA_DIR` is a fresh dir under `~/.agrep-p4/RealHistory`
  and the run refuses to start if it resolves into the production data dir;
- `AGREP_DATA_READONLY` names the production dir as a second fence;
- `AGREP_NO_DAEMON`, `AGREP_NO_RESIDENT`, `AGREP_NO_SEM_WORKER`, `AGREP_NO_FETCH` set,
  embeddings off in the scratch `settings.json`, `TMPDIR`/`XDG_RUNTIME_DIR` in scratch;
- never `setup`, `teach`, `remove` or `doctor --fix`; the only commands are `index`,
  `search --json`, `chats --json`, `around --json`, `audit --full --json`,
  `agrep-rs stores --paths`;
- `du -sk` over the store roots first; refuses unless free disk is at least
  `--min-free-ratio` (2.0) times the store size;
- the scratch dir is deleted at the end unless `--keep`; background children bound to the
  scratch data dir are reaped.

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
| `search_first_lines` | a sample of session first lines is found by `search --json` |

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
