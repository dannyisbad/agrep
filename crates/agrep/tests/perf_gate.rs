#[path = "../examples/support/perf.rs"]
mod perf;
#[path = "../examples/support/results.rs"]
mod results;
use serde_json::{json, Value};

fn case(name: &str) -> Value {
    json!({"name":name,"median_ms":1.0,"p95_ms":2.0,"samples_ms":vec![1.0;20],"exit_codes":vec![0;20],"verified_results":vec![true;20]})
}
fn dense() -> Vec<Value> {
    [
        "first_index",
        "no_change_census",
        "one_session_refresh",
        "old_dense",
        "old_dense_no_auto",
        "version_control",
    ]
    .into_iter()
    .map(case)
    .chain(std::iter::once(case("search_changed_source")))
    .collect()
}
fn budget(key: &str) -> f64 {
    let b: Value = serde_json::from_str(include_str!("../../../docs/perf-budgets.json")).unwrap();
    b[key].as_f64().unwrap()
}
fn failures(cases: &[Value], dense: bool, semantic: bool, rows: usize, text_bytes: u64) -> String {
    perf::check(cases, dense, semantic, rows, text_bytes)
        .failures
        .join(" ")
}
fn targets(cases: &[Value], dense: bool, semantic: bool, rows: usize, text_bytes: u64) -> String {
    perf::check(cases, dense, semantic, rows, text_bytes)
        .targets
        .join("\n")
}
#[test]
fn check_accepts_complete_fast_evidence() {
    assert_eq!(failures(&dense(), true, false, 2_000_000, 886_000_000), "");
}
#[test]
fn check_refuses_failure_in_any_command() {
    let mut cases = dense();
    cases[5]["exit_codes"][0] = json!(2);
    assert!(failures(&cases, true, false, 2_000_000, 886_000_000).contains("failed CLI exit"));
}
#[test]
fn check_refuses_missing_cases_small_corpus_and_tail_samples() {
    let mut cases = dense();
    cases.retain(|c| c["name"] != "version_control");
    cases[3]["p95_ms"] = Value::Null;
    cases[3]["samples_ms"] = json!([1.0]);
    let errors = failures(&cases, true, false, 1000, 886_000_000);
    for text in [
        "missing benchmark",
        "2000000",
        "20 samples",
        "missing or non-finite",
    ] {
        assert!(errors.contains(text), "{errors}");
    }
}
#[test]
fn latency_above_a_target_is_reported_never_failed() {
    let mut cases = dense();
    cases[3]["p95_ms"] = json!(budget("dense_p95_ms") + 1.0);
    cases[4]["median_ms"] = json!(budget("dense_p50_ms") + 1.0);
    let checked = perf::check(&cases, true, false, 2_000_000, 886_000_000);
    assert!(checked.failures.is_empty(), "{:?}", checked.failures);
    let lines = checked.targets.join("\n");
    assert!(
        lines.contains("old_dense p95: 41.00 ms (target 40.00 ms, above target)"),
        "{lines}"
    );
    assert!(
        lines.contains("old_dense_no_auto median: 21.00 ms (target 20.00 ms, above target)"),
        "{lines}"
    );
}
#[test]
fn check_refuses_missing_nonfinite_or_negative_samples() {
    let mut cases = dense();
    cases[0]["samples_ms"][0] = json!(-1.0);
    cases[1]["median_ms"] = Value::Null;
    let errors = failures(&cases, true, false, 2_000_000, 886_000_000);
    assert!(errors.contains("invalid measurement samples"));
    assert!(errors.contains("missing or non-finite"));
}

#[test]
fn hybrid_check_requires_meaning_results_but_not_a_startup_deadline() {
    let mut cases: Vec<Value> = [
        "first_index",
        "no_change_census",
        "one_session_refresh",
        "compact_first_hybrid",
        "compact_warm_hybrid",
        "version_control",
    ]
    .into_iter()
    .map(case)
    .chain(std::iter::once(case("search_changed_source")))
    .collect();
    cases[3]["median_ms"] = json!(budget("hybrid_first_ms") + 1.0);
    let errors = failures(&cases, false, true, 1000, 886_000_000);
    assert!(errors.contains("no meaning results"));
    assert!(!errors.contains("compact_first_hybrid median"));
}

#[test]
fn check_refuses_reduced_mixed_and_hybrid_workloads() {
    assert!(failures(&dense(), false, false, 99999, 886_000_000).contains("at least 100000 rows"));
    assert!(failures(&dense(), false, true, 999, 886_000_000).contains("at least 1000 rows"));
}

#[test]
fn check_rejects_empty_answers_and_unexpected_no_match() {
    let mut cases = dense();
    cases[3]["exit_codes"][0] = json!(1);
    cases[4]["verified_results"][0] = json!(false);
    let errors = failures(&cases, true, false, 2_000_000, 886_000_000);
    assert!(errors.contains("failed CLI exit"));
    assert!(errors.contains("empty result evidence"));
    let any = results::Expect::default();
    assert!(!results::valid_result(
        "count",
        &json!({"bench_count":0}),
        &any
    ));
    assert!(!results::valid_result(
        "json",
        &json!({"bench_stdout_bytes":200,"bench_result_rows":0}),
        &any
    ));
}
#[test]
fn latency_targets_do_not_shrink_with_corpus_size() {
    let mut release = case("release");
    release["median_ms"] = json!(budget("keyword_p50_ms") - 1.0);
    let mut census = case("no_change_census");
    census["median_ms"] = json!(budget("census_ms") - 1.0);
    let lines = targets(&[release, census], false, false, 100_000, 1_000_000);
    for text in [
        "release median: 39.00 ms (target 40.00 ms)",
        "release p95: 2.00 ms (target 80.00 ms)",
        "no_change_census median: 14.00 ms (target 15.00 ms)",
    ] {
        assert!(lines.contains(text), "{lines}");
    }
}
#[test]
fn index_target_scales_by_the_larger_workload_fraction() {
    let mut index = case("first_index");
    index["median_ms"] = json!(budget("first_index_ms") * 0.45);
    let rows_target = budget("first_index_ms") * 600_000.0 / budget("reference_rows");
    let rows_dominate = targets(&[index.clone()], false, false, 600_000, 88_600_000);
    assert!(
        rows_dominate.contains(&format!(
            "first_index median: 27000.00 ms (target {rows_target:.2} ms)"
        )),
        "{rows_dominate}"
    );
    let bytes_dominate = targets(&[index], false, false, 100_000, 88_600_000);
    assert!(
        bytes_dominate
            .contains("first_index median: 27000.00 ms (target 6000.00 ms, above target)"),
        "{bytes_dominate}"
    );
    let mut dense_index = case("first_index");
    dense_index["median_ms"] = json!(budget("dense_index_ms") + 1.0);
    let dense = targets(&[dense_index], true, false, 2_000_000, 1_000_000);
    assert!(
        dense.contains("first_index median: 30001.00 ms (target 30000.00 ms, above target)"),
        "{dense}"
    );
}
#[test]
fn semantic_workload_build_time_has_no_target() {
    let mut index = case("first_index");
    index["median_ms"] = json!(budget("first_index_ms") * 10.0);
    assert!(!targets(&[index], false, true, 1000, 200_000).contains("first_index median"));
}

#[path = "../examples/support/process.rs"]
mod bench_process;
#[test]
fn benchmark_children_have_isolated_homes_and_windows_system_root() {
    use std::{ffi::OsStr, path::Path};
    let command = bench_process::command(
        Path::new("binary"),
        Path::new("fixture-home"),
        Path::new("fixture-data"),
        "classic",
        "fixture-bin".into(),
        Some("system-directory".into()),
    );
    let env: std::collections::HashMap<_, _> = command.get_envs().collect();
    for key in ["HOME", "AGREP_HOME", "USERPROFILE"] {
        assert_eq!(env[OsStr::new(key)], Some(OsStr::new("fixture-home")));
    }
    assert_eq!(
        env[OsStr::new("SystemRoot")],
        Some(OsStr::new("system-directory"))
    );
    assert_eq!(
        env[OsStr::new("AGREP_DATA_DIR")],
        Some(OsStr::new("fixture-data"))
    );
    assert!(!env.contains_key(OsStr::new("AGREP_MODEL_DIR")));
}

#[test]
fn benchmark_verifies_nonterminal_tsv_hit_rows() {
    assert!(results::flat_hit(
        "example-session\tpi\tdemo\t2\tuser\trelease workflow"
    ));
    assert!(!results::flat_hit("no results"));
    assert!(!results::flat_hit(
        "session\tpi\tdemo\tnot-a-turn\tuser\ttext"
    ));
    assert!(!results::flat_hit("session\tpi\tdemo\t2\tuser\t"));
}

#[test]
fn evidence_counts_rows_hits_counts_and_meaning_matches() {
    let rows = results::evidence(
        b"{\"kind\":\"agrep-meta\"}\n{\"session\":\"s\",\"handle\":\"@s:1.ab\"}\n{\"session\":\"t\",\"lane\":\"semantic\",\"sem_score\":0.9}\n",
        b"",
    );
    assert_eq!(rows["kind"], "agrep-meta");
    assert_eq!(rows["bench_result_rows"], 2);
    assert_eq!(rows["bench_semantic_rows"], 1);
    assert_eq!(rows["bench_handles"], json!(["@s:1.ab"]));
    let rendered = results::evidence(
        b"@s:1.ab pi/user demo 0m ~semantic (meaning match) text\n@t:2.cd pi/user demo 0m ~all text\n",
        b"",
    );
    assert_eq!(rendered["bench_meaning_lines"], 1);
    assert_eq!(rendered["bench_text_hits"], 2);
    let any = results::Expect::default();
    assert!(results::valid_result("warm_hybrid", &rendered, &any));
    assert_eq!(rendered["bench_top_handle"], "@s:1.ab");
    let keyword_only = results::evidence(b"@t:2.cd pi/user demo 0m ~all text\n", b"");
    assert!(!results::valid_result("warm_hybrid", &keyword_only, &any));
    let count = results::evidence(b"2555\n", b"");
    assert_eq!(count["bench_count"], 2555);
    assert!(results::valid_result("count", &count, &any));
    let empty = results::evidence(b"", b"keyword: 0 matching rows\n");
    assert!(!results::valid_result("release", &empty, &any));
    assert!(!results::valid_result("status", &empty, &any));
    assert_eq!(rows["bench_top_handle"], "@s:1.ab");
    let report = results::evidence(
        "=== indexing transcripts ===\n  indexed 8 messages across 5 sessions (40ms)\n".as_bytes(),
        b"",
    );
    assert_eq!(
        (
            &report["bench_index_messages"],
            &report["bench_index_sessions"]
        ),
        (&json!(8), &json!(5))
    );
    let index = results::evidence(
        b"=== indexing transcripts ===\n",
        b"{\"phase\":\"freshness\",\"wall_ms\":0.5}\n{\"lock_wait\":{\"lock\":\"writer_queue\",\"ms\":0.25}}\n{\"lock_wait\":{\"lock\":\"writer_queue\",\"ms\":0.5}}\n{\"index\":{\"parsed\":3,\"census_ms\":1.5}}\nnot json\n",
    );
    assert_eq!(
        (
            &index["parsed"],
            &index["census_ms"],
            &index["wait_writer_queue_ms"]
        ),
        (&json!(3), &json!(1.5), &json!(0.75))
    );
    assert!(index.get("phase").is_none());
}

#[test]
fn every_dense_latency_target_has_twofold_headroom() {
    let mut cases = Vec::new();
    for name in [
        "no_change_census",
        "one_session_refresh",
        "search_changed_source",
        "version_control",
    ] {
        let mut c = case(name);
        c["median_ms"] = json!(100.0);
        c["p95_ms"] = json!(100.0);
        cases.push(c);
    }
    let lines = targets(&cases, true, false, 2_000_000, 886_000_000);
    for (name, key, tail) in [
        ("no_change_census", "census_ms", "census_ms"),
        ("one_session_refresh", "incremental_ms", "incremental_ms"),
        ("search_changed_source", "keyword_p50_ms", "keyword_p95_ms"),
        ("version_control", "keyword_p50_ms", "keyword_p95_ms"),
    ] {
        for (stat, target) in [("median", budget(key) / 2.0), ("p95", budget(tail) / 2.0)] {
            let line = format!("{name} {stat}: 100.00 ms (target {target:.2} ms, above target)");
            assert!(lines.contains(&line), "{line}\n{lines}");
        }
    }
}
#[test]
fn every_warm_case_reports_its_tail_and_each_first_index_sample() {
    let lines = targets(
        &[
            case("around"),
            case("recall"),
            case("compact_warm_hybrid"),
            case("first_index"),
        ],
        false,
        false,
        100_000,
        886_000_000,
    );
    for (name, key) in [
        ("around", "navigation_ms"),
        ("recall", "recall_ms"),
        ("compact_warm_hybrid", "hybrid_p50_ms"),
    ] {
        let line = format!("{name} p95: 2.00 ms (target {:.2} ms)", budget(key));
        assert!(lines.contains(&line), "{line}\n{lines}");
    }
    assert_eq!(lines.matches("first_index sample").count(), 20, "{lines}");
}
#[test]
fn raw_samples_cannot_hide_behind_an_understated_summary() {
    let mut c = case("release");
    c["samples_ms"] = json!(vec![budget("keyword_p50_ms") + 1.0; 20]);
    assert!(targets(&[c], false, false, 100_000, 886_000_000)
        .contains("release median: 41.00 ms (target 40.00 ms, above target)"));
}
