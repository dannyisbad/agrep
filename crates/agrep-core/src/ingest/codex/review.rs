//! Codex's review mode. A chat that runs a review hands a review thread a request Codex
//! composes, and the review's result returns to the chat as its reply. Nothing the user typed
//! precedes that result, so its row is labeled with the request: `[review] ` and the request's
//! first line, which titles a chat that holds only the review.

use serde::{Deserialize, Serialize};

/// What a review's row says without its request: Codex's review mode opened.
const REVIEW: &str = "[review]";

/// A review a chat started whose result has not arrived.
#[derive(Default, Serialize, Deserialize)]
pub(super) struct Review {
    /// The first line of the request the review thread received, else of the hint Codex showed
    /// when the review began.
    about: String,
    /// `about` is the request's.
    requested: bool,
}

impl Review {
    pub(super) fn entered(hint: Option<&str>) -> Self {
        Self {
            about: first_line(hint.unwrap_or_default()),
            requested: false,
        }
    }

    /// The review thread has not received its request yet.
    pub(super) fn awaits_request(&self) -> bool {
        !self.requested
    }

    /// The first request the review thread receives names the review.
    pub(super) fn requested(&mut self, request: &str) {
        if self.requested {
            return;
        }
        let line = first_line(request);
        if !line.is_empty() {
            self.about = line;
        }
        self.requested = true;
    }

    /// The text of the review's row.
    pub(super) fn label(&self) -> String {
        if self.about.is_empty() {
            REVIEW.to_owned()
        } else {
            format!("{REVIEW} {}", self.about)
        }
    }
}

fn first_line(text: &str) -> String {
    text.trim()
        .lines()
        .next()
        .unwrap_or_default()
        .trim()
        .to_owned()
}
