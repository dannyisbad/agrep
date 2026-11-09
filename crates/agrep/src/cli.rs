use crate::{cmd, help};
use agrep_core::query::Filters;
use anyhow::{Context, Result};
use clap::{Args, Parser, Subcommand};
#[derive(Parser)]
#[command(
    version,
    about = "agentic grep: find, open, resume and watch your AI agents' work",
    override_usage = help::TOP_USAGE,
    after_help = help::TOP,
    disable_help_subcommand = true,
    args_override_self = true
)]
pub(crate) struct Cli {
    #[command(subcommand)]
    pub(crate) command: Option<Command>,
}
#[derive(Args, Default)]
pub(crate) struct Scope {
    #[arg(long, value_name = "AGENT", help = help::AGENT)]
    pub(crate) agent: Option<String>,
    #[arg(long, conflicts_with = "here", value_name = "PROJECT", help = help::PROJECT)]
    pub(crate) project: Option<String>,
    #[arg(long, help = help::HERE)]
    pub(crate) here: bool,
    #[arg(long, value_name = "EXCLUDE_PROJECT", help = help::EXCLUDE_PROJECT)]
    pub(crate) exclude_project: Option<String>,
    #[arg(
        long,
        value_name = "MODEL",
        help = "only turns from this exact model name"
    )]
    pub(crate) model: Option<String>,
    #[arg(
        long,
        visible_alias = "model-soft",
        help = "with --model, substring-match the model name (like *model*)"
    )]
    pub(crate) soft: bool,
    #[arg(long, value_name = "LIST", help = help::SEARCH_WHO)]
    pub(crate) who: Option<String>,
    #[arg(long, value_name = "LIST", help = help::SEARCH_NO_WHO)]
    pub(crate) no_who: Option<String>,
    #[arg(long, help = help::NO_META)]
    pub(crate) no_meta: bool,
    #[arg(long, visible_alias = "session", value_name = "ID", help = help::CHAT)]
    pub(crate) chat: Option<String>,
    #[arg(long, value_name = "WHEN", help = help::SINCE)]
    pub(crate) since: Option<String>,
    #[arg(long, visible_alias = "before", value_name = "WHEN", help = help::UNTIL)]
    pub(crate) until: Option<String>,
    #[arg(long = "self", conflicts_with = "no_self", help = help::SELF)]
    pub(crate) include_self: bool,
    #[arg(long, help = help::SEARCH_NO_SELF)]
    pub(crate) no_self: bool,
    #[arg(long, help = help::SEARCH_ALL_SIDE_CHATS)]
    pub(crate) all_side_chats: bool,
    #[arg(long, help = help::SEARCH_NO_SIDE)]
    pub(crate) no_side: bool,
}
#[derive(Args)]
pub(crate) struct Search {
    #[arg(num_args = 0.., help = "text to search for (joined with spaces)")]
    pub(crate) pattern: Vec<String>,
    #[arg(short = 'n', long = "max", value_name = "N", help = help::SEARCH_MAX)]
    pub(crate) max: Option<usize>,
    #[arg(short = 'E', long, help = "treat pattern as a regex")]
    pub(crate) regex: bool,
    #[arg(short = 'w', long, help = "match whole words only")]
    pub(crate) word: bool,
    #[arg(
        short = 'i',
        long,
        help = "(default; search is always case-insensitive)"
    )]
    pub(crate) ignore_case: bool,
    #[arg(
        short = 'l',
        long,
        help = "list matching chats, not every line (like grep -l)"
    )]
    pub(crate) chats: bool,
    #[arg(short = 'c', long, help = "print only the match count (like grep -c)")]
    pub(crate) count: bool,
    #[arg(
        long,
        help = "exhaustively count phrase boundary classes and all-terms rows"
    )]
    pub(crate) count_by_tier: bool,
    #[arg(
        long,
        value_name = "HANDLE",
        help = "continue a frozen compact result page"
    )]
    pub(crate) more: Option<String>,
    #[arg(
        long,
        help = "use the classic renderer even in an agent or compact profile (compact hybrid retrieval off; --lexical disables semantics entirely, -s forces them)"
    )]
    pub(crate) classic: bool,
    #[arg(
        long,
        help = "one tab-separated row per hit (the piped default outside agent shells)"
    )]
    pub(crate) flat: bool,
    #[command(flatten)]
    pub(crate) scope: Scope,
    #[arg(
        long,
        value_parser = ["score", "time", "position"],
        default_value = "score",
        help = "hit order: relevance score (default), newest first, or corpus position (the pre-ranking order)"
    )]
    pub(crate) sort: String,
    #[arg(
        short = 's',
        long,
        help = "semantic search: confident relevant chats (embeddings are maintained in the background)"
    )]
    pub(crate) semantic: bool,
    #[arg(long, help = "keyword only: disable compact-profile meaning results")]
    pub(crate) lexical: bool,
    #[arg(
        long,
        help = "force the over-specification recovery lane: bm25 term coverage over OR-ed query terms, best row per family"
    )]
    pub(crate) coverage: bool,
    #[arg(
        long,
        help = "one agrep-meta run envelope, then one object per hit (any-order hits carry matched: \"all-terms\")"
    )]
    pub(crate) json: bool,
    #[arg(long, help = help::NO_AUTO)]
    pub(crate) no_auto: bool,
    #[arg(long, value_parser = ["auto", "always", "never"], default_value = "auto", help = help::COLOR)]
    pub(crate) color: String,
}
#[derive(Args, Default)]
pub(crate) struct ChatScope {
    #[arg(long, value_name = "AGENT", help = help::AGENT)]
    pub(crate) agent: Option<String>,
    #[arg(long, conflicts_with = "here", value_name = "PROJECT", help = help::PROJECT)]
    pub(crate) project: Option<String>,
    #[arg(long, help = help::HERE)]
    pub(crate) here: bool,
    #[arg(long, value_name = "EXCLUDE_PROJECT", help = help::EXCLUDE_PROJECT)]
    pub(crate) exclude_project: Option<String>,
    #[arg(
        long,
        value_name = "WHEN",
        help = "only chats active at/after WHEN (7d / 24h / 2w / 30m, or 2026-06-01); content hits must fall inside too"
    )]
    pub(crate) since: Option<String>,
    #[arg(
        long,
        visible_alias = "before",
        value_name = "WHEN",
        help = "only chats started before WHEN (same formats as --since)"
    )]
    pub(crate) until: Option<String>,
    #[arg(
        long = "self",
        conflicts_with = "no_self",
        help = "include the calling agent's current-window echoes"
    )]
    pub(crate) include_self: bool,
    #[arg(long, help = help::SEARCH_NO_SELF)]
    pub(crate) no_self: bool,
    #[arg(
        long,
        help = "hide side chats (the default; accepted for symmetry with search and recall)"
    )]
    pub(crate) no_side: bool,
}
impl ChatScope {
    pub(crate) fn filters(&self) -> Result<Filters> {
        Scope {
            agent: self.agent.clone(),
            project: self.project.clone(),
            here: self.here,
            exclude_project: self.exclude_project.clone(),
            since: self.since.clone(),
            until: self.until.clone(),
            no_side: self.no_side,
            ..Default::default()
        }
        .filters()
    }
}
#[derive(Args)]
pub(crate) struct Chats {
    #[arg(
        num_args = 0..,
        help = "words matched against chat identity (project, id, opening line) or indexed conversation content (all must hit in one place)"
    )]
    pub(crate) pattern: Vec<String>,
    #[arg(
        short = 'n',
        long = "max",
        default_value_t = 20,
        value_name = "N",
        help = "show at most N chats (default 20; 0 = all)"
    )]
    pub(crate) max: usize,
    #[arg(
        long,
        conflicts_with = "no_side",
        help = "include side chats (spawned subagent sessions)"
    )]
    pub(crate) side: bool,
    #[command(flatten)]
    pub(crate) scope: ChatScope,
    #[arg(
        long,
        help = "one metadata object, then one JSON object per chat (content rows carry score, matched, who, match_ts)"
    )]
    pub(crate) json: bool,
    #[arg(long, help = help::NO_AUTO)]
    pub(crate) no_auto: bool,
    #[arg(long, value_parser = ["auto", "always", "never"], default_value = "auto", help = help::COLOR)]
    pub(crate) color: String,
}
#[derive(Args)]
pub(crate) struct Around {
    #[arg(
        help = "session id or @session (alone: its latest turn), session:turn, or a compact @session:turn handle"
    )]
    pub(crate) session: String,
    #[arg(
        allow_negative_numbers = true,
        help = "turn number to center on (default: the latest indexed turn)"
    )]
    pub(crate) turn: Option<i64>,
    #[arg(
        short = 'C',
        long,
        visible_alias = "radius",
        value_name = "N",
        help = "turns before and after the center turn (default 4; a precise @session:turn handle defaults to 0 - just the named turn); `all` reads the whole chat"
    )]
    pub(crate) context: Option<String>,
    #[arg(
        long,
        help = "read every turn (same as -C all); combine with --who user for just the user's messages"
    )]
    pub(crate) whole: bool,
    #[arg(
        long,
        value_name = "M",
        help = "per-message text cap (default 4000; exact delegated prose may use the ingest cap; 0 = uncapped)"
    )]
    pub(crate) max_chars: Option<usize>,
    #[arg(
        long,
        help = "include tool events and remove the display text cap; source and tool-result limits still apply"
    )]
    pub(crate) full: bool,
    #[arg(long, help = "hide tool-call lines")]
    pub(crate) no_tools: bool,
    #[arg(
        long,
        value_name = "N",
        help = "include raw multiline tool results, capped at N chars (default: concise one-line preview)"
    )]
    pub(crate) tool_output: Option<usize>,
    #[arg(
        long,
        value_name = "WHO",
        value_parser = ["you", "user", "subagent", "agent", "tool", "control", "synthetic", "recap", "harness"],
        hide_possible_values = true,
        help = "only this speaker: you, user, subagent, agent, tool, control, synthetic, recap or harness (`you` is an alias for `user`)"
    )]
    pub(crate) who: Option<String>,
    #[arg(long, help = "one JSON object per message/event (for piping)")]
    pub(crate) json: bool,
    #[arg(long, help = help::NO_AUTO)]
    pub(crate) no_auto: bool,
    #[arg(long, value_parser = ["auto", "always", "never"], default_value = "auto", help = help::COLOR)]
    pub(crate) color: String,
}
#[derive(Args)]
pub(crate) struct Recall {
    #[arg(required = true, num_args = 1.., help = "what to look for")]
    pub(crate) query: Vec<String>,
    #[arg(long, value_name = "N", help = help::RECALL_HITS)]
    pub(crate) hits: Option<usize>,
    #[arg(long, default_value_t = 200000, value_name = "BYTES", help = help::RECALL_BUDGET)]
    pub(crate) budget: usize,
    #[arg(
        short = 'C',
        long,
        default_value_t = 2,
        value_name = "N",
        help = "turns before and after each hit (default 2)"
    )]
    pub(crate) context: usize,
    #[arg(
        short = 's',
        long,
        help = "force semantic-only search (recall normally uses keyword + meaning)"
    )]
    pub(crate) semantic: bool,
    #[arg(long, help = "search keywords only")]
    pub(crate) lexical: bool,
    #[command(flatten)]
    pub(crate) scope: Scope,
    #[arg(
        long,
        help = "one JSON object: {query, engine, hits: [{..., window: [...]}]}"
    )]
    pub(crate) json: bool,
    #[arg(
        long,
        help = "return one result pointer or a brief miss (exit 0 found / 1 no confident match / 2 incomplete)"
    )]
    pub(crate) probe: bool,
    #[arg(long, help = help::NO_AUTO)]
    pub(crate) no_auto: bool,
    #[arg(long, value_parser = ["auto", "always", "never"], default_value = "auto", help = help::COLOR)]
    pub(crate) color: String,
}
#[derive(Args)]
pub(crate) struct Postcompact {
    #[arg(long, help = "emit one structured packet")]
    pub(crate) json: bool,
    #[arg(long, help = help::NO_AUTO)]
    pub(crate) no_auto: bool,
    #[arg(
        long,
        value_name = "ID",
        help = "recover a specific session's tail when the caller cannot be auto-identified (e.g. under opencode); requires the session to have an indexed structural compaction boundary"
    )]
    pub(crate) session: Option<String>,
    #[arg(
        long,
        value_name = "TIMESTAMP",
        help = "wait for the compaction timestamp supplied by the recovery hook"
    )]
    pub(crate) boundary_ms: Option<i64>,
}
#[derive(Args)]
pub(crate) struct Summary {
    #[arg(
        value_parser = ["pending", "time"],
        help = "pending: open items only; time: estimated active time table; omitted: full briefing"
    )]
    pub(crate) mode: Option<String>,
    #[arg(
        long,
        value_name = "WHEN",
        help = "window start (7d / 24h / 2w / 30m, or 2026-06-01); default 7d before now, or before --until when only --until is given"
    )]
    pub(crate) since: Option<String>,
    #[arg(
        long,
        visible_alias = "before",
        value_name = "WHEN",
        help = "window end (same formats as --since); alone, the window is the 7d before it"
    )]
    pub(crate) until: Option<String>,
    #[arg(long, value_name = "PROJECT", help = help::PROJECT)]
    pub(crate) project: Option<String>,
    #[arg(long, value_name = "AGENT", help = help::AGENT)]
    pub(crate) agent: Option<String>,
    #[arg(
        long,
        value_parser = ["day", "week", "month"],
        help = "time mode: bucket by local day (default), ISO week or month"
    )]
    pub(crate) group: Option<String>,
    #[arg(
        long,
        default_value = "20m",
        value_name = "DURATION",
        help = "longest gap between turns still counted as active (5m / 20m / 1h; default 20m)"
    )]
    pub(crate) idle_cap: String,
    #[arg(
        short = 'n',
        long = "max",
        value_name = "N",
        help = "briefing: chats listed per project (default 5; 0 = all)"
    )]
    pub(crate) max: Option<usize>,
    #[arg(
        long = "self",
        conflicts_with = "no_self",
        help = "include the calling agent's current-window turns"
    )]
    pub(crate) include_self: bool,
    #[arg(long, help = help::SEARCH_NO_SELF)]
    pub(crate) no_self: bool,
    #[arg(
        long,
        help = "one agrep-meta object, then one JSON object per project row, pending item or time row"
    )]
    pub(crate) json: bool,
    #[arg(long, help = help::NO_AUTO)]
    pub(crate) no_auto: bool,
    #[arg(long, value_parser = ["auto", "always", "never"], default_value = "auto", help = help::COLOR)]
    pub(crate) color: String,
}
#[derive(Args)]
pub(crate) struct Board {
    #[arg(
        long,
        value_name = "AGENT",
        help = "filter to one supported agent (one-shot default: all; interactive remembers its prior view)"
    )]
    pub(crate) agent: Option<String>,
    #[arg(
        long,
        visible_alias = "chat",
        value_name = "SESSION",
        help = "focus one live session (id substring or pasted @handle)"
    )]
    pub(crate) session: Option<String>,
    #[arg(long, help = "print one frame and exit (implied when not a tty)")]
    pub(crate) once: bool,
    #[arg(long, help = "one bounded live-snapshot object (implies --once)")]
    pub(crate) json: bool,
    #[arg(
        long,
        value_parser = ["working", "updated"],
        default_value = "working",
        help = "family order: working first or latest family activity"
    )]
    pub(crate) sort: String,
    #[arg(
        long,
        value_name = "PROJECT",
        help = "only families whose project contains this text"
    )]
    pub(crate) project: Option<String>,
    #[arg(
        long,
        value_parser = ["all", "active", "working", "idle"],
        default_value = "all",
        help = "session/family state filter"
    )]
    pub(crate) state: String,
    #[arg(long, conflicts_with_all = ["side", "side_only"], help = "root sessions only")]
    pub(crate) roots: bool,
    #[arg(
        long,
        conflicts_with = "side_only",
        help = "show completed side sessions too"
    )]
    pub(crate) side: bool,
    #[arg(long, help = "side sessions only")]
    pub(crate) side_only: bool,
    #[arg(
        short = 'n',
        long = "max",
        value_name = "N",
        help = "show at most N sessions (JSON default 20; maximum 100)"
    )]
    pub(crate) max: Option<usize>,
    #[arg(
        long,
        value_name = "SECONDS",
        help = "bounded one-shot startup wait (default 10; max 10)"
    )]
    pub(crate) wait: Option<f64>,
    #[arg(long, help = "start in expanded mode (per-session events + model)")]
    pub(crate) expanded: bool,
}
#[derive(Subcommand)]
pub(crate) enum Command {
    #[command(
        about = "search every agent's history by keyword, plus meaning when confident",
        help_template = help::SUB,
        before_help = "agentic grep: search everything your AI agents have done",
        after_help = help::SEARCH
    )]
    Search(Search),
    #[command(
        about = "open a hit at its source, with turns of context",
        help_template = help::SUB,
        before_help = "show the conversation around one turn of a chat",
        after_help = help::AROUND
    )]
    Around(Around),
    #[command(
        about = "top hits + the conversation around each, within one byte budget",
        help_template = help::SUB,
        before_help = "top hits + their conversation windows within one byte budget",
        after_help = help::RECALL,
        mut_arg("who", |a| a.help(help::RECALL_WHO)),
        mut_arg("no_who", |a| a.help(help::RECALL_NO_WHO)),
        mut_arg("no_self", |a| a.help(help::RECALL_NO_SELF)),
        mut_arg("all_side_chats", |a| a.help(help::RECALL_ALL_SIDE_CHATS)),
        mut_arg("no_side", |a| a.help(help::RECALL_NO_SIDE))
    )]
    Recall(Recall),
    #[command(
        about = "find a chat by project, opener, or content; bare lists newest first",
        help_template = help::SUB,
        before_help = "find indexed chats by project, id, opening line, or conversation content",
        after_help = help::CHATS
    )]
    Chats(Chats),
    #[command(
        about = "this session's own turns from before a compaction, verbatim",
        help_template = help::SUB,
        before_help = "supplement a compact summary with the exact recent root tail",
        after_help = help::POSTCOMPACT
    )]
    Postcompact(Postcompact),
    #[command(
        about = "live agent activity on this box right now",
        help_template = help::SUB,
        before_help = "show the bounded live-activity window (not indexed chat history)",
        after_help = help::BOARD
    )]
    Board(Board),
    #[command(
        about = "reopen a past session in its own agent, cd'd to where it ran",
        help_template = help::SUB,
        before_help = "resume a past session in its own agent, cd'd to where it ran",
        after_help = help::RESUME
    )]
    Resume {
        #[arg(
            value_name = "REFERENCE",
            help = "session id/prefix (with optional @, uuid or ses_…), search-hit handle, project label, or first-line substring; omit to pick"
        )]
        reference: Option<String>,
        #[arg(
            short = 'n',
            long = "max",
            default_value_t = 15,
            value_name = "N",
            help = "how many recent/matching sessions to show in the picker or list (default 15; 0 = all)"
        )]
        max: usize,
        #[arg(short = 'l', long, help = "just list recent sessions; don't resume")]
        list: bool,
        #[arg(
            long,
            help = "skip the automatic freshness check of the agent stores before resolving"
        )]
        no_auto: bool,
        #[arg(long, value_parser = ["auto", "always", "never"], default_value = "auto", help = help::COLOR)]
        color: String,
    },
    #[command(
        about = "bounded diagnostic; --json is the cheap machine summary",
        after_help = help::STATUS
    )]
    Status {
        #[arg(long, help = "cheap machine-readable index summary instead")]
        json: bool,
        #[arg(long, help = "skip the automatic freshness check of the agent stores")]
        no_auto: bool,
    },
    #[command(
        about = "explain why a chat is or isn't indexed",
        help_template = help::SUB,
        before_help = "explain why a chat is, or is not, indexed - without indexing anything",
        after_help = help::WHY
    )]
    Why {
        #[arg(
            value_name = "REFERENCE",
            help = "session id/prefix, @handle, alias, project label, first-line substring, or transcript path"
        )]
        reference: String,
        #[arg(
            long,
            help = "one {\"version\":1,\"verdict\":...} object with every evidence field"
        )]
        json: bool,
    },
    #[command(
        about = "per-project briefing: work done, open items, estimated time",
        help_template = help::SUB,
        before_help = "per-project briefings from indexed chats: estimated active time, chats and turns, what was worked on, what is still open",
        after_help = help::SUMMARY
    )]
    Summary(Summary),
    #[command(
        about = "recall over several queries: merged, deduped, one budget",
        help_template = help::SUB,
        before_help = "several queries merged and deduped within one byte budget",
        after_help = help::PACK,
        mut_arg("query", |a| a.help("queries, each searched separately")),
        mut_arg("who", |a| a.help(help::RECALL_WHO)),
        mut_arg("no_who", |a| a.help(help::RECALL_NO_WHO)),
        mut_arg("no_self", |a| a.help(help::RECALL_NO_SELF)),
        mut_arg("all_side_chats", |a| a.help(help::RECALL_ALL_SIDE_CHATS)),
        mut_arg("no_side", |a| a.help(help::RECALL_NO_SIDE))
    )]
    Pack(Recall),
    #[command(
        about = "build the required search index; offer optional semantics, agent instructions, and recovery archives",
        help_template = help::SUB,
        before_help = "build the required search index; optionally prepare meaning search, agent instructions, and recovery archives",
        after_help = help::SETUP
    )]
    Setup {
        #[arg(
            short = 'y',
            long,
            help = "accept both instruction and archive prompts (for scripts)"
        )]
        yes: bool,
        #[arg(
            long,
            help = "do not write recall instructions into agent config files"
        )]
        no_teach: bool,
        #[arg(
            long,
            help = "do not install post-compact recovery integrations (claude, codex, pi, and OMP)"
        )]
        no_hook: bool,
        #[arg(long, help = "skip the semantic model download for this setup run")]
        no_semantic: bool,
        #[arg(
            long,
            conflicts_with = "no_archive",
            help = "enable compressed source-store recovery retention"
        )]
        archive: bool,
        #[arg(long, help = "explicitly keep source-store recovery retention off")]
        no_archive: bool,
    },
    #[command(
        about = "rebuild the base index from your agent stores",
        after_help = help::INDEX
    )]
    Index {
        #[arg(long, help = "ignore the parse cache; re-read every store file")]
        full: bool,
        #[arg(
            long,
            help = "also download the semantic model if it is missing and embed every row before exiting"
        )]
        semantic: bool,
    },
    #[command(
        about = "refresh ingest, search, and semantic embeddings",
        help_template = help::SUB,
        before_help = "refresh transcript ingest, keyword search, and semantic embeddings",
        after_help = help::REINDEX
    )]
    Reindex {
        #[arg(
            long,
            help = "also rebuild every semantic embedding (reindex always reparses every source)"
        )]
        full: bool,
        #[arg(
            long,
            value_name = "MAX_NEW",
            allow_negative_numbers = true,
            help = "cap messages embedded this run (background/bounded refresh)"
        )]
        max_new: Option<i64>,
    },
    #[command(
        about = "bounded diagnostic; --deep runs full integrity checks and --fix repairs safe faults",
        help_template = help::SUB,
        before_help = help::DOCTOR_ABOUT,
        after_help = help::DOCTOR
    )]
    Doctor {
        #[arg(
            long,
            help = "also check the index's SQLite integrity and the semantic model"
        )]
        deep: bool,
        #[arg(
            long,
            help = "refresh sources, repair enrolled integrations, and rebuild a corrupt index after confirmation"
        )]
        fix: bool,
        #[arg(
            long,
            requires = "deep",
            help = "with --deep, skip the semantic model check"
        )]
        no_semantic: bool,
        #[arg(long, help = "machine-readable report")]
        json: bool,
    },
    #[command(
        about = "prove the ingest misses nothing: per-file intake accounting vs an independent raw census",
        help_template = help::SUB,
        before_help = "hold the ingest to its accounting: nothing seen goes missing",
        after_help = help::AUDIT
    )]
    Audit {
        #[arg(long, value_name = "AGENT", help = "audit only this agent")]
        agent: Vec<String>,
        #[arg(long, help = "machine output")]
        json: bool,
        #[arg(
            long,
            help = "promote coverage gaps and live/uncertified evidence to errors (exit 2)"
        )]
        strict: bool,
        #[arg(
            long,
            help = "reparse every source with the adapters and compare its text with the index"
        )]
        full: bool,
    },
    #[command(
        about = "snapshot agent store files: files byte-for-byte, sqlite via backup api, deduped, compressed",
        help_template = help::SUB,
        before_help = "snapshot the agents' own store files (files byte-for-byte, sqlite via backup api; content-addressed, compressed); `agrep restore` brings one back",
        after_help = help::ARCHIVE
    )]
    Archive {
        #[arg(
            long,
            group = "action",
            help = "enable scheduled captures by the background worker"
        )]
        on: bool,
        #[arg(
            long,
            group = "action",
            help = "disable scheduled captures (archived copies stay until --purge)"
        )]
        off: bool,
        #[arg(long, group = "action", help = "show archive size and retention")]
        status: bool,
        #[arg(
            long,
            group = "action",
            value_name = "N",
            help = "versions kept per file (persists; 0 = never prune; default 3)"
        )]
        keep: Option<usize>,
        #[arg(long, group = "action", help = "prune old versions now")]
        prune: bool,
        #[arg(
            long,
            group = "action",
            help = "delete every archived copy and the catalog; the on/off and keep settings stay"
        )]
        purge: bool,
        #[arg(long, help = "with --purge: skip the confirmation")]
        yes: bool,
    },
    #[command(
        about = "bring an archived store file back, exact bytes, resumable",
        help_template = help::SUB,
        before_help = "restore an archived store file, hash-verified, back to its path so its agent can try to resume the session",
        after_help = help::RESTORE
    )]
    Restore {
        #[arg(help = "session id (8+ chars) or a path substring")]
        needle: String,
        #[arg(
            long,
            value_name = "DIR",
            help = "restore into this directory instead of the original path"
        )]
        to: Option<String>,
        #[arg(long, help = "overwrite an existing live file")]
        force: bool,
    },
    #[command(
        about = "change a setting (`agrep set` lists them)",
        override_usage = help::SET_USAGE,
        after_help = help::SET
    )]
    Set {
        #[arg(
            num_args = 0..,
            value_name = "KEY VALUE",
            help = "a setting and its new value; none lists the current settings"
        )]
        values: Vec<String>,
    },
    #[command(
        about = "remove the agent-teaching integration (blocks and hooks; index/data untouched)",
        help_template = help::SUB,
        before_help = "remove agrep's agent instructions and post-compact hooks; the search index and source histories are left untouched",
        after_help = help::REMOVE
    )]
    Remove,
    #[command(
        about = "follow live agent events as JSON lines (turn ends by default)",
        help_template = help::SUB,
        before_help = "agrep tail: follow live agent-session events as JSON lines on stdout.",
        after_help = help::TAIL
    )]
    Tail {
        #[arg(
            long,
            value_name = "AGENT",
            help = "store filter: claude/codex/opencode/antigravity/cursor/pi/agy/omp (repeatable or comma-separated)"
        )]
        agent: Vec<String>,
        #[arg(
            long,
            visible_alias = "chat",
            value_name = "SESSION",
            help = "substring filter on the session/chat id"
        )]
        session: Option<String>,
        #[arg(
            long,
            value_name = "EVENTS",
            help = "comma list (done/queued/reply/subagent_result/tool/tool_done/user), or \"all\" (default: done = turn ends)"
        )]
        events: Option<String>,
        #[arg(long, help = "print the current active-session state once and exit")]
        snapshot: bool,
    },
    #[command(
        about = "launch an agent, recording child-process liveness",
        help_template = help::SUB,
        before_help = "launch an agent, recording child-process liveness from start",
        override_usage = help::RUN_USAGE,
        after_help = help::RUN
    )]
    Run {
        #[arg(
            value_name = "AGENT",
            help = "agent to launch (claude/codex/opencode/antigravity/pi)"
        )]
        agent: String,
        #[arg(long, value_name = "DIR", help = "working directory for the agent")]
        cwd: Option<std::path::PathBuf>,
        #[arg(
            trailing_var_arg = true,
            allow_hyphen_values = true,
            value_name = "AGENT_ARGS",
            help = "arguments passed to the agent; put them after `--`"
        )]
        args: Vec<std::ffi::OsString>,
    },
    #[command(name = "__work", hide = true)]
    Work { tasks: Vec<String> },
    #[command(name = "__census", hide = true)]
    Census,
    #[command(name = "__hook", hide = true)]
    Hook { agent: String },
}
impl Scope {
    pub(crate) fn filters(&self) -> Result<Filters> {
        let (since, until) = cmd::usage::bounds(self.since.as_deref(), self.until.as_deref())?;
        Ok(Filters {
            agent: self.agent.clone(),
            project: if self.here {
                Some(
                    std::env::current_dir()?
                        .file_name()
                        .context("working directory has no name")?
                        .to_string_lossy()
                        .into(),
                )
            } else {
                self.project.clone()
            },
            exclude_project: self.exclude_project.clone(),
            model: self.model.clone(),
            model_soft: self.soft,
            who: self.who.clone(),
            no_who: self.no_who.clone(),
            no_meta: self.no_meta,
            chat: self.chat.clone(),
            since,
            until,
            no_side: self.no_side,
            caller: None,
            session_rows: None,
            sessions: None,
        })
    }
}
