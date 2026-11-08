mod cli;
mod cmd;
mod dispatch;
mod freshness;
mod help;
use agrep_core::render;
use clap::{CommandFactory, FromArgMatches};
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
            eprintln!("{}", render::safe(&message));
            agrep_core::semantic_profile::process_endpoint("process_exit_ns");
            std::process::exit(2);
        }
    }
    let parsed = Cli::command()
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
            let text = error.to_string();
            eprintln!(
                "{}",
                render::safe(text.lines().next().unwrap_or("invalid arguments"))
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
                Some(refusal) => eprintln!("{}", render::safe(&refusal.to_string())),
                None => eprintln!("agrep: {}", render::safe(&format!("{e:#}"))),
            }
            agrep_core::semantic_profile::process_endpoint("process_exit_ns");
            std::process::exit(2)
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    // Each command's options as 0.4.9's `--help` listed them, less search's
    // `--strict-semantic`, plus `index --semantic` and `--no-auto` on status, resume, ui and
    // serve (docs/PORTING.md).
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
            ("ui", &["port", "no-auto"]),
            ("serve", &["port", "no-auto"]),
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
