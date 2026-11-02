use agrep_core::{archive, data::DataRoot};
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
pub fn run(o: Options) -> Result<i32> {
    if o.yes && !o.purge {
        bail!("--yes only confirms --purge")
    }
    let root = DataRoot::discover()?;
    root.validate(true)?;
    if o.on || o.off || o.keep.is_some() {
        let _lock = root.lock(true)?;
        let mut c = root.config()?;
        if o.on || o.off {
            c.archive.enabled = o.on;
            println!(
                "archive auto-capture {}",
                if o.on { "enabled" } else { "disabled" }
            );
        }
        if let Some(keep) = o.keep {
            c.archive.keep = keep;
            println!(
                "archive keep = {} version(s) per source file",
                if keep == 0 {
                    "0 (never prune)".into()
                } else {
                    keep.to_string()
                }
            );
        }
        root.save_config(&c)?;
        return Ok(0);
    }
    if o.status {
        let s = archive::status(&root)?;
        if s["enabled"] == false {
            println!("archive retention is off (`agrep archive --on` turns it on)");
        }
        println!(
            "archive: {} files, {} versions, {} bytes stored; keep {} per source file",
            s["files"], s["versions"], s["stored_bytes"], s["keep"]
        );
        println!("data: {}", root.path().join("archive").display());
        if let Some(failures) = s["failures"].as_array() {
            for failure in failures {
                println!(
                    "  ! {}",
                    agrep_core::render::safe(failure.as_str().unwrap_or_default())
                );
            }
            if !failures.is_empty() {
                return Ok(1);
            }
        }
        return Ok(0);
    }
    if o.purge {
        let status = archive::status(&root)?;
        if status["versions"] == 0 {
            println!("archive holds no captured copies; nothing to purge");
            return Ok(0);
        }
        if !o.yes {
            if !io::stdin().is_terminal() {
                println!("archive purge would delete {} bytes of archived copies in {}; pass --yes to confirm", status["stored_bytes"], root.path().join("archive").display());
                return Ok(1);
            }
            print!("delete every archived copy? [y/N] ");
            io::stdout().flush()?;
            let mut answer = String::new();
            io::stdin().read_line(&mut answer)?;
            if !matches!(answer.trim().to_lowercase().as_str(), "y" | "yes") {
                println!("archive purge cancelled");
                return Ok(1);
            }
        }
        archive::purge(&root)?;
        println!("archive purged; on/off and keep settings preserved");
        return Ok(0);
    }
    if o.prune {
        let (count, bytes) = archive::prune(&root)?.expect("blocking archive lock");
        println!("pruned {count} old version record(s), reclaimed {bytes} bytes");
        return Ok(0);
    }
    let start = std::time::Instant::now();
    let s = archive::capture(&root, false)?;
    println!(
        "archive: {} files - {} unchanged, {} appended, {} full captures, {} bytes new content in {:.1}s",
        s.files, s.unchanged, s.appended, s.full, s.bytes_stored, start.elapsed().as_secs_f64()
    );
    for e in &s.failures {
        eprintln!("  ! {}", agrep_core::render::safe(e));
    }
    Ok(if s.failures.is_empty() { 0 } else { 1 })
}
