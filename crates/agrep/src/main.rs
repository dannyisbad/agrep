mod cmd;
use agrep_core::{data::DataRoot, query::Filters, render, semantic, store};
use anyhow::{bail, Context, Result};
use clap::{Args, Parser, Subcommand};
// On hosted Windows runners the allocation-heavy index phases (parsing, page
// packing) ran 2-3x slower than the same work on macOS under the system heap.
#[cfg(windows)]
#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;
#[derive(Parser)]
#[command(
    version,
    about = "Search local agent conversations",
    disable_help_subcommand = true
)]
struct Cli {
    #[arg(long, global = true)]
    no_auto: bool,
    #[arg(long,global=true,value_parser=["auto","always","never"],default_value="auto")]
    color: String,
    #[arg(long, global = true)]
    more: Option<String>,
    #[command(subcommand)]
    command: Option<Command>,
}
#[derive(Args, Default)]
struct Scope {
    #[arg(long)]
    agent: Option<String>,
    #[arg(long, conflicts_with = "here")]
    project: Option<String>,
    #[arg(long)]
    here: bool,
    #[arg(long)]
    exclude_project: Option<String>,
    #[arg(long)]
    model: Option<String>,
    #[arg(long, visible_alias = "model-soft")]
    soft: bool,
    #[arg(long)]
    who: Option<String>,
    #[arg(long)]
    no_who: Option<String>,
    #[arg(long)]
    no_meta: bool,
    #[arg(long, visible_alias = "session")]
    chat: Option<String>,
    #[arg(long)]
    since: Option<String>,
    #[arg(long, visible_alias = "before")]
    until: Option<String>,
    #[arg(long = "self", conflicts_with = "no_self")]
    include_self: bool,
    #[arg(long)]
    no_self: bool,
    #[arg(long, conflicts_with = "no_side")]
    all_side_chats: bool,
    #[arg(long)]
    no_side: bool,
}
#[derive(Args)]
struct Search {
    #[arg(required=true,num_args=1..)]
    pattern: Vec<String>,
    #[arg(short = 'n', long = "max")]
    max: Option<usize>,
    #[arg(short = 'E', long, conflicts_with = "word")]
    regex: bool,
    #[arg(short = 'w', long)]
    word: bool,
    #[arg(short = 'i', long)]
    ignore_case: bool,
    #[arg(short = 'l', long)]
    chats: bool,
    #[arg(short = 'c', long, conflicts_with = "semantic")]
    count: bool,
    #[arg(long, conflicts_with = "semantic")]
    count_by_tier: bool,
    #[arg(long)]
    classic: bool,
    #[arg(long)]
    flat: bool,
    #[arg(long)]
    json: bool,
    #[arg(long,value_parser=["score","time","position"],default_value="score")]
    sort: String,
    #[arg(short='s',long,conflicts_with_all=["lexical","regex","word"])]
    semantic: bool,
    #[arg(long)]
    lexical: bool,
    #[arg(long)]
    coverage: bool,
    #[command(flatten)]
    scope: Scope,
}
#[derive(Args, Default)]
struct ChatScope {
    #[arg(long)]
    agent: Option<String>,
    #[arg(long, conflicts_with = "here")]
    project: Option<String>,
    #[arg(long)]
    here: bool,
    #[arg(long)]
    exclude_project: Option<String>,
    #[arg(long)]
    since: Option<String>,
    #[arg(long, visible_alias = "before")]
    until: Option<String>,
    #[arg(long = "self", conflicts_with = "no_self")]
    include_self: bool,
    #[arg(long)]
    no_self: bool,
    #[arg(long)]
    no_side: bool,
}
impl ChatScope {
    fn filters(&self) -> Result<Filters> {
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
struct Chats {
    #[arg(num_args=0..)]
    pattern: Vec<String>,
    #[arg(short = 'n', long = "max", default_value_t = 20)]
    max: usize,
    #[arg(long, conflicts_with = "no_side")]
    side: bool,
    #[arg(long)]
    json: bool,
    #[command(flatten)]
    scope: ChatScope,
}
#[derive(Args)]
struct Around {
    session: String,
    #[arg(allow_negative_numbers = true)]
    turn: Option<i64>,
    #[arg(short = 'C', long, visible_alias = "radius")]
    context: Option<String>,
    #[arg(long)]
    whole: bool,
    #[arg(long)]
    max_chars: Option<usize>,
    #[arg(long)]
    full: bool,
    #[arg(long)]
    no_tools: bool,
    #[arg(long)]
    tool_output: Option<usize>,
    #[arg(long)]
    who: Option<String>,
    #[arg(long)]
    json: bool,
}
#[derive(Args)]
struct Recall {
    #[arg(required=true,num_args=1..)]
    query: Vec<String>,
    #[arg(long)]
    hits: Option<usize>,
    #[arg(long, default_value_t = 200000)]
    budget: usize,
    #[arg(short = 'C', long, default_value_t = 2)]
    context: usize,
    #[arg(short = 's', long, conflicts_with = "lexical")]
    semantic: bool,
    #[arg(long)]
    lexical: bool,
    #[arg(long)]
    json: bool,
    #[arg(long)]
    probe: bool,
    #[command(flatten)]
    scope: Scope,
}
#[derive(Args)]
struct Postcompact {
    #[arg(long)]
    json: bool,
    #[arg(long)]
    session: Option<String>,
    #[arg(long)]
    boundary_ms: Option<i64>,
}
#[derive(Args)]
struct Summary {
    #[arg(value_parser=["pending","time"])]
    mode: Option<String>,
    #[arg(long)]
    since: Option<String>,
    #[arg(long, visible_alias = "before")]
    until: Option<String>,
    #[arg(long)]
    project: Option<String>,
    #[arg(long)]
    agent: Option<String>,
    #[arg(long,value_parser=["day","week","month"])]
    group: Option<String>,
    #[arg(long, default_value = "20m")]
    idle_cap: String,
    #[arg(short = 'n', long = "max")]
    max: Option<usize>,
    #[arg(long = "self", conflicts_with = "no_self")]
    include_self: bool,
    #[arg(long)]
    no_self: bool,
    #[arg(long)]
    json: bool,
}
#[derive(Args)]
struct Board {
    #[arg(long)]
    agent: Option<String>,
    #[arg(long, visible_alias = "chat")]
    session: Option<String>,
    #[arg(long)]
    once: bool,
    #[arg(long)]
    json: bool,
    #[arg(long,value_parser=["working","updated"],default_value="working")]
    sort: String,
    #[arg(long)]
    project: Option<String>,
    #[arg(long,value_parser=["all","active","working","idle"],default_value="all")]
    state: String,
    #[arg(long,conflicts_with_all=["side","side_only"])]
    roots: bool,
    #[arg(long, conflicts_with = "side_only")]
    side: bool,
    #[arg(long)]
    side_only: bool,
    #[arg(short = 'n', long = "max")]
    max: Option<usize>,
    #[arg(long)]
    wait: Option<f64>,
    #[arg(long)]
    expanded: bool,
}
#[derive(Subcommand)]
enum Command {
    Search(Search),
    Chats(Chats),
    Around(Around),
    Recall(Recall),
    Pack(Recall),
    Postcompact(Postcompact),
    Summary(Summary),
    Board(Board),
    Tail {
        #[arg(long)]
        agent: Vec<String>,
        #[arg(long, visible_alias = "chat")]
        session: Option<String>,
        #[arg(long)]
        events: Option<String>,
        #[arg(long)]
        snapshot: bool,
    },
    Resume {
        reference: Option<String>,
        #[arg(short = 'n', long = "max", default_value_t = 15)]
        max: usize,
        #[arg(short = 'l', long)]
        list: bool,
    },
    Run {
        agent: String,
        #[arg(long)]
        cwd: Option<std::path::PathBuf>,
        #[arg(last = true)]
        args: Vec<std::ffi::OsString>,
    },
    Index {
        #[arg(long)]
        full: bool,
        #[arg(long)]
        semantic: bool,
    },
    Reindex {
        #[arg(long)]
        full: bool,
    },
    /// Install selected agent instructions, recovery hooks and optional local features.
    #[command(
        after_help = "Examples:\n  agrep setup\n  agrep setup --yes --no-semantic --no-archive"
    )]
    Setup {
        #[arg(short = 'y', long)]
        yes: bool,
        #[arg(long)]
        no_teach: bool,
        #[arg(long)]
        no_hook: bool,
        #[arg(long)]
        no_semantic: bool,
        #[arg(long, conflicts_with = "no_archive")]
        archive: bool,
        #[arg(long)]
        no_archive: bool,
    },
    /// Remove enrolled integrations and marked agrep instruction blocks.
    Remove,
    /// Capture native conversation files or manage opt-in archive retention.
    #[command(
        after_help = "Examples:\n  agrep archive --on\n  agrep archive --status\n  agrep archive --prune"
    )]
    Archive {
        #[arg(long, group = "action")]
        on: bool,
        #[arg(long, group = "action")]
        off: bool,
        #[arg(long, group = "action")]
        status: bool,
        #[arg(long, group = "action")]
        keep: Option<usize>,
        #[arg(long, group = "action")]
        prune: bool,
        #[arg(long, group = "action")]
        purge: bool,
        #[arg(long)]
        yes: bool,
    },
    /// Restore a captured source or export matching files to a directory.
    #[command(
        after_help = "Examples:\n  agrep restore session-prefix --to ./recovered\n  agrep restore session-prefix --force"
    )]
    Restore {
        needle: String,
        #[arg(long)]
        to: Option<String>,
        #[arg(long)]
        force: bool,
    },
    /// Show or set tools, embeddings and the post_index command.
    #[command(
        after_help = "Examples:\n  agrep set tools off\n  agrep set embeddings auto\n  agrep set post_index off"
    )]
    Set {
        #[arg(num_args=0..)]
        values: Vec<String>,
    },
    Status {
        #[arg(long)]
        json: bool,
    },
    Doctor {
        #[arg(long)]
        deep: bool,
        #[arg(long)]
        fix: bool,
        #[arg(long, requires = "deep")]
        no_semantic: bool,
        #[arg(long)]
        json: bool,
    },
    Audit {
        #[arg(long)]
        agent: Vec<String>,
        #[arg(long)]
        json: bool,
        #[arg(long)]
        strict: bool,
        #[arg(long)]
        full: bool,
    },
    Why {
        reference: String,
        #[arg(long)]
        json: bool,
    },
    Ui {
        #[arg(long, default_value_t = 0)]
        port: u16,
    },
    Serve {
        #[arg(long, default_value_t = 0)]
        port: u16,
    },
    #[command(name = "__work", hide = true)]
    Work {
        tasks: Vec<String>,
    },
    #[command(name = "__census", hide = true)]
    Census,
    #[command(name = "__hook", hide = true)]
    Hook {
        agent: String,
    },
}
fn date(s: &str) -> Result<i64> {
    let units = [
        ("ms", 1.),
        ("s", 1000.),
        ("m", 60000.),
        ("h", 3600000.),
        ("d", 86400000.),
        ("w", 604800000.),
    ];
    for (suffix, mul) in units {
        if let Some(n) = s.strip_suffix(suffix) {
            let n: f64 = n.parse().context("invalid duration")?;
            if !n.is_finite() || n < 0. {
                bail!("invalid duration: {s}")
            }
            return Ok(chrono::Utc::now().timestamp_millis() - (n * mul) as i64);
        }
    }
    if let Ok(d) = chrono::DateTime::parse_from_rfc3339(s) {
        return Ok(d.timestamp_millis());
    }
    let d = chrono::NaiveDate::parse_from_str(s, "%Y-%m-%d")
        .context("expected a duration or ISO date")?;
    Ok(d.and_hms_opt(0, 0, 0).unwrap().and_utc().timestamp_millis())
}
impl Scope {
    fn filters(&self) -> Result<Filters> {
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
            since: self.since.as_deref().map(date).transpose()?,
            until: self.until.as_deref().map(date).transpose()?,
            no_side: self.no_side,
            caller: None,
            session_rows: None,
            sessions: None,
        })
    }
}
fn compact(classic: bool) -> bool {
    if classic {
        return false;
    }
    match std::env::var("AGREP_PROFILE").as_deref() {
        Ok("classic") => false,
        Ok("compact") => true,
        _ => agrep_core::caller::ENV_KEYS
            .iter()
            .any(|k| std::env::var(k).is_ok_and(|v| !v.is_empty())),
    }
}
type CensusTask = std::thread::JoinHandle<Result<agrep_core::freshness::Pending>>;
fn ready(no_auto: bool) -> Result<(DataRoot, Option<CensusTask>)> {
    let no_auto = no_auto || agrep_core::data::read_only();
    let root = DataRoot::discover()?;
    root.validate(!no_auto)?;
    let census = if no_auto {
        None
    } else if !root.db().exists() {
        store::refresh(&root, true, false)?;
        semantic::initialize(&root)?;
        None
    } else {
        let copy = root.clone();
        Some(std::thread::spawn(move || {
            let _span = agrep_core::semantic_profile::span("freshness");
            let pending = agrep_core::freshness::pending(&copy)?;
            agrep_core::semantic_profile::event(serde_json::json!({"freshness":pending}));
            Ok(pending)
        }))
    };
    Ok((root, census))
}
fn finish_census(root: &DataRoot, census: Option<CensusTask>) -> Result<usize> {
    let Some(task) = census else {
        semantic::schedule(root)?;
        return Ok(0);
    };
    let census = task
        .join()
        .map_err(|_| anyhow::anyhow!("source census failed"))??;
    if census.changed_sources > 0 {
        agrep_core::freshness::spawn(root, &std::env::current_exe()?)?;
    } else {
        if census.merge_pending {
            agrep_core::freshness::spawn_merge(root, &std::env::current_exe()?)?;
        }
        semantic::schedule(root)?;
    }
    Ok(census.changed_sources)
}

fn run(cli: Cli) -> Result<i32> {
    if let Some(handle) = cli.more {
        if cli.command.is_some() {
            bail!("--more cannot be combined with a command or query")
        }
        return cmd::search::more(&handle);
    }
    match cli.command {
        Some(Command::Search(s)) => cmd::search::run(s, cli.no_auto, &cli.color),
        Some(Command::Around(a)) => cmd::around::run(a, cli.no_auto, &cli.color),
        Some(Command::Recall(a)) => cmd::recall::run(a, false, cli.no_auto, &cli.color),
        Some(Command::Pack(a)) => cmd::pack::run(a, cli.no_auto, &cli.color),
        Some(Command::Postcompact(a)) => cmd::postcompact::run(a, cli.no_auto),
        Some(Command::Board(a)) => cmd::board::run(a),
        Some(Command::Tail {
            agent,
            session,
            events,
            snapshot,
        }) => cmd::tail::run(agent, session, events, snapshot),
        Some(Command::Serve { port }) => cmd::serve::run(port, cli.no_auto),
        Some(Command::Ui { port }) => cmd::ui::run(port, cli.no_auto),
        Some(Command::Chats(s)) => cmd::chats::run(s, cli.no_auto, &cli.color),
        Some(Command::Summary(s)) => cmd::summary::run(s, cli.no_auto, &cli.color),
        Some(Command::Resume {
            reference,
            max,
            list,
        }) => cmd::resume::run(reference, max, list, cli.no_auto, &cli.color),
        Some(Command::Run { agent, cwd, args }) => cmd::run::run(agent, cwd, args),
        Some(Command::Doctor {
            deep,
            fix,
            no_semantic,
            json,
        }) => cmd::doctor::run(deep, fix, no_semantic, json),
        Some(Command::Audit {
            agent,
            json,
            strict,
            full,
        }) => cmd::audit::run(agent, json, strict, full),
        Some(Command::Why { reference, json }) => cmd::why::run(reference, json),
        Some(Command::Index { full, semantic }) => cmd::index::run(full, semantic),
        Some(Command::Reindex { full }) => {
            let root = DataRoot::discover()?;
            root.validate(true)?;
            let _foreground = root.foreground()?;
            let result = store::reindex(&root)?;
            agrep_core::surface::line(&agrep_core::surface::refresh_json(&result)?)?;
            if full {
                semantic::reset(&root)?;
            }
            agrep_core::coverage::initialize(&root)?;
            if result.changed_rows > 0 {
                semantic::mark_index_changed(&root)?;
            }
            semantic::initialize(&root)?;
            cmd::work::after_index(&root, false)?;
            Ok(0)
        }
        None | Some(Command::Status { .. }) => cmd::status::run(
            matches!(cli.command, Some(Command::Status { json: true })),
            cli.no_auto,
        ),
        Some(Command::Set { values }) => cmd::set::run(values),
        Some(Command::Setup {
            yes,
            no_teach,
            no_hook,
            no_semantic,
            archive,
            no_archive,
        }) => cmd::setup::run(cmd::setup::Options {
            yes,
            no_teach,
            no_hook,
            no_semantic,
            archive,
            no_archive,
        }),
        Some(Command::Remove) => cmd::remove::run(),
        Some(Command::Archive {
            on,
            off,
            status,
            keep,
            prune,
            purge,
            yes,
        }) => cmd::archive::run(cmd::archive::Options {
            on,
            off,
            status,
            keep,
            prune,
            purge,
            yes,
        }),
        Some(Command::Restore { needle, to, force }) => cmd::restore::run(needle, to, force),
        Some(Command::Work { tasks }) => cmd::work::run(&tasks),
        Some(Command::Census) => {
            let root = DataRoot::discover()?;
            root.validate(false)?;
            println!(
                "{}",
                agrep_core::json_output::to_string(&agrep_core::freshness::pending(&root)?)?
            );
            Ok(0)
        }
        Some(Command::Hook { agent }) => {
            use std::io::Read;
            let mut input = String::new();
            if agent == "codex" {
                let mut bytes = Vec::new();
                std::io::stdin().take(65537).read_to_end(&mut bytes)?;
                input = String::from_utf8(bytes).unwrap_or_default();
            }
            agrep_core::integrations::hook(&agent, &input)?;
            Ok(0)
        }
    }
}
fn main() {
    agrep_core::semantic_profile::process_endpoint("process_entry_ns");
    let parse_span = agrep_core::semantic_profile::span("cli_parse");
    let mut args: Vec<_> = std::env::args_os().collect();
    let commands = [
        "search",
        "chats",
        "around",
        "recall",
        "pack",
        "postcompact",
        "summary",
        "board",
        "tail",
        "resume",
        "run",
        "index",
        "reindex",
        "setup",
        "remove",
        "archive",
        "restore",
        "set",
        "status",
        "doctor",
        "audit",
        "why",
        "ui",
        "serve",
        "__work",
        "__census",
        "__hook",
    ];
    let mut first_at = 1;
    while let Some(arg) = args.get(first_at).and_then(|v| v.to_str()) {
        match arg {
            "--no-auto" => first_at += 1,
            "--color" | "--more" => first_at += 2,
            _ if arg.starts_with("--color=") || arg.starts_with("--more=") => first_at += 1,
            _ => break,
        }
    }
    if let Some(first) = args.get(first_at).and_then(|v| v.to_str()) {
        if first == "--json" && args.len() == first_at + 1 {
            args.insert(first_at, "status".into());
        } else if !commands.contains(&first)
            && !["-h", "--help", "-V", "--version"].contains(&first)
        {
            args.insert(first_at, "search".into());
        }
    }
    let cli = match Cli::try_parse_from(args) {
        Ok(cli) => cli,
        Err(error)
            if matches!(
                error.kind(),
                clap::error::ErrorKind::DisplayHelp | clap::error::ErrorKind::DisplayVersion
            ) =>
        {
            let _ = error.print();
            agrep_core::semantic_profile::process_endpoint("process_exit_ns");
            std::process::exit(0);
        }
        Err(error) => {
            let text = error.to_string();
            eprintln!(
                "{}",
                render::safe(text.lines().next().unwrap_or("invalid arguments"))
            );
            agrep_core::semantic_profile::process_endpoint("process_exit_ns");
            std::process::exit(2);
        }
    };
    drop(parse_span);
    let result = {
        let _span = agrep_core::semantic_profile::span("command");
        run(cli)
    };
    match result {
        Ok(code) => {
            agrep_core::semantic_profile::process_endpoint("process_exit_ns");
            std::process::exit(code)
        }
        Err(e) => {
            eprintln!("agrep: {}", render::safe(&format!("{e:#}")));
            agrep_core::semantic_profile::process_endpoint("process_exit_ns");
            std::process::exit(2)
        }
    }
}
