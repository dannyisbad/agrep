pub mod archive;
pub mod around;
pub mod audit;
pub mod board;
pub mod chats;
pub mod doctor;
pub mod index;
pub mod pack;
pub mod postcompact;
pub mod recall;
pub mod remove;
pub mod restore;
pub mod resume;
pub mod run;
pub mod search;
pub mod set;
pub mod setup;
pub mod status;
pub mod summary;
pub mod tail;
pub mod usage;
pub mod why;
pub mod work;

/// Whether stdout gets colour under `--color` (auto, always or never) and the environment.
pub(crate) fn color_on(color: &str) -> bool {
    use std::io::IsTerminal;
    agrep_core::surface::color_enabled(color, std::io::stdout().is_terminal())
}
pub(crate) fn paint(text: &str, color: &str) -> String {
    if color_on(color) {
        format!("\x1b[36m{text}\x1b[0m")
    } else {
        text.into()
    }
}
