//! The common Board selector, family ordering, and output contracts.
use super::{cap, handle, normalize_agent, normalize_session, Session, Snapshot};
use anyhow::{ensure, Result};
use serde_json::{json, Value};
use std::collections::{BTreeMap, HashSet};
#[derive(Clone, Debug, Default, serde::Serialize, serde::Deserialize)]
pub struct Preference {
    pub agent: Option<String>,
    pub expanded: bool,
}
pub fn remember(root: &crate::data::DataRoot, o: &Options) -> Result<()> {
    let Some(_lock) = root.lock(false)? else {
        return Ok(());
    };
    let mut config = root.config()?;
    config.board_view = Some(Preference {
        agent: o.agent.clone(),
        expanded: o.expanded,
    });
    root.save_config(&config)
}
#[derive(Clone, Debug)]
pub struct Options {
    pub agent: Option<String>,
    pub session: Option<String>,
    pub project: Option<String>,
    pub state: String,
    pub side: String,
    pub sort: String,
    pub limit: Option<usize>,
    pub json: bool,
    pub wait: f64,
    pub expanded: bool,
}
impl Default for Options {
    fn default() -> Self {
        Self {
            agent: None,
            session: None,
            project: None,
            state: "all".into(),
            side: "collapse".into(),
            sort: "working".into(),
            limit: None,
            json: false,
            wait: 0.18,
            expanded: false,
        }
    }
}
impl Options {
    pub fn validate(&mut self) -> Result<()> {
        if let Some(a) = &self.agent {
            self.agent = if a.trim().eq_ignore_ascii_case("all") {
                None
            } else {
                Some(normalize_agent(a)?)
            }
        }
        if let Some(s) = &self.session {
            self.session = Some(normalize_session(s)?)
        }
        ensure!(
            self.project.as_ref().is_none_or(|p| !p.trim().is_empty()),
            "--project requires non-blank text"
        );
        ensure!(
            self.limit.is_none_or(|n| (1..=100).contains(&n)),
            "--max must be between 1 and 100"
        );
        ensure!(
            self.wait.is_finite() && (0.0..=10.0).contains(&self.wait),
            "--wait must be between 0 and 10 seconds"
        );
        ensure!(
            !self.json || !self.expanded,
            "--expanded cannot be combined with --json"
        );
        Ok(())
    }
}
#[derive(Clone, Debug)]
pub struct Row {
    pub session: Session,
    pub family: (String, String),
    pub family_updated: i64,
    pub family_working: bool,
    pub family_active: bool,
    pub side_count: usize,
    pub side_working: usize,
}
fn family(s: &Session, parents: &BTreeMap<(String, String), String>) -> (String, String) {
    let mut key = (s.agent.clone(), s.session.clone());
    let mut path: Vec<(String, String)> = vec![];
    loop {
        if let Some(i) = path.iter().position(|p| p == &key) {
            return path[i..].iter().min().unwrap().clone();
        }
        path.push(key.clone());
        if let Some(p) = parents.get(&key) {
            key = (key.0, p.clone())
        } else {
            return key;
        }
    }
}
pub fn select(snapshot: &Snapshot, o: &Options) -> Vec<Row> {
    let parents = snapshot
        .sessions
        .iter()
        .filter(|s| !s.parent.is_empty())
        .map(|s| ((s.agent.clone(), s.session.clone()), s.parent.clone()))
        .collect();
    let mut groups: BTreeMap<(String, String), Vec<Session>> = BTreeMap::new();
    for s in &snapshot.sessions {
        if !s.session.is_empty() {
            groups
                .entry(family(s, &parents))
                .or_default()
                .push(s.clone())
        }
    }
    let mut rows = vec![];
    for (key, family) in groups {
        let updated = family.iter().map(|s| s.last_ts).max().unwrap_or(0);
        let working = family.iter().any(|s| s.working);
        let active = family.iter().any(|s| s.active);
        let side_count = family.iter().filter(|s| s.sub).count();
        let side_working = family.iter().filter(|s| s.sub && s.working).count();
        for s in family
            .iter()
            .filter(|s| shown(s, &family, working, active, o))
        {
            rows.push(Row {
                session: s.clone(),
                family: key.clone(),
                family_updated: updated,
                family_working: working,
                family_active: active,
                side_count,
                side_working,
            });
        }
    }
    rows.sort_by(|a, b| {
        let family_cmp = if o.sort == "updated" {
            b.family_updated.cmp(&a.family_updated)
        } else {
            b.family_working
                .cmp(&a.family_working)
                .then_with(|| (b.family_updated / 30000).cmp(&(a.family_updated / 30000)))
        };
        family_cmp
            .then_with(|| a.family.cmp(&b.family))
            .then_with(|| a.session.sub.cmp(&b.session.sub))
            .then_with(|| b.session.working.cmp(&a.session.working))
            .then_with(|| b.session.last_ts.cmp(&a.session.last_ts))
            .then_with(|| a.session.session.cmp(&b.session.session))
    });
    rows
}
/// Whether the options select this member of a family. A root answers for its family's
/// project, working and active state; a side session for its own.
fn shown(s: &Session, family: &[Session], working: bool, active: bool, o: &Options) -> bool {
    if o.agent.as_ref().is_some_and(|a| a != &s.agent)
        || o.session.as_ref().is_some_and(|q| !s.session.contains(q))
    {
        return false;
    }
    if let Some(p) = &o.project {
        let p = p.to_lowercase();
        if if s.sub {
            !s.project.to_lowercase().contains(&p)
        } else {
            !family.iter().any(|s| s.project.to_lowercase().contains(&p))
        } {
            return false;
        }
    }
    let w = if s.sub { s.working } else { working };
    let a = if s.sub { s.active } else { active };
    if match o.state.as_str() {
        "working" => !w,
        "idle" => w,
        "active" => !a,
        _ => false,
    } {
        return false;
    }
    !match o.side.as_str() {
        "roots" => s.sub,
        "only" => !s.sub,
        "collapse" => s.sub && !s.working && o.session.is_none(),
        _ => false,
    }
}
pub fn title(s: &Session) -> String {
    if !s.title.is_empty() {
        s.title.clone()
    } else if !s.last_text.is_empty() {
        s.last_text.clone()
    } else {
        format!("{} in {} · {}", s.agent, s.project, cap(&s.session, 8))
    }
}
fn safe(s: &str, n: usize) -> String {
    cap(
        &crate::render::safe(&super::privacy::redact(s))
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" "),
        n,
    )
}
pub fn machine(snapshot: &Snapshot, o: &Options) -> (Value, i32) {
    let mut rows = select(snapshot, o);
    let errors: Vec<_> = snapshot
        .degraded_sources
        .iter()
        .filter(|e| o.agent.as_ref().is_none_or(|a| &e.agent == a) || e.agent == "process")
        .collect();
    let complete = !snapshot.booting && errors.is_empty();
    let exact = rows
        .iter()
        .filter(|r| o.session.as_ref() == Some(&r.session.session))
        .count();
    if exact > 0 {
        rows.retain(|r| o.session.as_ref() == Some(&r.session.session));
    }
    let matched = rows.len();
    let status = if o.session.is_none() {
        "unscoped"
    } else if !complete {
        "unresolved"
    } else {
        match matched {
            0 => "none",
            1 => "resolved",
            _ => "ambiguous",
        }
    };
    let rc = if !complete || status == "ambiguous" {
        2
    } else if status == "none" {
        1
    } else {
        0
    };
    let limit = o.limit.unwrap_or(if o.json { 20 } else { usize::MAX });
    let sessions:Vec<_>=rows.iter().take(limit).map(|r|{let s=&r.session;let h=handle(&s.session);json!({"active":s.active,"agent":s.agent,"family_active":r.family_active,"family_updated_ms":r.family_updated,"family_working":r.family_working,"handle":h,"parent":s.parent,"project":s.project,"session":s.session,"side":s.sub,"side_count":r.side_count,"side_working":r.side_working,"state":s.state,"title":safe(&title(s),200),"updated_ms":s.last_ts,"working":s.working})}).collect();
    let retry = retry_argv(o);
    let retry_line = retry
        .iter()
        .map(|s| format!("'{}'", s.replace('\'', "'\\''")))
        .collect::<Vec<_>>()
        .join(" ");
    (
        json!({"kind":"agrep-board-snapshot","scope":"live-window","sessions":sessions,"snapshot_complete":complete,"partial":!complete,"page_complete":matched<=limit,"window_seconds":90,"sort":o.sort,"history_command":"agrep chats --json","selector":{"exact_matches":exact,"matches":matched,"query":o.session.as_deref().unwrap_or(""),"status":status},"filters":{"agent":o.agent.as_deref().unwrap_or("all"),"project":o.project.as_deref().unwrap_or(""),"session":o.session.as_deref().unwrap_or(""),"side":o.side,"state":o.state},"feed":{"scope":"recent-tail"},"completeness":{"booting":snapshot.booting,"matched":matched,"matched_basis":if complete{"exact"}else{"floor"},"shown":matched.min(limit),"sessions_complete":complete,"truncated":matched>limit,"source_errors":errors.len(),"source_error_details":errors.into_iter().take(5).collect::<Vec<_>>(),"retry":if snapshot.booting{Some(retry_line)}else{None},"retry_argv":if snapshot.booting{Some(retry)}else{None},}}),
        rc,
    )
}
/// The command that repeats this selection once the watcher has finished booting.
fn retry_argv(o: &Options) -> Vec<String> {
    let mut retry = vec![
        "agrep".to_owned(),
        "board".into(),
        "--once".into(),
        "--wait".into(),
        "5".into(),
    ];
    if o.json {
        retry.push("--json".into())
    }
    for (flag, v) in [
        ("--agent", o.agent.as_deref()),
        ("--project", o.project.as_deref()),
        ("--session", o.session.as_deref()),
        ("--sort", Some(o.sort.as_str())),
        ("--state", Some(o.state.as_str())),
    ] {
        if let Some(v) = v {
            retry.extend([flag.into(), v.into()])
        }
    }
    match o.side.as_str() {
        "roots" => retry.push("--roots".into()),
        "show" => retry.push("--side".into()),
        "only" => retry.push("--side-only".into()),
        _ => {}
    }
    if let Some(n) = o.limit {
        retry.extend(["--max".into(), n.to_string()])
    }
    retry
}
pub fn frame(
    snapshot: &Snapshot,
    o: &Options,
    selected: usize,
    focus: Option<&str>,
    scroll: usize,
    width: usize,
    height: usize,
) -> String {
    let rows = select(snapshot, o);
    let (_, rc) = machine(snapshot, o);
    let mut lines = vec![
        format!(
            "agrep board · {} sessions · {}",
            rows.len(),
            if snapshot.booting {
                "scanning"
            } else if rc == 2 {
                "partial snapshot"
            } else {
                "live window"
            }
        ),
        "↑/↓ select · enter focus · esc back · a agent · c expanded · 1-9 jump · q quit".into(),
    ];
    for e in snapshot
        .degraded_sources
        .iter()
        .filter(|e| o.agent.as_ref().is_none_or(|a| a == &e.agent) || e.agent == "process")
    {
        lines.push(format!(
            "! unreadable {} source: {}: {}",
            e.agent, e.path, e.error
        ));
    }
    if let Some(s) = focus
        .and_then(|id| rows.iter().find(|r| r.session.session == id))
        .map(|r| &r.session)
    {
        focus_lines(s, scroll, height, &mut lines);
    } else {
        list_lines(&rows, o, selected, snapshot.booting, &mut lines);
    }
    lines
        .into_iter()
        .take(height)
        .map(|s| safe(&s, width))
        .collect::<Vec<_>>()
        .join("\n")
}
/// One session's recent events, as many as fit above the scroll position.
fn focus_lines(s: &Session, scroll: usize, height: usize, lines: &mut Vec<String>) {
    lines.push(format!(
        "{} {} · {} · {}",
        s.agent, s.session, s.project, s.state
    ));
    lines.push(title(s));
    let n = s.recent.len().saturating_sub(scroll);
    let start = n.saturating_sub(height.saturating_sub(lines.len() + 1));
    for e in &s.recent[start..n] {
        lines.push(format!(
            "{} {} {}",
            super::text(&e["type"]),
            super::text(&e["name"]),
            super::text(
                e.get("text")
                    .or(e.get("input"))
                    .or(e.get("output"))
                    .unwrap_or(&Value::Null)
            )
        ));
    }
}
fn list_lines(rows: &[Row], o: &Options, selected: usize, booting: bool, lines: &mut Vec<String>) {
    for (i, r) in rows.iter().take(o.limit.unwrap_or(usize::MAX)).enumerate() {
        let s = &r.session;
        lines.push(format!(
            "{} {} {} {:10} {:16} {:18} {}",
            if i == selected { ">" } else { " " },
            i + 1,
            if if s.sub { s.working } else { r.family_working } {
                "●"
            } else {
                "○"
            },
            s.agent,
            s.project,
            if !s.working && r.family_working {
                format!("{} side working", r.side_working)
            } else {
                s.state.clone()
            },
            title(s)
        ));
        if o.expanded {
            lines.push(format!("    {} {}", s.model, handle(&s.session)));
            for e in s.recent.iter().rev().take(3).rev() {
                lines.push(format!(
                    "    {} {} {}",
                    super::text(&e["type"]),
                    super::text(&e["name"]),
                    super::text(&e["text"])
                ));
            }
        }
    }
    if rows.is_empty() {
        lines.push(
            if booting {
                "scanning agent stores…"
            } else {
                "no sessions in the live window"
            }
            .into(),
        )
    }
}
pub fn public(snapshot: &Snapshot) -> Value {
    let o = Options {
        limit: Some(100),
        ..Options::default()
    };
    let rows = select(snapshot, &o);
    let identities: HashSet<_> = snapshot
        .sessions
        .iter()
        .map(|s| (&s.agent, &s.session))
        .collect();
    let sessions:Vec<_>=rows.iter().take(160).map(|r|{let s=&r.session;json!({"agent":s.agent,"session":s.session,"project":s.project,"title":cap(&super::privacy::redact(&title(s)),1000),"title_truncated":title(s).chars().count()>1000,"model":s.model,"state":s.state,"working":s.working,"active":s.active,"sub":s.sub,"parent":if s.parent.is_empty(){None}else{Some(&s.parent)},"last_ts":s.last_ts,"state_ts":s.state_ts,"queued":s.queued,"queued_text":super::privacy::redact(&s.queued_text),"orphan":s.sub && !identities.contains(&(&s.agent,&s.parent)),"family_last_ts":r.family_updated,"family_active":r.family_active,"family_working":r.family_working,"side_total":r.side_count,"side_working":r.side_working,"recent":s.recent.iter().rev().take(12).rev().map(|e|json!({"type":e["type"],"ts":e["ts"],"turn":e["tn"],"name":e["name"].as_str().unwrap_or(""),"text":super::privacy::redact(e["text"].as_str().or(e["output"].as_str()).unwrap_or("")),"input":super::privacy::redact(e["input"].as_str().unwrap_or("")),"ok":e["ok"],"duration_ms":e["dur"]})).collect::<Vec<_>>()})}).collect();
    json!({"status":if snapshot.booting{"scanning"}else if !snapshot.degraded_sources.is_empty(){"degraded"}else{"ready"},"updated_at":snapshot.now,"window_s":90,"total":snapshot.sessions.len(),"working":snapshot.sessions.iter().filter(|s|s.working).count(),"roots":snapshot.sessions.iter().filter(|s|!s.sub).count(),"side":snapshot.sessions.iter().filter(|s|s.sub).count(),"hidden_orphan_sides":snapshot.sessions.iter().filter(|s|s.sub&&!s.working&&!identities.contains(&(&s.agent,&s.parent))).count(),"truncated":rows.len()>160,"degraded_agents":snapshot.degraded_sources.iter().map(|e|&e.agent).collect::<HashSet<_>>(),"sessions":sessions})
}

#[cfg(test)]
mod tests {
    use super::*;
    fn s(id: &str, parent: &str, working: bool, ts: i64) -> Session {
        Session {
            agent: "pi".into(),
            session: id.into(),
            parent: parent.into(),
            sub: !parent.is_empty(),
            working,
            active: working,
            last_ts: ts,
            project: if id == "child" { "target" } else { "repo" }.into(),
            ..Session::default()
        }
    }
    #[test]
    fn family_filters_order_and_cycle() {
        let snap = Snapshot {
            booting: false,
            sessions: vec![
                s("root", "", false, 1),
                s("child", "root", true, 90),
                s("other", "", true, 60),
                s("done", "root", false, 100),
            ],
            ..Snapshot::default()
        };
        let o = Options {
            project: Some("target".into()),
            state: "working".into(),
            ..Options::default()
        };
        let rows = select(&snap, &o);
        assert_eq!(
            rows.iter()
                .map(|r| r.session.session.as_str())
                .collect::<Vec<_>>(),
            ["root", "child"]
        );
        assert_eq!(rows[0].side_count, 2);
        assert!(rows[0].family_working);
        let o = Options {
            side: "roots".into(),
            sort: "updated".into(),
            limit: Some(1),
            json: true,
            ..Options::default()
        };
        let (v, rc) = machine(&snap, &o);
        assert_eq!(rc, 0);
        assert_eq!(v["sessions"][0]["session"], "root");
        assert_eq!(v["completeness"]["matched"], 2);
        assert_eq!(v["page_complete"], false);
        let cycle = Snapshot {
            sessions: vec![s("aa", "bb", true, 1), s("bb", "aa", true, 2)],
            ..snap.clone()
        };
        assert_eq!(
            select(
                &cycle,
                &Options {
                    side: "show".into(),
                    ..Options::default()
                }
            )
            .len(),
            2
        );
    }
    #[test]
    fn exact_selector_ambiguity_and_source_errors() {
        let mut snap = Snapshot {
            booting: false,
            sessions: vec![s("root", "", false, 1), s("root-more", "", false, 2)],
            ..Snapshot::default()
        };
        let o = Options {
            session: Some("root".into()),
            json: true,
            ..Options::default()
        };
        let (v, rc) = machine(&snap, &o);
        assert_eq!(rc, 0);
        assert_eq!(v["selector"]["status"], "resolved");
        assert_eq!(v["sessions"].as_array().unwrap().len(), 1);
        let o = Options {
            session: Some("roo".into()),
            ..o
        };
        assert_eq!(machine(&snap, &o).1, 2);
        let o = Options {
            session: Some("absent".into()),
            ..o
        };
        assert_eq!(machine(&snap, &o).1, 1);
        snap.degraded_sources.push(super::super::SourceError {
            agent: "pi".into(),
            path: "broken".into(),
            error: "denied".into(),
        });
        let (v, rc) = machine(&snap, &o);
        assert_eq!(rc, 2);
        assert_eq!(v["completeness"]["matched_basis"], "floor");
        assert!(v["completeness"]["retry_argv"].is_null());
        snap.booting = true;
        assert!(machine(&snap, &o).0["completeness"]["retry_argv"].is_array());
        for wait in [-1., f64::INFINITY, f64::NAN, 11.] {
            assert!(Options {
                wait,
                ..Options::default()
            }
            .validate()
            .is_err())
        }
    }
}
#[cfg(test)]
mod preference_tests {
    use super::*;
    #[test]
    fn all_and_saved_preferences_preserve_other_settings() {
        let mut o = Options {
            agent: Some("ALL".into()),
            expanded: true,
            ..Options::default()
        };
        o.validate().unwrap();
        assert!(o.agent.is_none());
        let t = tempfile::tempdir().unwrap();
        let root = crate::data::DataRoot::open(t.path(), true).unwrap();
        let config = crate::data::Config {
            tools: false,
            ..Default::default()
        };
        root.save_config(&config).unwrap();
        remember(&root, &o).unwrap();
        let config = root.config().unwrap();
        assert!(!config.tools);
        assert!(config.board_view.unwrap().expanded);
    }
}
