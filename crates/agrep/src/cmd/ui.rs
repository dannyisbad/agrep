use anyhow::Result;
pub fn run(port: u16, no_auto: bool) -> Result<i32> {
    super::serve::start(port, no_auto, true)
}
