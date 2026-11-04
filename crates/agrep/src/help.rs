//! Help text, taken from 0.4.9's parsers (cli.py and each command's module) wherever
//! 0.5 behaves the same.

pub(crate) const TOP_USAGE: &str = "agrep [-h] [-V] <command> [options]
       agrep \"<pattern>\" [options]        bare text searches (same as agrep search)";
pub(crate) const TOP: &str = "examples:
  agrep \"race condition\" --here --since 14d -l --sort time
  agrep around @11111111:144.5a5e -C 8 --tool-output 400
  agrep recall \"index lock\" --here --since 30d --hits 5
  agrep chats \"retry backoff\" --project webapp
  agrep postcompact
  agrep board --once
  agrep resume @11111111:144.5a5e
  agrep status --json
  agrep why 11111111
  agrep summary --since 30d

a bare first argument that isn't a command searches; `agrep search index`
searches a word that is also a command. `agrep <command> --help` shows each
command's flags and examples.";

/// A subcommand's help leads with its own description (`before_help`), not the one-line
/// `about` the command list shows.
pub(crate) const SUB: &str = "{before-help}{usage-heading} {usage}\n\n{all-args}{after-help}";

pub(crate) const NO_AUTO: &str =
    "skip automatic index/freshness work; an empty result exits 2 because absence is unverified";
pub(crate) const COLOR: &str = "color output: auto (on a terminal, the default), always, or never";
pub(crate) const AGENT: &str = "only this agent (claude, codex, opencode, antigravity, agy, kimi, cline, gemini, crush, cursor, pi, omp)";
pub(crate) const PROJECT: &str = "only chats whose project label, or its last path segment, is exactly this (case-insensitive; * or ? make it a glob, e.g. 'webapp*')";
pub(crate) const HERE: &str = "--project <this folder's name> (the current directory's basename; filesystem roots are refused)";
pub(crate) const EXCLUDE_PROJECT: &str = "hide chats whose project label matches this (same rule)";
pub(crate) const SINCE: &str = "only hits at/after WHEN (7d / 24h / 2w / 30m, or 2026-06-01)";
pub(crate) const UNTIL: &str = "only hits before WHEN (same formats as --since)";

pub(crate) const SEARCH: &str = "examples:
  agrep \"race condition\"            grep every agent for a phrase
  agrep deadlock --agent codex       just codex
  agrep bug --model gpt-5            only turns from that exact model
  agrep bug --model spark --soft     model contains spark
  agrep -w leak                      whole word only
  agrep -E \"TODO|FIXME\" --who agent  regex, agent replies only
  agrep oom --who user,agent         several speakers, comma list
  agrep oom --no-side                hide side chats (spawned subagents)
  agrep fixture --no-meta            drop tooling-about-history rows
  agrep -l auth                      which chats mention it
  agrep -c oom                       just the count
  agrep \"flaky test\" -s              semantic search
  agrep memory --json                structured hits

search is case-insensitive. exit: 0 found, 1 proven none, 2 error or unverified result.
grouped rows mark the speaker: › you  → agent  ⚙ tool  ⇒ subagent";
pub(crate) const SEARCH_MAX: &str = "show at most N hits (classic: default 40 keyword or 10 semantic; 0 = all keyword hits or up to 200 semantic chats. compact: adaptive 4-16 rows within 3584 bytes; explicit positive N requests up to 40 frozen rows, still byte-limited)";
pub(crate) const SEARCH_WHO: &str = "only these speakers, comma-separated (user, subagent, agent, tool, control, synthetic, recap, harness)";
pub(crate) const SEARCH_NO_WHO: &str = "exclude these speakers' rows (same names; --no-who subagent drops subagent-spoken turns, not side chats)";
pub(crate) const NO_META: &str = "drop structurally proven ~meta rows; retain one marked row when it is the query's only evidence";
pub(crate) const CHAT: &str = "only this chat: an 8-char id prefix (as shown) or full session uuid";
pub(crate) const SELF: &str = "include the calling agent's live context window (hidden, with older turns labeled ~self, only when the calling session is identified)";
pub(crate) const SEARCH_NO_SELF: &str =
    "exclude the calling session and its indexed family, even outside agent shells";
pub(crate) const SEARCH_ALL_SIDE_CHATS: &str = "ranking only, with -s: let sibling side chats take separate slots instead of one best hit per root family";
pub(crate) const SEARCH_NO_SIDE: &str =
    "hide side chats (spawned subagent sessions) entirely; -l then lists no [side chat] rows";

pub(crate) const AROUND: &str = "examples:
  agrep around 11111111 144            show that turn and four on either side
  agrep around 11111111:144 --context 10  a wider window
  agrep around 11111111 --whole        the entire conversation
  agrep around 11111111 --whole --who user  every user turn: a compact transcript
  agrep around 11111111 144 -C all     the same whole chat, centered for --json
  agrep around 11111111 144 -C 0 --max-chars 0  one turn, its text uncapped
  agrep around 11111111 144 -C 0 --full  include tool events and full text
  agrep around 11111111 144 --tool-output 800  include tool results
  agrep around 11111111              latest indexed turn (same as @11111111)
  agrep around @11111111:144.5a5e     compact result handle: that turn only
  agrep around 11111111 144 --json     one object per message/event

session ids and turns come from `agrep <pattern> --json`.
exit: 0 shown, 1 no selected messages/events, 2 bad target / no index.";

pub(crate) const RECALL: &str = "examples:
  agrep recall \"index lock\"            have I hit this before?
  agrep recall @11111111:144.5a5e     pull one compact result directly
  agrep recall oom --hits 5 --budget 4000
  agrep recall deadlock --json          structured, for piping

Combines keyword and meaning search; -s uses meaning only.
exit: 0 found, 1 no confident match with complete search coverage, 2 invalid arguments or incomplete search.";
pub(crate) const PACK: &str = "examples:
  agrep pack unicode encoding cp1252    search several angles together
  agrep pack \"index lock\" deadlock --hits 5
  agrep pack oom timeout --budget 8000  one shared output budget
  agrep pack parser dispatch --json     structured, for piping

Combines keyword and meaning search; -s uses meaning only.
exit: 0 found, 1 no confident match with complete search coverage, 2 invalid arguments or incomplete search.";
pub(crate) const RECALL_HITS: &str =
    "chats to show (default: 3, increasing with --budget; at least one per query)";
pub(crate) const RECALL_BUDGET: &str = "maximum output bytes (default 200000; 0 = uncapped; a finite budget is at least 64, and a page whose first hit block needs more is refused, exit 2, naming the bytes)";
pub(crate) const RECALL_WHO: &str =
    "only these speakers, comma-separated (same names as `agrep search`)";
pub(crate) const RECALL_NO_WHO: &str = "exclude these speakers' rows (e.g. --no-who subagent drops subagent-spoken turns; --no-side hides side chats)";
pub(crate) const RECALL_NO_SELF: &str =
    "conservatively exclude the whole calling session family, even outside agent shells";
pub(crate) const RECALL_ALL_SIDE_CHATS: &str =
    "ranking only: let sibling side chats from one root conversation occupy separate ranked slots";
pub(crate) const RECALL_NO_SIDE: &str = "hide side chats (spawned subagent sessions) entirely";

pub(crate) const CHATS: &str = "examples:
  agrep chats                      newest indexed chats
  agrep chats apartment            project / opening match
  agrep chats drafting             topic match inside chats
  agrep chats drafting --here      only this folder's chats
  agrep chats 0199fa               exact id prefixes include side chats
  agrep chats webapp --agent codex --json

Content lookup: the words as an adjacent phrase first, then all words anywhere in one turn; the best turn per chat is kept and the top max(20, 2N) chats are ranked by that turn, the rest newest first.
A leading @session opens its latest indexed turn with `agrep around` and also works as a `--chat` selector; `agrep around @...` pins an exact turn when available.
exit: 0 found, 1 proven none, 2 no index or unverified result.";

pub(crate) const POSTCOMPACT: &str = "examples:
  agrep postcompact         recent pre-compact root context
  agrep postcompact --json  one bounded structured packet

exit: 0 recovered, 1 proven empty, 2 unavailable or invalid.";

pub(crate) const SUMMARY: &str = "examples:
  agrep summary                     last 7 days, one briefing per project
  agrep summary --since 30d         the month in review
  agrep summary pending             open items only, most confident first
  agrep summary time --group week   estimated active time per week and project
  agrep summary --project webapp --json

Time is an estimate from turn timestamps (idle cap, side chats folded into their family), never elapsed span or billable time. Pending statuses: waiting_on_user (high), open_next_steps (high), agent_work_incomplete (medium), todo_open (medium), unknown (low).
exit: 0 something to report, 1 proven nothing, 2 no index or unverified.";

pub(crate) const BOARD: &str = "interactive keys:
  ↑/↓ select · enter focus · esc back · a agent filter
  c compact/expanded · 1-9 jump · q quit

examples:
  agrep board                 interactive board
  agrep board --once          one printable live-window frame
  agrep board --once --json --sort updated --agent claude --state active --roots -n 3
  agrep board --agent codex   filter the initial view
  agrep board --session @ID   focus a copied chat/result handle
  agrep chats                 indexed history, newest first

exit: 0 shown, 1 no live session match, 2 partial snapshot, invalid, or ambiguous selector.
a partial snapshot means some store was unreadable: it is not proof that no other agent is live.";

pub(crate) const TAIL: &str = "examples:
  agrep tail                    stream turn-end events as JSON lines
  agrep tail --agent codex      stream Codex turn-end events only
  agrep tail --snapshot         print active sessions once and exit

exit: 0 snapshot complete or output closed; 2 invalid arguments; 130 interrupted while streaming.";

pub(crate) const RESUME: &str = "IDs and handles take priority. A hex fragment of at least 6 characters
also matches inside an id when no prefix matches. Project labels use
the same exact-label-or-leaf rule as --project (including * and ? globs);
otherwise, match a first-line substring, ignoring case.
Multiple project/first-line matches open a restricted picker on a
terminal; without one, candidates are listed and nothing is launched.

examples:
  agrep resume @01a06003:4.82c5   reopen the session from a search hit
  agrep resume solo-finder        match a project; pick if several
  agrep resume                    pick from recent sessions
  agrep resume --list             list recent resumable sessions
  agrep resume --list -n 0        list every resumable session

exit: 0 listed, picker closed, or the resumed agent exited 0; 1 no unique match or launch failed; 2 unavailable data or invalid arguments; 127 the agent's CLI isn't on PATH. Other resumed-agent exit codes are passed through.";

pub(crate) const RUN_USAGE: &str = "agrep run [-h] [--cwd DIR] AGENT [-- AGENT_ARGS...]";
pub(crate) const RUN: &str = "examples:
  agrep run claude              launch claude, captured
  agrep run codex --cwd . -- --help
                                pass --help to codex here";

pub(crate) const INDEX: &str = "examples:
  agrep index          ingest new sessions + refresh the search db
  agrep index --full   cold reparse of every store file
  agrep index --semantic  also embed every row for meaning search";

pub(crate) const REINDEX: &str = "examples:
  agrep reindex          reparse every source; unchanged text keeps its embeddings
  agrep reindex --full   also rebuild every embedding

exit: 0 refreshed, 1 a required stage failed, 2 invalid arguments.";

pub(crate) const SETUP: &str = "examples:
  agrep setup                     interactive, every choice disclosed
  agrep setup -y                  accept the prompts (for scripts)
  agrep setup --no-teach          skip agent instructions
  agrep setup --no-semantic       skip the ~95 MiB model download
  agrep setup --archive -y        also keep recovery archives

exit: 0 setup complete, 1 required setup failed, 2 invalid arguments.";

pub(crate) const REMOVE: &str = "examples:
  agrep remove         remove the enrolled agent integration
  agrep setup          enroll it again later

exit: 0 removed, 1 cleanup incomplete, 2 invalid arguments.";

pub(crate) const ARCHIVE: &str = "examples:
  agrep archive --status  inspect retention and the last capture
  agrep archive --on      enable automatic source-store captures
  agrep archive           capture changed source stores now
  agrep archive --purge   delete every archived copy (asks first)

exit: 0 complete; 1 incomplete, busy, or unhealthy; 2 archive unavailable or invalid arguments.";

pub(crate) const RESTORE: &str = "examples:
  agrep restore 11111111              restore one session's store file
  agrep restore rollout-2026 --to .  restore matching files here

exit: 0 restored; 1 no unique match or a file was not restored; 2 archive unavailable or invalid arguments.";

pub(crate) const SET_USAGE: &str = "agrep set <key> <value>";
pub(crate) const SET: &str = "keys:
  tools: on/off
  embeddings: auto/off
  post_index: any command, or off

examples:
  agrep set                  list current settings
  agrep set embeddings off   disable semantic downloads and builds

exit: 0 listed or changed, 1 settings could not be changed, 2 invalid arguments.";

pub(crate) const STATUS: &str = "examples:
  agrep status         bounded diagnostic
  agrep status --json  cheap machine summary, one JSON object
  agrep doctor --deep  index integrity and semantic model checks

exit: 0 diagnostic complete; 2 invalid arguments, or an unsupported or unreadable index.";

pub(crate) const DOCTOR_ABOUT: &str =
    "bounded diagnostic: data, index, semantic model, integrations.

Every row answers what it is and what state it's in. `agrep status` shows the
same facts; `agrep doctor --deep` adds the index's SQLite integrity check and the
semantic model check, and `--fix` repairs what is safe to repair.";
pub(crate) const DOCTOR: &str = "examples:
  agrep doctor          bounded operational report
  agrep doctor --deep   index integrity and semantic model checks
  agrep doctor --deep --no-semantic   the same, without the semantic model check
  agrep doctor --fix    refresh sources, repair integrations, rebuild a corrupt index
  agrep doctor --json   machine-readable bounded report

exit: 0 healthy, 1 source or integration problems, 2 failed checks, a missing or unreadable index, or invalid options.";

pub(crate) const AUDIT: &str = "examples:
  agrep audit                 bounded cached accounting check
  agrep audit --agent codex   check one source adapter
  agrep audit --full          recount every eligible source file

exit: 0 clean, 1 coverage gaps under --strict, 2 unreadable evidence or invalid input.";

pub(crate) const WHY: &str =
    "The reference resolves like `agrep resume`: session id or prefix, @handle,
store alias, uuid or ses_ id, a hex fragment of six or more characters, a
project label, then a first-line substring. A transcript path, as the index
records it, is checked when nothing else matches.

verdicts: indexed, ambiguous, unprovable, not-indexed

examples:
  agrep why 11111111                        a session id prefix
  agrep why ~/.claude/projects/x/y.jsonl    a transcript path
  agrep why 'retry backoff' --json          a first-line fragment, machine form

exit: 0 indexed; 1 not indexed (explained); 2 ambiguous, unprovable, or invalid arguments.";

pub(crate) const UI: &str = "examples:
  agrep ui               use an available loopback port
  agrep ui --port 8732   request a specific loopback port

The server binds only to 127.0.0.1, requires a one-use browser link, and has no mutation endpoints.
Press Ctrl-C to stop it.

exit: 0 stopped normally, 2 invalid arguments or bind failure.";
pub(crate) const SERVE: &str = "examples:
  agrep serve               use an available loopback port
  agrep serve --port 8732   request a specific loopback port

The server binds only to 127.0.0.1, requires a one-use browser link, and has no mutation endpoints.
Press Ctrl-C to stop it.

exit: 0 stopped normally, 2 invalid arguments or bind failure.";
