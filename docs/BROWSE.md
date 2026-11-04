# Browsing, briefings, native launch, and diagnostics

`chats` reads session metadata before fetching the visible rows for its selected
page. With a pattern it combines metadata matches and the keyword engine's best
turn per chat. A direct id prefix includes side chats; ordinary listings hide
them. Each printed row includes its own digest-bound `agrep around` command.
JSON is one `agrep-meta` envelope and then row-only objects, including on pipes;
empty output is a single envelope with `hits: []`.

`summary`, `summary pending`, and `summary time` read the canonical `visible=1`
prose, retained reply shapes, and event facts. Active time is explicitly an
estimate: adjacent prompt/compaction timestamps are capped, tool activity can
close the last interval, and all intervals are unioned within each root family.
Local midnight boundaries split intervals before day/week/month grouping. A
single prompt with no later activity contributes zero time, not an invented
minimum. Missing timestamps are counted separately. Pending rows distinguish
high-confidence user questions and open next steps, medium-confidence unfinished
work and open task lists, and low-confidence unknown endings. Completed lists,
work after a plan, failed task updates, and returned side results do not reopen
finished work. All evidence links use the canonical public position; reply
shapes are looked up with their source-local turn.

`resume` resolves ids, handles, upstream aliases, projects, and opening text. The
noninteractive ambiguous case lists candidates and exits 1. Interactive selection
is numbered and bounded by `-n`; `--list` never launches. Side chats walk their
parent chain with cycle/missing-parent refusal. Native argument tables are:

| Agent | Resume arguments |
|---|---|
| Claude | `claude --resume ID` |
| Codex | `codex resume ID` |
| opencode | `opencode CWD --session ID` |
| Antigravity | `agy --conversation ID` |
| pi / omp | originating fork, then `-r ID` |

Kimi, Gemini, and crush have no verified native resume/run contract; Cursor and
Cline are editor-owned. They receive an explicit unsupported answer. Fresh pi
launches prefer the spelling requested, with the sibling as a PATH fallback.

`run` and `resume` pass arguments directly to native processes. POSIX launches
use a private process group, forward termination signals, return the first
signal's conventional exit code, restore terminal ownership/attributes, and
terminate remaining descendants. Windows binds the wrapper and descendants to
a kill-on-close Job Object before spawning. A non-inheritable job handle stays
open until process exit. No shell is used to assemble an argument string.

`status` is a read-only snapshot, including when no index exists. It reports the
format marker, recursive disk bytes without following links, canonical counts,
refresh timestamp, per-source parse errors, semantic model/coverage/backlog,
recorded integrations, archive presence, and version. Coverage comes from the
index's trigger-maintained counts; diagnostics never load a model to count it.
Enrollment names resolve to known instruction files; status checks for the owned
block and reports missing/drifted enrollment.

`doctor` adds integrity checks, safe explicit refresh, integration setup replay
on `--fix`, and corrupt-index rebuilding only after terminal confirmation. The
corrupt database is retained under the same data root. `audit` independently
counts visible rows, compares census fingerprints, and `--full` reparses sources
with the adapters and compares canonical text rather than cached counters.
`--strict` turns proven gaps into exit 1; unreadable evidence exits 2. `why` never
refreshes and returns the version-1 reference/verdict/exit/summary/candidates/
evidence/next_action object, naming index and source paths.

Intentional 0.4.9 differences: full session ids are emitted instead of abbreviated
ones, while both resolve; status/doctor omit all removed process/storage states;
`why` describes the single SQLite snapshot; native launch never creates transcript
links in an agent's store.
