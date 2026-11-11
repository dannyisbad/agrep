//! Forked chats. A pi or omp fork copies its parent's history, ids and times included, so
//! one message can sit in several chats; a page shows it once. A Codex fork child's
//! rollout starts by replaying its parent's transcript up to the fork; ingest keeps that
//! history once, under the parent, so a search scoped to the child reaches it there.
use crate::query::{Fork, Hit, Inherited, Results, Timings};
use anyhow::Result;
use rusqlite::{Connection, OptionalExtension};
use std::{
    collections::{HashMap, HashSet},
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
    /// Whether `h` is a copy of this message from a chat it holds no copy from yet.
    fn takes(&self, h: &Hit) -> bool {
        self.holds_copy_of(h) && !self.copies.iter().any(|copy| copy.hit.session == h.session)
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

/// Ranked keyword rows as page rows, in rank order: each message several chats hold is
/// one row at its best copy's rank. Repeats within one chat stay rows of their own.
fn group(hits: Vec<Hit>) -> Vec<Message> {
    let mut messages: Vec<Message> = Vec::with_capacity(hits.len());
    // Rows by agent, speaker and time; only those can be copies of one another.
    let mut stamped: HashMap<u64, Vec<usize>> = HashMap::new();
    for hit in hits {
        if hit.ts != 0 {
            let mut stamp = std::collections::hash_map::DefaultHasher::new();
            (hit.ts, &hit.agent, &hit.who).hash(&mut stamp);
            let held = stamped.entry(stamp.finish()).or_default();
            if let Some(&at) = held.iter().find(|&&at| messages[at].takes(&hit)) {
                messages[at].copies.push(Copy { hit, last: 0 });
                continue;
            }
            held.push(messages.len());
        }
        messages.push(Message::new(hit, 0));
    }
    messages
}

/// Adds to each page row the copies other chats hold that may rank below `last`, the
/// last row the page ranked, as `copies` finds them. A copy joins the first row with no
/// copy from its chat, as it would on a page ranked deep enough to hold it.
fn gather(
    messages: &mut [Message],
    last: &Hit,
    sort: &str,
    copies: &mut impl FnMut(&Hit) -> Result<Vec<Hit>>,
) -> Result<()> {
    let mut placed: HashSet<(bool, i64)> = messages
        .iter()
        .flat_map(|message| &message.copies)
        .map(|copy| (copy.hit.who == "tool", copy.hit.rowid))
        .collect();
    for message in messages.iter_mut() {
        if !crate::query::copy_may_rank_below(message.shown(), last, sort) {
            continue;
        }
        for hit in copies(message.shown())? {
            let row = (hit.who == "tool", hit.rowid);
            if !placed.contains(&row) && message.takes(&hit) {
                placed.insert(row);
                message.copies.push(Copy { hit, last: 0 });
            }
        }
    }
    Ok(())
}

/// Shows each folded row from the most recently active chat holding it.
fn show_newest(c: &Connection, messages: &mut [Message]) -> Result<()> {
    if messages.iter().all(|message| message.copies.len() == 1) {
        return Ok(());
    }
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
    Ok(())
}

/// A keyword page of `limit` rows (0: every row), each message several chats hold folded
/// into one row at its best copy's rank and shown from the most recently active of those
/// chats. `search(n)` ranks the best `n` matching rows; `copies(row)` finds the copies
/// other chats hold of `row`. Copies take no row of the page, so a page they shrank is
/// ranked again, deeper, until it holds `limit` rows or every match. Copies rank alike
/// until their chats break the tie, so a page can end among them: a row tied that far
/// with the last row ranked takes its copies from `copies`, and no row's fold depends on
/// how deep the page ranked.
pub fn keyword_page(
    c: &Connection,
    limit: usize,
    sort: &str,
    mut search: impl FnMut(usize) -> Result<Results>,
    mut copies: impl FnMut(&Hit) -> Result<Vec<Hit>>,
) -> Result<Results> {
    let mut depth = limit;
    let mut ranked = 0;
    let mut spent = Timings::default();
    loop {
        let mut result = search(depth)?;
        let rows = result.hits.len();
        let more = result.total_basis != "exact" || result.total > rows;
        let last = result.hits.last().filter(|_| more).cloned();
        let mut messages = group(std::mem::take(&mut result.hits));
        let shown = messages.len();
        if limit == 0 || shown >= limit || shown == rows || !more || rows <= ranked {
            if limit > 0 {
                messages.truncate(limit);
            }
            if let Some(last) = &last {
                let _span = crate::semantic_profile::span("fork_copies");
                gather(&mut messages, last, sort, &mut copies)?;
            }
            show_newest(c, &mut messages)?;
            result.hits = messages.into_iter().map(Message::into_hit).collect();
            // A floor counts the rows the ranking read; the copies found after it match too.
            result.total = result.total.max(result.hits.iter().map(Hit::rows).sum());
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
    fn fold(c: &Connection, hits: Vec<Hit>) -> Vec<Hit> {
        let mut messages = group(hits);
        show_newest(c, &mut messages).unwrap();
        messages.into_iter().map(Message::into_hit).collect()
    }
    /// The best `n` of `ranked` (0: all), a total exact over `ranked`.
    fn ranking(ranked: &[Hit], n: usize) -> Result<Results> {
        let mut r = Results::default();
        r.hits = ranked
            .iter()
            .take(if n == 0 { usize::MAX } else { n })
            .cloned()
            .collect();
        r.total = ranked.len();
        r.total_basis = "exact".into();
        Ok(r)
    }
    /// The rows of `ranked` that copy `shown` from other chats.
    fn copies_in(ranked: &[Hit], shown: &Hit) -> Vec<Hit> {
        ranked
            .iter()
            .filter(|h| same_message(h, shown) && h.session != shown.session)
            .cloned()
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
        let folded = fold(&c, hits);
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
        let mut depths = Vec::new();
        let page = keyword_page(
            &c,
            3,
            "score",
            |n| {
                depths.push(n);
                ranking(&ranked, n)
            },
            |_| Ok(Vec::new()),
        )
        .unwrap();
        assert_eq!(depths, [3, 9]);
        let shown: Vec<_> = listed(&page.hits)
            .into_iter()
            .map(|(session, _, forks)| (session, forks.len()))
            .collect();
        assert_eq!(shown, [("c", 2), ("d", 0), ("e", 0)]);
        assert_eq!(page.total, 6);

        depths.clear();
        let page = keyword_page(
            &c,
            2,
            "score",
            |n| {
                depths.push(n);
                ranking(&ranked[3..], n)
            },
            |_| Ok(Vec::new()),
        )
        .unwrap();
        assert_eq!(depths, [2]);
        assert_eq!(page.hits.len(), 2);
    }
    /// A page ranked too shallow to hold a row's copies looks them up, so the row folds
    /// alike at every depth; a row ranked clear of the last one is not looked up.
    #[test]
    fn a_row_folds_alike_however_deep_its_page_ranked() {
        let c = chats(&[("root", 10), ("fork-a", 30), ("fork-b", 20)]);
        let mut ranked = vec![
            row("fork-a", "shared", 5),
            row("fork-b", "shared", 5),
            row("root", "shared", 5),
            row("other", "own", 4),
            row("older", "old", 3),
        ];
        for (id, hit) in ranked.iter_mut().enumerate() {
            hit.rowid = id as i64 + 1;
        }
        let page = |limit: usize| {
            let mut looked = Vec::new();
            let page = keyword_page(
                &c,
                limit,
                "score",
                |n| ranking(&ranked, n),
                |shown| {
                    looked.push(shown.session.clone());
                    Ok(copies_in(&ranked, shown))
                },
            )
            .unwrap();
            (page, looked)
        };
        let (one, looked) = page(1);
        assert_eq!(looked, ["fork-a"]);
        assert_eq!(
            listed(&one.hits),
            [("fork-a", "shared", vec!["fork-b", "root"])]
        );
        assert_eq!(one.total, 5);
        let (two, looked) = page(2);
        assert_eq!(looked, ["other"]);
        assert_eq!(listed(&two.hits)[0], listed(&one.hits)[0]);
        assert_eq!(listed(&two.hits)[1], ("other", "own", vec![]));
        let (all, looked) = page(0);
        assert!(looked.is_empty());
        assert_eq!(listed(&all.hits)[..2], listed(&two.hits)[..]);
    }
    /// A copy looked up joins the first row holding none from its chat, as on a page
    /// ranked deep enough to hold it, and a repeat within one chat stays a row of its own.
    #[test]
    fn a_looked_up_copy_joins_the_first_row_it_fits() {
        let c = chats(&[]);
        let mut ranked: Vec<Hit> = ["a", "a", "b", "b", "c"]
            .iter()
            .map(|s| row(s, "same", 5))
            .collect();
        for (id, hit) in ranked.iter_mut().enumerate() {
            hit.rowid = id as i64 + 1;
        }
        let page = |limit| {
            keyword_page(
                &c,
                limit,
                "score",
                |n| ranking(&ranked, n),
                |shown| Ok(copies_in(&ranked, shown)),
            )
            .unwrap()
            .hits
        };
        let deep = page(0);
        assert_eq!(
            listed(&deep),
            [("c", "same", vec!["b", "a"]), ("b", "same", vec!["a"])]
        );
        assert_eq!(listed(&page(2)), listed(&deep));
    }
}
