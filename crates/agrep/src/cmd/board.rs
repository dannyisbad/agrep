use agrep_core::{
    live::{
        board::{self, Options},
        Service,
    },
    terminal::{Screen, Signals},
};
use anyhow::Result;
use std::{
    io::{self, IsTerminal, Write},
    time::Duration,
};
pub fn run(a: crate::Board) -> Result<i32> {
    match board(a) {
        Err(e) if agrep_core::terminal::output_closed(&e) => Ok(0),
        other => other,
    }
}
fn board(a: crate::Board) -> Result<i32> {
    let explicit_agent = a.agent.is_some();
    let explicit_expanded = a.expanded;
    let mut o = Options {
        agent: a.agent,
        session: a.session,
        project: a.project,
        state: a.state,
        side: if a.roots {
            "roots"
        } else if a.side {
            "show"
        } else if a.side_only {
            "only"
        } else {
            "collapse"
        }
        .into(),
        sort: a.sort,
        limit: a.max,
        json: a.json,
        wait: a.wait.unwrap_or(0.18),
        expanded: a.expanded,
    };
    o.validate()?;
    let once = a.once || a.json || !io::stdin().is_terminal() || !io::stdout().is_terminal();
    let signals = Signals::board()?;
    let watcher = Service::new();
    if once {
        let snap = watcher.snapshot(Duration::from_secs_f64(o.wait));
        let (value, rc) = board::machine(&snap, &o);
        if o.json {
            agrep_core::json_output::to_writer(io::stdout().lock(), &value)?;
            writeln!(io::stdout().lock())?;
        } else {
            let (w, h) = Screen::size();
            writeln!(
                io::stdout().lock(),
                "{}",
                board::frame(&snap, &o, usize::MAX, o.session.as_deref(), 0, w, h)
            )?;
            if snap.booting {
                eprintln!("initial live scan is still running; rerun with --wait 5");
            }
        }
        return Ok(rc);
    }
    let preferences = agrep_core::data::DataRoot::discover()
        .ok()
        .filter(|r| r.validate(false).is_ok());
    if let Some(config) = preferences.as_ref().and_then(|r| r.config().ok()) {
        if let Some(view) = config.board_view {
            if !explicit_agent {
                o.agent = view
                    .agent
                    .filter(|a| agrep_core::live::normalize_agent(a).is_ok());
            }
            if !explicit_expanded {
                o.expanded = view.expanded;
            }
        }
    }
    let mut screen = Screen::enter()?;
    let mut selected = 0;
    let mut selected_id = String::new();
    let mut focus = None;
    let mut initial_focus = o.session.is_some();
    let mut scroll = 0usize;
    let mut generation = u64::MAX;
    let mut snap = watcher.snapshot(Duration::ZERO);
    loop {
        if let Some(code) = screen.signal(&signals)? {
            return Ok(code);
        }
        let key = screen.key(Duration::from_millis(30))?;
        let changed = watcher.snapshot_if_changed(&mut generation, &mut snap);
        if !changed && key.is_none() {
            continue;
        }
        let rows = board::select(&snap, &o);
        if let Some(i) = rows.iter().position(|r| r.session.session == selected_id) {
            selected = i
        } else {
            selected = selected.min(rows.len().saturating_sub(1));
        }
        if initial_focus && !snap.booting {
            focus = rows.first().map(|r| r.session.session.clone());
            initial_focus = false;
        }
        if let Some(k) = key.as_deref() {
            match k {
                "q" | "Q" | "eof" => return Ok(0),
                "ctrl-c" => return Ok(130),
                "esc" | "b" | "B" => {
                    focus = None;
                    scroll = 0
                }
                "c" | "C" => o.expanded = !o.expanded,
                "a" | "A" => {
                    let mut agents: Vec<_> =
                        snap.sessions.iter().map(|s| s.agent.clone()).collect();
                    agents.sort();
                    agents.dedup();
                    let i = o
                        .agent
                        .as_ref()
                        .and_then(|a| agents.iter().position(|s| s == a));
                    o.agent = match i {
                        None => agents.first().cloned(),
                        Some(i) => agents.get(i + 1).cloned(),
                    };
                    focus = None;
                }
                "up" | "k" => {
                    if focus.is_some() {
                        scroll += 1
                    } else {
                        selected = selected.saturating_sub(1)
                    }
                }
                "down" | "j" => {
                    if focus.is_some() {
                        scroll = scroll.saturating_sub(1)
                    } else {
                        selected = (selected + 1).min(rows.len().saturating_sub(1))
                    }
                }
                "enter" => {
                    focus = rows.get(selected).map(|r| r.session.session.clone());
                    scroll = 0;
                }
                n if n.len() == 1 && ("1"..="9").contains(&n) => {
                    let i = n.parse::<usize>()? - 1;
                    if let Some(r) = rows.get(i) {
                        selected = i;
                        focus = Some(r.session.session.clone());
                        scroll = 0;
                    }
                }
                _ => {}
            }
        }
        if key
            .as_deref()
            .is_some_and(|k| matches!(k, "a" | "A" | "c" | "C"))
        {
            if let Some(root) = &preferences {
                let _ = board::remember(root, &o);
            }
        }
        selected_id = rows
            .get(selected)
            .map(|r| r.session.session.clone())
            .unwrap_or_default();
        let (w, h) = Screen::size();
        screen.draw(
            &board::frame(&snap, &o, selected, focus.as_deref(), scroll, w, h),
            &signals,
        )?;
        io::stdout().flush()?;
    }
}
