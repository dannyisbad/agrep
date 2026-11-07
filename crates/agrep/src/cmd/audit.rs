use agrep_core::{
    data::DataRoot, diagnostics, history, ingest::registry::ADAPTERS, intake_batch::IntakeBatch,
    store,
};
use anyhow::Result;
use serde_json::json;
use std::collections::{HashMap, HashSet};
type EvidenceRows = HashMap<(String, String, String, String), usize>;
pub fn run(agents: Vec<String>, json_output: bool, strict: bool, full: bool) -> Result<i32> {
    // 0.4.9's parser refusals (py/audit.py:2457-2469), before anything is read.
    let refuse = |message: String| super::usage::refusal("agrep audit", message);
    if agents.len() > 1 {
        return Err(refuse("--agent may be supplied only once".into()));
    }
    let names: Vec<_> = ADAPTERS.iter().map(|a| a.name()).collect();
    if agents.iter().any(|a| a.trim().is_empty()) {
        return Err(refuse(format!(
            "--agent needs a value ({}) - an empty value would silently match every agent",
            names.join(", ")
        )));
    }
    let wanted: Vec<_> = agents.iter().map(|a| history::normalize_agent(a)).collect();
    if wanted.iter().any(|agent| !names.contains(agent)) {
        return Err(refuse(format!(
            "--agent must name one of: {}",
            names.join(", ")
        )));
    }
    let root = DataRoot::discover()?;
    // Before the first index nothing is indexed, and 0.4.9 audited the sources anyway.
    let unborn = std::fs::read_dir(&root.0).map_or_else(
        |e| e.kind() == std::io::ErrorKind::NotFound,
        |mut entries| entries.next().is_none(),
    );
    if !unborn {
        root.validate(false)?;
    }
    let c = root
        .db()
        .exists()
        .then(|| store::reader(&root))
        .transpose()?;
    let selected = |agent: &str| wanted.is_empty() || wanted.contains(&agent);
    let sources = c
        .as_ref()
        .map(diagnostics::source_rows)
        .transpose()?
        .unwrap_or_default();
    let live = store::census()?;
    let mut gaps = vec![];
    let mut errors = vec![];
    for source in live.iter().filter(|s| selected(&s.agent)) {
        if let Some(error) = &source.error {
            errors.push(json!({"path":source.path,"reason":error}));
        }
        match sources.iter().find(|s| s["path"] == source.path) {
            None => gaps.push(json!({"path":source.path,"reason":"source not indexed"})),
            Some(s) => {
                if s["bytes"].as_u64() != Some(source.size)
                    || s["mtime_ns"].as_i64() != Some(source.mtime_ns)
                    || s["change_token"].as_str() != Some(source.token.to_string().as_str())
                {
                    gaps.push(json!({"path":source.path,"reason":"source changed since refresh"}));
                }
                if !s["last_error"].is_null() {
                    errors.push(json!({"path":source.path,"reason":s["last_error"]}));
                }
            }
        }
    }
    for source in sources
        .iter()
        .filter(|s| selected(s["agent"].as_str().unwrap_or("")))
    {
        if source["derived"] != true && !live.iter().any(|s| source["path"] == s.path) {
            gaps.push(
                json!({"path":source["path"],"reason":"indexed source no longer discovered"}),
            );
        }
    }
    for source in sources.iter().filter(|s| {
        s["derived"] == true
            && selected(s["agent"].as_str().unwrap_or(""))
            && !s["last_error"].is_null()
    }) {
        errors.push(json!({"path":source["path"],"reason":source["last_error"]}));
    }
    let mut counts = serde_json::Map::new();
    let mut indexed = EvidenceRows::new();
    for table in ["prose", "tools"] {
        let Some(c) = &c else {
            counts.insert(table.into(), json!(0));
            continue;
        };
        if !full {
            let total: usize = c
                .prepare(&format!(
                    "SELECT agent,count(*) FROM {table} WHERE visible=1 GROUP BY agent"
                ))?
                .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, usize>(1)?)))?
                .collect::<rusqlite::Result<Vec<_>>>()?
                .into_iter()
                .filter(|(agent, _)| selected(agent))
                .map(|(_, n)| n)
                .sum();
            counts.insert(table.into(), json!(total));
            continue;
        }
        let mut q = c.prepare(&format!(
            "SELECT agent,session,who,text FROM {table} WHERE visible=1"
        ))?;
        let mut rows = q.query([])?;
        let mut n = 0;
        while let Some(r) = rows.next()? {
            let agent: String = r.get(0)?;
            if !selected(&agent) {
                continue;
            }
            n += 1;
            if full {
                *indexed
                    .entry((
                        agent,
                        r.get(1)?,
                        r.get(2)?,
                        store::digest(&store::decode(table, r.get(3)?)?),
                    ))
                    .or_default() += 1;
            }
        }
        counts.insert(table.into(), json!(n));
    }
    let mut expected = EvidenceRows::new();
    if full {
        let policy = agrep_core::row_class::HarnessPolicy::parse(
            &std::fs::read_to_string(root.0.join("harness_prefixes.txt")).unwrap_or_default(),
        );
        for adapter in ADAPTERS.iter().filter(|a| selected(a.name())) {
            let paths: Vec<std::path::PathBuf> = live
                .iter()
                .filter(|s| s.agent == adapter.name())
                .map(|s| s.path.clone().into())
                .collect();
            let mut batch = IntakeBatch {
                changed: paths.iter().cloned().collect(),
                source_paths: paths,
                force: true,
                ..Default::default()
            };
            let (messages, events, outcome, issues) = adapter.collect_checked(&mut batch);
            let mut groups = vec![(messages, events)];
            for source in batch.batches {
                if let Some(error) = source.error {
                    errors.push(json!({"path":source.key,"reason":error}));
                }
                groups.push((source.messages, source.events));
            }
            for issue in issues.into_iter().chain(batch.issues) {
                errors.push(json!({"path":issue.path,"reason":issue.reason}));
            }
            if !matches!(
                outcome,
                agrep_core::intake_batch::ReadOutcome::Complete
                    | agrep_core::intake_batch::ReadOutcome::Undone
            ) && errors.is_empty()
            {
                errors.push(
                    json!({"agent":adapter.name(),"reason":format!("source read {outcome:?}")}),
                );
            }
            for (messages, events) in groups {
                let mut source_rows = EvidenceRows::new();
                for m in agrep_core::row_class::normalize_messages(messages, &policy) {
                    for (who, text) in [
                        (m.who.as_ref(), m.text.as_ref()),
                        ("agent", m.reply.as_ref()),
                    ] {
                        if !text.is_empty() {
                            *source_rows
                                .entry((
                                    m.agent.into(),
                                    m.session.to_string(),
                                    who.into(),
                                    store::digest(text),
                                ))
                                .or_default() += 1;
                        }
                    }
                }
                let mut seen_events = HashSet::new();
                for e in events {
                    if e.kind == "control"
                        || !seen_events.insert((e.agent, e.session.clone(), e.call_id.clone()))
                    {
                        continue;
                    }
                    let mut head = match e.kind {
                        "subagent_start" => format!("subagent {}", e.name.trim()),
                        "subagent_result" => format!("subagent result {}", e.name.trim()),
                        _ => e.name.trim().into(),
                    };
                    if e.ok == Some(false) {
                        head.push_str(" [failed]");
                    }
                    let mut text = if head.is_empty() {
                        e.input.trim().to_string()
                    } else if e.input.trim().is_empty() {
                        head
                    } else {
                        format!("{head}: {}", e.input.trim())
                    };
                    if !e.output.is_empty() {
                        text.push('\n');
                        text.push_str(&e.output);
                    }
                    *source_rows
                        .entry((
                            e.agent.into(),
                            e.session,
                            "tool".into(),
                            store::digest(text.trim()),
                        ))
                        .or_default() += 1;
                }
                // Native replay copies contribute one copy, while repeated turns within a
                // source remain separate evidence. Do not collapse them to a set of texts.
                for (key, count) in source_rows {
                    let n = expected.entry(key).or_default();
                    *n = (*n).max(count);
                }
            }
        }
        for ((agent, session, who, text), count) in &expected {
            let found = indexed
                .get(&(agent.clone(), session.clone(), who.clone(), text.clone()))
                .copied()
                .unwrap_or(0);
            if found < *count {
                gaps.push(json!({"agent":agent,"session":session,"who":who,"rows":count-found,"reason":"parsed row missing from index"}));
            }
        }
        for ((agent, session, who, text), count) in &indexed {
            let found = expected
                .get(&(agent.clone(), session.clone(), who.clone(), text.clone()))
                .copied()
                .unwrap_or(0);
            if found < *count {
                gaps.push(json!({"agent":agent,"session":session,"who":who,"rows":count-found,"reason":"indexed row absent from parsed sources"}));
            }
        }
    }
    let discovered = live.iter().filter(|s| selected(&s.agent)).count();
    // 0.4.9 certified nothing when no source file was found (py/audit.py:2383-2388).
    let unavailable = discovered == 0 && gaps.is_empty() && errors.is_empty();
    if unavailable {
        errors.push(json!({"path":null,"reason":"no source file census completed; audit cannot certify completeness"}));
    }
    let (exit, status) = if !errors.is_empty() || (strict && !gaps.is_empty()) {
        (2, "error")
    } else if !gaps.is_empty() {
        (1, "warning")
    } else {
        (0, "clean")
    };
    let v = json!({"version":1,"full":full,"strict":strict,"agents":agents,"counts":counts,"discovered_sources":discovered,"expected_rows":if full{Some(expected.values().sum::<usize>())}else{None},"status":status,"gaps":gaps.len(),"gap_details":gaps,"problem_count":errors.len(),"problems":errors,"exit_code":exit,"index_path":root.db()});
    if json_output {
        println!("{}", agrep_core::json_output::to_string(&v)?)
    } else if unavailable {
        let scope = match wanted.as_slice() {
            [agent] => *agent,
            _ => "supported",
        };
        println!(
            "audit unavailable: no {scope} source files were found; no completeness claim was made."
        );
    } else {
        println!(
            "audit: {} sources · {} prose rows · {} tool rows · {} gaps · {} errors",
            v["discovered_sources"],
            v["counts"]["prose"],
            v["counts"]["tools"],
            gaps.len(),
            errors.len()
        );
        for gap in gaps.iter().chain(errors.iter()) {
            println!("{}", agrep_core::render::safe(&gap.to_string()));
        }
        if !gaps.is_empty() {
            println!("refresh with agrep index --full");
        }
    }
    Ok(exit)
}
