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
pub mod serve;
pub mod set;
pub mod setup;
pub mod status;
pub mod summary;
pub mod tail;
pub mod ui;
pub mod usage;
pub mod why;
pub mod work;

pub(crate) fn paint(text: &str, color: &str) -> String {
    use std::io::IsTerminal;
    if color == "always" || (color == "auto" && std::io::stdout().is_terminal()) {
        format!("\x1b[36m{text}\x1b[0m")
    } else {
        text.into()
    }
}
