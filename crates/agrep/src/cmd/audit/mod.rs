mod problems;
mod reparse;
mod tally;

use agrep_core::{data::DataRoot, diagnostics, history, ingest::registry::ADAPTERS, store};
use anyhow::Result;
use problems::{Problems, Seen};
use serde_json::json;
use tally::Tally;

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
    let (mut gaps, mut problems) = source_gaps(&live, &sources, &selected);
    let mut tally = Tally::default();
    // The searchable rows are read before the reparse streams the whole history
    // through the page cache; the few hidden rows need the reparse's chats.
    let counts = indexed_rows(c.as_ref(), full, &selected, &mut tally)?;
    if full {
        reparse::reparse(&root, &live, &selected, &mut problems, &mut tally)?;
        if let Some(c) = &c {
            for table in ["prose", "tools"] {
                tally.add_hidden(c, table, &selected)?;
            }
        }
        gaps.extend(tally.gaps());
    }
    let discovered = live.iter().filter(|s| selected(&s.agent)).count();
    // 0.4.9 certified nothing when no source file was found (py/audit.py:2383-2388).
    let unavailable = discovered == 0 && gaps.is_empty() && problems.is_empty();
    if unavailable {
        problems.other(json!({"path":null,"reason":"no source file census completed; audit cannot certify completeness"}));
    }
    let errors = problems.into_values();
    let (exit, status) = if !errors.is_empty() || (strict && !gaps.is_empty()) {
        (2, "error")
    } else if !gaps.is_empty() {
        (1, "warning")
    } else {
        (0, "clean")
    };
    let v = json!({"version":1,"full":full,"strict":strict,"agents":agents,"counts":counts,"discovered_sources":discovered,"expected_rows":if full{Some(tally.expected_rows())}else{None},"status":status,"gaps":gaps.len(),"gap_details":gaps,"problem_count":errors.len(),"problems":errors,"exit_code":exit,"index_path":root.db()});
    report(Report {
        value: &v,
        wanted: &wanted,
        gaps: &gaps,
        errors: &errors,
        unavailable,
        json_output,
    })?;
    Ok(exit)
}

fn source_gaps(
    live: &[store::Stamp],
    sources: &[serde_json::Value],
    selected: &impl Fn(&str) -> bool,
) -> (Vec<serde_json::Value>, Problems) {
    let mut gaps = vec![];
    let mut problems = Problems::default();
    for source in live.iter().filter(|s| selected(&s.agent)) {
        if let Some(error) = &source.error {
            problems.source(source.path.as_str(), error.as_str(), Seen::Census);
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
                    problems.source(source.path.as_str(), s["last_error"].clone(), Seen::Stored);
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
        problems.source(
            source["path"].clone(),
            source["last_error"].clone(),
            Seen::Stored,
        );
    }
    (gaps, problems)
}

/// Each table's searchable row count; a full audit tallies the rows themselves.
fn indexed_rows(
    c: Option<&rusqlite::Connection>,
    full: bool,
    selected: &impl Fn(&str) -> bool,
    tally: &mut Tally,
) -> Result<serde_json::Map<String, serde_json::Value>> {
    let mut counts = serde_json::Map::new();
    for table in ["prose", "tools"] {
        let total = match c {
            None => 0,
            Some(c) if full => tally.add_searchable(c, table, selected)?,
            Some(c) => c
                .prepare(&format!(
                    "SELECT agent,count(*) FROM {table} WHERE visible=1 GROUP BY agent"
                ))?
                .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, usize>(1)?)))?
                .collect::<rusqlite::Result<Vec<_>>>()?
                .into_iter()
                .filter(|(agent, _)| selected(agent))
                .map(|(_, n)| n)
                .sum(),
        };
        counts.insert(table.into(), json!(total));
    }
    Ok(counts)
}

struct Report<'a> {
    value: &'a serde_json::Value,
    wanted: &'a [&'a str],
    gaps: &'a [serde_json::Value],
    errors: &'a [serde_json::Value],
    unavailable: bool,
    json_output: bool,
}
fn report(report: Report<'_>) -> Result<()> {
    let Report {
        value: v,
        wanted,
        gaps,
        errors,
        unavailable,
        json_output,
    } = report;
    if json_output {
        agrep_core::outln!("{}", agrep_core::json_output::to_string(&v)?)?
    } else if unavailable {
        let scope = match wanted {
            [agent] => *agent,
            _ => "supported",
        };
        agrep_core::outln!("audit unavailable: no {scope} source files were found; no completeness claim was made.")?;
    } else {
        agrep_core::outln!(
            "audit: {} sources · {} prose rows · {} tool rows · {} gaps · {} errors",
            v["discovered_sources"],
            v["counts"]["prose"],
            v["counts"]["tools"],
            gaps.len(),
            errors.len()
        )?;
        for gap in gaps.iter().chain(errors.iter()) {
            agrep_core::outln!("{}", agrep_core::render::safe(&gap.to_string()))?;
        }
        if !gaps.is_empty() {
            agrep_core::outln!("refresh with agrep index --full")?;
        }
    }
    Ok(())
}
