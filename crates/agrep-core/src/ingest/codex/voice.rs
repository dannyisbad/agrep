//! Codex voice sessions. Each spoken request reaches the chat as a `<realtime_delegation>`
//! holding the request Codex's realtime model took from the speech (`<input>`) and the
//! transcript since the delegation before (`<transcript_delta>`, lines `user: …` and
//! `assistant: …`); a flush ends the session with the transcript's tail and a note of Codex's
//! own instead of a request. The requests are rows. The rest of the transcript, the words no
//! request took and what the voice assistant said, is one more row once the flush arrives.

use serde::{Deserialize, Serialize};

use super::subagent::{delegated_input, unescape};

/// Who said a transcript line.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub(super) enum Speaker {
    User,
    Assistant,
}

/// One `<realtime_delegation>` message.
pub(super) struct Delegation {
    /// The request it hands Codex; `None` for a flush, whose input is Codex's own note.
    pub(super) request: Option<String>,
    /// It ends the session.
    pub(super) flush: bool,
    said: Vec<(Speaker, String)>,
}

impl Delegation {
    pub(super) fn parse(text: &str) -> Option<Self> {
        let text = text.trim_start();
        if !text.starts_with("<realtime_delegation") {
            return None;
        }
        let head = text.split_once("<input>").map_or(text, |(head, _)| head);
        let flush = head.contains("transcript_tail_flush");
        Some(Self {
            request: if flush { None } else { delegated_input(text) },
            flush,
            said: transcript(text),
        })
    }
}

/// The lines of a delegation's `<transcript_delta>` with their speakers. A line naming no
/// speaker continues the line before it.
fn transcript(text: &str) -> Vec<(Speaker, String)> {
    let Some((_, rest)) = text.split_once("<transcript_delta>") else {
        return Vec::new();
    };
    let body = rest
        .split_once("</transcript_delta>")
        .map_or(rest, |(body, _)| body);
    let mut said: Vec<(Speaker, String)> = Vec::new();
    for line in unescape(body).lines() {
        let spoken = [("user:", Speaker::User), ("assistant:", Speaker::Assistant)]
            .into_iter()
            .find_map(|(tag, speaker)| line.strip_prefix(tag).map(|words| (speaker, words)));
        match (spoken, said.last_mut()) {
            (Some((speaker, words)), _) => said.push((speaker, words.trim().to_owned())),
            (None, Some((_, last))) => {
                last.push('\n');
                last.push_str(line.trim());
            }
            (None, None) => said.push((Speaker::User, line.trim().to_owned())),
        }
    }
    said.retain(|(_, words)| !words.trim().is_empty());
    said
}

/// A voice session from its first delegation to its flush.
#[derive(Default, Serialize, Deserialize)]
pub(super) struct Transcript {
    /// The requests its delegations handed Codex, rows already.
    requests: Vec<String>,
    said: Vec<(Speaker, String)>,
}

impl Transcript {
    pub(super) fn heard(&mut self, delegation: Delegation) {
        self.requests.extend(delegation.request);
        self.said.extend(delegation.said);
    }

    /// What the session's transcript holds that its requests do not, each part once: the
    /// user's words and the assistant's.
    pub(super) fn unheard(self) -> (Vec<String>, Vec<String>) {
        let (mut user, mut assistant) = (Vec::new(), Vec::new());
        let (mut held, mut spoken) = (self.requests, Vec::new());
        for (speaker, line) in &self.said {
            let (held, parts) = match speaker {
                Speaker::User => (&mut held, &mut user),
                Speaker::Assistant => (&mut spoken, &mut assistant),
            };
            for part in uncovered(line, held) {
                held.push(part.to_owned());
                parts.push(part.to_owned());
            }
        }
        (user, assistant)
    }
}

/// The parts of `line` that no text in `held` holds. A held text holds the words of `line`
/// it has as a run: all of `line`, or all of the held text, or where one ends as the other
/// begins. A part keeps `line`'s own text from its first word to its last, and to the end of
/// `line` when no held word follows it.
fn uncovered<'a>(line: &'a str, held: &[String]) -> Vec<&'a str> {
    let line = line.trim();
    let spans = words(line);
    let ours: Vec<&str> = spans.iter().map(|&(from, to)| &line[from..to]).collect();
    let mut covered = vec![false; ours.len()];
    for text in held {
        let theirs: Vec<&str> = words(text)
            .into_iter()
            .map(|(from, to)| &text[from..to])
            .collect();
        cover(&ours, &theirs, &mut covered);
    }
    let mut parts = Vec::new();
    let mut at = 0;
    while at < ours.len() {
        if covered[at] {
            at += 1;
            continue;
        }
        let first = at;
        while at < ours.len() && !covered[at] {
            at += 1;
        }
        let end = if at == ours.len() {
            line.len()
        } else {
            spans[at - 1].1
        };
        parts.push(&line[spans[first].0..end]);
    }
    parts
}

/// Marks the words of `line` that `held` has as a run (see `uncovered`).
fn cover(line: &[&str], held: &[&str], covered: &mut [bool]) {
    if line.is_empty() || held.is_empty() {
        return;
    }
    if held.windows(line.len()).any(|run| run == line) {
        covered.fill(true);
        return;
    }
    for from in 0..line.len().saturating_sub(held.len()) + 1 {
        if line[from..].starts_with(held) {
            covered[from..from + held.len()].fill(true);
        }
    }
    let most = line.len().min(held.len());
    if let Some(n) = (1..=most)
        .rev()
        .find(|&n| line[..n] == held[held.len() - n..])
    {
        covered[..n].fill(true);
    }
    if let Some(n) = (1..=most)
        .rev()
        .find(|&n| line[line.len() - n..] == held[..n])
    {
        covered[line.len() - n..].fill(true);
    }
}

/// The byte spans of `text`'s words: runs of letters, digits and apostrophes.
fn words(text: &str) -> Vec<(usize, usize)> {
    let mut spans = Vec::new();
    let mut start = None;
    for (at, c) in text.char_indices() {
        let in_word = c.is_alphanumeric() || c == '\'' || c == '\u{2019}';
        match (in_word, start) {
            (true, None) => start = Some(at),
            (false, Some(from)) => {
                spans.push((from, at));
                start = None;
            }
            _ => {}
        }
    }
    if let Some(from) = start {
        spans.push((from, text.len()));
    }
    spans
}

#[cfg(test)]
mod tests {
    use super::*;

    fn held(texts: &[&str]) -> Vec<String> {
        texts.iter().map(|t| t.to_string()).collect()
    }

    #[test]
    fn a_line_keeps_only_the_words_no_held_text_has_as_a_run() {
        let requests = held(&[
            "please book a table for two",
            "at seven or is eight better",
            "then call the restaurant back",
        ]);
        // All of the line inside one request, and a request inside the line with another
        // starting where the line ends.
        assert!(uncovered(", or is eight better", &requests).is_empty());
        assert!(uncovered("please book a table for two at seven or", &requests).is_empty());
        // Words before a request that the line ends with stay, in the line's own spelling.
        assert_eq!(
            uncovered("wait, no, hold on. then call", &requests),
            ["wait, no, hold on"]
        );
        assert_eq!(uncovered("Right, sure.", &requests), ["Right, sure."]);
        // Words a held text has only apart, not as a run, are not held.
        assert_eq!(uncovered("table seven", &requests), ["table seven"]);
        assert_eq!(uncovered("  ", &requests), Vec::<&str>::new());
    }

    #[test]
    fn a_session_keeps_each_unheard_part_once_by_speaker() {
        let mut session = Transcript::default();
        for text in [
            "<realtime_delegation>\n  <input>then call the restaurant back</input>\n  <transcript_delta>assistant:  Let me check that.\nuser:  Right, sure\nassistant:  Let me check that. They open at five.\nuser:  wait no hold on then call\nuser: then call the restaurant back</transcript_delta>\n</realtime_delegation>",
            "<realtime_delegation>\n  <input>and book for eight</input>\n  <transcript_delta>user:  Right, sure wait no hold on then call the restaurant back\nuser: and book for eight</transcript_delta>\n</realtime_delegation>",
            "<realtime_delegation>\n  <source>transcript_tail_flush</source>\n  <input>The user just ended their realtime session.</input>\n  <transcript_delta>assistant:  Let’s\nuser:  That's all, it's fine</transcript_delta>\n</realtime_delegation>",
        ] {
            let delegation = Delegation::parse(text).unwrap();
            session.heard(delegation);
        }
        let (user, assistant) = session.unheard();
        assert_eq!(
            user,
            ["Right, sure", "wait no hold on", "That's all, it's fine"]
        );
        assert_eq!(
            assistant,
            ["Let me check that.", "They open at five.", "Let’s"]
        );
    }

    #[test]
    fn a_flush_hands_no_request() {
        let flush = Delegation::parse("<realtime_delegation>\n  <source>transcript_tail_flush</source>\n  <input>The user just ended their realtime session.</input>\n</realtime_delegation>").unwrap();
        assert!(flush.flush && flush.request.is_none() && flush.said.is_empty());
        let ask = Delegation::parse("<realtime_delegation>\n  <input>is a bagel &amp; tea ok</input>\n  <transcript_delta>user: is a bagel &amp; tea ok\nand milk</transcript_delta>\n</realtime_delegation>").unwrap();
        assert_eq!(
            (ask.flush, ask.request.as_deref()),
            (false, Some("is a bagel & tea ok"))
        );
        assert_eq!(
            ask.said,
            [(Speaker::User, "is a bagel & tea ok\nand milk".to_owned())]
        );
        assert!(Delegation::parse("<codex_delegation><input>x</input>").is_none());
    }
}
