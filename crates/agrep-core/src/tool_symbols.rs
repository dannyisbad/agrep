//! Sparse symbol grams of folded tool text, indexed in `tools_sym`.
//!
//! The tool dictionary holds runs of letters and digits. This index holds the rest: each
//! character that is no ASCII letter, digit, whitespace or control character
//! (punctuation, symbols, emoji, other scripts), and each adjacent pair holding one.
//! Wherever folded text holds a folded literal it holds each gram of the literal, so the
//! rows holding all of them include every row the literal matches. Python's lowercase
//! shortcut turns `İ` into `i` and U+0307, which the per-character fold does not, so a
//! gram holding `i`, `ı`, `İ` or U+0307 is neither indexed nor asked for.
use anyhow::{Context, Result};
use rusqlite::Connection;
use std::path::{Path, PathBuf};

/// One token per gram, written as itself: every ASCII punctuation character is a token
/// character, and the `ascii` tokenizer keeps every non-ASCII one.
pub(crate) const SCHEMA: &str = r##"CREATE VIRTUAL TABLE IF NOT EXISTS tools_sym USING fts5(text,content='',contentless_delete=1,tokenize="ascii tokenchars '!""#$%&''()*+,-./:;<=>?@[\]^_`{|}~'",detail=none);"##;

/// Grams an expression asks for at most: each is a necessary condition, so a few rare
/// pairs bound the rows as well as all of a long literal's.
const ASKED: usize = 24;

fn breaks(c: char) -> bool {
    c.is_whitespace() || c.is_control() || matches!(c, 'i' | 'ı' | 'İ' | '\u{307}')
}

fn symbolic(c: char) -> bool {
    !c.is_ascii_alphanumeric()
}

/// The FTS document of one folded tool text: its distinct grams, space-separated.
pub(crate) fn document(folded: &str) -> String {
    let mut grams: Vec<(char, Option<char>)> = Vec::new();
    let mut previous: Option<char> = None;
    for c in folded.chars() {
        if breaks(c) {
            previous = None;
            continue;
        }
        if symbolic(c) {
            grams.push((c, None));
        }
        if let Some(p) = previous.filter(|&p| symbolic(p) || symbolic(c)) {
            grams.push((p, Some(c)));
        }
        previous = Some(c);
    }
    grams.sort_unstable();
    grams.dedup();
    let mut out = String::with_capacity(grams.len() * 3);
    for (first, second) in grams {
        if !out.is_empty() {
            out.push(' ');
        }
        out.push(first);
        out.extend(second);
    }
    out
}

/// The grams every row whose folded text holds `folded` holds, as an FTS5 expression;
/// None when there are none. A pair already asks for its symbols, which then need no
/// gram of their own.
pub(crate) fn expression(folded: &str) -> Option<String> {
    let chars: Vec<char> = folded.chars().collect();
    let mut asked: Vec<String> = Vec::new();
    let mut paired = vec![false; chars.len()];
    for (at, pair) in chars.windows(2).enumerate() {
        if pair.iter().all(|&c| !breaks(c)) && pair.iter().any(|&c| symbolic(c)) {
            paired[at] = true;
            paired[at + 1] = true;
            asked.push(pair.iter().collect());
        }
    }
    for (at, &c) in chars.iter().enumerate() {
        if !paired[at] && !breaks(c) && symbolic(c) {
            asked.push(c.to_string());
        }
    }
    asked.sort_unstable();
    asked.dedup();
    if asked.is_empty() {
        return None;
    }
    // Pairs before single characters, which more rows hold.
    asked.sort_by_key(|gram| std::cmp::Reverse(gram.chars().count()));
    asked.truncate(ASKED);
    Some(
        asked
            .iter()
            .map(|gram| format!("\"{}\"", gram.replace('"', "\"\"")))
            .collect::<Vec<_>>()
            .join(" AND "),
    )
}

/// Builds a first build's `tools_sym` in a database of its own, beside the writer's
/// transaction, for [`import`] to copy in when the index is sealed.
pub(crate) struct Builder {
    connection: Connection,
}

/// Where a first build of `index` keeps its symbol index until the seal.
fn side(index: &Path) -> PathBuf {
    let mut path = index.as_os_str().to_owned();
    path.push("-sym");
    PathBuf::from(path)
}

impl Builder {
    pub(crate) fn open(index: &Path) -> Result<Self> {
        let path = side(index);
        match std::fs::remove_file(&path) {
            Err(e) if e.kind() != std::io::ErrorKind::NotFound => return Err(e.into()),
            _ => {}
        }
        let connection = Connection::open(&path)?;
        connection.execute_batch(
            "PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA locking_mode=EXCLUSIVE; PRAGMA cache_size=-65536;",
        )?;
        connection.execute_batch(SCHEMA)?;
        connection.execute_batch("INSERT INTO tools_sym(tools_sym,rank) VALUES('automerge',0);INSERT INTO tools_sym(tools_sym,rank) VALUES('crisismerge',64);INSERT INTO tools_sym(tools_sym,rank) VALUES('hashsize',67108864);BEGIN;")?;
        Ok(Self { connection })
    }
    pub(crate) fn insert(&self, rows: &[(i64, String)]) -> Result<()> {
        let mut insert = self
            .connection
            .prepare_cached("INSERT INTO tools_sym(rowid,text) VALUES(?1,?2)")?;
        for (id, document) in rows {
            insert.execute(rusqlite::params![id, document])?;
        }
        Ok(())
    }
    /// Merges the build into one segment and leaves the settings later writes use.
    pub(crate) fn finish(self) -> Result<()> {
        self.connection
            .execute_batch("INSERT INTO tools_sym(tools_sym) VALUES('optimize');")?;
        crate::fts_segments::configure(&self.connection, "tools_sym")?;
        self.connection.execute_batch("COMMIT;")?;
        self.connection.close().map_err(|(_, e)| e)?;
        Ok(())
    }
}

/// The shadow tables of `tools_sym`, which hold its whole index and settings.
const SHADOWS: [&str; 4] = [
    "tools_sym_data",
    "tools_sym_idx",
    "tools_sym_docsize",
    "tools_sym_config",
];

/// Replaces `c`'s `tools_sym` with the one a [`Builder`] finished for its database, in a
/// transaction of its own, and removes that build. Run outside a transaction, which the
/// attached build could not leave, on a connection that has not used the table, so no
/// cached FTS5 structure goes stale.
pub(crate) fn import(c: &Connection) -> Result<()> {
    let index = c.path().context("the index has no file")?;
    let path = side(Path::new(index));
    anyhow::ensure!(
        path.is_file(),
        "the symbol index build is missing: {}",
        path.display()
    );
    c.execute("ATTACH DATABASE ?1 AS symbols", [path.to_string_lossy()])?;
    let copied = (|| -> Result<()> {
        c.execute_batch("BEGIN IMMEDIATE")?;
        for table in SHADOWS {
            c.execute_batch(&format!(
                "DELETE FROM main.{table}; INSERT INTO main.{table} SELECT * FROM symbols.{table};"
            ))?;
        }
        c.execute_batch("COMMIT")?;
        Ok(())
    })();
    if copied.is_err() && !c.is_autocommit() {
        c.execute_batch("ROLLBACK")?;
    }
    c.execute_batch("DETACH DATABASE symbols")?;
    copied?;
    std::fs::remove_file(&path)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tokens(document: &str) -> std::collections::HashSet<String> {
        document.split(' ').map(str::to_owned).collect()
    }

    #[test]
    fn documents_hold_every_gram_a_literal_asks_for() {
        let doc = tokens(&document("we build libc++ and o(n log n) 🚀 ok"));
        for literal in ["c++", "o(n", "n)", "🚀", "++", "+"] {
            let expression = expression(literal).unwrap();
            for gram in expression.split(" AND ") {
                assert!(
                    doc.contains(gram.trim_matches('"')),
                    "{literal}: {gram} missing"
                );
            }
        }
        assert_eq!(expression("c++").as_deref(), Some("\"++\" AND \"c+\""));
        assert_eq!(
            expression("a\"b").as_deref(),
            Some("\"\"\"b\" AND \"a\"\"\"")
        );
        assert_eq!(expression("i(").as_deref(), Some("\"(\""));
        for literal in ["abc", "a b", "ii", "", "\u{1b}"] {
            assert!(expression(literal).is_none(), "{literal:?}");
        }
    }

    /// Every substring of every text, as a literal, finds each gram it asks for in the
    /// text's document: the index never rules out a row that holds the literal.
    #[test]
    fn every_substring_finds_its_grams() {
        let texts = [
            "fn main() { let x = a.b(c++); } // 🚀 ok",
            "O(n log n) -> std::sync::Arc<T>; e-mail: it's 100% #tag @owner/review",
            "tab\there\nnew\u{0}nul \u{1b}[0m esc café 日本語 ✅ —… \u{a0}nbsp\u{85}nel",
            "\"quoted\" 'single' `tick` [x] {y} $HOME ~/.git \\path a/b",
            "ſ K Σσς İı i\u{307} ß ẞ ﬁ ǅ ΐ",
            "👩‍💻 family 👨‍👩‍👧 flags 🇯🇵 keycap 1️⃣ variation ✌︎",
        ];
        for text in texts {
            let folded = crate::store::search_fold(text);
            let doc = tokens(&document(&folded));
            let chars: Vec<char> = folded.chars().collect();
            for start in 0..chars.len() {
                for end in start + 1..=(start + 8).min(chars.len()) {
                    let literal: String = chars[start..end].iter().collect();
                    let Some(expression) = expression(&literal) else {
                        continue;
                    };
                    for gram in expression.split(" AND ") {
                        let gram = gram[1..gram.len() - 1].replace("\"\"", "\"");
                        assert!(
                            doc.contains(&gram),
                            "{literal:?} needs {gram:?} in {text:?}"
                        );
                    }
                }
            }
        }
    }

    /// The tokenizer reads each gram of a document, and each gram an expression asks
    /// for, as one token: the query finds exactly the rows holding the literal's grams.
    #[test]
    fn the_table_tokenizes_grams_whole() {
        let c = Connection::open_in_memory().unwrap();
        c.execute_batch(SCHEMA).unwrap();
        let texts = [
            "libc++ \"quoted\" o(n) 🚀",
            "c + + \"q\" o (n) 🚀🚀",
            "path\\to it's `tick` [x] {y} <z> 日本語",
        ];
        for (id, text) in texts.iter().enumerate() {
            c.execute(
                "INSERT INTO tools_sym(rowid,text) VALUES(?1,?2)",
                rusqlite::params![id as i64, document(&crate::store::search_fold(text))],
            )
            .unwrap();
        }
        let rows = |literal: &str| -> Vec<i64> {
            c.prepare("SELECT rowid FROM tools_sym WHERE tools_sym MATCH ? ORDER BY rowid")
                .unwrap()
                .query_map([expression(literal).unwrap()], |r| r.get(0))
                .unwrap()
                .collect::<rusqlite::Result<_>>()
                .unwrap()
        };
        assert_eq!(rows("c++"), [0]);
        assert_eq!(rows("\"q"), [0, 1]);
        assert_eq!(rows("o(n)"), [0]);
        assert_eq!(rows("🚀🚀"), [1]);
        assert_eq!(rows("🚀"), [0, 1]);
        assert_eq!(rows("h\\t"), [2]);
        assert_eq!(rows("t's"), [2]);
        assert_eq!(rows("`t"), [2]);
        assert_eq!(rows("[x]"), [2]);
        assert_eq!(rows("<z>"), [2]);
        assert_eq!(rows("本語"), [2]);
        assert_eq!(rows("{y}"), [2]);
    }
}
