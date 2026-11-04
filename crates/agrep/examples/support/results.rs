//! Result evidence for one CLI process, shared by the benchmark and the matched comparison.
use serde_json::{json, Value};

/// The first stdout line as JSON (or byte counts), plus counts of the result shapes
/// every surface can produce: JSON hit rows, rendered hits, an exact count and meaning rows,
/// and the statistics of 0.5's timing events.
pub fn evidence(stdout: &[u8], stderr: &[u8]) -> Value {
    let text = String::from_utf8_lossy(stdout);
    let first = text.lines().next().unwrap_or_default();
    let mut parsed: Value = serde_json::from_str(first)
        .unwrap_or_else(|_| json!({"stdout_bytes":stdout.len(),"stderr_bytes":stderr.len()}));
    if !parsed.is_object() {
        parsed = json!({"value":parsed});
    }
    timing_events(&mut parsed, stderr);
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
    let (messages, sessions) = index_report(&text);
    parsed["bench_index_messages"] = json!(messages);
    parsed["bench_index_sessions"] = json!(sessions);
    parsed["bench_top_handle"] = json!(objects
        .iter()
        .find(|r| r["kind"] != "agrep-meta" && r["session"].is_string())
        .map_or_else(
            || text
                .lines()
                .find(|line| line.starts_with('@'))
                .and_then(|line| line.split_whitespace().next()),
            |row| row["handle"].as_str()
        ));
    parsed
}

/// The totals an index run reports, in 0.4.9's words and 0.5's: "indexed N messages
/// across M sessions" after a refresh, "unchanged since last index (N messages)" without.
fn index_report(text: &str) -> (Option<u64>, Option<u64>) {
    for line in text.lines().map(str::trim_start) {
        if let Some(rest) = line.strip_prefix("indexed ") {
            let words: Vec<&str> = rest.split_whitespace().take(5).collect();
            if let [messages, "messages", "across", sessions, "sessions"] = words[..] {
                return (messages.parse().ok(), sessions.parse().ok());
            }
        }
        if let Some(rest) = line.strip_prefix("unchanged since last index (") {
            return (
                rest.split_whitespace().next().and_then(|n| n.parse().ok()),
                None,
            );
        }
    }
    (None, None)
}

/// The events 0.5 prints on stderr under `AGREP_QUERY_TIMING`, which index samples set in
/// both lanes (0.4.9 ignores it): an index run's statistics (its phases and counts) and
/// the time it was blocked on each lock file.
fn timing_events(evidence: &mut Value, stderr: &[u8]) {
    let text = String::from_utf8_lossy(stderr);
    for event in text
        .lines()
        .filter_map(|line| serde_json::from_str::<Value>(line).ok())
    {
        if let Some(Value::Object(fields)) = event.get("index") {
            for (key, value) in fields {
                evidence[key] = value.clone();
            }
        }
        let wait = &event["lock_wait"];
        if let (Some(lock), Some(ms)) = (wait["lock"].as_str(), wait["ms"].as_f64()) {
            let key = format!("wait_{lock}_ms");
            evidence[key.as_str()] = json!(evidence[key.as_str()].as_f64().unwrap_or(0.) + ms);
        }
    }
}

/// What a case's answer must be where the corpus fixes it. Without an expectation (real
/// history) a sample must still show a non-empty answer of its case's shape.
#[derive(Clone, Debug, Default)]
pub struct Expect {
    /// The messages and sessions the index holds: a full build and a no-change pass
    /// report them, a refresh reports them with its appended turns.
    pub corpus: Option<(u64, u64)>,
    /// The exact count of a `-c` case.
    pub count: Option<u64>,
    /// The handle of the planted answer, which must be the first result.
    pub top: Option<String>,
}

/// Every timed sample must prove its expected result, not merely exit quickly. A refresh
/// proves its commit with `bench_probe_count`: how often an untimed `--no-auto` count after
/// it found the unique text appended before it.
pub fn valid_result(name: &str, evidence: &Value, expect: &Expect) -> bool {
    let number = |key: &str| evidence[key].as_u64();
    if matches!(name, "rare" | "compact_rare" | "rare_filtered") {
        // A miss shows nothing but its exit code, which the caller checks.
        return true;
    }
    if matches!(name, "count" | "count_filtered") || name.ends_with("_count") {
        return match expect.count {
            Some(count) => number("bench_count") == Some(count),
            None => number("bench_count").is_some_and(|n| n > 0),
        };
    }
    if matches!(
        name,
        "first_index" | "no_change_index" | "one_session_refresh"
    ) {
        let (messages, sessions) = (
            number("bench_index_messages"),
            number("bench_index_sessions"),
        );
        let reported = match expect.corpus {
            // Only a pass that found nothing to do may omit the session count.
            Some((m, s)) => {
                messages == Some(m)
                    && (sessions == Some(s) || (name == "no_change_index" && sessions.is_none()))
            }
            None => messages.is_some_and(|n| n > 0),
        };
        return reported
            && (name != "one_session_refresh" || number("bench_probe_count") == Some(1));
    }
    if name == "no_change_census" {
        return number("changed_sources") == Some(0)
            && match expect.corpus {
                Some((_, sessions)) => number("stat_files") == Some(sessions),
                None => number("stat_files").is_some_and(|n| n > 0),
            };
    }
    if name.contains("hybrid") || name == "semantic_only" {
        let meaning = number("bench_semantic_rows").unwrap_or(0)
            + number("bench_meaning_lines").unwrap_or(0)
            > 0;
        return meaning
            && expect
                .top
                .as_ref()
                .is_none_or(|top| evidence["bench_top_handle"] == *top);
    }
    if matches!(
        name,
        "json" | "json_filtered" | "tool_error" | "all_error" | "old_dense" | "old_dense_no_auto"
    ) {
        return number("bench_result_rows").is_some_and(|n| n > 0);
    }
    if matches!(name, "status" | "version_control") {
        return number("bench_stdout_bytes").is_some_and(|n| n > 0);
    }
    number("bench_result_rows").unwrap_or(0) + number("bench_text_hits").unwrap_or(0) > 0
}

/// Non-terminal keyword output is the documented six-column TSV surface.
pub fn flat_hit(line: &str) -> bool {
    let columns: Vec<_> = line.split('\t').collect();
    columns.len() == 6
        && !columns[0].is_empty()
        && columns[3].parse::<u64>().is_ok()
        && !columns[5].is_empty()
}
