//! The one place timestamps become epoch-millis. Every adapter routes its `ts` here instead
//! of hand-rolling the parse, so a future timezone/format fix lands once.
//!
//! The entry points are format-shaped (a borrowed &str for the jsonl adapters, an f64 for
//! kimi's wire, a JSON value for the flexible cases) so no adapter pays a needless allocation
//! on its hot path; they share one epoch classifier so the numeric semantics can't drift.

/// RFC3339 -> epoch millis; 0 on failure/None. (claude/codex/antigravity string timestamps.)
pub fn rfc3339(s: Option<&str>) -> i64 {
    s.and_then(|s| chrono::DateTime::parse_from_rfc3339(s).ok())
        .map(|d| d.timestamp_millis())
        .unwrap_or(0)
}

/// A raw epoch number -> millis. A positive value below 1e11 is read as SECONDS (the largest
/// plausible seconds epoch is ~2e9; the smallest plausible millis epoch is ~1e12), everything
/// else as already-millis. Zero/negative pass through unscaled. This is the single classifier
/// the numeric entry points share.
pub fn epoch_number(n: f64) -> i64 {
    if n > 0.0 && n < 1e11 {
        (n * 1000.0) as i64
    } else {
        n as i64
    }
}

/// Epoch SECONDS (possibly fractional) -> millis, always scaled. Kimi's wire log records
/// `timestamp` as float seconds; always scaling (never classifying) preserves its
/// sub-second precision exactly.
pub fn epoch_secs_f64(secs: f64) -> i64 {
    (secs * 1000.0) as i64
}

/// A JSON timestamp of unknown shape -> millis: an RFC3339 string, a numeric string, or an
/// epoch number auto-classified by `epoch_number`. The canonical entry a new adapter should
/// reach for; also the antigravity mailbox, whose `timestamp` is a string OR a seconds/millis
/// number.
pub fn json(v: &serde_json::Value) -> i64 {
    match v {
        serde_json::Value::String(s) => {
            let r = rfc3339(Some(s));
            if r != 0 {
                return r;
            }
            s.trim().parse::<f64>().map(epoch_number).unwrap_or(0)
        }
        serde_json::Value::Number(n) => n.as_f64().map(epoch_number).unwrap_or(0),
        _ => 0,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rfc3339_and_none() {
        assert_eq!(rfc3339(Some("2026-01-02T10:00:00.000Z")), 1767348000000);
        assert_eq!(rfc3339(None), 0);
        assert_eq!(rfc3339(Some("not a date")), 0);
    }

    #[test]
    fn seconds_scale_millis_passthrough() {
        assert_eq!(epoch_number(1767348000.0), 1767348000000); // seconds -> millis
        assert_eq!(epoch_number(1767348000000.0), 1767348000000); // already millis
        assert_eq!(epoch_number(0.0), 0);
        assert_eq!(epoch_secs_f64(1767348000.5), 1767348000500); // fractional secs kept
    }

    #[test]
    fn json_shapes() {
        assert_eq!(
            json(&serde_json::json!("2026-01-02T10:00:00.000Z")),
            1767348000000
        );
        assert_eq!(json(&serde_json::json!(1767348000i64)), 1767348000000);
        assert_eq!(json(&serde_json::json!(1767348000000i64)), 1767348000000);
        assert_eq!(json(&serde_json::json!(null)), 0);
    }
}
