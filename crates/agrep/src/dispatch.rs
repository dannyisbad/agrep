use crate::{cli::*, cmd};
use agrep_core::data::DataRoot;
use anyhow::Result;
pub(crate) fn run(cli: Cli, invocation: &cmd::usage::Invocation) -> Result<i32> {
    match cli.command {
        Some(Command::Search(s)) => {
            cmd::usage::search(&s, invocation)?;
            if let Some(handle) = &s.more {
                return cmd::search::more(handle);
            }
            let (no_auto, color) = (s.no_auto, s.color.clone());
            cmd::search::run(s, no_auto, &color)
        }
        Some(Command::Around(a)) => {
            cmd::usage::around(&a, invocation)?;
            let (no_auto, color) = (a.no_auto, a.color.clone());
            cmd::around::run(a, no_auto, &color)
        }
        Some(Command::Recall(a)) => {
            cmd::usage::recall(&a, invocation, "recall")?;
            let (no_auto, color) = (a.no_auto, a.color.clone());
            cmd::recall::run(a, false, no_auto, &color)
        }
        Some(Command::Pack(a)) => {
            cmd::usage::recall(&a, invocation, "pack")?;
            let (no_auto, color) = (a.no_auto, a.color.clone());
            cmd::pack::run(a, no_auto, &color)
        }
        Some(Command::Postcompact(a)) => {
            let no_auto = a.no_auto;
            cmd::postcompact::run(a, no_auto)
        }
        Some(Command::Board(a)) => cmd::board::run(a),
        Some(Command::Tail {
            agent,
            session,
            events,
            snapshot,
        }) => {
            cmd::usage::tail(invocation)?;
            cmd::tail::run(agent, session, events, snapshot)
        }
        Some(Command::Chats(s)) => {
            cmd::usage::chats(&s, invocation)?;
            let (no_auto, color) = (s.no_auto, s.color.clone());
            cmd::chats::run(s, no_auto, &color)
        }
        Some(Command::Summary(s)) => {
            cmd::usage::summary(&s)?;
            let (no_auto, color) = (s.no_auto, s.color.clone());
            cmd::summary::run(s, no_auto, &color)
        }
        Some(Command::Resume {
            reference,
            max,
            list,
            no_auto,
            color,
        }) => cmd::resume::run(reference, max, list, no_auto, &color),
        Some(Command::Run { agent, cwd, args }) => {
            let escaped = invocation.escaped(args.len());
            cmd::run::run(agent, cwd, args, escaped)
        }
        Some(Command::Doctor {
            deep,
            fix,
            no_semantic,
            json,
        }) => {
            cmd::usage::doctor(invocation)?;
            cmd::doctor::run(deep, fix, no_semantic, json)
        }
        Some(Command::Audit {
            agent,
            json,
            strict,
            full,
        }) => cmd::audit::run(agent, json, strict, full),
        Some(Command::Why { reference, json }) => cmd::why::run(reference, json),
        Some(Command::Index { full, semantic }) => cmd::index::run(full, semantic),
        Some(Command::Reindex { full, max_new }) => cmd::index::reindex(full, max_new),
        None => cmd::status::run(false, false),
        Some(Command::Status { json, no_auto }) => cmd::status::run(json, no_auto),
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
            agrep_core::outln!(
                "{}",
                agrep_core::json_output::to_string(&agrep_core::freshness::pending(&root)?)?
            )?;
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
