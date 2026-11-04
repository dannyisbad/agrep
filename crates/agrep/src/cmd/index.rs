use super::super::*;
use agrep_core::surface::line;
use std::time::Instant;

pub(crate) fn run(full: bool, with_semantic: bool) -> Result<i32> {
    let root = DataRoot::discover()?;
    root.validate(true)?;
    line("=== indexing transcripts ===")?;
    let first = !root.db().exists();
    let refresh = store::refresh(&root, true, full)?;
    let after = Instant::now();
    report(&root, &refresh, first)?;
    if refresh.changed_rows > 0 {
        semantic::mark_index_changed(&root)?;
    }
    if with_semantic {
        semantic::provision(&root)?;
        let drain = semantic::Drain {
            explicit: true,
            ..Default::default()
        };
        super::work::after_index(&root, Some(drain))?;
        let coverage = semantic::coverage(&store::reader(&root)?)?;
        eprintln!("semantic {}/{} rows", coverage.embedded, coverage.total);
    } else {
        super::work::after_index(&root, None)?;
    }
    record(&refresh, after)?;
    Ok(0)
}

/// 0.4.9's reindex stages (reindex.py:72-85, 239-334): the index report, then the
/// embedding stage, each with its seconds, then the whole run's. `max_new` caps the rows
/// that stage embeds, newest first (reindex.py:190-194, py/embed.py:3296-3298).
pub(crate) fn reindex(full: bool, max_new: Option<i64>) -> Result<i32> {
    if max_new.is_some_and(|n| n < 1) {
        return Err(super::usage::refusal(
            "agrep reindex",
            "--max-new must be at least 1",
        ));
    }
    let started = Instant::now();
    let root = DataRoot::discover()?;
    root.validate(true)?;
    line("\n=== ingest transcripts ===")?;
    let first = !root.db().exists();
    let refresh = store::reindex(&root)?;
    let after = Instant::now();
    report(&root, &refresh, first)?;
    line(&format!("  ({:.1}s)", started.elapsed().as_secs_f64()))?;
    if full {
        semantic::reset(&root)?;
    }
    if refresh.changed_rows > 0 {
        semantic::mark_index_changed(&root)?;
    }
    let coverage = semantic::coverage(&store::reader(&root)?)?;
    let rows = coverage.total;
    let embedding = if root.config()?.embeddings == "off" {
        line("\n=== semantic embeddings (disabled) ===")?;
        line("  skipped because embeddings=off; keyword search is ready.")?;
        false
    } else if rows == 0 {
        line("\n=== semantic embeddings (no messages) ===")?;
        line("  nothing to embed; keyword search is ready.")?;
        false
    } else if !semantic::installed(&root) {
        line("\n=== semantic embeddings (optional; not installed) ===")?;
        line("  skipped without launching a worker; keyword search is ready. Enable it with `agrep setup`.")?;
        false
    } else {
        line("\n=== embed messages ===")?;
        true
    };
    let limit = max_new.map(|n| n as usize);
    let pending = coverage.total.saturating_sub(coverage.embedded);
    if let Some(n) = limit.filter(|&n| embedding && pending > n) {
        eprintln!("capping at {n} newest of {pending} pending messages");
    }
    let stage = Instant::now();
    let drain = semantic::Drain {
        limit,
        ..Default::default()
    };
    super::work::after_index(&root, Some(drain))?;
    if embedding {
        line(&format!("  ({:.1}s)", stage.elapsed().as_secs_f64()))?;
    }
    record(&refresh, after)?;
    line(&format!(
        "\n  reindex complete in {:.0}s.",
        started.elapsed().as_secs_f64()
    ))?;
    Ok(0)
}

fn changed(refresh: &store::Refresh) -> bool {
    refresh.parsed > 0 || refresh.changed_rows > 0
}

/// 0.4.9's index report (crates/agrep-cli/src/main.rs:6245-6251, 7580-7615) with the lines
/// that describe 0.5's index: its message and session counts, the time the refresh took,
/// its phases, and the setup pointer while no agent is enrolled. An unchanged pass names
/// only its census, and ends there.
fn report(root: &DataRoot, refresh: &store::Refresh, first: bool) -> Result<()> {
    let c = store::reader(root)?;
    // 0.4.9 counted one message record per user-side row, its reply attached.
    let messages: i64 = c.query_row(
        "SELECT coalesce((SELECT CAST(value AS INTEGER) FROM state WHERE key='prose_user_count'),0)",
        [],
        |r| r.get(0),
    )?;
    let ms = |v: f64| format!("{v:.0}ms");
    if !changed(refresh) {
        line(&format!(
            "  unchanged since last index ({messages} messages); skipped ingest + writes ({})",
            ms(refresh.total_ms)
        ))?;
        return line(&format!("  phases: census {}", ms(refresh.census_ms)));
    }
    let sessions: i64 = c.query_row("SELECT count(*) FROM sessions", [], |r| r.get(0))?;
    line(&format!(
        "  indexed {messages} messages across {sessions} sessions ({})",
        ms(refresh.total_ms)
    ))?;
    let phases = [
        ("census", refresh.census_ms),
        ("parse", refresh.parse_ms),
        ("write", refresh.write_ms),
        ("bounds", refresh.bounds_ms),
        ("stats", refresh.stats_ms),
    ]
    .map(|(name, value)| format!("{name} {}", ms(value)));
    line(&format!("  phases: {}", phases.join(" · ")))?;
    if root.config()?.enrollment.is_empty() {
        line("  next: `agrep setup` - teach your agents to search this history")?;
    }
    // A first build announced itself before parsing (store::refresh).
    if !first {
        eprintln!("refreshing the search db …");
    }
    Ok(())
}

/// The refresh's statistics as an `AGREP_QUERY_TIMING` event: its phases and counts, and
/// the time the command spent after the refresh. Internal lock coordination stays out.
fn record(refresh: &store::Refresh, after: Instant) -> Result<()> {
    if agrep_core::semantic_profile::timing_enabled() {
        let mut stats = serde_json::to_value(refresh)?;
        if let Some(fields) = stats.as_object_mut() {
            fields.shift_remove("skipped_lock");
        }
        stats["after_refresh_ms"] = serde_json::json!(after.elapsed().as_secs_f64() * 1000.);
        agrep_core::semantic_profile::event(serde_json::json!({ "index": stats }));
    }
    Ok(())
}
