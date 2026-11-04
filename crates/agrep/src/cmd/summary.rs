use crate::{date, ready, Summary};
use agrep_core::{briefing as b, history as h, query::Filters, render, store};
use anyhow::{bail, Context, Result};
use chrono::{Datelike, TimeZone};
use serde_json::{json, Value};
use std::collections::BTreeMap;
type FamilyMember<'a> = (&'a h::Session, Vec<h::Row>, Vec<Value>);
fn duration(s: &str) -> Result<i64> {
    for (unit, mul) in [
        ("ms", 1),
        ("s", 1000),
        ("m", 60000),
        ("h", 3600000),
        ("d", 86400000),
    ] {
        if let Some(n) = s.strip_suffix(unit) {
            let n: i64 = n.parse().context("invalid idle cap")?;
            if n <= 0 {
                bail!("idle cap must be positive")
            }
            return n.checked_mul(mul).context("idle cap is too large");
        }
    }
    bail!("invalid idle cap")
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
pub fn run(args: Summary, no_auto: bool, color: &str) -> Result<i32> {
    let (root, census) = ready(no_auto)?;
    let c = store::reader(&root)?;
    let all = h::sessions(&c)?;
    let until = args
        .until
        .as_deref()
        .map(date)
        .transpose()?
        .unwrap_or_else(|| chrono::Utc::now().timestamp_millis());
    if args.group.is_some() && args.mode.as_deref() != Some("time") {
        bail!("--group requires summary time")
    }
    if args.max.is_some() && args.mode.is_some() {
        bail!("--max is only used by the project briefing")
    }
    let group = args.group.as_deref().unwrap_or("day");
    let max = args.max.unwrap_or(5);
    let since_text = args.since.as_deref().unwrap_or("7d");
    let since = if let Some(since) = args.since.as_deref() {
        date(since)?
    } else {
        until - 7 * 86400000
    };
    if since >= until {
        bail!("--since must be before --until")
    };
    let cap = duration(&args.idle_cap)?;
    let filters = Filters {
        project: args.project.clone(),
        agent: args.agent.clone(),
        ..Default::default()
    };
    h::validate_filters(&filters)?;
    let caller = agrep_core::caller::Caller::resolve(&c, agrep_core::caller::Identity::detect())?;
    let request = agrep_core::caller::Request {
        include_self: args.include_self,
        no_self: args.no_self,
        waived: None,
    };
    let policy = caller.policy(request);
    let apply_self = !args.include_self;
    let mut excluded = 0;
    let mut unknown = 0;
    let mut side_count = 0;
    let mut families: BTreeMap<(String, String), Vec<FamilyMember<'_>>> = BTreeMap::new();
    let active: std::collections::HashSet<_> = all
        .iter()
        .filter(|s| s.first_ts < until && (s.last_ts == 0 || s.last_ts >= since))
        .map(|s| (s.agent.clone(), s.family.clone()))
        .collect();
    for s in &all {
        let owner = h::root(&all, s).unwrap_or(s);
        if !h::in_scope(owner, &filters) || !active.contains(&(s.agent.clone(), s.family.clone())) {
            continue;
        }
        let mut rows = h::rows(&c, s)?;
        let original = rows.len();
        if apply_self {
            rows.retain(|r| {
                !agrep_core::caller::excludes(
                    &caller,
                    &s.agent,
                    &s.id,
                    r.turn,
                    args.include_self,
                    args.no_self,
                )
            });
        }
        // 0.4.9 counted the root chats that lost turns; a side chat's rows fold into its root.
        if rows.len() != original && !s.side {
            excluded += 1;
        }
        if rows.is_empty() && original > 0 {
            continue;
        }
        unknown += rows
            .iter()
            .filter(|r| b::is_turn(&rows, r) && r.ts <= 0)
            .count();
        let events = b::events(&c, s)?;
        side_count += usize::from(s.side);
        families
            .entry((s.agent.clone(), s.family.clone()))
            .or_default()
            .push((s, rows, events));
    }
    let mut pending = vec![];
    let mut project_rows = BTreeMap::<String, Value>::new();
    let mut time = BTreeMap::<(String, String), (i64, usize)>::new();
    let mut dedup = 0;
    let mut total = 0;
    for members in families.values() {
        let root = members
            .iter()
            .find(|(s, _, _)| !s.side)
            .unwrap_or(&members[0]);
        let mut intervals = vec![];
        let mut raw = 0;
        for (_, rows, events) in members {
            let own = b::intervals(rows, events, cap, i64::MIN, i64::MAX);
            raw += b::span(&own);
            intervals.extend(own);
        }
        let merged = b::merge(intervals);
        dedup += raw - b::span(&merged);
        let merged = b::merge(
            merged
                .into_iter()
                .map(|(s, e)| (s.max(since), e.min(until)))
                .collect(),
        );
        let ms = b::span(&merged);
        total += ms;
        for (period, n) in days(&merged, group)? {
            let entry = time
                .entry((period, h::project_leaf(&root.0.project)))
                .or_default();
            entry.0 += n;
            entry.1 += 1;
        }
        if args.mode.as_deref() != Some("time") {
            let mut returned = std::collections::HashSet::new();
            returned.insert(root.0.id.clone());
            loop {
                let prior = returned.len();
                for (side, rows, events) in members.iter().filter(|(s, _, _)| s.side) {
                    if b::returned(rows, events)
                        || members.iter().any(|(holder, _, events)| {
                            returned.contains(&holder.id) && b::received(events, side, rows)
                        })
                    {
                        returned.insert(side.id.clone());
                    }
                }
                if returned.len() == prior {
                    break;
                }
            }

            let mut item = b::pending(&c, root.0, &root.1, &root.2)?;
            if item.is_none() {
                for (side, rows, events) in members
                    .iter()
                    .filter(|(s, _, _)| s.side && s.last_ts > root.0.last_ts)
                {
                    let delivered = returned.contains(&side.id)
                        || root.2.iter().any(|e| {
                            e["child_session"] == side.id
                                && (e["kind"] == "subagent_result"
                                    || !e["output"].as_str().unwrap_or("").is_empty())
                        });
                    if delivered {
                        continue;
                    }
                    if let Some(mut i) = b::pending(&c, side, rows, events)? {
                        if !matches!(
                            i["status"].as_str(),
                            Some("agent_work_incomplete" | "todo_open" | "unknown")
                        ) {
                            continue;
                        }
                        i["source"] = json!("side-chat");
                        i["evidence_session"] = json!(side.id);
                        i["session"] = json!(root.0.id);
                        i["session_handle"] = json!(h::target(&root.0.id));
                        i["project"] = json!(h::project_leaf(&root.0.project));
                        i["project_label"] = json!(root.0.project);
                        i["first_text"] = json!(root.0.title);
                        i["last_ts"] = json!(root.0.last_ts);
                        item = Some(i);
                        break;
                    }
                }
            }
            if let Some(item) = item {
                pending.push(item);
            }
        }
        let s = root.0;
        let project = h::project_leaf(&s.project);
        let entry=project_rows.entry(project.clone()).or_insert_with(||json!({"kind":"project","project":project,"project_labels":[s.project],"estimated_active_ms":0,"chats":0,"turns":0,"last_ts":0,"worked_on":[],"open":[]}));
        entry["estimated_active_ms"] =
            json!(entry["estimated_active_ms"].as_i64().unwrap_or(0) + ms);
        entry["chats"] = json!(entry["chats"].as_u64().unwrap_or(0) + 1);
        let turns = root
            .1
            .iter()
            .filter(|r| b::is_turn(&root.1, r) && r.ts >= since && r.ts < until)
            .count();
        entry["turns"] = json!(entry["turns"].as_u64().unwrap_or(0) + turns as u64);
        entry["last_ts"] = json!(entry["last_ts"].as_i64().unwrap_or(0).max(s.last_ts));
        entry["worked_on"].as_array_mut().unwrap().push(json!({"session":s.id,"session_handle":h::target(&s.id),"agent":s.agent,"project_label":s.project,"first_text":s.title,"last_ts":s.last_ts,"turns":turns,"estimated_active_ms":ms,"latest_handle":h::latest(&c,s)?.map(|r|h::handle(&s.id,&r)),"side_chats":members.len()-1}));
    }
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
        if let Some(p) = project_rows.get_mut(i["project"].as_str().unwrap_or("")) {
            p["open"].as_array_mut().unwrap().push(i.clone());
        }
    }
    let rows:Vec<Value>=match args.mode.as_deref() {
        Some("pending")=>pending,
        Some("time")=>time.into_iter().map(|((period,project),(ms,chats))| json!({"kind":"time","period":period,"group":group,"project":project,"estimated_active_ms":ms,"estimated_active":label(ms),"chats":chats})).collect(),
        _=> {
            let mut rows:Vec<_>=project_rows.into_values().collect();
            for entry in &mut rows {
                entry["estimated_active"]=json!(label(entry["estimated_active_ms"].as_i64().unwrap_or(0)));
                let worked=entry["worked_on"].as_array_mut().unwrap();
                worked.sort_by_key(|i|-i["last_ts"].as_i64().unwrap_or(0));
                if max>0 { worked.truncate(max); }
            }
            rows.sort_by_key(|r|(-r["estimated_active_ms"].as_i64().unwrap_or(0),-r["last_ts"].as_i64().unwrap_or(0)));
            rows
        }
    };
    let caveats=vec![format!("estimated active time is estimated from turn timestamps with a {} idle cap; it is not elapsed session span and not billable time",args.idle_cap),"side chats are folded into their family; overlapping minutes count once".into()];
    let pending_sources = crate::finish_census(&root, census)?;
    let checked = agrep_core::render::checked(no_auto);
    if args.json {
        let mut meta = json!({"kind":"agrep-meta","mode":args.mode,"window":{"since":since_text,"since_ts":since,"until":args.until,"until_ts":until},"timezone":chrono::Local::now().format("%Z %z").to_string(),"idle_cap":args.idle_cap,"idle_cap_ms":cap,"metric":"estimated active time","chats":families.len(),"side_chats":side_count,"unknown_timestamp_rows":unknown,"family_dedup_ms":dedup,"self_excluded":excluded,"self_exclusion":caller.metadata(request,None),"caveats":caveats,"freshness":render::freshness(checked,pending_sources),"total_estimated_active_ms":total});
        if rows.is_empty() {
            meta["hits"] = json!([])
        }
        println!("{}", agrep_core::json_output::to_string(&meta)?);
        for row in &rows {
            println!("{}", agrep_core::json_output::to_string(&row)?);
        }
    } else {
        for row in &rows {
            match row["kind"].as_str() {
                Some("time") => println!(
                    "{}  {}  {}  {} chats",
                    row["period"].as_str().unwrap_or(""),
                    super::paint(&render::safe(row["project"].as_str().unwrap_or("")), color),
                    row["estimated_active"].as_str().unwrap_or(""),
                    row["chats"]
                ),
                Some("pending") => print_pending(row, color),
                _ => {
                    println!(
                        "{} · estimated active time {} · {} chats",
                        super::paint(&render::safe(row["project"].as_str().unwrap_or("")), color),
                        row["estimated_active"].as_str().unwrap_or(""),
                        row["chats"]
                    );
                    for chat in row["worked_on"].as_array().unwrap() {
                        println!(
                            "  {} {} · {}",
                            chat["session_handle"].as_str().unwrap_or(""),
                            chat["agent"].as_str().unwrap_or(""),
                            render::safe(chat["first_text"].as_str().unwrap_or(""))
                        );
                    }
                    for item in row["open"].as_array().unwrap() {
                        print_pending(item, color)
                    }
                }
            }
        }
        for caveat in &caveats {
            eprintln!("{caveat}");
        }
        if unknown > 0 {
            eprintln!(
                "{unknown} turns without a usable timestamp excluded from time and turn counts"
            );
        }
        if let Some(notice) = policy.and_then(|p| p.notice(excluded, "chat")) {
            eprintln!("{notice}");
        }
    }
    Ok(if !rows.is_empty() {
        0
    } else if checked {
        1
    } else {
        2
    })
}
fn print_pending(row: &Value, color: &str) {
    println!(
        "  {} {} {} · {}",
        super::paint(
            &row["confidence"].as_str().unwrap_or("").to_uppercase(),
            color
        ),
        row["status"].as_str().unwrap_or(""),
        row["handle"].as_str().unwrap_or(""),
        render::safe(row["evidence"].as_str().unwrap_or(""))
    );
}
