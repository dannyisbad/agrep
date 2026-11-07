# Browsing, briefings, native launch, and diagnostics

`chats` reads session metadata before fetching the visible rows for its selected
page. With a pattern it combines metadata matches and the keyword engine's best
turn per chat. A direct id prefix includes side chats; ordinary listings hide
them. Each printed row includes its own digest-bound `agrep around` command.
JSON is one `agrep-meta` envelope and then row-only objects, including on pipes;
empty output is a single envelope with `hits: []`.

`summary`, `summary pending`, and `summary time` read the canonical `visible=1`
prose, retained reply shapes, and event facts of the families their window admits,
as 0.4.9 did, and no other history; a stored event text is decoded only when
classification reads it. Active time is explicitly an
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
use a private process group. With a terminal on both stdin and stdout the agent
is lent the terminal's foreground; otherwise it runs in a session of its own, so
the terminal stays with the caller's pipeline. When `fg` brings a still-running
background launch forward, the agent gets the terminal the first time it reads or
configures it, or when agrep is next signalled, where 0.4.9 checked every 50 ms:
a shell's `fg` sends a running job no signal, and 0.5 waits on the agent instead
of polling. Launches forward termination
signals, return the first signal's conventional exit code, restore terminal
ownership/attributes, and terminate remaining descendants. Windows binds the
wrapper and descendants to a kill-on-close Job Object before spawning. A
non-inheritable job handle stays open until process exit. No shell is used to
assemble an argument string.

With no index, `status` creates neither an index nor the data root. With an index it
censuses and ingests changed sources inline before reading committed counts, as other
history commands do (`--no-auto` and read-only data roots skip ingestion). It reports the
format marker, recursive disk bytes without following links, canonical counts,
refresh timestamp, per-source parse errors, semantic model/coverage/backlog,
recorded integrations, archive presence, and version. Coverage comes from the
index's trigger-maintained counts; diagnostics never load a model to count it.
Enrollment names resolve to known instruction files; status checks for the owned
block and reports missing/drifted enrollment.

`doctor` adds integrity checks, safe explicit refresh, integration setup replay
on `--fix`, and corrupt-index rebuilding only after terminal confirmation. The
corrupt database is retained under the same data root. As in 0.4.9, `doctor`
exits 0 once its report is complete, whatever it found, 1 when a repair `--fix`
attempted could not finish (including a corrupt index with no terminal to
confirm its rebuild), and 2 for invalid options. `audit` independently
counts visible rows, compares census fingerprints, and `--full` reparses sources
with the adapters and compares canonical text rather than cached counters.
As in 0.4.9, gaps exit 1 and `--strict` makes them errors (exit 2); a source that
fails to parse, unreadable evidence, or no source file to audit exits 2. `why` never
refreshes and returns the version-1 reference/verdict/exit/summary/candidates/
evidence/next_action object, naming index and source paths.

Intentional 0.4.9 differences: full session ids are emitted instead of abbreviated
ones, while both resolve; status/doctor omit all removed process/storage states;
`why` describes the single SQLite snapshot; native launch never creates transcript
links in an agent's store. `doctor --fix` does not download the semantic model as
0.4.9's did: a missing model is not a fault in 0.5, and `agrep setup` installs it.
`audit` reports sources, prose and tool rows, gaps and problems instead of 0.4.9's
per-agent evidence-book table, and a source written since the last refresh is a gap
where 0.4.9 called it live evidence: 0.5 keeps no evidence book and compares the
index with the sources themselves.
The explorer refuses a meaning search with no model installed (`semantic model not
installed: run agrep setup`, HTTP 400), where 0.4.9's answered it as unavailable and
its page said to try grep: 0.5 installs the model only through `agrep setup`, and the
search command's `-s` says the same.
