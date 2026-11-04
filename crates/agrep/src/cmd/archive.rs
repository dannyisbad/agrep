use agrep_core::{archive, data::DataRoot, render::safe};
use anyhow::{bail, Result};
use std::io::{self, IsTerminal, Write};
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
            println!(
                "archive auto-capture {}",
                if o.on { "enabled" } else { "disabled" }
            );
        }
        if o.off {
            let stored = archive::status(&root)?["stored_bytes"]
                .as_u64()
                .unwrap_or(0);
            if stored > 0 {
                println!("the copies already taken stay: {} in {dir}; `agrep archive --purge` deletes them", human(stored));
            }
        }
        if let Some(keep) = o.keep {
            println!(
                "archive keep = {} version(s) per source file",
                if keep == 0 {
                    "0 (never prune)".into()
                } else {
                    keep.to_string()
                }
            );
        }
        return Ok(0);
    }
    if o.status {
        // 0.4.9's sentences (py/archive.py:3222-3260): a measurement it lacks is omitted.
        let s = archive::status(&root)?;
        let stored = s["stored_bytes"].as_u64().unwrap_or(0);
        if s["enabled"] == false {
            println!("archive retention is off (`agrep archive --on` turns it on)");
            if stored > 0 {
                println!("earlier captures still stored: {} in {dir}; `agrep archive --purge` deletes them", human(stored));
            }
            return Ok(0);
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
                brief((chrono::Utc::now().timestamp_millis() - at) as f64 / 1000.)
            ),
            None => "never captured yet".into(),
        });
        let retention = match s["keep"].as_u64().unwrap_or(0) {
            0 => "keeps every version (never prunes)".to_string(),
            keep => format!("keeps the last {keep} version(s)"),
        };
        println!("archive {retention} per source file: {}", parts.join(", "));
        let failed = s["failures"].as_array().map_or(0, Vec::len);
        if failed > 0 {
            println!("archive partial: {failed} source path(s) failed");
            return Ok(1);
        }
        if s["last_capture"].is_null() {
            println!("archive freshness-unknown: no capture pass has completed");
            return Ok(1);
        }
        return Ok(0);
    }
    if o.purge {
        let status = archive::status(&root)?;
        if status["versions"] == 0 {
            println!("archive holds no captured copies; nothing to purge");
            return Ok(0);
        }
        let stored = human(status["stored_bytes"].as_u64().unwrap_or(0));
        if !o.yes {
            if !io::stdin().is_terminal() {
                println!("archive purge would delete {stored} of archived copies in {dir}; pass --yes to confirm");
                return Ok(1);
            }
            print!("delete {stored} of archived copies in {dir}, including copies of chats their agents already deleted? [y/N] ");
            io::stdout().flush()?;
            let mut answer = String::new();
            io::stdin().read_line(&mut answer)?;
            if !matches!(answer.trim().to_lowercase().as_str(), "y" | "yes") {
                println!("archive purge cancelled; nothing was deleted");
                return Ok(1);
            }
        }
        archive::purge(&root)?;
        println!("archive purged: {stored} of archived copies deleted from {dir}");
        if status["enabled"] == true {
            println!("retention is still on: the next capture pass re-archives the live stores (`agrep archive --off` stops that)");
        }
        return Ok(0);
    }
    if o.prune {
        let (count, bytes) = archive::prune(&root)?.expect("blocking archive lock");
        println!(
            "pruned {count} old version record(s), reclaimed {}",
            human(bytes)
        );
        return Ok(0);
    }
    let start = std::time::Instant::now();
    let s = archive::capture(&root, false)?;
    for line in &s.log {
        eprintln!("archive: {}", safe(line));
    }
    println!(
        "archive: {} files - {} unchanged, {} appended, {} full captures, {} new content in {:.1}s",
        s.files,
        s.unchanged,
        s.appended,
        s.full,
        human(s.bytes_stored),
        start.elapsed().as_secs_f64()
    );
    if s.failures.is_empty() {
        return Ok(0);
    }
    println!(
        "archive incomplete: {} source path(s) failed; successful captures were kept",
        s.failures.len()
    );
    for failure in s.failures.iter().take(10) {
        println!("  {}", safe(failure));
    }
    Ok(1)
}
