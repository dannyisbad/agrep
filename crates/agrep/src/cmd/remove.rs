use agrep_core::{data::DataRoot, integrations};
use anyhow::Result;
pub fn run() -> Result<i32> {
    let root = DataRoot::discover()?;
    root.validate(true)?;
    let errors = integrations::remove(&root)?;
    for error in &errors {
        eprintln!("  ! {}", agrep_core::render::safe(error));
    }
    println!(
        "{}",
        if errors.is_empty() {
            "agrep integrations removed"
        } else {
            "agrep removal incomplete"
        }
    );
    println!("index and archives kept at {}", root.path().display());
    Ok(if errors.is_empty() { 0 } else { 1 })
}
