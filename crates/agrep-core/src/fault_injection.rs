//! Race injection for agrep-ingest's own tests. Compiled only under `owner-vanish-injection`,
//! which agrep-ingest's dev-dependency enables, so shipped binaries carry none of it.

/// `AGREP_TEST_OWNER_VANISH=<n>[,<n>...]`: during the listed 1-based evaluations of the derived
/// writer's daemon-owner fence, every owner record the scan listed reads as already vanished.
#[cfg(feature = "owner-vanish-injection")]
pub fn owner_vanish_evaluations() -> Vec<usize> {
    std::env::var("AGREP_TEST_OWNER_VANISH")
        .ok()
        .map(|plan| {
            plan.split(',')
                .filter_map(|ordinal| ordinal.trim().parse().ok())
                .collect()
        })
        .unwrap_or_default()
}

#[cfg(not(feature = "owner-vanish-injection"))]
pub fn owner_vanish_evaluations() -> Vec<usize> {
    Vec::new()
}

/// `AGREP_TEST_PARK=<dir>`: the process parks at each named step until the test allows it on.
/// Arriving at `step` creates `<dir>/<step>`; execution continues once `<dir>/<step>.go` exists
/// (or after 30 s), so a test can land a peer's activity inside the gap. Steps: an exact
/// owner-record removal's `mutex` (judged, before the directory mutex), `rename` and `restore`
/// (lock-less fallback); the ingest writer's `warm-lane` (unchanged sources, before its writes),
/// `event-proof` (event write committed, before its proof is published), `cursor-open` (after
/// the census, before the collector opens Cursor's database), `cursor-read` (that database
/// opened, before the collector reads it), and a consented rebuild's, each parked only while one is
/// recorded: `consent-armed` (undo record written), `consent-cached` (parse cache committed, before
/// the commit point), `consent-commit` (commit point recorded, before the event write),
/// `consent-promote` (before the first promotion), `consent-promoted-<n>` (after its n-th derived
/// file), `consent-proof` and `consent-signature` (before and after the derived proof),
/// `consent-signed` (signature written) and `consent-published` (before it clears its record);
/// and the postflight snapshot's `postflight-open-wal` ([`postflight_wal_gate`]).
#[cfg(feature = "owner-vanish-injection")]
pub fn park_gate(step: &str) {
    let Some(dir) = std::env::var_os("AGREP_TEST_PARK") else {
        return;
    };
    let dir = std::path::Path::new(&dir);
    let _ = std::fs::write(dir.join(step), b"");
    let allowed = dir.join(format!("{step}.go"));
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(30);
    while !allowed.exists() && std::time::Instant::now() < deadline {
        std::thread::sleep(std::time::Duration::from_millis(5));
    }
}

#[cfg(not(feature = "owner-vanish-injection"))]
pub fn park_gate(_step: &str) {}

#[cfg(feature = "owner-vanish-injection")]
static POSTFLIGHT: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

/// Runs `validate`, a pass's postflight source snapshot, as the phase [`postflight_wal_gate`]
/// parks in.
#[cfg(feature = "owner-vanish-injection")]
pub fn postflight<T>(validate: impl FnOnce() -> T) -> T {
    use std::sync::atomic::Ordering;
    POSTFLIGHT.store(true, Ordering::SeqCst);
    let result = validate();
    POSTFLIGHT.store(false, Ordering::SeqCst);
    result
}

#[cfg(not(feature = "owner-vanish-injection"))]
pub fn postflight<T>(validate: impl FnOnce() -> T) -> T {
    validate()
}

/// Step `postflight-open-wal`: a SQLite WAL the postflight snapshot has stat'ed, before it opens
/// it to read its stable identity, so a test can commit to it inside that gap.
#[cfg(feature = "owner-vanish-injection")]
pub fn postflight_wal_gate(path: &std::path::Path) {
    if POSTFLIGHT.load(std::sync::atomic::Ordering::SeqCst)
        && path.to_string_lossy().ends_with("-wal")
    {
        park_gate("postflight-open-wal");
    }
}

#[cfg(not(feature = "owner-vanish-injection"))]
pub fn postflight_wal_gate(_path: &std::path::Path) {}

/// `AGREP_TEST_NOW_NS=<ns since the epoch>`: the clock a pass judges a failed read's stamps
/// against, so a test can observe one file both inside and past its timestamp tick.
#[cfg(feature = "owner-vanish-injection")]
pub fn pinned_now_ns() -> Option<i64> {
    std::env::var("AGREP_TEST_NOW_NS").ok()?.trim().parse().ok()
}

#[cfg(not(feature = "owner-vanish-injection"))]
pub fn pinned_now_ns() -> Option<i64> {
    None
}
