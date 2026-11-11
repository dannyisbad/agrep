mod cli;
mod cmd;
mod dispatch;
mod freshness;
mod help;
use agrep_core::render;
use clap::FromArgMatches;
use cli::*;
use freshness::{compact, finish_census, ready};
// Parse workers create and retire many differently sized strings. Return empty
// pages after the writer frees them instead of retaining the parse high-water mark.
#[cfg(target_os = "macos")]
mod allocator;
#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;
fn main() {
    #[cfg(target_os = "macos")]
    allocator::initialize();
    agrep_core::data::resolve_path_variables();
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
        "__work",
        "__census",
        "__hook",
    ];
    // `agrep help [COMMAND]` shows that help; `agrep search help` searches the word.
    if args.get(1).is_some_and(|first| first == "help") {
        let request = help_request(&args[2..], &commands).unwrap_or_else(|message| {
            agrep_core::errln!("{}", render::safe(&message));
            agrep_core::semantic_profile::process_endpoint("process_exit_ns");
            std::process::exit(2);
        });
        args.truncate(1);
        args.extend(request.into_iter().map(Into::into));
    }
    // `agrep version` alone prints the version; `agrep version bump` still searches.
    if args.len() == 2 && args[1] == "version" {
        args[1] = "--version".into();
    }
    // 0.4.9's dispatch (cli.py:1884-1929): any first word that is not a command, an option
    // included, starts a search, so `agrep --no-auto search X` searches "search X".
    if args.get(1).is_some_and(|first| {
        first.to_str().is_none_or(|w| {
            !commands.contains(&w) && !["-h", "--help", "-V", "--version"].contains(&w)
        })
    }) {
        args.insert(1, "search".into());
    }
    // 0.4.9 refused these commands' unknown --agent before reading their arguments
    // (cli.py:1703-1767).
    if args
        .get(1)
        .and_then(|c| c.to_str())
        .is_some_and(|c| ["search", "chats", "recall", "summary", "pack"].contains(&c))
    {
        if let Some(message) = cmd::usage::unknown_agent(&args[2..]) {
            agrep_core::errln!("{}", render::safe(&message));
            agrep_core::semantic_profile::process_endpoint("process_exit_ns");
            std::process::exit(2);
        }
    }
    let parsed = help::command()
        .try_get_matches_from(&args)
        .and_then(|matches| Ok((Cli::from_arg_matches(&matches)?, matches)));
    let (cli, matches) = match parsed {
        Ok(parsed) => parsed,
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
            agrep_core::errln!(
                "{}",
                agrep_core::surface::safe(error.to_string().trim_end(), true)
            );
            agrep_core::semantic_profile::process_endpoint("process_exit_ns");
            std::process::exit(2);
        }
    };
    let invocation = cmd::usage::Invocation::new(&args, &matches);
    drop(parse_span);
    let result = {
        let _span = agrep_core::semantic_profile::span("command");
        dispatch::run(cli, &invocation)
    };
    match result {
        Ok(code) => {
            agrep_core::semantic_profile::process_endpoint("process_exit_ns");
            std::process::exit(code)
        }
        Err(e) => {
            match e.downcast_ref::<cmd::usage::Refusal>() {
                Some(refusal) => agrep_core::errln!("{}", render::safe(&refusal.to_string())),
                None => agrep_core::errln!("agrep: {}", render::safe(&format!("{e:#}"))),
            }
            agrep_core::semantic_profile::process_endpoint("process_exit_ns");
            std::process::exit(2)
        }
    }
}
/// The arguments `agrep help ...` stands for.
fn help_request(rest: &[std::ffi::OsString], commands: &[&str]) -> Result<Vec<String>, String> {
    let words: Vec<&str> = rest.iter().map(|w| w.to_str().unwrap_or("")).collect();
    match words.as_slice() {
        [] | ["-h" | "--help"] => Ok(vec!["--help".into()]),
        [command] if !command.starts_with("__") && commands.contains(command) => {
            Ok(vec![command.to_string(), "--help".into()])
        }
        _ => {
            let refusal = match rest {
                [word] => format!("unknown command '{}'", word.to_string_lossy()),
                _ => "help takes one command name".into(),
            };
            let search: Vec<String> = ["agrep", "search", "help"]
                .into_iter()
                .map(String::from)
                .chain(rest.iter().map(|w| w.to_string_lossy().into_owned()))
                .collect();
            Err(format!(
                "agrep: {refusal} (agrep --help lists the commands); to search for these words, run: {}",
                agrep_core::surface::shell(&search)
            ))
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    use clap::CommandFactory;
    // Each command's options as 0.4.9's `--help` listed them, less search's
    // `--strict-semantic`, plus `index --semantic` and `--no-auto` on status and resume
    // (docs/PORTING.md).
    #[test]
    fn every_command_offers_its_own_options_each_described() {
        let scope = [
            "agent",
            "project",
            "here",
            "exclude-project",
            "model",
            "soft",
            "who",
            "no-who",
            "no-meta",
            "chat",
            "since",
            "until",
            "self",
            "no-self",
            "all-side-chats",
            "no-side",
        ];
        let recall: Vec<&str> = ["hits", "budget", "context", "semantic", "lexical"]
            .into_iter()
            .chain(scope)
            .chain(["json", "probe", "no-auto", "color"])
            .collect();
        let search: Vec<&str> = [
            "max",
            "regex",
            "word",
            "ignore-case",
            "chats",
            "count",
            "count-by-tier",
            "more",
            "classic",
            "flat",
        ]
        .into_iter()
        .chain(scope)
        .chain([
            "sort", "semantic", "lexical", "coverage", "json", "no-auto", "color",
        ])
        .collect();
        let expected: &[(&str, &[&str])] = &[
            ("search", &search),
            (
                "around",
                &[
                    "context",
                    "whole",
                    "max-chars",
                    "full",
                    "no-tools",
                    "tool-output",
                    "who",
                    "json",
                    "no-auto",
                    "color",
                ],
            ),
            ("recall", &recall),
            (
                "chats",
                &[
                    "max",
                    "side",
                    "agent",
                    "project",
                    "here",
                    "exclude-project",
                    "since",
                    "until",
                    "self",
                    "no-self",
                    "no-side",
                    "json",
                    "no-auto",
                    "color",
                ],
            ),
            (
                "postcompact",
                &["json", "no-auto", "session", "boundary-ms"],
            ),
            (
                "board",
                &[
                    "agent",
                    "session",
                    "once",
                    "json",
                    "sort",
                    "project",
                    "state",
                    "roots",
                    "side",
                    "side-only",
                    "max",
                    "wait",
                    "expanded",
                ],
            ),
            ("resume", &["max", "list", "no-auto", "color"]),
            ("status", &["json", "no-auto"]),
            ("why", &["json"]),
            (
                "summary",
                &[
                    "since", "until", "project", "agent", "group", "idle-cap", "max", "self",
                    "no-self", "json", "no-auto", "color",
                ],
            ),
            ("pack", &recall),
            (
                "setup",
                &[
                    "yes",
                    "no-teach",
                    "no-hook",
                    "no-semantic",
                    "archive",
                    "no-archive",
                ],
            ),
            ("index", &["full", "semantic"]),
            ("reindex", &["full", "max-new"]),
            ("doctor", &["deep", "fix", "no-semantic", "json"]),
            ("audit", &["agent", "json", "strict", "full"]),
            (
                "archive",
                &["on", "off", "status", "keep", "prune", "purge", "yes"],
            ),
            ("restore", &["to", "force"]),
            ("set", &[]),
            ("remove", &[]),
            ("tail", &["agent", "session", "events", "snapshot"]),
            ("run", &["cwd"]),
        ];
        let cli = Cli::command();
        assert!(cli.get_arguments().next().is_none());
        let shown: Vec<_> = cli.get_subcommands().filter(|c| !c.is_hide_set()).collect();
        assert_eq!(
            shown.iter().map(|c| c.get_name()).collect::<Vec<_>>(),
            expected.iter().map(|(name, _)| *name).collect::<Vec<_>>()
        );
        for (command, (name, options)) in shown.iter().zip(expected) {
            assert!(command.get_about().is_some(), "{name}");
            let mut offered: Vec<_> = command
                .get_arguments()
                .filter_map(|a| a.get_long())
                .collect();
            let mut options = options.to_vec();
            offered.sort_unstable();
            options.sort_unstable();
            assert_eq!(offered, options, "{name}");
            for arg in command.get_arguments() {
                assert!(arg.get_help().is_some(), "{name} {}", arg.get_id());
            }
        }
    }
}
