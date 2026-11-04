//! Result evidence for one CLI process, shared by the benchmark and the matched comparison.
use serde_json::{json, Value};

/// The first stdout line as JSON (or byte counts), plus counts of the result shapes
/// every surface can produce: JSON hit rows, rendered hits, an exact count and meaning rows.
pub fn evidence(stdout: &[u8], stderr: &[u8]) -> Value {
    let text = String::from_utf8_lossy(stdout);
    let first = text.lines().next().unwrap_or_default();
    let mut parsed: Value = serde_json::from_str(first)
        .unwrap_or_else(|_| json!({"stdout_bytes":stdout.len(),"stderr_bytes":stderr.len()}));
    if !parsed.is_object() {
        parsed = json!({"value":parsed});
    }
    let objects: Vec<Value> = text
        .lines()
        .filter_map(|line| serde_json::from_str(line).ok())
        .collect();
    parsed["bench_semantic_rows"] = json!(objects
        .iter()
        .filter(|row| {
            row.get("sem_score").is_some_and(|v| v.is_number()) || row["lane"] == "semantic"
        })
        .count());
    // Rendered agent-profile rows label meaning matches `~semantic` (or `~semantic-weak`).
    parsed["bench_meaning_lines"] = json!(text
        .lines()
        .filter(|line| serde_json::from_str::<Value>(line).is_err() && line.contains("~semantic"))
        .count());
    parsed["bench_stdout_bytes"] = json!(stdout.len());
    parsed["bench_result_rows"] = json!(objects
        .iter()
        .filter(|r| r["kind"] != "agrep-meta" && r["session"].is_string())
        .count());
    parsed["bench_handles"] = json!(objects
        .iter()
        .filter_map(|r| r["handle"].as_str())
        .collect::<Vec<_>>());
    parsed["bench_text_hits"] = json!(text
        .lines()
        .filter(|line| flat_hit(line)
            || line.contains('@')
            || line.contains('›')
            || line.contains('→')
            || line.contains('⚙')
            || line.contains("pi/user ")
            || line.contains("codex/user ")
            || line.starts_with("user: "))
        .count());
    parsed["bench_count"] = json!(text.trim().parse::<u64>().ok());
    parsed
}

/// The statistics file 0.5's `index` and `reindex` write to the data root; stdout carries
/// 0.4.9's human report.
pub const INDEX_STATS: &str = "index-stats.json";

/// `evidence` with the fields of an index run's statistics file: the phase timings and
/// counts that the first stdout line held before the report replaced it.
pub fn with_index_stats(mut evidence: Value, stats: &[u8]) -> Value {
    if let Ok(Value::Object(fields)) = serde_json::from_slice::<Value>(stats) {
        for (key, value) in fields {
            evidence[key] = value;
        }
    }
    evidence
}

/// Every timed sample must prove its expected result, not merely exit quickly.
pub fn valid_result(name: &str, evidence: &Value) -> bool {
    if matches!(name, "rare" | "compact_rare" | "rare_filtered") {
        return true;
    }
    if matches!(name, "count" | "count_filtered") || name.ends_with("_count") {
        return evidence["bench_count"].as_u64().is_some_and(|n| n > 0);
    }
    if matches!(
        name,
        "first_index" | "no_change_census" | "no_change_index" | "one_session_refresh"
    ) {
        return true;
    }
    if name.contains("hybrid") || name == "semantic_only" {
        return evidence["bench_semantic_rows"].as_u64().unwrap_or(0)
            + evidence["bench_meaning_lines"].as_u64().unwrap_or(0)
            > 0;
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
