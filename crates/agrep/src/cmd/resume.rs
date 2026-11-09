use crate::ready;
use agrep_core::{history as h, ingest::claude::launch as claude, launch, render, store};
use anyhow::{bail, Result};
use std::{
    collections::BTreeSet,
    io::{IsTerminal, Write},
    path::{Path, PathBuf},
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
    crate::finish_census(&root, census)?;
    let c = store::reader(&root)?;
    let all = h::sessions(&c)?;
    let (mut selected, human) = if let Some(reference) = reference.as_ref() {
        h::resolve_indexed(&c, &all, reference)?
    } else {
        (all.iter().filter(|s| !s.side).collect(), true)
    };
    if selected.is_empty() {
        agrep_core::errln!("no matching session; run agrep chats");
        return Ok(if list || reference.is_none() { 2 } else { 1 });
    }
    if list {
        if max > 0 {
            selected.truncate(max)
        }
        for s in selected {
            agrep_core::outln!("{}", label(s, color))?;
        }
        return Ok(0);
    }
    let chosen = if selected.len() == 1 && reference.is_some() {
        selected[0]
    } else {
        let shown = match max {
            0 => &selected[..],
            n => &selected[..n.min(selected.len())],
        };
        for (n, s) in shown.iter().enumerate() {
            agrep_core::errln!("{:>2}  {}", n + 1, label(s, color));
        }
        if !human || !std::io::stdin().is_terminal() {
            agrep_core::errln!("pass a unique session reference to resume");
            return Ok(if reference.is_none() { 0 } else { 1 });
        }
        match pick(shown, &selected)? {
            Some(s) => s,
            None => return Ok(0),
        }
    };
    let chosen = match h::root(&all, chosen) {
        Ok(s) => s,
        Err(e) => {
            agrep_core::errln!("{}", render::safe(&e.to_string()));
            return Ok(1);
        }
    };
    // 0.4.9 printed the refusal as a bare line (py/hookless/native.py:999-1002).
    launch::capability(&chosen.agent).map_err(|e| super::usage::refusal("", e))?;
    let source:String=c.query_row("SELECT src.path FROM sources src JOIN prose p ON p.source=src.rowid WHERE p.session=?1 AND p.agent=?2 AND p.visible=1 LIMIT 1",[&chosen.id,&chosen.agent],|r|r.get(0)).unwrap_or_default();
    let preferred = if source.replace('\\', "/").contains("/.omp/") {
        Some("omp")
    } else {
        Some("pi")
    };
    let Some(exe) = launch::choose_executable(&chosen.agent, preferred)? else {
        agrep_core::errln!("the {} CLI is not on PATH", chosen.agent);
        return Ok(127);
    };
    let anchored = (chosen.agent == "claude")
        .then(|| claude::anchor_cwd(&chosen.id))
        .flatten();
    let cwd = working_dir(
        anchored.as_deref().unwrap_or(&chosen.cwd),
        &agrep_core::ingest::home(),
    );
    if chosen.agent == "claude" {
        match claude::link_session(&chosen.id, &cwd) {
            Ok(Some(note)) => agrep_core::errln!("{note}"),
            Ok(None) => {}
            Err(e) => {
                agrep_core::errln!(
                    "couldn't prepare Claude resume: {}",
                    render::safe(&e.to_string())
                );
                return Ok(1);
            }
        }
    }
    let agent = launch::launched_agent(&chosen.agent, &exe);
    agrep_core::errln!(
        "↻ resuming {agent} · {}",
        render::safe(&cwd.to_string_lossy())
    );
    let mut target = chosen.clone();
    target.cwd = cwd.to_string_lossy().into_owned();
    match launch::run(&exe, &launch::resume_args(&target)?, &cwd) {
        Ok(code) => Ok(code),
        Err(e) => {
            agrep_core::errln!(
                "couldn't launch {agent}: {}",
                render::safe(&e.root_cause().to_string())
            );
            Ok(1)
        }
    }
}
/// 0.4.9's picker (py/resume.py:135-163): a listed number, or a reference resolved among
/// the picker's candidates. A number is only ever a list position, so a mistyped one never
/// resumes a session whose id happens to start with it. Anything else asks again; an
/// empty answer or the end of input cancels.
fn pick<'a>(
    shown: &[&'a h::Session],
    candidates: &[&'a h::Session],
) -> Result<Option<&'a h::Session>> {
    let mut input = String::new();
    loop {
        agrep_core::surface::note(format_args!("resume # (enter to cancel): "));
        std::io::stderr().flush()?;
        input.clear();
        std::io::stdin().read_line(&mut input)?;
        if !input.ends_with('\n') {
            // Ctrl-D: the cursor still sits on the prompt line.
            agrep_core::errln!();
            return Ok(None);
        }
        let answer = input.trim();
        if answer.is_empty() {
            return Ok(None);
        }
        let chosen = if answer.bytes().all(|b| b.is_ascii_digit()) {
            answer
                .parse::<usize>()
                .ok()
                .and_then(|n| shown.get(n.checked_sub(1)?).copied())
        } else {
            match h::resolve(candidates.iter().copied(), answer) {
                Ok((matches, _)) => (matches.len() == 1).then(|| matches[0]),
                Err(e) => {
                    agrep_core::errln!("{}", render::safe(&e.to_string()));
                    continue;
                }
            }
        };
        match chosen {
            Some(s) => return Ok(Some(s)),
            None => agrep_core::errln!(
                "'{}' isn't a listed number or a unique reference",
                render::safe(answer)
            ),
        }
    }
}
/// Where the agent starts, as 0.4.9's `resolve_cwd(...) or HOME`
/// (py/hookless/native.py:975-980, 1012): the recorded directory, else a re-anchored one,
/// else home.
fn working_dir(recorded: &str, home: &Path) -> PathBuf {
    if recorded.is_empty() {
        return home.to_path_buf();
    }
    if Path::new(recorded).is_dir() {
        return PathBuf::from(recorded);
    }
    reanchor(recorded, home).unwrap_or_else(|| home.to_path_buf())
}
/// A recorded directory missing here is usually a tree moved from another machine: try each
/// path suffix, longest first, under home and home's immediate non-hidden subdirectories, and
/// take a match only when it is unique (py/hookless/native.py:345-370).
fn reanchor(recorded: &str, home: &Path) -> Option<PathBuf> {
    let parts: Vec<&str> = recorded
        .split(['/', '\\'])
        .filter(|p| !p.is_empty() && !is_drive(p))
        .collect();
    let mut roots = vec![home.to_path_buf()];
    if let Ok(entries) = std::fs::read_dir(home) {
        roots.extend(entries.flatten().filter_map(|e| {
            let visible = e.file_name().as_encoded_bytes().first() != Some(&b'.');
            (visible && e.file_type().is_ok_and(|t| t.is_dir())).then(|| e.path())
        }));
    }
    let shown = render::safe(recorded);
    for start in 0..parts.len() {
        let hits: BTreeSet<PathBuf> = roots
            .iter()
            .map(|root| {
                parts[start..]
                    .iter()
                    .fold(root.clone(), |p, part| p.join(part))
            })
            .filter(|candidate| candidate.is_dir())
            .collect();
        match hits.len() {
            0 => continue,
            1 => {
                let hit = hits.into_iter().next()?;
                agrep_core::errln!(
                    "↪ {shown} doesn't exist here - re-anchored to {}",
                    render::safe(&hit.to_string_lossy())
                );
                return Some(hit);
            }
            n => {
                agrep_core::errln!(
                    "↪ {shown} doesn't exist here and {n} local dirs match its suffix - not guessing"
                );
                return None;
            }
        }
    }
    None
}
fn is_drive(part: &str) -> bool {
    matches!(part.as_bytes(), [letter, b':'] if letter.is_ascii_alphabetic())
}
