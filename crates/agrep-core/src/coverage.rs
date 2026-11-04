//! Surface validation and evidence for requested dimensions absent from the index.
use crate::query::Filters;
use anyhow::{bail, Result};
use rusqlite::Connection;
use serde_json::{json, Value};
pub fn speakers(value: &str) -> Result<String> {
    let mut out = Vec::new();
    for word in value.split(',') {
        let word = word.trim();
        let word = if word == "you" { "user" } else { word };
        if ![
            "user",
            "agent",
            "subagent",
            "tool",
            "control",
            "synthetic",
            "recap",
            "harness",
        ]
        .contains(&word)
        {
            bail!("unknown speaker '{word}' - valid: user, subagent, agent, tool, control, synthetic, recap, harness")
        }
        if !out.contains(&word) {
            out.push(word)
        }
    }
    Ok(out.join(","))
}
pub fn validate(f: &mut Filters) -> Result<()> {
    if f.model_soft && f.model.is_none() {
        bail!("--soft requires --model")
    }
    for (name, value) in [
        ("--agent", &f.agent),
        ("--project", &f.project),
        ("--exclude-project", &f.exclude_project),
        ("--model", &f.model),
        ("--chat", &f.chat),
        ("--who", &f.who),
        ("--no-who", &f.no_who),
    ] {
        if value.as_ref().is_some_and(|v| v.trim().is_empty()) {
            bail!("{name} cannot be empty")
        }
    }
    if let Some(v) = &f.who {
        f.who = Some(speakers(v)?)
    }
    if let Some(v) = &f.no_who {
        f.no_who = Some(speakers(v)?)
    }
    if let Some(agents) = &f.agent {
        f.agent = Some(
            agents
                .split(',')
                .map(|a| match a.trim().to_ascii_lowercase().as_str() {
                    "omp" | "oh-my-pi" => "pi".to_owned(),
                    "agy" => "antigravity".to_owned(),
                    other => other.to_owned(),
                })
                .collect::<Vec<_>>()
                .join(","),
        );
    }
    if let Some(agents) = &f.agent {
        for a in agents.split(',') {
            if !crate::ingest::registry::ADAPTERS
                .iter()
                .any(|adapter| adapter.name() == a)
            {
                bail!("unknown agent '{a}'")
            }
        }
    }
    Ok(())
}
/// 0.4.9's filter dimensions with their plural labels (py/surface_policy.py:843-848).
const LABELS: [(&str, &str); 4] = [
    ("agent", "agents"),
    ("project", "projects"),
    ("model", "models"),
    ("who", "speakers"),
];
pub fn dimensions(c: &Connection, f: &Filters) -> Result<Value> {
    let mut absent = Vec::new();
    for (flag, dimension, col, value, table) in [
        ("--agent", "agent", "agent", &f.agent, "sessions"),
        ("--project", "project", "project", &f.project, "sessions"),
        ("--model", "model", "model", &f.model, "prose"),
        ("--who", "who", "who", &f.who, "prose"),
    ] {
        let Some(value) = value else { continue };
        let tools = || -> Result<bool> {
            Ok(c.query_row("SELECT EXISTS(SELECT 1 FROM tools)", [], |r| r.get(0))?)
        };
        // `who` has no index: a requested speaker that occurs ends its scan at the first
        // row, so only an empty dimension pays for listing every speaker.
        if dimension == "who" {
            let mut any = false;
            for speaker in value.split(',') {
                any |= if speaker == "tool" {
                    tools()?
                } else {
                    c.query_row(
                        "SELECT EXISTS(SELECT 1 FROM prose WHERE who=?1)",
                        [speaker],
                        |r| r.get(0),
                    )?
                };
                if any {
                    break;
                }
            }
            if any {
                continue;
            }
        }
        let mut q = c.prepare(&format!(
            "SELECT DISTINCT {col} FROM {table} WHERE {col}!='' ORDER BY {col}"
        ))?;
        let mut values: Vec<String> = q
            .query_map([], |r| r.get(0))?
            .collect::<rusqlite::Result<_>>()?;
        if dimension == "who" && tools()? {
            values.push("tool".into());
            values.sort();
        }
        let pattern = if dimension == "model" && f.model_soft {
            format!("*{}*", value.to_lowercase())
        } else {
            value.to_lowercase()
        };
        let matcher = globset::Glob::new(&pattern)?.compile_matcher();
        let found = values.iter().any(|v| {
            matcher.is_match(v.to_lowercase())
                || (dimension == "project"
                    && matcher.is_match(crate::render::project(v).to_lowercase()))
                || (matches!(dimension, "agent" | "who") && value.split(',').any(|a| a == v))
        });
        if !found {
            absent.push(json!({"flag":flag,"dimension":dimension,"value":value,"indexed_values":values.len(),"known":values.iter().take(6).collect::<Vec<_>>(),"known_complete":values.len()<=6}));
        }
    }
    Ok(json!({"empty_dimensions":absent,"checked":true}))
}
/// 0.4.9's `empty_dimension_line` (py/surface_policy.py:896-910).
pub fn disclose(coverage: &Value) {
    if let Some(rows) = coverage["empty_dimensions"].as_array() {
        for r in rows {
            let dimension = r["dimension"].as_str().unwrap_or_default();
            let label = LABELS
                .iter()
                .find(|(d, _)| *d == dimension)
                .map_or(dimension, |(_, label)| label);
            let known = r["known"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .collect::<Vec<_>>()
                .join(", ");
            let known = crate::surface::safe(&known, false);
            let held = if known.is_empty() {
                format!("the index holds no {label} at all")
            } else if r["known_complete"] == true {
                format!("indexed {label}: {known}")
            } else {
                format!("{} indexed {label}, closest: {known}", r["indexed_values"])
            };
            eprintln!(
                "{} {} isn't in the index - {held}",
                r["flag"].as_str().unwrap_or_default(),
                crate::surface::safe(r["value"].as_str().unwrap_or_default(), false),
            );
        }
    }
}
/// Small covering index for model validation; built by explicit indexing/setup.
pub fn initialize(root: &crate::data::DataRoot) -> Result<()> {
    let _lock = root.lock(true)?;
    crate::store::writer(root)?
        .execute_batch("CREATE INDEX IF NOT EXISTS prose_model ON prose(model)")?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn speaker_alias_is_canonical() {
        assert_eq!(speakers("you,agent").unwrap(), "user,agent");
        assert!(speakers("user,").is_err());
    }
}
