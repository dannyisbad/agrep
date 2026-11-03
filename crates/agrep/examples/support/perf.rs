use serde_json::Value;

pub fn check(
    cases: &[Value],
    dense: bool,
    semantic: bool,
    rows: usize,
    text_bytes: u64,
) -> Result<(), Vec<String>> {
    let budgets: Value = serde_json::from_str(include_str!("../../../../docs/perf-budgets.json"))
        .expect("committed performance budget JSON");
    let mut failures = Vec::new();
    if dense && rows != 2_000_000 {
        failures.push("dense acceptance requires exactly 2000000 rows".into());
    }
    if !dense && !semantic && rows < 100_000 {
        failures.push("mixed acceptance requires at least 100000 rows".into());
    }
    if semantic && rows < 1_000 {
        failures.push("hybrid acceptance requires at least 1000 rows".into());
    }
    let required: &[&str] = if semantic {
        &[
            "first_index",
            "search_changed_source",
            "no_change_census",
            "one_session_refresh",
            "compact_first_hybrid",
            "compact_warm_hybrid",
            "version_control",
        ]
    } else if dense {
        &[
            "first_index",
            "search_changed_source",
            "no_change_census",
            "one_session_refresh",
            "old_dense",
            "old_dense_no_auto",
            "version_control",
        ]
    } else {
        &[
            "first_index",
            "search_changed_source",
            "no_change_census",
            "one_session_refresh",
            "release",
            "compact_release",
            "release_filtered",
            "compact_error",
            "error_filtered",
            "compact_natural",
            "natural_filtered",
            "compact_flaky",
            "flaky_filtered",
            "compact_rare",
            "rare_filtered",
            "short_filtered",
            "regex_filtered",
            "list_filtered",
            "json_filtered",
            "count_filtered",
            "rare_hit",
            "rare_hit_filtered",
            "rare_hit_pi_filtered",
            "tool_error",
            "all_error",
            "error",
            "natural",
            "flaky",
            "rare",
            "short",
            "regex",
            "list",
            "json",
            "count",
            "recall",
            "pack",
            "chats",
            "status",
            "around",
            "version_control",
        ]
    };
    if text_bytes == 0 {
        failures.push("indexed text byte count is required".into());
    }
    let index_scale = (rows as f64 / budgets["reference_rows"].as_f64().unwrap())
        .max(text_bytes as f64 / budgets["reference_text_bytes"].as_f64().unwrap())
        .min(1.0);
    for name in required {
        if !cases.iter().any(|c| c["name"] == *name) {
            failures.push(format!("missing benchmark case {name}"));
        }
    }
    for case in cases {
        let name = case["name"].as_str().unwrap_or("<missing>");
        let exits = case["exit_codes"].as_array();
        let samples = case["samples_ms"].as_array();
        let expected = if matches!(name, "rare" | "compact_rare" | "rare_filtered") {
            1
        } else {
            0
        };
        if exits.is_none_or(|v| v.is_empty() || v.iter().any(|rc| rc.as_i64() != Some(expected))) {
            failures.push(format!("{name}: failed CLI exit (expected {expected})"));
        }
        if !matches!(
            name,
            "first_index" | "no_change_census" | "one_session_refresh"
        ) && case["verified_results"]
            .as_array()
            .is_none_or(|v| Some(v.len()) != samples.map(Vec::len) || v.iter().any(|b| b != true))
        {
            failures.push(format!("{name}: missing or empty result evidence"));
        }
        if samples.is_none_or(|s| {
            s.is_empty()
                || s.iter()
                    .any(|n| n.as_f64().is_none_or(|v| !v.is_finite() || v < 0.))
        }) {
            failures.push(format!("{name}: invalid measurement samples"));
            continue;
        }
        if exits.map(Vec::len) != samples.map(Vec::len) {
            failures.push(format!("{name}: missing exit evidence"));
        }
        if (name == "compact_first_hybrid" || name == "compact_warm_hybrid")
            && case["evidence"]["bench_semantic_rows"]
                .as_u64()
                .unwrap_or(0)
                == 0
        {
            failures.push(format!("{name}: no meaning results"));
        }
        let minimum = match name {
            "first_index" => 3,
            "compact_first_hybrid" => 1,
            _ => 20,
        };
        if samples.unwrap().len() < minimum {
            failures.push(format!("{name}: requires at least {minimum} samples"));
        }
        if semantic && name == "first_index" {
            continue;
        }
        let key = match name {
            "compact_first_hybrid" => "hybrid_first_ms",
            "compact_warm_hybrid" => "hybrid_p50_ms",
            "first_index" if dense => "dense_index_ms",
            "first_index" => "first_index_ms",
            "no_change_census" => "census_ms",
            "one_session_refresh" => "incremental_ms",
            "recall" | "pack" => "recall_ms",
            "around" | "chats" | "status" => "navigation_ms",
            "old_dense" | "old_dense_no_auto" => "keyword_p50_ms",
            _ => "keyword_p50_ms",
        };
        let scale = if name == "first_index" {
            index_scale
        } else if dense {
            0.5
        } else {
            1.0
        };
        let mut observed: Vec<f64> = samples
            .unwrap()
            .iter()
            .map(|v| v.as_f64().unwrap())
            .collect();
        observed.sort_by(f64::total_cmp);
        let median = case["median_ms"]
            .as_f64()
            .map(|summary| summary.max(observed[observed.len() / 2]));
        measure(name, "median", median, &budgets, key, scale, &mut failures);
        if matches!(name, "first_index" | "compact_first_hybrid") {
            for (index, value) in samples.unwrap().iter().enumerate() {
                measure(
                    name,
                    &format!("sample {index}"),
                    value.as_f64(),
                    &budgets,
                    key,
                    scale,
                    &mut failures,
                );
            }
        } else {
            let tail = if key == "keyword_p50_ms" {
                "keyword_p95_ms"
            } else {
                key
            };
            let measured_tail = observed[(observed.len() * 95).div_ceil(100) - 1];
            // Neither a forged summary nor a forged sample list may hide a slow tail.
            let tail_value = case["p95_ms"]
                .as_f64()
                .map(|summary| summary.max(measured_tail));
            measure(
                name,
                "p95",
                tail_value,
                &budgets,
                tail,
                scale,
                &mut failures,
            );
        }
    }
    if failures.is_empty() {
        Ok(())
    } else {
        Err(failures)
    }
}

fn measure(
    name: &str,
    stat: &str,
    measured: Option<f64>,
    budgets: &Value,
    key: &str,
    scale: f64,
    failures: &mut Vec<String>,
) {
    let budget = budgets[key]
        .as_f64()
        .map(|n| n * scale)
        .filter(|n| n.is_finite() && *n > 0.);
    match (measured, budget) {
        (Some(value), Some(limit)) if value.is_finite() && value >= 0. => {
            eprintln!("{name} {stat}: {value:.2} ms / {limit:.2} ms budget");
            if value > limit {
                failures.push(format!("{name} {stat}: {value:.2} > {limit:.2} ms"));
            }
        }
        _ => failures.push(format!(
            "{name} {stat}: missing or non-finite measurement/budget"
        )),
    }
}

/// Every timed sample must prove its expected result, not merely exit quickly.
pub fn valid_result(name: &str, evidence: &Value) -> bool {
    if matches!(name, "rare" | "compact_rare" | "rare_filtered") {
        return true;
    }
    if matches!(name, "count" | "count_filtered") {
        return evidence["bench_count"].as_u64().is_some_and(|n| n > 0);
    }
    if matches!(
        name,
        "first_index" | "no_change_census" | "one_session_refresh"
    ) {
        return true;
    }
    if name.contains("hybrid") {
        return evidence["bench_semantic_rows"]
            .as_u64()
            .is_some_and(|n| n > 0);
    }
    if matches!(
        name,
        "json" | "json_filtered" | "tool_error" | "all_error" | "old_dense" | "old_dense_no_auto"
    ) {
        return evidence["bench_result_rows"]
            .as_u64()
            .is_some_and(|n| n > 0);
    }
    if matches!(name, "status" | "version_control") {
        return evidence["bench_stdout_bytes"]
            .as_u64()
            .is_some_and(|n| n > 0);
    }
    evidence["bench_result_rows"].as_u64().unwrap_or(0)
        + evidence["bench_text_hits"].as_u64().unwrap_or(0)
        > 0
}

/// Non-terminal keyword output is the documented six-column TSV surface.
pub fn flat_hit(line: &str) -> bool {
    let columns: Vec<_> = line.split('\t').collect();
    columns.len() == 6
        && !columns[0].is_empty()
        && columns[3].parse::<u64>().is_ok()
        && !columns[5].is_empty()
}
