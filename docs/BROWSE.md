# Browsing, briefings, native launch, and diagnostics

`chats` reads session metadata before fetching the visible rows for its selected
page. With a pattern it combines metadata matches and the keyword engine's best
turn per chat. A direct id prefix includes side chats; ordinary listings hide
them. Each printed row includes its own digest-bound `agrep around` command.
JSON is one `agrep-meta` envelope and then row-only objects, including on pipes;
empty output is a single envelope with `hits: []`. A content match's preview (96
characters, 240 as JSON `match_text`) starts where the match's snippet does unless a
later window holds more of the query's distinct words; then it is the window centred on
them, after `…`. 0.4.9 always cut the snippet's start, so a second word more than a few
words past the first was never shown. With no matching chat, stderr says `no chats match`,
adding `; some side chats do (--side shows them)` only when hidden side chats would match,
where 0.4.9 printed `showing 0 of 0 matching chats, best match first (side chats hidden;
--side shows them)`; the exit code is unchanged.

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
is numbered and bounded by `-n`: as in 0.4.9 an answer is a listed number or a
reference resolved among the picker's candidates, but a number is only ever a list
position (0.4.9 tried an out-of-range one as an id prefix), and anything else asks again
where 0.4.9 gave up; an empty answer or the end of input cancels. `--list` never launches.
The launch line names the program started (`↻ resuming omp · DIR`), and the picker and
`--list` label each session by that same program, where 0.4.9 labelled every omp session
`pi`: the fork that wrote it when that one is on PATH, else the sibling that would start.
Side chats walk their parent chain with cycle/missing-parent refusal. Native argument
tables are:

| Agent | Resume arguments |
|---|---|
| Claude | `claude --resume ID` |
| Codex | `codex resume ID` |
| opencode | `opencode CWD --session ID` |
| Antigravity | `agy --conversation ID` |
| pi / omp | originating fork, then `-r ID` |

Kimi, Gemini, and crush have no verified native resume/run contract; Cursor and
Cline are editor-owned. They receive an explicit unsupported answer: `resume` gives
the reason, and `agrep run gemini` says `gemini can't be launched by agrep run` and
lists the agents it launches (exit 2), where 0.4.9 gave `run` the resume refusal. Fresh pi
launches prefer the spelling requested, with the sibling as a PATH fallback.

The agent starts in the session's recorded working directory; Antigravity's is the
most frequent `Cwd` its tool calls recorded, ties going to the first seen, and Claude's is
the first recorded `cwd` whose slug names the transcript's project folder (else the first
recorded). A recorded
directory missing here re-anchors, as in 0.4.9, to the one existing directory that ends
with the longest of its path suffixes under home or home's immediate non-hidden
subdirectories; with no such directory, or several, the agent starts in home. A launch
failure prints `couldn't launch AGENT: REASON` and exits 1. Claude finds a session only
under the launch directory's project folder, so when that differs from the transcript's,
resume links the transcript there (a private copy where links are refused); its own earlier
link and any plain file already there are left as they are, and anything else there stops
the resume with exit 1.

`run` and `resume` pass arguments directly to native processes. POSIX launches
give the agent a process group of its own in the caller's session. With a terminal
on both stdin and stdout the agent is lent the terminal's foreground at once;
otherwise (a pipeline, `agrep run x | less`) the terminal stays with the caller's
job, so a partner reading /dev/tty is not stopped, until the agent's first read or
configuration of the terminal stops it with SIGTTIN/SIGTTOU, which lends it the
terminal from then on. 0.4.9 gave a piped agent a session of its own, out of the
terminal's job control: Ctrl-Z stopped agrep but not the agent, which went on reading
the shell's keystrokes. A Ctrl-Z aimed at agrep stops the agent's group first, then
agrep's job. When `fg` brings a still-running background launch forward, the agent
gets the terminal the first time it reads or configures it, or when agrep is next
signalled, where 0.4.9 checked every 50 ms: a shell's `fg` sends a running job no
signal, and 0.5 waits on the agent instead of polling. Launches forward termination
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
with the adapters and compares canonical text rather than cached counters. A row
the index stores but keeps out of search by design counts as indexed: a copy of a
row that a chat held by several files shows only once in its canonical replay.
`--full` lists parsed rows missing from the index first, then indexed rows no source
holds, each group by agent, session and speaker, so a rerun lists them in the same order.
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
