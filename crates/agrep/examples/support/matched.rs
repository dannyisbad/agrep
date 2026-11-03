//! Verdicts for the matched comparison of agrep 0.4.9 (the reference) and 0.5 (the
//! candidate): every sample of both lanes is verified, and a case is a win only when the
//! candidate's median and p95 are both strictly below the reference's.
use serde_json::{json, Value};

/// One lane's timed samples in one case, in the order they were taken.
#[derive(Clone, Debug, Default)]
pub struct Lane {
    pub samples_ms: Vec<f64>,
    pub exit_codes: Vec<i32>,
    pub verified: Vec<bool>,
}

impl Lane {
    pub fn record(&mut self, ms: f64, exit: i32, verified: bool) {
        self.samples_ms.push(ms);
        self.exit_codes.push(exit);
        self.verified.push(verified);
    }
}

fn sorted(samples: &[f64]) -> Vec<f64> {
    let mut sorted = samples.to_vec();
    sorted.sort_by(f64::total_cmp);
    sorted
}

/// The upper median, the statistic the benchmark reports.
pub fn median(samples: &[f64]) -> Option<f64> {
    let sorted = sorted(samples);
    sorted.get(sorted.len() / 2).copied()
}

/// Nearest-rank p95: with twenty samples, the second-slowest.
pub fn p95(samples: &[f64]) -> Option<f64> {
    let sorted = sorted(samples);
    (!sorted.is_empty()).then(|| sorted[(sorted.len() * 95).div_ceil(100) - 1])
}

/// Whether one sample proves its case's answer: the benchmark's result predicates for
/// every lane, and for the candidate on the dense corpus the exact full-corpus oracle.
pub fn sample_verified(
    name: &str,
    args: &[String],
    evidence: &Value,
    oracle: Option<&(Vec<String>, usize)>,
) -> bool {
    match oracle {
        Some((_, count)) if args.iter().any(|a| a == "-c") => {
            evidence["bench_count"].as_u64() == Some(*count as u64)
        }
        Some((handles, _)) => evidence["bench_handles"] == json!(handles),
        None => super::results::valid_result(name, evidence),
    }
}

/// Why a lane's samples cannot stand as a measurement of the case, if they cannot.
pub fn lane_failure(lane: &Lane, expected_exit: i32, minimum: usize) -> Option<String> {
    let n = lane.samples_ms.len();
    if n < minimum {
        return Some(format!("{n} samples; {minimum} required"));
    }
    if lane.exit_codes.len() != n || lane.verified.len() != n {
        return Some("exit or result evidence missing for some samples".into());
    }
    if lane.samples_ms.iter().any(|ms| !ms.is_finite() || *ms < 0.) {
        return Some("invalid timing sample".into());
    }
    let exits: Vec<_> = lane
        .exit_codes
        .iter()
        .filter(|rc| **rc != expected_exit)
        .collect();
    if !exits.is_empty() {
        return Some(format!(
            "{} of {n} exits were not {expected_exit}: {exits:?}",
            exits.len()
        ));
    }
    let unverified = lane.verified.iter().filter(|ok| !**ok).count();
    if unverified > 0 {
        return Some(format!("{unverified} of {n} results unverified"));
    }
    None
}

/// The comparison of one case. Each lane must reach the minimum sample count and match the
/// other lane's count; a lane that fails cannot win or be beaten.
pub fn verdict(reference: &Lane, candidate: &Lane, expected_exit: i32, minimum: usize) -> Value {
    let reference_failure = lane_failure(
        reference,
        expected_exit,
        minimum.max(candidate.samples_ms.len()),
    );
    let candidate_failure = lane_failure(
        candidate,
        expected_exit,
        minimum.max(reference.samples_ms.len()),
    );
    let stats =
        |lane: &Lane| json!({"median_ms":median(&lane.samples_ms),"p95_ms":p95(&lane.samples_ms)});
    let ratio = |stat: fn(&[f64]) -> Option<f64>| match (
        stat(&candidate.samples_ms),
        stat(&reference.samples_ms),
    ) {
        (Some(c), Some(r)) if r > 0. => Some(c / r),
        _ => None,
    };
    let (median_ratio, p95_ratio) = (ratio(median), ratio(p95));
    let verdict = match (&reference_failure, &candidate_failure) {
        (Some(_), Some(_)) => "both_failed",
        (Some(_), None) => "reference_failure",
        (None, Some(_)) => "candidate_failure",
        (None, None) => {
            if median_ratio.is_some_and(|r| r < 1.) && p95_ratio.is_some_and(|r| r < 1.) {
                "win"
            } else {
                "loss"
            }
        }
    };
    json!({
        "verdict": verdict,
        "reference_failure": reference_failure,
        "candidate_failure": candidate_failure,
        "reference": stats(reference),
        "candidate": stats(candidate),
        "median_ratio": median_ratio,
        "p95_ratio": p95_ratio,
    })
}

/// Gate failures: every required case must be present, and every case must be a win.
pub fn gate(cases: &[Value], required: &[&str]) -> Vec<String> {
    let mut failures = Vec::new();
    for name in required {
        if !cases.iter().any(|c| c["name"] == *name) {
            failures.push(format!("{name}: missing case"));
        }
    }
    for case in cases {
        let name = case["name"].as_str().unwrap_or("<unnamed>");
        let verdict = case["verdict"].as_str().unwrap_or("missing verdict");
        if verdict != "win" {
            let detail = [
                case["reference_failure"]
                    .as_str()
                    .map(|r| format!("reference: {r}")),
                case["candidate_failure"]
                    .as_str()
                    .map(|c| format!("candidate: {c}")),
                case["median_ratio"].as_f64().map(|r| {
                    format!(
                        "median ratio {r:.3}, p95 ratio {:.3}",
                        case["p95_ratio"].as_f64().unwrap_or(f64::NAN)
                    )
                }),
            ]
            .into_iter()
            .flatten()
            .collect::<Vec<_>>()
            .join("; ");
            failures.push(format!("{name}: {verdict} ({detail})"));
        }
    }
    failures
}
