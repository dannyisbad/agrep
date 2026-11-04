use agrep_core::launch;
use anyhow::Result;
use clap::CommandFactory;
use std::{ffi::OsString, path::PathBuf};
/// `escaped` says `args` followed a `--`.
pub fn run(
    agent: String,
    mut cwd: Option<PathBuf>,
    args: Vec<OsString>,
    escaped: bool,
) -> Result<i32> {
    // 0.4.9 read `run` with argparse's parse_known_args (cli.py:1797-1813): `--cwd` and
    // `-h` count anywhere before a `--`, and every other word passes to the agent in order.
    let mut passed = Vec::with_capacity(args.len());
    let mut words = args.into_iter().peekable();
    if escaped {
        // argparse kept a second `--` among the extras, and 0.4.9 dropped it too
        // (cli.py:1810-1812).
        words.next_if(|w| w.as_os_str() == "--");
        passed.extend(&mut words);
    }
    while let Some(word) = words.next() {
        match word.to_str() {
            Some("--") => {
                passed.push(word);
                passed.extend(words);
                break;
            }
            Some("--cwd") => match words.next() {
                Some(dir) => cwd = Some(dir.into()),
                None => {
                    return Err(super::usage::refusal(
                        "agrep run",
                        "argument --cwd: expected one argument",
                    ))
                }
            },
            Some(w) if w.starts_with("--cwd=") => cwd = Some(w["--cwd=".len()..].into()),
            Some("-h" | "--help") => {
                if let Err(help) =
                    crate::Cli::command().try_get_matches_from(["agrep", "run", "-h"])
                {
                    help.print()?;
                }
                return Ok(0);
            }
            _ => passed.push(word),
        }
    }
    // 0.4.9 logged the refusal as a bare line (cli.py:1814-1820).
    launch::capability(&agent).map_err(|e| super::usage::refusal("", e))?;
    let Some(exe) = launch::choose_executable(&agent, None)? else {
        eprintln!("the {agent} CLI is not on PATH");
        return Ok(127);
    };
    let cwd = cwd.unwrap_or(std::env::current_dir()?);
    launch::run(&exe, &passed, &cwd)
}
