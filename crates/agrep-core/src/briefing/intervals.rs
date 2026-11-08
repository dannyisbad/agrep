//! Active-time intervals and their union.
use super::Event;
use crate::history::Row;

pub fn merge(mut intervals: Vec<(i64, i64)>) -> Vec<(i64, i64)> {
    intervals.sort_unstable();
    let mut out: Vec<(i64, i64)> = vec![];
    for (start, end) in intervals {
        if end <= start {
            continue;
        }
        if let Some(last) = out.last_mut() {
            if start <= last.1 {
                last.1 = last.1.max(end);
                continue;
            }
        }
        out.push((start, end));
    }
    out
}
pub fn span(intervals: &[(i64, i64)]) -> i64 {
    intervals.iter().map(|(s, e)| e - s).sum()
}
pub fn intervals(
    rows: &[Row],
    events: &[Event],
    cap: i64,
    since: i64,
    until: i64,
) -> Vec<(i64, i64)> {
    let mut points: Vec<_> = rows
        .iter()
        .filter(|r| r.who != "agent" && r.ts > 0)
        .map(|r| r.ts)
        .chain(
            events
                .iter()
                .filter(|e| e.name == "compacted")
                .map(|e| e.ts),
        )
        .collect();
    points.sort_unstable();
    points.dedup();
    let mut raw: Vec<_> = points
        .windows(2)
        .map(|w| (w[0], w[1].min(w[0].saturating_add(cap))))
        .collect();
    if let Some(&last) = points.last() {
        if let Some(end) = events
            .iter()
            .filter(|e| e.kind == "tool" || e.kind == "subagent_result")
            .map(|e| e.ts)
            .max()
        {
            raw.push((last, end.min(last.saturating_add(cap))));
        }
    }
    merge(
        raw.into_iter()
            .map(|(s, e)| (s.max(since), e.min(until)))
            .collect(),
    )
}
