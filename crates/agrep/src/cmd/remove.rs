use agrep_core::{data::DataRoot, integrations};
use anyhow::Result;
pub fn run() -> Result<i32> {
    let root = DataRoot::discover()?;
    root.validate(true)?;
    let removal = integrations::remove(&root)?;
    for skipped in &removal.skipped {
        agrep_core::outln!(
            "  left alone (agrep has no record of writing it): {}",
            agrep_core::render::safe(skipped)
        )?;
    }
    for error in &removal.errors {
        agrep_core::errln!("  ! {}", agrep_core::render::safe(error));
    }
    agrep_core::outln!(
        "{}",
        if removal.errors.is_empty() {
            "agrep integrations removed"
        } else {
            "agrep removal incomplete"
        }
    )?;
    agrep_core::outln!("index and archives kept at {}", root.path().display())?;
    Ok(if removal.errors.is_empty() { 0 } else { 1 })
}
