//! The matched comparison's gate: a case is won only by a verified candidate whose median
//! and p95 are both below a verified reference's.
#[path = "../examples/support/matched.rs"]
mod matched;
#[path = "../examples/support/results.rs"]
mod results;
use matched::Lane;
use serde_json::{json, Value};

fn lane(samples: &[f64]) -> Lane {
    let mut lane = Lane::default();
    for ms in samples {
        lane.record(*ms, 0, true);
    }
    lane
}
fn twenty(ms: f64) -> Vec<f64> {
    (0..20).map(|i| ms + i as f64).collect()
}
fn case(name: &str, reference: &Lane, candidate: &Lane) -> Value {
    let mut value = matched::verdict(reference, candidate, 0, 20);
    value["name"] = json!(name);
    value
}

#[test]
fn faster_candidate_on_both_statistics_wins() {
    let verdict = matched::verdict(&lane(&twenty(300.)), &lane(&twenty(20.)), 0, 20);
    assert_eq!(verdict["verdict"], "win");
    assert_eq!(verdict["reference"]["median_ms"], 310.);
    assert_eq!(verdict["reference"]["p95_ms"], 318.);
    assert!((verdict["median_ratio"].as_f64().unwrap() - 30. / 310.).abs() < 1e-12);
    assert!(matched::gate(
        &[case("release", &lane(&twenty(300.)), &lane(&twenty(20.)))],
        &["release"]
    )
    .is_empty());
}

#[test]
fn slower_candidate_fails() {
    let slower = case("release", &lane(&twenty(20.)), &lane(&twenty(300.)));
    assert_eq!(slower["verdict"], "loss");
    let failures = matched::gate(&[slower], &["release"]);
    assert_eq!(failures.len(), 1);
    assert!(failures[0].starts_with("release: loss"), "{failures:?}");
}

#[test]
fn a_faster_median_cannot_hide_a_slower_tail_or_a_tie() {
    let reference = lane(&twenty(100.));
    let mut tail = twenty(50.);
    tail[18] = 500.;
    tail[19] = 500.;
    assert_eq!(
        matched::verdict(&reference, &lane(&tail), 0, 20)["verdict"],
        "loss"
    );
    let mut median = twenty(10.);
    for ms in median.iter_mut().take(11) {
        *ms = 200.;
    }
    assert_eq!(
        matched::verdict(&reference, &lane(&median), 0, 20)["verdict"],
        "loss"
    );
    assert_eq!(
        matched::verdict(&reference, &reference, 0, 20)["verdict"],
        "loss"
    );
}

#[test]
fn a_reference_failure_is_never_a_win() {
    let fast = lane(&twenty(1.));
    let mut crashed = lane(&twenty(1000.));
    crashed.exit_codes[3] = 2;
    let mut empty = lane(&twenty(1000.));
    empty.verified[0] = false;
    for reference in [crashed, empty, lane(&twenty(1000.)[..19])] {
        let value = case("release", &reference, &fast);
        assert_eq!(value["verdict"], "reference_failure");
        assert!(value["reference_failure"].is_string());
        let failures = matched::gate(&[value], &["release"]);
        assert!(failures[0].contains("reference_failure"), "{failures:?}");
    }
}

#[test]
fn a_candidate_failure_fails_whatever_the_reference_did() {
    let mut candidate = lane(&twenty(1.));
    candidate.exit_codes[0] = 1;
    assert_eq!(
        case("release", &lane(&twenty(500.)), &candidate)["verdict"],
        "candidate_failure"
    );
    let mut reference = lane(&twenty(500.));
    reference.verified[0] = false;
    assert_eq!(
        case("release", &reference, &candidate)["verdict"],
        "both_failed"
    );
    let mut invalid = lane(&twenty(1.));
    invalid.samples_ms[4] = f64::NAN;
    assert_eq!(
        case("release", &lane(&twenty(500.)), &invalid)["verdict"],
        "candidate_failure"
    );
}

#[test]
fn expected_misses_count_only_as_misses() {
    let miss = |exit| {
        let mut lane = Lane::default();
        for i in 0..20 {
            lane.record(5. + i as f64, exit, true);
        }
        lane
    };
    assert_eq!(
        matched::verdict(&miss(1), &miss(1), 1, 20)["verdict"],
        "loss"
    );
    assert_eq!(
        matched::verdict(&miss(2), &miss(1), 1, 20)["verdict"],
        "reference_failure"
    );
    assert_eq!(
        matched::verdict(&miss(1), &miss(0), 1, 20)["verdict"],
        "candidate_failure"
    );
}

#[test]
fn both_lanes_results_are_verified_by_the_same_predicates() {
    let hits = results::evidence(b"session\tpi\tdemo\t2\tuser\trelease workflow\n", b"");
    let empty = results::evidence(b"", b"keyword: 0 matching rows\n");
    let none: &[String] = &[];
    assert!(matched::sample_verified("release", none, &hits, None));
    assert!(!matched::sample_verified("release", none, &empty, None));
    let record = |evidence: &Value| {
        let mut lane = Lane::default();
        for i in 0..20 {
            lane.record(
                10. + i as f64,
                0,
                matched::sample_verified("release", none, evidence, None),
            );
        }
        lane
    };
    let fast_empty = record(&empty);
    let slow_hits = {
        let mut lane = record(&hits);
        lane.samples_ms.iter_mut().for_each(|ms| *ms *= 50.);
        lane
    };
    assert_eq!(
        matched::verdict(&slow_hits, &fast_empty, 0, 20)["verdict"],
        "candidate_failure"
    );
    assert_eq!(
        matched::verdict(&fast_empty, &slow_hits, 0, 20)["verdict"],
        "reference_failure"
    );
    let meaning = results::evidence(b"@s:0.ab pi/user p 0m ~semantic text\n", b"");
    let keyword = results::evidence(b"@s:0.ab pi/user p 0m ~all text\n", b"");
    assert!(matched::sample_verified(
        "warm_hybrid",
        none,
        &meaning,
        None
    ));
    assert!(!matched::sample_verified(
        "first_hybrid",
        none,
        &keyword,
        None
    ));
}

#[test]
fn the_dense_oracle_binds_the_candidate_to_exact_answers() {
    let oracle = (vec!["@a:1.aa".to_string(), "@b:2.bb".to_string()], 7usize);
    let args = |a: &[&str]| a.iter().map(|s| s.to_string()).collect::<Vec<_>>();
    let rows = results::evidence(
        b"{\"kind\":\"agrep-meta\"}\n{\"session\":\"a\",\"handle\":\"@a:1.aa\"}\n{\"session\":\"b\",\"handle\":\"@b:2.bb\"}\n",
        b"",
    );
    let reordered = results::evidence(
        b"{\"kind\":\"agrep-meta\"}\n{\"session\":\"b\",\"handle\":\"@b:2.bb\"}\n{\"session\":\"a\",\"handle\":\"@a:1.aa\"}\n",
        b"",
    );
    let json = args(&["deepold", "--json"]);
    assert!(matched::sample_verified(
        "old_dense",
        &json,
        &rows,
        Some(&oracle)
    ));
    assert!(!matched::sample_verified(
        "old_dense",
        &json,
        &reordered,
        Some(&oracle)
    ));
    assert!(matched::sample_verified(
        "old_dense",
        &json,
        &reordered,
        None
    ));
    let count = args(&["deepold", "-c"]);
    let seven = results::evidence(b"7\n", b"");
    let eight = results::evidence(b"8\n", b"");
    assert!(matched::sample_verified(
        "old_dense_count",
        &count,
        &seven,
        Some(&oracle)
    ));
    assert!(!matched::sample_verified(
        "old_dense_count",
        &count,
        &eight,
        Some(&oracle)
    ));
    assert!(matched::sample_verified(
        "old_dense_count",
        &count,
        &eight,
        None
    ));
}

#[test]
fn every_required_case_must_be_present() {
    let won = case("release", &lane(&twenty(300.)), &lane(&twenty(20.)));
    let failures = matched::gate(&[won], &["release", "first_index"]);
    assert_eq!(failures, vec!["first_index: missing case".to_string()]);
}

#[test]
fn unpaired_lanes_fail_the_short_lane() {
    let short = lane(&twenty(20.)[..19]);
    assert_eq!(
        matched::verdict(&lane(&twenty(300.)), &short, 0, 3)["verdict"],
        "candidate_failure"
    );
    assert_eq!(
        matched::verdict(&short, &lane(&twenty(300.)), 0, 3)["verdict"],
        "reference_failure"
    );
}

#[test]
fn statistics_are_the_upper_median_and_nearest_rank_p95() {
    let samples: Vec<f64> = (1..=20).map(f64::from).collect();
    assert_eq!(matched::median(&samples), Some(11.));
    assert_eq!(matched::p95(&samples), Some(19.));
    assert_eq!(matched::median(&[3., 1., 2.]), Some(2.));
    assert_eq!(matched::p95(&[3., 1., 2.]), Some(3.));
    assert_eq!(matched::median(&[]), None);
    assert_eq!(matched::p95(&[]), None);
}
