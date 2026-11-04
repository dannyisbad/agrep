//! The matched comparison's gate: a case is won only by a verified candidate whose median
//! and p95 are both below a verified reference's, over samples taken as the schedule asks.
#[path = "../examples/support/corpus.rs"]
mod corpus;
#[path = "../examples/support/matched.rs"]
mod matched;
#[path = "../examples/support/results.rs"]
mod results;
mod support;
use matched::{Lane, Next, Stop};
use results::Expect;
use serde_json::{json, Value};

/// Samples a case the way the harness does: `reference(i)` and `candidate(i)` are each
/// lane's i-th sample. Also returns the order of samples: `R` reference, `C` a candidate
/// sample paired with the reference sample before it, `c` a candidate sample taken alone.
fn drive(
    planned: usize,
    reference: impl Fn(usize) -> f64,
    candidate: impl Fn(usize) -> f64,
) -> (Lane, Lane, String) {
    let (mut r, mut c, mut order) = (Lane::default(), Lane::default(), String::new());
    loop {
        match matched::next(planned, &r.samples_ms, &c.samples_ms) {
            Next::Done => return (r, c, order),
            Next::Reference => {
                r.record(reference(r.samples_ms.len()), 0, true);
                order.push('R');
            }
            Next::Candidate { paired } => {
                c.record(candidate(c.samples_ms.len()), 0, true);
                order.push(if paired { 'C' } else { 'c' });
            }
        }
    }
}
fn counting(from: f64) -> impl Fn(usize) -> f64 {
    move |i| from + i as f64
}
fn case(name: &str, reference: &Lane, candidate: &Lane) -> Value {
    let mut value = matched::verdict(reference, candidate, 0, 20);
    value["name"] = json!(name);
    value
}
fn pairs(n: usize) -> String {
    "RC".repeat(n)
}

#[test]
fn a_separated_faster_case_stops_the_reference_after_five_pairs() {
    let (reference, candidate, order) = drive(20, counting(300.), counting(20.));
    assert_eq!(order, pairs(5) + &"c".repeat(15));
    let verdict = matched::verdict(&reference, &candidate, 0, 20);
    assert_eq!(verdict["verdict"], "win");
    assert_eq!(verdict["schedule"]["stop"], "early_win");
    assert_eq!(verdict["schedule"]["reference_samples"], 5);
    assert_eq!(verdict["schedule"]["candidate_samples"], 20);
    assert_eq!(verdict["reference"]["median_ms"], 302.);
    assert_eq!(verdict["reference"]["p95_ms"], 304.);
    assert_eq!(verdict["candidate"]["median_ms"], 30.);
    assert_eq!(verdict["candidate"]["p95_ms"], 38.);
    assert!((verdict["median_ratio"].as_f64().unwrap() - 30. / 302.).abs() < 1e-12);
    assert!(matched::gate(&[case("release", &reference, &candidate)], &["release"]).is_empty());
}

#[test]
fn a_separated_slower_case_stops_both_lanes_as_a_loss() {
    let (reference, candidate, order) = drive(20, counting(20.), counting(300.));
    assert_eq!(order, pairs(5));
    let slower = case("release", &reference, &candidate);
    assert_eq!(slower["verdict"], "loss");
    assert_eq!(slower["schedule"]["stop"], "early_loss");
    let failures = matched::gate(&[slower], &["release"]);
    assert_eq!(failures.len(), 1);
    assert!(failures[0].starts_with("release: loss"), "{failures:?}");
}

#[test]
fn overlapping_samples_take_every_pair_even_when_later_pairs_separate() {
    // One early overlap keeps the case from ever stopping: separation of more pairs would
    // include it.
    let candidate = |i: usize| if i == 0 { 400. } else { 20. + i as f64 };
    let (reference, candidate, order) = drive(20, counting(300.), candidate);
    assert_eq!(order, pairs(20));
    let verdict = matched::verdict(&reference, &candidate, 0, 20);
    assert_eq!(verdict["schedule"]["stop"], "full");
    assert_eq!(verdict["verdict"], "win");
}

#[test]
fn a_candidate_sample_alone_that_reaches_the_reference_sends_the_reference_back() {
    let candidate = |i: usize| if i == 9 { 300. } else { 20. + i as f64 };
    let (reference, candidate, order) = drive(20, counting(300.), candidate);
    // Five pairs, the candidate alone until its tenth sample equals the reference's
    // fastest, then alternation until the reference has twenty samples.
    assert_eq!(order, pairs(5) + "ccccc" + &pairs(15));
    assert_eq!(reference.samples_ms.len(), 20);
    assert_eq!(candidate.samples_ms.len(), 25);
    let verdict = matched::verdict(&reference, &candidate, 0, 20);
    assert_eq!(verdict["schedule"]["stop"], "resumed");
    assert!(verdict["reference_failure"].is_null() && verdict["candidate_failure"].is_null());
    assert_eq!(verdict["verdict"], "win");
}

#[test]
fn the_early_stop_decides_the_p95_as_strictly_as_the_median() {
    // Faster in the bulk, slower in the tail: the first five pairs are separated, so a
    // rule stopping both lanes there would call it a win. The candidate's own tail sends
    // the reference back, and the p95 decides the loss.
    let candidate = |i: usize| {
        if i == 12 || i == 16 {
            900.
        } else {
            50. + i as f64
        }
    };
    let (reference, candidate, order) = drive(20, counting(100.), candidate);
    assert!(candidate.samples_ms[..5].iter().all(|c| *c < 100.));
    assert!(order.starts_with(&(pairs(5) + &"c".repeat(8) + "RC")));
    let verdict = matched::verdict(&reference, &candidate, 0, 20);
    assert_eq!(verdict["schedule"]["stop"], "resumed");
    assert_eq!(verdict["verdict"], "loss");
    assert!(verdict["p95_ratio"].as_f64().unwrap() > 1.);
    assert!(verdict["median_ratio"].as_f64().unwrap() < 1.);
}

#[test]
fn an_early_win_fixes_the_p95_whatever_the_skipped_samples_measure() {
    let (reference, candidate, _) = drive(20, counting(300.), counting(20.));
    let (c_median, c_p95) = (
        matched::median(&candidate.samples_ms).unwrap(),
        matched::p95(&candidate.samples_ms).unwrap(),
    );
    // The fifteen samples the reference skipped, at their most damaging: as fast as
    // possible. The p95 comparison of twenty pairs cannot change; the median comparison
    // changes only once eleven of them are faster than the candidate's median.
    for zeros in 0..=15 {
        let mut full = reference.samples_ms.clone();
        full.extend(std::iter::repeat_n(0., zeros));
        full.extend(std::iter::repeat_n(f64::MAX, 15 - zeros));
        assert!(
            c_p95 < matched::p95(&full).unwrap(),
            "{zeros} fast skipped samples"
        );
        assert_eq!(
            c_median < matched::median(&full).unwrap(),
            zeros < 11,
            "{zeros} fast skipped samples"
        );
    }
}

#[test]
fn three_sample_plans_are_decided_exactly_after_two_pairs() {
    let values = [0., 1., 15., 25., 500., 1500., f64::MAX];
    let (reference, candidate, order) = drive(3, counting(1000.), counting(10.));
    assert_eq!(order, "RCRCc");
    assert_eq!(
        matched::verdict(&reference, &candidate, 0, 3)["verdict"],
        "win"
    );
    for skipped in values {
        let mut full = reference.samples_ms.clone();
        full.push(skipped);
        let c = &candidate.samples_ms;
        assert!(matched::median(c) < matched::median(&full), "{skipped}");
        assert!(matched::p95(c) < matched::p95(&full), "{skipped}");
    }
    let (reference, candidate, order) = drive(3, counting(10.), counting(1000.));
    assert_eq!(order, "RCRC");
    assert_eq!(
        matched::verdict(&reference, &candidate, 0, 3)["schedule"]["stop"],
        "early_loss"
    );
    for skipped_reference in values {
        for skipped_candidate in values {
            let mut r = reference.samples_ms.clone();
            r.push(skipped_reference);
            let mut c = candidate.samples_ms.clone();
            c.push(skipped_candidate);
            assert!(
                matched::median(&c) >= matched::median(&r),
                "{skipped_reference} {skipped_candidate}"
            );
        }
    }
}

/// Calls `visit` with every `chosen`-element subset of the ranks `0..of`, each in increasing
/// order.
fn subsets(of: usize, chosen: usize, visit: &mut impl FnMut(&[usize])) {
    fn walk(
        start: usize,
        of: usize,
        left: usize,
        picked: &mut Vec<usize>,
        visit: &mut impl FnMut(&[usize]),
    ) {
        if left == 0 {
            return visit(picked);
        }
        for rank in start..=of - left {
            picked.push(rank);
            walk(rank + 1, of, left - 1, picked, visit);
            picked.pop();
        }
    }
    walk(0, of, chosen, &mut Vec::new(), visit);
}

#[test]
fn a_tie_stops_early_only_at_the_stated_odds() {
    // With both versions equally fast, every assignment of ranks to samples is equally
    // likely. An early win needs the reference's five samples to outrank all twenty of the
    // candidate's: one assignment in C(25, 5) = 53,130.
    let mut wins = 0;
    let mut assignments = 0;
    subsets(25, 5, &mut |ranks| {
        let others: Vec<f64> = (0..25)
            .filter(|rank| !ranks.contains(rank))
            .map(|rank| rank as f64)
            .collect();
        let (reference, candidate, _) = drive(
            20,
            |i| ranks.get(i).map_or(100. + i as f64, |r| *r as f64),
            |i| others.get(i).copied().unwrap_or(100. + i as f64),
        );
        assignments += 1;
        wins += usize::from(
            matched::stop(20, &reference.samples_ms, &candidate.samples_ms) == Stop::EarlyWin,
        );
    });
    assert_eq!((wins, assignments), (1, 53_130));
    // An early loss needs the candidate's five early samples to outrank the reference's
    // five: one assignment in C(10, 5) = 252.
    let mut losses = 0;
    let mut assignments = 0;
    subsets(10, 5, &mut |ranks| {
        let others: Vec<f64> = (0..10)
            .filter(|rank| !ranks.contains(rank))
            .map(|rank| rank as f64)
            .collect();
        let (reference, candidate, _) = drive(
            20,
            |i| ranks.get(i).map_or(50. + i as f64, |r| *r as f64),
            |i| others.get(i).copied().unwrap_or(50. + i as f64),
        );
        assignments += 1;
        losses += usize::from(
            matched::stop(20, &reference.samples_ms, &candidate.samples_ms) == Stop::EarlyLoss,
        );
    });
    assert_eq!((losses, assignments), (1, 252));
}

#[test]
fn a_faster_p95_cannot_hide_a_tied_or_slower_median() {
    let (r, c, order) = drive(20, counting(100.), counting(100.));
    assert_eq!(order, pairs(20));
    assert_eq!(matched::verdict(&r, &c, 0, 20)["verdict"], "loss");
    // Its p95 (116) beats the reference's (118), its median (112) does not beat 110.
    let slower_median = [
        101., 110., 111., 112., 113., 110., 111., 112., 113., 114., 110., 111., 112., 113., 114.,
        115., 116., 117., 102., 103.,
    ];
    let (r, c, order) = drive(20, counting(100.), |i| slower_median[i]);
    assert_eq!(order, pairs(20));
    let verdict = matched::verdict(&r, &c, 0, 20);
    assert_eq!(verdict["candidate"]["p95_ms"], 116.);
    assert_eq!(verdict["candidate"]["median_ms"], 112.);
    assert_eq!(verdict["verdict"], "loss");
}

#[test]
fn a_crashed_or_misscheduled_reference_is_never_a_win() {
    let (reference, fast, _) = drive(20, counting(1000.), counting(1.));
    let mut crashed = reference.clone();
    crashed.exit_codes[3] = 2;
    let mut short = reference.clone();
    short.samples_ms.pop();
    short.exit_codes.pop();
    short.verified.pop();
    for reference in [crashed, short] {
        let value = case("release", &reference, &fast);
        assert_eq!(value["verdict"], "reference_failure");
        assert!(value["reference_failure"].is_string());
        let failures = matched::gate(&[value], &["release"]);
        assert!(failures[0].contains("reference_failure"), "{failures:?}");
    }
}

#[test]
fn a_degraded_reference_answer_is_only_a_lower_bound() {
    // 0.4.9 answered at its expected exit without a provable result: the candidate
    // wins only by being verified and faster than that bound at median and p95.
    let (reference, fast, _) = drive(20, counting(1000.), counting(1.));
    let mut degraded = reference.clone();
    degraded.verified[0] = false;
    let value = case("first_hybrid", &degraded, &fast);
    assert_eq!(value["verdict"], "win_over_degraded_reference");
    assert!(value["reference_failure"].is_string());
    assert!(matched::gate(&[value], &["first_hybrid"]).is_empty());
    let (fast_reference, slow, _) = drive(20, counting(10.), counting(500.));
    let mut fast_degraded = fast_reference.clone();
    fast_degraded.verified[0] = false;
    assert_eq!(
        case("first_hybrid", &fast_degraded, &slow)["verdict"],
        "reference_failure"
    );
    let (_, slow_tail, _) = drive(20, counting(1000.), |i| if i == 19 { 5000. } else { 1. });
    assert_eq!(
        case("first_hybrid", &degraded, &slow_tail)["verdict"],
        "reference_failure"
    );
}

#[test]
fn a_candidate_failure_fails_whatever_the_reference_did() {
    let (reference, fast, _) = drive(20, counting(500.), counting(1.));
    let mut candidate = fast.clone();
    candidate.exit_codes[0] = 1;
    assert_eq!(
        case("release", &reference, &candidate)["verdict"],
        "candidate_failure"
    );
    let mut unverified = reference.clone();
    unverified.verified[0] = false;
    assert_eq!(
        case("release", &unverified, &candidate)["verdict"],
        "both_failed"
    );
    let (reference, invalid, order) = drive(20, counting(500.), |i| {
        if i == 4 {
            f64::NAN
        } else {
            1. + i as f64
        }
    });
    assert_eq!(order, pairs(20));
    assert_eq!(
        case("release", &reference, &invalid)["verdict"],
        "candidate_failure"
    );
}

#[test]
fn lanes_must_hold_exactly_the_samples_their_schedule_asked_for() {
    // Twenty reference samples where the schedule stopped the reference at five.
    let (_, candidate, _) = drive(20, counting(300.), counting(20.));
    let mut extra = Lane::default();
    for ms in (0..20).map(|i| 300. + i as f64) {
        extra.record(ms, 0, true);
    }
    let value = case("release", &extra, &candidate);
    assert_eq!(value["verdict"], "reference_failure");
    assert_eq!(value["reference_failure"], "15 samples beyond the schedule");
    // A candidate that stopped short of completing its samples alone.
    let (reference, mut short, _) = drive(20, counting(300.), counting(20.));
    short.samples_ms.truncate(19);
    short.exit_codes.truncate(19);
    short.verified.truncate(19);
    let value = case("release", &reference, &short);
    assert_eq!(value["verdict"], "candidate_failure");
    assert_eq!(
        value["candidate_failure"],
        "19 samples; the schedule needs more"
    );
    // Overlapping lanes need all twenty pairs.
    let (mut reference, candidate, _) = drive(20, counting(100.), counting(100.));
    reference.samples_ms.truncate(19);
    reference.exit_codes.truncate(19);
    reference.verified.truncate(19);
    assert_eq!(
        case("release", &reference, &candidate)["verdict"],
        "reference_failure"
    );
}

#[test]
fn expected_misses_count_only_as_misses() {
    let miss = |reference_exit, candidate_exit| {
        let (mut r, mut c, _) = drive(20, counting(5.), counting(5.));
        r.exit_codes.fill(reference_exit);
        c.exit_codes.fill(candidate_exit);
        (r, c)
    };
    let (r, c) = miss(1, 1);
    assert_eq!(matched::verdict(&r, &c, 1, 20)["verdict"], "loss");
    let (r, c) = miss(2, 1);
    assert_eq!(
        matched::verdict(&r, &c, 1, 20)["verdict"],
        "reference_failure"
    );
    let (r, c) = miss(1, 0);
    assert_eq!(
        matched::verdict(&r, &c, 1, 20)["verdict"],
        "candidate_failure"
    );
}

#[test]
fn both_lanes_results_are_verified_by_the_same_predicates() {
    let hits = results::evidence(b"session\tpi\tdemo\t2\tuser\trelease workflow\n", b"");
    let empty = results::evidence(b"", b"keyword: 0 matching rows\n");
    let none: &[String] = &[];
    let any = Expect::default();
    assert!(matched::sample_verified("release", none, &hits, None, &any));
    assert!(!matched::sample_verified(
        "release", none, &empty, None, &any
    ));
    let checked = |lane: &mut Lane, evidence: &Value| {
        lane.verified.fill(matched::sample_verified(
            "release", none, evidence, None, &any,
        ));
    };
    let (mut slow_hits, mut fast_empty, _) = drive(20, counting(500.), counting(10.));
    checked(&mut slow_hits, &hits);
    checked(&mut fast_empty, &empty);
    assert_eq!(
        matched::verdict(&slow_hits, &fast_empty, 0, 20)["verdict"],
        "candidate_failure"
    );
    let (mut fast_empty, mut slow_hits, _) = drive(20, counting(10.), counting(500.));
    checked(&mut slow_hits, &hits);
    checked(&mut fast_empty, &empty);
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
        None,
        &any
    ));
    assert!(!matched::sample_verified(
        "first_hybrid",
        none,
        &keyword,
        None,
        &any
    ));
}

#[test]
fn the_dense_oracle_binds_the_candidate_to_exact_answers() {
    let oracle = vec!["@a:1.aa".to_string(), "@b:2.bb".to_string()];
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
    let any = Expect::default();
    assert!(matched::sample_verified(
        "old_dense",
        &json,
        &rows,
        Some(&oracle),
        &any
    ));
    assert!(!matched::sample_verified(
        "old_dense",
        &json,
        &reordered,
        Some(&oracle),
        &any
    ));
    assert!(matched::sample_verified(
        "old_dense",
        &json,
        &reordered,
        None,
        &any
    ));
    // The exact count binds both lanes.
    let count = args(&["deepold", "-c"]);
    let seven = Expect {
        count: Some(7),
        ..Expect::default()
    };
    for oracle in [Some(&oracle[..]), None] {
        let verified = |stdout: &[u8]| {
            let evidence = results::evidence(stdout, b"");
            matched::sample_verified("old_dense_count", &count, &evidence, oracle, &seven)
        };
        assert!(verified(b"7\n"));
        assert!(!verified(b"8\n"));
    }
}

#[test]
fn a_no_op_candidate_fails_every_case_that_proves_work() {
    // 0.4.9's answers on a 2000-row corpus (real output), and what a candidate that does
    // nothing shows: no output, or a report of work it never committed.
    let none: &[String] = &[];
    let built = Expect {
        corpus: Some((378, 11)),
        ..Expect::default()
    };
    let refreshed = Expect {
        corpus: Some((379, 11)),
        ..Expect::default()
    };
    let report = |stdout: &str| results::evidence(stdout.as_bytes(), b"");
    let first = report("=== indexing transcripts ===\n  [pi] 10 parsed files: 3420 seen -> 200 rows\n  indexed 378 messages across 11 sessions -> /d/messages.jsonl (70ms)\n");
    let unchanged = report("=== indexing transcripts ===\n  unchanged since last index (378 messages); skipped ingest + writes (36ms)\n  phases: source-check 10ms\n");
    let mut refresh = report("=== indexing transcripts ===\n  indexed 379 messages across 11 sessions -> /d/messages.jsonl (9ms)\n");
    let no_op = report("");
    let mut committed_nothing = unchanged.clone();
    committed_nothing["bench_probe_count"] = json!(0);
    refresh["bench_probe_count"] = json!(1);
    let mut stale = refresh.clone();
    stale["bench_probe_count"] = json!(0);
    let census = report(r#"{"changed_sources":0,"census_ms":1.3,"stat_files":11}"#);
    let missed = report(r#"{"changed_sources":0,"census_ms":0.1,"stat_files":0}"#);
    let count = Expect {
        count: Some(1260),
        ..Expect::default()
    };
    let planted = Expect {
        top: Some("@bench-10:21.6b71".into()),
        ..Expect::default()
    };
    let answer = report("@bench-10:21.6b71 pi/user project-0 0m ~semantic (meaning match) The cobalt lantern project's migration checklist\n@bench-3:9.c0de pi/user project-3 0m ~all …release…\n");
    let buried = report("@bench-3:9.c0de pi/user project-3 0m ~semantic (meaning match) …release…\n@bench-10:21.6b71 pi/user project-0 0m ~semantic (meaning match) The cobalt lantern\n");
    for (name, reference, failures, expect) in [
        ("first_index", &first, vec![&no_op, &unchanged], &built),
        ("no_change_index", &unchanged, vec![&no_op], &built),
        (
            "one_session_refresh",
            &refresh,
            vec![&no_op, &stale, &committed_nothing],
            &refreshed,
        ),
        ("no_change_census", &census, vec![&no_op, &missed], &built),
        (
            "count",
            &report("1260\n"),
            vec![&no_op, &report("1259\n")],
            &count,
        ),
        ("warm_hybrid", &answer, vec![&no_op, &buried], &planted),
    ] {
        assert!(
            matched::sample_verified(name, none, reference, None, expect),
            "{name}"
        );
        for failure in failures {
            assert!(
                !matched::sample_verified(name, none, failure, None, expect),
                "{name}: {failure}"
            );
            let (reference_lane, mut fast, _) = drive(20, counting(500.), counting(1.));
            fast.verified.fill(false);
            let value = case(name, &reference_lane, &fast);
            assert_eq!(value["verdict"], "candidate_failure", "{name}");
            assert!(!matched::gate(&[value], &[name]).is_empty(), "{name}");
        }
    }
    // A full build that reports an earlier corpus did not index the appended turn.
    assert!(!matched::sample_verified(
        "first_index",
        none,
        &first,
        None,
        &refreshed
    ));
}

#[test]
fn the_corpus_oracles_hold_the_answers_of_0_4_9() {
    // 0.4.9 on the generated 2000-row corpus: "indexed 378 messages across 11 sessions",
    // `error -c` 1260 and `error -c --agent codex --since 14d` 122.
    assert_eq!(corpus::expected_corpus(2000, false), (378, 11));
    assert_eq!(corpus::expected_count(2000, "error", false), 1260);
    assert_eq!(corpus::expected_count(2000, "error", true), 122);
    let temp = tempfile::TempDir::new().unwrap();
    let run = |home: &std::path::Path, args: &[&str]| {
        let out = std::process::Command::new(support::binary())
            .env_clear()
            .env("PATH", std::env::var_os("PATH").unwrap_or_default())
            .env("HOME", home)
            .env("USERPROFILE", home)
            .env("AGREP_HOME", home)
            .env("AGREP_DATA_DIR", home.join("data"))
            .env("AGREP_PROFILE", "classic")
            .args(args)
            .output()
            .unwrap();
        results::evidence(&out.stdout, &out.stderr)
    };
    let mixed = temp.path().join("mixed");
    let (last, _) = corpus::generate(&mixed, 2000, false).unwrap();
    corpus::append_turn(&last, "semantic-fixture", corpus::SEMANTIC_FIXTURE).unwrap();
    let fixture = Expect {
        corpus: Some((379, 11)),
        top: Some(corpus::planted_handle(2000)),
        ..Expect::default()
    };
    let built = run(&mixed, &["index"]);
    assert!(
        results::valid_result("first_index", &built, &fixture),
        "{} messages, {} sessions",
        built["bench_index_messages"],
        built["bench_index_sessions"]
    );
    assert_eq!(run(&mixed, &["error", "-c"])["bench_count"], 1260);
    // Without a model a search is keyword-only: the planted turn alone holds the query's
    // content words, and it is the first result.
    let words: Vec<&str> = corpus::SEMANTIC_QUERY
        .split(' ')
        .filter(|w| w.len() > 3 && corpus::SEMANTIC_FIXTURE.contains(w))
        .collect();
    assert_eq!(words, ["cobalt", "lantern", "deployment", "checklist"]);
    let found = run(&mixed, &[&words.join(" "), "--json"]);
    assert_eq!(found["bench_result_rows"], 1);
    assert_eq!(found["bench_top_handle"], json!(fixture.top));
    let dense = temp.path().join("dense");
    corpus::generate(&dense, 1700, true).unwrap();
    assert_eq!(corpus::expected_corpus(1700, true), (1700, 10));
    run(&dense, &["index"]);
    let (handles, count) = corpus::dense_expected(1700, &["deepold", "-c"]).unwrap();
    assert_eq!(run(&dense, &["deepold", "-c"])["bench_count"], count);
    assert_eq!(
        run(&dense, &["deepold", "--json", "-n", "40"])["bench_handles"],
        json!(handles)
    );
}

#[test]
fn every_required_case_must_be_present() {
    let (reference, candidate, _) = drive(20, counting(300.), counting(20.));
    let won = case("release", &reference, &candidate);
    let failures = matched::gate(&[won], &["release", "first_index"]);
    assert_eq!(failures, vec!["first_index: missing case".to_string()]);
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

#[test]
fn an_index_run_reports_its_phases_as_timing_events() {
    let temp = tempfile::TempDir::new().unwrap();
    let home = temp.path().join("home");
    corpus::generate(&home, 400, false).unwrap();
    let index = |timing: bool| {
        let mut command = std::process::Command::new(support::binary());
        command
            .env_clear()
            .env("PATH", std::env::var_os("PATH").unwrap_or_default())
            .env("HOME", &home)
            .env("USERPROFILE", &home)
            .env("AGREP_HOME", &home)
            .env("AGREP_DATA_DIR", temp.path().join("data"))
            .arg("index");
        if timing {
            command.env("AGREP_QUERY_TIMING", "1");
        }
        let out = command.output().unwrap();
        assert!(out.status.success());
        results::evidence(&out.stdout, &out.stderr)
    };
    let built = index(true);
    assert_eq!(
        [&built["sources"], &built["parsed"]],
        [&json!(2), &json!(2)]
    );
    assert!(built["changed_rows"].as_u64().is_some_and(|n| n > 0));
    for key in ["census_ms", "parse_ms", "write_ms", "after_refresh_ms"] {
        assert!(built[key].is_number(), "{key}");
    }
    assert!(built["wait_writer_lock_ms"].is_number());
    assert!(built.get("skipped_lock").is_none());
    // Without the variable an index run prints no statistics and writes none.
    let quiet = index(false);
    assert!(quiet.get("census_ms").is_none());
    assert_eq!(quiet["bench_index_messages"], built["bench_index_messages"]);
    assert!(!temp.path().join("data/index-stats.json").exists());
}
