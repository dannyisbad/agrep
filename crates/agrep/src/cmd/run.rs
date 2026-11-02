use agrep_core::launch;
use anyhow::Result;
use std::{ffi::OsString, path::PathBuf};
pub fn run(agent: String, cwd: Option<PathBuf>, args: Vec<OsString>) -> Result<i32> {
    launch::capability(&agent)?;
    let Some(exe) = launch::choose_executable(&agent, None)? else {
        eprintln!("the {agent} CLI is not on PATH");
        return Ok(127);
    };
    let cwd = cwd.unwrap_or(std::env::current_dir()?);
    launch::run(&exe, &args, &cwd)
}
