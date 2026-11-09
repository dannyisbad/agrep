//! The instant that ranking recency and displayed ages are measured from.
//!
//! Page expiry, locks, logs, archive scheduling, liveness and freshness stamps read the
//! system clock directly: they measure real elapsed time, which a pinned instant would stop.
use std::sync::OnceLock;

/// Milliseconds since the Unix epoch: `AGREP_NOW_MS` when it holds an integer, so that
/// separate runs rank and age rows against one instant, and the system clock otherwise.
pub fn now_ms() -> i64 {
    static PINNED: OnceLock<Option<i64>> = OnceLock::new();
    PINNED
        .get_or_init(|| std::env::var("AGREP_NOW_MS").ok()?.parse().ok())
        .unwrap_or_else(|| chrono::Utc::now().timestamp_millis())
}
