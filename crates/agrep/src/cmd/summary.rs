use crate::{cmd::usage, ready, Summary};
use agrep_core::{
    briefing as b, handles::SessionIndex, history as h, query::Filters, render, store, surface,
};
use anyhow::{bail, Context, Result};
use chrono::{Datelike, TimeZone};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, HashMap, HashSet},
    io::IsTerminal,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc,
    },
};
const METRIC: &str = "estimated active time";
fn duration(s: &str) -> Option<i64> {
    for (unit, mul) in [
        ("ms", 1),
        ("s", 1000),
        ("m", 60000),
        ("h", 3600000),
        ("d", 86400000),
    ] {
        if let Some(n) = s.strip_suffix(unit) {
            return n.parse::<i64>().ok().filter(|n| *n > 0)?.checked_mul(mul);
        }
    }
    None
}
fn label(ms: i64) -> String {
    let minutes = (ms.max(0) as f64 / 60000.).round_ties_even() as i64;
    if minutes < 60 {
        format!("{minutes}m")
    } else if minutes % 60 == 0 {
        format!("{}h", minutes / 60)
    } else {
        format!("{}h {:02}m", minutes / 60, minutes % 60)
    }
}
fn days(intervals: &[(i64, i64)], group: &str) -> Result<BTreeMap<String, i64>> {
    let mut out = BTreeMap::new();
    for &(mut start, end) in intervals {
        while start < end {
            let local = chrono::Local
                .timestamp_millis_opt(start)
                .single()
                .context("timestamp out of range")?;
            let day = local.date_naive();
            let next = day
                .succ_opt()
                .context("date out of range")?
                .and_hms_opt(0, 0, 0)
                .unwrap();
            // Some zones skip midnight when daylight saving begins.
            let mut candidate = next;
            let boundary = loop {
                if let Some(value) = chrono::Local.from_local_datetime(&candidate).earliest() {
                    break value.timestamp_millis().min(end);
                }
                candidate = candidate
                    .checked_add_signed(chrono::Duration::minutes(1))
                    .context("date out of range")?;
            };
            if boundary <= start {
                bail!("local day did not advance")
            };
            let key = match group {
                "month" => format!("{}-{:02}", day.year(), day.month()),
                "week" => format!("{}-W{:02}", day.iso_week().year(), day.iso_week().week()),
                _ => day.to_string(),
            };
            *out.entry(key).or_default() += boundary - start;
            start = boundary;
        }
    }
    Ok(out)
}
/// 0.4.9's `count_noun`.
fn noun(n: usize, word: &str) -> String {
    format!(
        "{} {word}{}",
        surface::number(n),
        if n == 1 { "" } else { "s" }
    )
}
/// 0.4.9's open-item line (py/summary.py:2355-2369).
fn pending_line(item: &Value, indent: &str) -> String {
    let text = |key: &str| item[key].as_str().unwrap_or_default();
    let handle = [text("handle"), text("session_handle")]
        .into_iter()
        .find(|h| !h.is_empty())
        .map(str::to_owned)
        .unwrap_or_else(|| format!("session={}", text("session")));
    let list = |key: &str| -> Vec<&str> {
        item[key]
            .as_array()
            .map(|a| a.iter().filter_map(Value::as_str).collect())
            .unwrap_or_default()
    };
    let items = list("items");
    let detail = if !items.is_empty() {
        let mut detail = format!(
            "{} open: {}",
            items.len(),
            items[..items.len().min(3)].join("; ")
        );
        if items.len() > 3 {
            detail += &format!("; +{} more", items.len() - 3);
        }
        detail
    } else if matches!(text("status"), "waiting_on_user" | "unknown")
        && !text("evidence").is_empty()
    {
        format!(
            "\u{201c}{}\u{201d}",
            text("evidence").chars().take(120).collect::<String>()
        )
    } else {
        list("signals").join("; ")
    };
    let source = if text("source") == "side-chat" {
        "  [side chat]"
    } else if item["parent_indexed"] == false {
        "  [side chat; parent not indexed]"
    } else {
        ""
    };
    format!(
        "{indent}{:<6} {:<21} {}{source}  {}",
        text("confidence").to_uppercase(),
        text("status"),
        render::safe(&handle),
        render::safe(&detail)
    )
}
pub fn run(args: Summary, no_auto: bool, color: &str) -> Result<i32> {
    let window = summary_window(&args)?;
    let SummaryWindow {
        cap,
        until_arg,
        mode,
        group,
        since_text,
        since,
        ..
    } = window;
    let (root, census) = ready(no_auto)?;
    let identity = agrep_core::caller::Identity::detect();
    let filters = Filters {
        project: args.project.clone(),
        agent: args.agent.clone(),
        ..Default::default()
    };
    h::validate_filters(&filters)?;
    crate::finish_census(&root, census)?;
    let c = store::reader(&root)?;
    // Under AGREP_QUERY_TIMING the briefing reports the SQLite work it did, which its window
    // must bound however much history lies outside it.
    let ops = agrep_core::semantic_profile::timing_enabled().then(|| {
        let ops = Arc::new(AtomicU64::new(0));
        let tally = ops.clone();
        c.progress_handler(
            100,
            Some(move || {
                tally.fetch_add(100, Ordering::Relaxed);
                false
            }),
        );
        ops
    });
    let all = h::sessions(&c)?;
    let symbols = b::Symbols::load(&c)?;
    if args.no_self && !args.include_self && !args.json {
        identity.disclose(true, false);
    }
    let caller = agrep_core::caller::Caller::resolve(&c, identity)?;
    let request = agrep_core::caller::Request {
        include_self: args.include_self,
        no_self: args.no_self,
        waived: None,
    };
    let policy = caller.policy(request);
    let Families {
        families,
        excluded,
        unknown,
        side_count,
    } = load_families(&c, &all, &symbols, &args, &caller, &filters, &window)?;
    let index = SessionIndex::load(&c)?;
    let SummaryRows { rows, dedup, total } = summarize(&c, &families, &index, &window)?;
    let mut caveats=vec![format!("{METRIC} is estimated from turn timestamps with a {} idle cap; it is not elapsed session span and not billable time",args.idle_cap),"side chats are folded into their family; overlapping minutes count once".into()];
    let unknown_line = format!(
        "{} without a usable timestamp excluded from time and turn counts",
        noun(unknown, "turn")
    );
    if unknown > 0 {
        caveats.push(unknown_line.clone());
    }
    if let Some(ops) = ops {
        agrep_core::semantic_profile::event(json!({"summary": {
            "chats": families.len(),
            "sqlite_ops": ops.load(Ordering::Relaxed),
        }}));
    }
    let timezone = render::timezone_label();
    let checked = agrep_core::render::checked(no_auto);
    if args.json {
        // 0.5 never skips index rows or loses a chat's events, so its totals are exact.
        let mut meta = json!({"kind":"agrep-meta","mode":mode,"window":{"since":since_text,"since_ts":since,"until":args.until,"until_ts":until_arg},"timezone":timezone,"idle_cap":args.idle_cap,"idle_cap_ms":cap,"metric":METRIC,"chats":families.len(),"side_chats":side_count,"unknown_timestamp_rows":unknown,"family_dedup_ms":dedup,"self_excluded":excluded,"self_exclusion":caller.metadata(request,None),"totals_exact":true,"events_unavailable_chats":0,"caveats":caveats,"freshness":render::machine_freshness(no_auto)});
        if mode == "time" {
            meta["group"] = json!(group);
            meta["total_estimated_active_ms"] = json!(total);
        }
        if rows.is_empty() {
            meta["hits"] = json!([])
        }
        println!("{}", agrep_core::json_output::to_string(&meta)?);
        for row in &rows {
            println!("{}", agrep_core::json_output::to_string(&row)?);
        }
    } else {
        print_rows(&rows, mode, group, total, color);
        print_footer(
            &args,
            &rows,
            &window,
            Footer {
                family_count: families.len(),
                unknown,
                unknown_line: &unknown_line,
                dedup,
                timezone: &timezone,
            },
        )?;
        if let Some(notice) = policy.and_then(|p| p.notice(excluded, "chat")) {
            eprintln!("{notice}");
        }
        agrep_core::render::note_no_auto(no_auto);
    }
    Ok(if !rows.is_empty() {
        0
    } else if checked {
        1
    } else {
        2
    })
}

struct Families<'a> {
    families: Vec<(Vec<b::Member<'a>>, bool)>,
    excluded: usize,
    unknown: usize,
    side_count: usize,
}
fn load_families<'a>(
    c: &rusqlite::Connection,
    all: &'a [h::Session],
    symbols: &'a b::Symbols,
    args: &Summary,
    caller: &agrep_core::caller::Caller,
    filters: &Filters,
    window: &SummaryWindow<'_>,
) -> Result<Families<'a>> {
    let apply_self = !args.include_self;
    // 0.4.9 folded a side chat into its family root when that root is indexed; every other
    // chat heads a family of its own, a side chat naming the parent it lacks, and families
    // list in session-id order (py/summary.py:2099-2130).
    let indexed: HashSet<(&str, &str)> = all
        .iter()
        .map(|s| (s.agent.as_str(), s.id.as_str()))
        .collect();
    let mut grouped: BTreeMap<(&str, &str), Vec<&h::Session>> = BTreeMap::new();
    for s in all {
        let head = if s.side
            && s.family != s.id
            && indexed.contains(&(s.agent.as_str(), s.family.as_str()))
        {
            s.family.as_str()
        } else {
            s.id.as_str()
        };
        grouped.entry((head, s.agent.as_str())).or_default().push(s);
    }
    let overlaps = |s: &h::Session| {
        s.last_ts >= window.since && (window.until_arg.is_none() || s.first_ts < window.until)
    };
    let (mut excluded, mut unknown, mut side_count) = (0, 0, 0);
    let mut families: Vec<(Vec<b::Member<'_>>, bool)> = Vec::new();
    'families: for ((head, _), sessions) in grouped {
        let Some(&first) = sessions.iter().find(|s| s.id == head) else {
            continue;
        };
        if !h::in_scope(first, filters) || !sessions.iter().any(|s| overlaps(s)) {
            continue;
        }
        let mut members = Vec::new();
        let mut withheld = false;
        for s in std::iter::once(first).chain(sessions.iter().copied().filter(|s| s.id != head)) {
            let mut rows = h::rows(c, s)?;
            let original = rows.len();
            if apply_self {
                rows.retain(|r| {
                    !agrep_core::caller::excludes(
                        caller,
                        &s.agent,
                        &s.id,
                        r.turn,
                        args.include_self,
                        args.no_self,
                    )
                });
            }
            // 0.4.9 counted the root chats that lost turns, dropping one that lost them all
            // with its family, and dropped a side chat that lost them all.
            if rows.len() != original {
                if s.id == head {
                    excluded += 1;
                    withheld = true;
                }
                if rows.is_empty() {
                    if s.id == head {
                        continue 'families;
                    }
                    continue;
                }
            }
            unknown += rows
                .iter()
                .filter(|r| b::is_turn(&rows, r) && r.ts <= 0)
                .count();
            let events = b::events(c, symbols, s)?;
            members.push((s, rows, events));
        }
        side_count += members.len() - 1;
        families.push((members, withheld));
    }
    Ok(Families {
        families,
        excluded,
        unknown,
        side_count,
    })
}

struct SummaryWindow<'a> {
    cap: i64,
    until_arg: Option<i64>,
    until: i64,
    mode: &'a str,
    group: &'a str,
    max: usize,
    since_text: &'a str,
    since: i64,
}
fn summary_window(args: &Summary) -> Result<SummaryWindow<'_>> {
    // 0.4.9's usage checks, in its order and all before the index
    // (py/summary.py:2036-2059).
    if args.group.is_some() && args.mode.as_deref() != Some("time") {
        return Err(usage::refusal(
            "agrep summary",
            "--group applies to `agrep summary time`",
        ));
    }
    let cap = duration(&args.idle_cap).ok_or_else(|| {
        usage::refusal(
            "agrep summary",
            "--idle-cap must be a positive duration like 5m, 20m or 1h",
        )
    })?;
    let now = chrono::Utc::now().timestamp_millis();
    let until_arg = args
        .until
        .as_deref()
        .map(|u| usage::date(u, now))
        .transpose()?;
    let until = until_arg.unwrap_or(now);
    let mode = args.mode.as_deref().unwrap_or("briefing");
    let group = args.group.as_deref().unwrap_or("day");
    let max = args.max.unwrap_or(5);
    let since_text = args.since.as_deref().unwrap_or("7d");
    let since = if let Some(since) = args.since.as_deref() {
        usage::date(since, now)?
    } else {
        until - 7 * 86400000
    };
    if let (Some(since_arg), Some(until_arg)) = (args.since.as_deref(), args.until.as_deref()) {
        usage::window(since_arg, since, until_arg, until)?;
    }
    Ok(SummaryWindow {
        cap,
        until_arg,
        until,
        mode,
        group,
        max,
        since_text,
        since,
    })
}

fn family_pending(c: &rusqlite::Connection, members: &[b::Member<'_>]) -> Result<Option<Value>> {
    let root = &members[0];
    let s = root.0;
    let mut item = b::pending(c, s, &root.1, &root.2)?;
    if item.is_none() {
        let latest: Vec<_> = members
            .iter()
            .map(|(_, rows, events)| b::latest_activity(rows, events))
            .collect();
        let mut later: Vec<_> = members
            .iter()
            .enumerate()
            .skip(1)
            .filter(|(at, _)| latest[*at] > latest[0])
            .collect();
        later.sort_by(|(a, (sa, _, _)), (b, (sb, _, _))| {
            latest[*b].cmp(&latest[*a]).then(sa.id.cmp(&sb.id))
        });
        let reached = if later.is_empty() {
            vec![]
        } else {
            b::handed_back(c, members)?
        };
        for (at, (side, rows, events)) in later {
            if reached[at] || b::delivered(c, &root.2, side)? {
                continue;
            }
            if let Some(mut i) = b::side_pending(c, side, rows, events)? {
                i["evidence_session"] = json!(side.id);
                i["session"] = json!(s.id);
                i["project"] = json!(h::project_leaf(&s.project));
                i["project_label"] = json!(s.project);
                i["first_text"] = json!(s.title.trim());
                i["last_ts"] = json!(s.last_ts);
                item = Some(i);
                break;
            }
        }
    }
    Ok(item)
}

struct SummaryRows {
    rows: Vec<Value>,
    dedup: i64,
    total: i64,
}
fn summarize(
    c: &rusqlite::Connection,
    families: &[(Vec<b::Member<'_>>, bool)],
    index: &SessionIndex,
    window: &SummaryWindow<'_>,
) -> Result<SummaryRows> {
    let session_handle = |session: &str| format!("@{}", index.target(session));
    let short = |handle: &str, session: &str| {
        handle.replacen(&h::target(session), &session_handle(session), 1)
    };
    let mut pending = vec![];
    let mut projects: Vec<Value> = Vec::new();
    let mut project_at = HashMap::<String, usize>::new();
    let mut time = BTreeMap::<(String, String), (i64, usize)>::new();
    let mut dedup = 0;
    let mut total = 0;
    for (members, withheld) in families {
        let root = &members[0];
        let s = root.0;
        let mut intervals = vec![];
        let mut raw = 0;
        for (_, rows, events) in members {
            let own = b::intervals(rows, events, window.cap, i64::MIN, i64::MAX);
            raw += b::span(&own);
            intervals.extend(own);
        }
        let merged = b::merge(intervals);
        dedup += raw - b::span(&merged);
        let merged = b::merge(
            merged
                .into_iter()
                .map(|(s, e)| (s.max(window.since), e.min(window.until)))
                .collect(),
        );
        let ms = b::span(&merged);
        total += ms;
        for (period, n) in days(&merged, window.group)? {
            let entry = time
                .entry((period, h::project_leaf(&s.project)))
                .or_default();
            entry.0 += n;
            entry.1 += 1;
        }
        let parent = (s.side && s.family != s.id).then(|| {
            if s.parent.is_empty() {
                s.family.clone()
            } else {
                s.parent.clone()
            }
        });
        if window.mode != "time" {
            let item = family_pending(c, members)?;
            if let Some(mut item) = item {
                item["session_handle"] = json!(session_handle(&s.id));
                let evidence = item["evidence_session"]
                    .as_str()
                    .unwrap_or(&s.id)
                    .to_owned();
                let handle = short(item["handle"].as_str().unwrap_or_default(), &evidence);
                item["handle"] = json!(handle);
                if let Some(parent) = &parent {
                    item["parent"] = json!(parent);
                    item["parent_indexed"] = json!(false);
                }
                if item["caveats"].as_array().is_some_and(Vec::is_empty) {
                    if let Some(fields) = item.as_object_mut() {
                        fields.remove("caveats");
                    }
                }
                pending.push(item);
            }
        }
        let project = h::project_leaf(&s.project);
        let at = *project_at.entry(project.clone()).or_insert_with(|| {
            projects.push(json!({"kind":"project","project":project,"project_labels":[],"estimated_active_ms":0,"chats":0,"turns":0,"last_ts":0,"worked_on":[],"open":[]}));
            projects.len() - 1
        });
        let entry = &mut projects[at];
        let labels = entry["project_labels"].as_array_mut().unwrap();
        if !labels.contains(&json!(s.project)) {
            labels.push(json!(s.project));
        }
        entry["estimated_active_ms"] =
            json!(entry["estimated_active_ms"].as_i64().unwrap_or(0) + ms);
        entry["chats"] = json!(entry["chats"].as_u64().unwrap_or(0) + 1);
        let turns = root
            .1
            .iter()
            .filter(|r| b::is_turn(&root.1, r) && r.ts >= window.since && r.ts < window.until)
            .count();
        entry["turns"] = json!(entry["turns"].as_u64().unwrap_or(0) + turns as u64);
        entry["last_ts"] = json!(entry["last_ts"].as_i64().unwrap_or(0).max(s.last_ts));
        // 0.4.9 bound a chat's latest handle to its last prompt (py/summary.py:2008-2018).
        let last = root
            .1
            .iter()
            .filter(|r| b::is_turn(&root.1, r))
            .max_by_key(|r| (r.turn, r.ts));
        let mut chat = json!({"session":s.id,"session_handle":session_handle(&s.id),"agent":s.agent,"project_label":s.project,"first_text":s.title.trim(),"last_ts":s.last_ts,"turns":turns,"estimated_active_ms":ms,"latest_handle":last.map(|r| index.mint(&s.id, r.turn, &r.text)),"side_chats":members.len()-1});
        if *withheld {
            chat["self"] = json!(true);
        }
        if let Some(parent) = &parent {
            chat["parent"] = json!(parent);
            chat["parent_indexed"] = json!(false);
        }
        entry["worked_on"].as_array_mut().unwrap().push(chat);
    }
    let rows = finish_rows(pending, project_at, projects, time, window);
    Ok(SummaryRows { rows, dedup, total })
}

fn print_rows(rows: &[Value], mode: &str, group: &str, total: i64, color: &str) {
    // 0.4.9's rendering (py/summary.py:2344-2404).
    let on = color == "always" || (color == "auto" && std::io::stdout().is_terminal());
    let head = |t: &str| {
        if on {
            format!("\x1b[1;36m{t}\x1b[0m")
        } else {
            t.to_owned()
        }
    };
    let dim = |t: &str| {
        if on {
            format!("\x1b[2m{t}\x1b[0m")
        } else {
            t.to_owned()
        }
    };
    let text = |v: &Value| v.as_str().unwrap_or_default().to_owned();
    match mode {
        "time" if !rows.is_empty() => {
            let width = |key: &str, floor: usize| {
                rows.iter()
                    .map(|r| text(&r[key]).chars().count())
                    .max()
                    .unwrap_or(0)
                    .max(floor)
            };
            let (pw, lw) = (width("period", group.len()), width("project", 7));
            println!(
                "{}",
                head(&format!(
                    "{group:<pw$}  {:<lw$}  {:>7}  chats",
                    "project", "time"
                ))
            );
            for row in rows {
                println!(
                    "{:<pw$}  {:<lw$}  {:>7}  {}",
                    text(&row["period"]),
                    render::safe(&text(&row["project"])),
                    text(&row["estimated_active"]),
                    row["chats"]
                );
            }
            println!(
                "{}",
                dim(&format!(
                    "{:<pw$}  {:<lw$}  {:>7}",
                    "total",
                    "",
                    label(total)
                ))
            );
        }
        "pending" => {
            for item in rows {
                println!(
                    "{}  {}",
                    pending_line(item, ""),
                    dim(&render::safe(&text(&item["project"])))
                );
            }
        }
        "time" => {}
        _ => {
            for entry in rows {
                println!(
                    "{}{}",
                    head(&render::safe(&text(&entry["project"]))),
                    dim(&format!(
                        "  ·  {METRIC} {}  ·  {}  ·  {}  ·  last {}",
                        text(&entry["estimated_active"]),
                        noun(entry["chats"].as_u64().unwrap_or(0) as usize, "chat"),
                        noun(entry["turns"].as_u64().unwrap_or(0) as usize, "turn"),
                        text(&entry["last_age"])
                    ))
                );
                println!("  worked on:");
                for chat in entry["worked_on"].as_array().into_iter().flatten() {
                    print_chat(chat, &dim);
                }
                let open = entry["open"].as_array().map(Vec::as_slice).unwrap_or(&[]);
                if !open.is_empty() {
                    println!("  open:");
                    for item in open {
                        println!("{}", pending_line(item, "    "));
                    }
                }
            }
        }
    }
}

fn print_chat(chat: &Value, dim: &impl Fn(&str) -> String) {
    let text = |v: &Value| v.as_str().unwrap_or_default().to_owned();
    let mut marks = String::new();
    if chat["self"] == true {
        marks += " ~self";
    }
    let sides = chat["side_chats"].as_u64().unwrap_or(0);
    if sides > 0 {
        marks += &format!(" (+{sides} side)");
    }
    if chat["parent_indexed"] == false {
        marks += " [side chat; parent not indexed]";
    }
    let tail = chat["latest_handle"]
        .as_str()
        .map(|handle| {
            format!(
                " · {}",
                surface::shell(&["agrep".to_owned(), "around".to_owned(), handle.to_owned()])
            )
        })
        .unwrap_or_default();
    let first: String = render::safe(&text(&chat["first_text"]))
        .chars()
        .take(96)
        .collect();
    println!(
        "    {} {} {} {}t{marks}  {}",
        render::safe(&text(&chat["session_handle"])),
        render::safe(&text(&chat["agent"])),
        render::age(chat["last_ts"].as_i64().unwrap_or(0)),
        chat["turns"],
        dim(&(first + &tail))
    );
}

struct Footer<'a> {
    family_count: usize,
    unknown: usize,
    unknown_line: &'a str,
    dedup: i64,
    timezone: &'a str,
}
fn print_footer(
    args: &Summary,
    rows: &[Value],
    window: &SummaryWindow<'_>,
    footer: Footer<'_>,
) -> Result<()> {
    let Footer {
        family_count,
        unknown,
        unknown_line,
        dedup,
        timezone,
    } = footer;
    let since_text = window.since_text;
    // 0.4.9's footer (py/summary.py:2309-2337).
    let relative = regex::Regex::new(r"(?i)^\d+\s*[a-z]+$")?.is_match(window.since_text.trim());
    let window_text = match (&args.until, &args.since) {
        (Some(until), None) => format!("in the 7d before {until}"),
        (Some(until), Some(_)) => format!("from {since_text} to {until}"),
        _ if relative => format!("in the last {since_text}"),
        _ => format!("since {since_text}"),
    };
    match window.mode {
            "time" => eprintln!(
                "{METRIC} for {} {window_text}, idle cap {}, local time {timezone} · not elapsed span, not billable",
                noun(family_count, "chat"),
                args.idle_cap
            ),
            "pending" => eprintln!(
                "{} across {} {window_text}, most confident first",
                noun(rows.len(), "open item"),
                noun(family_count, "chat")
            ),
            _ => eprintln!(
                "{}, {} {window_text}, most active first · {METRIC} with a {} idle cap, side chats folded into their family",
                noun(rows.len(), "project"),
                noun(family_count, "chat"),
                args.idle_cap
            ),
        }
    if unknown > 0 {
        eprintln!("{unknown_line}");
    }
    if dedup > 0 {
        eprintln!(
            "overlap between chats and their side chats counted once ({} de-duplicated)",
            label(dedup)
        );
    }
    Ok(())
}

fn finish_rows(
    mut pending: Vec<Value>,
    project_at: HashMap<String, usize>,
    mut projects: Vec<Value>,
    time: BTreeMap<(String, String), (i64, usize)>,
    window: &SummaryWindow<'_>,
) -> Vec<Value> {
    pending.sort_by_key(|i| {
        (
            match i["confidence"].as_str() {
                Some("high") => 0,
                Some("medium") => 1,
                _ => 2,
            },
            -i["last_ts"].as_i64().unwrap_or(0),
        )
    });
    for i in &pending {
        if let Some(&at) = project_at.get(i["project"].as_str().unwrap_or("")) {
            projects[at]["open"].as_array_mut().unwrap().push(i.clone());
        }
    }
    let rows: Vec<Value> = match window.mode {
        "pending" => pending,
        "time" => {
            let mut rows: Vec<_> = time.into_iter().collect();
            rows.sort_by(|((p1, n1), (a, _)), ((p2, n2), (b, _))| {
                p1.cmp(p2).then(b.cmp(a)).then(n1.cmp(n2))
            });
            rows.into_iter().map(|((period,project),(ms,chats))| json!({"kind":"time","period":period,"group":window.group,"project":project,"estimated_active_ms":ms,"estimated_active":label(ms),"chats":chats})).collect()
        }
        _ => {
            for entry in &mut projects {
                entry["estimated_active"] =
                    json!(label(entry["estimated_active_ms"].as_i64().unwrap_or(0)));
                entry["last_age"] = json!(render::age(entry["last_ts"].as_i64().unwrap_or(0)));
                let worked = entry["worked_on"].as_array_mut().unwrap();
                worked.sort_by_key(|i| -i["last_ts"].as_i64().unwrap_or(0));
                if window.max > 0 {
                    worked.truncate(window.max);
                }
            }
            projects.sort_by(|a, b| {
                let key = |r: &Value| {
                    (
                        -r["estimated_active_ms"].as_i64().unwrap_or(0),
                        -r["last_ts"].as_i64().unwrap_or(0),
                    )
                };
                key(a)
                    .cmp(&key(b))
                    .then(a["project"].as_str().cmp(&b["project"].as_str()))
            });
            projects
        }
    };
    rows
}
