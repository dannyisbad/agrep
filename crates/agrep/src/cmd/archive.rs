use agrep_core::{archive, data::DataRoot, render::safe};
use anyhow::{bail, Result};
use std::io::{self, IsTerminal};
#[derive(Default)]
pub struct Options {
    pub on: bool,
    pub off: bool,
    pub status: bool,
    pub keep: Option<usize>,
    pub prune: bool,
    pub purge: bool,
    pub yes: bool,
}
/// 0.4.9's `_human_bytes` (py/archive.py:212-222): decimal units, one decimal place.
fn human(bytes: u64) -> String {
    let mut size = bytes as f64;
    let mut unit = "B";
    for larger in ["KB", "MB", "GB"] {
        if size < 999.95 {
            break;
        }
        size /= 1000.;
        unit = larger;
    }
    match unit {
        "B" => format!("{size:.0} B"),
        "GB" => format!("{size:.2} GB"),
        _ => format!("{size:.1} {unit}"),
    }
}
/// 0.4.9's `brief_duration` (py/surface_policy.py:1939-1947).
fn brief(seconds: f64) -> String {
    let seconds = seconds.max(0.);
    if seconds < 100. {
        format!("{seconds:.0}s")
    } else if seconds < 6000. {
        format!("{:.0}m", seconds / 60.)
    } else if seconds < 48. * 3600. {
        format!("{:.0}h", seconds / 3600.)
    } else {
        format!("{:.0}d", seconds / 86400.)
    }
}
pub fn run(o: Options) -> Result<i32> {
    if o.yes && !o.purge {
        bail!("--yes only confirms --purge")
    }
    let root = DataRoot::discover()?;
    root.validate(true)?;
    let dir = root.path().join("archive");
    let dir = safe(&dir.to_string_lossy());
    if o.on || o.off || o.keep.is_some() {
        let _lock = root.lock(true)?;
        let mut c = root.config()?;
        if o.on || o.off {
            c.archive.enabled = o.on;
        }
        if let Some(keep) = o.keep {
            c.archive.keep = keep;
        }
        root.save_config(&c)?;
        if o.on || o.off {
            agrep_core::outln!(
                "archive auto-capture {}",
                if o.on { "enabled" } else { "disabled" }
            )?;
        }
        if o.off {
            let status = archive::status(&root)?;
            let stored = status["stored_bytes"].as_u64().unwrap_or(0);
            if let Some(damaged) = status["catalog_damaged"].as_str() {
                agrep_core::outln!("{}; {}", safe(damaged), archive::PURGE)?;
            } else if stored > 0 {
                agrep_core::outln!("the copies already taken stay: {} in {dir}; `agrep archive --purge` deletes them", human(stored))?;
            }
        }
        if let Some(keep) = o.keep {
            agrep_core::outln!(
                "archive keep = {} version(s) per source file",
                if keep == 0 {
                    "0 (never prune)".into()
                } else {
                    keep.to_string()
                }
            )?;
        }
        return Ok(0);
    }
    if o.status {
        return status(&root, &dir);
    }
    if o.purge {
        let status = archive::status(&root)?;
        if status["versions"] == 0 {
            agrep_core::outln!("archive holds no captured copies; nothing to purge")?;
            return Ok(0);
        }
        let stored = human(status["stored_bytes"].as_u64().unwrap_or(0));
        if !o.yes {
            if !io::stdin().is_terminal() {
                agrep_core::outln!("archive purge would delete {stored} of archived copies in {dir}; pass --yes to confirm")?;
                return Ok(1);
            }
            let shown = agrep_core::surface::ask(
                &mut io::stdout(),
                format_args!("delete {stored} of archived copies in {dir}, including copies of chats their agents already deleted? [y/N] "),
            )?;
            let mut answer = String::new();
            if shown {
                io::stdin().read_line(&mut answer)?;
            }
            if !matches!(answer.trim().to_lowercase().as_str(), "y" | "yes") {
                agrep_core::outln!("archive purge cancelled; nothing was deleted")?;
                return Ok(1);
            }
        }
        archive::purge(&root)?;
        agrep_core::outln!("archive purged: {stored} of archived copies deleted from {dir}")?;
        if status["enabled"] == true {
            agrep_core::outln!("retention is still on: the next capture pass re-archives the live stores (`agrep archive --off` stops that)")?;
        }
        return Ok(0);
    }
    if o.prune {
        let (count, bytes) = archive::prune(&root)?.expect("blocking archive lock");
        agrep_core::outln!(
            "pruned {count} old version record(s), reclaimed {}",
            human(bytes)
        )?;
        return Ok(0);
    }
    let start = std::time::Instant::now();
    let s = archive::capture(&root, false, &mut |line| {
        agrep_core::errln!("archive: {}", safe(line));
    })?;
    let repaired = if s.repaired > 0 {
        format!(", {} damaged chunk(s) re-stored", s.repaired)
    } else {
        String::new()
    };
    agrep_core::outln!(
        "archive: {} files - {} unchanged, {} appended, {} full captures, {} new content{repaired} in {:.1}s",
        s.files,
        s.unchanged,
        s.appended,
        s.full,
        human(s.bytes_stored),
        start.elapsed().as_secs_f64()
    )?;
    let mut code = 0;
    if !s.failures.is_empty() {
        agrep_core::outln!(
            "archive incomplete: {} source path(s) failed; successful captures were kept",
            s.failures.len()
        )?;
        for failure in s.failures.iter().take(10) {
            agrep_core::outln!("  {}", safe(failure))?;
        }
        code = 1;
    }
    if s.damaged_versions > 0 {
        agrep_core::outln!(
            "archive damaged: {} version(s) of {} file(s) cannot be restored: their stored chunks are missing or corrupt and no live file holds that content any more",
            s.damaged_versions,
            s.damaged_paths.len()
        )?;
        for path in s.damaged_paths.iter().take(10) {
            agrep_core::outln!("  {}", safe(path))?;
        }
        code = 1;
    }
    Ok(code)
}

fn status(root: &DataRoot, dir: &str) -> Result<i32> {
    // 0.4.9's sentences (py/archive.py:3222-3260): a measurement it lacks is omitted.
    let s = archive::status(root)?;
    if let Some(damaged) = s["catalog_damaged"].as_str() {
        agrep_core::outln!("{}; {}", safe(damaged), archive::PURGE)?;
        return Ok(2);
    }
    let stored = s["stored_bytes"].as_u64().unwrap_or(0);
    if s["enabled"] == false {
        agrep_core::outln!("archive retention is off (`agrep archive --on` turns it on)")?;
        if stored > 0 {
            agrep_core::outln!(
                "earlier captures still stored: {} in {dir}; `agrep archive --purge` deletes them",
                human(stored)
            )?;
        }
        return damage(&s, dir).map(i32::from);
    }
    let mut parts = vec![
        format!(
            "{} files",
            agrep_core::surface::number(s["files"].as_u64().unwrap_or(0) as usize)
        ),
        format!("{} stored", human(stored)),
    ];
    if stored > 0 {
        let ratio = s["raw_bytes"].as_u64().unwrap_or(0) as f64 / stored as f64;
        // Python printed round(ratio, 2) as its shortest float.
        let mut ratio = format!("{ratio:.2}");
        while ratio.ends_with('0') && !ratio.ends_with(".0") {
            ratio.pop();
        }
        if ratio != "0.0" {
            parts.push(format!("{ratio}x smaller than the originals"));
        }
    }
    parts.push(match s["last_capture"].as_i64() {
        Some(at) => format!(
            "last capture {} ago",
            brief((agrep_core::clock::now_ms() - at) as f64 / 1000.)
        ),
        None => "never captured yet".into(),
    });
    let retention = match s["keep"].as_u64().unwrap_or(0) {
        0 => "keeps every version (never prunes)".to_string(),
        keep => format!("keeps the last {keep} version(s)"),
    };
    agrep_core::outln!("archive {retention} per source file: {}", parts.join(", "))?;
    let failed = s["failures"].as_array().map_or(0, Vec::len);
    if failed > 0 {
        agrep_core::outln!("archive partial: {failed} source path(s) failed")?;
    }
    if damage(&s, dir)? || failed > 0 {
        return Ok(1);
    }
    if s["last_capture"].is_null() {
        agrep_core::outln!("archive freshness-unknown: no capture pass has completed")?;
        return Ok(1);
    }
    Ok(0)
}
/// Reports versions whose stored chunks are missing or corrupt; whether there are any.
fn damage(s: &serde_json::Value, dir: &str) -> Result<bool> {
    let versions = s["damaged_versions"].as_u64().unwrap_or(0);
    if versions > 0 {
        agrep_core::outln!(
            "archive damaged: {versions} version(s) of {} file(s) cannot be restored: their stored chunks in {dir} are missing or corrupt; `agrep archive` re-stores those that live files still hold",
            s["damaged_files"].as_u64().unwrap_or(0)
        )?;
    }
    Ok(versions > 0)
}
