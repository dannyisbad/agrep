//! The row texts the reparse found and the index holds, keyed by chat, speaker and the
//! text's SHA-256, so a whole history fits in memory without its texts.
use agrep_core::store;
use anyhow::Result;
use rayon::prelude::*;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::HashMap;

pub(super) type TextDigest = [u8; 32];

pub(super) fn text_digest(text: &str) -> TextDigest {
    Sha256::digest(text.as_bytes()).into()
}

#[derive(Clone, Copy, PartialEq, Eq, Hash)]
struct RowKey {
    chat: u32,
    who: u32,
    text: TextDigest,
}

/// A row's copies: the most any one parsed source holds, the total over every parsed
/// source holding its chat, and the index's searchable copies and the stored ones it
/// keeps out of search by design (see `Chat::hides`).
#[derive(Default)]
struct Copies {
    most: u32,
    all: u32,
    visible: u32,
    hidden: u32,
}

/// What the reparse found of one chat: how many sources hold a copy of it (none for a
/// chat only the index holds).
struct Chat {
    agent: Box<str>,
    session: Box<str>,
    copies: u32,
}

impl Chat {
    /// A chat several sources hold is searched as one canonical replay of its copies, so
    /// the copies it did not pick, each a duplicate of a searched row, are stored hidden.
    fn hides(&self) -> bool {
        self.copies > 1
    }
}

/// One parsed source's rows: each chat's texts by speaker, with the copies the source
/// holds of each.
pub(super) type SourceRows =
    HashMap<(&'static str, Box<str>), HashMap<(Box<str>, TextDigest), u32>>;

#[derive(Default)]
pub(super) struct Tally {
    ids: HashMap<Box<str>, HashMap<Box<str>, u32>>,
    chats: Vec<Chat>,
    whos: Vec<Box<str>>,
    rows: HashMap<RowKey, Copies>,
}

impl Tally {
    fn find(&self, agent: &str, session: &str) -> Option<u32> {
        self.ids.get(agent)?.get(session).copied()
    }

    fn chat(&mut self, agent: &str, session: &str) -> u32 {
        if let Some(id) = self.find(agent, session) {
            return id;
        }
        let id = u32::try_from(self.chats.len()).expect("fewer than 2^32 chats");
        self.ids
            .entry(agent.into())
            .or_default()
            .insert(session.into(), id);
        self.chats.push(Chat {
            agent: agent.into(),
            session: session.into(),
            copies: 0,
        });
        id
    }

    fn who(&mut self, who: &str) -> u32 {
        let at = self
            .whos
            .iter()
            .position(|w| &**w == who)
            .unwrap_or_else(|| {
                self.whos.push(who.into());
                self.whos.len() - 1
            });
        at as u32
    }

    /// Native replay copies contribute one copy, while repeated turns within a source
    /// remain separate evidence: a source's count of a text is never collapsed to one.
    pub(super) fn add_source(&mut self, rows: SourceRows) {
        for ((agent, session), texts) in rows {
            let chat = self.chat(agent, &session);
            self.chats[chat as usize].copies += 1;
            for ((who, text), count) in texts {
                let key = RowKey {
                    chat,
                    who: self.who(&who),
                    text,
                };
                let copies = self.rows.entry(key).or_default();
                copies.most = copies.most.max(count);
                copies.all += count;
            }
        }
    }

    /// Tallies `table`'s searchable rows of the selected agents; returns their count.
    pub(super) fn add_searchable(
        &mut self,
        c: &rusqlite::Connection,
        table: &str,
        selected: &impl Fn(&str) -> bool,
    ) -> Result<usize> {
        let mut stored = Stored::new(table);
        let mut q = c.prepare(&format!(
            "SELECT agent,session,who,text FROM {table} WHERE visible=1"
        ))?;
        let mut rows = q.query([])?;
        let mut visible = 0;
        while let Some(r) = rows.next()? {
            let agent = r.get_ref(0)?.as_str()?;
            if !selected(agent) {
                continue;
            }
            visible += 1;
            let row = StoredRow {
                chat: self.chat(agent, r.get_ref(1)?.as_str()?),
                who: self.who(r.get_ref(2)?.as_str()?),
                hidden: false,
                text: r.get(3)?,
            };
            stored.push(self, row)?;
        }
        stored.flush(self)?;
        Ok(visible)
    }

    /// Tallies `table`'s rows that the reparsed chats hide by design. Only such chats
    /// are read, through the session index.
    pub(super) fn add_hidden(
        &mut self,
        c: &rusqlite::Connection,
        table: &str,
        selected: &impl Fn(&str) -> bool,
    ) -> Result<()> {
        let mut stored = Stored::new(table);
        let mut q = c.prepare(&format!(
            "SELECT who,text FROM {table} WHERE agent=?1 AND session=?2 AND visible=0"
        ))?;
        for chat in 0..self.chats.len() {
            let held = &self.chats[chat];
            if !held.hides() || !selected(&held.agent) {
                continue;
            }
            let mut rows = q.query([&*held.agent, &*held.session])?;
            while let Some(r) = rows.next()? {
                let row = StoredRow {
                    chat: chat as u32,
                    who: self.who(r.get_ref(0)?.as_str()?),
                    hidden: true,
                    text: r.get(1)?,
                };
                stored.push(self, row)?;
            }
        }
        stored.flush(self)
    }

    /// The parsed rows each source should have put in the index.
    pub(super) fn expected_rows(&self) -> usize {
        self.rows.values().map(|n| n.most as usize).sum()
    }

    /// Parsed rows the index lacks copies of, then searchable rows no parsed source holds
    /// as often, each in chat and speaker order.
    pub(super) fn gaps(&self) -> Vec<Value> {
        let mut missing = vec![];
        let mut absent = vec![];
        for (key, n) in &self.rows {
            let found = n.visible + n.hidden;
            if found < n.most {
                missing.push((key, n.most - found));
            }
            if n.all < n.visible {
                absent.push((key, n.visible - n.all));
            }
        }
        let mut gaps = Vec::with_capacity(missing.len() + absent.len());
        for (found, reason) in [
            (missing, "parsed row missing from index"),
            (absent, "indexed row absent from parsed sources"),
        ] {
            let mut found: Vec<_> = found
                .into_iter()
                .map(|(key, rows)| {
                    let chat = &self.chats[key.chat as usize];
                    (
                        &*chat.agent,
                        &*chat.session,
                        &*self.whos[key.who as usize],
                        rows,
                    )
                })
                .collect();
            found.sort_unstable();
            gaps.extend(found.into_iter().map(|(agent, session, who, rows)| {
                json!({"agent":agent,"session":session,"who":who,"rows":rows,"reason":reason})
            }));
        }
        gaps
    }
}

/// A row read from the index before its text is decoded.
struct StoredRow {
    chat: u32,
    who: u32,
    hidden: bool,
    text: Vec<u8>,
}

/// Stored rows waiting for their texts to be decoded and digested, a batch at a time on
/// every core.
struct Stored<'a> {
    table: &'a str,
    rows: Vec<StoredRow>,
    bytes: usize,
}

impl<'a> Stored<'a> {
    const ROWS: usize = 4096;
    const BYTES: usize = 32 << 20;

    fn new(table: &'a str) -> Self {
        Self {
            table,
            rows: Vec::with_capacity(Self::ROWS),
            bytes: 0,
        }
    }

    fn push(&mut self, tally: &mut Tally, row: StoredRow) -> Result<()> {
        self.bytes += row.text.len();
        self.rows.push(row);
        if self.rows.len() >= Self::ROWS || self.bytes >= Self::BYTES {
            self.flush(tally)?;
        }
        Ok(())
    }

    /// Tallies the batch in the order it was read, so the first undecodable row fails.
    fn flush(&mut self, tally: &mut Tally) -> Result<()> {
        let table = self.table;
        let digested: Vec<_> = self
            .rows
            .par_drain(..)
            .map(|row| {
                let text = store::decode(table, row.text).map(|text| text_digest(&text));
                (row.chat, row.who, row.hidden, text)
            })
            .collect();
        self.bytes = 0;
        for (chat, who, hidden, text) in digested {
            let text = text?;
            let copies = tally.rows.entry(RowKey { chat, who, text }).or_default();
            if hidden {
                copies.hidden += 1;
            } else {
                copies.visible += 1;
            }
        }
        Ok(())
    }
}
