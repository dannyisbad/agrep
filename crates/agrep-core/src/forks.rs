//! Forked chats. A pi or omp fork copies its parent's history, ids and times included, so
//! one message can sit in several chats; a page shows it once. A Codex fork child's
//! rollout starts by replaying its parent's transcript up to the fork; ingest keeps that
//! history once, under the parent, so a search scoped to the child reaches it there.
use crate::query::{Fork, Hit, Inherited, Results, Timings};
use anyhow::Result;
use rusqlite::{Connection, OptionalExtension};
use std::{
    collections::HashMap,
    hash::{Hash, Hasher},
    path::Path,
};

/// Whether `a` and `b` are copies of one message: a fork's copy keeps the agent, speaker,
/// text and time. Rows without a time are never copies.
pub fn same_message(a: &Hit, b: &Hit) -> bool {
    a.ts != 0 && a.ts == b.ts && a.agent == b.agent && a.who == b.who && a.text == b.text
}

/// A message's copy in one chat, and when that chat was last active.
pub(crate) struct Copy {
    pub(crate) hit: Hit,
    pub(crate) last: i64,
}

/// One page row: the copies of one message, newest chat first. The copy in the most
/// recently active chat represents them all.
pub(crate) struct Message {
    pub(crate) copies: Vec<Copy>,
}
impl Message {
    pub(crate) fn new(hit: Hit, last: i64) -> Self {
        Self {
            copies: vec![Copy { hit, last }],
        }
    }
    pub(crate) fn shown(&self) -> &Hit {
        &self.copies[0].hit
    }
    /// Whether `h` is another chat's copy of this message.
    pub(crate) fn holds_copy_of(&self, h: &Hit) -> bool {
        same_message(self.shown(), h)
    }
    pub(crate) fn add(&mut self, copy: Copy) {
        self.copies.push(copy);
        self.order();
    }
    fn order(&mut self) {
        self.copies.sort_by(|a, b| {
            b.last
                .cmp(&a.last)
                .then_with(|| b.hit.session.cmp(&a.hit.session))
        });
    }
    /// The shown copy, listing the others.
    pub(crate) fn into_hit(mut self) -> Hit {
        let mut copies = self.copies.drain(..);
        let mut shown = copies.next().expect("a message holds a copy").hit;
        shown.forks = copies
            .map(|copy| Fork {
                session: copy.hit.session,
                project: copy.hit.project,
                turn: copy.hit.turn,
                handle: copy.hit.handle,
            })
            .collect();
        shown
    }
}

/// A ranked keyword page with each message several chats hold folded into one row at its
/// best copy's rank, shown from the most recently active of those chats. Repeats within
/// one chat stay rows of their own.
pub fn fold(c: &Connection, hits: Vec<Hit>) -> Result<Vec<Hit>> {
    let mut messages: Vec<Message> = Vec::with_capacity(hits.len());
    // Rows by agent, speaker and time; only those can be copies of one another.
    let mut stamped: HashMap<u64, Vec<usize>> = HashMap::new();
    let mut folded = false;
    for hit in hits {
        if hit.ts != 0 {
            let mut stamp = std::collections::hash_map::DefaultHasher::new();
            (hit.ts, &hit.agent, &hit.who).hash(&mut stamp);
            let held = stamped.entry(stamp.finish()).or_default();
            let copy_of = held.iter().copied().find(|&at| {
                let message = &messages[at];
                message.holds_copy_of(&hit)
                    && !message
                        .copies
                        .iter()
                        .any(|copy| copy.hit.session == hit.session)
            });
            if let Some(at) = copy_of {
                messages[at].copies.push(Copy { hit, last: 0 });
                folded = true;
                continue;
            }
            held.push(messages.len());
        }
        messages.push(Message::new(hit, 0));
    }
    if folded {
        let mut last = c.prepare_cached("SELECT last_ts FROM sessions WHERE agent=?1 AND id=?2")?;
        for message in messages.iter_mut().filter(|m| m.copies.len() > 1) {
            for copy in &mut message.copies {
                copy.last = last
                    .query_row([&copy.hit.agent, &copy.hit.session], |r| r.get(0))
                    .optional()?
                    .unwrap_or(0);
            }
            message.order();
        }
    }
    Ok(messages.into_iter().map(Message::into_hit).collect())
}

/// A keyword page of `limit` rows (0: every row) whose fork copies fold (see `fold`).
/// `search(n)` ranks the best `n` matching rows. Copies take no row of the page, so a page
/// they shrank is ranked again, deeper, until it holds `limit` rows or every match.
pub fn keyword_page(
    c: &Connection,
    limit: usize,
    mut search: impl FnMut(usize) -> Result<Results>,
) -> Result<Results> {
    let mut depth = limit;
    let mut ranked = 0;
    let mut spent = Timings::default();
    loop {
        let mut result = search(depth)?;
        let rows = result.hits.len();
        let more = result.total_basis != "exact" || result.total > rows;
        result.hits = fold(c, std::mem::take(&mut result.hits))?;
        let shown = result.hits.len();
        if limit == 0 || shown >= limit || shown == rows || !more || rows <= ranked {
            if limit > 0 {
                result.hits.truncate(limit);
            }
            result.timings.add(&spent);
            result.timings.total_ms += spent.total_ms;
            return Ok(result);
        }
        spent.add(&result.timings);
        spent.total_ms += result.timings.total_ms;
        ranked = rows;
        // Deeper by the missing rows at the rate copies took them.
        depth += (limit - shown) * depth.div_ceil(shown);
    }
}

/// Fork chains deeper than this are not followed.
const MAX_DEPTH: usize = 16;

/// The parent rows `session` inherited, nearest parent first: each ancestor's rows up to
/// the moment its descendant forked away. Empty for a chat that is not a fork child.
pub fn inherited(c: &Connection, session: &str) -> Result<Vec<Inherited>> {
    let mut chain: Vec<Inherited> = Vec::new();
    let mut child = session.to_owned();
    let mut until = i64::MAX;
    while chain.len() < MAX_DEPTH {
        let Some(parent) = c
            .prepare_cached(
                "SELECT parent FROM sessions WHERE agent='codex' AND id=?1 AND parent!=''",
            )?
            .query_row([&child], |r| r.get::<_, String>(0))
            .optional()?
        else {
            break;
        };
        if parent == session || chain.iter().any(|link| link.session == parent) {
            break;
        }
        let Some(forked) = fork_time(c, &child)? else {
            break;
        };
        until = until.min(forked);
        chain.push(Inherited {
            session: parent.clone(),
            until,
        });
        child = parent;
    }
    Ok(chain)
}

/// When `session`'s rollout replays its parent's history: the moment it forked.
fn fork_time(c: &Connection, session: &str) -> Result<Option<i64>> {
    let paths: Vec<String> = c
        .prepare_cached("SELECT s.path FROM session_sources ss JOIN sources s ON s.rowid=ss.source WHERE ss.agent='codex' AND ss.session=?1 ORDER BY s.path")?
        .query_map([session], |r| r.get(0))?
        .collect::<rusqlite::Result<_>>()?;
    Ok(paths
        .iter()
        .find_map(|path| crate::ingest::codex::fork_time(Path::new(path))))
}

/// The stderr line saying a `--chat` scope took in the history `chat` inherited.
pub fn notice(chat: &str, inherited: &[Inherited]) -> Option<String> {
    let parents: Vec<String> = inherited
        .iter()
        .map(|link| crate::render::safe(&link.session))
        .collect();
    (!parents.is_empty()).then(|| {
        format!(
            "--chat {} is a fork: also searching the history it inherited from {} (up to the fork)",
            crate::render::safe(chat),
            parents.join(", ")
        )
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    fn row(session: &str, text: &str, ts: i64) -> Hit {
        Hit {
            session: session.into(),
            agent: "pi".into(),
            who: "user".into(),
            ts,
            text: text.into(),
            handle: format!("@{session}:1.0000"),
            ..Hit::default()
        }
    }
    fn chats(last: &[(&str, i64)]) -> Connection {
        let c = Connection::open_in_memory().unwrap();
        c.execute_batch("CREATE TABLE sessions(agent TEXT,id TEXT,last_ts INTEGER)")
            .unwrap();
        for (id, ts) in last {
            c.execute(
                "INSERT INTO sessions VALUES('pi',?1,?2)",
                rusqlite::params![id, ts],
            )
            .unwrap();
        }
        c
    }
    fn listed(hits: &[Hit]) -> Vec<(&str, &str, Vec<&str>)> {
        hits.iter()
            .map(|h| {
                let forks = h.forks.iter().map(|f| f.session.as_str()).collect();
                (h.session.as_str(), h.text.as_str(), forks)
            })
            .collect()
    }
    /// Copies fold at their best copy's rank and show the most recently active chat; equal
    /// words at another time, a row without a time, and a repeat within one chat stay rows.
    #[test]
    fn copies_of_one_message_fold_into_the_newest_chat() {
        let c = chats(&[("root", 10), ("fork-a", 30), ("fork-b", 20)]);
        let hits = vec![
            row("root", "shared", 5),
            row("other", "own", 6),
            row("fork-a", "shared", 5),
            row("fork-b", "shared", 5),
            row("later", "shared", 7),
            row("root", "shared", 5),
            row("x", "untimed", 0),
            row("y", "untimed", 0),
        ];
        let folded = fold(&c, hits).unwrap();
        assert_eq!(
            listed(&folded),
            [
                ("fork-a", "shared", vec!["fork-b", "root"]),
                ("other", "own", vec![]),
                ("later", "shared", vec![]),
                ("root", "shared", vec![]),
                ("x", "untimed", vec![]),
                ("y", "untimed", vec![]),
            ]
        );
        assert_eq!(folded[0].forks[1].handle, "@root:1.0000");
        assert_eq!(folded.iter().map(Hit::rows).sum::<usize>(), 8);
    }
    /// A page the copies shrank is ranked again, deeper, and cut to its limit; a page
    /// without copies is ranked once.
    #[test]
    fn a_page_shrunk_by_copies_ranks_deeper() {
        let c = chats(&[]);
        let ranked: Vec<Hit> = ["a", "b", "c"]
            .iter()
            .map(|s| row(s, "shared", 9))
            .chain(["d", "e", "f"].iter().map(|s| row(s, s, 8)))
            .collect();
        let results = |hits: &[Hit], n: usize| -> Result<Results> {
            let mut r = Results::default();
            r.hits = hits.iter().take(n).cloned().collect();
            r.total = hits.len();
            r.total_basis = "exact".into();
            Ok(r)
        };
        let mut depths = Vec::new();
        let page = keyword_page(&c, 3, |n| {
            depths.push(n);
            results(&ranked, n)
        })
        .unwrap();
        assert_eq!(depths, [3, 9]);
        let shown: Vec<_> = listed(&page.hits)
            .into_iter()
            .map(|(session, _, forks)| (session, forks.len()))
            .collect();
        assert_eq!(shown, [("c", 2), ("d", 0), ("e", 0)]);
        assert_eq!(page.total, 6);

        depths.clear();
        let page = keyword_page(&c, 2, |n| {
            depths.push(n);
            results(&ranked[3..], n)
        })
        .unwrap();
        assert_eq!(depths, [2]);
        assert_eq!(page.hits.len(), 2);
    }
}
