use agrep_core::{archive, data::DataRoot};
use anyhow::{bail, Result};
use std::path::PathBuf;
pub fn run(needle: String, to: Option<String>, force: bool) -> Result<i32> {
    if needle.trim().is_empty() {
        bail!("needle must not be blank")
    }
    if to.as_ref().is_some_and(|s| s.trim().is_empty()) {
        bail!("--to must not be blank")
    }
    // Re-collecting the components drops the `.` a relative `--to .` leaves after joining.
    let to = to.map(|s| {
        let p = PathBuf::from(&s);
        let absolute = if s == "~" {
            agrep_core::ingest::home()
        } else if let Some(tail) = s.strip_prefix("~/") {
            agrep_core::ingest::home().join(tail)
        } else if p.is_absolute() {
            p
        } else {
            std::env::current_dir().unwrap_or_default().join(p)
        };
        absolute.components().collect::<PathBuf>()
    });
    let root = DataRoot::discover()?;
    root.validate(true)?;
    let (_, errors) = archive::restore(&root, &needle, to.as_deref(), force)?;
    for e in &errors {
        agrep_core::outln!("{}", agrep_core::render::safe(e))?;
    }
    Ok(if errors.is_empty() { 0 } else { 1 })
}
