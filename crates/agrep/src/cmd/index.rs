use super::super::*;
use agrep_core::surface::line;
use std::time::Instant;

pub(crate) fn run(full: bool, with_semantic: bool) -> Result<i32> {
    let root = DataRoot::discover()?;
    root.validate(true)?;
    let _foreground = root.foreground()?;
    line("=== indexing transcripts ===")?;
    let first = !root.db().exists();
    let refresh = store::refresh(&root, true, full)?;
    let after = Instant::now();
    report(&root, &refresh, first)?;
    agrep_core::coverage::initialize(&root)?;
    semantic::initialize(&root)?;
    if refresh.changed_rows > 0 {
        semantic::mark_index_changed(&root)?;
    }
    if with_semantic {
        semantic::provision(&root)?;
        semantic::initialize(&root)?;
        super::work::after_index(&root, true)?;
        let coverage = semantic::coverage(&store::reader(&root)?)?;
        eprintln!("semantic {}/{} rows", coverage.embedded, coverage.total);
    } else {
        super::work::after_index(&root, false)?;
    }
    record(&root, &refresh, after)?;
    Ok(0)
}

/// 0.4.9's reindex stages (reindex.py:72-85, 239-334): the index report, then the
/// embedding stage, each with its seconds, then the whole run's.
pub(crate) fn reindex(full: bool) -> Result<i32> {
    let started = Instant::now();
    let root = DataRoot::discover()?;
    root.validate(true)?;
    let _foreground = root.foreground()?;
    line("\n=== ingest transcripts ===")?;
    let first = !root.db().exists();
    let refresh = store::reindex(&root)?;
    let after = Instant::now();
    report(&root, &refresh, first)?;
    line(&format!("  ({:.1}s)", started.elapsed().as_secs_f64()))?;
    if full {
        semantic::reset(&root)?;
    }
    agrep_core::coverage::initialize(&root)?;
    if refresh.changed_rows > 0 {
        semantic::mark_index_changed(&root)?;
    }
    semantic::initialize(&root)?;
    let rows = semantic::coverage(&store::reader(&root)?)?.total;
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
    let stage = Instant::now();
    super::work::after_index(&root, false)?;
    if embedding {
        line(&format!("  ({:.1}s)", stage.elapsed().as_secs_f64()))?;
    }
    record(&root, &refresh, after)?;
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

/// Writes the refresh's statistics to `index-stats.json` in the data root: its phases, how
/// long the whole command was blocked on each lock file, and the time it spent after the
/// refresh.
fn record(root: &DataRoot, refresh: &store::Refresh, after: Instant) -> Result<()> {
    let mut extra = serde_json::to_value(agrep_core::data::lock_waits())?;
    extra["after_refresh_ms"] = serde_json::json!(after.elapsed().as_secs_f64() * 1000.);
    let stats = agrep_core::surface::refresh_json(refresh, extra)?;
    agrep_core::safe_fs::write_atomic(&root.index_stats(), format!("{stats}\n").as_bytes(), true)
}
