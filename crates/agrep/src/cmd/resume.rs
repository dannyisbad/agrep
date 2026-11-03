use crate::ready;
use agrep_core::{history as h, launch, render, store};
use anyhow::{bail, Result};
use std::{
    io::{IsTerminal, Write},
    path::PathBuf,
};
fn label(s: &h::Session, color: &str) -> String {
    format!(
        "{} · {}  {}  {}",
        super::paint(&s.agent, color),
        render::safe(&s.project),
        h::target(&s.id),
        render::safe(&h::one_line(&s.title, 70))
    )
}
pub fn run(
    reference: Option<String>,
    max: usize,
    list: bool,
    no_auto: bool,
    color: &str,
) -> Result<i32> {
    if list && reference.is_some() {
        bail!("--list cannot be combined with a reference")
    }
    let (root, census) = ready(no_auto)?;
    let c = store::reader(&root)?;
    let all = h::sessions(&c)?;
    crate::finish_census(&root, census)?;
    let (mut selected, human) = if let Some(reference) = reference.as_ref() {
        h::resolve_indexed(&c, &all, reference)?
    } else {
        (all.iter().filter(|s| !s.side).collect(), true)
    };
    if selected.is_empty() {
        eprintln!("no matching session; run agrep chats");
        return Ok(if list || reference.is_none() { 2 } else { 1 });
    }
    if list {
        if max > 0 {
            selected.truncate(max)
        }
        for s in selected {
            println!("{}", label(s, color));
        }
        return Ok(0);
    }
    let chosen = if selected.len() == 1 && reference.is_some() {
        selected[0]
    } else {
        if max > 0 {
            selected.truncate(max)
        }
        for (n, s) in selected.iter().enumerate() {
            eprintln!("{:>2}  {}", n + 1, label(s, color));
        }
        if !human || !std::io::stdin().is_terminal() {
            eprintln!("pass a unique session reference to resume");
            return Ok(if reference.is_none() { 0 } else { 1 });
        }
        eprint!("resume # (enter to cancel): ");
        std::io::stderr().flush()?;
        let mut input = String::new();
        std::io::stdin().read_line(&mut input)?;
        if input.trim().is_empty() {
            return Ok(0);
        }
        if let Ok(n) = input.trim().parse::<usize>() {
            *selected
                .get(n.wrapping_sub(1))
                .ok_or_else(|| anyhow::anyhow!("invalid selection"))?
        } else {
            let (matches, _) = h::resolve(&all, input.trim())?;
            if matches.len() != 1 {
                bail!("selection is not unique")
            }
            matches[0]
        }
    };
    let chosen = match h::root(&all, chosen) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("{}", render::safe(&e.to_string()));
            return Ok(1);
        }
    };
    launch::capability(&chosen.agent)?;
    let source:String=c.query_row("SELECT src.path FROM sources src JOIN prose p ON p.source=src.rowid WHERE p.session=?1 AND p.agent=?2 AND p.visible=1 LIMIT 1",[&chosen.id,&chosen.agent],|r|r.get(0)).unwrap_or_default();
    let preferred = if source.replace('\\', "/").contains("/.omp/") {
        Some("omp")
    } else {
        Some("pi")
    };
    let Some(exe) = launch::choose_executable(&chosen.agent, preferred)? else {
        eprintln!("the {} CLI is not on PATH", chosen.agent);
        return Ok(127);
    };
    let cwd = if !chosen.cwd.is_empty() {
        PathBuf::from(&chosen.cwd)
    } else {
        std::env::var_os("AGREP_HOME")
            .or_else(|| std::env::var_os("HOME"))
            .map(PathBuf::from)
            .ok_or_else(|| anyhow::anyhow!("session has no working directory"))?
    };
    if !cwd.is_dir() {
        bail!(
            "session working directory does not exist: {}",
            cwd.display()
        )
    }
    eprintln!(
        "↻ resuming {} · {}",
        chosen.agent,
        render::safe(&cwd.to_string_lossy())
    );
    let mut target = chosen.clone();
    target.cwd = cwd.to_string_lossy().into_owned();
    launch::run(&exe, &launch::resume_args(&target)?, &cwd)
}
