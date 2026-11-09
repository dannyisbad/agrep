//! The meaning lane's retained page: the best row of each session (or family), with the
//! copies other chats hold of a page row folded into it.
use crate::{
    forks::{Copy, Message},
    query::{self, Hit},
};
use anyhow::{bail, Result};
use std::{cmp::Ordering as Order, collections::HashSet};

/// The page order for `sort`, with a total tie-break.
fn order(sort: &str, a: &Hit, b: &Hit) -> Order {
    let order = match sort {
        "time" => b.ts.cmp(&a.ts).then(b.score.total_cmp(&a.score)),
        "position" => a
            .session
            .cmp(&b.session)
            .then(a.turn.cmp(&b.turn))
            .then(b.score.total_cmp(&a.score)),
        _ => b
            .score
            .total_cmp(&a.score)
            .then(b.ts.cmp(&a.ts))
            .then(a.session.cmp(&b.session))
            .then(a.turn.cmp(&b.turn)),
    };
    order
        .then(a.agent.cmp(&b.agent))
        .then(a.session.cmp(&b.session))
        .then(a.turn.cmp(&b.turn))
        .then(a.who.cmp(&b.who))
        .then(a.rowid.cmp(&b.rowid))
}

/// The retained page: the best row of each session (or family), one past the limit.
pub(super) struct Page<'a> {
    entries: Vec<Message>,
    pub(super) groups: HashSet<(String, String)>,
    want: usize,
    sort: &'a str,
    family_diverse: bool,
    /// Whether copies of a row in other chats fold into one entry; a chat listing keeps
    /// every chat.
    fold: bool,
}
impl<'a> Page<'a> {
    pub(super) fn new(want: usize, sort: &'a str, family_diverse: bool, fold: bool) -> Self {
        Self {
            entries: Vec::new(),
            groups: HashSet::new(),
            want,
            sort,
            family_diverse,
            fold,
        }
    }
    /// A full score-ordered page holds one row past the limit, the score to beat.
    pub(super) fn full(&self) -> bool {
        self.sort == "score" && self.entries.len() > self.want
    }
    pub(super) fn score_to_beat(&self) -> f64 {
        self.entries[self.want].shown().score
    }
    /// The page's rows, best first, at most `want`.
    pub(super) fn into_hits(self) -> Vec<Hit> {
        let want = self.want;
        self.entries
            .into_iter()
            .take(want)
            .map(Message::into_hit)
            .collect()
    }
    fn same_group(&self, old: &Hit, h: &Hit) -> bool {
        old.agent == h.agent
            && (old.session == h.session
                || (self.family_diverse && !h.family.is_empty() && old.family == h.family))
    }
    /// `last` is when `h`'s chat was last active.
    pub(super) fn offer(&mut self, mut h: Hit, last: i64) -> Result<()> {
        let group = (
            h.agent.clone(),
            if self.family_diverse && !h.family.is_empty() {
                h.family.clone()
            } else {
                h.session.clone()
            },
        );
        self.groups.insert(group);
        // Each session (or family) holds at most one copy anywhere on the page.
        let held = self.entries.iter().enumerate().find_map(|(at, entry)| {
            entry
                .copies
                .iter()
                .position(|copy| self.same_group(&copy.hit, &h))
                .map(|index| (at, index))
        });
        if let Some((at, index)) = held {
            if order(self.sort, &h, &self.entries[at].copies[index].hit).is_ge() {
                return Ok(());
            }
            self.entries[at].copies.remove(index);
            if self.entries[at].copies.is_empty() {
                self.entries.remove(at);
            }
        }
        let fold = self.fold;
        if let Some(at) = self
            .entries
            .iter()
            .position(|entry| fold && entry.holds_copy_of(&h))
        {
            decorate(&mut h)?;
            self.entries[at].add(Copy { hit: h, last });
        } else {
            if self.entries.len() > self.want
                && order(self.sort, &h, self.entries.last().unwrap().shown()).is_ge()
            {
                return Ok(());
            }
            decorate(&mut h)?;
            self.entries.push(Message::new(h, last));
        }
        let sort = self.sort;
        self.entries
            .sort_by(|a, b| order(sort, a.shown(), b.shown()));
        self.entries.truncate(self.want + 1);
        Ok(())
    }
}

/// A row's handle, and 0.4.9's meaning snippet: the text's first 240 characters with
/// whitespace runs (Python's, which counts \x1c-\x1f) folded to one space; compact pages
/// then narrow that around a keyword match.
fn decorate(h: &mut Hit) -> Result<()> {
    h.handle = format!(
        "{}:{}.{}",
        session_target(&h.session)?,
        h.turn,
        query::content_digest(&h.text)
    );
    h.snippet = h
        .text
        .split(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
        .filter(|word| !word.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
        .chars()
        .take(240)
        .collect();
    Ok(())
}

fn session_target(session: &str) -> Result<String> {
    if (3..=128).contains(&session.len())
        && session.as_bytes()[0].is_ascii_alphanumeric()
        && session
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
    {
        return Ok(format!("@{session}"));
    }
    const A: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let (mut token, mut bits, mut available) = (String::new(), 0u32, 0);
    for b in session.bytes() {
        bits = (bits << 8) | b as u32;
        available += 8;
        while available >= 6 {
            available -= 6;
            token.push(A[((bits >> available) & 63) as usize] as char);
        }
    }
    if available != 0 {
        token.push(A[((bits << (6 - available)) & 63) as usize] as char);
    }
    if token.is_empty() || token.len() > 1024 {
        bail!("session id cannot be encoded safely");
    }
    Ok(format!("@~{token}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    fn row(session: &str, text: &str, score: f64) -> Hit {
        Hit {
            session: session.into(),
            agent: "pi".into(),
            family: session.into(),
            who: "user".into(),
            ts: 1_000,
            text: text.into(),
            score,
            matched: "semantic".into(),
            ..Hit::default()
        }
    }
    fn shown(page: Page<'_>) -> Vec<(String, String, Vec<String>)> {
        page.into_hits()
            .into_iter()
            .map(|h| {
                let forks = h.forks.into_iter().map(|f| f.session).collect();
                (h.session, h.text, forks)
            })
            .collect()
    }
    /// A chat whose better row arrives after its copy was folded leaves the fold, which then
    /// shows its newest remaining copy; the chat keeps one row on the page.
    #[test]
    fn a_chat_leaves_a_fold_when_its_better_row_arrives() {
        let mut page = Page::new(5, "score", true, true);
        page.offer(row("old", "shared", 0.9), 1).unwrap();
        page.offer(row("new", "shared", 0.9), 3).unwrap();
        page.offer(row("mid", "shared", 0.9), 2).unwrap();
        page.offer(row("new", "own", 0.95), 3).unwrap();
        page.offer(row("other", "shared", 0.9), 0).unwrap();
        assert_eq!(page.groups.len(), 4);
        assert_eq!(
            shown(page),
            [
                ("new".into(), "own".into(), vec![]),
                (
                    "mid".into(),
                    "shared".into(),
                    vec!["old".into(), "other".into()]
                ),
            ]
        );
    }
    /// Equal words at another time, or with no time, are not copies of one message.
    #[test]
    fn only_copies_of_one_message_fold() {
        let mut page = Page::new(5, "score", true, true);
        page.offer(row("a", "yes", 0.9), 1).unwrap();
        page.offer(
            Hit {
                ts: 2_000,
                ..row("b", "yes", 0.9)
            },
            2,
        )
        .unwrap();
        page.offer(
            Hit {
                ts: 0,
                ..row("c", "no", 0.9)
            },
            1,
        )
        .unwrap();
        page.offer(
            Hit {
                ts: 0,
                ..row("d", "no", 0.9)
            },
            2,
        )
        .unwrap();
        assert_eq!(shown(page).len(), 4);
        let mut listing = Page::new(5, "score", true, false);
        listing.offer(row("a", "yes", 0.9), 1).unwrap();
        listing.offer(row("b", "yes", 0.9), 2).unwrap();
        assert_eq!(shown(listing).len(), 2);
    }
}
