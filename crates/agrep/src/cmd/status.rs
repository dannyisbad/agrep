use agrep_core::{data::DataRoot, diagnostics, render, store};
use anyhow::Result;
pub fn run(json: bool, no_auto: bool) -> Result<i32> {
    let root = DataRoot::discover()?;
    if root.path().join("FORMAT").exists()
        || root.path().is_dir() && std::fs::read_dir(root.path())?.next().is_some()
    {
        root.validate(false)?;
    }
    let census = if root.db().exists() {
        crate::ready(no_auto)?.1
    } else {
        None
    };
    crate::finish_census(&root, census)?;
    let mut s = diagnostics::status(&root)?;
    diagnostics::verify_model(&root, &mut s);
    if root.db().exists() {
        s["freshness"] = render::freshness(render::checked(no_auto));
    }
    let failing = agrep_core::archive::failing_captures(&root);
    if failing > 0 {
        s["archive"]["failing_captures"] = failing.into();
    }
    if json {
        agrep_core::outln!("{}", agrep_core::json_output::to_string(&s)?)?
    } else {
        agrep_core::outln!(
            "agrep {} · {}",
            s["version"].as_str().unwrap_or(""),
            root.0.display()
        )?;
        if s["index"]["state"] == "ready" {
            let n = &s["index"]["counts"];
            // 0.4.9's corpus row: the age of the last refresh, "just now" under a minute.
            let age = match s["last_indexed_age_s"].as_i64() {
                None => String::new(),
                Some(age) if age < 60 => " · updated just now".into(),
                Some(_) => format!(
                    " · updated {} ago",
                    render::age(s["index"]["last_refresh"].as_i64().unwrap_or(0))
                ),
            };
            agrep_core::outln!(
                "{} sessions · {} messages · {} tool rows{age}",
                count(&n["sessions"]),
                count(&s["messages"]),
                count(&n["tools"])
            )?;
            agrep_core::outln!(
                "footprint: {:.1} MiB across {} files",
                s["disk_bytes"].as_u64().unwrap_or(0) as f64 / (1024. * 1024.),
                count(&s["disk_files"])
            )?;
        } else if s["index"]["state"] == "first build in progress" {
            agrep_core::outln!("first build in progress")?;
        } else {
            agrep_core::outln!(
                "index {} · run agrep index",
                s["index"]["state"].as_str().unwrap_or("unreadable")
            )?;
        }
        agrep_core::outln!(
            "data format: {}",
            s["format"].as_str().unwrap_or("unreadable")
        )?;
        for line in meaning(&s["semantic"]) {
            agrep_core::outln!("{line}")?;
        }
        if s["index"]["state"] == "ready" {
            for source in diagnostics::failed_sources(&store::reader(&root)?)? {
                agrep_core::outln!(
                    "source failed: {}: {}",
                    render::safe(source["path"].as_str().unwrap_or("")),
                    render::safe(source["last_error"].as_str().unwrap_or(""))
                )?;
            }
        }
        for path in s["permissions"]["exposed"].as_array().into_iter().flatten() {
            agrep_core::outln!(
                "directory permissions expose local data: {}",
                render::safe(path.as_str().unwrap_or(""))
            )?;
        }
        let integrations = s["integrations"].as_array().map_or(&[][..], Vec::as_slice);
        if integrations.is_empty() {
            agrep_core::outln!("instructions: not installed - `agrep setup` teaches your agents to search this history")?;
        }
        for item in integrations {
            agrep_core::outln!(
                "integration {}: {}",
                if item["present"] == true {
                    "installed"
                } else {
                    "needs repair"
                },
                item["path"].as_str().unwrap_or("")
            )?;
        }
        let failing = if failing > 0 {
            format!("; the last {failing} capture pass(es) failed - `agrep doctor` explains")
        } else {
            String::new()
        };
        agrep_core::outln!(
            "archive: {}{failing}",
            if s["archive"]["enabled"] == true {
                "enabled"
            } else {
                "off"
            }
        )?;
    }
    Ok(
        if s["format"] == "unsupported"
            || s["format"] == "unreadable"
            || s["index"]["state"] == "unreadable"
        {
            2
        } else {
            0
        },
    )
}

fn count(v: &serde_json::Value) -> String {
    agrep_core::surface::number(v.as_u64().unwrap_or(0) as usize)
}

/// The meaning model's and index's lines.
fn meaning(semantic: &serde_json::Value) -> Vec<String> {
    if let Some(error) = semantic["integrity_error"].as_str() {
        return vec![format!(
            "semantic model failed verification: {} - `agrep setup` reinstalls it",
            render::safe(error)
        )];
    }
    if semantic["model_present"] != true {
        return vec!["semantic model not installed: run agrep setup".into()];
    }
    let mut lines = vec![format!(
        "meaning index: {} of {} rows · {} remaining",
        count(&semantic["indexed_rows"]),
        count(&semantic["total_rows"]),
        count(&semantic["backlog"])
    )];
    if semantic["state"] == "building" {
        lines.push(format!(
            "build runs in the background · {}",
            agrep_core::semantic::estimate(semantic["progress"]["estimated_seconds"].as_u64())
        ));
    }
    lines
}

#[cfg(test)]
mod tests {
    use super::meaning;
    use serde_json::json;

    // QA3 item 5: right after a first index the time left came from a guessed rate, and the
    // row counts had no thousands separators.
    #[test]
    fn meaning_rows_use_separators_and_estimate_only_from_a_measured_rate() {
        let building = |estimated: serde_json::Value| {
            json!({"model_present":true,"integrity_error":null,"indexed_rows":1200,
                "total_rows":142780,"backlog":141580,"state":"building",
                "progress":{"estimated_seconds":estimated}})
        };
        assert_eq!(
            meaning(&building(json!(null))),
            [
                "meaning index: 1,200 of 142,780 rows · 141,580 remaining",
                "build runs in the background · estimating time left"
            ]
        );
        assert_eq!(
            meaning(&building(json!(590)))[1],
            "build runs in the background · about 9m 50s left"
        );
    }
}
