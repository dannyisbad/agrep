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
pub fn dimensions(c: &Connection, f: &Filters) -> Result<Value> {
    let mut absent = Vec::new();
    for (flag, dimension, col, value, table) in [
        ("--agent", "agent", "agent", &f.agent, "sessions"),
        ("--project", "project", "project", &f.project, "sessions"),
        ("--model", "model", "model", &f.model, "prose"),
    ] {
        let Some(value) = value else { continue };
        let mut q = c.prepare(&format!(
            "SELECT DISTINCT {col} FROM {table} WHERE {col}!='' ORDER BY {col}"
        ))?;
        let values: Vec<String> = q
            .query_map([], |r| r.get(0))?
            .collect::<rusqlite::Result<_>>()?;
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
                || (dimension == "agent" && value.split(',').any(|a| a == v))
        });
        if !found {
            absent.push(json!({"flag":flag,"dimension":dimension,"value":value,"indexed_values":values.len(),"known":values.iter().take(6).collect::<Vec<_>>(),"known_complete":values.len()<=6}));
        }
    }
    Ok(json!({"empty_dimensions":absent,"checked":true}))
}
pub fn disclose(coverage: &Value) {
    if let Some(rows) = coverage["empty_dimensions"].as_array() {
        for r in rows {
            let values = r["known"]
                .as_array()
                .unwrap()
                .iter()
                .filter_map(Value::as_str)
                .collect::<Vec<_>>()
                .join(", ");
            eprintln!(
                "{} {} isn't in the index - indexed {}: {}",
                r["flag"].as_str().unwrap(),
                crate::surface::safe(r["value"].as_str().unwrap(), false),
                r["dimension"].as_str().unwrap(),
                crate::surface::safe(&values, false)
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
