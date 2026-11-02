use crate::Recall;
use anyhow::Result;
pub(crate) fn run(a: Recall, no_auto: bool, color: &str) -> Result<i32> {
    super::recall::run(a, true, no_auto, color)
}
