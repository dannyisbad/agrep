//! Verdicts for the matched comparison of agrep 0.4.9 (the reference) and 0.5 (the
//! candidate): every sample of both lanes is verified, and a case is a win only when the
//! candidate's median and p95 are both strictly below the reference's.
//!
//! [`next`] decides which lane samples next. Samples alternate reference and candidate.
//! After the early pairs ([`early_pairs`]: five, or fewer when fewer samples are planned),
//! a case whose samples are completely separated stops early:
//! - every candidate sample slower than every reference sample: both lanes stop (a loss);
//! - every candidate sample faster: the reference stops and the candidate takes its
//!   remaining planned samples alone. The case ends there when all of them are still faster
//!   than every reference sample; a single one that is not sends the reference back, and
//!   the lanes alternate again until the reference has its planned samples.
//!
//! Otherwise both lanes take their planned samples. DISTRIBUTION.md states what an early
//! stop guarantees.
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

/// Which lane takes a case's next sample.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Next {
    Reference,
    /// `paired`: the sample follows the reference sample it alternates with; otherwise the
    /// reference has stopped and the candidate samples alone.
    Candidate {
        paired: bool,
    },
    Done,
}

/// How a case's sampling ended.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Stop {
    /// Every planned pair.
    Full,
    /// The early pairs were separated and every later candidate sample stayed faster than
    /// every reference sample.
    EarlyWin,
    /// Every candidate sample of the early pairs was slower than every reference sample.
    EarlyLoss,
    /// The early pairs were separated, then a candidate sample was not faster than every
    /// reference sample, and the reference resumed.
    Resumed,
}

impl Stop {
    pub fn name(self) -> &'static str {
        match self {
            Stop::Full => "full",
            Stop::EarlyWin => "early_win",
            Stop::EarlyLoss => "early_loss",
            Stop::Resumed => "resumed",
        }
    }
}

/// Pairs after which a completely separated case stops: five, or with fewer than ten
/// planned samples per lane the fewest whose separation fixes both verdicts whatever the
/// skipped reference samples would have measured.
pub fn early_pairs(planned: usize) -> usize {
    5.min(planned - planned / 2)
}

/// The early pairs' separation and, when they are separated in the candidate's favour,
/// the candidate's sample count at the first later sample that was not faster than every
/// reference sample of those pairs.
enum Shape {
    Overlapping,
    Slower,
    Faster { resumed_after: Option<usize> },
}

fn shape(early: usize, reference: &[f64], candidate: &[f64]) -> Shape {
    let (head, tail) = candidate.split_at(early);
    let reference = &reference[..early];
    let fastest = reference.iter().copied().fold(f64::INFINITY, f64::min);
    let slowest = reference.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    if head.iter().all(|ms| *ms > slowest) {
        Shape::Slower
    } else if head.iter().all(|ms| *ms < fastest) {
        Shape::Faster {
            resumed_after: tail
                .iter()
                .position(|ms| ms.partial_cmp(&fastest) != Some(std::cmp::Ordering::Less))
                .map(|at| early + at + 1),
        }
    } else {
        Shape::Overlapping
    }
}

/// The schedule: given every sample a case has taken, which lane samples next. Separation
/// after more pairs implies separation of the early pairs, so checking once is checking
/// after every pair.
pub fn next(planned: usize, reference: &[f64], candidate: &[f64]) -> Next {
    let early = early_pairs(planned);
    let (r, c) = (reference.len(), candidate.len());
    // Alternation: the candidate completes each pair, the reference opens the next one.
    let alternate = |reference_taken: usize, candidate_taken: usize| {
        if candidate_taken < reference_taken {
            Next::Candidate { paired: true }
        } else if r < planned {
            Next::Reference
        } else {
            Next::Done
        }
    };
    if r < early || c < early {
        return alternate(r, c);
    }
    match shape(early, reference, candidate) {
        Shape::Slower => Next::Done,
        Shape::Overlapping => alternate(r, c),
        Shape::Faster {
            resumed_after: None,
        } if c < planned => Next::Candidate { paired: false },
        Shape::Faster {
            resumed_after: None,
        } => Next::Done,
        Shape::Faster {
            resumed_after: Some(alone),
        } => alternate(r - early, c - alone),
    }
}

/// How a complete schedule over these samples ended.
pub fn stop(planned: usize, reference: &[f64], candidate: &[f64]) -> Stop {
    let early = early_pairs(planned);
    if reference.len() < early || candidate.len() < early {
        return Stop::Full;
    }
    match shape(early, reference, candidate) {
        Shape::Overlapping => Stop::Full,
        Shape::Slower => Stop::EarlyLoss,
        Shape::Faster {
            resumed_after: None,
        } => Stop::EarlyWin,
        Shape::Faster {
            resumed_after: Some(_),
        } => Stop::Resumed,
    }
}

/// Replays the schedule over the samples taken, in order. Each lane must hold exactly the
/// samples the schedule asked of it; the result says why a lane does not.
pub fn conformance(planned: usize, reference: &[f64], candidate: &[f64]) -> [Option<String>; 2] {
    let (mut r, mut c) = (0, 0);
    loop {
        match next(planned, &reference[..r], &candidate[..c]) {
            Next::Done => break,
            Next::Reference if r < reference.len() => r += 1,
            Next::Candidate { .. } if c < candidate.len() => c += 1,
            Next::Reference => {
                return [Some(format!("{r} samples; the schedule needs more")), None]
            }
            Next::Candidate { .. } => {
                return [None, Some(format!("{c} samples; the schedule needs more"))]
            }
        }
    }
    let beyond = |taken: usize, used: usize| {
        (taken > used).then(|| format!("{} samples beyond the schedule", taken - used))
    };
    [beyond(reference.len(), r), beyond(candidate.len(), c)]
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
/// `schedule` is the lane's departure from the sampling schedule, from [`conformance`].
pub fn lane_failure(lane: &Lane, expected_exit: i32, schedule: Option<String>) -> Option<String> {
    let n = lane.samples_ms.len();
    if schedule.is_some() {
        return schedule;
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

/// A reference that reached its expected exit on every sample but could not prove a
/// correct answer (a degraded result, or an index that had not settled) spent at least
/// its measured time without one, so its samples bound its time to a correct answer
/// from below. A crash, a wrong exit code or broken evidence is not such a bound.
fn degraded_answer(lane: &Lane, expected_exit: i32, schedule: &Option<String>) -> bool {
    let n = lane.samples_ms.len();
    schedule.is_none()
        && n > 0
        && lane.exit_codes.len() == n
        && lane.verified.len() == n
        && lane.samples_ms.iter().all(|ms| ms.is_finite() && *ms >= 0.)
        && lane.exit_codes.iter().all(|rc| *rc == expected_exit)
        && lane.verified.iter().any(|ok| !*ok)
}

/// The comparison of one case planned at `planned` samples per lane. Each lane must hold
/// exactly the samples the schedule asked of it. A failed candidate never wins; a failed
/// reference is beaten only when its failure was a degraded answer (see
/// [`degraded_answer`]) and every candidate sample is verified and faster at both the
/// median and p95 of that lower bound.
pub fn verdict(reference: &Lane, candidate: &Lane, expected_exit: i32, planned: usize) -> Value {
    let [reference_schedule, candidate_schedule] =
        conformance(planned, &reference.samples_ms, &candidate.samples_ms);
    let degraded_reference = degraded_answer(reference, expected_exit, &reference_schedule);
    let reference_failure = lane_failure(reference, expected_exit, reference_schedule);
    let candidate_failure = lane_failure(candidate, expected_exit, candidate_schedule);
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
    let faster = median_ratio.is_some_and(|r| r < 1.) && p95_ratio.is_some_and(|r| r < 1.);
    let verdict = match (&reference_failure, &candidate_failure) {
        (Some(_), Some(_)) => "both_failed",
        (Some(_), None) if degraded_reference && faster => "win_over_degraded_reference",
        (Some(_), None) => "reference_failure",
        (None, Some(_)) => "candidate_failure",
        (None, None) if faster => "win",
        (None, None) => "loss",
    };
    json!({
        "verdict": verdict,
        "reference_failure": reference_failure,
        "candidate_failure": candidate_failure,
        "reference": stats(reference),
        "candidate": stats(candidate),
        "median_ratio": median_ratio,
        "p95_ratio": p95_ratio,
        "schedule": {
            "planned_samples": planned,
            "early_pairs": early_pairs(planned),
            "stop": stop(planned, &reference.samples_ms, &candidate.samples_ms).name(),
            "reference_samples": reference.samples_ms.len(),
            "candidate_samples": candidate.samples_ms.len(),
        },
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
        if !matches!(verdict, "win" | "win_over_degraded_reference") {
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
