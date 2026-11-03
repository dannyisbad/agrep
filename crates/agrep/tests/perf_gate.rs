#[path = "../examples/support/perf.rs"]
mod perf;
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
#[test]
fn gate_accepts_complete_fast_evidence() {
    assert!(perf::check(&dense(), true, false, 2_000_000, 886_000_000).is_ok());
}
#[test]
fn gate_refuses_failure_in_any_command() {
    let mut cases = dense();
    cases[5]["exit_codes"][0] = json!(2);
    assert!(perf::check(&cases, true, false, 2_000_000, 886_000_000)
        .unwrap_err()
        .iter()
        .any(|s| s.contains("failed CLI exit")));
}
#[test]
fn gate_refuses_missing_cases_small_corpus_and_tail_samples() {
    let mut cases = dense();
    cases.retain(|c| c["name"] != "version_control");
    cases[3]["p95_ms"] = Value::Null;
    cases[3]["samples_ms"] = json!([1.0]);
    let errors = perf::check(&cases, true, false, 1000, 886_000_000)
        .unwrap_err()
        .join(" ");
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
fn gate_enforces_tail_and_median_separately() {
    let mut cases = dense();
    cases[3]["p95_ms"] = json!(budget("dense_p95_ms") + 1.0);
    cases[4]["median_ms"] = json!(budget("dense_p50_ms") + 1.0);
    let errors = perf::check(&cases, true, false, 2_000_000, 886_000_000)
        .unwrap_err()
        .join(" ");
    assert!(errors.contains("old_dense p95"));
    assert!(errors.contains("old_dense_no_auto median"));
}
#[test]
fn gate_refuses_missing_nonfinite_or_negative_samples() {
    let mut cases = dense();
    cases[0]["samples_ms"][0] = json!(-1.0);
    cases[1]["median_ms"] = Value::Null;
    let errors = perf::check(&cases, true, false, 2_000_000, 886_000_000)
        .unwrap_err()
        .join(" ");
    assert!(errors.contains("invalid measurement samples"));
    assert!(errors.contains("missing or non-finite"));
}

#[test]
fn hybrid_gate_requires_meaning_results_and_startup_deadline() {
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
    let errors = perf::check(&cases, false, true, 1000, 886_000_000)
        .unwrap_err()
        .join(" ");
    assert!(errors.contains("no meaning results"));
    assert!(errors.contains("compact_first_hybrid median"));
}

#[test]
fn gate_refuses_reduced_mixed_and_hybrid_workloads() {
    let mixed = perf::check(&dense(), false, false, 99999, 886_000_000)
        .unwrap_err()
        .join(" ");
    assert!(mixed.contains("at least 100000 rows"));
    let hybrid = perf::check(&dense(), false, true, 999, 886_000_000)
        .unwrap_err()
        .join(" ");
    assert!(hybrid.contains("at least 1000 rows"));
}

fn budget(key: &str) -> f64 {
    let b: Value = serde_json::from_str(include_str!("../../../docs/perf-budgets.json")).unwrap();
    b[key].as_f64().unwrap()
}
#[test]
fn gate_rejects_empty_answers_and_unexpected_no_match() {
    let mut cases = dense();
    cases[3]["exit_codes"][0] = json!(1);
    cases[4]["verified_results"][0] = json!(false);
    let errors = perf::check(&cases, true, false, 2_000_000, 886_000_000)
        .unwrap_err()
        .join(" ");
    assert!(errors.contains("failed CLI exit"));
    assert!(errors.contains("empty result evidence"));
    assert!(!perf::valid_result("count", &json!({"bench_count":0})));
    assert!(!perf::valid_result(
        "json",
        &json!({"bench_stdout_bytes":200,"bench_result_rows":0})
    ));
}
fn errors(cases: &[Value], dense: bool, semantic: bool, rows: usize, text_bytes: u64) -> String {
    perf::check(cases, dense, semantic, rows, text_bytes)
        .err()
        .unwrap_or_default()
        .join(" ")
}
#[test]
fn latency_limits_do_not_shrink_with_corpus_size() {
    let mut release = case("release");
    release["median_ms"] = json!(budget("keyword_p50_ms") - 1.0);
    release["p95_ms"] = json!(budget("keyword_p95_ms") - 1.0);
    let mut census = case("no_change_census");
    census["median_ms"] = json!(budget("census_ms") - 1.0);
    let report = errors(&[release, census], false, false, 100_000, 1_000_000);
    for text in ["release median", "release p95", "no_change_census median"] {
        assert!(!report.contains(text), "{report}");
    }
}
#[test]
fn index_limit_scales_by_the_larger_workload_fraction() {
    let mut index = case("first_index");
    index["median_ms"] = json!(budget("first_index_ms") * 0.45);
    let rows_dominate = errors(&[index.clone()], false, false, 600_000, 88_600_000);
    assert!(
        !rows_dominate.contains("first_index median"),
        "{rows_dominate}"
    );
    let bytes_dominate = errors(&[index], false, false, 100_000, 88_600_000);
    assert!(
        bytes_dominate.contains("first_index median"),
        "{bytes_dominate}"
    );
    let mut dense_index = dense();
    dense_index[0]["median_ms"] = json!(budget("dense_index_ms") + 1.0);
    assert!(errors(&dense_index, true, false, 2_000_000, 1_000_000).contains("first_index median"));
}
#[test]
fn semantic_workload_build_time_is_not_budgeted() {
    let mut index = case("first_index");
    index["median_ms"] = json!(budget("first_index_ms") * 10.0);
    assert!(!errors(&[index], false, true, 1000, 200_000).contains("first_index median"));
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
    assert!(perf::flat_hit(
        "example-session\tpi\tdemo\t2\tuser\trelease workflow"
    ));
    assert!(!perf::flat_hit("no results"));
    assert!(!perf::flat_hit("session\tpi\tdemo\tnot-a-turn\tuser\ttext"));
    assert!(!perf::flat_hit("session\tpi\tdemo\t2\tuser\t"));
}

#[test]
fn every_dense_latency_has_twofold_headroom() {
    for (name, key, limit) in [
        ("no_change_census", "median_ms", budget("census_ms") / 2.0),
        ("no_change_census", "p95_ms", budget("census_ms") / 2.0),
        (
            "one_session_refresh",
            "median_ms",
            budget("incremental_ms") / 2.0,
        ),
        (
            "one_session_refresh",
            "p95_ms",
            budget("incremental_ms") / 2.0,
        ),
        (
            "search_changed_source",
            "median_ms",
            budget("keyword_p50_ms") / 2.0,
        ),
        (
            "search_changed_source",
            "p95_ms",
            budget("keyword_p95_ms") / 2.0,
        ),
        (
            "version_control",
            "median_ms",
            budget("keyword_p50_ms") / 2.0,
        ),
        ("version_control", "p95_ms", budget("keyword_p95_ms") / 2.0),
    ] {
        let mut c = case(name);
        c[key] = json!(limit + 0.1);
        assert!(
            errors(&[c], true, false, 2_000_000, 886_000_000).contains(name),
            "{name} {key}"
        );
    }
}
#[test]
fn every_warm_case_checks_its_tail_and_each_first_index_is_bounded() {
    for (name, limit) in [
        ("around", budget("navigation_ms")),
        ("recall", budget("recall_ms")),
        ("compact_warm_hybrid", budget("hybrid_p50_ms")),
    ] {
        let mut c = case(name);
        c["p95_ms"] = json!(limit + 1.0);
        assert!(errors(&[c], false, false, 100_000, 886_000_000).contains(&format!("{name} p95")));
    }
    let mut c = case("first_index");
    c["samples_ms"][1] = json!(budget("first_index_ms") + 1.0);
    assert!(errors(&[c], false, false, 100_000, 886_000_000).contains("first_index sample"));
}
#[test]
fn raw_samples_cannot_hide_behind_an_understated_summary() {
    let mut c = case("release");
    c["samples_ms"] = json!(vec![budget("keyword_p50_ms") + 1.0; 20]);
    assert!(errors(&[c], false, false, 100_000, 886_000_000).contains("release median"));
}
